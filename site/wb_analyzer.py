# -*- coding: utf-8 -*-
"""
WB Competitor Analyzer — three-step pipeline for competitive intelligence.

PIPELINE:
  Step 1 — detect_category_from_photos(photo_paths, brief, cfg)
      Claude multimodal sees product photos + optional brief.
      Returns: {category, category_wb, search_query_specific, search_query_broad,
                product_description, confidence}

  Step 2 — smart_search_competitors(query_specific, query_broad, limit, dest)
      WBParser: tries specific query → if < 5 results OR avg feedbacks < 10
      → fallback to broad query.
      Returns: {products, query_used, fallback_used, raw_count, avg_feedbacks}

  Step 3 — analyze_competitors(products, cfg, card_count, include_photos)
      Two-phase Claude analysis:

      Phase A — Per-competitor visual analysis (one Claude call per batch):
        • Download up to `card_count` photos per competitor in parallel threads.
        • PHOTO ORDER PRESERVED: photo 1 = main/hero card (most important),
          photos 2+ = feature/detail/lifestyle cards.
        • Batch formation:
          - If competitor has >= MIN_PHOTOS_OWN_BATCH photos → own batch (own Claude call)
          - If competitor has <  MIN_PHOTOS_OWN_BATCH photos → merged with next competitor
            until batch reaches threshold.
        • Each batch call returns per-competitor visual fingerprint.

      Phase B — Strategic synthesis (one final Claude call):
        • All text data (name, brand, price, description, characteristics) +
          per-competitor visual fingerprints from Phase A.
        • Returns full analysis: selling_angles, card_structure, copy_techniques,
          differentiators, recommended_card_plan, key_seo_keywords.

Public helpers:
  format_analysis_for_display(analysis) → Markdown string for UI collapsible block
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests as _req

from wb_parser import WBParser, Product, DEFAULT_DEST
import prompts as P

logger = logging.getLogger("wbnator.analyzer")

# ── Thresholds ────────────────────────────────────────────────────────────────
_MIN_PRODUCTS       = 5    # fewer results → try broad query
_MIN_AVG_FEEDBACKS  = 10   # lower avg → try broad query
# NB: при MAX_PHOTOS_FOR_VISUAL=3 ни один конкурент не набирает 5 фото → правило
# «own batch» сознательно не срабатывает, и батчи всегда склеиваются парами
# (2 конкурента × 3 фото = 6 ≥ 5). Это ДЕШЕВЛЕ (меньше vision-вызовов) — не
# «чинить» порог вниз: MIN_PHOTOS_OWN_BATCH=3 удвоил бы число батчей.
MIN_PHOTOS_OWN_BATCH = 5   # competitor needs >= this many photos to get their own Claude call
# Сколько фото с одного конкурента реально нужно для визуального анализа.
# Поля JSON в _VISUAL_ANALYSIS_SYSTEM (main_card_style, color_palette, typography,
# badge_style, detail_cards_approach, overall_impression, strengths, weaknesses)
# целиком считываются с MAIN_CARD + 2 любых DETAIL_CARD: 4-я/5-я/6-я фотка обычно
# повторяет фирстиль конкурента и для модели — шум. Это НЕ трогает card_count —
# тот по-прежнему рулит финальной генерацией карточек в _synthesize_analysis.
MAX_PHOTOS_FOR_VISUAL = 3


# ── extracted_text hygiene ────────────────────────────────────────────────────
# Клавиши клавиатуры/пульта и одиночные буквы — это ПРИРОДА товара, а не
# отличительная надпись. Дешёвый vision-тир иногда игнорирует прямой запрет из
# промпта (prompts.py: «НЕ списка из 40 клавиш») и вываливает в extracted_text
# все клавиши (Esc, F1…F12, Q, W, E…). Детерминированно вычищаем такой шум, чтобы
# он не утёк в design-промпт как «реальный текст товара» и не замусорил копирайт/
# рендер. Реальные данные (бренд, «500 мл», «RGB», «87 клавиш», артикул) — ≥2
# символов и не совпадают с именами клавиш, поэтому НЕ страдают.
_KEYCAP_NAMED = {
    "esc", "escape", "tab", "caps", "capslock", "caps lock", "shift", "ctrl",
    "control", "alt", "altgr", "win", "cmd", "super", "fn", "enter", "return",
    "space", "spacebar", "backspace", "bksp", "del", "delete", "ins", "insert",
    "home", "end", "pgup", "pgdn", "pageup", "pagedown", "prtsc", "printscreen",
    "scrlk", "scroll lock", "pause", "break", "menu", "numlk", "numlock", "num",
    "up", "down", "left", "right", "↑", "↓", "←", "→",
}
_FKEY_RE = re.compile(r"^f\d{1,2}$", re.I)


def _is_keycap_token(s: str) -> bool:
    """True для одиночной клавиши/буквы/цифры (шум клавиатуры/пульта)."""
    t = (s or "").strip()
    if not t:
        return True
    low = t.lower()
    if low in _KEYCAP_NAMED or _FKEY_RE.match(low):
        return True
    # одиночный символ-клавиша (буква/цифра, лат/кир) — никогда не отличительная надпись
    if len(t) == 1 and t.isalnum():
        return True
    return False


def _clean_extracted_text(items: list) -> list:
    """Нормализация + вычистка keycap-шума из extracted_text."""
    et = [str(s).strip() for s in items if str(s).strip()]
    clean = [s for s in et if not _is_keycap_token(s)]
    dropped = len(et) - len(clean)
    if dropped >= 3:
        logger.info(f"extracted_text: вычистил {dropped} keycap-токенов "
                    f"(клавиши/кнопки/буквы — природа товара, не надпись)")
    return clean

# ── Vision-analysis cache ─────────────────────────────────────────────────────
# Локальный SQLite-кеш для _analyze_batch_visuals. Ключ — sha256 от состава батча
# (data-URI каждой картинки + её detail-уровень + system/user prompts + версия
# логики). При повторном анализе тех же конкурентов (тот же запрос, те же фото
# WB) ответ берётся из кеша → ноль токенов в OpenAI. WB-карточки обычно живут
# месяцами, а юзер регулярно прогоняет анализ → попадание высокое.
#
# Файл: %USERPROFILE%/.wbnator/vision_cache.sqlite (рядом с backups). Безопасен
# к удалению вручную для сброса. Любая ошибка SQLite молча приводит к работе
# БЕЗ кеша — это оптимизация, не критичный путь.
_VISION_CACHE_VERSION = "v1-detail-hi-lo"
_VISION_CACHE_TTL_DAYS = 30
_VISION_CACHE_MODEL_TAG = "vision_cheap"


def _vision_cache_path() -> Path:
    return Path.home() / ".wbnator" / "vision_cache.sqlite"


def _vision_cache_conn() -> Optional[sqlite3.Connection]:
    """Open a one-shot SQLite connection (WAL, short timeout). Caller closes.

    Returns None on any setup error — callers MUST handle None as «no cache»."""
    try:
        path = _vision_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
        # WAL → читатели не блокируют писателей (важно: _analyze_batch_visuals
        # вызывается параллельно через ThreadPoolExecutor для нескольких батчей).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vision_cache ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " model TEXT,"
            " created_at INTEGER NOT NULL"
            ")"
        )
        return conn
    except Exception as e:
        logger.debug(f"_vision_cache_conn: {e}")
        return None


def _vision_cache_key(
    images: list[dict],
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Стабильный sha256 от того, что реально влияет на ответ модели.

    Хешируем сам data-URI картинки — он включает байты изображения; одна и та
    же фотка WB → один и тот же URI → один и тот же ключ. detail-уровень тоже
    в ключе: если когда-то решим перевести часть фоток с low на high, ключ
    поменяется и старые записи не подсунутся. _VISION_CACHE_VERSION — глобальный
    инвалидатор, если поменяется логика (например другой system_prompt)."""
    h = hashlib.sha256()
    for img in images:
        uri = img.get("image", "") if isinstance(img, dict) else str(img)
        detail = (img.get("detail") if isinstance(img, dict) else None) or "auto"
        h.update(hashlib.sha256(uri.encode("utf-8")).digest())
        h.update(b"|")
        h.update(detail.encode("ascii"))
        h.update(b";")
    h.update(b"sys=")
    h.update(hashlib.sha256(system_prompt.encode("utf-8")).digest())
    h.update(b"|user=")
    h.update(hashlib.sha256(user_prompt.encode("utf-8")).digest())
    h.update(b"|ver=")
    h.update(_VISION_CACHE_VERSION.encode("ascii"))
    return h.hexdigest()


