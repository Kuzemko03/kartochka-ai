"""Format presets for marketplace product cards.

Each preset defines:
  size:    (width, height) in pixels — final image dimensions
  aspect:  one of "3:4", "1:1", "9:16", "16:9", "4:3" — passed to VeoNonStop
  label:   user-facing name shown in the UI
  group:   logical group for the picker (WB / Ozon / Соцсети / Прочее)
"""

FORMATS = {
    "wb_main": {
        "label": "WB — основная (900×1200)",
        "size": (900, 1200),
        "aspect": "3:4",
        "group": "Wildberries",
    },
    "wb_extra": {
        "label": "WB — дополнительная (900×1200)",
        "size": (900, 1200),
        "aspect": "3:4",
        "group": "Wildberries",
    },
    "ozon_main": {
        "label": "Ozon — основная (700×900)",
        "size": (700, 900),
        "aspect": "3:4",
        "group": "Ozon",
    },
    "ozon_square": {
        "label": "Ozon — квадрат (1000×1000)",
        "size": (1000, 1000),
        "aspect": "1:1",
        "group": "Ozon",
    },
    "yamarket_main": {
        "label": "Я.Маркет — основная (1200×1200)",
        "size": (1200, 1200),
        "aspect": "1:1",
        "group": "Я.Маркет",
    },
    "ig_post": {
        "label": "Instagram пост (1080×1080)",
        "size": (1080, 1080),
        "aspect": "1:1",
        "group": "Соцсети",
    },
    "ig_story": {
        "label": "Сторис / Reels (1080×1920)",
        "size": (1080, 1920),
        "aspect": "9:16",
        "group": "Соцсети",
    },
    "tg_preview": {
        "label": "Telegram превью (1280×720)",
        "size": (1280, 720),
        "aspect": "16:9",
        "group": "Соцсети",
    },
}


CARD_TYPES = {
    "main": {
        "label": "Главная — товар + сильный заголовок",
        "default_count": 1,
    },
    "feature": {
        "label": "Инфографика — одно преимущество на карточку",
        "default_count": 3,
    },
    "lifestyle": {
        "label": "Лайфстайл — товар в окружении/использовании",
        "default_count": 1,
    },
    "comparison": {
        "label": "Состав / сравнение «было-стало»",
        "default_count": 0,
    },
}


STYLES = {
    "minimal":    "Минимализм (чистый студийный фон, мягкие тени)",
    "premium":    "Премиум (тёмный градиент, золотые акценты)",
    "lifestyle":  "Лайфстайл (естественный свет, мягкое окружение)",
    "studio":     "Студийная съёмка (белый фон, цветной геометрический акцент)",
    "nature":     "Природа (деревянные/каменные текстуры, листья)",
    "bright":     "Яркий поп (контрастные цвета, плоские формы)",
    "tech":       "Технологичный (градиент, неоновые блики, графика)",
}


