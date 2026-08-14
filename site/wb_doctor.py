# -*- coding: utf-8 -*-
"""WB-доктор: диагностика доступности Wildberries с ЭТОЙ машины.

Запуск на хостинге/локально:  python wb_doctor.py
Печатает, какой путь к WB жив (внутренний u-search / публичный search.wb.ru /
карточки), и вердикт — что именно блокируется и что делать.
Ничего не меняет, безопасен, ~30-60 секунд (добыча токена — самое долгое).
"""
import json
import sys
import time

import requests
import urllib3

urllib3.disable_warnings()
sys.path.insert(0, ".")

from wb_parser import _DUFF_URL, _DUFF_HEADERS, _duff_params, _random_headers  # noqa: E402

Q = "кружка с котом"
results = {}


def show(name, status, extra=""):
    mark = "✓" if status == 200 else "✗"
    print(f"  {mark} {name}: HTTP {status}{(' — ' + extra) if extra else ''}")


print("═══ WB-доктор ═══")
try:
    ip = requests.get("https://api.ipify.org?format=json", timeout=10).json().get("ip")
    print(f"  внешний IP: {ip}")
except Exception as e:
    print(f"  внешний IP: не определился ({type(e).__name__})")

# 1) Токен (как его добывает прод — Selenium; здесь просто берём из пула)
tok = None
try:
    from wb_token_pool import get_global_pool
    tok = get_global_pool(min_size=1, max_size=2).get_token(wait_seconds=45.0)
except Exception as e:
    print(f"  токен-пул: ошибка {type(e).__name__}: {e}")
print(f"  x_wbaas_token: {'ПОЛУЧЕН' if tok else 'НЕ ПОЛУЧЕН'}")

params = _duff_params(Q, dest="-1275551")
cookies = {"x_wbaas_token": tok} if tok else None

# 2) Внутренний u-search (метод smart-стека), обычный TLS
try:
    r = requests.get(_DUFF_URL, params=params, headers=_DUFF_HEADERS,
                     cookies=cookies, timeout=(8, 20))
    results["usearch"] = r.status_code
    show("u-search (внутренний, python-TLS)", r.status_code)
except Exception as e:
    results["usearch"] = 0
    print(f"  ✗ u-search: {type(e).__name__}: {str(e)[:100]}")

# 3) u-search под Chrome-TLS (curl_cffi), если установлен
try:
    from curl_cffi import requests as cf
    r = cf.get(_DUFF_URL, params=params, headers=_DUFF_HEADERS,
               cookies=cookies, timeout=25, impersonate="chrome124")
    results["usearch_cffi"] = r.status_code
    show("u-search (Chrome-TLS, curl_cffi)", r.status_code)
except ImportError:
    print("  · curl_cffi не установлен (pip install curl_cffi) — Chrome-TLS не проверен")
except Exception as e:
    results["usearch_cffi"] = 0
    print(f"  ✗ u-search chrome-TLS: {type(e).__name__}: {str(e)[:100]}")

# 4) Публичный search.wb.ru (легаси-фолбэк)
try:
    p2 = {"appType": "1", "curr": "rub", "dest": "-1275551", "lang": "ru",
          "query": Q, "resultset": "catalog", "sort": "popular", "spp": "30",
          "suppressSpellcheck": "false"}
    r = requests.get("https://search.wb.ru/exactmatch/ru/common/v5/search",
                     params=p2, headers=_random_headers(), timeout=(8, 20))
    n = 0
    if r.status_code == 200:
        j = r.json()
        n = len((j.get("data") or {}).get("products") or j.get("products") or [])
    results["legacy"] = r.status_code
    show("search.wb.ru (публичный, фолбэк)", r.status_code, f"товаров: {n}" if n else "")
except Exception as e:
    results["legacy"] = 0
    print(f"  ✗ search.wb.ru: {type(e).__name__}: {str(e)[:100]}")