def _vision_cache_get(key: str) -> Optional[list[dict]]:
    """HIT → list[dict]; MISS / ошибка / просрочено → None."""
    conn = _vision_cache_conn()
    if conn is None:
        return None
    try:
        cutoff = int(time.time()) - _VISION_CACHE_TTL_DAYS * 86400
        row = conn.execute(
            "SELECT value FROM vision_cache WHERE key=? AND created_at>=?",
            (key, cutoff),
        ).fetchone()
        if not row:
            return None
        parsed = json.loads(row[0])
        if isinstance(parsed, list):
            return parsed
        return None
    except Exception as e:
        logger.debug(f"_vision_cache_get: {e}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _vision_cache_put(key: str, value: list[dict]) -> None:
    """Пустой результат НЕ кешируем — это «модель не справилась», на следующем
    запуске стоит попробовать ещё раз, а не закреплять пустоту."""
    if not value:
        return
    conn = _vision_cache_conn()
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO vision_cache(key,value,model,created_at)"
            " VALUES(?,?,?,?)",
            (key, json.dumps(value, ensure_ascii=False),
             _VISION_CACHE_MODEL_TAG, int(time.time())),
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"_vision_cache_put: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Category detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_category_from_photos(
    photo_paths: list[str],
    brief: str = "",
    cfg: dict = None,
) -> dict:
    """
    Claude sees product photos (multimodal) + optional free-text brief.

    Returns dict:
      category, category_wb, search_query_specific, search_query_broad,
      product_description, confidence
    On failure: same keys, plus "error" key.
    """
    import generator as G

    if cfg is None:
        cfg = G.load_config()

    images = []
    image_hashes = []
    unreadable = []
    for p in (photo_paths or []):
        if not p:
            continue
        try:
            # strict=True — раньше Pillow без AVIF/HEIC плагина не открывал .avif/.heic,
            # код кидал raw-bytes в OpenAI, OpenAI отдавал vision_fail, и пайплайн
            # уходил в text-only → модель отвечала вслепую галлюцинацией. Теперь
            # если перекодировать НЕ удалось — фото в список не попадает и юзер
            # получит понятную ошибку «формат не поддерживается».
            # max_side: это чисто vision-вызов (анализ категории), полное разрешение
            # не нужно — OpenAI всё равно ужимает до 2048px. Экономит тайлы и трафик.
            uri = G._file_to_data_uri(p, strict=True, max_side=G._VISION_MAX_SIDE)
            # detail=high: детектор теперь не только классифицирует категорию, но и
            # читает надписи с товара (extracted_text) — объём, бренд, серию. На low
            # OpenAI ужимает фото до 512px и мелкий текст превращается в кашу: модель
            # не может различить «500 мл» на боку и в итоге выдаёт quick_facts с
            # хардкодными цифрами «350/480 мл», которые юзер видит как «зашитый бред».
            # high дороже на ~10× за фото, но это копейки на один вход в пайплайн
            # и убирает первоисточник «карточка с тупыми вопросами».
            images.append({"name": "product_photo", "image": uri, "detail": "high"})
            try:
                with open(p, "rb") as _fh:
                    image_hashes.append(hashlib.sha1(_fh.read()).hexdigest()[:12])
            except Exception:
                pass
        except G._UnreadableImage as e:
            unreadable.append(str(Path(p).suffix.lower().lstrip('.')) or 'unknown')
            logger.warning(f"detect_category: skipping unreadable photo {p}: {e}")
        except Exception as e:
            logger.warning(f"detect_category: can't read photo {p}: {e}")

    if not images:
        if unreadable:
            fmts = ", ".join(sorted(set(unreadable)))
            return _empty_category(error=(
                f"Не получается прочитать формат «{fmts}». Сохрани фото как "
                f"JPG или PNG и попробуй снова — нейросеть видит только их."
            ))
        return _empty_category(error="Нет доступных фото для анализа")

    # Cache-buster: per-photo SHA1 + per-call nonce make the user_prompt
    # unique for every distinct photo, so any upstream proxy that caches by
    # prompt-hash cannot return a stale "every photo is a cat" stub.
    nonce = uuid.uuid4().hex[:10]
    image_fingerprint = "+".join(image_hashes) if image_hashes else "no-hash"
    _stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    base_prompt = P.build_category_detect_user_prompt(brief, len(images))
    user_prompt = (
        f"{base_prompt}\n"
        f"[request_id: {nonce}]\n"
        f"[image_fingerprint: {image_fingerprint}]\n"
        f"[ts: {_stamp}]\n"
        f"Внимание: каждый запрос — про РАЗНЫЙ товар. Смотри ТОЛЬКО на приложенное "
        f"фото. Не используй кешированный или прошлый ответ. Если на фото "
        f"робот/техника/посуда/одежда — это НЕ мягкая игрушка. "
        f"Для каждого quick_fact заполни поле \"seen\" конкретной деталью С ЭТОГО фото — "
        f"если деталь назвать не можешь, вопрос шаблонный, выкинь его."
    )

    result = None

    # 1) OpenAI vision first — это входная точка пайплайна, поэтому долбим:
    #    до 4 полных проходов цепочки (модели gpt-4.1-mini / gpt-4o-mini /
    #    добавь больше в config), сбрасывая cooldown между проходами.
    #    Раньше «retries_per_model=1, dead_endpoint_threshold=1» делало шаг
    #    хрупким — один 90-секундный сетевой блип = пустой ответ для юзера.
    def _is_semantically_complete(r):
        """Pass-test: ответ модели не семантический мусор. Без category_wb или
        product_title_short вся карточка ниже по пайплайну будет пустой («Не
        получилось распознать фото»), но раньше extract_json формально
        возвращал dict (например только {_uniqueness_scan: [...], confidence:
        "high"}) и ретрая не происходило. Теперь проверяем явно."""
        if not isinstance(r, dict):
            return False
        if not str(r.get("category_wb") or "").strip():
            return False
        if not str(r.get("product_title_short") or "").strip():
            return False
        return True

    # max_tokens=3000 (было 1500): схема detect_category объёмная (category_wb,
    # title, description, _uniqueness_scan[5], quick_facts с "seen", extracted_text).
    # На 1500 модель РЕГУЛЯРНО обрывала JSON на полуслове → extract_json не умеет
    # чинить обрезанный JSON (Repair E = пустой pass) → весь vision-вызов терялся и
    # шёл ДОРОГОЙ ретрай на втором vision-проходе (повторно слался high-detail кадр).
    # Лишние out-токены копеечны, а сэкономленный второй vision-вызов — нет.
    try:
        text = G._claude_chat_persistent(
            cfg, P.CATEGORY_DETECT_SYSTEM_PROMPT, user_prompt,
            images=images, max_tokens=3000, temperature=0.4,
            purpose="detect_category",
            total_budget_seconds=75.0,
            max_chain_passes=4,
            pause_between_passes=4.0,
            reset_health_first=True,
        )
        parsed = P.extract_json(text)
        if _is_semantically_complete(parsed):
            result = parsed
        else:
            logger.warning(
                "detect_category: OpenAI вернула неполный JSON "
                f"(category_wb={parsed.get('category_wb') if isinstance(parsed, dict) else None!r}, "
                f"title={parsed.get('product_title_short') if isinstance(parsed, dict) else None!r}) "
                "— уходим в Claude-fallback"
            )
    except Exception as e:
        logger.warning(f"detect_category: OpenAI vision failed after persistent retries: {e}")

    # 2) OpenAI всё-таки не отдала или вернула семантически пустой JSON —
    # крутим Claude-прокси с такой же долбёжкой.
    if result is None:
        logger.info("detect_category: OpenAI exhausted — trying Claude proxies with persistence")
        try:
            text = G._claude_chat_persistent(
                cfg, P.CATEGORY_DETECT_SYSTEM_PROMPT, user_prompt,
                images=images, max_tokens=3000, temperature=0.4,
                purpose="detect_category",
                total_budget_seconds=90.0,
                max_chain_passes=4,
                pause_between_passes=5.0,
                reset_health_first=False,  # уже стёрли health на проходе 1
            )
            parsed = P.extract_json(text)
            if _is_semantically_complete(parsed):
                result = parsed
            else:
                logger.warning(
                    "detect_category: Claude fallback тоже отдал неполный JSON — "
                    "берём что есть, отдадим в UI как low-confidence."
                )
                result = parsed if isinstance(parsed, dict) else None
        except Exception as e:
            logger.error(f"detect_category: Claude fallback also failed: {e}")
            return _empty_category(error=str(e))

    if result is None:
        return _empty_category(error="Не удалось распарсить JSON-ответ от модели")

    for key in ("category", "category_wb", "search_query_specific",
                "search_query_broad", "product_description",
                "product_title_short", "brand_detected", "confidence"):
        result.setdefault(key, "")
    if not isinstance(result.get("brand_is_product"), bool):
        result["brand_is_product"] = False

    # extracted_text — то, что модель дословно прочитала на товаре. Это и
    # отладочный сигнал (видно, считала ли модель надпись «500 мл»), и потом
    # источник для design-промпта вместо галлюцинации цифр.
    raw_et = result.get("extracted_text")
    if isinstance(raw_et, list):
        result["extracted_text"] = _clean_extracted_text(raw_et)
    elif isinstance(raw_et, str) and raw_et.strip():
        result["extracted_text"] = _clean_extracted_text([raw_et])
    else:
        result["extracted_text"] = []

    # _uniqueness_scan — внутренняя фаза детектора, на которой модель ОБЯЗАНА
    # перечислить конкретные детали именно ЭТОГО фото перед формированием
    # quick_facts. Хранится для отладки и дальше уйдёт в design-промпт как
    # подсказка про что именно делать карточку (а не «декоративная кружка»).
    raw_us = result.get("_uniqueness_scan")
    if isinstance(raw_us, list):
        result["_uniqueness_scan"] = [str(s).strip() for s in raw_us if str(s).strip()]
    else:
        result["_uniqueness_scan"] = []

    # quick_facts: список объектов {key, label, options, seen, why_for_card,
    # category, allow_custom}. Валидируем строго. Вопросы без why_for_card
    # выкидываются — это анти-шаблонная страховка: если модель не может объяснить,
    # зачем ей этот ответ для карточек, значит вопрос либо общий, либо она
    # ответила на автопилоте.
    raw_qf = result.get("quick_facts")
    quick_facts = []
    if isinstance(raw_qf, list):
        for item in raw_qf:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            label = str(item.get("label") or "").strip()
            options = item.get("options")
            if not (key and label and isinstance(options, list)):
                continue
            opts_clean = [str(o).strip() for o in options if str(o).strip()]
            if len(opts_clean) < 2:
                continue
            # гарантируем "не указано" как последний вариант
            if not any(o.lower().startswith("не указ") for o in opts_clean):
                opts_clean.append("не указано")
            qf = {
                "key": key,
                "label": label,
                "options": opts_clean,
                # allow_custom — UI должен показать поле «свой вариант» рядом с
                # выпадающим списком. На уровне детектора всегда true; если фронт
                # решит локально запретить — переопределит.
                "allow_custom": bool(item.get("allow_custom", True)),
                # multi_select — даёт юзеру выбрать несколько опций (для
                # differentiator-вопросов: у крема может быть и ретинол, и
                # витамин C одновременно). Default false — безопаснее, чтоб
                # старые модели/ответы не сломали single-select поведение.
                "multi_select": bool(item.get("multi_select", False)),
            }
            seen = str(item.get("seen") or "").strip()
            if seen:
                qf["seen"] = seen
            why = str(item.get("why_for_card") or "").strip()
            if why:
                qf["why_for_card"] = why
            cat = str(item.get("category") or "").strip().lower()
            # "spec" — основной класс по промпту; "hidden_number" — легаси-ответы
            # старых моделей (back-compat). Раньше промпт учил "spec", а валидатор
            # его дропал — категория вопроса молча терялась.
            if cat in ("spec", "tone", "differentiator", "hidden_number"):
                qf["category"] = cat
            quick_facts.append(qf)
    result["quick_facts"] = quick_facts

    # Поле suggested_questions оставляем как удобный массив строк-подсказок
    # для свободного текстового брифа (обратная совместимость + UX). Берём
    # из quick_facts.labels — никакого дублирования логики.
    result["suggested_questions"] = [qf["label"] for qf in quick_facts][:6]

    # Если модель забыла короткий заголовок — построим компактный из specific.
    if not result["product_title_short"] and result.get("search_query_specific"):
        result["product_title_short"] = result["search_query_specific"][:80]

    logger.info(
        f"detect_category: {result.get('category_wb')!r} | "
        f"title={result.get('product_title_short')!r} | "
        f"brand={result.get('brand_detected')!r}/{result.get('brand_is_product')} | "
        f"specific={result.get('search_query_specific')!r} | "
        f"broad={result.get('search_query_broad')!r} | "
        f"extracted_text={result.get('extracted_text')!r} | "
        f"uniqueness={result.get('_uniqueness_scan')!r} | "
        f"quick_facts={len(quick_facts)} | "
        f"confidence={result.get('confidence')} | "
        f"img={image_fingerprint[:24]}"
    )
    return result


