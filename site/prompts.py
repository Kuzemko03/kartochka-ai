"""Prompt templates for Claude.

БОЕВЫЕ промпты (реально подключены к вызовам):
  • SYSTEM_PROMPT             — pack design from explicit product fields (advanced mode)
  • BRIEF_SYSTEM_PROMPT       — pack design from a free-form brief
  • COMPETITOR_BRIEF_SYSTEM_PROMPT — pack design + анализ конкурентов (основной режим сайта)
  • VARIATIONS_SYSTEM_PROMPT  — ONE main card, N intentionally different artistic takes
  • DESCRIPTION_SYSTEM_PROMPT — marketplace listing description (short, punchy)
  • VIDEO_MOTION_SYSTEM_PROMPT— per-card motion prompt for image-to-video
  • WB_CORE                   — ядро методологии, конкатенируется в три дизайн-промпта
  • STYLE_REF_RULE            — вставляется в user prompt через _refs_block при наличии реф-стиля

⚠️ АРХИВ МЕТОДОЛОГИИ (НЕ подключены ни к одному вызову — источник для код-гейтов
и для будущих компакт-вставок; НЕ ссылайся на них из боевых промптов по имени):
  PACK_CONSISTENCY_RULE, COPY_RULES, COPY_ENGINE, HOOK_FORMULA_SLOTS,
  WB_CARD_ANATOMY, PACK_TEXT_DISCIPLINE, QUALITY_BAR (живёт только в VARIATIONS),
  SELF_CRITIQUE_PHASE. Их рабочие выжимки уже внесены в WB_CORE (hook-таблица,
  анти-паспорт) и в код (bland_reason / card_copy_issues / _strip_unverified_claims).

KEY PRINCIPLES (encoded in these prompts):

1. PACK CONSISTENCY. A pack of cards is a SERIES — one palette, one type system,
   one composition grid, one light setup. Cards differ only in CONTENT (which
   feature is highlighted, how the product is framed). This matches how MPCard /
   Neiro-Card / Sellerden actually structure packs and how WB sellers expect
   them. Variations mode is the place for radical visual difference.

2. STYLE REF DOMINANCE. If the user attaches a reference card image, Claude SEES
   it (multimodal vision) and extracts its concrete visual language: hex palette,
   typography style/weight, composition system, light direction, graphic accents.
   Those concretes get baked into every image_prompt. The style-picker dropdown
   becomes irrelevant.

3. STYLE REFS GO TO IMAGE API WITH A ROLE ANCHOR. Style refs идут и в Claude
   vision (extracted_style), и в image API первыми в списке рефов — но промпт
   обязан содержать role-anchor: «styleRef = только стиль, НИКОГДА не копируй
   его товар/текст» (см. generator._style_ref_nudge). Product refs (фото товара)
   идут в image API как product1..N — единственный источник истины о товаре.

4. NO PILLOW OVERLAY. The image API renders Cyrillic well — text is baked into
   the image_prompt with explicit font/weight/color/position.

5. VIDEO: TEXT NEVER MOVES. Text is fixed on the card image. Motion is for
   the product (slow rotation, micro-zoom, parallax shadow, light pulse) — like
   WB/Ozon native video cards, not "AI music video".

6. MAIN ≠ DETAIL. Главная (type=main) проектируется и собирается ОТДЕЛЬНОЙ веткой:
   инфографика-обложка (TITLE-NAME + товар 60-75% + 3-4 icon-callouts + анти-паспорт
   фон). Detail-карточки (feature/lifestyle/comparison) — раскрытие ОДНОЙ фишки,
   товар 35-55%, свой scene_override. Сборщик generator._build_image_prompt держит
   эти ветки синхронно с WB_CORE — менять анатомию только в обоих местах сразу.
"""

import json
import re


# ════════════════════════════════════════════════════════════════════════════
# PACK CONSISTENCY — ⚠️ АРХИВ: НЕ подключён к боевым промптам (см. шапку файла).
# Рабочие выжимки перенесены в WB_CORE (анти-паспорт) и generator._build_image_prompt.
# ════════════════════════════════════════════════════════════════════════════
PACK_CONSISTENCY_RULE = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# QUALITY BAR (appended to design prompts)
# ════════════════════════════════════════════════════════════════════════════
COPY_RULES = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# COPY ENGINE — позитивный генератор крючков (раньше COPY_RULES был почти весь
# из запретов; без «движка» Claude на товаре без фактов выдумывал фичи/числа).
# ════════════════════════════════════════════════════════════════════════════
COPY_ENGINE = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


HOOK_FORMULA_SLOTS = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


WB_CARD_ANATOMY = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


PACK_TEXT_DISCIPLINE = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


QUALITY_BAR = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# STYLE REF DOMINANCE rule (appended when style_ref is present)
# ════════════════════════════════════════════════════════════════════════════
STYLE_REF_RULE = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# ART DIRECTION DIRECTIVE — задаётся, когда НЕТ реф-карточки и юзер не выбрал
# стиль вручную. Каждый прогон получает СВОЁ направление (см. formats.ART_DIRECTIONS),
# чтобы разные генерации не были на одно лицо. Внутри пака — направление одно.
# .format(signature=..., accent=..., secondary=...)
# ════════════════════════════════════════════════════════════════════════════
ART_DIRECTION_DIRECTIVE = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPTS
# ════════════════════════════════════════════════════════════════════════════

SELF_CRITIQUE_PHASE = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


WB_CORE = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


SYSTEM_PROMPT = """\
Ты — WB-маркетолог + арт-директор + копирайтер. Делаешь карточки товара для маркетплейсов (Wildberries / Ozon / Я.Маркет). Цель — максимальный CTR в выдаче, а не «красивая картинка».

Отвечай СТРОГО валидным JSON, без markdown, без префиксов. Имена полей точно как ниже (headline_ru / sub_ru / badges_ru / footer_ru / format_key / id / hook_formula).

""" + WB_CORE + """[prompt omitted from public repo -- see docs/prompt-system.md]"""


FACTUAL_HOOK_RULE = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


BRIEF_SYSTEM_PROMPT = """\
Ты — WB-маркетолог + копирайтер. Пользователь даёт СВОБОДНОЕ описание товара. Извлеки структуру и спроектируй пак карточек.
""" + FACTUAL_HOOK_RULE + """
Шаги:
1. Извлеки: name, brand, category, usp, features (3-6), audience, price_range. Что не дано — додумай разумно, но НЕ выдумывай сертификатов/материалов/чисел.
2. Спроектируй пак по WB-анатомии (см. ниже).

Отвечай СТРОГО валидным JSON, без markdown.

""" + WB_CORE + """[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# SPLIT-РЕЖИМ: text-agent + visual-agent (config.design_split = true)
# ════════════════════════════════════════════════════════════════════════════
#
# Раньше один single-shot писал И тексты, И scene-блок, И extracted_style,
# И _draft_thinking. Модель «распыляла» внимание, тексты получались
# средними, copy_fix-проход потом дописывал. Split разделяет работу:
#
#   STAGE 1 (TEXT_DESIGN) — копирайтер. Output: cards с headline_ru / sub_ru
#     / badges_ru / footer_ru / hook_formula / role_in_story + pack_concept.
#     БЕЗ scene_block / extracted_style — это другой агент.
#     Можно положить на gpt-4.1-mini (5× дешевле). Vision не нужно.
#
#   STAGE 2 (VISUAL_DESIGN) — арт-директор. Получает ВЕСЬ output Stage 1 +
#     visual-контекст. Output: pack_scene_block / extracted_style /
#     per-card scene_override (привязаны к hook каждой карточки).
#     gpt-4.1 + style_ref vision (detail=low) если есть.
#
# Координация: Stage 2 видит все texts из Stage 1 → сцены отражают хук
# каждой карточки. Merge — generator._design_pack_split.
#
# ⚠️ hook_formula: у Stage 1 СВОЯ таксономия (1=SPEC-ANCHOR … 7=PROOF), у
# WB_CORE/сингл-шота — другая (1=БРЕНД … 7=ЮМОР). Код семантику числа нигде не
# читает (поле хранится для наблюдаемости), поэтому НЕ ссылайся на номера формул
# в downstream-промптах по номеру между путями — только внутри одного системника
# (copy_fix ссылается на СВОЙ список углов, это ок).
# ════════════════════════════════════════════════════════════════════════════


TEXT_DESIGN_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]""" + FACTUAL_HOOK_RULE + """[prompt omitted from public repo -- see docs/prompt-system.md]"""


VISUAL_DESIGN_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


VARIATIONS_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]""" + QUALITY_BAR + """

