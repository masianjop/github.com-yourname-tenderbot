from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from asyncio import to_thread

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from rostender_filter_parser import fetch_rostender_tenders_filtered
from mce_filter import analyze_tender
from gpt_client import ask_gpt_about_tenders
from config_store import (
    get_keywords,
    set_keywords,
    get_exclude_keywords,
    set_exclude_keywords,
    get_city,
    set_city,
    get_gpt_filter_text,
    set_gpt_filter_text,
)

# ================== CONFIG & LOGGING ==================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

log = logging.getLogger(__name__)

ROST_MAX_PAGES = 2
MAX_GPT_TENDERS = 12  # максимум тендеров, которые отправляем в GPT за один запуск


# ================== МЕНЮ ==================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🔍 Найти тендеры (Ростендер)", callback_data="rost_mce"),
        ],
        [
            InlineKeyboardButton("⚙ Настройки фильтров", callback_data="settings"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ================== КОМАНДЫ ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я бот для проверки тендеров.\n\n"
        "Поддерживаемые функции:\n"
        "• Поиск тендеров Ростендера\n"
        "• Локальный фильтр МЦЭ\n"
        "• Фильтр GPT\n\n"
        "Настройки можно менять прямо через Telegram.\n\n"
        "Команды:\n"
        "/filters — показать текущие фильтры\n"
        "/set_keywords — ключевые слова\n"
        "/set_exclude — слова-исключения\n"
        "/set_city — город\n"
        "/set_gpt_filter — фильтр GPT\n"
        "/rost_mce — запустить поиск\n"
    )
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def rost_mce(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False):
    """
    1) Тянем тендеры с Ростендера.
    2) Прогоняем через локальный фильтр MCE.
    3) Если MCE никого не нашёл — отдаём в GPT первые MAX_GPT_TENDERS тендеров.
    4) GPT решает, что подходит.
    """
    chat_id = update.effective_chat.id

    if from_callback:
        msg = await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=update.callback_query.message.message_id,
            text="⏳ Загружаю тендеры Ростендера...",
        )
    else:
        msg = await context.bot.send_message(chat_id, "⏳ Загружаю тендеры Ростендера...")

    # --- грузим тендеры в отдельном потоке ---
    def load_tenders():
        return fetch_rostender_tenders_filtered(
            days=3,
            max_pages=ROST_MAX_PAGES,
            include_words=get_keywords(),
            exclude_words=get_exclude_keywords(),
            city_filter=get_city(),
        )

    tenders = await to_thread(load_tenders)

    if not tenders:
        await msg.edit_text("⚠ За последние 3 дня новых тендеров не найдено.")
        return

    log.info("Всего тендеров из Ростендера после базового фильтра: %d", len(tenders))

    # ---------------- ЛОКАЛЬНЫЙ ФИЛЬТР МЦЭ ----------------
    local_items: list[tuple[object, object | None]] = []

    for t in tenders:
        desc = getattr(t, "detail_text", "") or getattr(t, "raw_block", "") or ""
        customer = getattr(t, "customer", None) or (t.city or "") or (t.region or "")

        local = analyze_tender(
            code=t.number,
            title=t.title,
            url=t.url,
            customer=customer,
            description=desc,
        )

        is_local_match = getattr(local, "is_local_match", getattr(local, "is_match", False))

        if is_local_match:
            local_items.append((t, local))

    log.info("Локальный фильтр МЦЭ: нашёл %d тендеров", len(local_items))

    # сортируем по приоритету и дате
    if local_items:
        local_items.sort(
            key=lambda pair: (getattr(pair[1], "priority_level", 0), pair[0].published, pair[0].number),
            reverse=True,
        )
        local_items = local_items[:MAX_GPT_TENDERS]
        await msg.edit_text(
            f"🤖 Локальный фильтр МЦЭ нашёл {len(local_items)} кандидатов. Отправляю их в ИИ..."
        )
    else:
        # Fallback: если фильтр МЦЭ никого не нашёл — всё равно что-то отправим в GPT,
        # чтобы не сидеть с пустым результатом.
        fallback_count = min(MAX_GPT_TENDERS, len(tenders))
        local_items = [(t, None) for t in tenders[:fallback_count]]
        log.info(
            "Локальный фильтр МЦЭ не нашёл подходящих тендеров. "
            "Отправляю в GPT первые %d тендеров без локального отбора.",
            fallback_count,
        )
        await msg.edit_text(
            "⚠ Локальный фильтр МЦЭ не нашёл подходящих тендеров.\n"
            f"Отправляю в ИИ первые {fallback_count} тендеров для проверки."
        )

    # --- GPT в отдельном потоке ---
    def gpt_job():
        return ask_gpt_about_tenders(local_items)

    gpt_results = await to_thread(gpt_job)

    if not gpt_results:
        await msg.edit_text("⚠ ИИ не вернул ни одного подходящего тендера (или произошла ошибка).")
        return

    good_codes = {r.code for r in gpt_results if r.is_match}
    good_reasons = {r.code: r.reason for r in gpt_results if r.is_match}

    good_tenders = [t for (t, _local) in local_items if t.number in good_codes]

    if not good_tenders:
        await msg.edit_text("❌ ИИ не нашёл подходящих тендеров среди кандидатов.")
        return

    await msg.edit_text(
        f"🟢 ИИ нашёл {len(good_tenders)} подходящих тендер(ов). Отправляю детальный список..."
    )

    for t in good_tenders:
        reason = good_reasons.get(t.number, "")
        text_parts = [
            f"🟢 <b>ПОДХОДИТ (по мнению ИИ)</b>\n{t.title}",
            f"№ {t.number}",
            "",
            "<b>Комментарий ИИ:</b>",
            reason or "Комментарий отсутствует",
            "",
            f'<a href="{t.url}">Открыть тендер</a>',
        ]
        await context.bot.send_message(
            chat_id,
            "\n".join(text_parts),
            parse_mode="HTML",
            disable_web_page_preview=False,
        )