def refine_search_query(
    title: str,
    brand: str,
    brand_is_important: bool,
    answers: dict,
    quick_facts: list,
    user_free_text: str,
    cfg: dict = None,
) -> dict:
    """
    LLM собирает естественный WB-запрос из контекста (бренд + ответы юзера
    + свободный текст). На фронт уходят {query_specific, query_broad}.

    answers: {fact_key: chosen_value}
    quick_facts: список из detect_category (для маппинга key→label).

    Гарантированно возвращает dict — при любой ошибке fallback на простой
    «title без бренда» (никаких 500-ок наружу).
    """
    import generator as G

    if cfg is None:
        cfg = G.load_config()

    # Сопоставляем key → label, чтобы отдать LLM человечные строки «label: value».
    # value может быть строкой (single-select) или списком строк (multi_select=true) —
    # для multi-select склеиваем через запятую, иначе строка прилетает в LLM как
    # «фичи: ['ретинол', 'витамин C']», что портит запрос.
    key_to_label = {f.get("key"): f.get("label") for f in (quick_facts or []) if isinstance(f, dict)}
    answers_lines = []
    for key, value in (answers or {}).items():
        if not value:
            continue
        if isinstance(value, list):
            cleaned = [str(v).strip() for v in value
                       if v and not str(v).strip().lower().startswith("не указ")]
            if not cleaned:
                continue
            value_str = ", ".join(cleaned)
        else:
            if str(value).lower().startswith("не указ"):
                continue
            value_str = str(value)
        label = key_to_label.get(key) or key
        answers_lines.append(f"{label}: {value_str}")

    user_prompt = P.build_refine_query_user_prompt(
        title=title or "",
        brand=brand or "",
        brand_is_important=bool(brand_is_important),
        answers_lines=answers_lines,
        user_free_text=(user_free_text or "").strip(),
    )

    text = None
    # OpenAI vision не нужен — это чисто текстовая задача. Дешёвая модель справится.
    try:
        text = G._claude_chat(
            cfg, P.REFINE_QUERY_SYSTEM_PROMPT, user_prompt,
            images=None, max_tokens=300, temperature=0.3,
            retries_per_model=1, dead_endpoint_threshold=1,
            purpose="refine_query",
        )
    except Exception as e:
        logger.warning(f"refine_search_query: OpenAI failed: {e}")

    if text is None:
        try:
            text = G._claude_chat(
                cfg, P.REFINE_QUERY_SYSTEM_PROMPT, user_prompt,
                images=None, max_tokens=300, temperature=0.3,
                retries_per_model=1, dead_endpoint_threshold=1,
                purpose="refine_query",
            )
        except Exception as e:
            logger.warning(f"refine_search_query: retry failed: {e}")

    parsed = None
    if text:
        try:
            parsed = P.extract_json(text)
        except Exception:
            parsed = None

    # Фоллбэк: title (с/без бренда) + категория. Чтобы поиск всегда что-то возвращал.
    fallback_specific = (title or "").strip()
    if brand and not brand_is_important and brand.lower() in fallback_specific.lower():
        # вырезаем бренд аккуратно по словам
        import re as _re
        pat = _re.compile(_re.escape(brand), _re.IGNORECASE)
        fallback_specific = pat.sub("", fallback_specific).strip()
        fallback_specific = _re.sub(r"\s+", " ", fallback_specific)

    q_specific = ""
    q_broad = ""
    if isinstance(parsed, dict):
        q_specific = str(parsed.get("query_specific") or "").strip()
        q_broad = str(parsed.get("query_broad") or "").strip()

    if not q_specific:
        q_specific = fallback_specific or (title or "")
    if not q_broad:
        # broad по умолчанию — последние 1-2 слова title без бренда
        words = [w for w in (fallback_specific or title or "").split()
                 if w.lower() != (brand or "").lower()]
        q_broad = " ".join(words[-2:]) if words else (title or "")

    logger.info(
        f"refine_search_query: title={title!r} brand={brand!r}/{brand_is_important} "
        f"answers={len(answers_lines)} → specific={q_specific!r} broad={q_broad!r}"
    )
    return {
        "query_specific": q_specific,
        "query_broad": q_broad,
    }