# ════════════════════════════════════════════════════════════════════════════
# ART DIRECTIONS — пул художественных направлений. Когда юзер НЕ выбрал стиль
# вручную и НЕ приложил реф-карточку, генератор выбирает СВОЁ направление на
# каждый прогон (см. generator._pick_art_direction), чтобы разные генерации
# выглядели по-разному. Внутри одного пака направление одно (консистентность).
# Каждое: accent/secondary (палитра) + signature (англ. описание для image-модели).
# ════════════════════════════════════════════════════════════════════════════
ART_DIRECTIONS = {
    "premium_dark": {
        "label": "Премиум-тёмный",
        "accent": "#E8B65A", "secondary": "#10131C",
        "signature": "Deep charcoal-to-near-black vertical gradient with a soft central vignette and faint film grain. Cool key light from upper-left with a warm golden rim along one edge. Heavy condensed sans headline, 900 weight, near-white. Muted-gold pill badges. Cinematic low-key luxury catalog mood.",
    },
    "clean_light_studio": {
        "label": "Чистая светлая студия",
        "accent": "#FF4D6D", "secondary": "#F4F5F7",
        "signature": "Bright seamless off-white studio sweep with a soft natural shadow under the product and gentle falloff. Even diffused frontal daylight, true colours, no drama. Clean geometric sans headline, 700 weight, near-black. Rounded badges in one saturated accent. Airy premium e-commerce, Apple-store cleanliness.",
    },
    "bold_color_block": {
        "label": "Цветоблок поп",
        "accent": "#FFFFFF", "secondary": "#1F6FEB",
        "signature": "Flat saturated single-colour background (electric blue) split by one bold diagonal colour block in a contrasting hue. Hard even light, crisp shadows. Chunky grotesque headline, 800 weight, white, large. Solid rectangular badges. Confident graphic scroll-stopping poster energy.",
    },
    "warm_lifestyle": {
        "label": "Тёплый лайфстайл",
        "accent": "#C26B3D", "secondary": "#EFE3D2",
        "signature": "Warm beige and terracotta tones, soft golden-hour window light with long gentle shadows, a hint of cosy real-world surroundings (linen, wood) softly blurred behind. Friendly humanist sans headline, 600 weight, deep brown. Soft pill badges. Inviting homely emotional mood.",
    },
    "tech_minimal_mono": {
        "label": "Тех-минимал моно",
        "accent": "#5AC8FA", "secondary": "#181A1B",
        "signature": "Near-monochrome dark graphite background with a single thin cyan accent line and generous negative space. Precise cool directional light, clean speculars. Tight technical sans headline, 500 weight, light grey, lots of breathing room. Minimal hairline badges. Engineered restrained gadget-review mood.",
    },
    "editorial_magazine": {
        "label": "Журнальный эдиториал",
        "accent": "#111111", "secondary": "#FBF7F0",
        "signature": "Cream paper background with a strong asymmetric layout and a bold vertical margin. Soft north-window daylight. High-contrast display serif headline, large, near-black, mixed case. Thin underline accents instead of pills. Confident fashion-magazine editorial composition.",
    },
    "pastel_soft": {
        "label": "Пастель софт",
        "accent": "#FF8FB1", "secondary": "#F3E8FF",
        "signature": "Soft pastel lilac-to-pink gradient, very gentle diffused light, dreamy low contrast, subtle soft shadow. Rounded friendly sans headline, 700 weight, plum. Cloud-soft rounded badges. Tender gift-y beauty-and-kids mood.",
    },
    "high_contrast_poster": {
        "label": "Контрастный плакат",
        "accent": "#FFD400", "secondary": "#0A0A0A",
        "signature": "Pure black background with one screaming yellow accent and oversized typography. Hard dramatic single-source light, deep shadows, punchy speculars. Ultra-bold compressed sans headline, 900 weight, yellow, huge. Solid black-on-yellow badges. Loud urgent sale-poster impact.",
    },
    "nature_organic": {
        "label": "Природа органик",
        "accent": "#3E7C4F", "secondary": "#E8E2D0",
        "signature": "Natural stone or light-wood surface with soft green foliage shadows and warm dappled daylight. Earthy tactile true-to-life colours. Organic humanist sans headline, 600 weight, forest green. Earth-toned pill badges. Eco natural wholesome mood.",
    },
    "energetic_gradient": {
        "label": "Энергичный градиент",
        "accent": "#FFFFFF", "secondary": "#7A2BE2",
        "signature": "Vivid two-tone diagonal gradient (violet into magenta) with a soft glow halo behind the product and dynamic light streaks. Bright punchy light. Bold rounded sans headline, 800 weight, white with a subtle glow. Glossy capsule badges. Energetic youthful vibrant mood.",
    },
}


def list_art_direction_keys():
    return list(ART_DIRECTIONS.keys())


# ════════════════════════════════════════════════════════════════════════════
# Per-direction MOOD tail — заменяет единый хардкод-хвост «premium 8k catalog»,
# который раньше делал ВСЕ карточки одинаково-глянцевыми. Каждое направление
# завершает image_prompt своим настроением, поэтому рендеры различаются.
# ════════════════════════════════════════════════════════════════════════════
ART_DIRECTION_MOODS = {
    "premium_dark":        "cinematic low-key premium mood, dramatic studio lighting",
    "clean_light_studio":  "bright airy e-commerce clarity, true colours, soft daylight",
    "bold_color_block":    "bold graphic poster energy, flat saturated colour, crisp shadows",
    "warm_lifestyle":      "warm cosy lifestyle mood, soft golden-hour light",
    "tech_minimal_mono":   "restrained engineered tech mood, clean speculars, negative space",
    "editorial_magazine":  "high-end editorial magazine mood, soft north-window daylight",
    "pastel_soft":         "soft dreamy pastel mood, gentle diffused light, low contrast",
    "high_contrast_poster":"loud high-contrast sale-poster mood, hard dramatic light",
    "nature_organic":      "natural organic eco mood, warm dappled daylight, tactile textures",
    "energetic_gradient":  "energetic vibrant mood, glowing gradient, dynamic light streaks",
}

