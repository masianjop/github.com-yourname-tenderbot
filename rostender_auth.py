from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import List, Dict
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from rostender_parser import (
    Tender,
    _iter_blocks,
    _cleanup_lines,
    _parse_end_datetime,
    _parse_city_region,
    _parse_price,
)

log = logging.getLogger(__name__)

BASE_URL = "https://rostender.info/"


def _build_page_url(base_url: str, page_num: int) -> str:
    """
    Добавляем/меняем параметр page в URL шаблона.
    page=1 -> базовый URL, page>1 -> ?page=2 и т.п.
    """
    if page_num <= 1:
        return base_url

    parsed = urlparse(base_url)
    q = parse_qs(parsed.query)
    q["page"] = [str(page_num)]
    new_query = urlencode(q, doseq=True)

    return urlunparse(parsed._replace(query=new_query))


def _login(page, login: str, password: str) -> None:
    """
    Примерный логин на Ростендер.
    Может потребовать подправить селекторы, если форма входа другая.
    """
    log.info("Открываю главную страницу Ростендера для логина...")
    page.goto(BASE_URL, timeout=60_000)

    # Пытаемся нажать "Вход" или "Войти"
    try:
        page.click("text='Вход'")
    except Exception:
        try:
            page.click("text='Войти'")
        except Exception:
            log.info("Не удалось найти кнопку 'Вход'/'Войти' — возможно, уже на странице логина.")

    page.wait_for_timeout(1000)

    # Находим поля логина/пароля
    login_locators = [
        "input[name='login']",
        "input[name='email']",
        "input[name='username']",
    ]
    password_locators = [
        "input[name='password']",
        "input[type='password']",
    ]

    filled_login = False
    for sel in login_locators:
        loc = page.locator(sel)
        if loc.count():
            loc.fill(login)
            filled_login = True
            break

    if not filled_login:
        log.warning("Не удалось найти поле логина на странице. Логин может не сработать.")

    filled_password = False
    for sel in password_locators:
        loc = page.locator(sel)
        if loc.count():
            loc.fill(password)
            filled_password = True
            break

    if not filled_password:
        log.warning("Не удалось найти поле пароля на странице. Логин может не сработать.")

    # Пытаемся нажать кнопку входа
    try:
        page.click("button[type='submit']")
    except Exception:
        # fallback: Enter по полю пароля
        try:
            page.keyboard.press("Enter")
        except Exception:
            log.warning("Не удалось нажать submit при логине.")

    # Ждём, пока личный кабинет прогрузится
    page.wait_for_timeout(3000)


def _parse_page_html(html: str, min_date: date, tenders_by_number: Dict[str, Tender]) -> int:
    """
    Парсим HTML одной страницы шаблона и добавляем новые тендеры в словарь.
    Возвращает, сколько новых тендеров добавили.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    added = 0

    for number, date_str, body in _iter_blocks(text):
        # дата публикации "от 23.11.25"
        try:
            d = datetime.strptime(date_str, "%d.%m.%y").date()
        except ValueError:
            log.warning("Не смог распарсить дату публикации %r у тендера %s", date_str, number)
            continue

        if d < min_date:
            # старый тендер — дальше по нему не идём
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

        url = f"https://rostender.info/tender?search={number}"

        t = Tender(
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
        tenders_by_number[number] = t
        added += 1

    return added


def fetch_rostender_tenders_from_template(
    template_env_var: str,
    days: int = 1,
    max_pages: int = 5,
) -> List[Tender]:
    """
    Логинится на Ростендер и парсит тендеры из шаблона,
    URL которого хранится в переменной окружения template_env_var
    (например, 'ROSTENDER_TEMPLATE_ACTIVE_URL').

    Возвращает список Tender за последние days дней.
    """
    login = os.getenv("ROSTENDER_LOGIN", "").strip()
    password = os.getenv("ROSTENDER_PASSWORD", "").strip()
    template_url = os.getenv(template_env_var, "").strip()

    if not login or not password:
        raise RuntimeError("Не заданы ROSTENDER_LOGIN / ROSTENDER_PASSWORD в .env")

    if not template_url:
        raise RuntimeError(f"Не задан {template_env_var} в .env")

    today = date.today()
    min_date = today - timedelta(days=days)

    tenders_by_number: Dict[str, Tender] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        _login(page, login, password)

        for page_num in range(1, max_pages + 1):
            url = _build_page_url(template_url, page_num)
            log.info("Открываю шаблон %s (страница %s): %s", template_env_var, page_num, url)
            page.goto(url, timeout=60_000)
            page.wait_for_timeout(1500)

            html = page.content()
            added = _parse_page_html(html, min_date, tenders_by_number)

            log.info("Шаблон %s, страница %s: добавлено %s новых тендеров", template_env_var, page_num, added)

            if added == 0:
                # Если на странице ничего свежего не нашли — дальше листать смысла нет
                break

        browser.close()

    results = list(tenders_by_number.values())
    results.sort(key=lambda t: (t.published, t.number), reverse=True)

    log.info(
        "Итого по шаблону %s: %d тендеров за последние %d дн. (до %d страниц)",
        template_env_var,
        len(results),
        days,
        max_pages,
    )
    return results

