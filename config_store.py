from __future__ import annotations

import json
import os
from threading import Lock
from typing import List

BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

_lock = Lock()

DEFAULT_CONFIG = {
    # старое поле, пусть живёт для совместимости
    "rostender_filter_url": "",
    # текст системного промпта для GPT
    "gpt_filter_text": (
        "Ты помогаешь компании МЦЭ Инжиниринг отбирать тендеры. "
        "Компания занимается узлами учёта, СИКГ, КИПиА, системами контроля загазованности, "
        "газоанализаторами, шкафами автоматики и дозированием реагентов. "
        "По каждому тендеру отвечай строго JSON:\n"
        '{\n  "is_match": true/false,\n  "reason": "краткое объяснение"\n}\n'
        "Не добавляй никаких ```json и других обёрток — только чистый JSON."
    ),
    # фильтры Ростендера
    "keywords": [],            # положительные слова
    "exclude_keywords": [],    # исключения
    "city": "",                # фильтр по городу (подстрока)

    # параметры поиска
    "search_days": 3,          # за сколько дней смотреть тендеры
    "max_pages": 2,            # сколько страниц Ростендера листать
}


def _read_config_raw() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return DEFAULT_CONFIG.copy()

    cfg = DEFAULT_CONFIG.copy()
    cfg.update(data)
    return cfg


def _write_config_raw(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CONFIG_PATH)


# ============= ROSTENDER URL (на будущее, если вдруг) =============


def get_rostender_filter_url() -> str:
    with _lock:
        cfg = _read_config_raw()
    return cfg.get("rostender_filter_url", "").strip()


def set_rostender_filter_url(url: str) -> None:
    with _lock:
        cfg = _read_config_raw()
        cfg["rostender_filter_url"] = url.strip()
        _write_config_raw(cfg)


# ============= GPT FILTER TEXT =============


def get_gpt_filter_text() -> str:
    with _lock:
        cfg = _read_config_raw()
    return cfg.get("gpt_filter_text", DEFAULT_CONFIG["gpt_filter_text"])


def set_gpt_filter_text(text: str) -> None:
    with _lock:
        cfg = _read_config_raw()
        cfg["gpt_filter_text"] = text
        _write_config_raw(cfg)


# ============= ROSTENDER KEYWORDS / EXCLUDE / CITY =============


def get_keywords() -> List[str]:
    with _lock:
        cfg = _read_config_raw()
    value = cfg.get("keywords", [])
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def set_keywords(words: List[str]) -> None:
    clean = [str(w).strip() for w in (words or []) if str(w).strip()]
    with _lock:
        cfg = _read_config_raw()
        cfg["keywords"] = clean
        _write_config_raw(cfg)


def get_exclude_keywords() -> List[str]:
    with _lock:
        cfg = _read_config_raw()
    value = cfg.get("exclude_keywords", [])
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def set_exclude_keywords(words: List[str]) -> None:
    clean = [str(w).strip() for w in (words or []) if str(w).strip()]
    with _lock:
        cfg = _read_config_raw()
        cfg["exclude_keywords"] = clean
        _write_config_raw(cfg)


def get_city() -> str:
    with _lock:
        cfg = _read_config_raw()
    return str(cfg.get("city", "") or "").strip()


def set_city(city: str) -> None:
    with _lock:
        cfg = _read_config_raw()
        cfg["city"] = str(city or "").strip()
        _write_config_raw(cfg)


# ============= SEARCH DAYS / MAX PAGES =============


def get_search_days() -> int:
    with _lock:
        cfg = _read_config_raw()
    value = cfg.get("search_days", DEFAULT_CONFIG["search_days"])
    try:
        days = int(value)
    except Exception:
        days = DEFAULT_CONFIG["search_days"]
    if days < 1:
        days = 1
    if days > 30:
        days = 30
    return days


def set_search_days(days: int) -> None:
    try:
        d = int(days)
    except Exception:
        d = DEFAULT_CONFIG["search_days"]
    if d < 1:
        d = 1
    if d > 30:
        d = 30
    with _lock:
        cfg = _read_config_raw()
        cfg["search_days"] = d
        _write_config_raw(cfg)


def get_max_pages() -> int:
    with _lock:
        cfg = _read_config_raw()
    value = cfg.get("max_pages", DEFAULT_CONFIG["max_pages"])
    try:
        pages = int(value)
    except Exception:
        pages = DEFAULT_CONFIG["max_pages"]
    if pages < 1:
        pages = 1
    if pages > 10:
        pages = 10
    return pages


def set_max_pages(pages: int) -> None:
    try:
        p = int(pages)
    except Exception:
        p = DEFAULT_CONFIG["max_pages"]
    if p < 1:
        p = 1
    if p > 10:
        p = 10
    with _lock:
        cfg = _read_config_raw()
        cfg["max_pages"] = p
        _write_config_raw(cfg)