def _empty_category(error: str = "") -> dict:
    return {
        "category": "", "category_wb": "",
        "search_query_specific": "", "search_query_broad": "",
        "product_description": "",
        "product_title_short": "",
        "brand_detected": "",
        "brand_is_product": False,
        "quick_facts": [],
        "suggested_questions": [],
        "confidence": "low",
        "error": error,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Smart competitor search
# ─────────────────────────────────────────────────────────────────────────────

def smart_search_competitors(
    query_specific: str,
    query_broad: str,
    limit: int = 5,
    dest: str = DEFAULT_DEST,
    parser: "WBParser | None" = None,
) -> dict:
    """
    WBParser search with automatic broad-query fallback.

    Fallback triggers when specific query yields:
      - fewer than _MIN_PRODUCTS results, OR
      - average feedback count < _MIN_AVG_FEEDBACKS

    Если parser не передан — создаём локальный (back-compat без smart_client).
    В проде server.py передаёт singleton WBParser с подключённым WBSmartClient.

    Returns:
      products:      List[Product]
      query_used:    str
      fallback_used: bool
      raw_count:     int
      avg_feedbacks: float
      error:         str | None              # человекочитаемое описание
      error_type:    str | None              # "WB_UNAVAILABLE" | None — для UI
    """
    if parser is None:
        parser = WBParser(dest=dest, photo_limit=0)

    def _run(query: str, is_fallback: bool) -> dict:
        try:
            raw = parser.search(query, limit=limit)
        except RuntimeError as e:
            logger.warning(f"smart_search: WB error ({query!r}): {e}")
            return {"raw": [], "avg_feedbacks": 0.0, "enough": False,
                    "query_used": query, "fallback_used": is_fallback, "error": str(e)}
        fbs = sorted(r.get("feedbacks") or 0 for r in raw)
        avg_fb = (sum(fbs) / len(fbs)) if fbs else 0.0
        enough = len(raw) >= min(_MIN_PRODUCTS, limit)
        return {"raw": raw, "avg_feedbacks": avg_fb, "enough": enough,
                "query_used": query, "fallback_used": is_fallback, "error": None}

    result = _run(query_specific, is_fallback=False)

    # Broad-фолбэк ДОБИРАЕТ выдачу, а НЕ заменяет её. Раньше «мало отзывов у
    # specific» (порог median_fb) выбрасывал РЕЛЕВАНТНЫЕ результаты целиком и
    # брал топ-популярное по общему слову: для «игрушка робот-курьер» (3 нишевых
    # робота, avg_fb=7.7) конкурентами становились перчатка Человека-паука и
    # пингвин из broad «игрушка» — анализ строился на мусоре. Нишевые товары
    # легитимно малоотзывны; релевантность важнее популярности.
    need_fallback = (
        not result["enough"]
        and query_broad
        and query_broad.strip()
        and query_broad.strip().lower() != query_specific.strip().lower()
    )
    if need_fallback:
        logger.info(
            f"smart_search: specific={query_specific!r} → "
            f"{len(result['raw'])} результатов (< {min(_MIN_PRODUCTS, limit)}) — "
            f"ДОБИРАЮ из broad={query_broad!r} (specific остаётся первым)"
        )
        fb = _run(query_broad, is_fallback=True)
        if fb["raw"]:
            seen_ids = {r.get("nm_id") for r in result["raw"]}
            merged = list(result["raw"])
            for r in fb["raw"]:
                if r.get("nm_id") not in seen_ids:
                    merged.append(r)
                    seen_ids.add(r.get("nm_id"))
            result = {
                "raw": merged,
                "avg_feedbacks": result["avg_feedbacks"] or fb["avg_feedbacks"],
                "enough": len(merged) >= min(_MIN_PRODUCTS, limit),
                # query_used — specific, если он дал хоть что-то (это ЕГО выдача первая)
                "query_used": result["query_used"] if result["raw"] else fb["query_used"],
                "fallback_used": True,
                "error": result.get("error") if not merged else None,
            }

    raw_items = result["raw"] or []
    logger.info(f"smart_search: enriching {len(raw_items)} products…")

    # Каждый enrich = N HTTP-запросов к WB (карточка + до 30 HEAD на фото
    # из probe_photo_count). Раньше шло последовательно и без общего лимита —
    # если WB зажимал одну карточку, висел весь поиск.
    #
    # Сейчас: параллелим в пуле, ставим жёсткий общий timeout и НЕ ждём
    # зависшие futures на выходе (shutdown(wait=False, cancel_futures=True)).
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import concurrent.futures as _cf

    products: list[Product] = []
    items = raw_items[:limit]
    if items:
        ENRICH_TIMEOUT_TOTAL = 20.0   # жёсткий бюджет на всю фазу
        t0 = time.monotonic()
        ex = ThreadPoolExecutor(max_workers=max(2, len(items)))
        futures = {ex.submit(parser.enrich, base): base for base in items}
        try:
            for fut in as_completed(futures, timeout=ENRICH_TIMEOUT_TOTAL):
                base = futures[fut]
                try:
                    products.append(fut.result(timeout=1))
                except Exception as e:
                    logger.warning(f"smart_search: enrich nm={base.get('nm_id')}: {e}")
        except _cf.TimeoutError:
            done_count = len(products)
            stragglers = [futures[f].get('nm_id') for f in futures if not f.done()]
            logger.warning(
                f"smart_search: enrich timeout {ENRICH_TIMEOUT_TOTAL}s — "
                f"возвращаю {done_count}/{len(items)} (зависли: {stragglers})"
            )
        finally:
            # ВАЖНО: shutdown(wait=False) + cancel_futures=True — НЕ ждём зависшие
            # HTTP-вызовы на выходе. Иначе функция могла бы спокойно занять
            # 70+ секунд вместо обещанных 20.
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # Python < 3.9 — пробуем без cancel_futures
                ex.shutdown(wait=False)
        elapsed = time.monotonic() - t0
        logger.info(f"smart_search: enriched {len(products)}/{len(items)} за {elapsed:.1f}s")
        products.sort(key=lambda p: getattr(p, "feedbacks", 0) or 0, reverse=True)

    logger.info(f"smart_search: {len(products)} competitors ready")
    err = result.get("error")
    error_type = None
    if err and not products:
        # явный фейл WB — UI покажет модалку "продолжить без WB?"
        error_type = "WB_UNAVAILABLE"
    elif not products:
        # запрос отработал но без выдачи (редко) — это не сбой WB
        error_type = "NO_RESULTS"
    return {
        "products": products,
        "query_used": result["query_used"],
        "fallback_used": result["fallback_used"],
        "raw_count": len(raw_items),
        "avg_feedbacks": result["avg_feedbacks"],
        "error": err,
        "error_type": error_type,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Competitor analysis — two-phase (per-competitor visual + synthesis)
# ─────────────────────────────────────────────────────────────────────────────

# ── 3a: Photo fetching ───────────────────────────────────────────────────────

def _fetch_photo_uri(url: str, timeout: int = 12) -> Optional[str]:
    """Fetch one photo URL → base64 data URI. Returns None on any error."""
    try:
        r = _req.get(url, timeout=(5, timeout), headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.wildberries.ru/",
            "Accept": "image/webp,image/*,*/*",
        })
        if r.status_code == 200 and r.content:
            mime = "webp" if url.endswith(".webp") else "jpeg"
            b64 = base64.b64encode(r.content).decode("ascii")
            return f"data:image/{mime};base64,{b64}"
    except Exception as e:
        logger.debug(f"_fetch_photo_uri: {url[:60]}… → {e}")
    return None


_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)