async def cmd_rost_mce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rost_mce(update, context, from_callback=False)


# ================== НАСТРОЙКИ ФИЛЬТРОВ ==================

async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kw = ", ".join(get_keywords()) or "—"
    ex = ", ".join(get_exclude_keywords()) or "—"
    ct = get_city() or "—"

    gpt = get_gpt_filter_text()
    short_gpt = gpt[:300] + "…" if len(gpt) > 300 else gpt

    text = (
        "<b>Текущие фильтры:</b>\n\n"
        f"<b>Ключевые слова:</b> {kw}\n"
        f"<b>Исключения:</b> {ex}\n"
        f"<b>Город:</b> {ct}\n\n"
        "<b>Фильтр GPT:</b>\n"
        f"{short_gpt}"
    )

    await update.message.reply_text(text, parse_mode="HTML")


async def set_keywords_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "keywords"
    await update.message.reply_text(
        "Введите <b>ключевые слова</b> через запятую.",
        parse_mode="HTML",
    )


async def set_exclude_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "exclude"
    await update.message.reply_text(
        "Введите <b>слова-исключения</b> через запятую.",
        parse_mode="HTML",
    )


async def set_city_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "city"
    await update.message.reply_text(
        "Введите <b>город</b>, по которому фильтровать (или оставьте пустым, чтобы отключить).",
        parse_mode="HTML",
    )


async def set_gpt_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "gpt_filter"
    await update.message.reply_text(
        "Введите <b>новый текст фильтра GPT</b>.",
        parse_mode="HTML",
    )


# ================== ОБРАБОТКА ТЕКСТА ==================

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("awaiting", "")
    txt = (update.message.text or "").strip()

    if mode == "keywords":
        items = [w.strip() for w in txt.split(",") if w.strip()]
        set_keywords(items)
        context.user_data["awaiting"] = None
        await update.message.reply_text("✅ Ключевые слова обновлены.")
        return

    if mode == "exclude":
        items = [w.strip() for w in txt.split(",") if w.strip()]
        set_exclude_keywords(items)
        context.user_data["awaiting"] = None
        await update.message.reply_text("✅ Исключения обновлены.")
        return

    if mode == "city":
        set_city(txt)
        context.user_data["awaiting"] = None
        await update.message.reply_text("✅ Город обновлён.")
        return

    if mode == "gpt_filter":
        set_gpt_filter_text(txt)
        context.user_data["awaiting"] = None
        await update.message.reply_text("✅ Фильтр GPT обновлён.")
        return

    await update.message.reply_text(
        "Не понимаю сообщение. Используйте команды:\n"
        "/filters\n/set_keywords\n/set_exclude\n/set_city\n/set_gpt_filter\n/rost_mce"
    )


# ================== CALLBACK ==================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    if data == "rost_mce":
        await rost_mce(update, context, from_callback=True)
        return

    if data == "settings":
        await update.callback_query.message.edit_text(
            "⚙ <b>Настройки фильтров</b>\n"
            "Используйте команды:\n\n"
            "/filters\n"
            "/set_keywords\n"
            "/set_exclude\n"
            "/set_city\n"
            "/set_gpt_filter\n"
            "/rost_mce",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        return


# ================== MAIN ==================

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rost_mce", cmd_rost_mce))

    app.add_handler(CommandHandler("filters", filters_cmd))
    app.add_handler(CommandHandler("set_keywords", set_keywords_cmd))
    app.add_handler(CommandHandler("set_exclude", set_exclude_cmd))
    app.add_handler(CommandHandler("set_city", set_city_cmd))
    app.add_handler(CommandHandler("set_gpt_filter", set_gpt_filter_cmd))

    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()