СТРУКТУРА:
{
  "concept": "общая идея — какой товар, target",
  "variants": [
    {
      "id": "var_1",
      "style_label": "premium dark luxury",
      "palette": {"accent": "#HEX", "secondary": "#HEX"},
      "headline_ru": "...", "sub_ru": "...", "badges_ru": ["...", "..."],
      "image_prompt": "<полный детальный промпт по QUALITY_BAR>"
    }
  ]
}
"""


POOL_IDENTIFY_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


DESCRIPTION_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


VIDEO_EFFECT_SPECS = {
    "auto": "Pick ONE dominant effect from: rotate / zoom / glow / parallax. Match the product type — for hard goods (laptops, watches) prefer rotation; for liquids/perfume prefer steam+glow; for fabrics/cosmetics prefer micro-zoom.",
    "rotate": "SLOW PRODUCT ROTATION ONLY. The product rotates 10-15 degrees around its vertical axis at constant slow speed, like a turntable. NO other camera motion. NO zoom. NO glow.",
    "zoom":   "MICRO ZOOM-IN ONLY. Camera slowly zooms in from 1.0x to 1.05x over 4-6 seconds with smooth easing. The product itself does NOT move or rotate. NO glow effects.",
    "glow":   "LIGHT GLOW / RIM PULSE ONLY. A warm rim light slowly sweeps across the product surface, with subtle warm glow pulsing 1-2 times. Product is STATIC, no rotation, no zoom.",
    "parallax": "PARALLAX ONLY. Background shifts subtly by 3-5%; the product stays exactly in place creating a 3D-depth illusion. NO product rotation, NO zoom.",
    "particles": "PARTICLE DRIFT ONLY. Soft steam / dust particles drift slowly upward or sideways behind the product. Product is fully static. NO other movement.",
}


VIDEO_MOTION_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# USER PROMPT BUILDERS
# ════════════════════════════════════════════════════════════════════════════

def _refs_block(has_refs, ref_names, has_style_ref):
    """Style refs go to Claude vision (low-detail) for style/vibe parsing.
    Product refs (актуальные фото товара) идут ТОЛЬКО в image API как
    reference_images. В design-вызов их картинки НЕ передаются — товар уже
    распознан отдельным detect_category_from_photos (см. wb_analyzer.py),
    а его текстовое описание лежит в product_description / quick_facts /
    extracted_text payload'а. Это экономит ~2100 vision-токенов на каждое
    фото и спасает от OpenAI per-org TPM rate-limit на больших промптах
    (HTTP 429 "Request too large").
    """
    parts = []
    if has_refs:
        names_str = ", ".join(f'"{n}"' for n in ref_names)
        parts.append(
            f"У товара есть {len(ref_names)} реф-фото под именами {names_str} — "
            f"они уйдут в image-API как reference_images и подмешают РЕАЛЬНЫЙ "
            f"товар в рендер. Ты эти фото НЕ ВИДИШЬ: фактуру (форма, цвет, "
            f"материал, пропорции) бери из product_description / quick_facts / "
            f"extracted_text. В scene_override упоминай имена реф-фото ДОСЛОВНО "
            f"(например: 'a hand holding the {ref_names[0]} label toward the "
            f"camera') — это маркер для image-API. Формулируй кадр через камеру/"
            f"руки/сам товар, БЕЗ места и фона («on a table» движок вырежет)."
        )
    else:
        parts.append(
            "Реф-фото товара НЕТ. Детально опиши товар (форма, цвет, материал, пропорции) в каждом image_prompt."
        )
    if has_style_ref:
        parts.append(STYLE_REF_RULE)
    return "\n\n".join(parts)


# ════════════════════════════════════════════════════════════════════════════
# FACTS DISCIPLINE — лечит баг "AI выдумывает 330 мл, СВЧ-safe и т.п."
# ════════════════════════════════════════════════════════════════════════════

def _facts_block(user_facts: dict | None) -> str:
    """Жёсткое правило: использовать ТОЛЬКО подтверждённые юзером факты.
    Если факт не указан — НЕ писать конкретное число/значение.
    Возвращает пустую строку если facts нет (тогда default-разрешение AI «общие фразы»).
    """
    if not user_facts:
        return ("""[prompt omitted from public repo -- see docs/prompt-system.md]""")

    # Есть факты — даём AI их использовать как «разрешённые»
    fact_lines = []
    for key, value in user_facts.items():
        if value is None or value == "" or value == "не_указано":
            continue
        fact_lines.append(f"- {key}: {value}")
    if not fact_lines:
        return _facts_block(None)  # пусто внутри → тот же запрет

    # ФАКТ-ОТСУТСТВИЕ: значения «без X / нет X» — это КОНТЕКСТ, не текст карточки.
    # Клауза инжектится ТОЛЬКО когда среди фактов реально есть отрицание (на товарах
    # без таких фактов — 0 лишних токенов; принцип всегда есть и в no-facts ветке, и
    # в TEXT_DESIGN self-check). Сама клауза различает «боль покупателя» (оставить:
    # «без проводов», «без сахара») и «такой товар и не умеет» (молчать).
    _has_absence = any(
        re.match(r"^\s*(без|нет|no|without)\b", str(v), re.I)
        for v in user_facts.values() if isinstance(v, str)
    )
    _absence_note = ("""[prompt omitted from public repo -- see docs/prompt-system.md]""" if _has_absence else "")

    return f"""

