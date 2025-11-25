from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Tender:
    """Базовая модель тендера, с которой работает и парсер, и ИИ."""
    id: Optional[str]
    title: str
    url: str

    customer: Optional[str] = None
    location: Optional[str] = None
    price: Optional[str] = None

    published_at: Optional[datetime] = None
    end_at: Optional[datetime] = None

    description: Optional[str] = None  # строка "Предмет тендера: ..."

