from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Set


def _norm(text: str) -> str:
    """
    Нормализация текста:
    - в нижний регистр
    - сжать пробелы
    - добавить пробелы по краям, чтобы проще ловить фразы
    """
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t)
    return f" {t} "


# === НАПРАВЛЕНИЯ (ТОЧЕЧНЫЕ) ===

TOP_DIRECTIONS = {
    # Узлы учёта / измерения
    "узел учета",
    "узел учёта",
    "узел учета газа",
    "узел учёта газа",
    "узел учета нефти",
    "узел учёта нефти",
    "узел учета конденсата",
    "узел учёта конденсата",
    "узел измерения расхода газа",
    "узел измерения расхода жидкости",
    "узел учета тепловой энергии",
    "узел учёта тепловой энергии",

    # СИК*
    "сикг",
    "сикн",
    "сикв",
    "сикнс",
    "системы измерения количества газа",
    "система измерения количества газа",

    # Дозирование, реагенты
    "дозирование метанола",
    "станция дозирования",
    "насос дозирования",
    "дозирующая станция",
    "дозировочная станция",

    # Газоанализ / хроматографы
    "газоанализ",
    "газоанализатор",
    "газоанализаторы",
    "газовый хроматограф",
    "хроматограф",
    "хроматографы",
    "поточный хроматограф",

    # АСУ, КИПиА
    "кипиa",
    "кип и а",
    "кипиа",
    "контрольно-измерительные приборы",
    "контрольно измерительные приборы",
    "автоматизация технологического процесса",
    "шкаф автоматики",
    "шкаф управления",
}

OTHER_DIRECTIONS = {
    "датчик",
    "датчики",
    "расходомер",
    "расходомеры",
    "счётчик",
    "счетчик",
    "счётчики",
    "манометр",
    "преобразователь давления",
    "термопреобразователь",
    "вычислитель",
    "шкаф учета",
    "шкаф учёта",
    "шкаф сигнализации",
    "шкаф телемеханики",
    "телемеханика",
    "телеметрия",
    "сигнализатор",
    "анализатор",
    "анализаторы",
    "щит управления",
    "щит автоматики",
    "шкаф управления насосами",
}


# === ОТРИЦАТЕЛЬНЫЕ ТЕМЫ (ЧТО ТОЧНО НЕ НАШЕ) ===

BAD_TOPICS = {
    # Медицина, лекарства, СИЗ
    "лекарственн",
    "медицинск",
    "медицинское оборудование",
    "медицинские изделия",
    "средств индивидуальной защиты",
    "сиз",

    # Одежда, обувь, текстиль
    "обувь",
    "обуви",
    "одежда",
    "постельное бельё",
    "постельное белье",
    "портьеры",
    "шторы",
    "занавески",
    "ткани",

    # Быт, вода, еда
    "вода питьевая",
    "бутилированная вода",
    "питьевая вода",
    "продукты питания",
    "продовольств",
    "кейтеринг",
    "столовая",
    "буфет",

    # Лифты, здания, общестрой, ремонт
    "лифтов",
    "лифт",
    "лифтовое оборудование",
    "ремонт здания",
    "ремонт помещений",
    "капитальный ремонт",
    "строительно-монтажные работы",
    "строительно монтажные работы",
    "строительство",
    "отделочные работы",
    "ремонт кровли",
    "фасад",

    # Услуги общего типа
    "уборка помещений",
    "клининг",
    "вывоз мусора",
    "охрана объекта",
    "охранные услуги",
    "услуги такси",
    "пассажирские перевозки",

    # Канцелярия и расходники
    "канцелярск",
    "канцелярские товары",
    "бумага офисная",
    "картриджей",
    "картриджи",
    "заправка картриджей",
    "оргтехника",

    # Прочее явное мимо
    "мебель",
    "офисная мебель",
    "мягкая мебель",
    "игрушки",
    "книг",
    "книги",
    "игрушек",
}


@dataclass
class LocalAnalysis:
    code: str
    title: str
    url: str
    customer: str
    description: str
    is_local_match: bool
    priority_level: int | None
    matched_keywords: List[str] = field(default_factory=list)
    negative_reasons: List[str] = field(default_factory=list)


def _find_keywords(text: str, keywords: Set[str]) -> Set[str]:
    """
    Ищем ключевые слова/фразы в нормализованном тексте.
    """
    hits: Set[str] = set()
    for kw in keywords:
        kw_norm = kw.lower()
        if " " in kw_norm or len(kw_norm) >= 4:
            if kw_norm in text:
                hits.add(kw)
        else:
            if re.search(r"\b" + re.escape(kw_norm) + r"\b", text):
                hits.add(kw)
    return hits


def analyze_tender(
    code: str,
    title: str,
    url: str,
    customer: str,
    description: str,
) -> LocalAnalysis:
    """
    Очень мягкий локальный фильтр:
    - если есть BAD_TOPICS -> сразу мимо;
    - всё остальное считаем кандидатом (is_local_match = True),
      а приоритет выставляем:
        1 — если есть TOP_DIRECTIONS,
        2 — если есть OTHER_DIRECTIONS,
        0 — если просто «что-то вокруг» без наших ключей.
    Остальное режем по MAX_GPT_TENDERS в tg_bot.py.
    """

    text = _norm(f"{title} {description}")

    top_hits = _find_keywords(text, TOP_DIRECTIONS)
    other_hits = _find_keywords(text, OTHER_DIRECTIONS)
    bad_hits = _find_keywords(text, BAD_TOPICS)

    # если есть негативные темы — сразу мимо
    if bad_hits:
        return LocalAnalysis(
            code=code,
            title=title,
            url=url,
            customer=customer,
            description=description,
            is_local_match=False,
            priority_level=None,
            matched_keywords=sorted(top_hits | other_hits),
            negative_reasons=sorted(bad_hits),
        )

    # здесь уже точно нет BAD_TOPICS — считаем кандидатом
    if top_hits:
        priority = 1
    elif other_hits:
        priority = 2
    else:
        priority = 0  # нейтральный, но всё равно пойдёт в GPT, если попадёт в топ-12

    return LocalAnalysis(
        code=code,
        title=title,
        url=url,
        customer=customer,
        description=description,
        is_local_match=True,
        priority_level=priority,
        matched_keywords=sorted(top_hits | other_hits),
        negative_reasons=[],
    )