🛡 ПОДТВЕРЖДЁННЫЕ ФАКТЫ ОТ ПОЛЬЗОВАТЕЛЯ — ТОЛЬКО ЭТИ КОНКРЕТНЫЕ ЗНАЧЕНИЯ \
МОЖНО ПИСАТЬ НА КАРТОЧКАХ:
{chr(10).join(fact_lines)}
{_absence_note}[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# BRIEF → FACTS  (лечит баг: юзер пишет «Материал: фаянс / 200 мл» в свободный
# бриф, но user_facts остаётся пустым → срабатывает ветка «фактов нет»)
# ════════════════════════════════════════════════════════════════════════════

# Единицы измерения — токен «<число> <единица>» считаем подтверждённым фактом.
_MEASURE_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s?(?:мл|л|кг|гр|г|см|мм|м|°c|°|вт|w|квт|мач|mah|"
    r"гб|gb|тб|tb|мп|mp|дюйм(?:ов|а|е)?|\"|″|′|％|%|₽|руб(?:лей|ля|ль|\.)?|"
    r"шт|years?|лет|года?|час(?:ов|а)?|мин|сек|ppi|hz|гц|об/мин|ватт)(?=\W|$)",
    re.IGNORECASE,
)  # хвост (?=\W|$) вместо \b: ловит «15.6"», «90%», «2990₽» в конце строки/перед
   # пробелом/пунктуацией (символьные единицы на \b не матчились → терялись из
   # фактов и резались копирайт-гейтом как «выдумано»). Также: склонения
   # «рублей/рубля/рубль», «дюйма/дюймов» и unicode-штрих ″/′ (частые в брифах).
   # Порядок кг|гр|г важен — длинные альтернативы раньше коротких.
# Метки «Ключ: значение» в брифе — самый надёжный источник фактов.
_KV_RE = re.compile(r"^\s*([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9 /()\-]{1,38}?)\s*[:：]\s*(.+?)\s*$")
# Ключи, которые НЕ являются спецификацией товара (не тащим их в «факты»).
_KV_SKIP_KEYS = {"бриф", "описание", "стиль", "формат", "цена", "ссылка", "url",
                 "примечание", "комментарий", "задача", "title", "заголовок"}


def extract_facts_from_brief(brief) -> dict:
    """Достаёт подтверждённые факты из СВОБОДНОГО текста брифа, чтобы они дошли
    до модели как «разрешённая конкретика» (а не терялись из-за пустого user_facts).
    Берём только надёжные сигналы: строки «Ключ: значение» и токены «<число> <единица>».
    Ничего не выдумывает — если в тексте нет, не появится."""
    if not brief or not isinstance(brief, str):
        return {}
    facts: dict = {}
    for raw_line in brief.splitlines():
        line = raw_line.strip(" •-—\t")
        if not line:
            continue
        m = _KV_RE.match(line)
        if m:
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            if (key in _KV_SKIP_KEYS or len(val) > 60 or not val
                    or val.lower() in ("не указано", "—", "-", "не_указано")):
                continue
            facts[key] = val
    # Свободно стоящие измерения (вне «ключ: значение») — собираем отдельным фактом.
    inline = []
    for m in _MEASURE_RE.finditer(brief):
        token = m.group(0).strip()
        if token and token not in inline and not any(token in v for v in facts.values()):
            inline.append(token)
    if inline:
        facts.setdefault("характеристики из брифа", ", ".join(inline[:6]))
    return facts


# ════════════════════════════════════════════════════════════════════════════
# COPY QUALITY GATE — детектор «воды» (детерминированный) + переписыватель (LLM).
# COPY_RULES раньше существовали только как инструкция в промпте; теперь код их
# реально применяет: ловит пустые эпитеты и заставляет переписать ТЕКСТ.
# ════════════════════════════════════════════════════════════════════════════

# Фразы-вата целиком (нормализованные: lower, ё→е). Подходят к любому товару → БРАК.
# Расширено после баг-репорта 2026-06-02 («кружка с резным лицом» — большинство
# headline'ов прошли мимо старого whitelist'a: «Лицо утра», «Утро с характером»,
# «Яркий акцент цвета», «Безупречная текстура» и т.д.).
_BLAND_WHOLE = {
    # Старый набор
    "приятный материал", "удобный объем", "удобная ручка", "удобный дизайн",
    "удобное управление", "интуитивное управление", "легкое управление",
    "легкий вес", "четкий дисплей", "надежная упаковка", "чистая элегантность",
    "легкость и уют", "качественный материал", "высокое качество",
    "комфорт дома", "удобно держать", "качество в каждой детали",
    "стильный дизайн", "современный дизайн", "удобство и комфорт",
    "приятный на ощупь", "продуманный дизайн", "идеальный выбор",
    "элегантность и стиль", "комфорт и уют", "надежно и удобно",
    # Реальные брак-headline'ы из паков
    "яркий акцент цвета", "яркий акцент", "акцент цвета",
    "выразительный дизайн", "выразительный стиль", "уникальный стиль",
    "безупречная текстура", "безупречное качество", "безупречный стиль",
    "нестандартный формат", "нестандартный дизайн", "необычный формат",
    "оригинальный дизайн", "оригинальный формат", "характерный стиль",
    "лицо утра", "лицо вечера", "утро с характером", "вечер с характером",
    "день с характером", "ночь с характером",
    "подарок с характером", "подарок с душой", "подарок с историей",
    "вкус утра", "вкус победы", "вкус жизни",
    "ручная работа", "ручной работы", "ручная сборка",
    "легко мыть", "легко чистить", "легко использовать",
    "для души", "для настроения", "для комфорта",
    "ложка в комплекте", "в комплекте", "в подарок",
    "стильная подача", "эстетика быта", "эстетика дома",
    # 2026-06-04: новый bug-report «кружка с котом» — sub_ru-вода в реальных паках:
    "детализированный принт", "детализированный принт кота",
    "детализированный рисунок", "детализированный узор",
    "радует с утра", "радует каждый день", "радует глаз", "радует кота",
    "для уютных чаепитий", "уютные чаепития", "уютный вечер",
    "для уютных вечеров", "для тёплых вечеров", "для теплых вечеров",
    "яркая и гладкая", "яркая, гладкая",
    "крупно с любой стороны", "с любой стороны", "виден отовсюду",
    "в центре внимания", "рисунок в центре внимания",
    "в каждой детали", "улыбка в каждой детали", "забота в каждой детали",
    "кот на страже", "на страже утра", "на страже вечера", "на страже дома",
    "все как дома", "всё как дома", "как дома и в гостях",
    "керамика для любимых", "для любимых",
    "безопасно для напитков", "для напитков",
    # Brand-label обходы через цифру (которые _BLAND_STEMS не ловит, потому что
    # стем «бренд» там → но фраза «бренд №1» имеет цифру и проходит проверку
    # «короткая фраза без цифры»). Здесь ловим целиком как whole-list:
    "бренд №1", "бренд номер 1", "лидер категории", "лидер продаж",
    "эталон качества", "эталонное качество", "тот самый бренд",
    "оригинальный бренд", "лицензионный бренд",
    "приятная на ощупь", "приятная и гладкая",
}
# Одинокие пустые эпитеты-существительные как ВЕСЬ заголовок.
_BLAND_SOLO = {
    "премиум", "качество", "стиль", "комфорт", "уют", "элегантность",
    "надежность", "удобство", "стильно", "качественно", "роскошь",
    "оригинальность", "выразительность", "характер", "эстетика",
    "необычность", "яркость", "сочность", "лаконичность",
}
# Корни-эпитеты: заголовок целиком = эпитет(+нейтральное слово) → подозрение на вату.
# Расширено: «ярк», «выраз», «безупречн», «нестандартн», «характерн», «оригинальн»,
# «необычн», «изящн», «лаконичн», «утончен» — частые слова-наполнители.
_BLAND_STEMS = (
    "приятн", "качествен", "премиальн", "уникальн", "современн",
    "стильн", "надежн", "элегантн", "интуитивн", "комфортн",
    "ярк", "выраз", "безупречн", "нестандартн", "характерн",
    "оригинальн", "необычн", "изящн", "лаконичн", "утончен",
    "удобн", "практичн", "функциональн", "минималистичн",
    # Brand-label пустые ярлыки («<X> бренд», «бренд №1», «эталон качества»).
    # «марк»/«лидер» НЕ добавляю — слишком общие стемы (false-positives на
    # legit «маркер»/«лидер продаж» из user_facts). Только однозначно-bland.
    "бренд", "лицензи", "эталон",
)

# Структурные паттерны-шаблоны. Заголовок целиком = «шаблон-наполнитель», который
# можно подставить к ЛЮБОМУ товару. Каждое регекс-выражение — отдельный паттерн.
# Сюда попадают:
#   «Лицо X»       — «Лицо утра», «Лицо победы», «Лицо вечера»
#   «Утро X»       — «Утро с характером», «Утро победителя»
#   «X с характером» — «Подарок с характером», «Утро с характером»
#   «Подарок Y»     — «Подарок с душой», «Подарок для души»
#   «Для X»         — «Для души», «Для настроения», «Для комфорта»
#   «X в комплекте?» — «Ложка в комплекте?», «Подушка в комплекте?»
#   «Яркий акцент X»
#   «X дизайн» / «X формат» / «X текстура» когда X = эпитет
# Регексы матчим на _norm_copy(text), уже lowercase/ё→е.
_BLAND_TEMPLATE_RE = [
    (re.compile(r"^лицо\s+\S+$"),                  "шаблон «Лицо <что-то>»"),
    (re.compile(r"^утро\s+(с\s+)?\S+$"),           "шаблон «Утро <что-то>»"),
    (re.compile(r"^вечер\s+(с\s+)?\S+$"),          "шаблон «Вечер <что-то>»"),
    (re.compile(r"^день\s+(с\s+)?\S+$"),           "шаблон «День <что-то>»"),
    (re.compile(r"^\S+\s+с\s+характером$"),        "шаблон «<что-то> с характером»"),
    (re.compile(r"^\S+\s+с\s+душой$"),             "шаблон «<что-то> с душой»"),
    (re.compile(r"^подарок\s+(с\s+\S+|для\s+\S+|на\s+\S+)$"),
                                                    "шаблон «Подарок <обобщённое>»"),
    (re.compile(r"^для\s+(души|настроения|комфорта|радости|жизни|быта|любимых|своих|тепла|уюта)$"),
                                                    "пустое «Для X»"),
    (re.compile(r"^\S+\s+в\s+комплекте\??$"),      "шаблон «<что-то> в комплекте»"),
    (re.compile(r"^яркий\s+акцент(\s+\S+)?$"),     "шаблон «Яркий акцент …»"),
    (re.compile(r"^вкус\s+\S+$"),                  "шаблон «Вкус <что-то>»"),
    # «<эпитет> + дизайн|формат|текстура|стиль|вид|деталь» — типовой брак-наполнитель
    (re.compile(r"^\S+\s+(дизайн|формат|текстура|стиль|вид|деталь|акцент|штрих)$"),
                                                    "шаблон «<эпитет> + дизайн/формат/стиль»"),
    # 2026-06-04: «кружка с котом»-bug — sub_ru-наполнители реальных паков:
    (re.compile(r"^улыбка\s+в\s+\S+\s+\S+$"),       "шаблон «Улыбка в каждой <X>»"),
    (re.compile(r"^\S+\s+в\s+центре\s+внимания$"),  "шаблон «<X> в центре внимания»"),
    (re.compile(r"^\S+\s+на\s+страже(\s+\S+)?$"),   "шаблон «<X> на страже <Y>»"),
    (re.compile(r"^детализированн\S+\s+\S+(\s+\S+)?$"),
                                                    "шаблон «Детализированный <X>»"),
    (re.compile(r"^(все|всё)\s+как\s+дома$"),       "шаблон «Всё как дома» (любой товар)"),
    (re.compile(r"^крупно\s+с\s+\S+\s+\S+$"),       "шаблон «Крупно с любой стороны»"),
    (re.compile(r"^радует\s+\S+(\s+\S+)?$"),        "шаблон «Радует X»"),
    # «<существительное> + <предлог> + <существительное>» как ВЕСЬ headline (3 слова) —
    # читается как факт-описание, а не хук. «Кружка с котом», «Чай с лимоном» — БРАК.
    # (Кроме сильных триггеров: «без слов», «без шума», «за рулём» — разрешены
    # через _STRONG_HOOK_TRIGGERS ниже.)
    (re.compile(r"^[а-яё]+\s+(с|для|из|без|на|в|по|у)\s+[а-яё]+$"),
                                                    "шаблон «<вещь> <предлог> <вещь>» = описание, не хук"),
]
# Эти headline проходят как «сильный хук», даже если матчит шаблон «вещь+предлог+вещь».
_STRONG_HOOK_TRIGGERS = (
    "без слов", "без шума", "без боли", "без пыли", "без следов",
    "за минуту", "за рулем", "за рулём", "за деньги", "за копейки",
    "в минуту", "в подарок", "в кадре", "на максимум", "на скорость",
    "из коробки", "из печки", "по-домашнему", "по-русски",
)


def _norm_copy(s: str) -> str:
    return (s or "").strip().lower().replace("ё", "е").rstrip(".!?").strip()


def bland_reason(text: str) -> str:
    """Возвращает причину «воды» или '' если фраза ок. Детерминированно."""
    n = _norm_copy(text)
    if not n:
        return ""
    # Сильный хук-триггер — пропускаем, даже если матчит «вещь+предлог+вещь».
    # Без этой защиты «Открой за минуту», «Кофе без слов» отлетали как «описание».
    if any(t in n for t in _STRONG_HOOK_TRIGGERS):
        return ""
    if n in _BLAND_WHOLE:
        return f"пустой штамп «{text}» — подходит к любому товару"
    if n in _BLAND_SOLO:
        return f"одинокий эпитет «{text}» без смысла"
    # Структурные шаблоны-наполнители — «Лицо X», «Подарок с характером», «X в комплекте?»
    for rx, reason in _BLAND_TEMPLATE_RE:
        if rx.match(n):
            return f"заголовок «{text}» — {reason}, подставится к любому товару"
    # Если последний символ — «?», и фраза не вопрос-крючок («Кто здесь?»),
    # а вопрос-уточнение про комплектацию — брак.
    if text and text.strip().endswith("?") and len(n.split()) <= 4:
        # «Ложка в комплекте?» / «Что в коробке?» — это не крючок, а слабая интрига
        if any(w in n for w in ("комплект", "коробке", "набор", "идет")):
            return f"заголовок «{text}» — слабая вопрос-формулировка про комплектацию"
    words = n.split()
    # Короткий заголовок целиком из эпитета(+1 нейтрального слова) и без цифр/бренда.
    if len(words) <= 2 and any(w.startswith(_BLAND_STEMS) for w in words) \
            and not any(ch.isdigit() for ch in n):
        return f"заголовок «{text}» — только расплывчатый эпитет"
    # 2-3 слова из НЕ-бренд эпитетов + общее существительное (без цифр) — тоже брак.
    # Пример: «Яркий акцент цвета» (ярк-эпитет + 2 общих слова), «Утро победителя».
    if 2 <= len(words) <= 3 and not any(ch.isdigit() for ch in n):
        epithet_hits = sum(1 for w in words if w.startswith(_BLAND_STEMS))
        if epithet_hits >= 1 and not any(w in {"за", "от", "до", "под", "над", "после"} for w in words):
            # ещё одна страховка — нет имени собственного / бренда
            if not any(w[:1].isupper() for w in text.strip().split()[1:]):
                return f"заголовок «{text}» — эпитет + общие слова, не привязан к товару"
    return ""


_HEDGE_WORDS_RE = re.compile(
    r"\b(примерно|около|приблизительно|порядка|где-то|в\s+районе|почти|"
    r"около\s+того|примерно\s+как|порядка\s+того|~)\b",
    re.IGNORECASE,
)
# Число с единицей измерения — те же единицы что в extract_facts_from_brief,
# плюс «ml»/«ml», «g», «cm» и т.п. для англо-вкраплений в русском тексте.
_NUMBER_WITH_UNIT_RE = re.compile(
    r"(?<![\w\d.,])(\d+(?:[.,]\d+)?)\s?"
    r"(мл|ml|л|l|г\b|gr?\b|кг|kg|см|cm|мм|mm|м\b|°c|°|вт\b|w\b|квт|kw|"
    r"мач|mah|гб|gb|тб|tb|мп|mp|дюйм|″|\"|％|%|₽|руб|шт|years?|лет|"
    r"год(?:а)?|мес\b|месяц(?:ев|а)?|час(?:ов|а)?\b|hr|мин|min|сек|s\b|"
    r"ppi|hz|гц|об/мин|rpm)",
    re.IGNORECASE,
)


# Карта эквивалентных единиц: ml ↔ мл, kg ↔ кг и т.д. AI часто смешивает
# латиницу и кириллицу в описаниях — без нормализации «330 мл» в брифе и «330 ml»
# в описании трактовались бы как РАЗНЫЕ числа, и валидное число помечалось как
# выдуманное (ложное срабатывание).
_UNIT_ALIASES = {
    "ml": "мл", "l": "л",
    "g": "г", "gr": "г",
    "kg": "кг",
    "cm": "см", "mm": "мм", "m": "м",
    "w": "вт", "kw": "квт",
    "mah": "мач",
    "gb": "гб", "tb": "тб", "mp": "мп",
    "°": "°c",
    "\"": "дюйм", "″": "дюйм",
    "hr": "ч", "min": "мин", "s": "сек",
    "rpm": "об/мин", "hz": "гц",
    "year": "лет", "years": "лет",
    "час": "ч", "часов": "ч", "часа": "ч",
    "год": "лет", "года": "лет", "лет": "лет",
    "месяц": "мес", "месяцев": "мес", "месяца": "мес",
    "руб": "₽",
}


def _normalize_unit(unit: str) -> str:
    u = (unit or "").lower().rstrip(".")
    return _UNIT_ALIASES.get(u, u)


def _facts_numbers(user_facts: dict | None) -> set:
    """Нормализованный набор «<число><единица>» из user_facts. Используется как
    whitelist для unverified_number_reason: всё, что НЕ в этом наборе — выдумка.
    Нормализация: lower, без пробела между числом и единицей, запятая → точка,
    латинские единицы → кириллические (ml→мл, kg→кг)."""
    out: set = set()
    if not isinstance(user_facts, dict):
        return out
    for v in user_facts.values():
        if not isinstance(v, str):
            continue
        for m in _NUMBER_WITH_UNIT_RE.finditer(v):
            num = m.group(1).replace(",", ".")
            unit = _normalize_unit(m.group(2))
            out.add(f"{num}{unit}")
    return out


def unverified_number_reason(text: str, facts_norm: set) -> str:
    """Возвращает причину «выдуманное число» или '' если ок. Whitelist — facts_norm
    (получи через _facts_numbers(user_facts)). Также ловит hedge-слова рядом с числом."""
    if not text or not isinstance(text, str):
        return ""
    hedged = bool(_HEDGE_WORDS_RE.search(text))
    matches = list(_NUMBER_WITH_UNIT_RE.finditer(text))
    if not matches:
        # Hedge без числа («примерно как у конкурентов») — тоже маркер выдумки.
        if hedged:
            return f"hedge-слово в «{text}» без подтверждённого факта = маркер выдумки"
        return ""
    for m in matches:
        num = m.group(1).replace(",", ".")
        unit = _normalize_unit(m.group(2))
        key = f"{num}{unit}"
        if key not in facts_norm:
            tail = " (с hedge-словом)" if hedged else ""
            return f"число «{m.group(0)}»{tail} НЕТ в подтверждённых фактах — выдумано"
    # Все числа есть в facts, но рядом hedge → всё равно выкинуть hedge.
    if hedged:
        return f"hedge-слово в «{text}» рядом с подтверждённым числом = убрать hedge"
    return ""


def sanitize_description_numbers(description: str, user_facts: dict | None) -> tuple:
    """Чистит description от выдуманных чисел и hedge-слов.

    Правила:
      - Каждое <число> <единица>, которого НЕТ в user_facts → УДАЛЯЕМ
        ВЕСЬ буллет/предложение с ним. Не «переписываем» — это работа LLM, мы
        работаем детерминированно как safety net.
      - Hedge-слова («примерно», «около», «приблизительно», «порядка», «~»,
        «где-то», «в районе») → удаляются как слова (но фраза остаётся).
      - Буллеты определяются по началу строки «•», «—», «-», «*», «– », а также по
        предложениям (точка/!/?/новая строка).

    Возвращает (cleaned_text, removed_count). При removed_count > 0 имеет смысл
    залогировать в step() — селлер должен знать что у него вырезали.
    """
    if not description or not isinstance(description, str):
        return description or "", 0

    facts_norm = _facts_numbers(user_facts) if user_facts else set()
    removed = 0

    def _bullet_has_unverified_number(line: str) -> bool:
        for m in _NUMBER_WITH_UNIT_RE.finditer(line):
            num = m.group(1).replace(",", ".")
            unit = _normalize_unit(m.group(2))
            if f"{num}{unit}" not in facts_norm:
                return True
        return False

    out_lines = []
    for raw in description.splitlines():
        stripped = raw.lstrip(" \t•—–-*·").strip()
        is_bullet = bool(raw.lstrip() and raw.lstrip()[0] in "•—–-*·")
        # Hedge — снимаем слова, но строку оставляем.
        cleaned = _HEDGE_WORDS_RE.sub("", stripped).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        # Буллет с выдуманным числом — выкидываем целиком.
        if is_bullet and _bullet_has_unverified_number(cleaned):
            removed += 1
            continue
        # Не буллет — пересохраняем форматирование строки (с буллетом / без).
        if is_bullet:
            out_lines.append("• " + cleaned)
        else:
            # Вступительный абзац: если есть выдуманное число внутри предложения —
            # вырезаем целое предложение с этим числом (точка/!/?/новая_строка).
            sentences = re.split(r"(?<=[.!?])\s+", cleaned)
            kept = [s for s in sentences if not _bullet_has_unverified_number(s)]
            removed += len(sentences) - len(kept)
            if kept:
                out_lines.append(" ".join(kept))
    # Схлопываем тройные пустые строки до одной.
    text = "\n".join(out_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, removed


def card_copy_issues(card: dict, user_facts: dict | None = None) -> list:
    """Список текстовых проблем карточки (headline/sub/badges/footer). Пусто = ок.
    user_facts (опционально) — словарь подтверждённых фактов; если передан, проверяем
    что все числа в текстах буквально из этих фактов (иначе помечаем как выдумку).

    bland_reason применяется ко ВСЕМ текстовым полям (не только head/sub) — иначе
    «Надёжность зимних дорог» в footer и «Gleid бренд» в badge проходят, ломая
    карточку. 12.06 на незамерзайке именно так и было."""
    issues = []
    facts_norm = _facts_numbers(user_facts) if user_facts else set()
    for field in ("headline_ru", "sub_ru"):
        v = card.get(field) or ""
        r = bland_reason(v)
        if r:
            issues.append(f"{field}: {r}")
        nr = unverified_number_reason(v, facts_norm)
        if nr:
            issues.append(f"{field}: {nr}")
    # footer: bland И числовая дисциплина. Раньше проверялись только числа,
    # из-за чего штампы «Надёжность зимних дорог» / «Выбор автомобилистов» в
    # footer проходили как валидные.
    fr = card.get("footer_ru") or ""
    if fr:
        br = bland_reason(fr)
        if br:
            issues.append(f"footer_ru: {br}")
        nr = unverified_number_reason(fr, facts_norm)
        if nr:
            issues.append(f"footer_ru: {nr}")
    # badges: вопросы, числа И bland (раньше bland пропускался, пустой ярлык
    # «Gleid бренд» / «Премиум-формула» жил в бейдже как факт).
    for b in (card.get("badges_ru") or []):
        bt = (b or "").strip()
        if bt.endswith("?"):
            issues.append(f"badges_ru: бейдж-вопрос «{bt}» — неуверенность/непроверенный факт")
        nr = unverified_number_reason(bt, facts_norm)
        if nr:
            issues.append(f"badges_ru: {nr}")
        br = bland_reason(bt)
        if br:
            issues.append(f"badges_ru: «{bt}» — {br}")
    return issues


COPY_FIX_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


def build_copy_fix_user_prompt(bad_cards: list, ctx: dict) -> str:
    facts = ctx.get("facts") or {}
    facts_lines = "\n".join(f"  - {k}: {v}" for k, v in facts.items()) or "  — (точных фактов нет)"
    # Передаём type + reasons чтобы переписчик понимал, ЧТО конкретно сломано
    # в каждой карточке и в КАКОЙ роли карточка в паке (main = обложка-крючок).
    cards_payload = []
    for c in bad_cards:
        issues = (card_copy_issues(c, user_facts=facts) if isinstance(c, dict) else []) or [
            "усилить: сделать заголовок конкретным, грамотным и кликабельным про ИМЕННО этот товар; убрать пресность/несвязность"
        ]
        cards_payload.append({
            "id": c.get("id"),
            "type": c.get("type"),
            "issues": issues,
            "headline_ru": c.get("headline_ru"),
            "sub_ru": c.get("sub_ru"),
            "badges_ru": c.get("badges_ru") or [],
        })
    cards_json = json.dumps(cards_payload, ensure_ascii=False, indent=1)
    return f"""\
ТОВАР: {ctx.get('name','—')}
БРЕНД: {ctx.get('brand') or '—'}
КАТЕГОРИЯ: {ctx.get('category','—')}
КОНЦЕПТ ПАКА: {ctx.get('concept','—')}
ПОДТВЕРЖДЁННЫЕ ФАКТЫ (только это можно как конкретику):
{facts_lines}

СЛАБЫЕ КАРТОЧКИ (перепиши тексты, оставь id и порядок).
В каждой указано "issues" — РОВНО эти претензии нужно устранить:
{cards_json}[prompt omitted from public repo -- see docs/prompt-system.md]"""


def build_user_prompt(product, formats, card_plan, style_key, style_desc,
                      has_refs, ref_names, palette,
                      has_style_ref=False, user_facts=None,
                      art_direction_directive=""):
    cards_to_make = []
    for ctype, n in card_plan.items():
        for i in range(n):
            cards_to_make.append({"type": ctype, "index": i + 1})

    style_block = (
        ""  # style ref dominates → no directive (ref wins)
        if has_style_ref
        else (art_direction_directive or f"\nСТИЛЬ ВИЗУАЛА: {style_key} — {style_desc}")
    )

    return f"""\
Спроектируй пак карточек товара. Вводные:

ТОВАР: {product.get("name", "—")}
КАТЕГОРИЯ: {product.get("category", "—")}
БРЕНД: {product.get("brand", "—")}
USP: {product.get("usp", "—")}
ПРЕИМУЩЕСТВА:
{chr(10).join(f"- {f}" for f in product.get("features", [])) or "—"}
АУДИТОРИЯ: {product.get("audience", "—")}
ЦЕНА: {product.get("price_range", "—")}

{_refs_block(has_refs, ref_names, has_style_ref)}{style_block}
СТАРТОВАЯ ПАЛИТРА (если нет реф-стиля): accent={palette.get("accent","#FF4D6D")}, secondary={palette.get("secondary","#1B1B2F")}
{_facts_block(user_facts)}

НУЖНО СДЕЛАТЬ:
{json.dumps(cards_to_make, ensure_ascii=False)}

ФОРМАТЫ:
{json.dumps(formats, ensure_ascii=False)}

Правило: если format_key содержит "main" (wb_main / ozon_main / yamarket_main) — type=main. Для остальных распредели типы из плана пропорционально.

Верни {sum(card_plan.values()) * len(formats)} карточек (типы × форматы), у каждой уникальный id. ВСЕ карточки в ОДНОЙ визуальной системе (один шрифт, одна палитра, один стиль бейджей и света во всём паке), различаются только контентом.
"""


def build_brief_user_prompt(brief, formats, card_count, style_key, style_desc,
                            has_refs, ref_names, palette,
                            has_style_ref=False, user_facts=None,
                            category_confirmed="", art_direction_directive=""):
    style_block = (
        ""
        if has_style_ref
        else (art_direction_directive or f"\nСТИЛЬ ВИЗУАЛА: {style_key} — {style_desc}")
    )
    category_block = (
        f"\n🛡 КАТЕГОРИЯ ТОВАРА: {category_confirmed}\n"
        f"   ВСЕ headline_ru / sub_ru / badges_ru должны быть однозначно "
        f"про этот товар. Если фраза подходит и к другой категории — "
        f"переписывай конкретнее (лексика чужих категорий = брак).\n"
        if category_confirmed else ""
    )
    return f"""\
ОПИСАНИЕ ОТ ПОЛЬЗОВАТЕЛЯ:
\"\"\"
{brief}
\"\"\"
{category_block}
{_refs_block(has_refs, ref_names, has_style_ref)}{style_block}
СТАРТОВАЯ ПАЛИТРА (если нет реф-стиля): accent={palette.get("accent","#FF4D6D")}, secondary={palette.get("secondary","#1B1B2F")}
{_facts_block(user_facts)}

ФОРМАТЫ маркетплейса: {json.dumps(formats, ensure_ascii=False)}
ВСЕГО КАРТОЧЕК НА ФОРМАТ: {card_count} (первая всегда type=main; остальные распредели между feature/lifestyle/comparison разумно).

Извлеки структуру из описания и спроектируй ровно {card_count * len(formats)} карточек (типы × форматы). ВСЕ карточки в ОДНОЙ визуальной системе.
"""


def build_variations_user_prompt(brief_or_product, formats, n_variations,
                                 has_refs, ref_names, palette,
                                 has_style_ref=False):
    if isinstance(brief_or_product, dict):
        product_block = f"""\
ТОВАР: {brief_or_product.get("name", "—")}
КАТЕГОРИЯ: {brief_or_product.get("category", "—")}
USP: {brief_or_product.get("usp", "—")}
ПРЕИМУЩЕСТВА:
{chr(10).join(f"- {f}" for f in brief_or_product.get("features", [])) or "—"}"""
    else:
        product_block = f"ОПИСАНИЕ ТОВАРА:\n\"\"\"\n{brief_or_product}\n\"\"\""

    style_block = (
        "\n(Реф-стиль приложен — учитывай его палитру/типографику в КАЖДОМ варианте, но варианты всё равно должны различаться между собой композицией/настроением.)"
        if has_style_ref
        else ""
    )

    return f"""\
{product_block}

{_refs_block(has_refs, ref_names, has_style_ref)}{style_block}

ФОРМАТ: {formats[0] if formats else "wb_main"} (используй один — все варианты в одном формате).
СТАРТОВАЯ ПАЛИТРА: accent={palette.get("accent","#FF4D6D")}, secondary={palette.get("secondary","#1B1B2F")} (свободно меняй в каждом варианте).

КОЛИЧЕСТВО ВАРИАНТОВ: {n_variations}.

Спроектируй {n_variations} ОЧЕНЬ РАЗНЫХ художественных подач ОДНОЙ карточки.
"""


def build_description_user_prompt(product_or_design, competitor_analysis=None):
    """Build the user prompt for marketplace description.

    Args:
        product_or_design: dict with `inferred_product` key OR a flat product dict.
        competitor_analysis: optional dict from wb_analyzer.analyze_competitors()
                            with keys top_selling_angles, gaps_to_exploit,
                            copy_formulas. When provided, description is enriched
                            with competitive insights.
    """
    if "inferred_product" in product_or_design:
        p = product_or_design["inferred_product"]
    else:
        p = product_or_design

    competitor_block = ""
    if competitor_analysis:
        ca = competitor_analysis
        angles = ca.get("top_selling_angles") or []
        gaps = ca.get("gaps_to_exploit") or []
        formulas = ca.get("copy_formulas") or []
        if angles or gaps or formulas:
            lines = ["\n━━━ КОНКУРЕНТНЫЙ КОНТЕКСT (используй как ингредиенты) ━━━"]
            if angles:
                lines.append("🎯 Главные углы продаж в категории:")
                for a in angles[:5]:
                    lines.append(f"  • {a}")
            if gaps:
                lines.append("🏆 Чего НЕТ у конкурентов (твой шанс выделиться):")
                for g in gaps[:5]:
                    lines.append(f"  → {g}")
            if formulas:
                lines.append("✍️ Формулы заголовков из топ-карточек:")
                for f in formulas[:5]:
                    lines.append(f"  • {f}")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            competitor_block = "\n".join(lines)

    return f"""\
Напиши КОРОТКОЕ описание для карточки маркетплейса (600-1000 символов с пробелами, не больше).

ТОВАР: {p.get("name", "—")}
БРЕНД: {p.get("brand", "—")}
КАТЕГОРИЯ: {p.get("category", "—")}
USP: {p.get("usp", "—")}
ПРЕИМУЩЕСТВА:
{chr(10).join(f"- {f}" for f in p.get("features", [])) or "—"}
АУДИТОРИЯ: {p.get("audience", "—")}
ЦЕНА: {p.get("price_range", "—")}
{competitor_block}

Верни ТОЛЬКО текст описания. Лимит — 1000 символов.
"""


def build_pool_identify_user_prompt(brief, n_photos):
    return f"""\
ОПИСАНИЕ ОТ ПОЛЬЗОВАТЕЛЯ:
\"\"\"
{brief}
\"\"\"

К сообщению приложено {n_photos} фото — это разные товары в произвольном порядке (фото 1 = photo_index 0, фото 2 = photo_index 1, и т.д.).

Соотнеси каждое фото с товаром из описания (или извлеки из самого фото) и верни products[] согласно схеме.
"""


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY DETECTION (multimodal: Claude sees product photos → returns category)
# ════════════════════════════════════════════════════════════════════════════

CATEGORY_DETECT_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


def build_category_detect_user_prompt(brief: str, n_photos: int) -> str:
    brief_block = (
        f"\nДОПОЛНИТЕЛЬНОЕ ОПИСАНИЕ ОТ ПОЛЬЗОВАТЕЛЯ:\n\"\"\"\n{brief.strip()}\n\"\"\"\n"
        if (brief or "").strip()
        else ""
    )
    return (
        f"К сообщению приложено {n_photos}[prompt omitted from public repo -- see docs/prompt-system.md]"
        f"{brief_block}"
    )


# ════════════════════════════════════════════════════════════════════════════
# REFINE SEARCH QUERY — после того как юзер ответил на quick_facts и добавил
# своими словами описание, LLM собирает ОДИН естественный поисковый запрос
# для Wildberries (как живой человек его написал бы).
# ════════════════════════════════════════════════════════════════════════════

REFINE_QUERY_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


def build_refine_query_user_prompt(
    title: str,
    brand: str,
    brand_is_important: bool,
    answers_lines: list,
    user_free_text: str,
) -> str:
    parts = [f"название: {title or '—'}"]
    if brand:
        parts.append(f"бренд: «{brand}», бренд_важен: {str(bool(brand_is_important)).lower()}")
    else:
        parts.append("бренд: —")
    if answers_lines:
        parts.append("ответы:")
        for line in answers_lines:
            parts.append(f"  {line}")
    else:
        parts.append("ответы: —")
    parts.append(f"свободный текст: \"{user_free_text or ''}\"")
    parts.append("\nСобери query_specific и query_broad. Только JSON, без обёртки.")
    return "\n".join(parts)


# ════════════════════════════════════════════════════════════════════════════
# COMPETITOR ANALYSIS (text-only: deep structural analysis of competitor cards)
# ════════════════════════════════════════════════════════════════════════════

COMPETITOR_ANALYSIS_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


def build_competitor_analysis_user_prompt(products) -> str:
    lines = ["Вот карточки топ конкурентов с Wildberries. Проведи глубокий анализ.\n"]
    for i, p in enumerate(products, 1):
        price = f"{p.price:.0f} ₽" if p.price else "—"
        rating = f"★{p.rating}" if p.rating else "—"
        feedbacks = f"{p.feedbacks} отзывов" if p.feedbacks else "0 отзывов"
        chars_preview = ""
        if p.characteristics:
            top3 = p.characteristics[:3]
            chars_preview = "; ".join(f"{c['name']}: {c['value']}" for c in top3)
        desc_preview = (p.description or "")[:400].strip()
        if len(p.description or "") > 400:
            desc_preview += "…"
        lines.append(
            f"--- КОНКУРЕНТ {i} ---\n"
            f"Название: {p.name}\n"
            f"Бренд: {p.brand or '—'} | Поставщик: {p.supplier or '—'}\n"
            f"Цена: {price} | Рейтинг: {rating} | {feedbacks}\n"
            f"Фото: {p.photo_count} | Ссылка: {p.url}\n"
        )
        if chars_preview:
            lines.append(f"Характеристики: {chars_preview}\n")
        if desc_preview:
            lines.append(f"Описание: {desc_preview}\n")
        lines.append("")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# COMPETITOR-AWARE BRIEF PROMPT (enriched BRIEF_SYSTEM_PROMPT with competitor context)
# ════════════════════════════════════════════════════════════════════════════

COMPETITOR_BRIEF_SYSTEM_PROMPT = """\
Ты — арт-директор премиум-студии и копирайтер CTR. Ты получаешь:
1. Описание товара от пользователя + его фото + ответы на quick-facts.
2. Карту ниши: разбор того, как подают товар топ-конкуренты на WB.
""" + FACTUAL_HOOK_RULE + """[prompt omitted from public repo -- see docs/prompt-system.md]""" + WB_CORE + """[prompt omitted from public repo -- see docs/prompt-system.md]"""


# Категорийные «не пиши»-правила замка. Раньше ВСЕ четыре ветки шли в каждый
# дизайн-вызов (~300 токенов); теперь подставляется ТОЛЬКО ветка своей категории —
# чужие ветки для модели бесполезны (она и так знает, что наш товар не «та» категория).
_CATEGORY_LOCK_RULES = [
    (("посуд", "кружк", "чашк", "стакан", "тарелк", "керамик", "термокружк",
      "кастрюл", "сковород", "чайник", "кухн", "столов"),
     '\nНаша категория — посуда:\n'
     '  ❌ НЕ ПИШИ: "не линяет", "после стирок", "не садится", "не растягивается",\n'
     '              "плотность ткани", "100% хлопок", "натуральные волокна",\n'
     '              "не облезет", "стойкий рисунок после стирки", "усадка"\n'
     '  ✓ Можно: "не впитывает запах", "не царапается", "приятная на ощупь керамика"\n'),
    (("одежд", "текстиль", "футболк", "плать", "куртк", "пуховик", "бель",
      "плед", "штор", "костюм", "джинс", "носк", "обувь", "кроссовк"),
     '\nНаша категория — одежда/текстиль:\n'
     '  ❌ НЕ ПИШИ: "СВЧ-safe", "посудомойка", "тройной обжиг", "толстые стенки",\n'
     '              "термоустойчиво", "разогрев в микроволновке"\n'),
    (("электрон", "гаджет", "техник", "пульт", "наушник", "зарядк", "ноутбук",
      "телефон", "смартфон", "роутер", "камер", "монитор", "клавиатур"),
     '\nНаша категория — электроника:\n'
     '  ❌ НЕ ПИШИ: "натуральный материал", "ручная роспись", "100% хлопок"\n'),
    (("косметик", "крем", "сыворотк", "уход", "макияж", "парфюм", "шампун",
      "бальзам", "лосьон", "маск"),
     '\nНаша категория — косметика:\n'
     '  ❌ НЕ ПИШИ: "СВЧ", "стирка", "разогрев", "ёмкость в мл" (если не указана юзером)\n'),
    # Категорийный замок — ТОЛЬКО про физические/категорийные ОГРАНИЧЕНИЯ
    # (что физически бессмысленно для категории: «СВЧ» для одежды, «плотность ткани»
    # для посуды). Тон/стиль/формула headline — НЕ здесь, а в TONE MATRIX внутри
    # WB_CORE: универсальная классификация по 4 осям, работает для любой категории
    # без hardcoded списков (автохимия, парфюмерия, гик-мерч, электроника — модель
    # сама определяет product class из брифа и применяет нужную формулу).
]


def _category_lock_rules(category: str, brief: str = "") -> str:
    """Вернёт «не пиши»-правила ТОЛЬКО для категории товара (или пустую строку)."""
    hay = f"{category or ''} {brief or ''}".lower()
    for keywords, rules in _CATEGORY_LOCK_RULES:
        if any(k in hay for k in keywords):
            return rules
    return ""


def _art_directions_catalog_block() -> str:
    """Компактный каталог 10 ART_DIRECTIONS для подачи в дизайн-промпт. Claude
    видит ключи + 1-строчные signature + 1 hint про подходящую TONE-классификацию,
    и выбирает один. Это заменяет keyword-stem matching в formats.py (хардкод
    стемов на категорию). Цена: ~250-300 токенов в каждом дизайн-вызове —
    эквивалент 1 средней фото-карточки в visual_analysis.

    Hint про подходящий TONE класс — НЕ навязывает категорию, а помогает Claude
    выбрать. Финальное решение за моделью на основе её TONE MATRIX классификации.

    Lazy-импортируем formats.py чтобы prompts.py оставался самодостаточным."""
    try:
        from formats import ART_DIRECTIONS  # noqa: PLC0415
    except Exception:
        return ""
    tone_hints = {
        "premium_dark":         "luxury / премиум / аспирационный / тёмный-аниме / гик-мерч с мрачным вайбом / коллекционный-боевой",
        "clean_light_studio":   "функциональный / повседневный / расходник",
        "bold_color_block":     "функциональный / инструмент / молодёжный / яркий-мем",
        "warm_lifestyle":       "еда / лайфстайл / уют / культура дома (НЕ для тёмного/аниме/гротеск/мем вайба)",
        "tech_minimal_mono":    "функциональный / технический / расходник / инструмент / автохимия",
        "editorial_magazine":   "одежда / luxury / редакционный / авторская-эстетика / craft",
        "pastel_soft":          "детское / косметика / нежное / гифт / мультяшный-мем",
        "high_contrast_poster": "акции / громкое / спорт / аниме-мем-кричащий / гротеск-комикс",
        "nature_organic":       "эко / органик / натуральная косметика / food (НЕ для IP/аниме/гик-мерч)",
        "energetic_gradient":   "технологичный / гаджеты / спорт / энергия",
    }
    lines = ["━━━ 🎨 КАТАЛОГ ART_DIRECTIONS (выбери ключ под TONE MATRIX) ━━━"]
    for key, ad in ART_DIRECTIONS.items():
        # Берём короткую первую фразу signature до первой точки (~15-25 слов).
        sig = (ad.get("signature") or "").split(".", 1)[0].strip()
        hint = tone_hints.get(key, "")
        lines.append(f"  • {key}: {sig}. ПОДХОДИТ: {hint}.")
    lines.append(
        "[prompt omitted from public repo -- see docs/prompt-system.md]"
    )
    return "\n".join(lines) + "\n"


def _marketplace_directive(formats: list) -> str:
    """Возвращает platform-specific блок compliance/copywriting в зависимости от
    format_key пака. Wildberries (wb_*) — дефолт, инфографика разрешена.
    Ozon (ozon_*) — главное фото без текстовых блоков, инфографика только на
    допах. Яндекс.Маркет / IG — комбинированный.

    Это нужно, потому что copy-правила и compliance этих маркетплейсов реально
    различаются: ozon_main с инфографикой на главной = риск замечания модерации,
    wb_main без неё = пустая трата места в выдаче. Промпт должен знать."""
    keys = [str(f.get("format_key") or f) if isinstance(f, dict) else str(f) for f in (formats or [])]
    has_wb = any(k.startswith("wb_") for k in keys)
    has_ozon = any(k.startswith("ozon_") for k in keys)
    has_ym = any(k.startswith("yamarket_") for k in keys)
    if not (has_wb or has_ozon or has_ym):
        return ""
    blocks = ["━━━ 🛒 ПЛАТФОРМЫ В ПАКЕ ━━━"]
    if has_wb:
        blocks.append(
            "📦 Wildberries (wb_*): инфографика-главная — норма (плитка 30+ конкурентов, "
            "юзеру нужен якорь за 1-2 сек). main = TITLE-NAME + 3-5 callouts + товар крупно. "
            "Допы (wb_extra/feature/lifestyle) — раскрытие. Compliance: оценочные суперлативы, "
            "цены, скидки, контакты, QR — БАН модерации. Закон 168-ФЗ: иностр. оценочные слова "
            "(Sale/New/Best/Premium) — БАН, кроме зарегистрир. товарных знаков."
        )
    if has_ozon:
        blocks.append(
            "[prompt omitted from public repo -- see docs/prompt-system.md]"
        )
    if has_ym:
        blocks.append(
            "🅰 Яндекс.Маркет (yamarket_*): главная — товар на белом, минимум текста. "
            "Инфографика — допы. Compliance — общий маркетплейсный."
        )
    return "\n".join(blocks) + "\n"


def build_competitor_brief_user_prompt(
    brief: str,
    formats: list,
    card_count: int,
    style_key: str,
    style_desc: str,
    has_refs: bool,
    ref_names: list,
    palette: dict,
    competitor_analysis: dict,
    category_confirmed: str = "",
    has_style_ref: bool = False,
    art_direction_directive: str = "",
    text_only: bool = False,
) -> str:
    style_block = (
        ""
        if has_style_ref
        else (art_direction_directive or f"\nСТИЛЬ ВИЗУАЛА: {style_key} — {style_desc}")
    )

    category_lock_rules = _category_lock_rules(category_confirmed, brief)
    marketplace_block = _marketplace_directive(formats)
    # COUNT vs INSIGHT GUARD: в сингл-шоте опирается на поле _brainstorm_angles из
    # его схемы; у split Stage 1 (text_only, TEXT_DESIGN) такого поля нет — там
    # тот же принцип формулируется «мысленно», без ссылки на несуществующее поле.
    if text_only:
        insight_guard_block = f"""\
⚠️ COUNT vs INSIGHT GUARD (адаптивно): прежде чем заполнить cards[], мысленно
набросай минимум {card_count}[prompt omitted from public repo -- see docs/prompt-system.md]"""
    else:
        insight_guard_block = f"""\
⚠️ COUNT vs INSIGHT GUARD (адаптивно): прежде чем заполнить cards[], проверь
_brainstorm_angles — есть ли там минимум {card_count}[prompt omitted from public repo -- see docs/prompt-system.md]{card_count}+ уникальных
insights, потом заполняй cards[]."""
    # text_only=True (SPLIT Stage 1 — копирайтер): каталог ART_DIRECTIONS не нужен.
    # Он диктует выбор _art_direction_key и визуальные блоки (scene/extracted_style),
    # которые TEXT_DESIGN_SYSTEM_PROMPT прямо ЗАПРЕЩАЕТ копирайтеру выдавать, а
    # формулы заголовков у Stage 1 свои (HOOK-ФОРМУЛЫ в его системном промпте).
    # Это ~900 токенов входа на каждый дизайн-вызов, провисавшие впустую. В
    # single-shot (text_only=False) каталог остаётся — там он реально используется.
    art_catalog_block = "" if text_only else _art_directions_catalog_block()
    ca = competitor_analysis or {}

    recipe = ca.get("card_recipe") or []
    recipe_txt = ""
    if recipe:
        _type_ru = {"main": "Главная", "feature": "Инфографика", "lifestyle": "Лайфстайл",
                    "comparison": "Сравнение", "benefits": "Выгоды"}
        parts = []
        for c in recipe:
            # Имя поля — back-compat. Семантика — снимок ИХ карточки: их кадр, их
            # заголовок, чем их подача работает и чем не очень. why_works / weak_spot —
            # новые поля от обновлённого COMPETITOR_ANALYSIS, why — старое (back-compat).
            ctype = c.get("type", "?")
            type_label = c.get("type_ru") or _type_ru.get(ctype, str(ctype).upper())
            headline = c.get("headline_ru") or c.get("headline") or "—"
            sub = c.get("sub_ru") or c.get("sub") or "—"
            badges_str = ", ".join(c.get("badges_ru") or c.get("badges") or ["—"])
            why_works = c.get("why_works") or c.get("why") or "—"
            weak = c.get("weak_spot") or "—"
            parts.append(
                f"  [{c.get('n','?')}] {type_label}: "
                f"У них в кадре — {c.get('shot','—')} | "
                f"Их заголовок: «{headline}» / «{sub}» | "
                f"Бейджи: {badges_str} | "
                f"Что работает: {why_works} | "
                f"Слабое место: {weak}"
            )
        recipe_txt = "\n".join(parts)

    # marketing_dna — новый источник; visual_dna — старое имя для back-compat.
    # Объединяем, новые поля приоритетней.
    mdna = ca.get("marketing_dna") or {}
    vdna_old = ca.get("visual_dna") or {}
    vdna = {**vdna_old, **mdna}
    angles_txt = "\n".join(f"  • {a}" for a in (ca.get("top_selling_angles") or []))
    gaps_txt = "\n".join(f"  → {g}" for g in (ca.get("gaps_to_exploit") or []))
    formulas_txt = "\n".join(f"  • {f}" for f in (ca.get("copy_formulas") or []))
    wins_txt = "\n".join(f"  ✓ {w}" for w in (ca.get("winning_patterns") or []))
    losses_txt = "\n".join(f"  ✗ {l}" for l in (ca.get("losing_patterns") or []))
    market_summary = (ca.get("market_summary") or "").strip()
    rec_plan = ca.get("recommended_card_plan") or {}

    style_priority_note = (
        "\n⚠️ ПРИОРИТЕТ СТИЛЯ: реф-карточка стиля приложена → её фон/шрифт/цвета/свет = стиль пака. "
        "visual_dna конкурентов ниже — только для scene_override (что и как снимать). "
        "pack_scene_block ДОЛЖЕН отражать реф, а НЕ тёмно-синий фон конкурентов.\n"
        if has_style_ref else ""
    )

    return f"""\
ОПИСАНИЕ ТОВАРА ОТ ПОЛЬЗОВАТЕЛЯ:
\"\"\"
{brief}
\"\"\"

{_refs_block(has_refs, ref_names, has_style_ref)}{style_block}{style_priority_note}
СТАРТОВАЯ ПАЛИТРА: accent={palette.get("accent","#FF4D6D")}, secondary={palette.get("secondary","#1B1B2F")}

{marketplace_block}{art_catalog_block}━━━ 🛡 КАТЕГОРИЙНЫЙ ЗАМОК (читай ПЕРЕД анализом конкурентов) ━━━
КАТЕГОРИЯ НАШЕГО ТОВАРА: {category_confirmed or "определи из описания"}

Анализ конкурентов ниже мог зацепить ТОВАРЫ-ПРИМЕСИ из соседних категорий —
их copy_formulas / card_recipe / badges могут содержать лексику ДРУГИХ
категорий. Любую фразу, ЯВНО относящуюся к другой категории товара,
КАТЕГОРИЧЕСКИ ИГНОРИРУЙ.
{category_lock_rules}[prompt omitted from public repo -- see docs/prompt-system.md]{market_summary or "—"}

📋 СНИМКИ КАРТОЧЕК КОНКУРЕНТОВ (НЕ задание клиенту — справка о том, как подаёт топ):
{recipe_txt or "—"}

🎨 MARKETING DNA НИШИ:
  Фон: {vdna.get("background","—")}
  Ракурс главной: {vdna.get("hero_angle","—")}
  Цветовое настроение: {vdna.get("color_mood","—")}
  Типографика: {vdna.get("typography_feel","—")}
  Плотность текста на главной: {vdna.get("text_density","—")}
  Ценовое позиционирование: {vdna.get("price_positioning","—")}

✅ ЧТО У ТОПА РЕАЛЬНО РАБОТАЕТ (можно использовать как ПРИНЦИПЫ, не как готовые фразы):
{wins_txt or "—"}

❌ ЧТО У ТОПА СЛАБО / НЕ РАБОТАЕТ (анти-паттерны — этого избегай):
{losses_txt or "—"}

🎯 ГЛАВНЫЕ УГЛЫ ПРОДАЖ В НИШЕ:
{angles_txt or "—"}

🏆 ЧЕГО У ТОПА НЕТ — ТВОЙ ШАНС ОТЛИЧИТЬСЯ:
{gaps_txt or "—"}

✍️ ФОРМУЛЫ ЗАГОЛОВКОВ (только СТРУКТУРА; цифры/факты — только из user_facts клиента):
{formulas_txt or "—"}[prompt omitted from public repo -- see docs/prompt-system.md]{rec_plan.get("main",1)}, feature={rec_plan.get("feature",3)}, \
lifestyle={rec_plan.get("lifestyle",1)}, comparison={rec_plan.get("comparison",0)}
  → Итого минимум: {sum(rec_plan.get(t,0) for t in ["main","feature","lifestyle","comparison"])} карточек

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ФОРМАТЫ маркетплейса: {json.dumps(formats, ensure_ascii=False)}
ВСЕГО КАРТОЧЕК НА ФОРМАТ: {card_count}[prompt omitted from public repo -- see docs/prompt-system.md]{card_count * len(formats)}[prompt omitted from public repo -- see docs/prompt-system.md]{insight_guard_block}[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# SPLIT: build_visual_design_user_prompt
# Stage 2 видит ВСЕ тексты из Stage 1 как ЗАКОН + visual-контекст товара.
# ════════════════════════════════════════════════════════════════════════════
def build_visual_design_user_prompt(
    cards_from_stage1: list,        # Stage 1 cards (id/type/headline_ru/sub_ru/badges_ru/hook_formula/role_in_story/competitor_insight)
    pack_concept: str,               # Stage 1 pack_concept
    brief: str,
    category_confirmed: str,
    competitor_analysis: dict,
    has_refs: bool,
    ref_names: list,
    has_style_ref: bool,
    palette: dict,
    art_direction_directive: str,
    user_facts: dict | None,
) -> str:
    """User prompt для VISUAL_DESIGN_SYSTEM_PROMPT (Stage 2 split-режима).

    Stage 1 уже зафиксировал тексты. Stage 2 их НЕ переписывает — он строит
    pack_scene_block, extracted_style и scene_override под хук каждой карточки.
    """
    cards_block = []
    for c in cards_from_stage1:
        if not isinstance(c, dict):
            continue
        cid = c.get("id") or "?"
        ctype = c.get("type") or "?"
        head = c.get("headline_ru") or ""
        sub = c.get("sub_ru") or ""
        badges = ", ".join(c.get("badges_ru") or [])
        hf = c.get("hook_formula") or "?"
        role = c.get("role_in_story") or ""
        comp = c.get("competitor_insight") or ""
        cards_block.append(
            f"  [{cid}] type={ctype} hook_formula={hf}\n"
            f"    headline_ru: «{head}»\n"
            f"    sub_ru:      «{sub}»\n"
            f"    badges_ru:   {badges}\n"
            f"    role:        {role}\n"
            f"    insight:     {comp}"
        )
    cards_block_text = "\n\n".join(cards_block) or "(нет карточек)"

    ca = competitor_analysis or {}
    mdna = ca.get("marketing_dna") or {}
    vdna_old = ca.get("visual_dna") or {}
    vdna = {**vdna_old, **mdna}
    vdna_block = "\n".join(f"  • {k}: {v}" for k, v in vdna.items() if v)

    style_priority_note = (
        "[prompt omitted from public repo -- see docs/prompt-system.md]"
        if has_style_ref else ""
    )

    facts_txt = ""
    if isinstance(user_facts, dict) and user_facts:
        items = "\n".join(f"  • {k}: {v}" for k, v in user_facts.items() if v)
        facts_txt = f"\n\nQUICK_FACTS (что подтвердил юзер):\n{items}"

    return f"""[prompt omitted from public repo -- see docs/prompt-system.md]{pack_concept}

═══════════════════════════════════════════════════════════════════════════════
ТЕКСТЫ КАРТОЧЕК ОТ STAGE 1 (ЗАФИКСИРОВАНЫ — НЕ ПЕРЕПИСЫВАТЬ):
═══════════════════════════════════════════════════════════════════════════════
{cards_block_text}

═══════════════════════════════════════════════════════════════════════════════
ТОВАР:
═══════════════════════════════════════════════════════════════════════════════
Категория: {category_confirmed or "не уточнена"}
Бриф юзера:
\"\"\"
{brief}
\"\"\"
{facts_txt}

{_refs_block(has_refs, ref_names, has_style_ref)}

СТАРТОВАЯ ПАЛИТРА: accent={palette.get("accent","#FF4D6D")}, secondary={palette.get("secondary","#1B1B2F")}
{art_direction_directive}{style_priority_note}

═══════════════════════════════════════════════════════════════════════════════
VISUAL_DNA КОНКУРЕНТОВ (чтобы ОТЛИЧАТЬСЯ, не копировать):
═══════════════════════════════════════════════════════════════════════════════
{vdna_block or "(не было анализа конкурентов)"}[prompt omitted from public repo -- see docs/prompt-system.md]{", ".join(ref_names) or "—"}[prompt omitted from public repo -- see docs/prompt-system.md]"""


# ════════════════════════════════════════════════════════════════════════════
# BUYER QUESTIONS — лёгкий «второй раунд» quick_facts, который задаётся ЮЗЕРУ
# ПОСЛЕ парсинга конкурентов (а не до). На этом этапе модель уже видит и нишу,
# и анализ топа — поэтому может задать категорийно-специфичные вопросы, которых
# при первичной детекции по фото неоткуда было взять. Формат ответа модели
# совпадает с `quick_facts` от /api/detect-product (key/label/options/multi_select/
# allow_custom) — фронт мёрджит результат в state._detectResult.quick_facts и
# рендерит теми же ud-section чипсами, что и базовые. Отдельного шага не плодим.
# ════════════════════════════════════════════════════════════════════════════

BUYER_QUESTIONS_SYSTEM_PROMPT = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


def build_buyer_questions_user_prompt(
    category: str,
    brief: str,
    competitor_analysis: dict | None,
    already_answered: dict | None,
    existing_questions: list | None = None,
) -> str:
    """Собирает user-промпт для /api/buyer-questions. Передаём только релевантные
    срезы competitor_analysis (market_summary / top_selling_angles / gaps_to_exploit /
    winning_patterns) — полный analysis тут не нужен, он раздувает токены."""
    ca = competitor_analysis or {}
    aa = already_answered or {}

    angles = ca.get("top_selling_angles") or []
    gaps = ca.get("gaps_to_exploit") or []
    wins = ca.get("winning_patterns") or []
    market = (ca.get("market_summary") or "").strip()

    angles_txt = "\n".join(f"  • {a}" for a in angles[:8]) or "—"
    gaps_txt = "\n".join(f"  → {g}" for g in gaps[:8]) or "—"
    wins_txt = "\n".join(f"  ✓ {w}" for w in wins[:8]) or "—"

    aa_lines = []
    for k, v in aa.items():
        if v is None or v == "" or str(v).lower().startswith("не указ"):
            continue
        aa_lines.append(f"  • {k}: {v}")
    aa_txt = "\n".join(aa_lines) or "  (юзер пока ничего не ответил)"

    already_asked_txt = ""
    if existing_questions:
        labels = [str(q.get("label") or q.get("key") or "").strip()
                  for q in existing_questions if isinstance(q, dict)]
        labels = [l for l in labels if l]
        if labels:
            already_asked_txt = (
                "\n\nALREADY_ASKED — вопросы, которые уже ПОКАЗЫВАЮТСЯ юзеру "
                "(не дублируй их — задавай только НОВЫЕ темы):\n"
                + "\n".join(f"  • {l}" for l in labels)
            )

    return f"""\
КАТЕГОРИЯ: {category or "—"}

БРИФ ОТ ЮЗЕРА:
\"\"\"
{brief or "—"}
\"\"\"

ALREADY_ANSWERED — факты, которые юзер УЖЕ дал (не задавай по ним вопрос снова):
{aa_txt}{already_asked_txt}

━━━ АНАЛИЗ КОНКУРЕНТОВ ИЗ ЭТОЙ НИШИ (для понимания, что решает покупку) ━━━

📍 КОРОТКО О НИШЕ: {market or "—"}

🎯 ГЛАВНЫЕ УГЛЫ ПРОДАЖ В НИШЕ:
{angles_txt}

🏆 ЧЕГО У ТОПА НЕТ — МЕСТА ДЛЯ ВКЛИНИВАНИЯ:
{gaps_txt}

✅ ЧТО У ТОПА РЕАЛЬНО РАБОТАЕТ:
{wins_txt}[prompt omitted from public repo -- see docs/prompt-system.md]"""


def build_video_motion_user_prompt(card_meta, video_effect="auto"):
    """Card_meta: dict with headline_ru / sub_ru / badges_ru / image_prompt / type.
    video_effect: one of VIDEO_EFFECT_SPECS keys ('auto', 'rotate', 'zoom', 'glow', 'parallax', 'particles').
    """
    spec = VIDEO_EFFECT_SPECS.get(video_effect, VIDEO_EFFECT_SPECS["auto"])
    return f"""\
КАРТОЧКА ДЛЯ АНИМАЦИИ:
- Тип: {card_meta.get("type", "main")}
- Заголовок (НЕ ДВИЖЕТСЯ): «{card_meta.get("headline_ru", "—")}»
- Подзаголовок (НЕ ДВИЖЕТСЯ): «{card_meta.get("sub_ru", "—")}»
- Бейджи (НЕ ДВИЖУТСЯ): {card_meta.get("badges_ru", [])}
- Сцена (для контекста): {(card_meta.get("image_prompt") or "")[:300]}…

🎯 ВЫБРАННЫЙ ЭФФЕКТ: {video_effect.upper()}
ОПИСАНИЕ ЭФФЕКТА (это и только это движение должно быть в видео):
{spec}

Дай motion_prompt по этому эффекту. Текст и бейджи остаются ровно как на input image — без новых букв, без движений текста.
"""


# ════════════════════════════════════════════════════════════════════════════
# JSON extractor
# ════════════════════════════════════════════════════════════════════════════

def extract_json(text):
    import logging as _log
    _ej_log = _log.getLogger("wbnator.prompts")

    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # Поддерживаем И объект И массив. Раньше тут было ТОЛЬКО text.find("{") —
    # из-за чего ответы вида `[{"id":"card_1","headline_ru":"..."}]` (массив,
    # как просит COPY_FIX_SYSTEM_PROMPT) уезжали в первый `{` внутри массива.
    # _find_json_end доходил до закрытия первого объекта и возвращал ОДНУ
    # карточку как dict → _grade_and_fix_copy искал у неё ключ "cards" →
    # пусто → "переписчик не вернул валидный JSON". Теперь смотрим что встретится
    # первым: `{` или `[`, и парсим именно эту структуру.
    obj_pos = text.find("{")
    arr_pos = text.find("[")
    if obj_pos == -1 and arr_pos == -1:
        raise ValueError(f"Не нашёл JSON в ответе модели:\n{text[:500]}")
    if obj_pos == -1:
        start = arr_pos
    elif arr_pos == -1:
        start = obj_pos
    else:
        start = min(obj_pos, arr_pos)

    # Pre-repair: fix false escaped quotes BEFORE bracket-counting.
    # Claude outputs "IPS 15.6\"] — the \" swallows the ] into the string,
    # making _find_json_end think the JSON is truncated.
    text = re.sub(r'\\"(\s*[\]\},])', r'"\1', text)

    # Proper bracket-counting end-finder (not rfind which finds wrong } on truncated JSON)
    def _find_json_end(s, pos):
        depth = 0
        in_str = False
        i = pos
        while i < len(s):
            c = s[i]
            if c == '"':
                nbs, j = 0, i - 1
                while j >= 0 and s[j] == '\\':
                    nbs += 1
                    j -= 1
                if nbs % 2 == 0:
                    in_str = not in_str
            elif not in_str:
                if c in ('{', '['):
                    depth += 1
                elif c in ('}', ']'):
                    depth -= 1
                    if depth == 0:
                        return i
            i += 1
        return -1  # truncated

    end = _find_json_end(text, start)
    if end == -1:
        raise ValueError(
            f"JSON обрезан — ответ модели не завершён (вероятно, превышен лимит токенов). "
            f"Начало: {text[start:start+200]}…"
        )
    raw = text[start : end + 1]
    # Strip non-JSON control chars; keep tab \x09, newline \x0a, CR \x0d
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)

    def _try(s, label=""):
        try:
            return json.loads(s)
        except json.JSONDecodeError as e:
            _ej_log.debug(
                "extract_json [%s] pos=%d: %s | context: ...%s...",
                label, e.pos, e.msg,
                s[max(0, e.pos - 60): e.pos + 60].replace("\n", "↵"),
            )
            return None

    result = _try(raw, "raw")
    if result is not None:
        return result

    # Repair A — escape literal newlines/tabs/CRs inside JSON strings.
    # Must run BEFORE structural repairs so multiline strings don't confuse them.
    def _fix_strings(s):
        out = []
        in_str = False
        i = 0
        while i < len(s):
            c = s[i]
            if c == '"':
                # Count preceding backslashes: odd → this quote is escaped
                nbs, j = 0, i - 1
                while j >= 0 and s[j] == '\\':
                    nbs += 1
                    j -= 1
                out.append(c)
                if nbs % 2 == 0:
                    in_str = not in_str
            elif in_str and c == "\n":
                out.append("\\n")
            elif in_str and c == "\r":
                out.append("\\r")
            elif in_str and c == "\t":
                out.append("\\t")
            else:
                out.append(c)
            i += 1
        return "".join(out)

    rA = _fix_strings(raw)
    result = _try(rA, "fix_strings")
    if result is not None:
        return result

    # Repair B — remove trailing commas before } or ]
    rB = re.sub(r",\s*([\]}])", r"\1", rA)
    result = _try(rB, "trailing_commas")
    if result is not None:
        return result

    # Repair C — add missing commas between adjacent values/keys
    rC = re.sub(r'(["\d\]}\w])\s*\n(\s*")', r'\1,\n\2', rB)
    result = _try(rC, "missing_commas")
    if result is not None:
        return result

    # Repair D — fix missing ": " between key and value (proxy corruption).
    # Pattern: "keyValue" where key is latin and value starts with Cyrillic/digit/quote
    # e.g. "categoryНоутбуки" → "category": "Ноутбуки"
    rD = re.sub(r'"([a-z_]+)([А-Яа-яA-Z0-9\[])', r'"\1": "\2', rC)
    # Also fix cases where the closing quote of key + colon are missing: "key""value"
    rD = re.sub(r'"([a-z_]+)""', r'"\1": "', rD)
    result = _try(rD, "missing_colon")
    if result is not None:
        return result

    # Repair E — aggressive: try removing all non-ASCII from keys only
    rE = rD
    # Fix unbalanced quotes from Repair D by re-checking bracket balance
    try:
        json.loads(rE)
    except json.JSONDecodeError as e:
        # Last resort: truncate at the error position and close all open braces
        pass

    raise ValueError(
        f"Не удалось разобрать JSON после попыток исправления:\n{raw[:600]}"
    )
