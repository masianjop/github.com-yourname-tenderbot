from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, List, Tuple

import httpx
from dotenv import load_dotenv   # <<< добавили

from config_store import get_gpt_filter_text

load_dotenv()   # <<< добавили

log = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY не задан — GPT-функции работать не будут.")

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()  # можно поменять


@dataclass
class GPTResult:
    code: str
    is_match: bool
    reason: str


def _parse_gpt_json(raw: str, tender_code: str) -> dict | None:
    """
    Парсим JSON от GPT. Убираем ```json ... ``` и прочий мусор.
    """
    text = raw.strip()

    # вырезаем обёртки ```json ... ```
    if text.startswith("```"):
        # убираем первую строку (``` или ```json)
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        # если последний блок ``` — убираем
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except Exception as e:
        log.warning(
            "Не удалось распарсить JSON от GPT для %s: %s; сырой ответ: %r",
            tender_code,
            e,
            raw,
        )
        return None


def ask_gpt_about_tenders(
    items: List[Tuple[Any, Any]],
) -> List[GPTResult]:
    """
    items: список (Tender, local_analysis), но мы не тащим типы из mce_filter/rostender_parser для простоты.
    Для каждого тендера спрашиваем GPT: наш / не наш + причина.
    """

    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY не задан, возвращаю пустой список из GPT.")
        return []

    system_prompt = get_gpt_filter_text()
    results: List[GPTResult] = []

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=40) as client:
        for tender, local in items:
            code = getattr(tender, "number", "unknown")
            title = getattr(tender, "title", "")
            detail = getattr(tender, "detail_text", "") or getattr(tender, "raw_block", "")

            # режем описание, чтобы не жрать токены
            if len(detail) > 2000:
                detail_cut = detail[:2000] + "... (обрезано)"
            else:
                detail_cut = detail

            user_prompt = (
                "Оцени, подходит ли этот тендер под профиль компании МЦЭ Инжиниринг.\n\n"
                f"Номер: {code}\n"
                f"Название: {title}\n"
                f"Описание:\n{detail_cut}\n\n"
                "Ответь строго в формате JSON БЕЗ каких-либо комментариев и обёрток, "
                "строго так:\n"
                '{\n  "is_match": true/false,\n  "reason": "краткое объяснение на русском"\n}\n'
            )

            payload = {
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
            }

            try:
                resp = client.post(OPENAI_URL, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            except Exception as e:
                log.error("Ошибка при обращении к GPT для тендера %s: %s", code, e)
                continue

            parsed = _parse_gpt_json(content, code)
            if not parsed or "is_match" not in parsed:
                # если что-то не так — просто пропускаем
                continue

            is_match = bool(parsed.get("is_match"))
            reason = str(parsed.get("reason") or "").strip() or "Причина не указана GPT."

            results.append(GPTResult(code=code, is_match=is_match, reason=reason))

    return results