def _slug(s: str, maxlen: int = 40) -> str:
    """Filesystem-safe slug from a Russian/English string."""
    s = (s or "").strip().lower()
    s = s.replace(" ", "_")
    s = _SLUG_RE.sub("", s)
    return (s or "item")[:maxlen]


def _save_photo_from_uri(uri: str, dest_path: str) -> Optional[str]:
    """Decode base64 data-URI and write bytes to dest_path. Returns dest_path on success."""
    if not uri or not uri.startswith("data:"):
        return None
    try:
        head, _, payload = uri.partition(",")
        if not payload:
            return None
        data = base64.b64decode(payload)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return dest_path
    except Exception as e:
        logger.debug(f"_save_photo_from_uri: {dest_path} → {e}")
        return None


def _download_competitor_photos(
    product: Product,
    n_photos: int,
) -> list[dict]:
    """
    Download up to `n_photos` photos for ONE competitor in parallel.

    Photo ordering is preserved — index 1 is the main/hero card,
    index 2+ are detail/feature cards.

    Returns list of {"idx": int, "label": str, "image": data_uri} dicts,
    ordered by photo index. Skips failed downloads silently.
    """
    urls = product.photos[:n_photos]
    if not urls:
        return []

    # Submit all downloads in parallel
    with ThreadPoolExecutor(max_workers=min(8, len(urls)), thread_name_prefix="wp") as pool:
        fut_map = {pool.submit(_fetch_photo_uri, url): (i + 1, url)
                   for i, url in enumerate(urls)}
        results: dict[int, str] = {}
        for fut in as_completed(fut_map):
            idx, _ = fut_map[fut]
            try:
                uri = fut.result()
                if uri:
                    results[idx] = uri
            except Exception:
                pass

    # Return in original order, with role labels
    out = []
    for idx in range(1, len(urls) + 1):
        uri = results.get(idx)
        if uri is not None:
            role = "MAIN_CARD" if idx == 1 else f"DETAIL_CARD_{idx}"
            out.append({"idx": idx, "label": role, "image": uri})

    logger.debug(f"competitor nm={product.nm_id}: {len(out)}/{len(urls)} photos fetched")
    return out


# ── 3b: Batch formation ──────────────────────────────────────────────────────

def _form_batches(
    competitor_photos: list[tuple[Product, list[dict]]],
    min_own: int = MIN_PHOTOS_OWN_BATCH,
) -> list[list[tuple[Product, list[dict]]]]:
    """
    Group competitors into batches for Claude calls.

    Rule:
      - Competitor with >= min_own photos → own batch.
      - Competitor with  < min_own photos → merged with the NEXT competitor
        (keep merging until merged batch reaches min_own total photos
        or no more competitors to merge with).

    This guarantees each Claude call has at least min_own images (except
    possibly the very last batch if remaining competitors are all photo-poor).
    """
    batches: list[list[tuple[Product, list[dict]]]] = []
    pending: list[tuple[Product, list[dict]]] = []
    pending_photo_count = 0

    for comp, photos in competitor_photos:
        pending.append((comp, photos))
        pending_photo_count += len(photos)

        if len(photos) >= min_own:
            # This competitor has enough photos for own batch.
            # Flush any previously accumulated under-threshold competitors too.
            batches.append(list(pending))
            pending = []
            pending_photo_count = 0
        elif pending_photo_count >= min_own:
            # Merged batch reached threshold — flush.
            batches.append(list(pending))
            pending = []
            pending_photo_count = 0
        # else: continue accumulating

    # Flush remainder (may be < min_own if all remaining have few photos)
    if pending:
        if batches:
            # Merge remainder into last batch rather than creating a tiny batch
            batches[-1].extend(pending)
        else:
            batches.append(pending)

    return batches


# ── 3c: Per-batch visual analysis ────────────────────────────────────────────

_VISUAL_ANALYSIS_SYSTEM = """\
Ты — эксперт по визуальному дизайну маркетплейс-карточек. Получаешь ФОТОГРАФИИ карточек \
конкурентов с Wildberries. Проведи визуальный анализ.

🇷🇺 ЯЗЫК: ВСЕ значения в JSON — ИСКЛЮЧИТЕЛЬНО на русском языке. Никаких английских \
слов и калек («premium», «clean», «minimal», «lifestyle»). Пиши по-русски: «премиальный \
тёмный», «чистый минимализм», «лайфстайл-сцена», «дёшево и пёстро». Имена полей (ключи \
JSON) оставляй английскими, но ТЕКСТ внутри — только русский.

ВАЖНО О ПОРЯДКЕ ФОТО:
- Фото с меткой MAIN_CARD — это ПЕРВАЯ (лицевая) карточка. Самая главная: именно её видит \
покупатель в поиске. Уделяй ей наибольшее внимание.
- Фото с метками DETAIL_CARD_2, DETAIL_CARD_3 и т.д. — дополнительные карточки: \
характеристики, детали, лайфстайл, сравнение.

Если в батче несколько конкурентов — анализируй каждого ОТДЕЛЬНО, чётко маркируя comp_id.

Отвечай СТРОГО валидным JSON без markdown:
{
  "competitors_visual": [
    {
      "comp_id": "<comp1 / comp2 / ...>",
      "main_card_style": "<стиль первой карточки: фон, свет, расположение товара, текст — по-русски>",
      "color_palette": "<доминирующие цвета по-русски: тёмно-синий, графит, тёплый бежевый>",
      "typography": "<характер шрифтов по-русски: жирный/тонкий, с/без засечек, размер заголовка>",
      "badge_style": "<форма, цвет, тип бейджей — по-русски>",
      "detail_cards_approach": "<что показывают на карточках 2-N: характеристики/диаграммы/лайфстайл/сравнение>",
      "overall_impression": "<3-5 слов по-русски: премиальный тёмный / яркий минимализм / дёшево и пёстро / чисто и профессионально>",
      "strengths": "<что визуально сделано хорошо — по-русски>",
      "weaknesses": "<что визуально слабо или отсутствует — по-русски>"
    }
  ]
}
"""