# 5) Карточка/фото (enrich-путь) — на примере статичного basket-хоста
try:
    r = requests.head("https://basket-01.wbbasket.ru/vol123/part12345/12345678/images/big/1.webp",
                      timeout=(5, 10), allow_redirects=False)
    # 404 здесь НОРМАЛЕН (nm выдуман) — важно, что хост отвечает, а не банит
    ok = r.status_code in (200, 404)
    print(f"  {'✓' if ok else '✗'} basket-CDN (карточки/фото): HTTP {r.status_code} "
          f"({'доступен' if ok else 'блок?'})")
except Exception as e:
    print(f"  ✗ basket-CDN: {type(e).__name__}: {str(e)[:100]}")

# 6) Прокси из config.wb_smart (если настроен): жив ли + пускает ли WB через него
try:
    cfg = json.load(open("config.json", encoding="utf-8"))
    bases = (cfg.get("wb_smart") or {}).get("proxies") or []
    if bases:
        from wb_smart_client import _fresh_proxy_url
        purl = _fresh_proxy_url(bases[0], (cfg.get("wb_smart") or {}).get("proxy_country", "ru"))
        pxy = {"http": purl, "https": purl}
        t0 = time.time()
        try:
            r = requests.get("https://api.ipify.org?format=json",
                             proxies=pxy, timeout=(10, 15))
            print(f"  ✓ residential-прокси: жив, внешний IP через него: {r.json().get('ip')}")
            results["proxy"] = 200
        except Exception as e:
            print(f"  ✗ residential-прокси: НЕ РАБОТАЕТ ({type(e).__name__} за "
                  f"{time.time()-t0:.0f}s) — либо кончился баланс/трафик, либо его "
                  f"глушит VPN на ЭТОЙ машине (на хостинге без VPN может быть жив)")
            results["proxy"] = 0
        # Главная проверка «как раньше»: внутренний u-search ЧЕРЕЗ прокси
        if results.get("proxy") == 200:
            try:
                r = requests.get(_DUFF_URL, params=params, headers={**_DUFF_HEADERS,
                                 "Connection": "close"}, cookies=cookies,
                                 proxies=pxy, timeout=(12, 25))
                results["usearch_proxy"] = r.status_code
                show("u-search ЧЕРЕЗ residential-прокси", r.status_code,
                     "прежний быстрый метод работает" if r.status_code == 200 else "")
            except Exception as e:
                results["usearch_proxy"] = 0
                print(f"  ✗ u-search через прокси: {type(e).__name__}: {str(e)[:100]}")
    else:
        print("  · прокси в config.wb_smart.proxies не настроен")
except Exception as e:
    print(f"  · конфиг не прочитан: {e}")

# ── Вердикт ──────────────────────────────────────────────────────────────────
print("\n═══ ВЕРДИКТ ═══")
us = results.get("usearch")
us_c = results.get("usearch_cffi")
lg = results.get("legacy")
if us == 200:
    print("  Внутренний u-search работает напрямую — smart-стек здоров, проблем нет.")
elif results.get("usearch_proxy") == 200:
    print("  Direct забанен антиботом, но u-search ЧЕРЕЗ residential-прокси работает —")
    print("  после обновления 2026-07-06 (403 → переключение на прокси) парсер сам")
    print("  ходит этим путём: прежний быстрый метод, +~1с на прокси-хоп.")
elif us_c == 200:
    print("  u-search работает ТОЛЬКО под Chrome-TLS → WB стал сверять TLS-отпечаток.")
    print("  ЛЕЧЕНИЕ: в config.json → wb_smart.use_curl_cffi: true (+ pip install curl_cffi).")
elif lg == 200:
    print("  WB забанил этот IP для внутреннего u-search (антибот), прокси-путь недоступен")
    print("  (мёртв или глушится VPN), но ПУБЛИЧНЫЙ поиск жив — парсер работает через")
    print("  автофолбэк. Для прежнего метода нужен рабочий residential-прокси.")
else:
    print("  WB недоступен целиком с этого IP (или сети нет). Если это хостинг — ")
    print("  подсеть в жёстком бане: нужен рабочий residential-прокси или смена IP.")
