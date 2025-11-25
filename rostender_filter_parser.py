from __future__ import annotations

import logging
import re
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict

import requests
from bs4 import BeautifulSoup

from rostender_parser import Tender  # твой dataclass Tender

log = logging.getLogger(__name__)

# ФИКСИРОВАННЫЙ URL расширенного поиска Ростендера.
# Здесь уже можно один раз руками настроить "Приём заявок" и нужные базовые фильтры.
ROSTENDER_FILTER_URL = (
    "https://rostender.info/extsearch/advanced?query=a1dc870aa14ff1586ae725d56f7b2ee6"
)

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
    page=1 — без параметра page,
    page>1 — добавляем ?page=N или &page=N.
    """
    sess = session or requests.Session()

    if page <= 1:
        url = base_url
    else:
        # убираем старый page, если он вдруг уже есть
        url = re.sub(r"[?&]page=\d+", "", base_url)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}page={page}"

    log.info("Запрашиваю страницу поиска Ростендера: %s", url)
    resp = sess.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _iter_blocks(full_text: str):
    """
    Разбиваем сплошной текст страницы на блоки по 'Тендер №... от ...'.
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
            if "₽" in raw:
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


def _fill_details(
    tenders: List[Tender],
    session: Optional[requests.Session] = None,
) -> None:
    """
    Для каждого тендера заходим по ссылке t.url и вытаскиваем более детальный текст.
    detail_text добавляем как динамическое поле в объект.
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
    - этап должен быть «Приём заявок»;
    - должны присутствовать include_words (если заданы);
    - не должны присутствовать exclude_words;
    - если задан город — он должен встретиться в городе/регионе/описании.
    """
    text = f"{title}\n{body}".lower()

    # Этап "Приём заявок" — обязательно
    if "приём заявок" not in text and "прием заявок" not in text:
        return False

    # Положительные слова (если заданы) — хотя бы одно
    if include_words:
        if not any(w in text for w in include_words):
            return False

    # Исключения — ни одно не должно встретиться
    if exclude_words:
        if any(w in text for w in exclude_words):
            return False

    # Фильтр по городу (если задан)
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


def fetch_rostender_tenders_filtered(
    days: int = 3,
    max_pages: int = 2,
    with_details: bool = True,
    include_words: Optional[List[str]] = None,
    exclude_words: Optional[List[str]] = None,
    city_filter: Optional[str] = None,
) -> List[Tender]:
    """
    Парсит тендеры по заранее настроенному расширенному поиску Ростендера
    и дополнительно фильтрует их локально по:
      - ключевым словам (include_words),
      - словам-исключениям (exclude_words),
      - городу (city_filter),
      - этапу «Приём заявок».

    include_words / exclude_words — списки строк (уже разделённые по запятой и очищенные).
    city_filter — строка города (или None, если город не фильтруем).
    """

    # Нормализуем фильтры
    include_words = [w.strip().lower() for w in (include_words or []) if w.strip()]
    exclude_words = [w.strip().lower() for w in (exclude_words or []) if w.strip()]
    city_filter_norm = (
        city_filter.strip().lower() if city_filter and city_filter.strip() else None
    )

    base_url = ROSTENDER_FILTER_URL

    today = date.today()
    min_date = today - timedelta(days=days)

    sess = requests.Session()
    tenders_by_number: Dict[str, Tender] = {}

    for page in range(1, max_pages + 1):
        html = _get_search_html(base_url, page=page, session=sess)
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)

        page_added_any = False
        added_this_page = 0

        for number, date_str, body in _iter_blocks(text):
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
                log.debug("Пустой блок у тендера %s", number)
                continue

            title = lines[0]
            end_dt = _parse_end_datetime(lines)
            city, region = _parse_city_region(lines)
            price, price_raw = _parse_price(lines)

            # Локальная фильтрация по словам/городу/этапу
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
            page_added_any = True
            added_this_page += 1

        log.info(
            "Фильтрованный поиск, страница %s: добавлено %d тендера(ов), всего уникальных: %d",
            page,
            added_this_page,
            len(tenders_by_number),
        )

        if not page_added_any:
            log.info(
                "На странице %s фильтрованного поиска нет тендеров новее %s, останавливаюсь.",
                page,
                min_date.strftime("%d.%m.%Y"),
            )
            break

    results = list(tenders_by_number.values())

    if with_details and results:
        log.info("Загружаю детали для %d тендеров (фильтрованный поиск)...", len(results))
        _fill_details(results, session=sess)

    results.sort(key=lambda t: (t.published, t.number), reverse=True)
    log.info(
        "Итого по фильтрованному поиску: нашёл %d тендеров за последние %d дн.",
        len(results),
        days,
    )
    return results