def _analyze_batch_visuals(
    batch: list[tuple[Product, list[dict]]],
    cfg: dict,
) -> list[dict]:
    """
    One Claude vision call for a batch of competitors.
    Returns list of per-competitor visual analysis dicts.
    """
    import generator as G

    # Build image list with clear competitor+role labels
    images = []
    comp_descriptions = []

    for batch_idx, (comp, photos) in enumerate(batch, 1):
        cid = f"comp{batch_idx}"
        comp_descriptions.append(
            f"{cid}: «{comp.name}» (бренд: {comp.brand or '—'}, "
            f"цена: {f'{comp.price:.0f} ₽' if comp.price else '—'}, "
            f"{comp.feedbacks or 0} отзывов)"
        )
        for p in photos:
            # MAIN_CARD (idx=1) — лицевая в выдаче WB: мелкий текст плашек, цена,
            # бейджи, шрифт заголовка. Туда стоит high — там и считывается основной
            # «характер» бренда. DETAIL_CARD_N — характеристики/лайфстайл/сравнение:
            # модель смотрит общий стиль, палитру, композицию — для этого low (85
            # токенов/фото) хватает, OCR детальных подписей не нужен. Экономия ~10×
            # на батч из 6 фото при том же качестве main_card_style/color_palette/
            # typography/overall_impression. Сами поля JSON в _VISUAL_ANALYSIS_SYSTEM
            # — про впечатление, не про текст.
            is_main = p.get("idx") == 1
            images.append({
                "name": f"{cid}_{p['label']}",
                "image": p["image"],
                "detail": "high" if is_main else "low",
            })

    n_comps = len(batch)
    n_photos = len(images)
    user_prompt = (
        f"Проанализируй визуальный стиль {n_comps} конкурентов.\n\n"
        f"Конкуренты в этом батче:\n" +
        "\n".join(f"  • {d}" for d in comp_descriptions) +
        f"\n\nВсего фото: {n_photos}. "
        f"Метки фото: cid_MAIN_CARD = первая/лицевая карточка; cid_DETAIL_CARD_N = карточки 2-N.\n"
        f"Верни competitors_visual[] с записью для каждого comp_id из списка выше."
    )

    # Локальный кеш по hash(images+prompt+detail+version). Если те же конкуренты
    # анализировались раньше и WB не сменил фотки — мгновенный возврат без
    # обращения к OpenAI. На промахе работает прежний путь, ноль изменений.
    cache_key = _vision_cache_key(images, _VISUAL_ANALYSIS_SYSTEM, user_prompt)
    cached = _vision_cache_get(cache_key)
    if cached is not None:
        logger.info(
            f"_analyze_batch_visuals: cache HIT (key={cache_key[:12]}…, "
            f"{n_comps} comps, {n_photos} photos)"
        )
        return cached

    # Try Claude, then GPT fallback
    for source, retries in [("attempt-1", 1), ("attempt-2", 2)]:
        try:
            text = G._claude_chat(
                cfg, _VISUAL_ANALYSIS_SYSTEM, user_prompt,
                images=images, max_tokens=1500, temperature=0.2,
                purpose="visual_analysis",
                retries_per_model=retries, dead_endpoint_threshold=1,
            )
            data = P.extract_json(text)
            results = data.get("competitors_visual") or []
            if results:
                _vision_cache_put(cache_key, results)
            return results
        except Exception as e:
            logger.warning(f"_analyze_batch_visuals: {source} failed: {e}")
    return []


# ── 3d: Strategic synthesis ──────────────────────────────────────────────────

