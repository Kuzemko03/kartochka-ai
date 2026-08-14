"""
Pricing — the SINGLE source of truth for the card economy.

Карточки — единственная валюта. Одна **карточка = одна генерация** пака дизайнов
для одного товара. Правки уже сгенерированной карточки бесплатны (это часть оффера
«безлимит правок»).

Модель оплаты ОДНА — **ползунок**: пользователь выбирает произвольное число
карточек, и цена за карточку плавно падает с объёмом (чем больше — тем дешевле).
Никаких отдельных тарифов/пакетов — только токены-карточки.

Деньги — в копейках (int), без float. Кривая цены отдаётся фронту в
``public_pricing`` целиком, поэтому цена в ползунке считается мгновенно на
клиенте, без запроса на сервер за каждым движением (см. account.js).
"""

from __future__ import annotations

from bisect import bisect_right

# Free tier / referral (в карточках)
# Одна бесплатная карточка на аккаунт НАВСЕГДА (не в день) — проба продукта.
FREE_GENERATIONS_LIFETIME = 1  # сколько бесплатных генераций даётся новому аккаунту
SIGNUP_BONUS_CARDS = 0         # бонус на баланс при регистрации (free-tier и так даёт пробу)
REFERRAL_SIGNUP_CARDS = 3      # другу при регистрации по ссылке
REFERRAL_PURCHASE_CARDS = 5    # пригласившему — при первой оплате реферала


# ── Card slider (volume discount) — ЕДИНСТВЕННАЯ модель оплаты ────────────────
SLIDER_MIN_CARDS = 3
SLIDER_MAX_CARDS = 300
SLIDER_STEP = 1
SLIDER_DEFAULT_CARDS = 40   # стартовое положение ползунка (хорошая «середина»)

# Anchors for the per-card price curve: (cards, kopecks_per_card). Strictly
# decreasing → больше объём = дешевле за карточку; линейная интерполяция между
# якорями (без обрывов): 3 → 33 ₽/карточка, 300 → 20 ₽/карточка.
_PRICE_ANCHORS: list[tuple[int, float]] = [
    (3,   3300.0),   # 33 ₽/карточка
    (10,  3000.0),   # 30 ₽
    (40,  2500.0),   # 25 ₽
    (100, 2200.0),   # 22 ₽
    (300, 2000.0),   # 20 ₽
]
_ANCHOR_X = [a[0] for a in _PRICE_ANCHORS]

# Базовая («якорная») цена за карточку — от неё считаем «скидку N%» для UI.
_BASE_PRICE_KOPECKS = _PRICE_ANCHORS[0][1]


def _kopecks_per_card(cards: int) -> float:
    if cards <= _PRICE_ANCHORS[0][0]:
        return _PRICE_ANCHORS[0][1]
    if cards >= _PRICE_ANCHORS[-1][0]:
        return _PRICE_ANCHORS[-1][1]
    i = bisect_right(_ANCHOR_X, cards) - 1
    x0, y0 = _PRICE_ANCHORS[i]
    x1, y1 = _PRICE_ANCHORS[i + 1]
    t = (cards - x0) / (x1 - x0)
    return y0 + (y1 - y0) * t


def clamp_slider_cards(cards: int) -> int:
    cards = int(cards)
    cards = max(SLIDER_MIN_CARDS, min(SLIDER_MAX_CARDS, cards))
    return cards


def quote_cards(cards: int) -> dict:
    """Authoritative price for a slider card purchase (kopecks + display)."""
    cards = clamp_slider_cards(cards)
    per_card_kop = _kopecks_per_card(cards)
    amount_kopecks = int(round(cards * per_card_kop))
    base_total = cards * _BASE_PRICE_KOPECKS
    save_kopecks = max(0, int(round(base_total - amount_kopecks)))
    discount_pct = int(round((1 - per_card_kop / _BASE_PRICE_KOPECKS) * 100))
    return {
        "cards": cards,
        "amount_kopecks": amount_kopecks,
        "amount_rub": round(amount_kopecks / 100, 2),
        "per_card_kopecks": round(per_card_kop, 2),
        "per_card_rub": round(per_card_kop / 100, 2),
        "save_kopecks": save_kopecks,
        "save_rub": round(save_kopecks / 100, 2),
        "discount_pct": discount_pct,
    }


def public_pricing() -> dict:
    """Everything the frontend needs — computed in one place.

    ``packs`` оставлен пустым словарём для обратной совместимости со старым
    фронтом/кодом, который мог обращаться к ключу (новые экраны его не используют).
    """
    return {
        "free_generations": FREE_GENERATIONS_LIFETIME,
        # legacy-алиас (старый фронт ждал это имя) — оставляем для совместимости
        "daily_free_generations": 0,
        "packs": {},
        "slider": {
            "min_cards": SLIDER_MIN_CARDS,
            "max_cards": SLIDER_MAX_CARDS,
            "step": SLIDER_STEP,
            "default_cards": SLIDER_DEFAULT_CARDS,
            "base_price_rub": round(_BASE_PRICE_KOPECKS / 100, 2),
            # якоря кривой → фронт считает цену мгновенно, без round-trip
            "anchors": [[c, k] for c, k in _PRICE_ANCHORS],
            "samples": [quote_cards(c) for c in (3, 10, 40, 100, 300)],
        },
        "referral": {
            "signup_cards": REFERRAL_SIGNUP_CARDS,
            "purchase_cards": REFERRAL_PURCHASE_CARDS,
        },
    }
