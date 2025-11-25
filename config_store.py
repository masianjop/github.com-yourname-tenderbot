from __future__ import annotations

import json
import os
from threading import Lock

BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

_lock = Lock()

DEFAULT_CONFIG = {
    "rostender_filter_url": "",
    "gpt_filter_text": "",
    "keywords": [],
    "exclude_keywords": [],
    "city": ""
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
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ====== GETTERS ======

def get_keywords() -> list[str]:
    with _lock:
        return _read_config_raw().get("keywords", [])


def get_exclude_keywords() -> list[str]:
    with _lock:
        return _read_config_raw().get("exclude_keywords", [])


def get_city() -> str:
    with _lock:
        return _read_config_raw().get("city", "").strip()


def get_gpt_filter_text() -> str:
    with _lock:
        return _read_config_raw().get("gpt_filter_text", "")


# ====== SETTERS ======

def set_keywords(items: list[str]) -> None:
    with _lock:
        cfg = _read_config_raw()
        cfg["keywords"] = items
        _write_config_raw(cfg)


def set_exclude_keywords(items: list[str]) -> None:
    with _lock:
        cfg = _read_config_raw()
        cfg["exclude_keywords"] = items
        _write_config_raw(cfg)


def set_city(city: str) -> None:
    with _lock:
        cfg = _read_config_raw()
        cfg["city"] = city.strip()
        _write_config_raw(cfg)


def set_gpt_filter_text(text: str) -> None:
    with _lock:
        cfg = _read_config_raw()
        cfg["gpt_filter_text"] = text
        _write_config_raw(cfg)

