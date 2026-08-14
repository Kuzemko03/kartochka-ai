# -*- coding: utf-8 -*-
"""
WB парсер: по запросу возвращает топ-N товаров с описанием, характеристиками и фото.

Примеры:
    python wb_parser.py "наушники"
    python wb_parser.py "наушники" --top 5 --download
    python wb_parser.py --nm 334835953              # один товар по артикулу
    python wb_parser.py --nm 334835953 --download   # + скачать все фото

Что делает:
    1) search.wb.ru        — топ-N по запросу (по популярности).
    2) basket-XX.wbbasket  — полная карточка: имя, бренд, описание, характеристики.
    3) probe фото          — определяет реальное число фото на CDN.
    4) --download          — кладёт всё в output/<запрос>/<NN_nmid_имя>/.

Если search.wb.ru вернёт 429 — IP в рейт-лимите (часто DC/нерос. IP),
парсер скажет это явно и предложит включить RU-VPN.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import requests

# windows cp1251 → utf-8 (иначе крашится на кириллице)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ─── константы ──────────────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 OPR/109.0.0.0",
]

def _random_headers():
    ua = random.choice(_USER_AGENTS)
    return {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
        "Sec-Ch-Ua-Platform": '"Windows"' if "Windows" in ua else '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    }

USER_AGENT = _USER_AGENTS[0]
HEADERS = _random_headers()
DEFAULT_DEST = "-1257786"   # Москва (для другого региона — другое значение)


# ─── basket helpers ─────────────────────────────────────────────────

_BASKET_TABLE = [
    (143, "01"),  (287, "02"),  (431, "03"),  (719, "04"),
    (1007, "05"), (1061, "06"), (1115, "07"), (1169, "08"),
    (1313, "09"), (1601, "10"), (1655, "11"), (1919, "12"),
    (2045, "13"), (2189, "14"), (2405, "15"), (2621, "16"),
    (2837, "17"), (3053, "18"), (3269, "19"), (3485, "20"),
    (3701, "21"), (3917, "22"), (4133, "23"), (4349, "24"),
    (4565, "25"),
]


def basket_guess(vol: int) -> int:
    """Грубая оценка номера баскета по vol. Для vol > 4565 — экстраполяция."""
    for limit, num in _BASKET_TABLE:
        if vol <= limit:
            return int(num)
    # экстраполяция: после 25 баскеты идут шагом ~312
    extra = (vol - 4565 + 311) // 312
    return min(25 + extra, 99)


def card_url(nm: int, basket: int) -> str:
    vol = nm // 100000
    part = nm // 1000
    return (
        f"https://basket-{basket:02d}.wbbasket.ru/"
        f"vol{vol}/part{part}/{nm}/info/ru/card.json"
    )


def img_url(nm: int, basket: int, idx: int, size: str = "big") -> str:
    """size: 'big' (~1000px), 'c516x688' (~516px), 'tm' (~150px thumb)."""
    vol = nm // 100000
    part = nm // 1000
    return (
        f"https://basket-{basket:02d}.wbbasket.ru/"
        f"vol{vol}/part{part}/{nm}/images/{size}/{idx}.webp"
    )


# ─── модель ─────────────────────────────────────────────────────────

@dataclass
class Product:
    nm_id: int
    name: str
    brand: str = ""
    subject: str = ""
    description: str = ""
    characteristics: list[dict] = field(default_factory=list)
    photo_count: int = 0
    photos: list[str] = field(default_factory=list)
    price: Optional[float] = None
    rating: Optional[float] = None
    feedbacks: Optional[int] = None
    supplier: str = ""
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ─── парсер ─────────────────────────────────────────────────────────

# ─── Duff89 рецепт: внутренний эндпоинт + special headers ──────────

_DUFF_URL = "https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search"

_DUFF_HEADERS = {
    "accept": "*/*",
    "accept-language": "ru-RU,ru;q=0.9",
    "priority": "u=1, i",
    "referer": ("https://www.wildberries.ru/catalog/0/search.aspx?"
                "page=1&sort=popular&search="
                "%D0%BA%D1%80%D0%BE%D1%81%D1%81%D0%BE%D0%B2%D0%BA%D0%B8"  # url-encoded
                "+%D0%BC%D1%83%D0%B6%D1%81%D0%BA%D0%B8%D0%B5"
                "&priceU=250000%3B350000"),
    "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"),
    "x-requested-with": "XMLHttpRequest",
    "x-spa-version": "13.21.4",
    "x-userid": "0",
}


def _duff_params(query: str, dest: str = "-1275551", page: int = 1,
                 min_price: int = 0, max_price: int = 9999999) -> dict:
    return {
        "ab_testid": "promo_mask_test_1",
        "appType": "1",
        "autoselectFilters": "false",
        "curr": "rub",
        "dest": dest,
        "hide_dtype": "9",
        "hide_vflags": "4294967296",
        "inheritFilters": "false",
        "lang": "ru",
        "page": str(page),
        "priceU": f"{min_price};{max_price}",
        "query": query,
        "resultset": "catalog",
        "spp": "30",
        "suppressSpellcheck": "false",
    }


class WBParser:
    def __init__(self, dest: str = DEFAULT_DEST, timeout: int = 15,
                 photo_limit: int = 5,
                 delay_min: float = 0.5, delay_max: float = 1.5,
                 smart_client=None):
        """
        photo_limit=0 → качать все фото; >0 → не больше N штук на товар.
        delay_min/max — случайная пауза перед каждым HTTP-запросом (антиспам).
        smart_client — опциональный WBSmartClient. Если передан:
            * search() идёт через __internal/u-search v18 с x_wbaas_token
            * smart_client сам ротирует токены/sid/прокси при 429/498
            * delay_min/max снижаются — клиент сам решает
        """
        self.dest = dest
        self.timeout = timeout
        self.connect_timeout = 5
        self.photo_limit = photo_limit
        self.delay_min = max(0.0, delay_min)
        self.delay_max = max(self.delay_min, delay_max)
        self._basket_cache: dict[int, int] = {}
        self._pace_lock = threading.Lock()
        self._last_call = 0.0
        self._cookies_initialized = False

        self.smart_client = smart_client
        if smart_client is not None:
            # smart_client сам управляет сессией и токенами
            self.s = smart_client
            self.s.headers.update(_DUFF_HEADERS)
            self._cookies_initialized = True   # токен теперь из пула
        else:
            self.s = requests.Session()
            self.s.headers.update(_random_headers())
        # ПРЯМАЯ сессия для легаси-фолбэка search.wb.ru. ВСЕГДА обычный requests:
        # при smart-режиме self.s = smart_client, и если фолбэк ходит через него,
        # он наследует залипший proxy-режим с мёртвым прокси → HTTP 0 (диагностика
        # 2026-07-06). Публичный search.wb.ru работает напрямую даже под VPN.
        self._plain = requests.Session()
        self._plain_cookies_ok = False
        # Предохранитель «smart-путь мёртв»: если u-search провалился, а легаси
        # сработал — 10 минут ходим сразу в легаси, не платя таймауты smart-пути
        # (~30-40с) на КАЖДЫЙ поиск. Потом пробуем smart снова (мог ожить).
        self._smart_dead_until = 0.0
        if smart_client is not None:
            # Обещание docstring «delay снижаются»: анализ конкурентов делает
            # ~10-30 запросов (карточки+фото), пейс 0.5-1.5с под глобальным локом
            # добавлял до половины времени enrich. Со smart-стеком антиспам-паузу
            # держим символической.
            self.delay_min = min(self.delay_min, 0.15)
            self.delay_max = min(self.delay_max, 0.4)

    @property
    def _to(self) -> tuple[float, float]:
        return (self.connect_timeout, self.timeout)

    # --- pacing -----------------------------------------------------

    def _pace(self) -> None:
        """
        Случайная пауза между запросами. Берём jitter ∈ [delay_min, delay_max]
        от момента последнего вызова — если уже прошло больше, не ждём вообще.
        Лок защищает от «склеивания» из параллельных тредов в download_all.
        """
        if self.delay_max <= 0:
            return
        target = random.uniform(self.delay_min, self.delay_max)
        with self._pace_lock:
            elapsed = time.monotonic() - self._last_call
            wait = target - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _warm_cookies(self):
        """Visit wildberries.ru once to get session cookies (helps avoid 429)."""
        if self._cookies_initialized:
            return
        try:
            self.s.headers.update(_random_headers())
            self.s.get("https://www.wildberries.ru/", timeout=(5, 10),
                        allow_redirects=True)
            self._cookies_initialized = True
        except Exception:
            pass

    def _get(self, url: str, **kw):
        if not self._cookies_initialized and "search.wb.ru" in url:
            self._warm_cookies()
        self._pace()
        kw.setdefault("timeout", self._to)
        if self.smart_client is not None:
            # card.wb.ru / basket-хосты / фото — ПУБЛИЧНЫЕ: x_wbaas_token им не
            # нужен (он только для __internal/u-search), и прокси им не нужен.
            # Без with_token=False каждый enrich-подзапрос при умершем пуле ждал
            # get_token(15s) → enrich умирал по бюджету 20s («0/3»). Без
            # force_direct enrich наследовал proxy-режим клиента и вязнул на
            # полуживом прокси → терял товары («2/3, зависли: [...]»).
            kw.setdefault("with_token", False)
            kw.setdefault("force_direct", True)
        return self.s.get(url, **kw)

    def _head(self, url: str, **kw):
        self._pace()
        kw.setdefault("timeout", self._to)
        kw.setdefault("allow_redirects", False)
        if self.smart_client is not None:
            kw.setdefault("with_token", False)    # см. _get: фото-HEAD'ам токен не нужен
            kw.setdefault("force_direct", True)   # и прокси не нужен — публичный CDN
        return self.s.head(url, **kw)

    # --- basket discovery -------------------------------------------

    def find_basket(self, nm: int) -> Optional[int]:
        """
        Находит номер баскета, на котором лежит товар.
        Сначала статическая таблица, потом HEAD-проба соседних номеров.
        Кеширует результат.
        """
        if nm in self._basket_cache:
            return self._basket_cache[nm]

        vol = nm // 100000
        guess = basket_guess(vol)

        order: list[int] = [guess]
        for d in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, 7, 8, 9, 10):
            n = guess + d
            if 1 <= n <= 50 and n not in order:
                order.append(n)

        for n in order:
            try:
                r = self._head(card_url(nm, n))
            except requests.RequestException:
                continue
            if r.status_code == 200:
                self._basket_cache[nm] = n
                return n
        return None

    # --- search ----------------------------------------------------

    # Multiple search endpoints — rotate on 429.
    # v7 is the current stable version; v5 and v9 as fallbacks.
    # appType=1 = web, appType=4 = mobile (different rate limits).
    _SEARCH_CONFIGS = [
        {"url": "https://search.wb.ru/exactmatch/ru/common/v7/search", "appType": "1"},
        {"url": "https://search.wb.ru/exactmatch/ru/common/v7/search", "appType": "4"},
        {"url": "https://search.wb.ru/exactmatch/ru/common/v9/search", "appType": "1"},
        {"url": "https://search.wb.ru/exactmatch/ru/common/v5/search", "appType": "1"},
        {"url": "https://search.wb.ru/exactmatch/ru/common/v9/search", "appType": "4"},
        {"url": "https://search.wb.ru/exactmatch/ru/male/v7/search",   "appType": "1"},
        {"url": "https://search.wb.ru/exactmatch/ru/female/v7/search", "appType": "1"},
    ]

    def _search_via_smart(self, query: str, limit: int) -> list[dict]:
        """
        Метод Duff89: __internal/u-search v18 + x_wbaas_token (приходит из
        smart_client.token_pool автоматом). smart_client сам делает retry,
        ротацию sid и переключение direct↔proxy.
        """
        params = _duff_params(query, dest="-1275551")
        try:
            r = self.smart_client.get(_DUFF_URL, params=params, headers=_DUFF_HEADERS)
        except Exception as e:
            raise RuntimeError(f"WB internal search упал: {type(e).__name__}: {e}")

        if r is None:
            raise RuntimeError("Wildberries не ответил (сетевая ошибка после всех попыток)")
        if r.status_code != 200:
            # Человекочитаемый диагноз по коду — этот текст доходит до юзера в модалке.
            if r.status_code in (403, 401):
                raise RuntimeError(
                    "Wildberries блокирует запросы с текущего IP (антибот, "
                    f"HTTP {r.status_code}). Частая причина — включённый VPN или "
                    "зарубежный IP: отключите VPN и повторите. Если VPN нужен — "
                    "добавьте residential-прокси в config.wb_smart.proxies.")
            if r.status_code == 429:
                raise RuntimeError(
                    "Wildberries ограничил частоту запросов (HTTP 429) — "
                    "подождите минуту и повторите поиск.")
            if r.status_code == 0:
                raise RuntimeError(
                    "Не удалось соединиться с Wildberries (таймаут/обрыв сети "
                    "после всех попыток) — проверьте интернет/VPN и повторите.")
            raise RuntimeError(
                f"Wildberries вернул ошибку HTTP {r.status_code} после всех попыток — "
                "попробуйте ещё раз чуть позже.")
        try:
            j = r.json()
        except Exception:
            raise RuntimeError("WB internal search: не-JSON ответ")

        products = (j.get("data") or {}).get("products") or j.get("products") or []
        if not products:
            return []

        items: list[dict] = []
        for p in products[:limit]:
            sizes = p.get("sizes") or [{}]
            raw_price = (sizes[0].get("price") or {}).get("product")
            items.append({
                "nm_id":     p.get("id"),
                "name":      p.get("name", ""),
                "brand":     p.get("brand", ""),
                "supplier":  p.get("supplier", ""),
                "rating":    p.get("reviewRating") or p.get("rating"),
                "feedbacks": p.get("feedbacks", 0),
                "pics":      p.get("pics", 0),
                "price":     (raw_price / 100) if raw_price else None,
            })
        return items

    def search(self, query: str, limit: int = 5, max_attempts: int = 8) -> list[dict]:
        """Топ-N карточек. smart_client (внутренний u-search) → при провале
        АВТОФОЛБЭК на публичный search.wb.ru → только потом ошибка.

        Зачем фолбэк (диагностика 2026-07-06, юзер под VPN): антибот WB (Angie)
        режет по IP ТОЛЬКО __internal/u-search (HTTP 403), а публичный
        search.wb.ru/exactmatch v5/v9 с того же VPN-IP отдаёт 200 и полную
        выдачу. Раньше провал smart-пути валил весь поиск, хотя рабочий
        публичный эндпоинт был в этом же классе строкой ниже."""
        if self.smart_client is not None:
            if time.monotonic() < self._smart_dead_until:
                # smart-путь недавно падал, а легаси работал → не платим его
                # таймауты на каждый запрос; после cooldown попробуем smart снова.
                try:
                    return self._search_legacy(query, limit, max_attempts=3)
                except RuntimeError:
                    pass  # легаси внезапно сдох → ниже всё же пробуем smart
            try:
                return self._search_via_smart(query, limit)
            except RuntimeError as e_smart:
                print(f"  WB smart-путь не прошёл ({e_smart}) → пробую публичный search.wb.ru")
                try:
                    # 3 быстрые попытки: фолбэк не должен добавлять минуты ожидания
                    items = self._search_legacy(query, limit, max_attempts=3)
                    self._smart_dead_until = time.monotonic() + 600.0
                    print("  публичный search.wb.ru работает → следующие 10 мин ищем сразу через него")
                    return items
                except RuntimeError as e_legacy:
                    print(f"  публичный search.wb.ru тоже не прошёл: {e_legacy}")
                    # Наружу — smart-ошибка: она человекочитаемая (VPN/антибот/лимит)
                    raise e_smart
        return self._search_legacy(query, limit, max_attempts)

    def _search_legacy(self, query: str, limit: int = 5, max_attempts: int = 8) -> list[dict]:
        """Публичный search.wb.ru (v5/v7/v9 exactmatch) с ротацией конфигов.
        Ходит СТРОГО через self._plain (прямая requests.Session) — НЕ через self.s,
        который в smart-режиме является smart_client'ом (см. __init__)."""
        if not self._plain_cookies_ok:
            try:
                self._plain.get("https://www.wildberries.ru/", timeout=(5, 10),
                                headers=_random_headers(), allow_redirects=True)
            except Exception:
                pass
            self._plain_cookies_ok = True
        last_err = "no attempts"
        for attempt in range(1, max_attempts + 1):
            req_headers = _random_headers()
            cfg = self._SEARCH_CONFIGS[(attempt - 1) % len(self._SEARCH_CONFIGS)]
            search_url = cfg["url"]

            params = {
                "appType": cfg["appType"],
                "curr": "rub",
                "dest": self.dest,
                "lang": "ru",
                "query": query,
                "resultset": "catalog",
                "sort": "popular",
                "spp": "30",
                "suppressSpellcheck": "false",
            }

            if attempt > 1:
                # Progressive backoff: 3-6s, 5-10s, 8-16s, ...
                wait = random.uniform(2, 5) + 2 * attempt
                ver = search_url.split("/")[-2]
                app = cfg["appType"]
                print(f"  search: попытка {attempt}/{max_attempts}, "
                      f"{ver}/appType={app}, жду {wait:.0f}с")
                time.sleep(wait)

            try:
                self._pace()
                r = self._plain.get(search_url, params=params,
                                    headers=req_headers, timeout=self._to)
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                continue

            if r.status_code == 200:
                try:
                    j = r.json()
                except ValueError:
                    last_err = "не json"
                    continue

                # v5: products top-level; v7/v9: may be in data.products
                products = (j.get("data") or {}).get("products") or j.get("products") or []
                if not products:
                    last_err = "пустой ответ (вероятно блок по IP или пустая выдача)"
                    continue

                items: list[dict] = []
                for p in products[:limit]:
                    sizes = p.get("sizes") or [{}]
                    raw_price = (sizes[0].get("price") or {}).get("product")
                    items.append({
                        "nm_id":     p.get("id"),
                        "name":      p.get("name", ""),
                        "brand":     p.get("brand", ""),
                        "supplier":  p.get("supplier", ""),
                        "rating":    p.get("reviewRating") or p.get("rating"),
                        "feedbacks": p.get("feedbacks", 0),
                        "pics":      p.get("pics", 0),
                        "price":     (raw_price / 100) if raw_price else None,
                    })
                return items

            last_err = f"HTTP {r.status_code}"
            if r.status_code == 429:
                # Check X-Ratelimit-Retry header
                retry_after = r.headers.get("X-Ratelimit-Retry")
                if retry_after:
                    try:
                        wait_s = int(retry_after)
                        print(f"  search → 429, X-Ratelimit-Retry={wait_s}с")
                        time.sleep(wait_s)
                        continue
                    except ValueError:
                        pass
                ver = search_url.split("/")[-2]
                print(f"  search → 429 на {ver}, ротирую (попытка {attempt}/{max_attempts})")
                continue
            if r.status_code in (500, 502, 503, 504):
                continue
            break

        raise RuntimeError(f"search.wb.ru недоступен после {max_attempts} попыток: {last_err}")

    # --- card ------------------------------------------------------

    def fetch_card(self, nm_id: int) -> dict:
        basket = self.find_basket(nm_id)
        if basket is None:
            return {}
        try:
            r = self._get(card_url(nm_id, basket))
        except requests.RequestException:
            return {}
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return {}
        return {}

    # --- photo count probe -----------------------------------------

    def probe_photo_count(self, nm_id: int, hint: int = 0, max_check: int = 8) -> int:
        """
        Если есть hint (из search.pics или media.photo_count) — берём его.
        Иначе пробегаемся HEAD-ами до первого не-200.

        max_check снижен с 30 до 8: для анализа конкурентов нам достаточно
        первых 6-8 фото (главное + feature/lifestyle). 30 последовательных
        HEAD-ов могли занимать десятки секунд на медленном прокси.
        """
        if hint > 0:
            return hint

        basket = self.find_basket(nm_id)
        if basket is None:
            return 0

        count = 0
        for i in range(1, max_check + 1):
            try:
                r = self._head(img_url(nm_id, basket, i, "big"))
            except requests.RequestException:
                break
            if r.status_code == 200:
                count = i
            else:
                break
        return count

    # --- enrich ----------------------------------------------------

    def enrich(self, base: dict) -> Product:
        nm = int(base["nm_id"])
        card = self.fetch_card(nm)

        name = base.get("name") or card.get("imt_name", "")
        brand = base.get("brand") or (card.get("selling") or {}).get("brand_name", "")
        subject = card.get("subj_name", "") or card.get("subj_root_name", "")
        description = (card.get("description") or "").strip()

        # характеристики: сначала grouped_options (новый формат), потом плоский options
        chars: list[dict] = []
        for grp in card.get("grouped_options") or []:
            for opt in grp.get("options") or []:
                if opt.get("name"):
                    chars.append({"name": opt["name"], "value": opt.get("value", "")})
        if not chars:
            for opt in card.get("options") or []:
                if opt.get("name"):
                    chars.append({"name": opt["name"], "value": opt.get("value", "")})

        # фото
        hint = base.get("pics") or (card.get("media") or {}).get("photo_count") or 0
        photo_count = self.probe_photo_count(nm, hint=int(hint))
        basket = self._basket_cache.get(nm)
        photos = (
            [img_url(nm, basket, i, "big") for i in range(1, photo_count + 1)]
            if basket is not None else []
        )

        return Product(
            nm_id=nm,
            name=name,
            brand=brand,
            subject=subject,
            description=description,
            characteristics=chars,
            photo_count=photo_count,
            photos=photos,
            price=base.get("price"),
            rating=base.get("rating"),
            feedbacks=base.get("feedbacks"),
            supplier=base.get("supplier", ""),
            url=f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        )

    def top(self, query: str, limit: int = 5) -> list[Product]:
        base = self.search(query, limit)
        return [self.enrich(b) for b in base]

    # --- download --------------------------------------------------

    def download_photo(self, url: str, dst: Path, attempts: int = 3) -> bool:
        """
        Скачивает одно фото. До `attempts` попыток с бэкофом.
        404 не ретрается (фото нет — нет смысла).
        Запись на диск тоже в try: битые пути не валят процесс.
        """
        last_status: Optional[int] = None
        for i in range(1, attempts + 1):
            try:
                r = self._get(url)
            except requests.RequestException:
                time.sleep(0.5 * i)
                continue

            last_status = r.status_code
            if r.status_code == 404:
                return False
            if r.status_code == 200 and r.content:
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(r.content)
                    return True
                except OSError:
                    return False
            time.sleep(0.5 * i)
        return False

    def select_photos(self, p: Product) -> list[tuple[int, str]]:
        """Возвращает (idx, url) для скачивания с учётом photo_limit."""
        items = list(enumerate(p.photos, 1))
        if self.photo_limit > 0:
            items = items[: self.photo_limit]
        return items

    def download_all(self, products: list[Product], folder: Path,
                     workers: int = 8) -> dict[int, tuple[int, int]]:
        """
        Качает фото параллельно с учётом photo_limit. Возвращает {nm_id: (ok, total)}.
        Ошибки одного товара не валят остальные.
        """
        stats: dict[int, list[int]] = {p.nm_id: [0, 0] for p in products}
        tasks: list[tuple[int, str, Path]] = []

        for i, p in enumerate(products, 1):
            sub = folder / f"{i:02d}_{p.nm_id}_{safe_name(p.name, 40)}"
            try:
                sub.mkdir(parents=True, exist_ok=True)
                (sub / "card.json").write_text(
                    json.dumps(p.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                # папка/json не создались — пропускаем фото этого товара
                continue
            for j, url in self.select_photos(p):
                tasks.append((p.nm_id, url, sub / f"{j:02d}.webp"))
                stats[p.nm_id][1] += 1

        if not tasks:
            return {nm: (ok, tot) for nm, (ok, tot) in stats.items()}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(self.download_photo, url, dst): nm for nm, url, dst in tasks}
            for fut in as_completed(futs):
                nm = futs[fut]
                try:
                    if fut.result():
                        stats[nm][0] += 1
                except Exception:
                    pass

        return {nm: (ok, tot) for nm, (ok, tot) in stats.items()}


# ─── утилиты ────────────────────────────────────────────────────────

def safe_name(s: str, n: int = 60) -> str:
    """
    Имя для файла/папки в Windows.
    Чистит запрещённые символы и — критично — обрезает пробелы/точки в конце
    (Windows их молча режет на mkdir, потом не находит файл по тому же пути).
    """
    s = re.sub(r'[\\/:*?"<>|\r\n\t\x00-\x1f]+', "_", s)
    s = s[:n]
    s = s.strip().rstrip(" ._")
    return s or "_"


def save_results(products: list[Product], query: str, out_dir: Path,
                 download: bool, parser: WBParser) -> Path:
    folder = out_dir / safe_name(query)
    folder.mkdir(parents=True, exist_ok=True)

    summary = {
        "query": query,
        "count": len(products),
        "products": [p.to_dict() for p in products],
    }
    json_path = folder / "results.json"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if download and products:
        print("\nкачаю фото…")
        stats = parser.download_all(products, folder)
        for i, p in enumerate(products, 1):
            ok, tot = stats.get(p.nm_id, (0, 0))
            print(f"  [{i}] nm={p.nm_id}  {ok}/{tot} фото  ←  {p.name[:60]}")

    return json_path


def print_products(products: list[Product]) -> None:
    for i, p in enumerate(products, 1):
        price = f"{p.price:.0f} ₽" if p.price else "—"
        rating = f"★ {p.rating}" if p.rating else "—"
        fb = f"{p.feedbacks} отзывов" if p.feedbacks else "0 отзывов"
        print(f"\n  [{i}] {p.name}")
        print(f"      бренд: {p.brand or '—'}   категория: {p.subject or '—'}")
        print(f"      {price}   {rating}   {fb}")
        print(f"      артикул: {p.nm_id}   фото: {p.photo_count}")
        print(f"      url: {p.url}")
        if p.description:
            d = re.sub(r"\s+", " ", p.description).strip()
            print(f"      описание: {d[:160]}{'…' if len(d) > 160 else ''}")
        if p.characteristics:
            print(f"      характеристик: {len(p.characteristics)}  "
                  f"(напр.: {p.characteristics[0]['name']} = "
                  f"{str(p.characteristics[0]['value'])[:40]})")


# ─── CLI ────────────────────────────────────────────────────────────

def cli() -> int:
    ap = argparse.ArgumentParser(
        description="WB parser: поиск + карточки + фото",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("query", nargs="?", help="поисковый запрос")
    ap.add_argument("--nm", type=int, help="вместо поиска — артикул товара")
    ap.add_argument("--top", type=int, default=5, help="сколько товаров (default 5)")
    ap.add_argument("--download", action="store_true", help="скачать фото")
    ap.add_argument("--photos", type=int, default=5,
                    help="макс фото на товар (0 = все, default 5)")
    ap.add_argument("--out", default="output", help="папка для результатов (default output)")
    ap.add_argument("--dest", default=DEFAULT_DEST, help="dest id региона WB")
    args = ap.parse_args()

    if not args.query and not args.nm:
        ap.error("укажи запрос или --nm <артикул>")

    parser = WBParser(dest=args.dest, photo_limit=args.photos)
    out_dir = Path(args.out)

    if args.nm:
        print(f"загружаю карточку nm={args.nm}…")
        try:
            p = parser.enrich({"nm_id": args.nm, "name": "", "pics": 0})
        except Exception as e:
            print(f"ОШИБКА: {e}")
            return 2
        products = [p]
        query = f"nm_{args.nm}"
    else:
        print(f"ищу «{args.query}», топ {args.top}…")
        try:
            products = parser.top(args.query, args.top)
        except RuntimeError as e:
            print(f"ОШИБКА: {e}")
            print("Совет: search.wb.ru режет твой IP (вероятно 429).")
            print("       Включи RU-VPN или обычный домашний интернет и повтори.")
            return 2
        query = args.query

    if not products:
        print("ничего не нашлось.")
        return 1

    print(f"\nнайдено товаров: {len(products)}")
    print_products(products)
    json_path = save_results(products, query, out_dir, args.download, parser)
    print(f"\n→ JSON: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(cli())
