from __future__ import annotations

import logging
import os
import re
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from rostender_parser import Tender

log = logging.getLogger(__name__)

# Загружаем .env, чтобы подхватить ROSTENDER_FILTER_URL
load_dotenv()

# Если в .env есть готовый URL расширенного поиска (с query),
# используем его. Иначе — базовый advanced без фильтров.
ROSTENDER_FILTER_URL = os.getenv("ROSTENDER_FILTER_URL", "").strip() or \
    "https://rostender.info/extsearch/advanced"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _get_search_html(
    base_url: str,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> str:
    """
    Загружает страницу расширенного поиска Ростендера по готовому URL.
    page=1 — base_url как есть.
    page>1 — аккуратно добавляем/обновляем параметр page=N.
    """
    sess = session or requests.Session()

    if page <= 1:
        url = base_url
    else:
        # убираем старый page, если есть
        url = re.sub(r"[?&]page=\d+", "", base_url)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}page={page}"

    log.info("Запрашиваю страницу Ростендера: %s", url)
    resp = sess.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _iter_blocks(full_text: str):
    """
    Разбиваем текст страницы на блоки по 'Тендер №... от ...'.
    """
    pattern = re.compile(
        r"Тендер\s+№(?P<number>\d+)\s+от\s+"
        r"(?P<date>\d{2}\.\d{2}\.\d{2})(?P<body>.*?)(?=Тендер\s+№\d+\s+от|\Z)",
        re.S,
    )
    for m in pattern.finditer(full_text):
        yield m.group("number"), m.group("date"), m.group("body")


def _cleanup_lines(body: str) -> list[str]:
    lines = [l.strip() for l in body.splitlines()]
    return [l for l in lines if l]


def _parse_price(lines: list[str]) -> tuple[Optional[int], Optional[str]]:
    for i, line in enumerate(lines):
        if line == "Начальная цена" and i + 1 < len(lines):
            raw = lines[i + 1]
            digits = re.sub(r"[^\d]", "", raw)
            if digits:
                return int(digits), raw
            return None, raw
    return None, None


def _parse_end_datetime(lines: list[str]) -> Optional[datetime]:
    for i, line in enumerate(lines):
        if line.startswith("Окончание (МСК)"):
            part = line.replace("Окончание (МСК)", "").strip()
            if not part and i + 1 < len(lines):
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
    for i, line in enumerate(lines):
        if line.startswith("Окончание (МСК)") and i + 2 < len(lines):
            city = lines[i + 1]
            region = lines[i + 2]
            return city, region
    return None, None


def _matches_basic_filters(
    title: str,
    body: str,
    city: Optional[str],
    region: Optional[str],
    include_words: list[str],
    exclude_words: list[str],
    city_filter: Optional[str],
) -> bool:
    """
    Локальная фильтрация:
    - include_words (если заданы) — хотя бы одно вхождение;
    - exclude_words — ни одного вхождения;
    - city_filter (если задан) должен встречаться в городе/регионе/тексте.

    ВАЖНО: Этап 'приём заявок' здесь больше НЕ проверяем — считаем,
    что он уже настроен в самом URL расширенного поиска Ростендера.
    """
    text = f"{title}\n{body}".lower()

    # Положительные слова (если заданы) — хотя бы одно
    if include_words and not any(w in text for w in include_words):
        return False

    # Исключения — ни одно не должно встречаться
    if exclude_words and any(w in text for w in exclude_words):
        return False

    # Фильтр по городу
    if city_filter:
        geo_text = " ".join(
            [
                (city or "").lower(),
                (region or "").lower(),
                text,
            ]
        )
        if city_filter not in geo_text:
            return False

    return True


def _fill_details(
    tenders: List[Tender],
    session: Optional[requests.Session] = None,
) -> None:
    """
    Для каждого тендера заходим по ссылке и выдёргиваем detail_text.
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
            setattr(t, "detail_text", detail_text)
        except Exception as e:
            log.warning("Не удалось загрузить детали тендера %s: %s", t.number, e)


def fetch_rostender_tenders_filtered(
    days: int = 3,
    max_pages: int = 2,
    with_details: bool = True,
    include_words: Optional[List[str]] = None,
    exclude_words: Optional[List[str]] = None,
    city_filter: Optional[str] = None,
) -> List[Tender]:
    """
    Парсит тендеры по сохранённому расширенному поиску Ростендера
    (ROSTENDER_FILTER_URL из .env) и дополнительно фильтрует:
      - include_words / exclude_words,
      - city_filter,
      - дата публикации за последние `days` дней.
    """

    include_words = [w.strip().lower() for w in (include_words or []) if w.strip()]
    exclude_words = [w.strip().lower() for w in (exclude_words or []) if w.strip()]
    city_filter_norm = city_filter.strip().lower() if city_filter else None

    base_url = ROSTENDER_FILTER_URL

    today = date.today()
    min_date = today - timedelta(days=days)

    sess = requests.Session()
    tenders_by_number: Dict[str, Tender] = {}

    for page in range(1, max_pages + 1):
        html = _get_search_html(base_url, page=page, session=sess)
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)

        raw_blocks = 0
        added_this_page = 0

        for number, date_str, body in _iter_blocks(text):
            raw_blocks += 1
            try:
                d = datetime.strptime(date_str, "%d.%m.%y").date()
            except ValueError:
                log.warning(
                    "Не смог распарсить дату публикации %r у тендера %s",
                    date_str,
                    number,
                )
                continue

            if d < min_date:
                continue

            if number in tenders_by_number:
                continue

            lines = _cleanup_lines(body)
            if not lines:
                continue

            title = lines[0]
            end_dt = _parse_end_datetime(lines)
            city, region = _parse_city_region(lines)
            price, price_raw = _parse_price(lines)

            if not _matches_basic_filters(
                title=title,
                body=body,
                city=city,
                region=region,
                include_words=include_words,
                exclude_words=exclude_words,
                city_filter=city_filter_norm,
            ):
                continue

            url = f"https://rostender.info/tender?search={number}"

            tender = Tender(
                source="rostender-filter",
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
            added_this_page += 1

        log.info(
            "Страница %s: сырых блоков: %d, прошло фильтр: %d, всего уникальных: %d",
            page,
            raw_blocks,
            added_this_page,
            len(tenders_by_number),
        )

        if raw_blocks == 0 or added_this_page == 0:
            log.info(
                "На странице %s новых подходящих тендеров не нашли, останавливаемся.",
                page,
            )
            break

    results = list(tenders_by_number.values())

    if with_details and results:
        log.info("Загружаю детали для %d тендеров…", len(results))
        _fill_details(results, session=sess)

    results.sort(key=lambda t: (t.published, t.number), reverse=True)
    log.info(
        "Итого по фильтрованному поиску: %d тендеров за последние %d дн.",
        len(results),
        days,
    )
    return results