# Базовое техническое качество (без «commercial-grade catalog», который гомогенизировал).
ART_QUALITY_TAIL = "ultra-detailed, photorealistic materials, sharp focus, high resolution"


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY → подходящие арт-направления. Раньше направление выбиралось чисто
# рандомно (uuid % 10) и игнорировало категорию — пульт кондиционера попадал на
# «дуб + листва» (не по теме). Теперь выбор взвешен по категории товара,
# но с вариативностью внутри подходящего пула (повторные прогоны всё равно разные).
# ════════════════════════════════════════════════════════════════════════════
CATEGORY_DIRECTION_MAP = [
    # (ключевые слова в категории/брифе/бренде, [подходящие направления])
    (("ноутбук", "компьютер", "электрон", "гаджет", "телефон", "смартфон", "наушник",
      "пульт", "зарядк", "powerbank", "техник", "монитор", "клавиатур", "мышь", "ssd",
      "роутер", "камера", "дрон", "приставк", "консол"),
     ["tech_minimal_mono", "premium_dark", "bold_color_block", "energetic_gradient"]),
    (("косметик", "крем", "сыворотк", "уход", "макияж", "парфюм", "духи", "маск",
      "шампун", "бальзам", "сред для", "бьюти", "beauty", "лосьон"),
     ["pastel_soft", "clean_light_studio", "editorial_magazine", "warm_lifestyle"]),
    (("детск", "игрушк", "ребен", "ребён", "малыш", "робот", "кукл", "конструктор",
      "машинк", "плюш", "развива"),
     ["bold_color_block", "high_contrast_poster", "pastel_soft", "energetic_gradient"]),
    (("посуд", "кружк", "чашк", "тарелк", "стакан", "термокружк", "чайник", "кастрюл",
      "сковород", "столов", "керамик", "фаянс", "кухн"),
     ["warm_lifestyle", "clean_light_studio", "nature_organic", "editorial_magazine"]),
    (("еда", "продукт", "чай", "кофе", "снэк", "орех", "мёд", "мед ", "эко", "органик",
      "натурал", "специи", "напит"),
     ["nature_organic", "warm_lifestyle", "clean_light_studio"]),
    (("одежд", "куртк", "пуховик", "платье", "футболк", "обувь", "кроссовк", "сумк",
      "аксессуар", "бель", "костюм", "джинс", "пальто", "текстиль", "штор", "плед"),
     ["editorial_magazine", "clean_light_studio", "warm_lifestyle", "pastel_soft"]),
    # Функциональные/инструментальные категории — широкий стем-набор для
    # ПЕРВИЧНОГО pre-pick (до Claude). Claude видит каталог направлений + TONE
    # MATRIX и при необходимости переопределяет через _art_direction_key —
    # поэтому stems нужны не для точности, а только чтобы pre-pick не падал
    # в warm_lifestyle/pastel_soft для функционального товара ДО Claude ответа
    # (если бы упал — design.pack_scene_block гипотетически уже сгенерится
    # под warm_lifestyle, и Claude override поменяет только ключ, не сцену).
    # «авто» как substring ловит и «автохимия», и «автотовар», и «авто» в брифе.
    (("спорт", "фитнес", "турист", "велосипед", "тренаж", "инструмент", "дрель",
      "строит", "сад", "дача", "авто", "химия", "хим.", "жидкост", "масл",
      "канистр", "расходник", "техжидкост",
      "незамерзайк", "антифриз", "тосол", "омывател", "стеклоомыв", "зимн жидк",
      "моторн масл", "тормозн", "охлаждающ"),
     ["bold_color_block", "tech_minimal_mono", "high_contrast_poster", "premium_dark"]),
    (("ювелир", "часы", "украшен", "золот", "серебр", "premium", "люкс", "элитн"),
     ["premium_dark", "editorial_magazine", "tech_minimal_mono"]),
    (("распродаж", "скидк", "акци", "хит", "выгод", "дешев", "недорог"),
     ["high_contrast_poster", "bold_color_block", "energetic_gradient"]),
]

# Бренды, у которых есть устойчивый фирменный визуальный код.
BRAND_DIRECTION_HINTS = {
    "яндекс": "high_contrast_poster",   # жёлтый/чёрный
    "yandex": "high_contrast_poster",
    "ozon": "bold_color_block",
    "тинькофф": "high_contrast_poster",
    "т-банк": "high_contrast_poster",
    "сбер": "nature_organic",           # зелёный
    "lego": "bold_color_block",
}


def art_directions_for_category(hint_text: str):
    """Вернёт список подходящих ключей направлений по тексту (категория/бриф/бренд).
    Пусто → подходит весь пул (рандом). Бренд-хинт ставится первым (приоритет)."""
    if not hint_text:
        return []
    t = str(hint_text).lower()
    picks = []
    for brand, key in BRAND_DIRECTION_HINTS.items():
        if brand in t and key in ART_DIRECTIONS:
            picks.append(key)
    for keywords, dirs in CATEGORY_DIRECTION_MAP:
        if any(k in t for k in keywords):
            for d in dirs:
                if d not in picks:
                    picks.append(d)
            break  # первая совпавшая категория — самая специфичная
    return picks


def list_format_choices():
    """Returns list of (key, label) ordered by group for UI dropdown/checkbox."""
    grouped = {}
    for key, meta in FORMATS.items():
        grouped.setdefault(meta["group"], []).append((key, meta["label"]))
    out = []
    for group in ("Wildberries", "Ozon", "Я.Маркет", "Соцсети"):
        out.extend(grouped.get(group, []))
    return out


def list_card_type_choices():
    return [(k, v["label"]) for k, v in CARD_TYPES.items()]


def list_style_choices():
    return [(k, v) for k, v in STYLES.items()]