def _synthesize_analysis(
    products: list[Product],
    visual_analyses: list[dict],
    cfg: dict,
    card_count: int = 6,
    user_facts: Optional[dict] = None,
    user_brief: str = "",
    user_title: str = "",
    user_category: str = "",
) -> dict:
    """
    Final Claude text-only call: combines competitor text data + visual analyses
    into full strategic competitor intelligence.

    card_count — сколько карточек пользователь будет генерировать. Передаётся
    Claude и используется при нормализации recommended_card_plan, чтобы план
    в UI совпадал с реальным количеством рендеров (раньше был баг: UI показывал
    «План: 5», но рендерилось 6 — Claude дефолтил 1+3+1+0=5).
    """
    import generator as G

    def _clean(s, limit=400):
        """Strip control chars and truncate so WB texts don't break Claude's JSON."""
        import re as _re
        s = _re.sub(r"[\x00-\x1f\x7f]", " ", str(s or ""))
        s = s.replace('"', "'").strip()
        return s[:limit] + ("…" if len(s) > limit else "")

    # Build enriched competitor descriptions
    lines = []
    # Блок ТОВАР КЛИЕНТА — даёт модели опорные факты для card_recipe. Без него
    # модель брала «типичные для ниши» цифры и подставляла «4.3 л / 36 мес»
    # к товару, у которого юзер чётко указал «5 л».
    user_block = []
    if user_title:
        user_block.append(f"Название: {user_title}")
    if user_category:
        user_block.append(f"Категория: {user_category}")
    if isinstance(user_facts, dict) and user_facts:
        kvs = []
        for k, v in user_facts.items():
            if v is None or v == "":
                continue
            kvs.append(f"  • {k}: {v}")
        if kvs:
            user_block.append("Подтверждённые факты:")
            user_block.extend(kvs)
    if user_brief and len(user_brief) > 0:
        # бриф может содержать дубликаты labels из facts — отдаём как есть,
        # дизайн-промпт сам нормализует extract_facts_from_brief'ом.
        user_block.append(f"Свободный текст пользователя: {user_brief[:600]}")
    if user_block:
        lines.append("═══ ТОВАР КЛИЕНТА (источник истины для card_recipe) ═══")
        lines.extend(user_block)
        lines.append("")
        lines.append(
            "🚫 В card_recipe и copy_formulas используй ТОЛЬКО эти цифры/факты. "
            "Не выдумывай объём, срок годности, мощность, состав, проценты — если их "
            "нет в подтверждённых фактах. Лучше пропусти бейдж, чем подставь чужое число."
        )
        lines.append("")
    lines.append("Топ конкурентов с Wildberries — текстовые данные + визуальный анализ:\n")
    for i, p in enumerate(products, 1):
        price = f"{p.price:.0f} ₽" if p.price else "—"
        rating = f"★{p.rating}" if p.rating else "—"
        feedbacks = f"{p.feedbacks} отзывов" if p.feedbacks else "0 отзывов"
        chars_txt = "; ".join(
            f"{_clean(c['name'], 40)}: {_clean(c['value'], 60)}"
            for c in p.characteristics[:5]
        )
        desc_txt = _clean(p.description, 300)

        visual = next((v for v in visual_analyses if v.get("comp_id") == f"comp{i}"), {})

        lines.append(f"═══ КОНКУРЕНТ {i} ═══")
        lines.append(f"Название: {p.name}")
        lines.append(f"Бренд: {p.brand or '—'} | Поставщик: {p.supplier or '—'}")
        lines.append(f"Цена: {price} | {rating} | {feedbacks}")
        lines.append(f"URL: {p.url}")
        lines.append(f"Кол-во фото: {p.photo_count}")
        if chars_txt:
            lines.append(f"Характеристики: {chars_txt}")
        if desc_txt:
            lines.append(f"Описание: {desc_txt}")
        if visual:
            lines.append(f"Визуал — первая карточка: {visual.get('main_card_style', '—')}")
            lines.append(f"Визуал — палитра: {visual.get('color_palette', '—')}")
            lines.append(f"Визуал — типографика: {visual.get('typography', '—')}")
            lines.append(f"Визуал — карточки 2-N: {visual.get('detail_cards_approach', '—')}")
            lines.append(f"Визуал — впечатление: {visual.get('overall_impression', '—')}")
            lines.append(f"Сильные стороны: {visual.get('strengths', '—')}")
            lines.append(f"Слабые стороны: {visual.get('weaknesses', '—')}")
        lines.append("")

    user_prompt = "\n".join(lines)
    user_prompt += (
        f"\nПОЛЬЗОВАТЕЛЬ ПЛАНИРУЕТ {card_count} КАРТОЧЕК — твой recommended_card_plan "
        f"должен суммироваться РОВНО в {card_count} (main + feature + lifestyle + comparison). "
        f"Обычно main=1, остальное распредели по фишкам товара: больше feature если фишек много, "
        f"добавь lifestyle если нужна сцена, добавь comparison если есть «до/после».\n"
        "Составь рецепт карточек и верни СТРОГО валидный JSON без markdown. "
        "Все строки — одна строка, без переносов внутри кавычек, максимум 120 символов на значение. "
        "Не забывай запятые между полями. "
        "НИКОГДА не используй обратный слэш перед кавычкой внутри значений — "
        "если в тексте есть кавычки или дюймы, замени на одинарные ' или слово."
    )

    analysis = None

    # 1) Try Claude ONCE (fast fail)
    text = ""
    try:
        text = G._claude_chat(
            cfg, P.COMPETITOR_ANALYSIS_SYSTEM_PROMPT, user_prompt,
            images=None, max_tokens=4000, temperature=0.15,
            retries_per_model=1, dead_endpoint_threshold=1,
            purpose="competitor_synth",
        )
        analysis = P.extract_json(text)
    except Exception as e:
        logger.warning("_synthesize_analysis: Claude failed: %s (response %d chars)", e, len(text))

    # 2) Первый заход не дал валидный JSON → второй проход ТОЙ ЖЕ цепочки
    #    endpoint×model, но с полными ретраями (retries_per_model по умолчанию).
    if analysis is None:
        logger.info("_synthesize_analysis: attempt 1 JSON failed — retrying chain with full retries")
        try:
            text = G._claude_chat(
                cfg,
                P.COMPETITOR_ANALYSIS_SYSTEM_PROMPT,
                user_prompt,
                images=None,
                max_tokens=4000,
                temperature=0.15,
                purpose="competitor_synth",
            )
            analysis = P.extract_json(text)
        except Exception as e:
            logger.error(f"_synthesize_analysis: OpenAI fallback also failed: {e}")
            return {**_empty_analysis(), "error": str(e)}

    # Normalize recommended_card_plan чтобы сумма = card_count (раньше был баг:
    # план показывал 5, рендерилось 6, потому что Claude дефолтил 1+3+1+0=5).
    plan = analysis.get("recommended_card_plan") or {}
    for t in ("main", "feature", "lifestyle", "comparison"):
        plan.setdefault(t, 0)
    plan["main"] = max(plan.get("main", 0), 1)  # main всегда 1
    plan["feature"] = max(plan.get("feature", 0), 2)
    plan["lifestyle"] = max(plan.get("lifestyle", 0), 1)

    current_sum = plan["main"] + plan["feature"] + plan["lifestyle"] + plan["comparison"]
    if current_sum < card_count:
        # Не хватает — добавляем feature и/или comparison чтобы сумма совпала
        deficit = card_count - current_sum
        # Сначала добавляем feature (универсальный info-кадр)
        add_feature = min(deficit, 2)
        plan["feature"] += add_feature
        deficit -= add_feature
        if deficit > 0:
            plan["comparison"] += deficit
    elif current_sum > card_count:
        # Перебор — режем comparison/lifestyle/feature по очереди
        excess = current_sum - card_count
        for t in ("comparison", "lifestyle", "feature"):
            if excess <= 0:
                break
            can_cut = max(0, plan[t] - (1 if t == "feature" else 0))
            cut = min(excess, can_cut)
            plan[t] -= cut
            excess -= cut

    analysis["recommended_card_plan"] = plan

    # Attach raw competitor summaries + visual analyses for display
    analysis["competitors_summary"] = [
        {
            "name": p.name, "brand": p.brand,
            "price": p.price, "rating": p.rating,
            "feedbacks": p.feedbacks, "url": p.url,
            "photo_count": p.photo_count,
            "visual": next((v for v in visual_analyses
                            if v.get("comp_id") == f"comp{i}"), {}),
        }
        for i, p in enumerate(products, 1)
    ]
    analysis["visual_analyses"] = visual_analyses

    return analysis


# ── 3 public entry point ─────────────────────────────────────────────────────

def analyze_competitors(
    products: list[Product],
    cfg: dict,
    card_count: int = 6,
    include_photos: bool = True,
    save_photos_dir: Optional[str] = None,
    user_facts: Optional[dict] = None,
    user_brief: str = "",
    user_title: str = "",
    user_category: str = "",
) -> dict:
    """
    Two-phase competitor analysis.

    Args:
        products:       Enriched Product list from smart_search_competitors().
        cfg:            Config dict.
        card_count:     How many cards user wants to generate — used as
                        photos_per_competitor (so Claude sees as many competitor
                        images as user plans to create cards).
        include_photos: If False, skip photo download and do text-only analysis.
        save_photos_dir: If set, save first photo of each competitor as JPEG to
                        this directory. Saved paths appear in the returned
                        analysis under competitors_summary[i].photo_path / photo_paths.

    Returns full analysis dict per COMPETITOR_ANALYSIS_SYSTEM_PROMPT schema,
    plus "competitors_summary" and "visual_analyses" keys for UI display.
    """
    if not products:
        return _empty_analysis()

    visual_analyses: list[dict] = []
    saved_paths: dict[int, list[str]] = {}

    # ── Phase A: per-competitor visual analysis ───────────────────────────────
    if include_photos:
        t0 = time.monotonic()

        # 1. Download photos for all competitors (parallel per-competitor)
        n_photos = max(1, card_count)
        logger.info(f"analyze_competitors: downloading up to {n_photos} photos × {len(products)} competitors…")

        comp_photo_list: list[tuple[Product, list[dict]]] = []
        with ThreadPoolExecutor(max_workers=len(products), thread_name_prefix="cpdl") as pool:
            fut_map = {
                pool.submit(_download_competitor_photos, p, n_photos): p
                for p in products
            }
            # Preserve competitor ordering
            results_by_comp: dict[int, list[dict]] = {}
            for fut in as_completed(fut_map):
                comp = fut_map[fut]
                idx = products.index(comp)
                try:
                    results_by_comp[idx] = fut.result()
                except Exception as e:
                    logger.warning(f"analyze_competitors: photo download for nm={comp.nm_id}: {e}")
                    results_by_comp[idx] = []

        for i, p in enumerate(products):
            comp_photo_list.append((p, results_by_comp.get(i, [])))

        total_fetched = sum(len(ph) for _, ph in comp_photo_list)
        logger.info(
            f"analyze_competitors: {total_fetched} photos fetched in "
            f"{time.monotonic() - t0:.1f}s"
        )

        # 1b. Persist photos to disk (so UI can show them by path)
        if save_photos_dir:
            for i, (comp, photos) in enumerate(comp_photo_list):
                paths = []
                slug = _slug(f"{comp.nm_id}_{comp.name}", maxlen=40)
                for ph in photos:
                    idx = ph.get("idx", 1)
                    uri = ph.get("image", "")
                    # WB uses .webp, but we save under .jpg for browser compat
                    ext = ".webp" if "image/webp" in uri[:30] else ".jpg"
                    fname = f"{i+1:02d}__{slug}__p{idx}{ext}"
                    out = os.path.join(save_photos_dir, fname)
                    if _save_photo_from_uri(uri, out):
                        paths.append(out)
                saved_paths[i] = paths
            saved_count = sum(len(v) for v in saved_paths.values())
            logger.info(f"analyze_competitors: saved {saved_count} photos to {save_photos_dir}")

        # 2. Form batches.
        # Visual-view: для LLM шлём только MAIN + первые DETAIL_CARDs (cap
        # MAX_PHOTOS_FOR_VISUAL). Полный comp_photo_list по-прежнему ушёл на
        # диск выше → UI видит все скачанные фото, не теряем display. Это
        # разделение «что показывает UI» vs «что видит модель»: после 3-й
        # фотки модель и так считывает повторение фирстиля, поля JSON
        # (стиль/палитра/типографика/впечатление) не выигрывают от 6-й фотки.
        comp_visual_list = [
            (comp, photos[:MAX_PHOTOS_FOR_VISUAL])
            for comp, photos in comp_photo_list
        ]
        batches = _form_batches(comp_visual_list, min_own=MIN_PHOTOS_OWN_BATCH)
        logger.info(
            f"analyze_competitors: {len(batches)} visual-analysis batch(es) "
            f"(MIN_PHOTOS_OWN_BATCH={MIN_PHOTOS_OWN_BATCH}, "
            f"MAX_PHOTOS_FOR_VISUAL={MAX_PHOTOS_FOR_VISUAL}): "
            + ", ".join(
                f"batch{i+1}={len(b)} comps × ~{sum(len(ph) for _,ph in b)} photos"
                for i, b in enumerate(batches)
            )
        )

        # 3. Run per-batch Claude vision calls in parallel
        all_visual: dict[int, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=len(batches), thread_name_prefix="cvan") as pool:
            fut_map2 = {
                pool.submit(_analyze_batch_visuals, batch, cfg): (bi, batch)
                for bi, batch in enumerate(batches)
            }
            for fut in as_completed(fut_map2):
                bi, batch = fut_map2[fut]
                try:
                    results = fut.result()
                    # Re-label comp_ids to global competitor indices
                    # batch_idx=1 in batch bi maps to overall competitor position
                    offset = sum(len(batches[j]) for j in range(bi))
                    for local_idx, visual in enumerate(results, 1):
                        global_comp_id = f"comp{offset + local_idx}"
                        visual["comp_id"] = global_comp_id
                    all_visual[bi] = results
                except Exception as e:
                    logger.error(f"analyze_competitors: batch {bi} visual analysis failed: {e}")
                    all_visual[bi] = []

        # Flatten in batch order
        for bi in range(len(batches)):
            visual_analyses.extend(all_visual.get(bi, []))

        logger.info(
            f"analyze_competitors: visual analysis done, "
            f"{len(visual_analyses)} competitor profiles"
        )

    # ── Phase B: strategic synthesis ─────────────────────────────────────────
    # card_count прокидываем чтобы план карточек в UI совпал с реальным числом
    # карточек, которое будет сгенерировано (раньше план показывал 5, а делалось 6).
    analysis = _synthesize_analysis(
        products, visual_analyses, cfg, card_count=card_count,
        user_facts=user_facts, user_brief=user_brief,
        user_title=user_title, user_category=user_category,
    )

    # Attach disk paths of saved competitor photos to summary entries
    summaries = analysis.get("competitors_summary") or []
    for i, summary in enumerate(summaries):
        paths = saved_paths.get(i, [])
        summary["photo_paths"] = paths
        summary["photo_path"] = paths[0] if paths else ""

    return analysis


