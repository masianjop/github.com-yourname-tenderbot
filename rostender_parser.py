from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = "https://rostender.info/tender"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


@dataclass
class Tender:
    source: str           # "rostender"
    number: str           # 88109280
    published: date       # дата "от 22.11.25"
    title: str            # Поставка чего-то там
    end_datetime: Optional[datetime]  # Окончание (МСК)
    city: Optional[str]
    region: Optional[str]
    price: Optional[int]          # в рублях, без пробелов
    price_raw: Optional[str]      # "6 375 000 ₽"
    url: Optional[str]            # ссылка-поиск по номеру
    raw_block: str                # сырой текст блока, на всякий случай
    detail_text: Optional[str] = None   # ДЕТАЛЬНОЕ ОПИСАНИЕ ИЗ КАРТОЧКИ


def _get_html(
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> str:
    """
    Загружает страницу каталога Ростендера.
    page=1 — первая страница, page=2 — вторая и т.д.
    """
    sess = session or requests.Session()

    params = {}
    if page > 1:
        # /tender?page=2
        params["page"] = page

    log.info("Запрашиваю каталог Ростендера: %s, страница %s", BASE_URL, page)
    resp = sess.get(BASE_URL, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.text


def _iter_blocks(full_text: str):
    """
    Разбиваем сплошной текст страницы на блоки по 'Тендер №... от ...'.
    """
    pattern = re.compile(
        r"Тендер\s+№(?P<number>\d+)\s+от\s+(?P<date>\d{2}\.\d{2}\.\d{2})(?P<body>.*?)(?=Тендер\s+№\d+\s+от|\Z)",
        re.S,
    )
    for m in pattern.finditer(full_text):
        yield m.group("number"), m.group("date"), m.group("body")


def _parse_price(lines: list[str]) -> tuple[Optional[int], Optional[str]]:
    for i, line in enumerate(lines):
        if line == "Начальная цена" and i + 1 < len(lines):
            raw = lines[i + 1]
            # пример: "6 375 000 ₽" или "—"
            if "₽" in raw:
                digits = re.sub(r"[^\d]", "", raw)
                if digits:
                    return int(digits), raw
            return None, raw
    return None, None


def _parse_end_datetime(lines: list[str]) -> Optional[datetime]:
    # Ищем строку "Окончание (МСК)" и берём либо дату с неё,
    # либо со следующей строки, если на самой строке только заголовок.
    for i, line in enumerate(lines):
        if line.startswith("Окончание (МСК)"):
            part = line.replace("Окончание (МСК)", "").strip()
            if not part and i + 1 < len(lines):
                # дата/время на следующей строке
                part = lines[i + 1].strip()

            if not part:
                return None

            try:
                if " " in part:
                    return datetime.strptime(part, "%d.%m.%Y %H:%M")
                else:
                    d = datetime.strptime(part, "%d.%m.%Y").date()
                    return datetime(d.year, d.month, d.day)
            except ValueError:
                log.warning("Не смог распарсить дату окончания: %r", part)
                return None
    return None


def _parse_city_region(lines: list[str]) -> tuple[Optional[str], Optional[str]]:
    """
    В большинстве случаев сразу после строки с Окончанием:
    [ ... 'Окончание (МСК)...', 'г. Лобня', 'Московская область', ... ]
    """
    for i, line in enumerate(lines):
        if line.startswith("Окончание (МСК)") and i + 2 < len(lines):
            city = lines[i + 1]
            region = lines[i + 2]
            return city, region
    return None, None


def _cleanup_lines(body: str) -> list[str]:
    lines = [l.strip() for l in body.splitlines()]
    return [l for l in lines if l]  # убираем пустые


def _fill_details(
    tenders: List[Tender],
    session: Optional[requests.Session] = None,
) -> None:
    """
    Для каждого тендера заходим по ссылке t.url и вытаскиваем более детальный текст.
    """
    sess = session or requests.Session()
    for t in tenders:
        if not t.url:
            continue
        try:
            log.info("Загружаю детали тендера %s: %s", t.number, t.url)
            resp = sess.get(t.url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            detail_text = soup.get_text("\n", strip=True)
            t.detail_text = detail_text
        except Exception as e:
            log.warning("Не удалось загрузить детали тендера %s: %s", t.number, e)


def fetch_rostender_tenders(
    days: int = 1,
    max_pages: Optional[int] = None,
    with_details: bool = False,
    session: Optional[requests.Session] = None,
) -> List[Tender]:
    """
    Забирает тендеры из каталога Ростендера за последние `days` дней.

    Логика:
      * идём по страницам 1..N (пока есть тендеры новее порога)
      * парсим блоки "Тендер №... от ..."
      * отбрасываем тендеры старше заданного порога
      * защищаемся от дублей по номеру.

    Если max_pages=None — ограничиваемся только датой.
    Если with_details=True — дополнительно заходим в каждый тендер по t.url и тянем detail_text.
    """

    today = date.today()
    min_date = today - timedelta(days=days)

    sess = session or requests.Session()
    tenders_by_number: Dict[str, Tender] = {}

    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            log.info(
                "Достигнут предел max_pages=%s, останавливаемся. Сейчас тендеров: %d",
                max_pages,
                len(tenders_by_number),
            )
            break

        html = _get_html(page=page, session=sess)
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)

        page_added_any = False
        added_this_page = 0

        for number, date_str, body in _iter_blocks(text):
            # 1) дата публикации
            try:
                # на сайте год в формате "25" -> считаем 20xx
                d = datetime.strptime(date_str, "%d.%m.%y").date()
            except ValueError:
                log.warning(
                    "Не смог распарсить дату публикации %r у тендера %s",
                    date_str,
                    number,
                )
                continue

            if d < min_date:
                # старый тендер, пропускаем
                continue

            if number in tenders_by_number:
                # уже добавляли этот тендер с другой страницы
                continue

            lines = _cleanup_lines(body)
            if not lines:
                log.debug("Пустой блок у тендера %s", number)
                continue

            # 2) название (первая строка после "Тендер №... от ...")
            title = lines[0]

            # 3) окончание, город, регион, цена
            end_dt = _parse_end_datetime(lines)
            city, region = _parse_city_region(lines)
            price, price_raw = _parse_price(lines)

            # 4) ссылка (поиск по номеру)
            url = f"https://rostender.info/tender?search={number}"

            tender = Tender(
                source="rostender",
                number=number,
                published=d,
                title=title,
                end_datetime=end_dt,
                city=city,
                region=region,
                price=price,
                price_raw=price_raw,
                url=url,
                raw_block=body.strip(),
            )
            tenders_by_number[number] = tender
            page_added_any = True
            added_this_page += 1

        log.info(
            "Страница %s: добавлено %d тендера(ов), всего уникальных тендеров: %d",
            page,
            added_this_page,
            len(tenders_by_number),
        )

        # Если на странице вообще не появилось ни одного нового тендера
        # за наш период, считаем, что дальше только старые и выходим.
        if not page_added_any:
            log.info(
                "На странице %s не найдено тендеров новее %s, останавливаюсь.",
                page,
                min_date.strftime("%d.%m.%Y"),
            )
            break

        page += 1

    results = list(tenders_by_number.values())

    if with_details and results:
        log.info("Загружаю детали для %d тендеров...", len(results))
        _fill_details(results, session=sess)

    # Сортируем по дате публикации и номеру (самые свежие наверху)
    results.sort(key=lambda t: (t.published, t.number), reverse=True)
    log.info(
        "Итого: нашёл %d тендеров Ростендера за последние %d дн.",
        len(results),
        days,
    )
    return results


def format_tender_for_telegram(t: Tender) -> str:
    price_part = f"{t.price_raw}" if t.price_raw else "—"
    end_part = t.end_datetime.strftime("%d.%m.%Y %H:%M") if t.end_datetime else "неизвестно"
    place = " / ".join(p for p in [t.city, t.region] if p)

    return (
        f"📌 {t.title}\n"
        f"№ {t.number} от {t.published.strftime('%d.%m.%Y')}\n"
        f"📍 {place or 'место не указано'}\n"
        f"⏱ Окончание (МСК): {end_part}\n"
        f"💰 Начальная цена: {price_part}\n"
        f"🔗 {t.url}"
    )

