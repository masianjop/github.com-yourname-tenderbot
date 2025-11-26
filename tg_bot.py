from __future__ import annotations

import logging
import os
import re
from asyncio import to_thread
from typing import Any

from dotenv import load_dotenv
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
    get_search_days,
    set_search_days,
    get_max_pages,
    set_max_pages,
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

MAX_GPT_TENDERS = 12  # максимум тендеров, которые отправляем в GPT за один запуск


# ================== КЛАВИАТУРЫ ==================


def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🔍 Проверить тендеры", callback_data="menu_rost_mce")],
        [InlineKeyboardButton("⚙ Настройки фильтра", callback_data="menu_settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def settings_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("🟩 Ключевые слова", callback_data="set_kw"),
            InlineKeyboardButton("🟥 Исключения", callback_data="set_ex"),
        ],
        [
            InlineKeyboardButton("🟦 Город", callback_data="set_city"),
            InlineKeyboardButton("🤖 Фильтр GPT", callback_data="set_gpt"),
        ],
        [
            InlineKeyboardButton("⏱ Период и страницы", callback_data="set_period"),
        ],
        [
            InlineKeyboardButton("ℹ Текущие фильтры", callback_data="show_filters"),
        ],
        [
            InlineKeyboardButton("⬅ Назад", callback_data="back_main"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


# ================== ХЕЛПЕРЫ ==================


def _format_filters_text() -> str:
    kw_list = get_keywords() or []
    ex_list = get_exclude_keywords() or []
    city = get_city() or ""
    days = get_search_days()
    pages = get_max_pages()

    kw = ", ".join(kw_list) if kw_list else "—"
    ex = ", ".join(ex_list) if ex_list else "—"
    ct = city if city else "—"

    gpt = get_gpt_filter_text()
    short_gpt = gpt.strip()
    if len(short_gpt) > 500:
        short_gpt = short_gpt[:500] + "…"

    text = (
        "<b>Текущие фильтры:</b>\n\n"
        f"<b>Ключевые слова:</b> {kw}\n"
        f"<b>Исключающие слова:</b> {ex}\n"
        f"<b>Город:</b> {ct}\n"
        f"<b>Период поиска:</b> последние {days} дн.\n"
        f"<b>Страниц Ростендера:</b> {pages}\n\n"
        "<b>Фильтр GPT (начало текста):</b>\n"
        f"{short_gpt or '—'}"
    )
    return text


def _get_desc_for_local(t: Any) -> str:
    """
    Описание для локального фильтра: сначала берём raw_block (кусок из списка),
    если его нет — detail_text.
    """
    return (
        getattr(t, "raw_block", "")
        or getattr(t, "detail_text", "")
        or ""
    )


def _get_desc_for_snippet(t: Any) -> str:
    """
    Описание для выводимого сниппета в Телеграме.
    Берём raw_block, чтобы не тянуть рекламный мусор со страницы.
    """
    return _get_desc_for_local(t)


def _build_pretty_description(t: Any) -> str:
    """
    Делает структурированный список пунктов из сырого текста,
    чтобы не было каши из строк.
    """
    text = _get_desc_for_snippet(t).strip()
    if not text:
        return ""

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    cleaned: list[str] = []
    for line in lines:
        # выкидываем технические даты/таймстемпы
        if re.match(r"\d{4}-\d{2}-\d{2}", line):
            continue
        if re.match(r"\d{4}\.\d{2}\.\d{2}", line):
            continue
        if re.match(r"\d{2}\.\d{2}\.\d{4}", line):
            continue
        if re.match(r"\d{2}:\d{2}:\d{2}$", line):
            continue

        # выкидываем совсем странные строки
        if len(line) > 200:
            continue

        cleaned.append(line)

    # первые 8–10 строк достаточно
    cleaned = cleaned[:10]
    if not cleaned:
        return ""

    return "\n".join(f"• {l}" for l in cleaned)


def _format_tender_message(t: Any, reason: str) -> str:
    """
    Формируем максимально информативное сообщение по тендеру,
    но аккуратно и читаемо.
    """
    title = getattr(t, "title", "") or "Без названия"
    number = getattr(t, "number", "") or "—"

    published = getattr(t, "published", None)
    if published is not None:
        try:
            pub_str = published.strftime("%d.%m.%Y")
        except Exception:
            pub_str = str(published)
    else:
        pub_str = "—"

    end_dt = getattr(t, "end_datetime", None)
    if end_dt is not None:
        try:
            end_str = end_dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            end_str = str(end_dt)
    else:
        end_str = "—"

    city = getattr(t, "city", "") or ""
    region = getattr(t, "region", "") or ""
    geo_parts = [p for p in [city, region] if p]
    geo = ", ".join(geo_parts) if geo_parts else "—"

    price_raw = getattr(t, "price_raw", None) or getattr(t, "price", None)
    if price_raw is None:
        price_str = "—"
    else:
        price_str = str(price_raw)

    url = getattr(t, "url", "") or ""

    pretty_desc = _build_pretty_description(t)

    parts: list[str] = []

    parts.append("🟢 <b>ПОДХОДИТ (по мнению ИИ)</b>")
    parts.append(title)
    parts.append(f"№ {number}")
    parts.append("")

    parts.append("<b>Основная информация:</b>")
    parts.append(f"• <b>Дата публикации:</b> {pub_str}")
    parts.append(f"• <b>Окончание приёма заявок:</b> {end_str}")
    parts.append(f"• <b>Цена:</b> {price_str}")
    parts.append(f"• <b>География:</b> {geo}")
    parts.append("")

    if pretty_desc:
        parts.append("<b>Краткое описание тендера:</b>")
        parts.append(pretty_desc)
        parts.append("")

    parts.append("<b>Комментарий ИИ:</b>")
    parts.append(reason or "Комментарий отсутствует")
    parts.append("")

    if url:
        parts.append(f'<a href="{url}">Открыть тендер на сайте</a>')

    return "\n".join(parts)


# ================== КОМАНДЫ ==================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я бот для проверки тендеров МЦЭ Инжиниринг.\n\n"
        "Я умею:\n"
        "• Парсить Ростендер по фильтру\n"
        "• Фильтровать тендеры по профилю МЦЭ (локальный фильтр)\n"
        "• Отдавать кандидатов в GPT для финальной оценки\n\n"
        "Пользуйся кнопками ниже 👇\n\n"
        "Дополнительно доступны команды:\n"
        "/filters — показать текущие фильтры\n"
        "/rost_mce — запустить проверку вручную\n"
    )
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = _format_filters_text()
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_rost_mce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rost_mce(update, context, from_callback=False)


# ================== ОСНОВНАЯ ЛОГИКА ПОИСКА ==================


async def rost_mce(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False):
    """
    1) Тянем тендеры с Ростендера с учётом keywords/exclude/city и параметров поиска.
    2) Прогоняем через локальный фильтр MCE.
    3) Если локальный фильтр никого не нашёл — отправляем в GPT первые N тендеров.
    4) GPT решает, что подходит.
    """
    chat_id = update.effective_chat.id

    if from_callback:
        query = update.callback_query
        msg = await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=query.message.message_id,
            text="⏳ Загружаю тендеры Ростендера...",
        )
    else:
        msg = await context.bot.send_message(chat_id, "⏳ Загружаю тендеры Ростендера...")

    include_words = get_keywords()
    exclude_words = get_exclude_keywords()
    city_filter = get_city()
    days = get_search_days()
    pages = get_max_pages()

    def load_tenders():
        return fetch_rostender_tenders_filtered(
            days=days,
            max_pages=pages,
            include_words=include_words,
            exclude_words=exclude_words,
            city_filter=city_filter,
        )

    tenders = await to_thread(load_tenders)
    total_tenders = len(tenders)

    if not tenders:
        await msg.edit_text(f"⚠ За последние {days} дн. новых тендеров не найдено.")
        return

    log.info(
        "Всего тендеров из Ростендера после базового фильтра: %d (days=%d, pages=%d)",
        total_tenders,
        days,
        pages,
    )

    # ---------------- ЛОКАЛЬНЫЙ ФИЛЬТР МЦЭ ----------------
    local_items_full: list[tuple[object, object | None]] = []
    for t in tenders:
        desc = _get_desc_for_local(t)
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
            local_items_full.append((t, local))

    local_found = len(local_items_full)
    log.info("Локальный фильтр МЦЭ: нашёл %d тендеров", local_found)

    # выбираем, кого отправлять в GPT
    if local_found:
        local_items_full.sort(
            key=lambda pair: (
                getattr(pair[1], "priority_level", 0),
                getattr(pair[0], "published", None),
                getattr(pair[0], "number", ""),
            ),
            reverse=True,
        )
        local_items = local_items_full[:MAX_GPT_TENDERS]
        sent_to_gpt = len(local_items)
        await msg.edit_text(
            f"🤖 Локальный фильтр МЦЭ нашёл {local_found} кандидатов. "
            f"Отправляю в ИИ {sent_to_gpt} лучших..."
        )
    else:
        # fallback: если локальный фильтр никого не нашёл — всё равно что-то отдадим в GPT
        sent_to_gpt = min(MAX_GPT_TENDERS, total_tenders)
        local_items = [(t, None) for t in tenders[:sent_to_gpt]]
        log.info(
            "Локальный фильтр МЦЭ не нашёл подходящих тендеров. "
            "Отправляю в GPT первые %d тендеров без локального отбора.",
            sent_to_gpt,
        )
        await msg.edit_text(
            "⚠ Локальный фильтр МЦЭ не нашёл подходящих тендеров.\n"
            f"Отправляю в ИИ первые {sent_to_gpt} тендеров для проверки."
        )

    # --- GPT в отдельном потоке ---
    def gpt_job():
        return ask_gpt_about_tenders(local_items)

    gpt_results = await to_thread(gpt_job)
    gpt_answers = len(gpt_results)

    if not gpt_results:
        await msg.edit_text("⚠ ИИ не вернул ни одного подходящего тендера (или произошла ошибка).")
        # даже если ошибка, покажем статистику до этого места
        stats_text = (
            "📊 <b>Статистика запуска</b>\n\n"
            f"• Всего тендеров с Ростендера: <b>{total_tenders}</b>\n"
            f"• Прошли локальный фильтр МЦЭ: <b>{local_found}</b>\n"
            f"• Отправлено в GPT: <b>{sent_to_gpt}</b>\n"
            f"• Ответов от GPT: <b>{gpt_answers}</b>\n"
            f"• GPT признал подходящими: <b>0</b>\n"
        )
        await context.bot.send_message(chat_id, stats_text, parse_mode="HTML")
        return

    good_codes = {r.code for r in gpt_results if r.is_match}
    good_reasons = {r.code: r.reason for r in gpt_results if r.is_match}
    good_tenders = [t for (t, _local) in local_items if t.number in good_codes]
    matched_count = len(good_tenders)

    # сначала всегда шлём статистику
    stats_text = (
        "📊 <b>Статистика запуска</b>\n\n"
        f"• Всего тендеров с Ростендера: <b>{total_tenders}</b>\n"
        f"• Прошли локальный фильтр МЦЭ: <b>{local_found}</b>\n"
        f"• Отправлено в GPT: <b>{sent_to_gpt}</b>\n"
        f"• Ответов от GPT: <b>{gpt_answers}</b>\n"
        f"• GPT признал подходящими: <b>{matched_count}</b>\n"
    )
    await context.bot.send_message(chat_id, stats_text, parse_mode="HTML")

    if not good_tenders:
        await msg.edit_text("❌ ИИ не нашёл подходящих тендеров среди кандидатов.")
        return

    await msg.edit_text(
        f"🟢 ИИ нашёл {matched_count} подходящих тендер(ов). Отправляю детальный список..."
    )

    for t in good_tenders:
        reason = good_reasons.get(t.number, "")
        text = _format_tender_message(t, reason)
        await context.bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )


