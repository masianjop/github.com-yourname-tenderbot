from __future__ import annotations

import json
import os
from threading import Lock

BASE_DIR = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

_lock = Lock()

DEFAULT_CONFIG = {
    "rostender_filter_url": "",   # сюда подставим текущий рабочий URL
    "gpt_filter_text": (
        "Ты помогаешь компании МЦЭ Инжиниринг отбирать тендеры. "
        "Компания занимается узлами учёта, СИКГ, КИПиА, системами контроля загазованности, "
        "газоанализаторами, шкафами автоматики и дозированием реагентов. "
        "По каждому тендеру отвечай строго JSON:\n"
        '{\n  "is_match": true/false,\n  "reason": "краткое объяснение"\n}\n'
        "Не добавляй никаких ```json и других обёрток — только чистый JSON."
    ),
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


def get_rostender_filter_url() -> str:
    with _lock:
        cfg = _read_config_raw()
    return cfg.get("rostender_filter_url", "").strip()


def set_rostender_filter_url(url: str) -> None:
    with _lock:
        cfg = _read_config_raw()
        cfg["rostender_filter_url"] = url.strip()
        _write_config_raw(cfg)


def get_gpt_filter_text() -> str:
    with _lock:
        cfg = _read_config_raw()
    return cfg.get("gpt_filter_text", DEFAULT_CONFIG["gpt_filter_text"])


def set_gpt_filter_text(text: str) -> None:
    with _lock:
        cfg = _read_config_raw()
        cfg["gpt_filter_text"] = text
        _write_config_raw(cfg)