def _empty_analysis() -> dict:
    return {
        "card_recipe": [],
        "visual_dna": {
            "background": "",
            "hero_angle": "",
            "color_mood": "",
            "typography_feel": "",
        },
        "top_selling_angles": [],
        "gaps_to_exploit": [],
        "copy_formulas": [],
        "recommended_card_plan": {"main": 1, "feature": 3, "lifestyle": 1, "comparison": 0},
        "competitors_summary": [],
        "visual_analyses": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Display helper
# ─────────────────────────────────────────────────────────────────────────────

def format_analysis_for_display(analysis: dict) -> str:
    """Markdown summary for the UI collapsible block."""
    if not analysis:
        return ""

    lines = []

    recipe = analysis.get("card_recipe") or []
    if recipe:
        lines.append("**📋 Рецепт карточек (из анализа конкурентов):**")
        type_icon = {"main": "🖼", "feature": "📊", "lifestyle": "🌿", "comparison": "⚖️",
                     "benefits": "✅"}
        type_ru = {"main": "Главная", "feature": "Инфографика", "lifestyle": "Лайфстайл",
                   "comparison": "Сравнение", "benefits": "Выгоды"}
        for card in recipe:
            ctype = card.get("type", "")
            icon = type_icon.get(ctype, "•")
            # принимаем и новые (_ru), и старые имена полей — на случай старых ответов модели
            label = card.get("type_ru") or type_ru.get(ctype, ctype)
            headline = card.get("headline_ru") or card.get("headline")
            sub = card.get("sub_ru") or card.get("sub")
            badges = card.get("badges_ru") or card.get("badges")
            lines.append(f"\n**{card.get('n','?')}. {icon} {label}**")
            if card.get("shot"):
                lines.append(f"  📷 *{card['shot']}*")
            if headline:
                lines.append(f"  Заголовок: **{headline}**")
            if sub:
                lines.append(f"  Подзаголовок: {sub}")
            if badges:
                lines.append(f"  Бейджи: {' | '.join(badges)}")
            if card.get("why"):
                lines.append(f"  ↳ {card['why']}")
        lines.append("")

    vdna = analysis.get("visual_dna") or {}
    if any(vdna.values()):
        lines.append("**🎨 Визуальный ДНК конкурентов:**")
        if vdna.get("background"):
            lines.append(f"  Фон: {vdna['background']}")
        if vdna.get("hero_angle"):
            lines.append(f"  Ракурс: {vdna['hero_angle']}")
        if vdna.get("color_mood"):
            lines.append(f"  Цвет: {vdna['color_mood']}")
        if vdna.get("typography_feel"):
            lines.append(f"  Типографика: {vdna['typography_feel']}")
        lines.append("")

    angles = analysis.get("top_selling_angles") or []
    if angles:
        lines.append("**🎯 Главные углы продаж:**")
        for a in angles:
            lines.append(f"  • {a}")
        lines.append("")

    gaps = analysis.get("gaps_to_exploit") or []
    if gaps:
        lines.append("**🏆 Наш шанс (чего нет у конкурентов):**")
        for g in gaps:
            lines.append(f"  ✅ {g}")
        lines.append("")

    formulas = analysis.get("copy_formulas") or []
    if formulas:
        lines.append("**✍️ Формулы заголовков:**")
        for f in formulas:
            lines.append(f"  • {f}")
        lines.append("")

    plan = analysis.get("recommended_card_plan") or {}
    if plan:
        total = sum(plan.get(t, 0) for t in ("main", "feature", "lifestyle", "comparison"))
        lines.append(
            f"**Рекомендованный план:** "
            f"Главных: {plan.get('main',0)} | "
            f"Инфографика: {plan.get('feature',0)} | "
            f"Лайфстайл: {plan.get('lifestyle',0)} | "
            f"Сравнение: {plan.get('comparison',0)} | "
            f"**Итого: {total}**\n"
        )

    summaries = analysis.get("competitors_summary") or []
    if summaries:
        lines.append("**Топ конкурентов:**")
        for i, c in enumerate(summaries, 1):
            price = f"{c['price']:.0f} ₽" if c.get("price") else "—"
            rating = f"★{c['rating']}" if c.get("rating") else "—"
            fb = f"{c['feedbacks']} отз." if c.get("feedbacks") else "0 отз."
            lines.append(f"  {i}. [{c.get('name','')}]({c.get('url','')}) — {price} {rating} {fb}")
            vis = c.get("visual") or {}
            if vis.get("overall_impression"):
                lines.append(f"     Стиль: *{vis['overall_impression']}*")
        lines.append("")

    return "\n".join(lines)