# ================== НАСТРОЙКИ ЧЕРЕЗ КНОПКИ/ТЕКСТ ==================


async def set_keywords_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "keywords"
    await update.message.reply_text(
        "Введи <b>ключевые слова</b> через запятую.\n\n"
        "Пример: узел учета, сикг, газоанализ",
        parse_mode="HTML",
        reply_markup=settings_menu_keyboard(),
    )


async def set_exclude_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "exclude"
    await update.message.reply_text(
        "Введи <b>исключающие слова</b> через запятую.\n\n"
        "Пример: строительство, ремонт, благоустройство",
        parse_mode="HTML",
        reply_markup=settings_menu_keyboard(),
    )


async def set_city_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "city"
    await update.message.reply_text(
        "Введи <b>город</b>, по которому фильтровать.\n"
        "Оставь пустым, чтобы отключить фильтр по городу.",
        parse_mode="HTML",
        reply_markup=settings_menu_keyboard(),
    )


async def set_gpt_filter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "gpt_filter"
    await update.message.reply_text(
        "Введи <b>новый текст фильтра для GPT</b>.\n\n"
        "Это описание, чем занимается МЦЭ и какие тендеры считаем подходящими.",
        parse_mode="HTML",
        reply_markup=settings_menu_keyboard(),
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("awaiting")
    txt = (update.message.text or "").strip()

    if not mode:
        await update.message.reply_text(
            "Я жду действие через кнопки.\n"
            "Используй главное меню или /start, чтобы открыть его.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if mode == "keywords":
        items = [w.strip() for w in txt.split(",") if w.strip()]
        set_keywords(items)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            "✅ Ключевые слова обновлены.",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if mode == "exclude":
        items = [w.strip() for w in txt.split(",") if w.strip()]
        set_exclude_keywords(items)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            "✅ Исключающие слова обновлены.",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if mode == "city":
        set_city(txt)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            "✅ Город обновлён.",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if mode == "gpt_filter":
        set_gpt_filter_text(txt)
        context.user_data["awaiting"] = None
        await update.message.reply_text(
            "✅ Фильтр GPT обновлён.",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if mode == "period":
        parts = txt.split()
        if not parts:
            await update.message.reply_text(
                "⚠ Ничего не понял. Введи, например: 1 2 (1 день, 2 страницы) или просто 3 (3 дня).",
                reply_markup=settings_menu_keyboard(),
            )
            return

        try:
            days = int(parts[0])
        except Exception:
            await update.message.reply_text(
                "⚠ Первый параметр должен быть числом (кол-во дней). Пример: 1 2",
                reply_markup=settings_menu_keyboard(),
            )
            return

        if len(parts) >= 2:
            try:
                pages = int(parts[1])
            except Exception:
                await update.message.reply_text(
                    "⚠ Второй параметр (страницы) должен быть числом. Пример: 1 2",
                    reply_markup=settings_menu_keyboard(),
                )
                return
        else:
            pages = get_max_pages()

        set_search_days(days)
        set_max_pages(pages)

        days_eff = get_search_days()
        pages_eff = get_max_pages()

        context.user_data["awaiting"] = None
        await update.message.reply_text(
            f"✅ Параметры поиска обновлены.\n"
            f"Теперь смотрим последние {days_eff} дн., страниц Ростендера: {pages_eff}.",
            reply_markup=settings_menu_keyboard(),
        )
        return

    # если какой-то левый режим
    context.user_data["awaiting"] = None
    await update.message.reply_text(
        "Что-то пошло не так, режим сброшен. Используй меню ещё раз.",
        reply_markup=main_menu_keyboard(),
    )


# ================== CALLBACK-КНОПКИ ==================


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "menu_rost_mce":
        await rost_mce(update, context, from_callback=True)
        return

    if data == "menu_settings":
        await query.edit_message_text(
            "⚙ <b>Настройки фильтра</b>\n\n"
            "Выбери, что хочешь изменить:",
            parse_mode="HTML",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if data == "set_kw":
        context.user_data["awaiting"] = "keywords"
        await query.edit_message_text(
            "Введи <b>ключевые слова</b> через запятую.\n\n"
            "Пример: узел учета, сикг, газоанализ",
            parse_mode="HTML",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if data == "set_ex":
        context.user_data["awaiting"] = "exclude"
        await query.edit_message_text(
            "Введи <b>исключающие слова</b> через запятую.\n\n"
            "Пример: строительство, ремонт, благоустройство",
            parse_mode="HTML",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if data == "set_city":
        context.user_data["awaiting"] = "city"
        await query.edit_message_text(
            "Введи <b>город</b>, по которому фильтровать.\n"
            "Оставь пустым, чтобы отключить фильтр по городу.",
            parse_mode="HTML",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if data == "set_gpt":
        context.user_data["awaiting"] = "gpt_filter"
        await query.edit_message_text(
            "Введи <b>новый текст фильтра для GPT</b>.\n\n"
            "Это описание, чем занимается МЦЭ и какие тендеры считаем подходящими.",
            parse_mode="HTML",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if data == "set_period":
        context.user_data["awaiting"] = "period"
        days = get_search_days()
        pages = get_max_pages()
        await query.edit_message_text(
            "⏱ <b>Параметры поиска по времени и страницам</b>\n\n"
            f"Сейчас:\n"
            f"• последние <b>{days}</b> дн.\n"
            f"• страниц Ростендера: <b>{pages}</b>\n\n"
            "Введи новые значения в формате:\n"
            "<code>дни страницы</code>\n\n"
            "Например:\n"
            "<code>1 2</code> — за последние 1 день, 2 страницы\n"
            "<code>3</code> — за последние 3 дня, страниц столько же, как сейчас.",
            parse_mode="HTML",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if data == "show_filters":
        text = _format_filters_text()
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=settings_menu_keyboard(),
        )
        return

    if data == "back_main":
        await query.edit_message_text(
            "Главное меню:",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        return


# ================== MAIN ==================


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("filters", filters_cmd))
    app.add_handler(CommandHandler("rost_mce", cmd_rost_mce))

    # доп. команды для ручного вызова (дублируют кнопки)
    app.add_handler(CommandHandler("set_keywords", set_keywords_cmd))
    app.add_handler(CommandHandler("set_exclude", set_exclude_cmd))
    app.add_handler(CommandHandler("set_city", set_city_cmd))
    app.add_handler(CommandHandler("set_gpt_filter", set_gpt_filter_cmd))

    # callback-кнопки
    app.add_handler(CallbackQueryHandler(callbacks))

    # текст — когда бот кого-то "ждёт"
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Бот запущен. Нажми /start в Telegram.")
    app.run_polling()


if __name__ == "__main__":
    main()

