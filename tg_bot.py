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
    get_rostender_filter_url,
    set_rostender_filter_url,
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


# ================== ХЕЛПЕРЫ ДЛЯ МЕНЮ ==================


def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("Ростендер: МЦЭ фильтр", callback_data="rost_mce"),
        ],
        [
            InlineKeyboardButton("⚙ Настройки фильтров", callback_data="settings"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ================== ХЭНДЛЕРЫ КОМАНД ==================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я бот для проверки тендеров.\n\n"
        "Сейчас подключен источник: <b>Ростендер (расширенный поиск)</b>.\n"
        "Нажми кнопку ниже, чтобы найти тендеры по профилю МЦЭ Инжиниринг.\n\n"
        "Команды:\n"
        "/start — показать меню\n"
        "/rost_mce — запустить поиск по Ростендеру\n"
        "/show_filters — показать текущие фильтры\n"
        "/set_rost_url — сменить URL фильтра Ростендера\n"
        "/set_gpt_filter — сменить текст фильтра для ИИ\n"
    )
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def rost_mce(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False):
    """
    Главная функция: забираем тендеры с Ростендера, фильтруем локально,
    затем отправляем часть в GPT и показываем только те, что GPT признал подходящими.
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
        return fetch_rostender_tenders_filtered(days=3, max_pages=ROST_MAX_PAGES)

    tenders = await to_thread(load_tenders)

    if not tenders:
        await msg.edit_text("⚠ За последние 3 дня новых тендеров на Ростендере не нашёл.")
        return

    # ---------------- ЛОКАЛЬНЫЙ ФИЛЬТР ----------------
    # тут не импортируем LocalAnalysis, просто используем то, что вернёт analyze_tender
    local_items: list[tuple[object, object]] = []

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

        # ожидаем, что analyze_tender вернёт объект с полем is_local_match (или is_match)
        is_local_match = getattr(local, "is_local_match", None)
        if is_local_match is None:
            # fallback: поддержка старой версии, где было is_match
            is_local_match = getattr(local, "is_match", False)

        if is_local_match:
            local_items.append((t, local))

    # сортируем по приоритету и дате
    def sort_key(pair: tuple[object, object]):
        tender, local = pair
        priority = getattr(local, "priority_level", 0) or 0
        published = getattr(tender, "published", None)
        number = getattr(tender, "number", "")
        return (priority, published, number)

    local_items.sort(key=sort_key, reverse=True)

    # ограничение на количество для GPT
    local_items = local_items[:MAX_GPT_TENDERS]

    if not local_items:
        await msg.edit_text(
            "⚠ Локальный фильтр не нашёл тендеров, похожих на профиль МЦЭ Инжиниринг.\n"
            "Если хочешь ослабить фильтр — отредактируй mce_filter.py или фильтр ИИ."
        )
        return

    await msg.edit_text(
        f"🤖 Локальный фильтр нашёл {len(local_items)} кандидатов. "
        f"Отправляю их в ИИ на детальный анализ..."
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
            f"🟢 <b>ПОДХОДИТ (по мнению ИИ)</b> — {t.title}",
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


# ================== НАСТРОЙКИ ФИЛЬТРОВ ЧЕРЕЗ ТГ ==================


async def show_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rost_url = get_rostender_filter_url() or "не задан"
    gpt_text = get_gpt_filter_text()
    short_gpt = gpt_text.strip()
    if len(short_gpt) > 400:
        short_gpt = short_gpt[:400] + "…"

    text = (
        "<b>Текущие фильтры:</b>\n\n"
        f"<b>Ростендер URL:</b>\n{rost_url}\n\n"
        f"<b>Фильтр ИИ (начало текста):</b>\n{short_gpt}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def set_rost_url_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "rost_url"
    await update.message.reply_text(
        "Пришли мне <b>новый URL</b> расширенного поиска Ростендера "
        "(строка из браузера после настройки фильтра).",
        parse_mode="HTML",
    )


async def set_gpt_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "gpt_filter"
    await update.message.reply_text(
        "Пришли <b>новый текст фильтра для ИИ</b>.\n\n"
        "Это текст, где описано, чем занимается МЦЭ Инжиниринг и какие тендеры считаем подходящими. "
        "По нему GPT решает, наш это тендер или нет.",
        parse_mode="HTML",
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("awaiting")
    text = (update.message.text or "").strip()

    if mode == "rost_url":
        set_rostender_filter_url(text)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            "✅ Новый URL фильтра Ростендера сохранён.\n"
            "Следующий запуск /rost_mce будет использовать этот URL."
        )
        return

    if mode == "gpt_filter":
        set_gpt_filter_text(text)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            "✅ Новый текст фильтра ИИ сохранён.\n"
            "Все следующие обращения к ИИ будут использовать этот текст."
        )
        return

    # если текст вне режимов — даём подсказку
    await update.message.reply_text(
        "Я не понял этот текст. Для настроек фильтров используй команды:\n"
        "/show_filters — показать текущие фильтры\n"
        "/set_rost_url — сменить URL фильтра Ростендера\n"
        "/set_gpt_filter — сменить фильтр ИИ"
    )


# ================== CALLBACK'И ==================


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "rost_mce":
        await rost_mce(update, context, from_callback=True)
        return

    if data == "settings":
        # просто покажем текущие фильтры
        rost_url = get_rostender_filter_url() or "не задан"
        gpt_text = get_gpt_filter_text()
        short_gpt = gpt_text.strip()
        if len(short_gpt) > 400:
            short_gpt = short_gpt[:400] + "…"

        text = (
            "<b>Настройки фильтров</b>\n\n"
            f"<b>Ростендер URL:</b>\n{rost_url}\n\n"
            f"<b>Фильтр ИИ (начало текста):</b>\n{short_gpt}\n\n"
            "Для изменения используй команды:\n"
            "/set_rost_url — сменить URL фильтра Ростендера\n"
            "/set_gpt_filter — сменить текст фильтра ИИ"
        )

        await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=main_menu_keyboard())
        return

    # по умолчанию просто игнор
    await query.answer()


# ================== MAIN ==================


def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rost_mce", cmd_rost_mce))

    # настройки
    app.add_handler(CommandHandler("show_filters", show_filters))
    app.add_handler(CommandHandler("set_rost_url", set_rost_url_cmd))
    app.add_handler(CommandHandler("set_gpt_filter", set_gpt_filter_cmd))

    # callback-кнопки
    app.add_handler(CallbackQueryHandler(callbacks))

    # любые текстовые сообщения (не команды) — в роутер
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Бот запущен. Нажми /start в Telegram.")
    app.run_polling()


if __name__ == "__main__":
    main()

