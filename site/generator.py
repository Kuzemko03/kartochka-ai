"""Orchestrator for WBnator. Streaming-first: generator entry points YIELD partial
results so the UI can show cards / videos / description as soon as each finishes,
without waiting for the whole pack.

═══════════════════════════════════════════════════════════════════════════════
PUBLIC API CONTRACT (read this if wiring a UI):

  Streaming (preferred — UI updates incrementally):
    generate_pack_streaming(...) -> Generator yielding {files, videos, description, log, run_dir, info}
    generate_pack_brief_streaming(...) -> same shape

  Blocking (single result, simple wrappers around streaming):
    generate_pack(...)            -> dict (last yield from streaming)
    generate_pack_brief(...)      -> dict
    generate_style_variations(...)-> dict
    generate_description(...)     -> str

  Standalone:
    generate_videos_for_run(run_dir, ...) -> dict — animate cards from a previous run
    generate_videos_for_files(files, ...) -> dict

  All streaming yields:
    {
      "files":       list[str]   — finalized cards available so far
      "videos":      list[str]   — finalized .mp4 paths so far
      "description": str         — marketplace description (filled when ready)
      "log":         list[str]   — accumulated log lines
      "info":        str         — short status message
      "run_dir":     str         — absolute path
      "design":      dict | None — Claude's design plan
      "inferred_product": dict | None  — for brief mode
      "done":        bool        — True only on the final yield
    }

  Knobs:
    ref_paths           : list of product photo paths (multi-ref → image API blends)
    style_ref_paths     : list of CARD reference images (style guide).
                          → goes to Claude vision ONLY (not image API)
                          → when present, OVERRIDES the style_key picker
    write_description   : also calls Claude for a marketplace description (700-1000 chars)
    generate_videos     : start image-to-video for each card AS SOON AS the image is ready
    image_provider      : "veononstop" / "fastgen_nanopro" / "fastgen_gemini" / "fastgen_imagen" / "fastgen_flower" / "fastgen_grok"
    video_provider      : "fastgen_flower" (Veo 3.1, default) / "fastgen_flow" / "fastgen_gemini"
                          / "fastgen_whisk" / "fastgen_grok" / "veononstop"
═══════════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import time
import base64
import re
import uuid
import logging
import threading
import urllib3
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime

import formats as fmt
import prompts as P
import overlay
from fastgen_client import FastGenClient        # legacy v4 (kept for rollback)
from fastgen_v5_client import FastGenV5Client    # V5 image API (active)
from fastgen_video import FastGenVideoClient
from openai_image_client import OpenAIImageClient  # прямой gpt-image-2 фолбэк

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ─── Logging hygiene ────────────────────────────────────────────────────────
class _StripBase64(logging.Filter):
    _DURI = re.compile(r"data:image/[^,\s]+,[A-Za-z0-9+/=]+")
    _LONG_B64 = re.compile(r"[A-Za-z0-9+/=]{500,}")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "data:image" in msg or len(msg) > 2000:
            msg = self._DURI.sub("<data-uri stripped>", msg)
            msg = self._LONG_B64.sub("<base64 stripped>", msg)
            if len(msg) > 4000:
                msg = msg[:1500] + "... <truncated>"
            record.msg = msg
            record.args = ()
        return True


def _install_log_hygiene():
    root = logging.getLogger()
    if not getattr(root, "_wbnator_hygiene", False):
        f = _StripBase64()
        root.addFilter(f)
        for h in list(root.handlers):
            h.addFilter(f)
        root._wbnator_hygiene = True
    for name in ("urllib3", "requests", "httpx", "httpcore", "h11",
                 "openai", "PIL", "gradio_client"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # asyncio on Windows logs benign ConnectionReset/Proactor errors at ERROR level
    # when the browser refreshes / closes a websocket — silence them.
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)


logger = logging.getLogger("wbnator")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
_install_log_hygiene()


# V5 image providers (3 живых). ChatGPT — основа/по умолчанию.
# Все три поддерживают реф-фото (V5 принимает рефы в inputs[] для всех моделей).
PROVIDER_LABELS = {
    "fastgen_openai":      "ChatGPT Image (V5, с реф-фото до 10) — основной",
    "fastgen_nanopro":     "Nano Banana Pro (V5, с реф-фото до 10)",
    "fastgen_nanobanana2": "Nano Banana 2 (V5, с реф-фото до 10)",
    # alias старого слота «Gemini» → Nano Banana 2 (gemini-картинок в V5 нет)
    "fastgen_gemini":      "Nano Banana 2 (V5, с реф-фото) — псевдоним",
    # Прямой OpenAI gpt-image-2 (минуя FastGen). Платится из своего OpenAI-ключа.
    # quality/size настраиваются в админке (config.openai_image_quality / _size).
    "openai_direct":       "GPT Image 2 (прямой OpenAI · medium/high · 1024–1536)",
}

VIDEO_PROVIDER_LABELS = {
    "fastgen_keyframes":"FastGen · Flow Keyframes (v4, лучше держит текст карточки)",
    "fastgen_flower":   "FastGen · Flower (Veo 3.1, премиум) — рекомендуется",
    "fastgen_flow":     "FastGen · Flow Ingredients (Veo)",
    "fastgen_gemini":   "FastGen · Gemini Veo",
    "fastgen_whisk":    "FastGen · Whisk video",
    "fastgen_grok":     "FastGen · Grok video",
    "veononstop":       "VeoNonStop · video (старый клиент)",
}


# ─── Config + small utils ────────────────────────────────────────────────────

def load_config():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_root"] = here
    cfg["output_dir"] = os.path.abspath(os.path.join(here, cfg.get("output_dir", "./output")))
    cfg["tmp_dir"] = os.path.abspath(os.path.join(here, cfg.get("tmp_dir", "./tmp")))
    os.makedirs(cfg["output_dir"], exist_ok=True)
    os.makedirs(cfg["tmp_dir"], exist_ok=True)
    return cfg


def effective_ai_proxy(cfg, fallback=""):
    """Глобальный рубильник прокси НЕЙРОНКИ (управляется из админ-панели).

    config.json → "ai_proxy": {"enabled": bool, "url": "http://user:pass@host:port"}
      • enabled=False            → НАПРЯМУЮ (перебивает любой per-endpoint proxy_url);
      • enabled=True + url        → этот url используется для ВСЕХ AI-запросов;
      • enabled=True, url пустой   → откат на fallback (старое per-endpoint значение);
      • ключа ai_proxy нет вообще → старое поведение (возвращаем fallback).
    """
    ap = cfg.get("ai_proxy") if isinstance(cfg, dict) else None
    if isinstance(ap, dict) and ap.get("enabled") is not None:
        if not ap.get("enabled"):
            return ""
        return (ap.get("url") or "").strip() or (fallback or "")
    return fallback or ""


def _import_veo_client(vidinator_core_path):
    p = os.path.abspath(vidinator_core_path)
    if not os.path.isdir(p):
        raise FileNotFoundError(f"Не найден core-каталог Vidinator: {p}")
    if p not in sys.path:
        sys.path.insert(0, p)
    server_prompts = os.path.join(os.path.dirname(p), "server_prompts")
    if os.path.isdir(server_prompts) and server_prompts not in sys.path:
        sys.path.insert(0, server_prompts)
    from veo_client import VeoNonStopClient
    return VeoNonStopClient


def _slugify(s):
    s = (s or "product").lower()
    s = re.sub(r"[^a-zа-я0-9_\- ]+", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", "_", s.strip())
    return (s or "product")[:48]


_SAFE_IMG_EXT = {"jpg", "jpeg", "png"}

# Регистрируем декодеры современных мобильных форматов (iPhone HEIC, новые Android AVIF).
# Без них Pillow.open(*.avif/*.heic) → UnidentifiedImageError → код раньше отправлял
# сырые байты «как есть» → OpenAI отбивал `vision не поддерживается` → fallback в
# text-only → модель отвечала вслепую (галлюцинировала «Lego» на чашке).
# Импорты лёгкие, ошибки молча игнорим — старые машины без libavif продолжат работу.
try:
    import pillow_avif  # noqa: F401  (регистрирует AVIF плагин в PIL)
except Exception:
    pass
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass


class _UnreadableImage(Exception):
    """Картинку не получилось привести к JPG/PNG — отправлять её бессмысленно."""


def _file_to_data_uri(path, *, strict: bool = False, max_side: int = None):
    """Базовый64 data-URI картинки. ВАЖНО: openai-image (fastgen_openai) принимает
    ТОЛЬКО JPG/PNG — webp/heic/gif/bmp он отбивает HTTP 400, из-за чего рендер
    8 раз ретраил и терял ~2 мин до фолбэка. Поэтому любой небезопасный формат
    перекодируем в PNG через Pillow ещё на входе (и для image-API, и для vision).

    strict=True → если перекодировать не удалось, бросаем `_UnreadableImage`.
    Используется в detect_category, чтобы НЕ продолжать с битым фото и не отвечать
    юзеру галлюцинацией из text-only-режима.

    max_side=N → даунскейл длинной стороны до N px. ТОЛЬКО для LLM-vision вызовов:
    OpenAI vision сам ужимает входы до 2048px long-side перед тайлингом, поэтому
    N=1536 для анализа = нулевая потеря качества, но минус мегабайты base64
    (юзеры кидают фото по 10-20 МБ) и минус оплаченные vision-тайлы. Рефам для
    image-API теперь тоже передаём кап (_IMG_REF_MAX_SIDE=2048): gpt-image всё равно
    ужимает вход к этому потолку, так что идентичность держится, а полный оригинал
    зря раздувал бы оплачиваемый edit-input ($8/1M)."""
    ext = Path(path).suffix.lower().lstrip(".")
    if ext in _SAFE_IMG_EXT and not max_side:
        mime = "jpeg" if ext in ("jpg", "jpeg") else "png"
        with open(path, "rb") as f:
            data = f.read()
        return f"data:image/{mime};base64,{base64.b64encode(data).decode('ascii')}"
    try:
        from PIL import Image
        import io as _io
        with Image.open(path) as im:
            has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if has_alpha else "RGB")
            if max_side and max(im.size) > max_side:
                ratio = max_side / float(max(im.size))
                im = im.resize((max(1, round(im.width * ratio)),
                                max(1, round(im.height * ratio))), Image.LANCZOS)
            buf = _io.BytesIO()
            if has_alpha:
                im.save(buf, format="PNG")
                mime = "png"
            else:
                # Для vision-вызовов JPEG q88: в разы меньше PNG, для анализа без разницы.
                fmt_out = "JPEG" if max_side else "PNG"
                if fmt_out == "JPEG":
                    im.save(buf, format="JPEG", quality=88)
                    mime = "jpeg"
                else:
                    im.save(buf, format="PNG")
                    mime = "png"
            data = buf.getvalue()
        if not max_side:
            logger.info(f"_file_to_data_uri: перекодировал {ext or '?'} → {mime.upper()} ({Path(path).name})")
        return f"data:image/{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception as e:
        msg = f"не удалось перекодировать {path} ({e})"
        if strict:
            logger.warning(f"_file_to_data_uri: {msg} — отказ (strict mode)")
            raise _UnreadableImage(msg)
        logger.warning(f"_file_to_data_uri: {msg}; шлю как есть")
        with open(path, "rb") as f:
            data = f.read()
        return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"


_VISION_MAX_SIDE = 1536    # для LLM-vision (OpenAI сам режет до 2048 → потерь нет)
_QC_MAX_SIDE = 1024        # для vision-QC готовых карточек (дефекты текста видны)
# Реф для image-API (edit-база gpt-image): кап длинной стороны = 2048 px. Это РОВНО
# потолок, до которого OpenAI и так ужимает любой вход перед обработкой (output
# gpt-image максимум 1536 px — модель физически не работает в большем разрешении),
# поэтому 2048 идентичность товара/логотипов/мелкого текста держит байт-в-байт, а
# юзерские оригиналы 10-20 МБ / 3000-4000 px перестают лететь в high-fidelity
# image-input ($8/1M токенов) целиком. Шире vision-варианта (1536) — у edit-базы
# выше планка детализации, т.к. она РЕПРОДУЦИРУЕТ товар, а не просто читает атрибуты.
_IMG_REF_MAX_SIDE = 2048


def _prepare_refs(ref_paths, style_ref_paths=None):
    """product_refs И style_refs идут в ОБА места:
       • LLM-vision (дизайн) — даунскейл-вариант `image_vision` (экономия токенов);
       • image API — полноразмерный `image` (разрешение = идентичность товара);
         style_refs туда идут ПЕРВЫМИ с обязательным role-anchor в промпте
         («styleRef = только стиль») — см. _style_ref_nudge / _refs_for_provider.
    """
    def _load(p, name):
        # image-API реф (edit-база): кап до _IMG_REF_MAX_SIDE=2048. Раньше слался
        # полный размер (10-20 МБ фото) → раздувал оплачиваемый image-input в edit-
        # вызове gpt-image. 2048 = потолок OpenAI, идентичность не страдает.
        try:
            full = _file_to_data_uri(p, max_side=_IMG_REF_MAX_SIDE)
        except Exception:
            full = _file_to_data_uri(p)
        try:
            vis = _file_to_data_uri(p, max_side=_VISION_MAX_SIDE)
        except Exception:
            vis = full
        return {"name": name, "image": full, "image_vision": vis}

    product_refs = []
    for i, p in enumerate(ref_paths or []):
        if not p or not os.path.isfile(p):
            if p: logger.warning(f"Ref-фото не найдено: {p}")
            continue
        try:
            product_refs.append(_load(p, f"product{i+1}"))
        except Exception as e:
            logger.warning(f"Не удалось прочитать ref {p}: {e}")
    style_refs = []
    for i, p in enumerate(style_ref_paths or []):
        if not p or not os.path.isfile(p):
            if p: logger.warning(f"Style-ref не найден: {p}")
            continue
        try:
            style_refs.append(_load(p, f"styleRef{i+1}" if i > 0 else "styleRef"))
        except Exception as e:
            logger.warning(f"Не удалось прочитать style-ref {p}: {e}")
    return product_refs, style_refs


# ─── Claude (multimodal-aware with text fallback) ────────────────────────────

import time as _time


_TRANSIENT_HTTP = {429, 502, 503, 504}


# ════════════════════════════════════════════════════════════════════════════
# LLM USAGE TRACKING — пишем каждый успешный ответ в accounts.db.llm_usage,
# чтобы админка считала токены/деньги, видела разрезы по дням/моделям/юзерам.
# Тред-локальный контекст ставит server.py перед запуском конкретной работы
# (см. set_llm_context / clear_llm_context), чтобы строки помечались правильным
# user_id, purpose ('design' / 'detect_category' / 'copy_fix' / ...) и generation_id.
# ════════════════════════════════════════════════════════════════════════════
import threading as _threading_mod
_LLM_CTX = _threading_mod.local()

def set_llm_context(*, user_id=None, purpose=None, generation_id=None, img_policy=None):
    """Установить контекст ДО серии Claude/OpenAI вызовов. Безопасно вызывать
    параллельно из разных потоков — каждый поток держит свой контекст.

    img_policy — тир-политика картинок этой генерации (provider/quality/mix),
    см. resolve_image_policy(). Читается в _stream_render_from_prepared (тот же
    поток, что и владелец контекста) и применяется по карточкам."""
    _LLM_CTX.user_id = user_id
    _LLM_CTX.purpose = purpose
    _LLM_CTX.generation_id = generation_id
    _LLM_CTX.img_policy = img_policy

def clear_llm_context():
    for k in ("user_id", "purpose", "generation_id", "img_policy"):
        if hasattr(_LLM_CTX, k):
            delattr(_LLM_CTX, k)

def get_llm_context() -> dict:
    """Снимок тред-локального контекста ТЕКУЩЕГО потока. Нужен чтобы передать
    user_id/generation_id в воркер-потоки рендера картинок (ThreadPoolExecutor):
    thread-local НЕ наследуется дочерними потоками, поэтому контекст снимаем в
    потоке-владельце и пробрасываем явно в _record_image_usage."""
    return {
        "user_id": getattr(_LLM_CTX, "user_id", None),
        "purpose": getattr(_LLM_CTX, "purpose", None),
        "generation_id": getattr(_LLM_CTX, "generation_id", None),
        "img_policy": getattr(_LLM_CTX, "img_policy", None),
    }


# ════════════════════════════════════════════════════════════════════════════
# ТИР-ПОЛИТИКА КАРТИНОК — какой провайдер/качество дать этой генерации.
#   • paid  (тратит купленные карточки) → gpt-image-2, quality = openai_image_quality
#     (по умолчанию medium). «Заплатил — получи medium».
#   • free  (бесплатная дневная проба)  → по настройке new_user_provider:
#       - "mix" (дефолт): чередуем nano-pro / gpt-image low по карточкам
#       - "fastgen_nanopro": все карточки на FastGen Nano Pro
#       - "openai_low": все карточки на gpt-image-2 low
# Master-рубильник tier_policy_enabled=false → вернуть None (legacy: берём
# image_provider из запроса/конфига и глобальный openai_image_quality).
# ════════════════════════════════════════════════════════════════════════════
def resolve_image_policy(cfg, tier):
    # NB: без аннотации возврата намеренно — в этом модуле нет
    # `from __future__ import annotations`, а `dict | None` (PEP 604) упал бы
    # на Python 3.9 (хостинг). Возвращает dict-политику или None.
    cfg = cfg or {}
    if not cfg.get("tier_policy_enabled", True):
        return None
    if tier == "paid":
        q = (cfg.get("openai_image_quality") or "medium").strip().lower()
        if q not in ("low", "medium", "high", "auto"):
            q = "medium"
        return {"mode": "single", "provider": "openai_direct", "quality": q,
                "primary": "openai_direct"}
    # free / new user
    choice = (cfg.get("new_user_provider") or "mix").strip().lower()
    if choice == "fastgen_nanopro":
        return {"mode": "single", "provider": "fastgen_nanopro", "quality": None,
                "primary": "fastgen_nanopro"}
    if choice in ("openai_low", "gpt_low", "openai_direct_low"):
        return {"mode": "single", "provider": "openai_direct", "quality": "low",
                "primary": "openai_direct"}
    # mix (default): чередуем nano-pro и gpt-image low
    return {"mode": "mix", "providers": ["fastgen_nanopro", "openai_direct"],
            "quality": "low", "primary": "fastgen_nanopro"}

# Прайс на токены (USD за 1M токенов). Если модели нет в таблице — считаем
# по дефолту gpt-4o-mini-tier. Цены сверены с OpenAI public pricing на 06-2026;
# админ-UI отображает оценку, не платёжный документ. cached_in — цена входа из кеша.
_LLM_PRICING_USD_PER_M = {
    # OpenAI GPT-4.1 — основная линейка vision (правда что 4.1 умнее 4o при той же цене входа)
    "gpt-4.1":       {"in": 2.00, "out": 8.00,  "cached_in": 0.50},
    "gpt-4.1-mini":  {"in": 0.40, "out": 1.60,  "cached_in": 0.10},
    "gpt-4.1-nano":  {"in": 0.10, "out": 0.40,  "cached_in": 0.025},
    # OpenAI GPT-4o (legacy; vision OK, остаётся как proven fallback)
    "gpt-4o-mini":   {"in": 0.15, "out": 0.60,  "cached_in": 0.075},
    "gpt-4o":        {"in": 2.50, "out": 10.00, "cached_in": 1.25},
    # Claude — цена НЕ оценивается (прокси, ставки неизвестны): cost_usd=0, считаем
    # только токены. Ставки ниже оставлены справочно и в расчёт стоимости НЕ идут.
    "claude-opus-4.6":           {"in": 15.0, "out": 75.0},
    "claude-opus-4-7":           {"in": 15.0, "out": 75.0},
    "claude-sonnet-4.6":         {"in": 3.00, "out": 15.0},
    "claude-sonnet-4-20250514":  {"in": 3.00, "out": 15.0},
    "claude-haiku-4.5":          {"in": 0.25, "out": 1.25},
    "claude-haiku-4-5":          {"in": 0.25, "out": 1.25},
    "claude-3-5-sonnet-20241022":{"in": 3.00, "out": 15.0},
}
_LLM_PRICING_DEFAULT = {"in": 0.5, "out": 1.5}

def _is_claude_model(model: str) -> bool:
    """Claude-вызовы (через прокси stepanovikov / wellflow) — ставки неизвестны,
    поэтому деньги НЕ считаем, только токены. cost_usd для них всегда 0."""
    return "claude" in (model or "").lower()


def _estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int,
                       cached_tokens: int = 0) -> float:
    """Оценка $ с учётом кеша. Claude → 0 (цену не пишем). Cached prompt-токены
    тарифицируются по cached_in (≈10% от обычного входа), остальной вход — по in.
    Модель может прийти как slug OpenRouter ('openai/gpt-4.1') — берём хвост."""
    if _is_claude_model(model):
        return 0.0
    key = (model or "").split("/")[-1]
    p = _LLM_PRICING_USD_PER_M.get(key) or _LLM_PRICING_USD_PER_M.get(model) or _LLM_PRICING_DEFAULT
    cached = max(0, min(int(cached_tokens or 0), int(prompt_tokens or 0)))
    fresh_in = max(0, int(prompt_tokens or 0) - cached)
    cached_rate = p.get("cached_in", p["in"])
    return round(
        (fresh_in / 1_000_000) * p["in"]
        + (cached / 1_000_000) * cached_rate
        + (int(completion_tokens or 0) / 1_000_000) * p["out"],
        6,
    )

def _record_llm_usage(endpoint: str, model: str,
                      prompt_tokens: int, completion_tokens: int,
                      total_tokens: int, cached_tokens: int = 0,
                      purpose_override: str = None) -> None:
    """Запись использования. user_id/purpose/generation_id — из тред-локального
    контекста. Если контекст не задан (вызов из ручки админки или фоновой
    задачи) — user_id=NULL, purpose=NULL.

    Импорт accounts.db делается лениво — generator может быть импортирован до
    инициализации БД в проце CLI/тестов."""
    if total_tokens <= 0 and prompt_tokens <= 0 and completion_tokens <= 0:
        # Прокси не отдал usage — нечего писать; но всё же логируем чтобы был
        # счётчик «сколько вызовов было» (нули в токенах допустимы).
        pass
    cost = _estimate_cost_usd(model, prompt_tokens, completion_tokens, cached_tokens)
    uid = getattr(_LLM_CTX, "user_id", None)
    # Точный (пошаговый) purpose из _claude_chat имеет приоритет над грубым
    # thread-local (set_llm_context ставит один на весь запрос) — чтобы разрез
    # «по назначению» в админке был правдивым (design / copy_fix / detect_category / …).
    purpose = purpose_override or getattr(_LLM_CTX, "purpose", None)
    gid = getattr(_LLM_CTX, "generation_id", None)
    try:
        from accounts import db as _adb  # lazy
        from datetime import datetime as _dt
        with _adb.tx() as cur:
            cur.execute(
                """INSERT INTO llm_usage
                   (user_id, endpoint, model, prompt_tokens, completion_tokens,
                    total_tokens, cached_tokens, cost_usd, purpose, generation_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uid, endpoint, model,
                 prompt_tokens, completion_tokens, total_tokens,
                 int(cached_tokens or 0), cost,
                 purpose, gid, _dt.utcnow().isoformat()),
            )
    except Exception as e:
        logger.debug(f"_record_llm_usage DB insert failed (non-fatal): {e}")


# ════════════════════════════════════════════════════════════════════════════
# IMAGE USAGE TRACKING — у картинок нет токенов, поэтому отдельная таблица
# image_usage + своя оценка $. Пишем на каждый успешный рендер карточки в
# _render_card_with_fallback (там известен РЕАЛЬНЫЙ провайдер после фолбэка).
#   • openai_direct (gpt-image-2) → реальные деньги: считаем по прайсу image
#     output-токенов OpenAI (фикс. кол-во токенов на (quality,size)).
#   • fastgen_* → кредиты FastGen. $ = credits * ставка из config (по умолчанию
#     0 — кредиты дёшевы/безлимит; админ может задать fastgen_credit_usd).
# ════════════════════════════════════════════════════════════════════════════

# Кол-во image-output токенов на 1 картинку gpt-image (quality × size). Источник:
# тарифная сетка OpenAI Images (gpt-image). Стоимость = tokens * $/1M (ниже).
# Это ОЦЕНКА для админ-дашборда, не платёжный документ (как и _LLM_PRICING_*).
_IMG_GPT_TOKENS = {
    ("low",    "1024x1024"): 272,  ("medium", "1024x1024"): 1056, ("high", "1024x1024"): 4160,
    ("low",    "1024x1536"): 408,  ("medium", "1024x1536"): 1584, ("high", "1024x1536"): 6240,
    ("low",    "1536x1024"): 400,  ("medium", "1536x1024"): 1568, ("high", "1536x1024"): 6208,
}
_IMG_GPT_USD_PER_M = 40.0  # $/1M image-output токенов gpt-image

# Кредиты FastGen по провайдеру (источник: fastgen_v5_client._MODELS).
_IMG_FASTGEN_CREDITS = {
    "fastgen_nanopro": 4, "fastgen_nanobanana2": 4, "fastgen_gemini": 4,
    "fastgen_imagen": 4, "fastgen_openai": 1, "fastgen_flower": 1,
    "fastgen_grok": 1, "fastgen_whisk_remix": 4,
}
_IMG_PROVIDER_MODEL = {
    "openai_direct": "gpt-image-2", "fastgen_openai": "openai-image",
    "fastgen_nanopro": "nano-banana-pro", "fastgen_nanobanana2": "nano-banana-2",
    "fastgen_gemini": "nano-banana-2", "fastgen_imagen": "imagen-4",
    "fastgen_flower": "flower-image", "fastgen_grok": "grok-image",
    "fastgen_whisk_remix": "nano-banana-pro", "veononstop": "veo",
}


def _live_openai_image_qs(cfg: dict) -> tuple[str, str]:
    """Живые quality/size gpt-image из server.CFG (зеркало логики
    _render_card_image_raw — что в админке, то и применилось)."""
    live = cfg or {}
    try:
        import server as _srv  # type: ignore
        if isinstance(getattr(_srv, "CFG", None), dict):
            live = _srv.CFG
    except Exception:
        pass
    quality = (live.get("openai_image_quality") or "high").strip().lower()
    size = (live.get("openai_image_size") or "auto").strip().lower().replace("х", "x")
    return quality, size


def _estimate_image_cost_usd(provider: str, cfg: dict,
                             quality: str = None, size: str = None) -> tuple[float, int, str, str, str]:
    """→ (cost_usd, credits, model, quality, size). gpt-image → реальная оценка $;
    fastgen → credits*ставка; всё прочее → 0."""
    model = _IMG_PROVIDER_MODEL.get(provider, provider or "")
    if provider == "openai_direct":
        q = (quality or "").strip().lower() or None
        s = (size or "").strip().lower().replace("х", "x") or None
        if not q or not s:
            lq, ls = _live_openai_image_qs(cfg)
            q = q or lq
            s = s or ls
        # 'auto' нормализуем к фактическому: качество auto≈medium; размер auto для
        # WB-карточки → вертикаль 1024x1536 (по aspect_ratio запроса).
        q_eff = "medium" if q in ("auto", "", None) else q
        s_eff = "1024x1536" if s in ("auto", "", None) else s
        tokens = _IMG_GPT_TOKENS.get((q_eff, s_eff)) or _IMG_GPT_TOKENS.get((q_eff, "1024x1536")) or 1584
        cost = round(tokens * _IMG_GPT_USD_PER_M / 1_000_000, 6)
        return cost, 0, model, q, s
    # FastGen-фолбэк: цены свои/непрозрачные — НЕ считаем в деньгах и кредитах,
    # просто учитываем в ЧИСЛЕ фоток (cost=0, credits=0). По просьбе: «фастген
    # в кредитах не надо считать, просто считай его в числе фоток».
    if provider and provider.startswith("fastgen_"):
        return 0.0, 0, model, "", ""
    return 0.0, 0, model, "", ""


def _record_image_usage(provider: str, cfg: dict = None, *,
                        user_id=None, generation_id=None, purpose=None,
                        quality: str = None, size: str = None,
                        images: int = 1) -> None:
    """Запись одной (или N) сгенерированной картинки в image_usage. user_id/
    generation_id/purpose — явно из снятого контекста (воркер-поток рендера НЕ
    видит thread-local); если не передано — добираем из thread-local текущего
    потока (на случай синхронных вызовов). Никогда не роняет рендер."""
    try:
        cost, credits, model, q, s = _estimate_image_cost_usd(provider, cfg or {}, quality, size)
        uid = user_id if user_id is not None else getattr(_LLM_CTX, "user_id", None)
        gid = generation_id if generation_id is not None else getattr(_LLM_CTX, "generation_id", None)
        purp = purpose or getattr(_LLM_CTX, "purpose", None)
        from accounts import db as _adb  # lazy
        from datetime import datetime as _dt
        with _adb.tx() as cur:
            cur.execute(
                """INSERT INTO image_usage
                   (user_id, generation_id, provider, model, quality, size,
                    images, credits, cost_usd, purpose, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uid, gid, provider or "?", model, q, s,
                 int(images or 1), int(credits or 0), float(cost or 0.0),
                 purp, _dt.utcnow().isoformat()),
            )
    except Exception as e:
        logger.debug(f"_record_image_usage DB insert failed (non-fatal): {e}")


# Session-scoped blacklists (adapted from Vidinator's _url_model_blacklist
# + STEPANOV_FAIL_THRESHOLD carousel).
#
# - _DEAD_ENDPOINTS: base_url → reason. Marked on auth errors (401/403) — endpoint
#   has wrong key, retrying it the rest of the session wastes time and slows the user.
# - _DEAD_URL_MODELS: (base_url, model) → reason. Marked on 400 "unknown_model"-type
#   responses so we never re-try a model the proxy rejects as not supported.
# - _ENDPOINT_HEALTH: base_url → {consec_fails, cooldown_until}. Endpoints that fail
#   transiently (502/timeout) too many times go into a soft cooldown so the next
#   _claude_chat call skips them entirely. After the cooldown elapses, they get
#   retried (transient outages do recover).
# - _ENDPOINT_PRIORITY: ordered list of base_urls that succeeded recently — these
#   are tried FIRST on subsequent calls, before any other endpoint. Mimics
#   Vidinator's "active URL pointer" idea: once an endpoint proves healthy, stick
#   with it until it fails.
# All reset on process restart (intentional — config changes pick up).
_DEAD_ENDPOINTS: dict = {}
_DEAD_URL_MODELS: set = set()
_ENDPOINT_HEALTH: dict = {}
_ENDPOINT_PRIORITY: list = []
# Последний успешный маршрут Claude-вызова (endpoint/model). Пишется под локом на
# каждом 200; читается дизайном для логирования «какой моделью собран пак».
_LAST_CLAUDE_ROUTE: dict = {}
_CLAUDE_HEALTH_LOCK = threading.Lock()
# Логируем «использую прокси X» только ОДИН раз на endpoint (иначе спамит).
_PROXY_LOGGED: dict = {}

_ENDPOINT_FAIL_THRESHOLD = 2     # consecutive call failures before cooldown
_ENDPOINT_COOLDOWN_SECS  = 180   # 3 minutes of "skip this endpoint" after threshold


def _is_endpoint_dead(base_url: str) -> bool:
    with _CLAUDE_HEALTH_LOCK:
        return base_url in _DEAD_ENDPOINTS


def _mark_endpoint_dead(base_url: str, reason: str) -> None:
    with _CLAUDE_HEALTH_LOCK:
        if base_url in _DEAD_ENDPOINTS:
            return
        _DEAD_ENDPOINTS[base_url] = reason
    logger.warning(f"[Claude] endpoint marked DEAD for session: {base_url} ({reason})")


def _is_url_model_blacklisted(base_url: str, model: str) -> bool:
    with _CLAUDE_HEALTH_LOCK:
        return (base_url, model) in _DEAD_URL_MODELS


def _blacklist_url_model(base_url: str, model: str, reason: str) -> None:
    with _CLAUDE_HEALTH_LOCK:
        if (base_url, model) in _DEAD_URL_MODELS:
            return
        _DEAD_URL_MODELS.add((base_url, model))
    logger.warning(f"[Claude] (url,model) blacklisted: {base_url} / {model} ({reason})")


def _is_endpoint_in_cooldown(base_url: str) -> bool:
    with _CLAUDE_HEALTH_LOCK:
        h = _ENDPOINT_HEALTH.get(base_url)
        if not h:
            return False
        return h.get("cooldown_until", 0) > _time.time()


def _endpoint_cooldown_remaining(base_url: str) -> int:
    with _CLAUDE_HEALTH_LOCK:
        h = _ENDPOINT_HEALTH.get(base_url) or {}
        return max(0, int(h.get("cooldown_until", 0) - _time.time()))


def _report_endpoint_call_fail(base_url: str) -> None:
    """Whole _claude_chat invocation exhausted retries on this endpoint without success."""
    with _CLAUDE_HEALTH_LOCK:
        h = _ENDPOINT_HEALTH.setdefault(base_url, {"consec_fails": 0, "cooldown_until": 0})
        h["consec_fails"] += 1
        if h["consec_fails"] >= _ENDPOINT_FAIL_THRESHOLD:
            h["cooldown_until"] = _time.time() + _ENDPOINT_COOLDOWN_SECS
            h["consec_fails"] = 0
            logger.warning(
                f"[Claude] endpoint {base_url} → cooldown {_ENDPOINT_COOLDOWN_SECS}s "
                f"(consec call failures hit threshold)"
            )


def _report_endpoint_call_success(base_url: str) -> None:
    """A model on this endpoint returned 200 — endpoint is healthy. Reset & promote."""
    with _CLAUDE_HEALTH_LOCK:
        h = _ENDPOINT_HEALTH.setdefault(base_url, {"consec_fails": 0, "cooldown_until": 0})
        h["consec_fails"] = 0
        h["cooldown_until"] = 0
        # Promote this endpoint to the front of the priority list — try it first next time.
        if base_url in _ENDPOINT_PRIORITY:
            _ENDPOINT_PRIORITY.remove(base_url)
        _ENDPOINT_PRIORITY.insert(0, base_url)


def _ordered_endpoints(endpoints: list) -> list:
    """Return endpoints reordered so recently-successful URLs come first.
    Stable order preserved within each group."""
    with _CLAUDE_HEALTH_LOCK:
        priority = list(_ENDPOINT_PRIORITY)
    if not priority:
        return endpoints
    by_url = {e["base_url"]: e for e in endpoints if e.get("base_url")}
    front = [by_url[u] for u in priority if u in by_url]
    rest  = [e for e in endpoints if e.get("base_url") not in priority]
    return front + rest


def claude_health_snapshot() -> dict:
    """Diagnostic helper for the website backend — returns current session health."""
    now = _time.time()
    with _CLAUDE_HEALTH_LOCK:
        return {
            "dead_endpoints": dict(_DEAD_ENDPOINTS),
            "dead_url_models": [{"url": u, "model": m} for (u, m) in _DEAD_URL_MODELS],
            "endpoint_health": {
                u: {
                    "consec_fails": h.get("consec_fails", 0),
                    "cooldown_remaining_secs": max(0, int(h.get("cooldown_until", 0) - now)),
                }
                for u, h in _ENDPOINT_HEALTH.items()
            },
            "priority_order": list(_ENDPOINT_PRIORITY),
        }


def reset_claude_health() -> None:
    """Reset session blacklists. Call after config changes / proxy resurrection."""
    with _CLAUDE_HEALTH_LOCK:
        _DEAD_ENDPOINTS.clear()
        _DEAD_URL_MODELS.clear()
        _ENDPOINT_HEALTH.clear()
        _ENDPOINT_PRIORITY.clear()
    logger.info("[Claude] session blacklists reset")


def _build_claude_endpoints(cfg):
    """Returns list[{name, base_url, api_key, models, proxy_url}] from config.

    Two config schemas supported:
      NEW (preferred): cfg['claude_endpoints'] — list of {name, base_url, api_key, models, [proxy_url], [enabled]}
      LEGACY: cfg['claude_base_url'] + cfg['claude_api_key']
              + cfg['claude_model_primary'/'claude_model_fallback']

    Поверх массива применяются:
      • cfg['claude_endpoint_order'] — список имён в нужном порядке (то что админ
        перетащил в UI). Имена не из массива идут после в исходном порядке.
      • per-endpoint cfg['claude_endpoints'][*]['enabled'] = false — пропускаем.

    Это позволяет менять приоритет / выключать endpoints вживую через админку
    без рестарта процесса (сервер делает CFG = G.load_config() после сохранения).
    """
    eps = cfg.get("claude_endpoints")
    out = []
    if isinstance(eps, list) and eps:
        for e in eps:
            if not (e.get("base_url") and e.get("models")):
                continue
            # enabled=false → endpoint полностью выключен из цепочки.
            # Отсутствие поля enabled трактуем как True (back-compat).
            if e.get("enabled") is False:
                continue
            out.append({
                "name":      e.get("name", e.get("base_url", "?")),
                "base_url":  e["base_url"],
                "api_key":   e.get("api_key") or cfg.get("claude_api_key"),
                "models":    list(e.get("models") or []),
                "proxy_url": (e.get("proxy_url") or "").strip(),
            })
        if out:
            # Применяем пользовательский порядок из админки (если задан).
            order = cfg.get("claude_endpoint_order") or []
            if isinstance(order, list) and order:
                ranked = []
                seen = set()
                for name in order:
                    for ep in out:
                        if ep["name"] == name and name not in seen:
                            ranked.append(ep)
                            seen.add(name)
                            break
                for ep in out:
                    if ep["name"] not in seen:
                        ranked.append(ep)
                out = ranked
            return out
    # Legacy fallback
    models = [m for m in (cfg.get("claude_model_primary"),
                          cfg.get("claude_model_fallback")) if m]
    return [{
        "name":     "default",
        "base_url": cfg.get("claude_base_url"),
        "api_key":  cfg.get("claude_api_key"),
        "models":   models,
    }]


def _endpoints_for_purpose(cfg, purpose):
    """Per-task маршрутизация моделей. Если в config заданы 'llm_tiers' и
    'purpose_tier' — возвращает ПЛОСКУЮ цепочку «виртуальных endpoint'ов» (по одной
    модели в каждом) в порядке tier'а: primary → fallback. Это позволяет послать
    дорогую модель (gpt-4.1 / opus) на креатив-шаги (design / copy_fix / synth) и
    дешёвую (gpt-4o-mini / gpt-4.1-nano) на механический vision (detect / визуал
    конкурентов), при этом фолбэк остаётся ВНУТРИ tier'а (не падает с умной модели
    сразу на самую дешёвую). purpose=None → берётся llm_default_tier (по умолчанию 'balanced').
    Возвращает None, если tier-блоки не заданы → старое поведение сохраняется.

    Заодно это и есть «батч-экономия» на повторных вызовах: системный промпт у
    одинаковых шагов идентичен, а провайдер (OpenAI/OpenRouter) кеширует стабильный
    префикс автоматически — повторные вызовы (5 батчей конкурентов и т.п.) платят
    за систему ~10% цены."""
    tiers = cfg.get("llm_tiers")
    if not isinstance(tiers, dict) or not tiers:
        return None
    ptmap = cfg.get("purpose_tier") if isinstance(cfg.get("purpose_tier"), dict) else {}
    tier_name = (ptmap.get(purpose) if purpose else None) or cfg.get("llm_default_tier") or "balanced"
    chain = tiers.get(tier_name)
    if not isinstance(chain, list) or not chain:
        return None
    by_name = {e["name"]: e for e in _build_claude_endpoints(cfg)}
    out = []
    for step in chain:
        if not isinstance(step, dict):
            continue
        ep = by_name.get(step.get("endpoint"))
        model = step.get("model")
        if not ep or not model:
            continue
        # Виртуальный endpoint = копия реального, но с РОВНО одной моделью — так
        # основной цикл _claude_chat пробует пары (endpoint, model) строго в порядке
        # tier'а. Дубли base_url между шагами допустимы (reorder для цепочки выключен).
        out.append({**ep, "models": [model]})
    return out or None


def _claude_chat(cfg, system_prompt, user_prompt, images=None,
                 max_tokens=6000, temperature=0.7,
                 retries_per_model=2, dead_endpoint_threshold=2,
                 only_endpoints=None, purpose=None):
    """Resilient OpenAI/Claude chat across an (endpoint × model) chain.

    Adapted from Vidinator's gemini_api_client.send_messages() error classification:

    For each endpoint in cfg['claude_endpoints']:
      Skip if endpoint is in session-level _DEAD_ENDPOINTS (auth-failed earlier).
      For each model in endpoint['models']:
        Skip if (url, model) is in _DEAD_URL_MODELS (unknown_model blacklist).
        Try with images first (if provided), text-only fallback on vision-fail.
        retries_per_model=4 attempts on transient (429/502/503/504/timeout/conn) with
        exponential backoff up to 30s.

      Error → action:
        200          → success
        401/403      → mark endpoint dead for whole session, jump to next endpoint
        400 unknown_model → blacklist (url, model), try next model
        400/415/422 + images → vision_fail, retry same model text-only
        429/502/503/504/timeout → retry with backoff, then next model after limit
        Other 4xx/5xx → hard error, next model

      If `dead_endpoint_threshold=3` consecutive transient timeouts on this endpoint →
      assume the proxy is having a bad time, jump to next endpoint (don't burn all models).

    only_endpoints: optional list of endpoint names to use (e.g. ["openai-fallback"]).

    Raises RuntimeError only if EVERY (alive endpoint × non-blacklisted model) fails.
    """
    endpoints = _build_claude_endpoints(cfg)
    if only_endpoints:
        endpoints = [e for e in endpoints if e.get("name") in only_endpoints]
    # Per-purpose маршрутизация: если заданы llm_tiers + purpose_tier — строим явную
    # цепочку (endpoint, model) под задачу. Порядок цепочки = приоритет (primary →
    # fallback) и НЕ переставляется health-приоритетом. only_endpoints (узкие vision-QA
    # вызовы) маршрутизацию обходят и работают по-старому.
    _respect_chain_order = False
    if not only_endpoints:
        _purpose_chain = _endpoints_for_purpose(cfg, purpose)
        if _purpose_chain:
            endpoints = _purpose_chain
            _respect_chain_order = True
    if not endpoints:
        raise RuntimeError("Нет endpoints для Claude в config.json")

    def _build_messages(with_images):
        if with_images and images:
            content = [{"type": "text", "text": user_prompt}]
            for img in images:
                if isinstance(img, dict):
                    uri = img["image"]
                    detail = img.get("detail")
                else:
                    uri = img
                    detail = None
                # detail управляет тайлингом OpenAI vision:
                #   low  → фиксированно 85 токенов на фото (картинка даунсемплится до 512px)
                #   high → ~170 токенов за каждый 512×512 тайл + 85 base (обычно 1k+)
                #   auto / None → OpenAI сам решает (для больших фото = high → дорого)
                # Передаём только если явно задано — иначе сохраняем старое поведение
                # для всех других вызовов (vision-QC, refs и т.п.).
                image_url = {"url": uri}
                if detail in ("low", "high", "auto"):
                    image_url["detail"] = detail
                content.append({"type": "image_url", "image_url": image_url})
            user_msg = {"role": "user", "content": content}
        else:
            user_msg = {"role": "user", "content": user_prompt}
        return [{"role": "system", "content": system_prompt}, user_msg]

    def _classify_400(text: str) -> str:
        """Decide whether a 400 means 'wrong model' or 'bad payload'."""
        t = (text or "").lower()
        markers = ("unknown_model", "model not found", "model_not_found",
                   "invalid model", "no such model", "does not support model")
        return "unknown_model" if any(m in t for m in markers) else "hard"

    def _try_call(endpoint, model, use_images):
        """Returns (ok, content_or_err, kind).
        kind ∈ {'ok','timeout','transient','vision_fail','unknown_model','auth','hard'}
        """
        headers = {
            "Authorization": f"Bearer {endpoint['api_key']}",
            "x-api-key": endpoint["api_key"],
            "Content-Type": "application/json",
        }
        payload = {"model": model, "max_tokens": max_tokens,
                   "temperature": temperature,
                   "messages": _build_messages(use_images)}

        # Per-endpoint proxy. Источники по приоритету (выигрывает первый непустой):
        #   1) endpoint["proxy_url"] из config.json
        #   2) env CLAUDE_PROXY_URL (общий) или OPENAI_PROXY_URL (только для openai-fallback)
        # Если ничего не задано — идём напрямую (так и работало раньше).
        # Зачем: пользовательский системный VPN может рвать TLS до api.openai.com
        # (SSLError на каждой модели = классический симптом). Свой прокси здесь
        # обходит ОС-уровень и стабилизирует выход в OpenAI/Claude независимо от VPN.
        ep_proxy = (endpoint.get("proxy_url") or "").strip()
        if not ep_proxy:
            if endpoint.get("name") == "openai-fallback":
                ep_proxy = (os.environ.get("OPENAI_PROXY_URL") or "").strip()
            if not ep_proxy:
                ep_proxy = (os.environ.get("CLAUDE_PROXY_URL") or "").strip()
        # Глобальный рубильник прокси нейронки (админ-панель) — перебивает всё выше.
        ep_proxy = effective_ai_proxy(cfg, ep_proxy)
        proxies_arg = {"http": ep_proxy, "https": ep_proxy} if ep_proxy else None
        if ep_proxy and not _PROXY_LOGGED.get(endpoint.get("name") or ""):
            # Маскируем пароль если он в URL
            log_proxy = ep_proxy
            if "@" in log_proxy:
                scheme, _, rest = log_proxy.partition("://")
                creds, _, host = rest.partition("@")
                u = creds.split(":", 1)[0]
                log_proxy = f"{scheme}://{u}:***@{host}"
            logger.info(f"[Claude] {endpoint.get('name')!r} использует прокси: {log_proxy}")
            _PROXY_LOGGED[endpoint.get("name") or ""] = True

        # trust_env=False — игнорируем системные HTTPS_PROXY/HTTP_PROXY/NO_PROXY,
        # Windows-registry прокси и .netrc. Раньше из терминала с активным VPN-клиентом
        # эти env-переменные могли перебить наш явный proxies= аргумент в edge-кейсах
        # (или сделать так что прокси-объект merge'ился непредсказуемо). Теперь
        # ТОЛЬКО наш proxy_url из config решает, куда идёт TCP.
        sess = requests.Session()
        sess.trust_env = False
        if proxies_arg:
            sess.proxies = dict(proxies_arg)

        last = "unknown"
        for attempt in range(retries_per_model + 1):
            try:
                r = sess.post(endpoint["base_url"], json=payload,
                              headers=headers,
                              timeout=(float(cfg.get("llm_connect_timeout") or 15),
                                       float(cfg.get("llm_read_timeout") or 120)),
                              verify=False)
            except (requests.Timeout, requests.ConnectionError) as e:
                # Полный текст исключения в логе — без него «SSLError» ничего не говорит.
                # Часто там видно: `[SSL: WRONG_VERSION_NUMBER]` (антивирус MITM),
                # `[Errno 10054] WSAECONNRESET` (VPN разорвал), `Tunnel connection failed:
                # 407 Proxy Authentication Required`, итп.
                err_str = str(e)[:240]
                last = f"network {type(e).__name__}: {err_str}"
                if attempt < retries_per_model:
                    # Exponential backoff up to 30s — gives upstream time to recover.
                    wait = min(2 * (2 ** attempt), 30)
                    logger.warning(f"[Claude] {endpoint['name']}/{model}: {last} — "
                                   f"retry {attempt+1}/{retries_per_model+1} in {wait}s")
                    _time.sleep(wait)
                    continue
                return False, last, "timeout"
            except Exception as e:
                return False, f"{type(e).__name__}: {str(e)[:160]}", "hard"

            if r.status_code == 200:
                try:
                    body = r.json()
                    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
                    usage = body.get("usage") or {}
                except Exception as e:
                    return False, f"Bad JSON: {e}", "hard"
                if content:
                    # Записываем потребление токенов — данные для админки/биллинга.
                    # Если в ответе нет usage (некоторые прокси не возвращают) —
                    # пишем нули; общий счёт вызовов всё равно остаётся.
                    try:
                        # Cached prompt-токены: OpenAI → usage.prompt_tokens_details.cached_tokens;
                        # OpenRouter/Anthropic → usage.cache_read_input_tokens / cached_tokens.
                        _details = usage.get("prompt_tokens_details") or {}
                        _cached = int(_details.get("cached_tokens") or 0) if isinstance(_details, dict) else 0
                        if not _cached:
                            _cached = int(usage.get("cache_read_input_tokens")
                                          or usage.get("cached_tokens") or 0)
                        _record_llm_usage(
                            endpoint=endpoint.get("name") or "?",
                            model=model,
                            prompt_tokens=int(usage.get("prompt_tokens") or 0),
                            completion_tokens=int(usage.get("completion_tokens") or 0),
                            total_tokens=int(usage.get("total_tokens") or 0),
                            cached_tokens=_cached,
                            purpose_override=purpose,
                        )
                    except Exception as _e:
                        logger.debug(f"_record_llm_usage failed (non-fatal): {_e}")
                    return True, content, "ok"
                return False, "Empty content", "hard"

            # Auth → endpoint is misconfigured for THIS process, blacklist forever.
            if r.status_code in (401, 403):
                return False, f"HTTP {r.status_code} {(r.text or '')[:120]}", "auth"

            if r.status_code in _TRANSIENT_HTTP:
                last = f"HTTP {r.status_code} ({(r.text or '')[:120]})"
                if attempt < retries_per_model:
                    wait = min(2 * (2 ** attempt), 30)
                    logger.warning(f"[Claude] {endpoint['name']}/{model}: {last} — "
                                   f"retry {attempt+1}/{retries_per_model+1} in {wait}s")
                    _time.sleep(wait)
                    continue
                return False, last, "transient"

            if r.status_code == 400:
                kind = _classify_400(r.text)
                if kind == "unknown_model":
                    return False, (r.text or "")[:200], "unknown_model"
                if use_images:
                    return False, (r.text or "")[:200], "vision_fail"
                return False, f"HTTP 400 {(r.text or '')[:200]}", "hard"

            if r.status_code in (415, 422) and use_images:
                return False, (r.text or "")[:200], "vision_fail"

            return False, f"HTTP {r.status_code} {(r.text or '')[:200]}", "hard"

        return False, last, "transient"

    last_err = None
    tried_anything = False
    # Promote recently-healthy endpoints to the front so we don't waste time on
    # the broken one when we already know which proxy is alive.
    ordered = endpoints if _respect_chain_order else _ordered_endpoints(endpoints)
    for endpoint in ordered:
        base_url = endpoint.get("base_url")
        if not base_url or not endpoint.get("models"):
            continue
        if _is_endpoint_dead(base_url):
            logger.info(f"[Claude] skip {endpoint['name']!r} — marked dead this session "
                        f"({_DEAD_ENDPOINTS.get(base_url, '?')})")
            continue
        if _is_endpoint_in_cooldown(base_url):
            remaining = _endpoint_cooldown_remaining(base_url)
            logger.info(f"[Claude] skip {endpoint['name']!r} — cooldown {remaining}s remaining")
            continue
        unhealthy_count = 0
        endpoint_failed_transient = False
        for model in endpoint["models"]:
            if _is_url_model_blacklisted(base_url, model):
                logger.info(f"[Claude] skip {endpoint['name']}/{model} — url-model blacklist")
                continue
            modes = [True, False] if images else [False]
            for use_images in modes:
                tried_anything = True
                ok, content_or_err, kind = _try_call(endpoint, model, use_images)
                if ok:
                    _report_endpoint_call_success(base_url)
                    suffix = " (vision)" if use_images else " (text-only)"
                    logger.info(f"[Claude] ✓ {endpoint['name']}/{model}{suffix}"
                                f"{(' [' + purpose + ']') if purpose else ''}, "
                                f"{len(content_or_err)} chars")
                    # Запоминаем, какой endpoint/model/purpose реально ответил — чтобы
                    # в логе/мете было видно, какой tier отработал (а не молчаливый
                    # откат дизайна на самую дешёвую модель).
                    with _CLAUDE_HEALTH_LOCK:
                        _LAST_CLAUDE_ROUTE["endpoint"] = endpoint["name"]
                        _LAST_CLAUDE_ROUTE["model"] = model
                        _LAST_CLAUDE_ROUTE["purpose"] = purpose
                    return content_or_err
                last_err = f"{endpoint['name']}/{model}: {content_or_err}"
                if kind == "auth":
                    # Endpoint misconfigured — kill it for the whole session.
                    _mark_endpoint_dead(base_url, content_or_err[:120])
                    unhealthy_count = dead_endpoint_threshold  # force endpoint break below
                    break
                if kind == "unknown_model":
                    _blacklist_url_model(base_url, model, "400 unknown_model")
                    break  # next model on same endpoint
                # Both timeouts and HTTP transients (502/503/504/429) count toward
                # "this endpoint is unhealthy right now". Bail to the next endpoint
                # before burning through every model on a known-bad proxy.
                if kind in ("timeout", "transient"):
                    unhealthy_count += 1
                    endpoint_failed_transient = True
                if kind == "vision_fail":
                    logger.warning(f"[Claude] {endpoint['name']}/{model} vision не поддерживается — "
                                   f"fallback text-only")
                    continue  # retry same model text-only
                break  # next model
            if unhealthy_count >= dead_endpoint_threshold:
                logger.warning(f"[Claude] endpoint {endpoint['name']!r} unhealthy "
                               f"({unhealthy_count} consec transient/timeout) — "
                               f"переключаюсь на следующий endpoint")
                break
        if endpoint_failed_transient and unhealthy_count >= dead_endpoint_threshold:
            # Whole call cycle on this endpoint failed transiently — record toward cooldown.
            _report_endpoint_call_fail(base_url)

    if not tried_anything:
        raise RuntimeError(
            "Claude: все endpoints либо мёртвые либо в cooldown. "
            "Проверь config.json (api_key) или вызови reset_claude_health() и попробуй снова."
        )
    raise RuntimeError(f"Claude API недоступен: {last_err}")


def _claude_chat_persistent(cfg, system_prompt, user_prompt, images=None,
                             max_tokens=6000, temperature=0.7,
                             only_endpoints=None, purpose=None,
                             total_budget_seconds: float = 90.0,
                             max_chain_passes: int = 5,
                             pause_between_passes: float = 4.0,
                             reset_health_first: bool = True):
    """Версия `_claude_chat` с долбёжкой: прогоняет ВСЮ цепочку endpoint×model
    до `max_chain_passes` раз в пределах `total_budget_seconds`, с короткой
    паузой между прогонами. Между проходами cooldown очищается, чтобы прошлый
    транзиентный сбой не маскировал живой endpoint.

    Используется там, где «один промах = провал шага» неприемлемо (например,
    /api/detect-category — это входная точка, и если она молча падает, юзер
    видит «Вижу: . Категория: .»).
    """
    if reset_health_first:
        # Стираем сессионные блек-листы и cooldown — раз пользователь явно
        # просит ещё попытку, не подсовываем ему 180-секундный cooldown с
        # прошлого транзиентного сбоя.
        reset_claude_health()

    deadline = _time.monotonic() + max(5.0, total_budget_seconds)
    last_err = "no attempts made"
    for pass_idx in range(1, max(1, max_chain_passes) + 1):
        try:
            return _claude_chat(
                cfg, system_prompt, user_prompt, images=images,
                max_tokens=max_tokens, temperature=temperature,
                # Внутри ОДНОГО прохода уже не экономим: 3 ретрая на модель,
                # 3 транзиентных промаха на endpoint до переключения.
                retries_per_model=3, dead_endpoint_threshold=3,
                only_endpoints=only_endpoints, purpose=purpose,
            )
        except RuntimeError as e:
            last_err = str(e)
            remaining = deadline - _time.monotonic()
            if remaining <= 0 or pass_idx >= max_chain_passes:
                logger.warning(f"[Claude persistent] {pass_idx} проходов исчерпаны: {last_err[:160]}")
                raise
            wait = min(pause_between_passes, max(1.0, remaining * 0.2))
            logger.info(f"[Claude persistent] проход {pass_idx} провалился ({last_err[:120]}); "
                        f"жду {wait:.1f}с и крутим цепочку заново (осталось {remaining:.0f}с)")
            _time.sleep(wait)
            # Перед новым проходом снова стираем cooldown — endpoint мог
            # «оклематься» за эти секунды.
            with _CLAUDE_HEALTH_LOCK:
                _ENDPOINT_HEALTH.clear()
    raise RuntimeError(f"Claude API недоступен после {max_chain_passes} проходов: {last_err}")


# ─── Pack design ─────────────────────────────────────────────────────────────

# style_key values that mean "user did NOT deliberately pick a style" → free to
# rotate a fresh art direction each generation (so runs don't all look the same).
_AUTO_STYLE_KEYS = {"", None, "premium", "auto", "default"}


def _pick_art_direction(hint_text: str = ""):
    """Pick ONE art direction for this generation.
    Раньше: чистый рандом (uuid % 10), игнорировал товар → пульт попадал на
    «дуб+листву» (не по теме) и шанс попасть в тему = 1/10.
    Теперь: если по категории/брифу/бренду есть подходящий пул направлений —
    выбираем ИЗ него (с вариативностью: повторные прогоны всё равно разные),
    иначе рандом по всему пулу. Внутри пака выбор один → карточки согласованы.
    Returns (key, direction_dict)."""
    all_keys = fmt.list_art_direction_keys()
    candidates = []
    try:
        candidates = [k for k in fmt.art_directions_for_category(hint_text) if k in all_keys]
    except Exception as e:
        logger.debug(f"_pick_art_direction: category map failed: {e}")
    pool = candidates or all_keys
    key = pool[uuid.uuid4().int % len(pool)]
    return key, fmt.ART_DIRECTIONS[key]


def _design_pack(cfg, mode, payload, style_key, product_refs, style_refs, palette,
                 user_facts=None):
    """mode: 'advanced' | 'brief' | 'competitor'.

    payload shapes:
      advanced:   (product_dict, formats_list, card_plan_dict)
      brief:      (brief_str, formats_list, card_count)
      competitor: (brief_str, formats_list, card_count, competitor_analysis_dict, category_str)

    user_facts: optional dict with confirmed product facts (volume, material, etc).
                If provided — Claude can ONLY use these for concrete claims on cards.
                If absent/empty — Claude must avoid inventing specific numbers.
    """
    has_style_ref = bool(style_refs)
    ref_names = [r["name"] for r in product_refs]

    # Текст-подсказка для выбора арт-направления по теме товара (категория/бриф/бренд).
    def _ad_hint():
        try:
            if mode == "advanced" and isinstance(payload[0], dict):
                p = payload[0]
                return " ".join(str(p.get(k) or "") for k in ("category", "name", "brand"))
            if mode == "competitor":
                return f"{payload[4] or ''} {payload[0] or ''}"   # category_confirmed + brief
            return str(payload[0] or "")                          # brief text
        except Exception:
            return ""

    # ── Per-generation ART DIRECTION ─────────────────────────────────────────
    # If the user neither attached a style ref nor deliberately picked a style,
    # rotate a fresh art direction each run so different generations look different
    # (was: always dark «premium»). One direction per pack → cards stay consistent.
    # Выбор теперь учитывает КАТЕГОРИЮ товара (см. _pick_art_direction), чтобы
    # стиль был по теме, а не случайным «дуб+листва на пульт».
    art_direction_directive = ""
    resolved_ad = None          # {"key","accent","secondary"} — для меты/тэйла/лога
    if not has_style_ref and (style_key in _AUTO_STYLE_KEYS):
        ad_key, ad = _pick_art_direction(_ad_hint())
        style_key = ad_key                 # logs/_meta now reflect the real direction
        style_desc = ad["signature"]
        # only override the palette if the caller left the default pink/navy
        if isinstance(palette, dict) and palette.get("accent") in (None, "#FF4D6D") \
                and palette.get("secondary") in (None, "#1B1B2F"):
            palette = {**palette, "accent": ad["accent"], "secondary": ad["secondary"]}
        art_direction_directive = P.ART_DIRECTION_DIRECTIVE.format(
            signature=ad["signature"],
            accent=palette.get("accent", ad["accent"]),
            secondary=palette.get("secondary", ad["secondary"]),
        )
        resolved_ad = {"key": ad_key, "accent": ad["accent"], "secondary": ad["secondary"]}
        logger.info(f"[design] 🎨 Art-direction этого прогона: {ad_key}")
    else:
        style_desc = fmt.STYLES.get(style_key, "Премиум") if style_key else "Премиум"

    if mode == "advanced":
        product, formats_list, card_plan = payload
        user_prompt = P.build_user_prompt(
            product=product, formats=formats_list, card_plan=card_plan,
            style_key=style_key, style_desc=style_desc,
            has_refs=bool(product_refs), ref_names=ref_names, palette=palette,
            has_style_ref=has_style_ref, user_facts=user_facts,
            art_direction_directive=art_direction_directive,
        )
        system_prompt = P.SYSTEM_PROMPT
    elif mode == "competitor":
        brief, formats_list, card_count, competitor_analysis, category_confirmed = payload
        user_prompt = P.build_competitor_brief_user_prompt(
            brief=brief, formats=formats_list, card_count=card_count,
            style_key=style_key, style_desc=style_desc,
            has_refs=bool(product_refs), ref_names=ref_names, palette=palette,
            competitor_analysis=competitor_analysis,
            category_confirmed=category_confirmed or "",
            has_style_ref=has_style_ref,
            art_direction_directive=art_direction_directive,
        )
        # competitor-режим — добавляем facts-block отдельным куском
        # (т.к. build_competitor_brief_user_prompt не принимает user_facts)
        from prompts import _facts_block
        user_prompt = user_prompt + _facts_block(user_facts)
        system_prompt = P.COMPETITOR_BRIEF_SYSTEM_PROMPT
    else:  # brief
        brief, formats_list, card_count = payload
        user_prompt = P.build_brief_user_prompt(
            brief=brief, formats=formats_list, card_count=card_count,
            style_key=style_key, style_desc=style_desc,
            has_refs=bool(product_refs), ref_names=ref_names, palette=palette,
            has_style_ref=has_style_ref, user_facts=user_facts,
            art_direction_directive=art_direction_directive,
        )
        system_prompt = P.BRIEF_SYSTEM_PROMPT

    # Архитектурный intent: vision-анализ товара уже сделан ОТДЕЛЬНЫМ вызовом
    # detect_category_from_photos → product_description / extracted_text /
    # quick_facts / category уже лежат текстом в payload и user_facts. Раньше
    # мы при этом ПОВТОРНО слали product_refs как vision-картинки в design
    # (~2100 high-detail токенов на каждое фото). Это удваивало vision-бюджет
    # без новой информации и упирало COMPETITOR-режим в TPM-лимит OpenAI
    # (HTTP 429 "Request too large for gpt-4.1 in organization …").
    # Теперь design идёт TEXT-only по продукту.
    # style_ref — другой класс рефа: «сделай как на этой обложке». Это новая
    # визуальная информация, которой нет в брифе/распознавании. Оставляем,
    # но с detail="low" (85 токенов вместо ~2100) — для считывания вайба
    # (палитра/шрифт/композиция) этого хватает.
    images = [{"name": r["name"], "image": r.get("image_vision") or r["image"],
               "detail": "low"} for r in style_refs]

    def _try_design(text):
        design = P.extract_json(text)
        # Валидация ПОЛНОТЫ дизайна. extract_json умеет «починить» битый JSON до
        # парсящегося, но семантически пустого/повреждённого. Если дизайн неполный —
        # бросаем ValueError, чтобы сработал РЕТРАЙ (повторный запрос модели), а НЕ
        # генерация кривых фото из мусора. Картинки (дорогие токены) рендерятся ПОСЛЕ
        # дизайна — поэтому фейл здесь = пользователь НЕ тратит токены на «говнофото».
        cards = design.get("cards")
        if not isinstance(cards, list) or not cards:
            raise ValueError("Ответ не содержит непустой cards[]")
        has_headline = any(
            isinstance(c, dict) and str(
                c.get("headline_ru") or c.get("headline") or c.get("title") or ""
            ).strip()
            for c in cards
        )
        if not has_headline:
            raise ValueError("cards[] без единого заголовка — ответ повреждён, нужен повтор")
        has_style = bool(
            str(design.get("pack_scene_block") or design.get("pack_scene_template") or "").strip()
        ) or isinstance(design.get("extracted_style"), dict)
        if not has_style:
            raise ValueError("нет pack_scene_block/extracted_style — стиль пака не задан, нужен повтор")
        # Прокидываем выбранное арт-направление и реальный маршрут модели в дизайн —
        # чтобы _prepare_design мог записать их в _meta.json и лог (наблюдаемость).
        #
        # TONE-driven override: если Claude вернул _art_direction_key из каталога
        # (поле в системном промпте), это его осознанный выбор по TONE MATRIX —
        # он бьёт pre-Claude keyword-based pick. Это убирает зависимость от
        # хардкодных stem-списков для категорий и позволяет модели самой решать
        # «функциональная автохимия → tech_minimal_mono, не warm_lifestyle».
        # Pre-pick остаётся fallback'ом если Claude промахнулся / вернул мусор.
        claude_ad_key = str(design.get("_art_direction_key") or "").strip()
        if claude_ad_key and claude_ad_key in fmt.ART_DIRECTIONS:
            ad = fmt.ART_DIRECTIONS[claude_ad_key]
            override_ad = {
                "key": claude_ad_key,
                "accent": ad.get("accent"),
                "secondary": ad.get("secondary"),
            }
            if not resolved_ad or resolved_ad.get("key") != claude_ad_key:
                logger.info(
                    f"[design] 🎨 Art-direction override Claude'ом: "
                    f"{(resolved_ad or {}).get('key', '—')} → {claude_ad_key}"
                )
            design["_art_direction"] = override_ad
        elif resolved_ad:
            design["_art_direction"] = resolved_ad
        with _CLAUDE_HEALTH_LOCK:
            if _LAST_CLAUDE_ROUTE:
                design["_design_route"] = dict(_LAST_CLAUDE_ROUTE)
        return design

    # 1) Try Claude ONCE (fast fail — retries_per_model=1, low threshold)
    try:
        text = _claude_chat(cfg, system_prompt, user_prompt,
                            images=images, max_tokens=8000, purpose="design",
                            retries_per_model=1, dead_endpoint_threshold=1)
        return _try_design(text)
    except (ValueError, RuntimeError) as e:
        logger.warning(f"[design] Claude attempt 1 failed: {e}")

    # 2) Вторая попытка по тому же smart-tier'у (цепочка сама уйдёт в фолбэк-модель
    #    внутри tier'а: gpt-4.1 → opus → gpt-4o — НЕ на дешёвый mini).
    logger.info("[design] Retry on smart tier")
    try:
        text = _claude_chat(cfg, system_prompt, user_prompt,
                            images=images, max_tokens=8000, purpose="design")
        return _try_design(text)
    except (ValueError, RuntimeError) as e:
        logger.warning(f"[design] smart-tier retry failed: {e}")

    # 3) Last resort: ещё одна попытка smart-tier с полными ретраями
    logger.info("[design] Last-resort smart-tier attempt with full retries")
    text = _claude_chat(cfg, system_prompt, user_prompt,
                        images=images, max_tokens=8000, purpose="design")
    return _try_design(text)


# ════════════════════════════════════════════════════════════════════════════
# SPLIT режим: text-agent (Stage 1) → visual-agent (Stage 2) → merge.
# Включается через config.design_split = true. Идея: каждый агент сфокусирован,
# тексты получаются сильнее (text-agent не отвлекается на scene/style),
# визуал точнее (visual-agent видит финальный хук каждой карточки и привязывает
# сцену к нему). См. TEXT_DESIGN_SYSTEM_PROMPT / VISUAL_DESIGN_SYSTEM_PROMPT.
# ════════════════════════════════════════════════════════════════════════════
def _design_pack_split(cfg, mode, payload, style_key, product_refs, style_refs,
                       palette, user_facts=None, step=None):
    """SPLIT-двухпроходная архитектура design'а.

    Возвращает dict совместимый со single-shot _design_pack: cards[] с
    headline_ru/sub_ru/badges_ru/scene_override, pack_scene_block, extracted_style.

    Если mode='advanced' (старый редкий путь) — fallback на single-shot,
    SPLIT поддерживает COMPETITOR и BRIEF.
    """
    if mode == "advanced":
        return _design_pack(cfg, mode, payload, style_key, product_refs,
                            style_refs, palette, user_facts=user_facts)

    _step = step if callable(step) else (lambda *a, **k: None)
    has_style_ref = bool(style_refs)
    ref_names = [r["name"] for r in product_refs]

    # ── общий visual-контекст (берётся для обоих stage по необходимости) ─────
    def _ad_hint():
        try:
            if mode == "competitor":
                return f"{payload[4] or ''} {payload[0] or ''}"
            return str(payload[0] or "")
        except Exception:
            return ""

    art_direction_directive = ""
    resolved_ad = None
    if not has_style_ref and (style_key in _AUTO_STYLE_KEYS):
        ad_key, ad = _pick_art_direction(_ad_hint())
        style_key = ad_key
        style_desc = ad["signature"]
        if isinstance(palette, dict) and palette.get("accent") in (None, "#FF4D6D") \
                and palette.get("secondary") in (None, "#1B1B2F"):
            palette = {**palette, "accent": ad["accent"], "secondary": ad["secondary"]}
        art_direction_directive = P.ART_DIRECTION_DIRECTIVE.format(
            signature=ad["signature"], accent=palette.get("accent", ad["accent"]),
            secondary=palette.get("secondary", ad["secondary"]),
        )
        resolved_ad = {"key": ad_key, "accent": ad["accent"], "secondary": ad["secondary"]}
        _step(f"🎨 Арт-направление этого прогона: {ad_key}")
    else:
        style_desc = fmt.STYLES.get(style_key, "Премиум") if style_key else "Премиум"

    # ── STAGE 1: text-agent ───────────────────────────────────────────────────
    _step("📝 Stage 1 — копирайтер пишет тексты карточек…")

    if mode == "competitor":
        brief, formats_list, card_count, competitor_analysis, category_confirmed = payload
        text_user_prompt = P.build_competitor_brief_user_prompt(
            brief=brief, formats=formats_list, card_count=card_count,
            style_key=style_key, style_desc=style_desc,
            has_refs=bool(product_refs), ref_names=ref_names, palette=palette,
            competitor_analysis=competitor_analysis,
            category_confirmed=category_confirmed or "",
            has_style_ref=has_style_ref,
            art_direction_directive="",   # визуал — НЕ работа text-agent
            text_only=True,               # Stage 1 = копирайтер → без визуального арт-каталога
        )
        text_user_prompt = text_user_prompt + P._facts_block(user_facts)
    else:  # brief
        brief, formats_list, card_count = payload
        competitor_analysis = {}
        category_confirmed = ""
        text_user_prompt = P.build_brief_user_prompt(
            brief=brief, formats=formats_list, card_count=card_count,
            style_key=style_key, style_desc=style_desc,
            has_refs=bool(product_refs), ref_names=ref_names, palette=palette,
            has_style_ref=False, user_facts=user_facts,
            art_direction_directive="",   # визуал — НЕ работа text-agent
        )

    # text-agent: vision НЕ нужен (распознавание уже сделано detect_category)
    try:
        text_stage1 = _claude_chat(
            cfg, P.TEXT_DESIGN_SYSTEM_PROMPT, text_user_prompt,
            images=None, max_tokens=4000, temperature=0.7,
            purpose="text_design", retries_per_model=2,
        )
        stage1 = P.extract_json(text_stage1)
    except Exception as e:
        _step(f"⚠️ Stage 1 упал ({type(e).__name__}: {str(e)[:120]}) → fallback на single-shot")
        return _design_pack(cfg, mode, payload, style_key, product_refs,
                            style_refs, palette, user_facts=user_facts)

    stage1_cards = stage1.get("cards") if isinstance(stage1, dict) else None
    if not isinstance(stage1_cards, list) or not stage1_cards:
        _step("⚠️ Stage 1 не вернул cards[] → fallback на single-shot")
        return _design_pack(cfg, mode, payload, style_key, product_refs,
                            style_refs, palette, user_facts=user_facts)

    _step(f"✓ Stage 1 готов: {len(stage1_cards)} карточек, "
          f"hooks={[c.get('hook_formula') for c in stage1_cards if isinstance(c, dict)]}")

    # ── STAGE 2: visual-agent ─────────────────────────────────────────────────
    _step("🎨 Stage 2 — арт-директор проектирует сцены под хук каждой карточки…")
    visual_user_prompt = P.build_visual_design_user_prompt(
        cards_from_stage1=stage1_cards,
        pack_concept=str(stage1.get("pack_concept") or ""),
        brief=(payload[0] if mode in ("brief", "competitor") else ""),
        category_confirmed=category_confirmed,
        competitor_analysis=competitor_analysis,
        has_refs=bool(product_refs), ref_names=ref_names,
        has_style_ref=has_style_ref,
        palette=palette,
        art_direction_directive=art_direction_directive,
        user_facts=user_facts,
    )

    # visual-agent — vision ТОЛЬКО для style_ref (detail=low, ~85 токенов)
    visual_images = [
        {"name": r["name"], "image": r.get("image_vision") or r["image"], "detail": "low"}
        for r in style_refs
    ] or None

    try:
        text_stage2 = _claude_chat(
            cfg, P.VISUAL_DESIGN_SYSTEM_PROMPT, visual_user_prompt,
            images=visual_images, max_tokens=4000, temperature=0.7,
            purpose="visual_design", retries_per_model=2,
        )
        stage2 = P.extract_json(text_stage2)
    except Exception as e:
        _step(f"⚠️ Stage 2 упал ({type(e).__name__}: {str(e)[:120]}) → дизайн без scene_block")
        stage2 = {}

    # ── MERGE ─────────────────────────────────────────────────────────────────
    overrides_by_id = {}
    for ov in (stage2.get("scene_overrides") or []):
        if isinstance(ov, dict) and ov.get("id"):
            overrides_by_id[str(ov["id"])] = str(ov.get("scene_override") or "").strip()

    merged_cards = []
    for c in stage1_cards:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        # копируем текстовые поля от Stage 1
        merged = {
            "id": c.get("id"),
            "type": c.get("type"),
            "format_key": c.get("format_key"),
            "headline_ru": c.get("headline_ru"),
            "sub_ru": c.get("sub_ru"),
            "badges_ru": c.get("badges_ru") or [],
            "footer_ru": c.get("footer_ru"),
            "hook_formula": c.get("hook_formula"),
            "role_in_story": c.get("role_in_story"),
            "competitor_insight": c.get("competitor_insight"),
            # focus / focus_kind от Stage 1 — ЧТО в кадре detail-карточки (макро части /
            # масштаб / среда / комплектация). Без них _build_image_prompt не различит
            # детальные карточки, и они выйдут как обложка (FIX 3).
            "focus": c.get("focus"),
            "focus_kind": c.get("focus_kind"),
            # scene_override от Stage 2
            "scene_override": overrides_by_id.get(cid, ""),
        }
        merged_cards.append(merged)
    # FIX 3 — дедуп кадров вынесен в общий _diversify_detail_cards (см. ниже),
    # вызывается в _prepare_design/_stream_render → работает и на split, и на
    # single-shot/fallback пути.

    # Имена полей: используем «канонические» (без pack_ префикса) — downstream
    # обработка в _prepare_design ищет design["product_block"] / design["concept"].
    # Если возвращать pack_product_block / pack_concept, downstream подставляет
    # generic-описание → image-API получает менее точные сцены.
    design = {
        "concept":            stage1.get("pack_concept") or "",
        "inferred_product":   stage1.get("inferred_product") or {},
        "uncertainties":      stage1.get("uncertainties") or [],
        "cards":              merged_cards,
        "pack_scene_block":   stage2.get("pack_scene_block") or "",
        "product_block":      stage2.get("pack_product_block") or "",
        "extracted_style":    stage2.get("extracted_style") or {},
    }
    # Код-сигналы Stage 1 для plan-cards/фронта. Раньше merge их ронял → в
    # split-режиме (прод) plan-cards ВСЕГДА отдавал missing_critical_facts=[]
    # и фича «мягко попросить юзера дополнить факты» не могла сработать.
    for _sig in ("missing_critical_facts", "_mind_state", "_buyer_questions", "_niche_voice"):
        if stage1.get(_sig):
            design[_sig] = stage1[_sig]

    # Art-direction override: visual-agent мог выбрать другой ключ из каталога
    claude_ad_key = str(stage2.get("art_direction_key") or "").strip()
    if claude_ad_key and claude_ad_key in fmt.ART_DIRECTIONS:
        ad = fmt.ART_DIRECTIONS[claude_ad_key]
        override_ad = {
            "key": claude_ad_key,
            "accent": ad.get("accent"),
            "secondary": ad.get("secondary"),
        }
        if not resolved_ad or resolved_ad.get("key") != claude_ad_key:
            logger.info(f"[design-split] 🎨 Art-direction override Stage 2: "
                        f"{(resolved_ad or {}).get('key', '—')} → {claude_ad_key}")
        design["_art_direction"] = override_ad
    elif resolved_ad:
        design["_art_direction"] = resolved_ad

    with _CLAUDE_HEALTH_LOCK:
        if _LAST_CLAUDE_ROUTE:
            design["_design_route"] = dict(_LAST_CLAUDE_ROUTE)
    design["_design_route_split"] = True   # marker for observability/_meta.json

    _step(f"✓ Stage 2 готов: pack_scene_block={len(design['pack_scene_block'])} chars, "
          f"scene_overrides={sum(1 for c in merged_cards if c.get('scene_override'))}/{len(merged_cards)}")
    return design


# ─── Image clients ───────────────────────────────────────────────────────────

_VEO_CLIENT = None
_FASTGEN_CLIENT = None
_FASTGEN_VIDEO_CLIENT = None
_OPENAI_IMG_CLIENT = None
_OPENAI_IMG_FINGERPRINT = None  # (api_key_tail, base_url, proxy_url) — для пересборки на reload


def _get_veo(cfg):
    global _VEO_CLIENT
    if _VEO_CLIENT is None:
        VeoCls = _import_veo_client(cfg["vidinator_core_path"])
        _VEO_CLIENT = VeoCls(cfg["veo_api_key"], max_concurrent=cfg.get("veo_max_concurrent", 8))
        if not _VEO_CLIENT.is_available():
            raise RuntimeError("VeoNonStop недоступен — проверь veo_api_key в config.json")
    return _VEO_CLIENT


def _get_fastgen(cfg):
    """Singleton FastGenClient с поддержкой proxy.

    Источники proxy_url по приоритету (выигрывает первый непустой):
      1) переменная окружения FASTGEN_PROXY_URL — быстро перебить локально
         (PowerShell: $env:FASTGEN_PROXY_URL = "http://user:pass@ip:port")
      2) config.json → "fastgen_proxy_url"

    Список провайдеров для проксирования:
      config.json → "fastgen_proxy_for_providers": ["fastgen_openai"]
      Если null/пусто — прокси применяется ко ВСЕМ FastGen запросам.

    Сейчас по умолчанию: ISP HTTP прокси (176.106.62.248) → только для
    fastgen_openai. Nano Pro / Gemini идут напрямую без прокси.
    """
    global _FASTGEN_CLIENT
    if _FASTGEN_CLIENT is None:
        proxy_url = os.environ.get("FASTGEN_PROXY_URL") or cfg.get("fastgen_proxy_url") or None
        prov_list = cfg.get("fastgen_proxy_for_providers")
        proxy_for_providers = set(prov_list) if isinstance(prov_list, list) and prov_list else None

        # Глобальный рубильник прокси нейронки (админ-панель). Включён → единый
        # прокси на ВСЕ FastGen-провайдеры; выключен → напрямую. Перебивает всё выше.
        _ap = cfg.get("ai_proxy") if isinstance(cfg, dict) else None
        if isinstance(_ap, dict) and _ap.get("enabled") is not None:
            _eff = effective_ai_proxy(cfg, proxy_url or "")
            proxy_url = _eff or None
            if proxy_url:
                proxy_for_providers = None  # единый прокси на всё

        _FASTGEN_CLIENT = FastGenV5Client(
            cfg["fastgen_api_key"], cfg.get("fastgen_base_url"),
            proxy_url=proxy_url,
            proxy_for_providers=proxy_for_providers,
        )

        if proxy_url:
            # маскируем пароль в логе
            log_url = proxy_url
            if "@" in log_url:
                scheme, _, rest = log_url.partition("://")
                creds, _, host = rest.partition("@")
                u = creds.split(":", 1)[0]
                log_url = f"{scheme}://{u}:***@{host}"
            scope = (f"только {sorted(proxy_for_providers)}"
                     if proxy_for_providers else "ВСЕ провайдеры")
            src = "env" if os.environ.get("FASTGEN_PROXY_URL") else "config.json"
            logger.info(f"FastGen: прокси настроен ({scope}) ← {src}: {log_url}")
        else:
            logger.info("FastGen: прокси НЕ настроен — все запросы идут напрямую")

        if not _FASTGEN_CLIENT.is_available():
            raise RuntimeError("FastGen недоступен — проверь fastgen_api_key в config.json")
    return _FASTGEN_CLIENT


def _get_fastgen_video(cfg):
    global _FASTGEN_VIDEO_CLIENT
    if _FASTGEN_VIDEO_CLIENT is None:
        _FASTGEN_VIDEO_CLIENT = FastGenVideoClient(cfg["fastgen_api_key"], cfg.get("fastgen_base_url"))
        if not _FASTGEN_VIDEO_CLIENT.is_available():
            raise RuntimeError("FastGen video недоступен — проверь fastgen_api_key")
    return _FASTGEN_VIDEO_CLIENT


def _resolve_openai_image_key(cfg):
    """Берём (api_key, proxy_url) для прямого gpt-image-2.

    Приоритет ключа:
      1) cfg.openai_image_api_key  — отдельный ключ под Image API
      2) ключ из claude_endpoints с name='openai-fallback' (или first, чей base_url содержит api.openai.com)
    Это даёт админу выбор: вынести Image API на отдельный organization, либо
    переиспользовать LLM-ключ (он уже залит в claude_endpoints).
    """
    key = (cfg.get("openai_image_api_key") or "").strip()
    proxy = (cfg.get("openai_image_proxy_url") or "").strip() or None
    result_key, result_proxy = "", proxy
    if key:
        result_key, result_proxy = key, proxy
    else:
        for ep in cfg.get("claude_endpoints", []):
            name = (ep.get("name") or "").lower()
            base = (ep.get("base_url") or "").lower()
            if name == "openai-fallback" or "api.openai.com" in base:
                k = (ep.get("api_key") or "").strip()
                if k:
                    # прокси берём из того же endpoint, если своего нет
                    result_key, result_proxy = k, proxy or (ep.get("proxy_url") or None)
                    break
    # Глобальный рубильник прокси нейронки (админ-панель) — перебивает всё выше.
    _ap = cfg.get("ai_proxy") if isinstance(cfg, dict) else None
    if isinstance(_ap, dict) and _ap.get("enabled") is not None:
        result_proxy = effective_ai_proxy(cfg, result_proxy or "") or None
    return result_key, result_proxy


def reset_openai_image_client() -> None:
    """Сбрасываем singleton — вызывает _reload_server_cfg при PUT /api/admin/settings,
    чтобы новые ключ/прокси/base_url подхватились без рестарта."""
    global _OPENAI_IMG_CLIENT, _OPENAI_IMG_FINGERPRINT
    _OPENAI_IMG_CLIENT = None
    _OPENAI_IMG_FINGERPRINT = None


def reset_fastgen_clients() -> None:
    """Сброс FastGen-синглтонов — зовётся _reload_server_cfg при сохранении
    настроек в админке. Без этого смена ai_proxy / fastgen_proxy_url доезжала
    до картинок только после рестарта процесса: LLM-вызовы уже шли через новый
    прокси (effective_ai_proxy читается на каждый вызов), а _FASTGEN_CLIENT
    навсегда держал прокси, зашитый при первом вызове, — со стороны админки
    это выглядело как «прокси слетает/не применяется»."""
    global _FASTGEN_CLIENT, _FASTGEN_VIDEO_CLIENT
    _FASTGEN_CLIENT = None
    _FASTGEN_VIDEO_CLIENT = None


def _get_openai_image(cfg):
    """Singleton OpenAIImageClient (прямой gpt-image-2).

    quality/size читаются из cfg НА КАЖДЫЙ вызов (см. _render_card_image_raw) —
    поэтому пересоздавать клиент при их смене не нужно. Пересоздаём только при
    смене ключа/прокси/base_url (отслеживаем через fingerprint)."""
    global _OPENAI_IMG_CLIENT, _OPENAI_IMG_FINGERPRINT
    api_key, proxy_url = _resolve_openai_image_key(cfg)
    base_url = (cfg.get("openai_image_base_url") or "https://api.openai.com/v1").rstrip("/")
    model = cfg.get("openai_image_model") or "gpt-image-2"
    fp = ((api_key or "")[-6:], base_url, proxy_url or "", model)
    if _OPENAI_IMG_CLIENT is not None and _OPENAI_IMG_FINGERPRINT == fp:
        return _OPENAI_IMG_CLIENT
    if not api_key:
        raise RuntimeError(
            "OpenAI Image API ключ не задан — заполните openai_image_api_key в админке "
            "(или включите openai-fallback endpoint в claude_endpoints)."
        )
    _OPENAI_IMG_CLIENT = OpenAIImageClient(
        api_key=api_key, base_url=base_url, model=model, proxy_url=proxy_url,
    )
    _OPENAI_IMG_FINGERPRINT = fp
    if proxy_url:
        scheme, _, rest = proxy_url.partition("://")
        creds, _, host = rest.partition("@") if "@" in rest else ("", "", rest)
        u = creds.split(":", 1)[0] if creds else ""
        log_url = f"{scheme}://{u}:***@{host}" if creds else proxy_url
        logger.info(f"OpenAI Image client: model={model} proxy={log_url}")
    else:
        logger.info(f"OpenAI Image client: model={model} (без прокси)")
    return _OPENAI_IMG_CLIENT


# (Единый _PROMPT_TAIL удалён — финальный хвост теперь свой у каждой арт-дирекции,
#  см. _prompt_tail() + formats.ART_DIRECTION_MOODS / ART_QUALITY_TAIL.)

# Regex matching photographic technical tokens that image AIs often render as visible labels.
_TECH_TOKEN_RE = re.compile(
    r'\bf/[\d.]+\b'    # f/1.8, f/2.8, f/11
    r'|\b\d+°'         # 45°, 30°, 90°
    r'|\b\d{4,5}K\b'   # 5500K, 6500K (color temperature)
    r'|\bISO\s*\d+'    # ISO 3200, ISO800
    r'|\b\d+mm\b'      # 85mm, 50mm (focal length)
)


def _sanitize_tech_tokens(text: str) -> str:
    """Remove photographic technical tokens that image AIs render as visible text labels."""
    cleaned = _TECH_TOKEN_RE.sub('', text)
    return re.sub(r'  +', ' ', cleaned).strip()


_VARIATIONS_FORBID_TAIL = (
    "[prompt omitted from public repo -- see docs/prompt-system.md]"
)


# ════════════════════════════════════════════════════════════════════════════
# FALLBACK CHAINS — adapted from Vidinator_Structured/core/gemini_image_generator.py
# When primary provider fails, the orchestrator walks the chain. Each entry =
# (provider_name, censorship_limit) — number of content-policy rejections
# tolerated before moving to the next provider in the chain.
# ════════════════════════════════════════════════════════════════════════════

# V5 image stack — ровно три живых провайдера, все с поддержкой реф-фото:
#   fastgen_nanopro     → Nano Banana Pro (ДЕФОЛТ: лучшая идентичность товара, 4 кредита)
#   fastgen_openai      → ChatGPT Image  (фолбэк, лучший рендер кириллицы, 1 кредит)
#   fastgen_nanobanana2 → Nano Banana 2   (4 кредита)
# Стоимость кредитов признана несущественной (решение от 2026-06: «картинки = 0»),
# поэтому дефолт выбран чисто по качеству. При падении основного цепочка перебирает
# два других — рефы переносятся как есть.
_LIVE_PROVIDERS = ("fastgen_nanopro", "fastgen_openai", "fastgen_nanobanana2")
_DEFAULT_PRIMARY = "fastgen_nanopro"   # Nano holds product identity on ANY product (verified)

def _build_chain(primary: str) -> list[tuple[str, int]]:
    """Primary провайдер первый, потом два других живых."""
    if primary not in _LIVE_PROVIDERS:
        primary = _DEFAULT_PRIMARY
    others = [p for p in _LIVE_PROVIDERS if p != primary]
    # высокий censorship_limit — даём шанс много раз
    return [(primary, 6)] + [(p, 4) for p in others]

FALLBACK_CHAINS = {p: _build_chain(p) for p in _LIVE_PROVIDERS}
# Совместимость со старыми ключами: gemini-слот = Nano Banana 2, остальное → основной.
FALLBACK_CHAINS["fastgen_gemini"] = _build_chain("fastgen_nanobanana2")
for _legacy in ("veononstop", "fastgen_flower", "fastgen_imagen",
                "fastgen_grok", "fastgen_whisk_remix"):
    FALLBACK_CHAINS[_legacy] = _build_chain(_DEFAULT_PRIMARY)

# Прямой OpenAI gpt-image-2 — ИЗОЛИРОВАННАЯ цепочка. openai_direct появляется
# только когда юзер сам его выбрал в админке (default_provider="openai_direct"),
# и в fastgen-цепочки его НЕ кладём — чтобы кошелёк не утекал, когда юзер думает
# «я выбрал fastgen». На случай падения OpenAI (auth/rate/server) спокойно
# уходим в fastgen_nanopro→openai-image (FastGen) — рефы передадутся как есть.
FALLBACK_CHAINS["openai_direct"] = [
    ("openai_direct",       6),
    ("fastgen_nanopro",     4),
    ("fastgen_openai",      4),
]

# Hard limits across the entire chain for one card.
_FALLBACK_MAX_TOTAL_REWRITES = 4   # max prompt rewrites across whole chain
_FALLBACK_MAX_TOTAL_SERVER_ERRORS = 40    # больше "бюджета" для занятых провайдеров
_FALLBACK_MAX_CONSECUTIVE_RATE_LIMIT = 6  # ждём долго — потоки могут освободиться
_FALLBACK_MAX_CONSECUTIVE_SERVER = 8      # 8 ретраев на ТОТ ЖЕ провайдер до переключения
_FALLBACK_PROVIDER_DEAD_THRESHOLD = 8     # tracker marks provider unavailable past this


class _ProviderTracker:
    """Per-pack tracker. Counts consecutive failures per provider so that
    once provider X has died ~8 times, subsequent cards in the same pack skip it
    immediately rather than wasting time on it."""

    def __init__(self):
        self.errors = {}
        self.lock = threading.Lock()

    def mark_error(self, provider: str) -> None:
        with self.lock:
            self.errors[provider] = self.errors.get(provider, 0) + 1

    def mark_success(self, provider: str) -> None:
        with self.lock:
            self.errors[provider] = 0

    def is_available(self, provider: str,
                     max_errors: int = _FALLBACK_PROVIDER_DEAD_THRESHOLD) -> bool:
        with self.lock:
            return self.errors.get(provider, 0) < max_errors

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.errors)


# ─── Prompt rewriter (for content-policy fallback) ──────────────────────────
_PROMPT_REWRITE_SYSTEM = """[prompt omitted from public repo -- see docs/prompt-system.md]"""


def _rewrite_prompt_softer(cfg: dict, original_prompt: str,
                            max_retries: int = 2) -> str:
    """Ask Claude to rewrite a prompt to be content-policy-safer. Returns the
    rewritten prompt, or empty string if rewrite failed / produced same text."""
    user = f"Rewrite this image prompt to be content-policy-safe:\n\n{original_prompt}"
    for _ in range(max(1, max_retries)):
        try:
            new = _claude_chat(cfg, _PROMPT_REWRITE_SYSTEM, user, images=None,
                                max_tokens=2000, temperature=0.4,
                                purpose="prompt_softer").strip()
            if new and len(new) >= max(50, int(len(original_prompt) * 0.5)) \
                    and new.strip().lower() != original_prompt.strip().lower():
                return new
        except Exception:
            continue
    return ""


def _format_orientation_hint(format_key: str) -> str:
    aspect = fmt.FORMATS.get(format_key, {}).get("aspect", "3:4")
    m = {
        "3:4":  "Vertical 3:4 product card.",
        "9:16": "Vertical 9:16 social-story card.",
        "1:1":  "Square 1:1 product card.",
        "4:3":  "Horizontal 4:3 banner card.",
        "16:9": "Horizontal 16:9 banner.",
    }
    return m.get(aspect, "Vertical 3:4 product card.")


_BADGE_POSITIONS = ["lower-left", "lower-right", "upper-right"]


# ── Раскладка заголовка ──────────────────────────────────────────────────────
# Раньше КАЖДАЯ карточка была «HEADLINE top center» (хардкод) → паки выглядели
# одинаково. Теперь позиция выводится из extracted_style.composition (что модель
# вернула под арт-дирекцию), с откатом по ключу направления. Всё держим внутри
# safety-zone (без крайних верхних/нижних 12% — чтобы не обрезалось при кропе).
_HEADLINE_LAYOUTS = {
    "top":        ("top center, within the upper third", "centered just under the headline"),
    "upper_left": ("upper-left, aligned to the left margin", "left-aligned just under the headline"),
    "bottom":     ("lower third, centered, inside the safe area above the very bottom",
                   "centered just above the footer band"),
    "left":       ("left margin at mid-height", "left-aligned under the headline"),
}
_AD_LAYOUT_FALLBACK = {
    "editorial_magazine":   "upper_left",
    "bold_color_block":     "upper_left",
    "tech_minimal_mono":    "left",
    "warm_lifestyle":       "bottom",
    "nature_organic":       "bottom",
    "high_contrast_poster": "top",
    "energetic_gradient":   "top",
    "pastel_soft":          "top",
    "clean_light_studio":   "top",
    "premium_dark":         "top",
}


def _headline_layout(es: dict, art_key: str, card: dict = None):
    """(headline_position, sub_position) из composition арт-дирекции / ключа.
    Если арт-дирекция выбрана автоматически (art_key задан) и композиция генерик —
    РАЗНООБРАЗИМ раскладку ВНУТРИ пака по типу карточки, чтобы пак не был стеной
    одинаковых «заголовок сверху». Явную композицию рефа/AD (bottom/left/asymmetric)
    всегда уважаем и не трогаем."""
    comp = str((es or {}).get("composition") or "").lower()
    key = None
    if any(w in comp for w in ("bottom", "lower")):
        key = "bottom"
    elif "asymmetr" in comp or ("left" in comp and "right" not in comp):
        key = "upper_left"
    elif any(w in comp for w in ("center", "centre", "top")):
        key = "top"
    if key is None:
        key = _AD_LAYOUT_FALLBACK.get(art_key or "", "top")

    # Внутрипаковая вариативность: только в авто-режиме (есть art_key) и только
    # когда раскладка свелась к дефолтному «top» — иначе уважаем явный замысел.
    if art_key and key == "top" and isinstance(card, dict):
        ctype = str(card.get("type") or "").lower()
        cid = str(card.get("id") or "")
        if ctype == "main":
            key = "top"                       # обложка — сильный классический верх
        elif ctype == "lifestyle":
            key = "bottom"                     # текст над средой использования
        elif ctype == "comparison":
            key = "top"
        else:                                  # feature и пр. — чередуем верх/низ по id
            key = "bottom" if (sum(ord(ch) for ch in cid) % 2) else "top"
    return _HEADLINE_LAYOUTS.get(key, _HEADLINE_LAYOUTS["top"])


# Тон фона (для dark-gate в _prompt_tail) и «светлые» слова mood, которые на
# тёмном фоне ломают единый стиль (тянут рендер в белую студию).
_DARK_BG_WORDS = (
    "dark", "graphite", "black", "charcoal", "night", "navy", "espresso",
    "midnight", "obsidian", "gunmetal", "ink", "slate", "onyx", "deep",
    "moody", "dim", "smoke", "anthracite", "pitch",
)
_LIGHT_MOOD_WORDS = ("daylight", "airy", "bright", "white", "sunlit", "sunny")


def _prompt_tail(design: dict) -> str:
    """Финальный хвост image_prompt ПО АРТ-ДИРЕКЦИИ вместо единого
    'premium product photography, 8k, commercial-grade catalog', который делал
    все карточки одинаково-глянцево-студийными.

    DARK-PACK GATE: art_direction иногда РАСХОДИТСЯ с реальным фоном пака
    (напр. _art_direction.key='clean_light_studio', а extracted_style рисует
    тёмный графит — так было на паке-клавиатуре). Тогда «soft daylight / bright
    airy» в хвосте активно тянул рендер в белую студию и ломал единый тёмный
    стиль. Тон фона берём из extracted_style.background_texture (а НЕ из art_key)
    и для тёмных паков отбрасываем светлый mood — иначе хвост противоречит
    STYLE LOCK."""
    art_key = ""
    bg = ""
    if isinstance(design, dict):
        art_key = (design.get("_art_direction") or {}).get("key") or ""
        bg = ((design.get("extracted_style") or {}).get("background_texture") or "").lower()
    mood = fmt.ART_DIRECTION_MOODS.get(art_key)
    quality = fmt.ART_QUALITY_TAIL
    if mood and any(d in bg for d in _DARK_BG_WORDS) and any(w in mood for w in _LIGHT_MOOD_WORDS):
        # Тёмный фон + светлый mood = конфликт. Нейтральный хвост без daylight.
        return f"{quality}, true colours."
    if mood:
        return f"{quality}, {mood}."
    return f"premium product photography, {quality}."

# Кириллица в extracted_style идёт прямо в image-API prompt и иногда
# прорисовывается на готовой картинке как видимый текст — поэтому такие
# поля принудительно прогоняются через _en_only.
_CYR_RE = re.compile(r"[Ѐ-ӿ]")

# Безопасные английские дефолты для extracted_style — используются и в
# рантайм-санитайзере, и в более раннем _enforce_english_extracted_style.
_ES_FALLBACKS = {
    "headline_font":       "modern condensed sans-serif at 900 weight",
    "composition":         "centered hero product, single subject, generous whitespace",
    "light":               "soft studio key with warm fill and gentle shadows",
    "badges":              "pill-shaped accent-coloured chips with bold white text",
    "graphic_accents":     "minimal geometric accents and thin separators",
    "background_texture":  "clean matte studio backdrop with subtle vignette",
}


# ════════════════════════════════════════════════════════════════════════════
# Per-art-direction TYPO + BADGE fallbacks. Без них _en_only(...) || (один и
# тот же sans-900) гомогенизировал все 10 арт-направлений в одно лицо: один
# шрифт, одна «pill»-плашка под текстом. Эти фоллбэки уважают характер
# направления, даже если Claude не заполнил extracted_style.headline_font/badges.
# ════════════════════════════════════════════════════════════════════════════
_AD_FONT_FALLBACK = {
    "premium_dark":         "heavy condensed sans-serif at 900 weight, near-white",
    "clean_light_studio":   "clean geometric humanist sans at 700 weight, near-black",
    "bold_color_block":     "chunky grotesque sans at 800 weight, oversized, white",
    "warm_lifestyle":       "friendly humanist sans at 600 weight, deep brown",
    "tech_minimal_mono":    "tight technical sans at 500 weight, light grey, wide tracking",
    "editorial_magazine":   "high-contrast display serif, large, mixed case, near-black",
    "pastel_soft":          "rounded friendly sans at 700 weight, plum",
    "high_contrast_poster": "ultra-bold compressed sans at 900 weight, screaming yellow",
    "nature_organic":       "organic humanist sans at 600 weight, forest green",
    "energetic_gradient":   "bold rounded sans at 800 weight, white with subtle glow",
}
_AD_BADGE_FALLBACK = {
    "premium_dark":         "muted-gold rounded pill chips with white text, subtle inner glow",
    "clean_light_studio":   "rounded pills in one saturated accent colour with white text",
    "bold_color_block":     "solid rectangular blocks, no rounding, white text on contrasting colour",
    "warm_lifestyle":       "soft pill chips in terracotta tones with cream-coloured text",
    "tech_minimal_mono":    "hairline outline pills, thin 1px stroke, no fill, light-grey text",
    "editorial_magazine":   "no chip — thin underline accents under short labels, near-black",
    "pastel_soft":          "cloud-soft rounded pills in pastel pink/plum with white text",
    "high_contrast_poster": "solid black-on-yellow blocks, hard rectangular edges, oversized",
    "nature_organic":       "earth-toned soft pill chips with cream text, slightly textured",
    "energetic_gradient":   "glossy capsule chips with subtle gradient fill, white text",
}


def _smart_trunc(s: str, n: int) -> str:
    """Усекает строку до n символов, откатываясь к последнему пробелу /
    знаку препинания — без обрывов внутри слова («whit», «overal»)."""
    if not s:
        return ""
    s = str(s).strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    for sep in (". ", ", ", "; ", " — ", " "):
        idx = cut.rfind(sep)
        if idx > n * 0.55:
            return cut[:idx].rstrip(" .,;—-")
    return cut.rstrip(" .,;—-")


def _en_only(s: str, cap: int) -> str:
    """Возвращает чистую английскую строку до cap символов либо ''.
    Если в значении найдена кириллица — поле дропаем (вернёт ''), чтобы
    не протекло в текст готовой картинки."""
    if not s:
        return ""
    s = str(s).strip()
    if _CYR_RE.search(s):
        logger.warning("[image-prompt] dropping Cyrillic-tainted style field: %r", s[:80])
        return ""
    return _smart_trunc(s, cap)


def _enforce_english_extracted_style(design: dict, step) -> None:
    """Если LLM просочила кириллицу в extracted_style — заменяем поле
    безопасным английским дефолтом (логируем). Вызывается ОДИН раз сразу
    после _normalize_design, до сборки image_prompt'ов."""
    es = (design.get("extracted_style") or {}) if isinstance(design, dict) else {}
    if not isinstance(es, dict):
        return
    replaced = []
    for key, fallback in _ES_FALLBACKS.items():
        val = es.get(key)
        if isinstance(val, str) and _CYR_RE.search(val):
            es[key] = fallback
            replaced.append(key)
    if replaced and callable(step):
        try:
            step(f"⚠ extracted_style: заменил кириллические поля на английские дефолты: {', '.join(replaced)}")
        except Exception:
            pass


# When a product reference photo is attached, the prompt must read as an IMAGE EDIT,
# not a fresh generation — otherwise OpenAI (and others) draw a generic look-alike
# instead of the exact product. Verified A/B: edit-framing makes OpenAI hold the
# reference pixel-perfect (same buttons/colours/labels/logo); scene-framing does not.
_EDIT_PREAMBLE_CORE = (
    "Edit the attached product1 image: keep the product 100% identical (shape, proportions, "
    "true colours, materials, logo, on-device text in original language — never re-letter). "
    "Change ONLY the scene around the product"
)
_EDIT_PREAMBLE = _EDIT_PREAMBLE_CORE + " and add the marketing text."
# Для no-text карточек (Ozon-обложка / пустые тексты): преамбула НЕ должна
# приказывать «add the marketing text» — это противоречило бы text_section
# «NO on-image text» и провоцировало модель дорисовать надпись.
_EDIT_PREAMBLE_NO_TEXT = _EDIT_PREAMBLE_CORE + ". Do NOT add any text."


# ── Эмодзи-callout'ы → графические иконки ────────────────────────────────────
# Дизайн-модель кладёт в badges_ru callout'ы вида «❄ Морозостойкость» (эмодзи несёт
# СМЫСЛ иконки). Но юникод-глифы image-модели рендерят нестабильно (артефактные
# значки, кракозябры). Поэтому эмодзи из ТЕКСТА вырезаем, а смысл переносим в
# отдельную ICONS-директиву («draw a small flat icon») ВНЕ списка RENDER ONLY —
# текст остаётся чистым, иконка рисуется графикой, а не шрифтом.
_EMOJI_ICON_MAP = {
    "❄": "snowflake", "🧊": "ice cube", "💧": "water drop", "🌧": "rain cloud",
    "🚗": "car", "🔥": "flame", "🎁": "gift box", "⚡": "lightning bolt",
    "✓": "checkmark", "✔": "checkmark", "✅": "checkmark",
    "⏱": "stopwatch", "⏰": "alarm clock", "🕐": "clock face",
    "📏": "ruler", "😊": "smiling face", "🌿": "leaf", "🌱": "leaf sprout",
    "🍃": "leaf", "🛡": "shield", "💪": "flexed arm", "⭐": "star", "★": "star",
    "☀": "sun", "🌙": "crescent moon", "🔋": "battery", "📦": "parcel box",
    "🪶": "feather", "🐾": "paw print", "♻": "recycling arrows",
    "🌡": "thermometer", "🧴": "cosmetic bottle", "☕": "steaming cup",
    "🍳": "frying pan", "💎": "gemstone", "📱": "smartphone", "💻": "laptop",
    "🎮": "game controller", "🚿": "shower head", "✨": "sparkles",
    "❤": "heart", "♥": "heart", "☂": "umbrella", "🧤": "winter glove",
    "🏠": "house", "👶": "baby face", "🔇": "muted speaker",
}

# Ведущие символы-НЕ-буквы перед текстом callout'а. ВАЖНО: +/−/×/% и кавычки
# НЕ срезаем — это части значений («-30°C», «×3 быстрее»), их терял старый стрип.
_LEADING_SYMBOL_RE = re.compile(r"^[^\w+\-−×%«»\"']+")


def _split_badge_icon(text: str):
    """«❄ Морозостойкость» → ("Морозостойкость", "snowflake").
    Эмодзи нет / маппинга нет → (текст без глифа | исходный текст, None)."""
    s = (text or "").strip()
    m = _LEADING_SYMBOL_RE.match(s)
    if not m:
        return s, None
    rest = s[m.end():].strip()
    if not rest:                 # бейдж целиком из символов — не трогаем
        return s, None
    icon = next((_EMOJI_ICON_MAP[ch] for ch in m.group(0) if ch in _EMOJI_ICON_MAP), None)
    return rest, icon


def _clean_card_text(s):
    """Нормализация текста карточки перед image-промптом (вынесено из
    _build_image_prompt без изменений логики — см. комментарии внутри)."""
    s = (s or "").strip()
    for ch in ("«", "»", "“", "”", "‘", "’"):
        s = s.replace(ch, "")
    # WB / Ozon карточки: длинные тире и en-dash убивают кликабельность —
    # читаются как «грамотный текст», а нужен короткий рекламный хук. На превью
    # фразы вида «Кружка — для любимых» становятся «Кружка для любимых» (сильнее).
    # Не заменяем на «:», иначе модель отрисует знак как часть макета.
    s = s.replace("—", " ").replace("–", " ").replace("―", " ")
    # Схлопываем двойные пробелы, удаляем хвостовые знаки препинания-наполнители.
    # ВАЖНО: НЕ срезаем ведущий/хвостовой «-» — это часть числа («-30°C»,
    # «-40%»). Если стрипаем — «-30 не страшно» становится «30 не страшно»,
    # минус теряется и смысл ломается. Стрипаем только знаки пунктуации.
    s = re.sub(r"\s{2,}", " ", s).strip(" ,.;:!?")
    return s


# ── scene_override → SHOT scrubber ──────────────────────────────────────────
# scene_override низведён до КАДРА. Любое предложение, упоминающее СРЕДУ
# (фон/свет/студию/настроение/палитру), выбрасываем — единственный авторитет по
# среде это STYLE LOCK. Так гарантия IP-05 (ровно одно описание среды на промпт)
# становится СТРУКТУРНОЙ, а не зависящей от того, послушался ли Stage 2.
# Слова СРЕДЫ/СВЕТА/ПОГОДЫ/НАСТРОЕНИЯ — однозначные (практически никогда не
# «часть товара»). МЕСТА/поверхности/мебель/технику СЮДА НЕ кладём — они ловятся
# СТРУКТУРНО через _ENV_LOCATIVE_RE (предлог места), иначе «kitchen scale»,
# «table lamp», «wall clock» (часть названия товара) ложно резались. Блэклист слов
# проигрывает (всегда что-то упускает — dishwasher/window/sunlight) → основная
# защита теперь СТРУКТУРНАЯ.
_ENV_WORD_RE = re.compile(
    r"(?i)\b(?:"
    r"backgrounds?|backdrops?|studios?|scen(?:e|es|ery|ic)|environments?|"
    r"settings?|ambien\w+|lighting|lights?|lit|glows?|glowing|glowed|shadows?|backlit|"
    r"sunlit|sunlight|sunshine|sunny|daylight|moonlight|candlelight|lamplight|"
    r"dawn|dusk|twilight|nighttime|sunset|sunrise|golden\s+hour|stormy|storm\w*|starry|"
    r"moonlit|overcast|cloud\w*|snow\w*|frost\w*|frosty|rain\w*|"
    r"fog\w*|mist\w*|hazy|breeze|moods?|atmospher\w+|palettes?|gradients?|vignettes?|"
    r"bokeh|outdoors?|indoors?|windows?|windshields?|colou?r of|tone of"
    r")\b"
)

# Камера/направление/субъект — НЕ окружение; исключаем из локативного детектора,
# чтобы «on the left», «in a tight crop», «on the product» не считались средой.
# Двусмысленные «поверхностные» слова (side/edge/top/corner/front/back) сюда НЕ
# кладём: они одновременно камера-направление И часть мебели («side table») —
# последнее должно резаться как окружение, поэтому защищаем только однозначные
# камера-термины. upper/lower — для «upper-left»/«lower-right».
_CAMERA_HEAD = (
    r"left|right|cent(?:re|er)|middle|upper|lower|"
    r"frame|foreground|distance|angle|crop|close-?up|closeup|macro|view|"
    r"shot|profile|focus|hand|hands|palm|fingers?|product\d*|products?|item|items|it|its|itself"
)
# СТРУКТУРНЫЙ детектор МЕСТА: предлог места + артикль вводит ОКРУЖЕНИЕ с ЛЮБЫМ
# (в т.ч. неизвестным словарю) объектом — «on a table», «inside an open dishwasher»,
# «next to a car», «placed on a marble counter». НО не камера-идиомы («on the left»,
# «in a tight crop», «in a tight three-quarter crop»), не притяжательные («on its
# base» — нет артикля), не части товара, названные как место («table lamp»,
# «kitchen scale» — без предлога места). Камера-исключение применяется к ОБЕИМ
# ветвям (глагол-постановка И голый предлог); слот допускает дефис
# («three-quarter», «wide-angle»), чтобы не резать дефисные камера-термины.
_ENV_LOCATIVE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:next\s+to|in\s+front\s+of|surrounded\s+by|close\s+to)\s+(?:a|an|the)\b"
    r"|\b(?:placed|standing|sitting|seated|resting|mounted|hung|hanging|displayed|laid|"
    r"lying|set|nestled|positioned|arranged)\s+"
    r"(?:on|in|inside|against|atop|near|by|upon|onto|beside)\s+"
    r"(?:a|an|the)\s+(?!(?:[\w-]+\s+){0,3}?(?:" + _CAMERA_HEAD + r")\b)"
    r"|\b(?:on|in|inside|into|onto|atop|against|near|beside|behind|underneath|over|upon|amid|amidst)\s+"
    r"(?:a|an|the)\s+(?!(?:[\w-]+\s+){0,3}?(?:" + _CAMERA_HEAD + r")\b)"
    r")"
)


def _scrub_environment(text: str) -> str:
    """scene_override → чистый КАДР (камера/субъект). STYLE LOCK — единственный
    авторитет по среде, поэтому любое окружение/свет/место из scene_override режем
    ДЕТЕРМИНИРОВАННО (over-scrub безопасен: кадр всё равно задают STYLE LOCK +
    frame_clause + composition). Слои:
      1) предложения со словами среды/света/погоды/настроения (_ENV_WORD_RE);
      2) предложения со СТРУКТУРНЫМ местом «предлог+артикль+объект» (_ENV_LOCATIVE_RE)
         — ловит любые места без словаря, но не камера-идиомы и не части товара;
      2b) КЛАУЗНОЕ СПАСЕНИЕ: LLM часто пишет SHOT ОДНИМ предложением с env-хвостом
         («Camera tight on the label, ... with a soft vignette») — раньше терялся
         ВЕСЬ кадр (~1 карточка на пак в live-тестах). Теперь env-предложение
         режем по запятым/точкам-с-запятой и выбрасываем ТОЛЬКО env-клаузы;
         чистый остаток ≥25 симв. сохраняем. Инвариант не ослаблен: ни одна
         env-клауза не проходит, финальная проверка результата остаётся;
      3) если в остатке сигнал всё же остался — глушим SHOT целиком."""
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", str(text).strip())
    kept = []
    for p in parts:
        if not p:
            continue
        if not _ENV_WORD_RE.search(p) and not _ENV_LOCATIVE_RE.search(p):
            kept.append(p)
            continue
        clauses = re.split(r"\s*[;,]\s*", p)
        good = [c for c in clauses
                if c and not _ENV_WORD_RE.search(c) and not _ENV_LOCATIVE_RE.search(c)]
        if good:
            salvaged = ", ".join(good).strip(" ,;")
            if len(salvaged) >= 25:   # осмысленный кадр, а не огрызок «cap open»
                kept.append(salvaged if salvaged.endswith((".", "!", "?")) else salvaged + ".")
    result = _smart_trunc(" ".join(kept).strip(), 220)
    if result and (_ENV_WORD_RE.search(result) or _ENV_LOCATIVE_RE.search(result)):
        return ""
    return result


def _diversify_detail_cards(design: dict) -> None:
    """FIX 3 — детерминированный дедуп кадров detail-карточек. Работает на ЛЮБОМ
    пути (split / single-shot / fallback): вызывается перед сборкой image_prompt.
    Если две detail-карточки дают одинаковый кадр (одинаковые focus_kind+focus,
    включая ОБА пустых focus при одной роли) — вторую НЕЙТРАЛИЗУЕМ (сбрасываем
    focus_kind/focus → нейтральная SHOT-ведомая директива). НИКОГДА не форсим
    scale/environment/contents (это фабриковало бы реквизит/масштаб у неподходящих
    товаров). Идемпотентно, без LLM-вызова."""
    if not isinstance(design, dict):
        return
    seen = set()
    for c in (design.get("cards") or []):
        if not isinstance(c, dict):
            continue
        if str(c.get("type") or "").lower() == "main":
            continue
        fk = str(c.get("focus_kind") or "").strip().lower()
        if fk not in ("part", "scale", "environment", "contents"):
            continue  # уже нейтральная — кадр ведёт SHOT, дедуп не нужен
        # Ключ совпадает с тем, что РЕАЛЬНО различает кадр в _build_image_prompt:
        # part кадрируется ПО focus (нормализуем как _clean_card_text — так же, как
        # crop-ветка), а scale/environment/contents focus ИГНОРИРУЮТ → ключ без focus,
        # чтобы две одно-ролевые карточки (даже с разным focus) дедуплицировались.
        f = re.sub(r"\s+", " ", _clean_card_text(c.get("focus")).lower()) if fk == "part" else ""
        if fk == "part" and not f:
            continue  # part без focus рендерит нейтральную ветку — уже как neutral
        sig = (fk, f)
        if sig in seen:
            c["focus_kind"] = ""   # коллизия → нейтральная директива (defer to SHOT)
            c["focus"] = ""
        else:
            seen.add(sig)


def _build_image_prompt(card: dict, design: dict, has_product_ref: bool = False) -> str:
    """Programmatically assemble the final image_prompt for ONE card.

    Structure:
      [orientation hint]
      [pack_scene_block]
      [product_block]
      RENDER ONLY THESE TEXTS:
        - <text 1 with position + style>
        - <text 2 ...>
      DO NOT render any other text/labels/numbers/technical terms.
      [tail: 8k commercial]

    This deterministic structure prevents prompt-instruction-bleeding
    (where "soft fill" / "rim light" / "key info" leak into the rendered image).
    """
    pack_scene = (design.get("pack_scene_block") or "").strip()
    scene_override = (card.get("scene_override") or "").strip()
    product = (design.get("product_block") or "").strip()
    orient = _format_orientation_hint(card.get("format_key") or "wb_main")

    es = design.get("extracted_style") or {}
    has_scene_override = bool(scene_override)

    # MAIN = HERO. The main/cover card (the first thing a buyer sees in WB search) must look
    # DIFFERENT from the detail cards — it's the scroll-stopper, not an info card.
    # СТРОГО по type=="main" (раньше fallback «нет scene_override → hero» катил
    # любую забытую Claude feature-карточку в hero-ветку → пак выглядел одинаково).
    is_hero = str(card.get("type") or "").lower() == "main"

    # Тексты извлекаем ДО композиционной директивы: счётчик callout'ов в директиве
    # обязан совпадать со списком RENDER ONLY (раньше директива требовала «3-5
    # callouts», а в списке был 1 бейдж → image-модель ВЫДУМЫВАЛА недостающие
    # надписи — главный источник галлюцинированного текста на main).
    head   = _clean_card_text(card.get("headline_ru"))
    sub    = _clean_card_text(card.get("sub_ru"))
    badge_pairs = [_split_badge_icon(_clean_card_text(b))
                   for b in (card.get("badges_ru") or []) if b]
    badge_pairs = [(t, ic) for t, ic in badge_pairs if t]
    footer = _clean_card_text(card.get("footer_ru"))
    # Карточка ВООБЩЕ без текстов (Ozon-обложка / дизайн сознательно вернул пустые
    # поля): собираем честную «чистую» карточку. Раньше и тут писалась директива
    # «TITLE-NAME at top ~22%» + заголовок «RENDER ONLY THESE TEXTS:» с ПУСТЫМ
    # списком — image-модель на таком входе ВЫДУМЫВАЛА надпись сама.
    no_texts = not (head or sub or badge_pairs or footer)

    if is_hero:
        # WB MAIN — инфографика-обложка: TITLE-NAME + товар 60-75% + icon-callouts.
        # sub занимает слот — тогда callout'ов максимум 3 (дисциплина ≤6 элементов).
        hero_calls = badge_pairs[: (3 if sub else 4)]
        n_call = len(hero_calls)
        call_clause = (
            f"exactly {n_call} small icon+text callouts stacked beside the product "
            f"(each = one small flat icon + 1-3 words, ONLY the texts listed below)"
            if n_call else "no callouts"
        )
        composition_directive = (
            "WB main-card layout — INFOGRAPHIC COVER for marketplace search: TITLE-NAME at "
            "top ~22% of canvas (bold sans, the product name from headline, not a slogan); "
            "product LARGE 60-75% of frame, centred at its most flattering 3/4 hero angle "
            f"with cinematic depth; {call_clause}"
            + ("; one thin footer line just above the bottom safe margin" if footer else "")
            + ". Keep top corners 12%×12% empty (WB overlays heart/New). Keep bottom "
            "10% strip empty (WB overlays price plate). One product, no extra props."
        )
        if no_texts:
            composition_directive = (
                "[prompt omitted from public repo -- see docs/prompt-system.md]"
            )
    else:
        hero_calls = []
        # WB DETAIL: TITLE-фишка + sub + 1-2 sticker + opt footer.
        n_det = len(badge_pairs[:3])
        # FIX 3 — кадр detail-карточки задаётся focus_kind (part|scale|environment|
        # contents), построен из СТРУКТУРНЫХ полей Stage-1 (НЕ из scene_override → не
        # режется скруббером, переживает падение Stage 2). part-ветка несёт и crop, и
        # carve-out fidelity (частичный вид ОК) одним клаузом — поэтому полный
        # silhouette-замок остаётся нетронутым и работает для scale/environment/contents.
        focus = _clean_card_text(card.get("focus"))
        # ВАЖНО: ОТСУТСТВУЮЩИЙ focus_kind НЕ равен явному "part" — иначе legacy/
        # non-split карточки с whole-product/lifestyle scene_override насильно
        # кропались (противоречие с их SHOT). Пустой → нейтральная SHOT-ведомая
        # директива (как до FIX 3). Жёсткий кроп только при ЯВНОМ focus.
        fkind = (card.get("focus_kind") or "").strip().lower()
        if fkind == "scale":
            frame_clause = (
                "show the WHOLE product1 beside a common size reference (a hand or a "
                "familiar everyday object), both fully visible, so the buyer reads its "
                "real size at a glance. Keep the locked background."
            )
        elif fkind == "environment":
            # STYLE LOCK владеет фоном: environment = сценарий ИСПОЛЬЗОВАНИЯ на ТОМ ЖЕ
            # фоне, без новой комнаты/сцены (иначе конфликт с авторитетным фоном).
            frame_clause = (
                "show the WHOLE product1 in use (a hand interacting with it, mid-use) "
                "ON THE SAME locked background — do NOT introduce a new room or scene; "
                "the pack's locked background stays. Natural scale."
            )
        elif fkind == "contents":
            # Анти-фабрикация: НЕ выдумывать коробки/аксессуары, которых нет.
            frame_clause = (
                "show product1 with what genuinely ships with it; if it ships ALONE, "
                "show ONLY product1 — do NOT invent boxes, manuals or accessories that "
                "are not part of the product. Keep the locked background."
            )
        elif focus:  # явная названная деталь → плотный кроп (carve-out: частичный вид ОК)
            frame_clause = (
                "the camera is CROPPED IN on the " + focus
                + "[prompt omitted from public repo -- see docs/prompt-system.md]"
            )
        else:  # focus_kind не задан → нейтральная директива, кадр ведёт SHOT (без форс-кропа)
            frame_clause = (
                "frame product1 at a fresh angle distinct from the cover — whole-product, "
                "macro detail, in-use or a different crop are all fine, as long as it "
                "differs from the hero and matches any per-card SHOT direction given. "
                "Keep the locked background."
            )
        composition_directive = (
            "WB detail-card layout: TITLE-LABEL at top (feature name, ≤4 words); "
            + ("1 short sub-line under it (≤8 words); " if sub else "")
            + (f"exactly {n_det} floating badge labels placed around the product "
               f"(NO arrows or lines pointing at specific features; ONLY the texts listed below); " if n_det else "")
            + frame_clause
            + " Keep top corners 12% and bottom 10% empty. Max 5 on-image text elements total."
        )
        if no_texts:
            composition_directive = (
                "Marketplace detail card, NO-TEXT version: "
                + frame_clause
                + " Keep top corners 12% and bottom 10% empty. Pure product photography."
            )

    # ── 1. SCENE — STYLE LOCK ────────────────────────────────────────────────
    # Единственный кросс-карточный якорь для openai_direct — это БАЙТ-В-БАЙТ
    # одинаковый ТЕКСТ (seed дропается в openai_image_client, каждая карточка —
    # независимый /images/edits одного фото). Раньше общий фон/свет доходил ТОЛЬКО
    # до main (pack_scene_block), а feature-карточкам говорили «scene_override
    # авторитетен для фона» — но фона в нём нет → gpt-image дефолтил в белую
    # студию → стиль «плясал» (card_2/3 белые при тёмной main).
    #
    # STYLE LOCK строится ОДИН раз из pack-level extracted_style и вставляется
    # ОДИНАКОВО в КАЖДУЮ карточку (и hero, и detail). Скоуп — только 3 поля,
    # которых карточкам реально не хватает: background + light + palette.
    # headline_font/badges/graphic_accents сюда НЕ кладём — они и так доходят до
    # каждой карточки ниже (font_clause/badge_style), дублировать = лишние токены.
    # Precedence фона: extracted_style → legacy pack_scene_block → _ES_FALLBACKS
    # (чтобы старая схема / тонкий extracted_style не теряли сцену).
    _bg = _en_only(es.get("background_texture"), 160)
    if not _bg:
        _bg = _smart_trunc(pack_scene, 160) if pack_scene else _ES_FALLBACKS["background_texture"]
    _lgt = _en_only(es.get("light"), 120)
    if not _lgt and not pack_scene:
        _lgt = _ES_FALLBACKS["light"]
    _pal = (", ".join(str(h) for h in es["palette_hex"][:4])
            if isinstance(es.get("palette_hex"), list) and es["palette_hex"] else "")
    _seg = lambda label, val: (f"; {label} — {val}" if val else "")
    style_lock = (
        "STYLE LOCK (identical on every card of this pack — this is the "
        "authoritative background & lighting): background — " + _bg
        + _seg("lighting", _lgt) + _seg("palette", _pal) + "."
    )
    if has_scene_override:
        # scene_override низведён до КАДРА (ракурс/фокус/реквизит/момент). Прогон
        # через _scrub_environment: даже если Stage 2 «протёк» фоном/светом/местом в
        # кадр, ассемблер вырезает его → ровно ОДНО описание среды на промпт (IP-05
        # снова структурно невозможен, а не «на доверии» к LLM).
        _shot = _scrub_environment(scene_override)
        # Для scale/environment/contents кадр ПОЛНОСТЬЮ задаёт frame_clause выше; SHOT
        # там лишний и рискует внести место/сцену в конфликт со STYLE LOCK — поэтому
        # добавляем SHOT только для part / нейтральных карточек.
        _fk_scene = (card.get("focus_kind") or "").strip().lower()
        if not is_hero and _fk_scene in ("scale", "environment", "contents"):
            _shot = ""
        scene = style_lock + ("\n\nSHOT: " + _shot if _shot else "")
    else:
        scene = style_lock

    # ── 2. PRODUCT ANCHOR ────────────────────────────────────────────────────
    # Удаляем дублирующуюся строку "Render it exactly matching the product1
    # reference image" если product_block уже её упоминает (баг IP-07).
    if has_product_ref:
        # product1 IMAGE is the only source of truth — short prompt holds identity best.
        product_anchor = (
            "[prompt omitted from public repo -- see docs/prompt-system.md]"
            + (" Place it into the new scene unchanged." if has_scene_override else "")
        )
        # КАТЕГОРИЯ-ЯКОРЬ для feature/lifestyle: на детальных карточках сцена
        # («у двери авто», «макро ручки», «зимняя среда») часто перевешивает
        # рефу — gpt-image-1 видит контекст и подменяет реальный товар на
        # «подходящий» гаджет (термос/пылесос/компрессор). Лечим явным запретом
        # подмены КАТЕГОРИИ + одной короткой строкой про реальный товар из
        # inferred_product. Только когда есть scene_override (т.е. не hero).
        if has_scene_override:
            inferred = design.get("inferred_product") or {}
            cat = (inferred.get("category") or "").strip()
            name = (inferred.get("name") or "").strip()
            ident_bits = []
            if cat:
                ident_bits.append(f"category: {cat}")
            if name and name.lower() != cat.lower():
                ident_bits.append(f"product: {name}")
            ident = " · ".join(ident_bits) if ident_bits else ""
            product_anchor += (
                "[prompt omitted from public repo -- see docs/prompt-system.md]"
                + (f" Reality check: {ident}." if ident else "")
                # PRODUCT FIDELITY: even when the scene implies action, gpt-image-1 deforms the
                # product to fit the action (5L canister narrows to 3L when «hand pours», t-shirt
                # changes cut when «model raises arm»). Lock silhouette/cut explicitly — covers
                # both rigid (bottle/jar/gadget/tool) and soft (apparel/fabric/bag) products.
                + "[prompt omitted from public repo -- see docs/prompt-system.md]"
            )
    elif has_scene_override and product:
        # No ref photo → Claude's text description IS the source; keep the full block.
        product_anchor = (
            "PRODUCT IDENTITY — the item in this scene is EXACTLY the same physical product, "
            "no design changes, no re-imagining: " + product.strip()
        )
    else:
        product_anchor = product

    # ── 3. TEXTS ─────────────────────────────────────────────────────────────
    # head/sub/badge_pairs/footer уже извлечены выше (до композиционной директивы).
    # Чистый список ТЕКСТОВ без описания типографики — он отдельным
    # блоком ниже (баг IP-08: типографские слова рисовались как видимый
    # текст из-за «adjacency leakage»).
    art_key = (design.get("_art_direction") or {}).get("key") or ""
    hl_pos, sub_pos = _headline_layout(es, art_key, card)
    text_lines = []
    icon_notes = []
    # Тексты передаём BARE (без гайльметов). Предыдущий эксперимент с
    # обёрткой `«text»` для защиты ведущего минуса дал side-effect — image-API
    # рендерил сами кавычки как украшение видимого текста («Видно до капли»
    # с гайльметами на превью). Минус защищаем через adaptive signed-number
    # guard ниже (он триггерится только когда в тексте есть знак).
    if head:
        text_lines.append(f"  - HEADLINE ({hl_pos}): {head}")
    if is_hero:
        # HERO = инфографика-обложка (по WB-анатомии): TITLE-NAME + опц. короткий
        # sub + icon-callouts колонкой + опц. footer. Список здесь 1-в-1 совпадает
        # со счётчиком в composition_directive — модель НЕ должна додумывать тексты.
        if sub:
            text_lines.append(f"  - SUB-LINE (small, directly under the headline): {sub}")
        half = (len(hero_calls) + 1) // 2
        for i, (btxt, icon) in enumerate(hero_calls):
            side = ("left column beside the product, mid-height"
                    if (len(hero_calls) <= 2 or i < half)
                    else "right column beside the product, mid-height")
            text_lines.append(f"  - CALLOUT {i + 1} ({side}): {btxt}")
            if icon:
                icon_notes.append(f"callout {i + 1} — {icon}")
        if footer:
            text_lines.append(
                f"  - FOOTER (thin caption just above the bottom safe margin): {footer}")
        badges = [t for t, _ in hero_calls]
        kept_sub = bool(sub)
        kept_footer = bool(footer)
    else:
        if sub:
            text_lines.append(f"  - SUB-LINE ({sub_pos}): {sub}")
        det_pairs = badge_pairs[:3]
        for i, (btxt, icon) in enumerate(det_pairs):
            pos = _BADGE_POSITIONS[i % len(_BADGE_POSITIONS)]
            text_lines.append(f"  - BADGE {i + 1} ({pos}): {btxt}")
            if icon:
                icon_notes.append(f"badge {i + 1} — {icon}")
        if footer:
            text_lines.append(
                f"  - FOOTER (thin caption just above the bottom safe margin): {footer}")
        badges = [t for t, _ in det_pairs]
        kept_sub = bool(sub)
        kept_footer = bool(footer)
    if no_texts:
        text_section = (
            "TEXT: this card has NO on-image text at all. Do NOT draw any words, letters, "
            "numbers, labels, badges, stickers, logos or watermarks anywhere on the image — "
            "only the product itself (with its own factory markings) in the scene."
        )
    else:
        text_section = (
            "RENDER ONLY THESE EXACT TEXTS as the visible on-image text, each in Russian, drawn "
            "EXACTLY as written (do NOT translate, transliterate or change spelling):\n"
            + "\n".join(text_lines)
        )
    # Иконки — ГРАФИКОЙ, не текстом, отдельной директивой вне RENDER ONLY
    # (см. _EMOJI_ICON_MAP: эмодзи вырезаны из текстов, смысл перенесён сюда).
    icons_section = (
        "ICONS — draw a small flat minimalist icon next to the matching text "
        "(as a GRAPHIC, never as a word): " + "; ".join(icon_notes) + "."
        if icon_notes else ""
    )

    # ── 4. TYPOGRAPHY HINT ───────────────────────────────────────────────────
    # Случай заголовка (UPPERCASE vs Sentence case) выводим из extracted_style
    # вместо жёстко прибитого «uppercase» (баг IP-02 — конфликт с референсом).
    font_desc = (str(es.get("headline_font") or "")).lower()
    if any(k in font_desc for k in ("sentence case", "sentence-case", "mixed case", "title case")):
        case_word = "in sentence case"
    elif any(k in font_desc for k in ("uppercase", "all caps", "all-caps", "caps")):
        case_word = "in UPPERCASE"
    else:
        case_word = "as written (no forced casing)"

    # Per-art-direction шрифт/бейдж — раньше один sans-900 + один pill для всех
    # 10 направлений делал паки на одно лицо. Теперь фоллбэк выбирается по
    # ключу арт-направления (см. _AD_FONT_FALLBACK / _AD_BADGE_FALLBACK).
    font_clause = (
        _en_only(es.get("headline_font"), 110)
        or _AD_FONT_FALLBACK.get(art_key)
        or "modern condensed sans-serif at 900 weight"
    )
    badge_style = (
        _en_only(es.get("badges"), 90)
        or _AD_BADGE_FALLBACK.get(art_key)
        or "badges styled to match the art-direction"
    )
    typography_section = (
        "TYPOGRAPHY (style of the texts above — never draw these words):\n"
        f"  - Headline: {case_word}, {font_clause}. HUGE size (11-16% canvas height per line), "
        f"brutal contrast (near-white on dark/colour, near-black on light), 1.5-2px outline or "
        f"25% drop-shadow on similar-tone background.\n"
    )
    if kept_sub:
        typography_section += "  - Sub-line: medium weight, 4-6% canvas height, same contrast logic.\n"
    if badges:
        if is_hero:
            typography_section += (
                f"  - Callouts: {badge_style}, compact (4-5% canvas height each), aligned in "
                f"a tidy column, icon left of text, generous spacing between callouts.\n"
            )
        else:
            typography_section += (
                f"  - Badges: {badge_style}, pill 4-5% canvas height, bold white text on accent fill.\n"
            )
    if kept_footer:
        typography_section += "  - Footer: subtle caption with «•» separator (never em-dash).\n"

    # Защита кириллицы — возвращена детальная версия после бага «TOLBKO» (Только → TOLBKO):
    # image-модель заменила «о» на латинскую «o», «лько» на «lbko». Сжатая версия 200 chars
    # давала недостаточно сигнала, нужны явные примеры лук-аликов с КОНКРЕТНЫМИ парами.
    cyrillic_guard = (
        "[prompt omitted from public repo -- see docs/prompt-system.md]"
    )

    # ── 5. FORBID ────────────────────────────────────────────────────────────
    # Расширенный black-list типографских терминов — чтобы из секции TYPOGRAPHY
    # ничего не «утекло» на картинку как видимый текст.
    # POSITIVE framing only — listing forbidden words (the old approach) put those very
    # tokens into the prompt and the image model rendered them ON the image. We instead
    # state once, positively, that quoted texts are the only drawable text.
    forbid = (
        "[prompt omitted from public repo -- see docs/prompt-system.md]"
    )

    # ── 5b. ANTI-HALLUCINATION ───────────────────────────────────────────────
    # Сжатая форма (1300→~400 chars). Сохраняет 5 ключевых запретов в плотном
    # императиве вместо буллетов с объяснениями: image-модели лучше следуют
    # коротким приказам чем длинным «почему».
    anti_hallucination = (
        "[prompt omitted from public repo -- see docs/prompt-system.md]"
    )

    # ── 6. ASSEMBLE ──────────────────────────────────────────────────────────
    parts = []
    if has_product_ref:
        # frame as an edit of product1 → exact identity; для no-text карточки
        # вариант БЕЗ «and add the marketing text» (см. _EDIT_PREAMBLE_NO_TEXT)
        parts.append(_EDIT_PREAMBLE_NO_TEXT if no_texts else _EDIT_PREAMBLE)
    parts.append(orient)
    parts.append(composition_directive)   # hero vs detail → main looks distinct from details
    if scene:
        parts.append(scene)
    if product_anchor:
        parts.append(product_anchor)
    # Anti-hallucination идёт СРАЗУ после product_anchor — пока контекст «product1»
    # ещё свежий в промпте, и до того как text_section может «увести» модель в
    # креативный режим. Без рефа модель меньше склонна додумывать — но добавляем
    # и тогда: «пар-в-форме-сердечка», «отражение-в-форме-X» бывают и без рефа.
    parts.append(anti_hallucination)
    parts.append(text_section)
    if icons_section:
        parts.append(icons_section)
    if not no_texts:
        parts.append(typography_section.rstrip())
    # Кириллица идёт после typography — чтобы инструкция «русские буквы как
    # надо» не утонула в общем хвосте промпта.
    if any(_CYR_RE.search(t or "") for t in (head, sub, footer, *(badges or []))):
        parts.append(cyrillic_guard)
    # Adaptive signed-number guard: вставляется ТОЛЬКО когда в текстах есть число
    # со знаком ('-30', '−40°C', '+5'). В ~95% карточек триггер не сработает →
    # 0 chars overhead. На остальных — одна короткая директива именно там где
    # нужно. Это лечит баг «−30 НЕ СТРАШНО» → image-модель пишет «30» без минуса
    # потому что трактует ведущий «-» как пунктуацию или list-marker.
    _SIGNED_RE = re.compile(r"[-−+]\s?\d")
    if any(_SIGNED_RE.search(t or "") for t in (head, sub, footer, *(badges or []))):
        parts.append(
            "[prompt omitted from public repo -- see docs/prompt-system.md]"
        )
    if not no_texts:
        # forbid ссылается на «quoted texts above» — для no-text карточки его роль
        # уже выполняет text_section («NO on-image text at all»).
        parts.append(forbid)
    parts.append(_prompt_tail(design).strip())

    return "\n\n".join(parts)


def _normalize_design(design: dict) -> None:
    """Normalize alternate field names Claude often returns instead of the spec.

    Mutates `design` in place:
      cards[].card_number / number          → cards[].id (str)
      cards[].headline / title              → cards[].headline_ru
      cards[].subtitle / sub                → cards[].sub_ru
      cards[].badge_texts / badges          → cards[].badges_ru (list[str])
      cards[].format / formatKey            → cards[].format_key
      cards[].id when missing               → "card_<n>"
      bottom_specs / footer / specs         → folded into card metadata for UI logging only

    Also builds `pack_scene_template` from `extracted_style` if Claude omitted it.
    Also strips the Russian guillemets «...» from headline/sub/badges so we keep
    the literal text (the guillemets are inside image_prompt for AI rendering, but
    we want clean strings for metadata/UI).
    """
    cards = design.get("cards") or design.get("variants") or []
    if not isinstance(cards, list):
        return

    def _strip_quotes(s):
        if not isinstance(s, str):
            return s
        s = s.strip()
        for pair in ("«»", "\"\"", "''", "“”", "‘’"):
            if s.startswith(pair[0]) and s.endswith(pair[1]):
                s = s[1:-1].strip()
                break
        # WB-кликабельность: em-dash / en-dash в коротких хедлайнах читаются как
        # «нейтральный текст», убивают триггер. «Кружка — для любимых» → «Кружка
        # для любимых» сильнее. footer_ru разделитель «•» оставляем (не тире).
        s = s.replace("—", " ").replace("–", " ").replace("―", " ")
        # ВАЖНО: НЕ срезаем ведущий/хвостовой «-» — это часть числа («-30°C»,
        # «-40%»). Если стрипаем — «-30 не страшно» становится «30 не страшно»,
        # минус теряется и смысл ломается. Стрипаем только знаки пунктуации.
        s = re.sub(r"\s{2,}", " ", s).strip(" ,.;:!?")
        return s

    aliases_id     = ("id", "card_id", "number", "card_number", "card_num")
    aliases_head   = ("headline_ru", "headline", "title", "header", "head", "ru_headline")
    aliases_sub    = ("sub_ru", "subtitle", "sub", "subhead", "subheadline", "ru_subtitle")
    aliases_badges = ("badges_ru", "badge_texts", "badges", "tags", "labels", "ru_badges")
    aliases_footer = ("footer_ru", "footer", "specs", "spec_strip", "ru_footer")
    aliases_fmt    = ("format_key", "format", "formatKey", "format_id", "fmt")
    aliases_type   = ("type", "card_type", "kind")
    aliases_scene  = ("scene_override", "scene", "shot", "shot_description", "image_scene", "card_scene")
    aliases_focus  = ("focus", "focus_region", "detail_focus")
    aliases_fkind  = ("focus_kind", "focus_type", "shot_kind")

    for i, c in enumerate(cards):
        if not isinstance(c, dict):
            continue
        # First-found wins
        def pick(keys, default=None, transform=None):
            for k in keys:
                if k in c and c[k] is not None:
                    v = c[k]
                    return transform(v) if transform else v
            return default

        cid    = pick(aliases_id, default=f"card_{i+1}")
        head   = pick(aliases_head, transform=_strip_quotes)
        sub    = pick(aliases_sub, transform=_strip_quotes)
        badges = pick(aliases_badges)
        if isinstance(badges, list):
            badges = [_strip_quotes(b) for b in badges if b]
        elif isinstance(badges, str):
            badges = [_strip_quotes(badges)]
        else:
            badges = []
        fmt_key = pick(aliases_fmt)
        ctype   = pick(aliases_type, default="main")
        # footer тоже чистим через _strip_quotes (em-dash → пробел, кавычки → off).
        # Раньше footer не нормализовался, и тире из «330 мл — керамика» утекало в render.
        footer  = pick(aliases_footer, transform=_strip_quotes)

        scene_ov = pick(aliases_scene, default="")
        if not isinstance(scene_ov, str):
            scene_ov = ""

        c["id"]             = str(cid)
        c["headline_ru"]    = head
        c["sub_ru"]         = sub
        c["badges_ru"]      = badges
        c["format_key"]     = fmt_key
        c["type"]           = ctype
        c["scene_override"] = scene_ov.strip()
        # focus / focus_kind — кадр detail-карточки (FIX 3). ОТСУТСТВУЮЩИЙ focus_kind
        # оставляем ПУСТЫМ (НЕ "part") — иначе legacy/non-split карточки насильно
        # кропались поверх своего whole-product SHOT. Пусто → нейтральная директива.
        # Валидируем к enum только если значение ЕСТЬ; мусор → "".
        _foc = pick(aliases_focus, default="")
        c["focus"] = (_foc.strip() if isinstance(_foc, str) else "")
        _fk = str(pick(aliases_fkind, default="") or "").strip().lower()
        c["focus_kind"] = _fk if _fk in ("part", "scale", "environment", "contents") else ""
        if footer is not None:
            c["footer_ru"] = footer
        # hook_formula — целочисленный код 1..7 из HOOK_FORMULA_SLOTS. Опционален
        # для обратной совместимости: если LLM поле не отдала (старый промпт или
        # custom-вариант) — оставим 0 = «не задано», но НЕ ломаемся.
        hf_raw = c.get("hook_formula")
        try:
            hf_int = int(hf_raw) if hf_raw not in (None, "") else 0
            if not (1 <= hf_int <= 7):
                hf_int = 0
        except (TypeError, ValueError):
            hf_int = 0
        c["hook_formula"] = hf_int

    # Build pack_scene_block / product_block from alternates if missing.
    # New schema uses pack_scene_block + product_block (separate). Old schema had
    # one pack_scene_template. We bridge both.
    if not (design.get("pack_scene_block") or "").strip():
        # Try old field name
        legacy = (design.get("pack_scene_template") or "").strip()
        if legacy:
            design["pack_scene_block"] = legacy
        else:
            es = design.get("extracted_style") or {}
            if isinstance(es, dict) and es:
                design["pack_scene_block"] = _scene_block_from_extracted_style(es)


def _scene_block_from_extracted_style(es: dict) -> str:
    """Build a prosaic scene block from a Claude extracted_style dict, prosaic English only."""
    bg = (es.get("background_texture") or "").strip()
    light = (es.get("light") or "").strip()
    comp = (es.get("composition") or "").strip()
    font = (es.get("headline_font") or "").strip()
    badges = (es.get("badges") or "").strip()
    accents = (es.get("graphic_accents") or "").strip()
    pal = es.get("palette_hex") or []
    pal_str = ", ".join(str(p) for p in pal[:4]) if isinstance(pal, list) else str(pal or "")

    parts = []
    if bg:    parts.append(f"Background: {bg}.")
    if light: parts.append(f"Lighting: {light}.")
    if pal_str: parts.append(f"Core palette: {pal_str}.")
    if font:  parts.append(f"Headline typography: {font}.")
    if badges:parts.append(f"Badges: {badges}.")
    if accents: parts.append(f"Graphic accents: {accents}.")
    if comp:  parts.append(f"Composition rhythm: {comp}.")

    if not parts:
        return ("A premium product card with a clean, minimal background, soft studio lighting, "
                "modern typography, and clear composition.")
    return " ".join(parts)


def _refs_for_provider(provider, product_refs, style_refs):
    """Build the reference_images list to send to a specific image provider.

    Style refs go to image API too (not just Claude vision) — that's how the
    AI actually reproduces the visual treatment.
    """
    # V5: все три живых провайдера (openai / nanopro / nanobanana2) принимают рефы.
    if provider == "fastgen_flower":
        # Single ref slot. If style ref present — STYLE wins over product
        if style_refs:
            return [style_refs[0]]
        if product_refs:
            return [product_refs[0]]
        return []
    if provider == "fastgen_whisk_remix":
        # CATEGORIZED refs — model uses each by its assigned role.
        # max 3 refs per Whisk remix endpoint.
        out = []
        # 2 product photos as SUBJECT (the actual thing)
        for r in product_refs[:2]:
            out.append({"image": r["image"], "category": "MEDIA_CATEGORY_SUBJECT"})
        # 1 style ref as STYLE (visual treatment to copy)
        for r in style_refs[:1]:
            out.append({"image": r["image"], "category": "MEDIA_CATEGORY_STYLE"})
        return out
    # Multi-ref capable: combine, style refs FIRST so AI prioritizes the look
    return list(style_refs) + list(product_refs)


def _whisk_remix_prompt_hint(prompt: str, has_style: bool) -> str:
    """For Whisk remix, prepend a short hint that aligns with category roles.
    The endpoint already encodes SUBJECT/STYLE roles via reference_images, but
    a textual nudge helps the model commit to the style ref's aesthetics fully.
    """
    if not has_style:
        return prompt
    nudge = ("[prompt omitted from public repo -- see docs/prompt-system.md]")
    return nudge + prompt


def _style_ref_nudge(prompt: str, has_style: bool) -> str:
    """Role-anchor для провайдеров БЕЗ категоризированных рефов (openai/nanopro/
    nanobanana2): styleRef уходит туда просто картинкой в общем списке, и без
    явной роли модель может скопировать с него ЧУЖОЙ товар или текст (ровно от
    этого предупреждал НОВЫЙ-ПОДХОД-промпты.md). Якорь жёстко ограничивает
    styleRef ролью style-guide, product1 остаётся единственным источником товара."""
    if not has_style:
        return prompt
    nudge = ("The styleRef reference image is a STYLE GUIDE only: copy its colour palette, "
             "typography feel, lighting mood, background treatment and composition rhythm. "
             "NEVER copy the product, objects, people or any text from styleRef — the product "
             "comes ONLY from the product1 reference image. ")
    return nudge + prompt


def _render_card_image_raw(cfg, provider, image_prompt, aspect,
                            product_refs, style_refs, out_dir, idx, seed=None,
                            progress_cb=None, quality_override=None):
    """Render ONE card. Returns (ok: bool, path_or_err: str, etype: str).
    Does NOT raise on provider failure — caller decides whether to retry/fallback.

    progress_cb(message: str) — опциональный коллбэк, FastGen прокидывает в
    него «polling Ns/Ms» каждые ~10s. Используется чтобы не молчать в SSE.

    etype values: "none" (success) | "rate_limit" | "server" | "content" | "fatal"
    """
    refs = _refs_for_provider(provider, product_refs, style_refs)
    if provider == "fastgen_whisk_remix":
        image_prompt = _whisk_remix_prompt_hint(image_prompt, bool(style_refs))
    else:
        # V5: openai/nanopro/nanobanana2 получают style-ref картинкой в общем
        # списке refs — без role-anchor модель может скопировать с него чужой
        # товар/текст. Якорь ограничивает styleRef ролью style-guide.
        image_prompt = _style_ref_nudge(image_prompt, bool(style_refs))
    if provider == "veononstop":
        try:
            veo = _get_veo(cfg)
        except Exception as e:
            return False, f"VeoNonStop init: {e}"[:200], "fatal"
        ok, path_or_err, etype = veo.generate_image(
            prompt=image_prompt,
            model_key=cfg.get("veononstop_default_model", "GEM_PIX_2"),
            aspect_ratio=aspect, reference_images=refs or None,
            num_images=1, output_dir=Path(out_dir), index=idx,
            seed=seed,
        )
    elif provider.startswith("fastgen_"):
        try:
            fg = _get_fastgen(cfg)
        except Exception as e:
            return False, f"FastGen init: {e}"[:200], "fatal"
        ok, path_or_err, etype = fg.generate_image(
            prompt=image_prompt, provider=provider, aspect_ratio=aspect,
            reference_images=refs or None,
            output_dir=Path(out_dir), index=idx,
            seed=seed,
            progress_cb=progress_cb,
        )
    elif provider == "openai_direct":
        # Прямой gpt-image-2.
        # ЖЁСТКАЯ ГАРАНТИЯ "что в админке, то и применилось":
        #   читаем quality/size из ЖИВОГО server.CFG, а не из переданного cfg.
        #   Причина: cfg уже мог быть захвачен фоновой задачей как ссылка на
        #   старый dict ещё до того, как PUT /api/admin/settings обновил конфиг.
        #   server.CFG мутируется in-place в _reload_server_cfg → всегда свежий.
        # Параллельно логируем что реально уходит в API — чтобы был аудит.
        try:
            oic = _get_openai_image(cfg)
        except Exception as e:
            return False, f"OpenAI image init: {e}"[:200], "fatal"
        live = cfg
        try:
            import server as _srv  # type: ignore
            if isinstance(getattr(_srv, "CFG", None), dict):
                live = _srv.CFG
        except Exception:
            pass
        quality = (live.get("openai_image_quality") or "high").strip().lower()
        size = (live.get("openai_image_size") or "auto").strip().lower().replace("х", "x")
        # Тир-политика: если генерация передала явное качество (low для free /
        # medium для paid), оно ПЕРЕБИВАЕТ глобальный openai_image_quality.
        if quality_override:
            qo = str(quality_override).strip().lower()
            if qo in ("low", "medium", "high", "auto"):
                quality = qo
        logger.info(f"[openai_direct] quality={quality} size={size} "
                    f"(override={quality_override!r}, живой server.CFG)")
        ok, path_or_err, etype = oic.generate_image(
            prompt=image_prompt, provider=provider, aspect_ratio=aspect,
            reference_images=refs or None,
            output_dir=Path(out_dir), index=idx,
            seed=seed,
            progress_cb=progress_cb,
            quality=quality, size=size,
        )
    else:
        return False, f"Unknown provider: {provider}", "fatal"
    return ok, path_or_err, etype


def _render_card_image(cfg, provider, image_prompt, aspect,
                       product_refs, style_refs, out_dir, idx, seed=None):
    """Single-shot render. Raises RuntimeError on failure (backward-compat wrapper)."""
    ok, path_or_err, etype = _render_card_image_raw(
        cfg, provider, image_prompt, aspect,
        product_refs, style_refs, out_dir, idx, seed=seed,
    )
    if not ok:
        raise RuntimeError(f"{provider} fail [{etype}]: {path_or_err}")
    return path_or_err


# ─── Fallback orchestrator (the heart of "site never dies") ────────────────
def _render_card_with_fallback(cfg, primary_provider: str,
                                image_prompt: str, aspect: str,
                                product_refs, style_refs,
                                out_dir, idx: int,
                                seed=None,
                                tracker: "_ProviderTracker" = None,
                                log_fn=None,
                                rec_ctx: dict = None,
                                quality_override: str = None):
    """Render one card walking the FALLBACK_CHAINS for `primary_provider`.

    Behavior per provider:
      - content (safety filter)  → rewrite prompt via Claude, retry; cap at
        per-provider censorship_limit and global _FALLBACK_MAX_TOTAL_REWRITES.
      - rate_limit               → exponential backoff, cap consecutive at 3.
      - server                   → linear backoff, cap consecutive at 3.
      - fatal                    → move to next provider immediately.

    Returns: raw image path on success.
    Raises:  RuntimeError after the entire chain is exhausted.
    """
    chain = FALLBACK_CHAINS.get(primary_provider)
    if chain is None:
        # Раньше тут шёл тихий fallback на veononstop — юзер видел картинки от
        # совсем другого провайдера и не понимал, почему его выбор «не работает».
        # Теперь хотя бы громко логируем, чтобы было видно в server.log.
        logger.warning(
            f"[#{idx+1}] image provider '{primary_provider}' is UNKNOWN "
            f"(valid keys: {sorted(FALLBACK_CHAINS.keys())}) → fallback на veononstop"
        )
        chain = FALLBACK_CHAINS.get("veononstop")
    log = log_fn or (lambda _msg: None)

    total_rewrites = 0
    total_server_errors = 0
    current_prompt = image_prompt
    original_prompt = image_prompt
    last_err = "no providers tried"

    for chain_idx, (provider, censorship_limit) in enumerate(chain):
        if tracker and not tracker.is_available(provider):
            logger.info(f"[#{idx+1}] skip {provider} — already failed too many times this session")
            continue

        if chain_idx > 0:
            log(f"  ↻ #{idx+1} fallback → {provider} (шаг {chain_idx+1}/{len(chain)})")
        else:
            logger.info(f"[#{idx+1}] start {provider}")

        consecutive_censorship = 0
        consecutive_rate_limit = 0
        consecutive_server = 0

        while True:
            if total_server_errors >= _FALLBACK_MAX_TOTAL_SERVER_ERRORS:
                raise RuntimeError(
                    f"Too many server errors across chain ({total_server_errors}); last: {last_err[:120]}"
                )

            if consecutive_censorship >= censorship_limit:
                logger.info(f"[#{idx+1}] {provider}: {consecutive_censorship} content rejects → next provider")
                break

            ok, path_or_err, etype = _render_card_image_raw(
                cfg, provider, current_prompt, aspect,
                product_refs, style_refs, out_dir, idx, seed=seed,
                progress_cb=lambda m, _i=idx: log(f"  ⏳ #{_i+1} {m}"),
                quality_override=quality_override,
            )

            if ok:
                if tracker:
                    tracker.mark_success(provider)
                # Учёт картинки: РЕАЛЬНЫЙ provider (после фолбэка) + контекст,
                # снятый в потоке-владельце (воркер не видит thread-local).
                # quality_override прокидываем в учёт, чтобы $ openai_direct
                # считался по ФАКТИЧЕСКОМУ качеству (low для free / medium для paid).
                _ctx = rec_ctx or {}
                _record_image_usage(
                    provider, cfg,
                    user_id=_ctx.get("user_id"),
                    generation_id=_ctx.get("generation_id"),
                    purpose=_ctx.get("purpose"),
                    quality=quality_override,
                )
                if chain_idx > 0 or total_rewrites > 0:
                    note = []
                    if chain_idx > 0:
                        note.append(f"fallback={provider}")
                    if total_rewrites > 0:
                        note.append(f"after {total_rewrites} rewrites")
                    log(f"  ✓ #{idx+1} recovered ({', '.join(note)})")
                return path_or_err

            # Failure path
            last_err = f"{provider}[{etype}]: {path_or_err}"
            if tracker:
                tracker.mark_error(provider)

            if etype == "content":
                consecutive_censorship += 1
                logger.info(f"[#{idx+1}] {provider} content reject {consecutive_censorship}/{censorship_limit}: {path_or_err[:100]}")
                if total_rewrites < _FALLBACK_MAX_TOTAL_REWRITES:
                    rewritten = _rewrite_prompt_softer(cfg, original_prompt)
                    if rewritten:
                        current_prompt = rewritten
                        original_prompt = rewritten
                        total_rewrites += 1
                        logger.info(f"[#{idx+1}] prompt rewritten ({total_rewrites}/{_FALLBACK_MAX_TOTAL_REWRITES})")
                        continue
                # No usable rewrite available — let next provider try the same prompt
                break

            if etype == "rate_limit":
                consecutive_rate_limit += 1
                if consecutive_rate_limit >= _FALLBACK_MAX_CONSECUTIVE_RATE_LIMIT:
                    logger.info(f"[#{idx+1}] {provider} rate limit {consecutive_rate_limit}x → next provider")
                    break
                wait = min(5 * (2 ** (consecutive_rate_limit - 1)), 60)
                logger.info(f"[#{idx+1}] {provider} rate limit, wait {wait}s")
                time.sleep(wait)
                continue

            if etype == "server":
                consecutive_server += 1
                total_server_errors += 1
                if consecutive_server >= _FALLBACK_MAX_CONSECUTIVE_SERVER:
                    logger.info(f"[#{idx+1}] {provider} server error {consecutive_server}x → next provider")
                    break
                wait = min(5 * consecutive_server, 30)
                logger.info(f"[#{idx+1}] {provider} server error: {path_or_err[:100]}, wait {wait}s")
                time.sleep(wait)
                continue

            if etype == "fatal":
                logger.warning(f"[#{idx+1}] {provider} fatal: {path_or_err[:120]} → next provider")
                break

            # Unknown etype — treat as server-ish
            consecutive_server += 1
            total_server_errors += 1
            if consecutive_server >= _FALLBACK_MAX_CONSECUTIVE_SERVER:
                break
            time.sleep(5)

    raise RuntimeError(f"All providers in chain exhausted for card #{idx+1}; last: {last_err[:160]}")


def _render_card_video(cfg, provider, image_path, motion_prompt, aspect, output_path):
    if provider == "veononstop":
        veo = _get_veo(cfg)
        ok, res = veo.generate_video_from_image(
            image_path=image_path, motion_prompt=motion_prompt,
            output_path=output_path, aspect_ratio=aspect, timeout=600,
        )
        if not ok:
            raise RuntimeError(res)
        return output_path
    elif provider.startswith("fastgen_"):
        fgv = _get_fastgen_video(cfg)
        ok, res = fgv.generate_video_from_image(
            image_path=image_path, motion_prompt=motion_prompt,
            output_path=output_path, aspect_ratio=aspect,
            provider=provider, timeout=600,
        )
        if not ok:
            raise RuntimeError(res)
        return output_path
    else:
        raise ValueError(f"Неизвестный video provider: {provider}")


# ─── Streaming pipeline (the heart of the new architecture) ──────────────────

_NO_OVERLAY_TAIL = ("[prompt omitted from public repo -- see docs/prompt-system.md]")

_VIDEO_EFFECT_FALLBACKS = {
    "auto":     "Slow 10 degree product rotation around vertical axis with subtle warm rim light." + _NO_OVERLAY_TAIL,
    "rotate":   "Slow product rotation 12 degrees around vertical axis at constant speed, like a turntable. Camera fixed, no zoom, no glow." + _NO_OVERLAY_TAIL,
    "zoom":     "Smooth micro zoom-in from 1.0x to 1.05x over 5 seconds with gentle easing. Product itself does not move, no rotation, no glow." + _NO_OVERLAY_TAIL,
    "glow":     "Warm rim light slowly sweeps across the product surface left to right, with subtle warm glow pulsing twice. Product is fully static, no rotation, no zoom." + _NO_OVERLAY_TAIL,
    "parallax": "Background shifts subtly horizontally by 4 percent while product stays exactly in place creating 3D depth. No rotation, no zoom." + _NO_OVERLAY_TAIL,
    "particles":"Soft warm dust particles slowly drift upward behind the product. Product fully static, no rotation, no zoom." + _NO_OVERLAY_TAIL,
}


def _make_motion_prompt(cfg, card_meta, video_effect="auto"):
    """Motion_prompt для одной карточки. LLM-вызов нужен ТОЛЬКО для effect='auto'
    (там модель выбирает эффект под тип товара). Для явного эффекта (rotate/zoom/
    glow/parallax/particles) детерминированный шаблон _VIDEO_EFFECT_FALLBACKS уже
    полный и по правилам («РОВНО ОДИН эффект», текст не движется, no-overlay tail)
    — LLM ничего не добавлял, только жёг токены и латентность на каждую карточку.
    """
    if video_effect != "auto" and video_effect in _VIDEO_EFFECT_FALLBACKS:
        return _VIDEO_EFFECT_FALLBACKS[video_effect]
    user_prompt = P.build_video_motion_user_prompt(card_meta or {}, video_effect=video_effect)
    text = _claude_chat(cfg, P.VIDEO_MOTION_SYSTEM_PROMPT, user_prompt,
                        images=None, max_tokens=400, temperature=0.4,
                        purpose="video_motion")
    try:
        data = P.extract_json(text)
        mp = (data.get("motion_prompt") or "").strip()
    except Exception:
        mp = ""
    if not mp:
        return _VIDEO_EFFECT_FALLBACKS.get(video_effect, _VIDEO_EFFECT_FALLBACKS["auto"])
    # Always force-append the negative tail — Claude often forgets despite the rule.
    if "no captions" not in mp.lower() and "no watermark" not in mp.lower():
        mp = mp + _NO_OVERLAY_TAIL
    return mp


# Маркеры «модель отказалась смотреть картинку» — некоторые прокси отвечают 200,
# но текстом-отказом вместо анализа. Раньше это молча проходило как «всё в порядке».
_VISION_REFUSAL_MARKERS = (
    "don't have the ability", "do not have the ability", "cannot view", "can't view",
    "unable to view", "unable to see", "can't see", "cannot see image",
    "not able to view", "no ability to", "can't analyze image", "cannot analyze image",
    "unable to analyze image", "i cannot process image", "as a text-based",
    "не могу просматривать", "не могу анализировать изобра", "нет возможности видеть",
)


def _looks_like_vision_refusal(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _VISION_REFUSAL_MARKERS)


def _validate_card_texts(card_paths: list, cfg: dict):
    """Vision-проверка ОТРЕНДЕРЕННОГО текста на картинках.

    Returns (checked: bool, issues: list[(basename, issue)]):
      checked=False            → проверку выполнить НЕ удалось (vision недоступен/отказ),
                                  чтобы НЕ выдавать ложное «всё в порядке»;
      checked=True, issues=[]  → проверено, дефектов рендера текста нет;
      checked=True, issues=[…] → найдены дефекты (искажения/дубли/обрезка/латиница).

    Сначала пробуем обычную цепочку endpoints; при отказе модели смотреть картинку
    ИЛИ нечитаемом ответе — повтор на openai-fallback (gpt-4o-mini надёжно видит).
    """
    images, names = [], []
    for p in card_paths:
        try:
            # QC-вызов: 1024px достаточно, чтобы увидеть битую кириллицу/обрезку,
            # а vision-тайлов уходит вдвое меньше, чем на полноразмерной 900×1200+.
            images.append({"name": os.path.basename(p),
                           "image": _file_to_data_uri(p, max_side=_QC_MAX_SIDE)})
            names.append(os.path.basename(p))
        except Exception as e:
            logger.debug(f"_validate_card_texts: skip {p}: {e}")
    if not images:
        return False, []

    system = (
        "You are a strict QA checker for Russian marketplace product-card images. You CAN see "
        "images. Look at each card and flag ONLY these AI-render defects in the ON-IMAGE TEXT:\n"
        "1. CORRUPTION: garbled/broken Cyrillic, nonsense glyphs, wrong-script substitutions.\n"
        "2. DUPLICATION: a word/phrase repeated back-to-back (e.g. 'УТРО УТРО С ТИТАНОМ').\n"
        "3. OVERFLOW/CLIPPING: text cut off at an edge or overlapping the product illegibly.\n"
        "4. WRONG LANGUAGE: Latin/English words where Russian is expected.\n"
        "Correct spelling and stylistic variation are FINE. Respond with ONLY a JSON array."
    )
    user = (
        f"Check these {len(images)} cards in order: {', '.join(names)}. "
        "Output ONLY this JSON array (one object per card):\n"
        '[{"name":"<file>","ok":true,"issue":""}]\n'
        "Set ok=false and describe the defect in issue ONLY for real defects from the checklist."
    )

    def _attempt(purpose):
        try:
            text = _claude_chat(cfg, system, user, images=images, max_tokens=700,
                                temperature=0.0, retries_per_model=1,
                                dead_endpoint_threshold=2, purpose=purpose)
        except (ValueError, RuntimeError) as e:
            logger.warning(f"_validate_card_texts call failed ({purpose}): {e}")
            return None
        if _looks_like_vision_refusal(text):
            logger.warning(f"_validate_card_texts: vision refusal on {purpose or 'default'}")
            return None
        try:
            data = P.extract_json(text)
        except Exception:
            return None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("cards") or data.get("results") or []
        else:
            return None
        if not isinstance(items, list):
            return None
        return [(it.get("name", "?"), it.get("issue", "")) for it in items
                if isinstance(it, dict) and not it.get("ok", True)]

    res = _attempt("visual_analysis")
    if res is not None:
        return True, res
    return False, []


_CARD_OBJ_RE = re.compile(
    r'\{[^{}]*?"id"\s*:\s*"([^"]+)"[^{}]*\}',
    re.DOTALL,
)

def _salvage_card_objects(text: str) -> list:
    """Достать индивидуальные `{"id":"card_N", ...}` объекты regex'ом, даже если
    общая структура ответа поломана (вырванная запятая, не закрыт массив, есть
    мусор/markdown между объектами). Каждый матч прогоняем через json.loads
    индивидуально — невалидные пропускаем. Возвращает list[dict]."""
    if not text:
        return []
    out = []
    for m in _CARD_OBJ_RE.finditer(text):
        chunk = m.group(0)
        # Убираем литеральные переводы строк внутри строк-значений
        chunk = re.sub(r'(?<!\\)\n', ' ', chunk)
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict) and obj.get("id"):
                out.append(obj)
        except Exception:
            continue
    return out


# Социал-пруф / скидки / рейтинги / ранги — это ФАКТЫ, не украшения. Слабая модель
# любит выдумывать «−40%», «Хит», «Топ-10», «1240 отзывов 5★», которых нет во вводных.
# Срезаем такие бейджи детерминированно, если факт НЕ подтверждён в фактах/брифе —
# это дисциплина «нет заявления без доказательства», а не хардкод под товар.
_UNVERIFIED_BADGE_RE = re.compile(
    r"([-−–]?\s*\d+\s*%"               # скидка: −40%, 40%
    r"|\d+(?:[.,]\d+)?\s*★"            # рейтинг: 4.8★
    r"|\d+\s*зв[её]зд"                 # 5 звёзд (bn нормализован ё→е — матчим обе формы)
    r"|\d+\s*отзыв"                    # 1240 отзывов
    r"|топ[\s\-]?\d+"                  # Топ-10
    r"|№\s?\d+"                        # №1
    # Множитель-заявление «×3», «x2 быстрее», «х2» (кириллица!), «в 3 раза» — это
    # факт-утверждение о результате; промптовые запреты его не ловили, а
    # _NUMBER_WITH_UNIT_RE требует единицу измерения. Lookbehind'ы отсекают
    # размеры: (?<![\w×]) — «40x60», «их 2»; (?<!\d\s) — «40 x 60». Хвост
    # «раза?\b» НЕ матчит «размерах»/«разных» (нет word boundary).
    r"|(?<![\w×])(?<!\d\s)[×xх]\s?\d+(?:\s*раза?\b)?"
    r"|в\s+\d+\s*раза?\b"
    r"|бестселлер|лидер\s*продаж|хит\s*продаж)",
    re.IGNORECASE,
)
_SUPERLATIVE_BADGES = {"хит", "бестселлер", "лидер продаж", "хит продаж", "№1", "топ", "топ-1"}


def _strip_unverified_claims(design: dict, facts: dict | None, brief: str = "", step=None) -> None:
    """Убирает НЕподтверждённые социал-пруф/скидку/рейтинг/ранг и бейджи/подзаголовки-
    вопросы. Факт «подтверждён», если встречается в facts или в тексте брифа."""
    hay = " ".join(str(v).lower() for v in (facts or {}).values())
    hay += " " + str(brief or "").lower()
    # bn ниже нормализован ё→е — hay обязан быть в той же форме, иначе бейдж
    # «5 звезд» при факте «5 звёзд» в брифе не подтверждался и срезался.
    hay = hay.replace("ё", "е")
    cards = design.get("cards") or []
    removed = 0
    for c in cards:
        if not isinstance(c, dict):
            continue
        kept = []
        for b in (c.get("badges_ru") or []):
            bs = str(b).strip()
            bn = bs.lower().replace("ё", "е").rstrip("?!. ").strip()
            if bs.endswith("?"):                      # бейдж-вопрос — слабая неуверенность
                removed += 1; continue
            unverified = bool(_UNVERIFIED_BADGE_RE.search(bn)) or (bn in _SUPERLATIVE_BADGES)
            if unverified and (bn not in hay and bs.lower() not in hay):
                removed += 1; continue
            kept.append(bs)
        if kept != (c.get("badges_ru") or []):
            c["badges_ru"] = kept
        sub = str(c.get("sub_ru") or "").strip()      # подзаголовок-вопрос про комплектацию
        if sub.endswith("?") and any(w in sub.lower() for w in ("комплект", "коробк", "набор")):
            c["sub_ru"] = ""; removed += 1
    if removed and callable(step):
        try:
            step(f"🧹 Факт-дисциплина: убрал {removed} неподтверждённых бейджей/вопросов "
                 f"(скидки/рейтинги/«Хит» без факта)")
        except Exception:
            pass


def _grade_and_fix_copy(cfg, design, ctx, step, max_passes=1):
    """Код-гейт КАЧЕСТВА ТЕКСТА (раньше COPY_RULES жили только в промпте и игнорились).
    Детектит «воду» детерминированно (prompts.card_copy_issues), и если нашёл —
    переписывает ТОЛЬКО слабые карточки отдельным LLM-вызовом. Стиль/картинки не трогает.
    Дёшево: при чистых текстах LLM-вызова нет. Безопасно: любая ошибка → оставляем как есть.

    max_passes=1 — раньше было 2, но второй проход в логах часто видно как
    «блокирую замену» / «новый снова штамп» — переписчик с тем же контекстом
    повторяет ошибки. После того как в COMPETITOR_BRIEF_SYSTEM_PROMPT добавлен
    FINAL CHECK блок (модель сама ловит воду на первом single-shot), второй
    проход стал в основном шумом. Если split-режим включён в config — copy_fix
    вообще вызывается только если в split-output обнаружена остаточная вода."""
    cards = design.get("cards") or []
    if not isinstance(cards, list) or not cards:
        return
    # Пользовательские факты передаём в card_copy_issues — детектор по ним
    # whitelist'ит числа: «330 мл» проходит ТОЛЬКО если 330 мл буквально в брифе.
    # Иначе любое число с единицей измерения = выдумка → переписать.
    facts_for_check = (ctx or {}).get("facts") or {}

    def _collect_bad(cards):
        """Карточки с «водой» (per-card) ИЛИ с дублем заголовка/подзаголовка по паку."""
        seen_h, seen_s, bad = {}, {}, []
        for c in cards:
            if not isinstance(c, dict):
                continue
            issues = list(P.card_copy_issues(c, user_facts=facts_for_check))
            h = P._norm_copy(c.get("headline_ru") or "")
            s = P._norm_copy(c.get("sub_ru") or "")
            if h and h in seen_h:
                issues.append("дубль заголовка с другой карточкой пака")
            elif h:
                seen_h[h] = True
            if s and s in seen_s:
                issues.append("дубль подзаголовка с другой карточкой пака")
            elif s:
                seen_s[s] = True
            if issues:
                bad.append(c)
        return bad

    # Переписываем ТОЛЬКО карточки, помеченные детектором «воды»/дублей. Раньше я
    # пробовал слепо переписывать ВЕСЬ пак на слабой модели — но слабый переписчик
    # портил уже хорошие крючки (живое «Кот в чашке» → пресное «Голубой кот 480 мл»).
    # «Не навреди»: трогаем только реально слабые тексты, хорошие оставляем как есть.
    try:
        for _pass in range(max(1, max_passes)):
            bad = _collect_bad(cards)
            if not bad:
                break
            # Логируем КАЖДУЮ карточку с конкретной претензией — раньше показывали
            # только заголовки в превью, не было видно ЧТО конкретно не понравилось.
            step(f"✏️ Проход {_pass+1}/{max_passes}: чиню {len(bad)} карт.:")
            for c in bad[:10]:
                cid = c.get("id") or "?"
                ctype = c.get("type") or "?"
                head = c.get("headline_ru") or "—"
                issues = P.card_copy_issues(c, user_facts=facts_for_check)
                reason = issues[0] if issues else "дубль в паке"
                step(f"   • {cid}[{ctype}] «{head}» — {reason}")
            text = _claude_chat(
                cfg, P.COPY_FIX_SYSTEM_PROMPT, P.build_copy_fix_user_prompt(bad, ctx),
                images=None, max_tokens=1800, temperature=0.55,
                retries_per_model=2, dead_endpoint_threshold=2,
                purpose="copy_fix",
            )
            fixed = None
            try:
                fixed = P.extract_json(text)
            except Exception as _je:
                step(f"   ⚠ extract_json failed: {str(_je)[:120]} — пробую вытащить карточки regex'ом")
            if isinstance(fixed, dict):
                # Модель могла вернуть объект-обёртку {"cards":[...]} или один объект.
                fixed = fixed.get("cards") or fixed.get("results") or [fixed]
            if not isinstance(fixed, list) or not fixed:
                # Последний шанс: вытаскиваем все JSON-объекты с полем "id":"card_N"
                # по отдельности (даже если общая структура совсем покорёжена).
                fixed = _salvage_card_objects(text)
                if not fixed:
                    step(f"   ⚠ переписчик не вернул валидный JSON — оставляю вариант модели")
                    break
                step(f"   ✓ regex-fallback вытащил {len(fixed)} карточек из мусорного ответа")
            by_id = {str(c.get("id")): c for c in cards if isinstance(c, dict)}
            applied = 0
            # Защита headline от downgrade. Универсальные эвристики (не привязаны
            # к категории/бренду):
            #
            #   (a) SPEC_TOKEN — число+единица / артикул-спека (-30°C, 5л, 5W-40,
            #       G12+). Если старый main содержал спеку, а новый — нет, это
            #       downgrade.
            #   (b) ANCHOR_OVERLAP — пересечение значимых слов (длиной ≥4 симв)
            #       между старым и новым. Если новый НЕ содержит НИ ОДНОГО слова
            #       из старого И старый не был bland — переписчик заменил суть.
            #       Это ловит «Castrol Edge 5W-40» → «Масло для тех, кто понимает»
            #       (где старый брендовый main теряет всё — бренд, модель, спеку).
            #
            # main блокируем оба случая. feature — только (b) И только если новый
            # сам по себе bland (чтобы не мешать законным улучшениям).
            _SPEC_TOKEN_RE = re.compile(
                r"(?ix)"
                r"(?:[−–\-+]?\d+(?:[.,]\d+)?\s*°\s*[cс])"
                r"|(?:\d+(?:[.,]\d+)?\s*(?:л|литр(?:а|ов)?|мл|мg|мг|г|кг|см|мм|м|вт|w|в|ма?ч|гб|тб|мп|шт|штук|штуки|м²|м³))"
                r"|(?:\d+\s*[wW]\s*[-/]\s*\d+)"
                r"|(?:[a-zA-Zа-яА-Я]+\d+\+?)"
            )
            _STOPWORDS_RU = {
                "для", "от", "на", "под", "при", "без", "над", "что",
                "это", "как", "так", "уже", "ещё", "тут", "там", "или",
                "и", "а", "но", "в", "с", "к", "о", "у", "из", "до", "по",
                "за", "ли", "же", "бы", "вам", "вас", "ваш", "наш", "его", "её",
            }

            def _significant_tokens(s: str) -> set:
                """Слова ≥4 символов, не стоп-слова, lowercase. Это «якоря смысла»
                headline — категория, спека, бренд, ключевой признак."""
                if not s:
                    return set()
                raw = re.findall(r"[\w\-]+", s, flags=re.UNICODE)
                out = set()
                for tok in raw:
                    low = tok.lower()
                    if len(low) >= 4 and low not in _STOPWORDS_RU:
                        out.add(low)
                    # короткие токены с цифрой считаем якорями всегда
                    # (5л / G12 / 5W30 — это спека)
                    elif any(ch.isdigit() for ch in low):
                        out.add(low)
                return out

            for fx in fixed:
                if not isinstance(fx, dict):
                    continue
                tgt = by_id.get(str(fx.get("id") or ""))
                if not tgt:
                    continue
                new_h = (fx.get("headline_ru") or "").strip()
                new_s = (fx.get("sub_ru") or "").strip()
                new_b = fx.get("badges_ru")
                old_h = tgt.get("headline_ru") or ""
                ctype = tgt.get("type")

                head_blocked = False
                block_reason = ""
                if new_h and old_h and new_h != old_h:
                    old_has_spec = bool(_SPEC_TOKEN_RE.search(old_h))
                    new_has_spec = bool(_SPEC_TOKEN_RE.search(new_h))
                    old_tokens = _significant_tokens(old_h)
                    new_tokens = _significant_tokens(new_h)
                    overlap = old_tokens & new_tokens
                    old_was_bland = bool(P.bland_reason(old_h))

                    # main — самые строгие правила
                    if ctype == "main":
                        if old_has_spec and not new_has_spec:
                            head_blocked = True
                            block_reason = "новый потерял спеку/цифру"
                        elif old_tokens and not overlap and not old_was_bland:
                            head_blocked = True
                            block_reason = (
                                "новый не содержит ни одного значимого слова из "
                                "старого (бренд/категория/спека ушли)"
                            )
                    # feature/lifestyle/comparison — мягче: блокируем ТОЛЬКО если
                    # новый сам по себе bland (защищаем хороший feature от того
                    # чтобы copy_fix его не превратил в «Качественный товар»).
                    else:
                        new_is_bland = bool(P.bland_reason(new_h))
                        if old_tokens and not overlap and not old_was_bland and new_is_bland:
                            head_blocked = True
                            block_reason = (
                                "новый feature-headline — bland без связи со старым"
                            )

                if head_blocked:
                    step(
                        f"   🛡 {tgt.get('id')}[{ctype}]: блокирую замену "
                        f"«{old_h}» → «{new_h}» ({block_reason}; "
                        f"оставляю старый, применяю sub/badges)"
                    )
                if new_h and not head_blocked:
                    tgt["headline_ru"] = new_h
                if new_s:
                    tgt["sub_ru"] = new_s
                if isinstance(new_b, list) and new_b:
                    tgt["badges_ru"] = [str(b).strip() for b in new_b if str(b).strip()][:3]
                if new_h and not head_blocked and P.bland_reason(new_h):
                    step(f"   ⚠ {tgt.get('id')}: новый headline «{new_h}» снова штамп")
                elif not head_blocked:
                    step(f"   ✓ {tgt.get('id')}: «{old_h}» → «{new_h or old_h}»")
                if (new_h and not head_blocked) or new_s or (isinstance(new_b, list) and new_b):
                    applied += 1
            if applied == 0:
                step(f"   ⚠ ни одной карты не переписано — выхожу")
                break
        remaining = _collect_bad(cards)
        if remaining:
            preview = ", ".join(f"«{(c.get('headline_ru') or '?')}»" for c in remaining[:6])
            step(f"⚠️ Тексты всё ещё слабоваты на {len(remaining)} карт. ({preview}) — оставил вариант модели")
    except Exception as e:
        logger.warning(f"_grade_and_fix_copy failed: {e}")
        step(f"⚠️ Гейт качества текста пропущен: {str(e)[:100]}")


def _prepare_design(cfg, mode, payload, style_key, ref_paths, style_ref_paths,
                    palette_override, image_provider, video_provider,
                    write_description, generate_videos, formats_selected,
                    video_effect, user_facts=None):
    """
    Run the design phase synchronously: prepare refs, create run_dir,
    call Claude for the design plan, normalize fields, build image_prompts.

    Returns a dict (call it `prepared`) consumable by _stream_render_from_prepared():
      {
        "design":          dict — final design (with image_prompt baked into each card),
        "inferred":        dict | None,
        "run_dir":         str,
        "raw_dir":         str,
        "log_lines":       list[str],
        "palette":         {"accent": "#HEX", "secondary": "#HEX"},
        "product_refs":    list[str],  # local paths
        "style_refs":      list[str],
        "mode":            str,
        "payload":         original payload (preserved for _meta.json),
        "style_key":       str,
        # Pass-through for the render phase:
        "image_provider":  str,
        "video_provider":  str,
        "write_description": bool,
        "generate_videos": bool,
        "formats_selected": list[str],
        "video_effect":    str,
      }

    May raise on Claude failure / invalid JSON — caller decides how to surface.
    """
    log_lines = []
    palette = {"accent": "#FF4D6D", "secondary": "#1B1B2F"}
    if palette_override:
        palette = {**palette, **{k: v for k, v in palette_override.items() if v}}

    product_refs, style_refs = _prepare_refs(ref_paths, style_ref_paths)

    # Engine stays OpenAI (best at Russian text on the preview). Identity is handled by
    # framing the prompt as an image EDIT (_EDIT_PREAMBLE) and by passing the reference
    # as a full-resolution storage file (see _prepare_refs → storage upload), so OpenAI
    # treats it as a true edit base rather than a loose downscaled inline hint.

    if mode == "advanced":
        product, formats_list, card_plan = payload
        slug = _slugify(product.get("name", "product"))
    else:
        slug = "product"
        formats_list = payload[1]

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    run_dir = os.path.join(cfg["output_dir"], f"{slug}_{run_id}")
    raw_dir = os.path.join(cfg["tmp_dir"], run_id)
    os.makedirs(run_dir, exist_ok=True)

    def step(msg):
        log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        logger.info(msg)

    step(f"📦 Запуск ({mode}) · картинки={image_provider} · видео={video_provider if generate_videos else '—'} · "
         f"refs={len(product_refs)}, style_refs={len(style_refs)} (vision-only)")

    # Факты из СВОБОДНОГО брифа. Юзер часто пишет «Материал: фаянс / 200 мл» прямо
    # в текст, а структурный user_facts остаётся пустым → раньше включалась ветка
    # «фактов нет», и модель лила воду, игнорируя реальные характеристики из брифа.
    # Подмешиваем извлечённые факты (явно переданные user_facts — приоритетнее).
    try:
        brief_text = str(payload[0] or "") if mode in ("brief", "competitor") else ""
        extracted_facts = P.extract_facts_from_brief(brief_text)
        if extracted_facts:
            merged_facts = {**extracted_facts, **(user_facts or {})}
            if merged_facts != (user_facts or {}):
                user_facts = merged_facts
                step(f"✓ Факты из брифа: {', '.join(list(extracted_facts.keys())[:6])}")
    except Exception as e:
        logger.debug(f"extract_facts_from_brief failed: {e}")

    # Split-режим (config.design_split=true): text-agent → visual-agent в 2
    # прохода. Сфокусированные агенты → тексты сильнее, сцены точнее под хук
    # каждой карточки. См. _design_pack_split.
    if cfg.get("design_split", False):
        step("🧠 Split-дизайн: 2 прохода (тексты → визуал)…")
        design = _design_pack_split(cfg, mode, payload, style_key,
                                    product_refs, style_refs, palette,
                                    user_facts=user_facts, step=step)
    else:
        step("🧠 Claude проектирует пак…")
        design = _design_pack(cfg, mode, payload, style_key,
                              product_refs, style_refs, palette,
                              user_facts=user_facts)

    # Наблюдаемость: какое арт-направление и какая модель реально сделали пак
    # (раньше нельзя было понять — упал ли дизайн на слабый gpt-4.1-mini).
    _ad_meta = design.get("_art_direction") or {}
    _route_meta = design.get("_design_route") or {}
    if _ad_meta.get("key"):
        step(f"🎨 Арт-направление: {_ad_meta['key']} (accent {_ad_meta.get('accent','?')})")
    if _route_meta.get("model"):
        step(f"🧩 Дизайн собрал: {_route_meta.get('endpoint','?')}/{_route_meta['model']}")

    inferred = design.get("inferred_product")
    if inferred and mode == "brief":
        new_slug = _slugify(inferred.get("name") or "product")
        if new_slug != slug:
            new_run_dir = os.path.join(cfg["output_dir"], f"{new_slug}_{run_id}")
            try:
                os.rename(run_dir, new_run_dir)
                run_dir = new_run_dir
            except Exception:
                pass
        step(f"✓ Распознан товар: «{inferred.get('name','?')}» / {inferred.get('category','?')}")

    _normalize_design(design)
    # Если Claude вернула extracted_style с кириллицей — она ПРОТЕЧЁТ как
    # видимый текст на готовой картинке. Подменяем такие поля английскими
    # дефолтами ДО сборки image_prompt'ов.
    _enforce_english_extracted_style(design, step)
    extracted_style = design.get("extracted_style") or {}
    if extracted_style and isinstance(extracted_style, dict):
        bits = []
        for k, label in (("palette_hex", "палитра"),
                         ("headline_font", "шрифт"),
                         ("background_texture", "фон"),
                         ("light", "свет"),
                         ("composition", "композиция")):
            v = extracted_style.get(k)
            if not v: continue
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v[:4])
            v = str(v)
            if len(v) > 60: v = v[:60] + "…"
            bits.append(f"{label}: {v}")
        if bits:
            step("✓ Реф-стиль учтён · " + " | ".join(bits))

    scene_block = _sanitize_tech_tokens(
        (design.get("pack_scene_block") or design.get("pack_scene_template") or "").strip()
    )
    product_block = _sanitize_tech_tokens(
        (design.get("product_block") or "").strip()
    )
    design["pack_scene_block"] = scene_block
    design["product_block"] = product_block
    if scene_block:
        step(f"✓ Scene-block: {scene_block[:140]}…")
    if product_block:
        step(f"✓ Product-block: {product_block[:140]}…")

    step(f"✓ Концепт: {design.get('concept', '—')}")
    cards = design["cards"]

    if not scene_block:
        step("⚠ pack_scene_block отсутствует — собираю минимальный шаблон из extracted_style")
        es = design.get("extracted_style") or {}
        scene_block = _scene_block_from_extracted_style(es)
        design["pack_scene_block"] = scene_block
    if not product_block:
        step("⚠ product_block отсутствует — использую generic-описание (товар не зафиксирован)")
        product_block = ("The product matching the product1 reference image exactly — same model, "
                         "same color, same proportions, centered in the frame, clearly visible.")
        design["product_block"] = product_block

    # ── Код-гейт качества ТЕКСТА (до сборки image_prompt, чтобы улучшенный текст
    #    попал на картинку). Ловит «воду» и переписывает слабые заголовки. ─────────
    _inf = design.get("inferred_product") or {}
    if mode == "advanced" and isinstance(payload[0], dict):
        _name  = payload[0].get("name") or _inf.get("name") or "—"
        _cat   = payload[0].get("category") or _inf.get("category") or "—"
        _brand = payload[0].get("brand") or _inf.get("brand") or ""
    else:
        _name  = _inf.get("name") or "—"
        _cat   = (payload[4] if mode == "competitor" and len(payload) > 4 else "") \
                 or _inf.get("category") or "—"
        _brand = _inf.get("brand") or ""
    _grade_and_fix_copy(cfg, design, {
        "name": _name, "brand": _brand, "category": _cat,
        "concept": design.get("concept") or "—",
        "facts": user_facts or {},
    }, step)

    # Факт-дисциплина (детерминированно, до сборки картинки): срезаем неподтверждённые
    # скидки/рейтинги/«Хит» и бейджи-вопросы — чтобы на картинку не попала ложь.
    _brief_txt = ""
    try:
        if isinstance(payload, (list, tuple)) and payload:
            _brief_txt = payload[0] if isinstance(payload[0], str) \
                else str((payload[0] or {}).get("brief") or (payload[0] or {}).get("name") or "")
    except Exception:
        _brief_txt = ""
    _strip_unverified_claims(design, user_facts, _brief_txt, step)

    # FIX 3 — дедуп кадров detail-карточек на ВСЕХ путях (split / single-shot / fallback).
    _diversify_detail_cards(design)
    for c in cards:
        c["image_prompt"] = _build_image_prompt(c, design, has_product_ref=bool(product_refs))

    return {
        "design": design,
        "inferred": inferred,
        "run_dir": run_dir,
        "raw_dir": raw_dir,
        "log_lines": log_lines,
        "palette": palette,
        "product_refs": product_refs,
        "style_refs": style_refs,
        "mode": mode,
        "payload": payload,
        "style_key": style_key,
        "art_direction": design.get("_art_direction") or {},
        "design_route": design.get("_design_route") or {},
        "image_provider": image_provider,
        "video_provider": video_provider,
        "write_description": write_description,
        "generate_videos": generate_videos,
        "formats_selected": formats_selected,
        "video_effect": video_effect,
        # Подтверждённые факты юзера — нужны на этапе рендера для sanitize_description_numbers
        # (вырезает «примерно 350 мл» если 350 мл не в фактах). Без этого ключа стриминговая
        # ветка падала с `name 'user_facts' is not defined`.
        "user_facts": user_facts or {},
    }


def _stream_render_from_prepared(cfg, prepared):
    """
    Streaming render generator that takes a `prepared` dict from _prepare_design.
    Used by both:
      - _stream_pack (which calls _prepare_design then us — original API)
      - render_pack_from_design (new public API: user edits design first)

    `prepared["design"]["cards"]` may have edited headline_ru / sub_ru / badges_ru
    since user gave it back — we rebuild image_prompt from the edited cards.
    """
    design          = prepared["design"]
    inferred        = prepared["inferred"]
    run_dir         = prepared["run_dir"]
    raw_dir         = prepared["raw_dir"]
    log_lines       = list(prepared.get("log_lines") or [])
    palette         = prepared["palette"]
    product_refs    = prepared["product_refs"]
    style_refs      = prepared["style_refs"]
    mode            = prepared["mode"]
    payload         = prepared["payload"]
    style_key       = prepared["style_key"]
    image_provider  = prepared["image_provider"]
    video_provider  = prepared["video_provider"]
    write_description = prepared["write_description"]
    generate_videos = prepared["generate_videos"]
    formats_selected = prepared["formats_selected"]
    video_effect    = prepared.get("video_effect") or "auto"
    art_direction   = prepared.get("art_direction") or design.get("_art_direction") or {}
    design_route    = prepared.get("design_route") or design.get("_design_route") or {}
    user_facts      = prepared.get("user_facts") or {}

    def step(msg):
        log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        logger.info(msg)

    cards = design["cards"]

    # Rebuild image_prompt for every card from the (possibly edited) text fields.
    # This is idempotent — if no edits happened, prompts are identical to _prepare_design's.
    _diversify_detail_cards(design)   # FIX 3 — идемпотентно (повторный вызов безвреден)
    for c in cards:
        c["image_prompt"] = _build_image_prompt(c, design, has_product_ref=bool(product_refs))

    # Сразу после copy_fix/rebuild выводим финальные тексты + короткий image_prompt
    # ПРЯМО В STREAM (виден в UI до старта рендера) и в _log.txt. Зачем: если
    # fastgen завалит весь пак (как 12.06 — 500 на ретраях), промпт виден сразу,
    # отлаживать можно из лога, не из памяти процесса. _design.json/_prompts.txt
    # пишутся ниже на диск — это онлайн-зеркало.
    # Структура image_prompt из _build_image_prompt:
    #   has_product_ref=True (типичный путь):
    #     [0]=_EDIT_PREAMBLE  [1]=orient  [2]=WB layout directive  [3]=SCENE  [4]=product anchor...
    #   has_product_ref=False (vision-only):
    #     [0]=orient  [1]=WB layout  [2]=SCENE  [3]=...
    # Парсить по индексу хрупко, поэтому берём scene/scene_override напрямую
    # из card/design — это и есть то, что юзер хочет видеть в логах для отладки.
    pack_scene = (design.get("pack_scene_block") or "").strip()
    _es_dbg = design.get("extracted_style") or {}
    _bg_dbg = (_es_dbg.get("background_texture") or pack_scene or "").strip()
    for c in cards:
        cid = c.get("id", "?")
        ctype = c.get("type", "?")
        head = (c.get("headline_ru") or "").strip() or "—"
        sub = (c.get("sub_ru") or "").strip() or "—"
        badges = c.get("badges_ru") or []
        # STYLE LOCK (общий фон/свет) одинаков на всех карточках; feature-карточки
        # добавляют свой КАДР (scene_override → scrubbed SHOT). Лог зеркалит сборку:
        # для scale/environment/contents SHOT в _build_image_prompt подавляется.
        _fk_dbg = (c.get("focus_kind") or "").strip().lower()
        _shot_dbg = ("" if (str(ctype).lower() != "main" and _fk_dbg in ("scale", "environment", "contents"))
                     else _scrub_environment((c.get("scene_override") or "").strip()))
        scene = ((f"[bg] {_bg_dbg}" + (f"  [shot] {_shot_dbg}" if _shot_dbg else ""))
                 if _bg_dbg else _shot_dbg)
        scene_short = scene.replace("\n", " ⏎ ")[:260].strip()
        step(
            f"📝 {cid} [{ctype}] head=«{head}» sub=«{sub}»"
            + (f" badges={badges}" if badges else "")
        )
        if scene_short:
            step(f"   ↳ scene: {scene_short}…")

    # One seed per pack — gives cards the same noise pattern / style baseline.
    pack_seed = uuid.uuid4().int % (2**31 - 1)
    step(f"🎲 Pack seed: {pack_seed} (общий на пак; работает у fastgen-провайдеров, "
         f"gpt-image seed не принимает — там единый стиль держит STYLE LOCK)")

    # Shared tracker — once provider X has died ~8 times across the pack,
    # subsequent cards skip it instantly (no point retrying a dead endpoint).
    pack_tracker = _ProviderTracker()

    # Дамп промптов на диск ДО рендера. Если fastgen умрёт со всеми ретраями,
    # _meta.json (пишется только в финале) НЕ создастся, и промпты будут потеряны.
    # _design.json (полный pack) + _prompts.txt (compact image_prompt по карточкам
    # для быстрого копипаста в браузер/другой провайдер) выживают любую ошибку
    # рендера и дают юзеру материал для отладки.
    try:
        with open(os.path.join(run_dir, "_design.json"), "w", encoding="utf-8") as fp:
            json.dump({
                "design": design,
                "image_provider": image_provider,
                "pack_seed": pack_seed,
                "art_direction": (art_direction or {}).get("key"),
                "art_direction_accent": (art_direction or {}).get("accent"),
            }, fp, ensure_ascii=False, indent=2, default=str)
        with open(os.path.join(run_dir, "_prompts.txt"), "w", encoding="utf-8") as fp:
            fp.write(f"# Pack seed: {pack_seed}\n")
            fp.write(f"# Image provider: {image_provider}\n")
            fp.write(f"# Art direction: {(art_direction or {}).get('key')}\n\n")
            for i, c in enumerate(cards, 1):
                fp.write(f"{'='*70}\n")
                fp.write(f"CARD {i}: {c.get('id','?')} ({c.get('type','?')})\n")
                fp.write(f"headline: {c.get('headline_ru','')}\n")
                fp.write(f"sub:      {c.get('sub_ru','')}\n")
                fp.write(f"badges:   {c.get('badges_ru','')}\n")
                fp.write(f"footer:   {c.get('footer_ru','')}\n")
                fp.write(f"{'-'*70}\nIMAGE_PROMPT:\n{c.get('image_prompt','')}\n\n")
    except Exception as e:
        logger.warning(f"_design.json/_prompts.txt write failed: {e}")

    step(f"🎨 Стартую рендер {len(cards)} карточек параллельно ({image_provider})…")

    yield {"files": [], "videos": [], "description": "", "log": list(log_lines),
           "info": f"Рендерю {len(cards)} карточек…",
           "run_dir": run_dir, "design": design, "inferred_product": inferred,
           "done": False}

    # ─── 2. Parallel pipeline ────────────────────────────────────────────────
    # Контекст (user_id/generation_id/purpose/img_policy) снимаем ЗДЕСЬ, в потоке-
    # владельце: воркеры image_executor его уже не увидят (thread-local не
    # наследуется). Тир-политику применяем по карточкам через _card_prov_qual.
    _rec_ctx = get_llm_context()
    _policy = (_rec_ctx or {}).get("img_policy") or None

    def _card_prov_qual(i: int):
        """→ (provider, quality_override) для карточки i по тир-политике.
        Без политики — legacy: provider из prepared, качество из глоб. конфига."""
        if not _policy:
            return image_provider, None
        if _policy.get("mode") == "mix":
            provs = _policy.get("providers") or [image_provider]
            prov = provs[i % len(provs)]
            qual = _policy.get("quality") if prov == "openai_direct" else None
            return prov, qual
        return (_policy.get("provider") or image_provider), _policy.get("quality")

    # Размер пула воркеров: fastgen лимитирован (3), gpt-image тянет 6.
    if _policy and _policy.get("mode") == "mix":
        _uses_fastgen = any(str(p).startswith("fastgen_")
                            for p in (_policy.get("providers") or []))
    elif _policy:
        _uses_fastgen = str(_policy.get("provider") or "").startswith("fastgen_")
    else:
        _uses_fastgen = image_provider.startswith("fastgen_")
    image_executor = ThreadPoolExecutor(
        max_workers=min(3 if _uses_fastgen else 6, len(cards) or 1),
        thread_name_prefix="img",
    )
    video_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vid")
    desc_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desc")

    # Submit all image tasks
    def render_image_task(card, idx):
        # Guard: LLM иногда кладёт card_type («feature», «lifestyle») в поле
        # format_key — fmt.FORMATS["feature"] раньше падал KeyError 'feature',
        # карточки feature/lifestyle тупо терялись, а юзер видел «Карточки
        # готовы: 1 дизайн». Любое значение, которое НЕ совпадает с реальным
        # пресетом формата, откатываем на formats_selected[0] (по дефолту
        # wb_main для WB-пака).
        raw_fmt_key = card.get("format_key")
        if raw_fmt_key not in fmt.FORMATS:
            if raw_fmt_key:
                logger.warning(
                    f"card {card.get('id', idx)}: format_key={raw_fmt_key!r} нет в FORMATS, "
                    f"откатываем на {formats_selected[0]!r}"
                )
            format_key = formats_selected[0]
            card["format_key"] = format_key   # фиксируем для downstream-кода
        else:
            format_key = raw_fmt_key
        aspect = fmt.FORMATS.get(format_key, {}).get("aspect", "3:4")
        image_prompt = (card.get("image_prompt") or "").strip()
        if not image_prompt:
            raise ValueError(f"card {card.get('id')} без image_prompt")
        _prov, _qual = _card_prov_qual(idx)
        raw_path = _render_card_with_fallback(
            cfg, _prov, image_prompt, aspect,
            product_refs, style_refs, raw_dir, idx, seed=pack_seed,
            tracker=pack_tracker, log_fn=step, rec_ctx=_rec_ctx,
            quality_override=_qual,
        )
        # Finalize immediately (resize/crop) — fast, in-thread
        size = fmt.FORMATS[format_key]["size"]
        card_id = card.get("id", f"card_{idx+1}")
        out_name = f"{format_key}__{card_id}.jpg"
        out_path = os.path.join(run_dir, out_name)
        overlay.finalize_card(raw_path, size, out_path)
        return idx, card, format_key, out_path

    image_futs = {image_executor.submit(render_image_task, c, i): (i, c)
                  for i, c in enumerate(cards)}

    # Description in parallel (uses different Claude conversation).
    # product_for_desc — что мы скармливаем DESCRIPTION_SYSTEM_PROMPT'у:
    #   • advanced  → payload[0] (полный продуктовый dict)
    #   • brief/competitor → inferred_product от дизайн-фазы, если оно есть.
    # FALLBACK (раньше его не было — десc-промпт молча не вызывался, и юзер
    # видел «карточки готовы» без описания): если дизайн не вернул inferred,
    # собираем минимальный product-stub из брифа и user_facts, чтобы описание
    # всё равно сгенерилось. На бэк это копейки (1 text-only вызов).
    desc_fut = None
    if write_description:
        product_for_desc = None
        if mode == "advanced":
            product_for_desc = payload[0]
        elif inferred:
            product_for_desc = {"inferred_product": inferred}
        else:
            # brief или competitor с пустым inferred_product → собираем сами.
            brief_text = ""
            try:
                brief_text = str(payload[0] or "") if mode in ("brief", "competitor") else ""
            except Exception:
                brief_text = ""
            category_hint = ""
            if mode == "competitor" and len(payload) >= 5:
                category_hint = str(payload[4] or "")
            stub = {
                "name": (brief_text.splitlines()[0] if brief_text else "").strip()[:80] or "товар",
                "category": category_hint or "",
                "usp": "",
                # ВАЖНО: features — это user_facts по сути. DESCRIPTION_SYSTEM_PROMPT
                # обращается к ним как «подтверждённые факты», поэтому без них
                # выходит описание-вода.
                "features": [f"{k}: {v}" for k, v in (user_facts or {}).items()
                             if v and str(v).strip()
                             and not str(v).lower().startswith("не указ")],
                "audience": "",
                "price_range": "",
                "_brief_raw": brief_text[:1200],   # для контекста в промпте
            }
            product_for_desc = {"inferred_product": stub}
        # competitor mode → payload is (brief, formats, count, comp_analysis, category)
        competitor_analysis_for_desc = (
            payload[3] if mode == "competitor" and len(payload) >= 4 else None
        )
        if product_for_desc:
            desc_fut = desc_executor.submit(
                lambda: _claude_chat(
                    cfg, P.DESCRIPTION_SYSTEM_PROMPT,
                    P.build_description_user_prompt(
                        product_for_desc, competitor_analysis_for_desc
                    ),
                    images=None, max_tokens=2500, temperature=0.5,
                    purpose="description",
                ).strip()
            )

    # Track completed work
    files_done = []
    videos_done = []
    description_text = ""
    video_futs = {}  # video_fut -> image_path (for logging)
    render_meta = {}  # out_path -> card dict (for re-render on text errors)

    def submit_video_for(image_path, card):
        """Build motion prompt + submit video task."""
        format_key = card.get("format_key", formats_selected[0])
        aspect = fmt.FORMATS.get(format_key, {}).get("aspect", "3:4")

        def task():
            motion = _make_motion_prompt(cfg, card, video_effect=video_effect)
            out_mp4 = os.path.splitext(image_path)[0] + ".mp4"
            return _render_card_video(cfg, video_provider, image_path,
                                      motion, aspect, out_mp4), motion

        return video_executor.submit(task)

    _render_t0 = time.monotonic()
    pending = set(image_futs.keys())
    if desc_fut:
        pending.add(desc_fut)

    while pending or video_futs:
        # Wait for ANY future to complete (image, video, or description)
        watch = list(pending) + list(video_futs.keys())
        if not watch:
            break
        done, _ = wait(watch, timeout=2.0, return_when=FIRST_COMPLETED)

        if not done:
            # Heartbeat — лёгкий sentinel вместо полного state-снапшота.
            # Серверу нужно только подтверждение «процесс жив» — он сам
            # переведёт в SSE-comment, чтобы не перекидывать каждые 2s
            # files+videos+log+design (десятки KB на пинг).
            info_str = (f"📸 {len(files_done)}/{len(cards)} карточек, "
                        f"🎬 {len(videos_done)} видео" if generate_videos
                        else f"📸 {len(files_done)}/{len(cards)} карточек")
            # Карточка дольше обычного (норм. medium-рендер ~85-90с): если фьючерсы
            # картинок ещё висят за этим порогом — внутри клиента уже идёт авто-
            # ретрай на ТОЙ ЖЕ модели/качестве (timeout=90с). Честно говорим юзеру
            # «дожимаю», а не оставляем застывший «рисую…», который читается как
            # «всё зависло» (ровно та жалоба, что была на скрине).
            imgs_left = sum(1 for f in pending if f in image_futs)
            if imgs_left and (time.monotonic() - _render_t0) > 95:
                tail = ("карточка дорисовывается дольше обычного, дожимаю…"
                        if imgs_left == 1
                        else f"{imgs_left} карточки дорисовываются дольше обычного, дожимаю…")
                info_str += " · " + tail
            yield {"heartbeat": True, "info": info_str, "done": False}
            continue

        for f in done:
            if f in image_futs:
                pending.discard(f)
                idx, card = image_futs[f]
                try:
                    _, card_done, format_key, out_path = f.result()
                    files_done.append(out_path)
                    render_meta[out_path] = card_done
                    step(f"  ✓ карточка [{len(files_done)}/{len(cards)}] {format_key} — {os.path.basename(out_path)}")
                    if generate_videos:
                        v_fut = submit_video_for(out_path, card)
                        video_futs[v_fut] = out_path
                except Exception as e:
                    err = str(e)[:200]
                    step(f"  ✗ карточка #{idx+1}: {err}")
            elif f in video_futs:
                src = video_futs.pop(f)
                try:
                    (out_mp4, motion) = f.result()
                    videos_done.append(out_mp4)
                    step(f"  ✓ видео [{len(videos_done)}] {os.path.basename(out_mp4)}")
                except Exception as e:
                    err = str(e)[:200]
                    step(f"  ✗ видео для {os.path.basename(src)}: {err}")
            elif f is desc_fut:
                pending.discard(f)
                try:
                    description_text = f.result()
                    # Post-фильтр: вырезаем выдуманные числа и hedge-слова. Бесплатно
                    # (детерминированно, без LLM), но критично: на маркетплейсе
                    # «примерно 350 мл» = жалоба покупателя по 39-ФЗ.
                    description_text, removed = P.sanitize_description_numbers(
                        description_text, user_facts or {}
                    )
                    if removed:
                        step(f"  🧹 description: убрал {removed} строк с выдуманными числами / hedge-словами")
                    step(f"  ✓ описание ({len(description_text)} символов)")
                    with open(os.path.join(run_dir, "description.txt"), "w", encoding="utf-8") as fp:
                        fp.write(description_text)
                except Exception as e:
                    step(f"  ✗ описание упало: {str(e)[:200]}")

        # Yield after each batch of completions
        yield {"files": list(files_done), "videos": list(videos_done),
               "description": description_text, "log": list(log_lines),
               "info": (f"📸 {len(files_done)}/{len(cards)} карточек, "
                        f"🎬 {len(videos_done)}{f'/{len(files_done)}' if generate_videos else ''}"
                        if generate_videos
                        else f"📸 {len(files_done)}/{len(cards)} карточек"),
               "run_dir": run_dir, "design": design,
               "inferred_product": inferred, "done": False}

    # ─── 3. Text quality validation ─────────────────────────────────────────
    image_executor.shutdown(wait=False)
    video_executor.shutdown(wait=False)
    desc_executor.shutdown(wait=False)

    # Vision-OCR проверка текста на отрендеренных карточках отключена по
    # просьбе юзера (13.06): vision-чек дорогой (vision-токены на ВСЕ карточки),
    # реген ещё дороже (повторный fastgen call), а сама проверка часто false-
    # positive (ловит легитимные стилизации текста как «дефект»). Доверяем
    # рендеру + bake-text-in-prompt: текст на карточках проверяется через
    # bland_reason/card_copy_issues на ТЕКСТОВОМ этапе (до рендера), а не
    # после-факта OCR'ом.

    meta = {
        "mode": mode,
        "payload": (payload[0] if mode == "advanced" else {"brief": payload[0]}),
        "formats": formats_selected,
        "style": style_key,
        "art_direction": (art_direction or {}).get("key") or style_key,
        "art_direction_palette": {k: art_direction.get(k) for k in ("accent", "secondary")} if art_direction else None,
        "design_model": (design_route or {}).get("model"),
        "design_endpoint": (design_route or {}).get("endpoint"),
        "palette": palette,
        "image_provider": image_provider,
        "video_provider": video_provider if generate_videos else None,
        "design": design,
        "inferred_product": inferred,
        "files": files_done,
        "videos": videos_done if generate_videos else None,
        "description": description_text or None,
        "run_dir": run_dir,
        "raw_dir": raw_dir,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(os.path.join(run_dir, "_meta.json"), "w", encoding="utf-8") as fp:
            json.dump(meta, fp, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.warning(f"_meta.json write failed: {e}")
    try:
        with open(os.path.join(run_dir, "_log.txt"), "w", encoding="utf-8") as fp:
            fp.write("\n".join(log_lines))
    except Exception:
        pass

    step(f"✅ Готово. {run_dir}")

    final = {
        "files": files_done, "videos": videos_done,
        "description": description_text, "log": list(log_lines),
        "info": (f"✅ {len(files_done)} карточек" +
                 (f", {len(videos_done)} видео" if generate_videos else "") +
                 (f", описание {len(description_text)}c" if description_text else "")),
        "run_dir": run_dir, "design": design,
        "inferred_product": inferred, "done": True,
    }
    yield final


# ─── Backwards-compat wrapper: design + render in one streaming generator ───
def _stream_pack(cfg, mode, payload, style_key, ref_paths, style_ref_paths,
                 palette_override, image_provider, video_provider,
                 write_description, generate_videos, formats_selected,
                 video_effect="auto", user_facts=None):
    """
    Original streaming pipeline (design → render → videos → description).
    Kept as a thin wrapper over _prepare_design + _stream_render_from_prepared
    so all existing public entry points keep their original semantics.
    """
    # Initial yield so UIs that drain the generator get immediate state.
    yield {"files": [], "videos": [], "description": "", "log": [],
           "info": "Запускаюсь…", "run_dir": "", "design": None,
           "inferred_product": None, "done": False}

    # Design phase раньше блокировал всю генерацию на 30-90 сек тишины,
    # из-за чего сервер не слал ни одного SSE-байта и фронт умирал по
    # сетевому таймауту. Сейчас запускаем design в отдельном потоке и
    # шлём heartbeat каждые ~3 секунды.
    import threading as _threading
    import time as _t

    _design_result = {"prepared": None, "exc": None}
    _design_done = _threading.Event()

    def _design_worker():
        try:
            _design_result["prepared"] = _prepare_design(
                cfg, mode, payload, style_key, ref_paths, style_ref_paths,
                palette_override, image_provider, video_provider,
                write_description, generate_videos, formats_selected, video_effect,
                user_facts=user_facts,
            )
        except Exception as e:
            _design_result["exc"] = e
        finally:
            _design_done.set()

    _design_thread = _threading.Thread(target=_design_worker, daemon=True)
    _design_thread.start()

    waited = 0.0
    while not _design_done.wait(timeout=3.0):
        waited += 3.0
        yield {"heartbeat": True,
               "info": f"🧠 Claude проектирует пак… ({int(waited)}s)",
               "done": False}

    if _design_result["exc"] is not None:
        e = _design_result["exc"]
        logger.exception("design phase failed", exc_info=e)
        yield {"files": [], "videos": [], "description": "",
               "log": [f"❌ Дизайн упал: {e}"],
               "info": f"Ошибка дизайна: {e}", "run_dir": "", "design": None,
               "inferred_product": None, "done": True}
        return

    prepared = _design_result["prepared"]
    yield from _stream_render_from_prepared(cfg, prepared)


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC: streaming entry points
# ═══════════════════════════════════════════════════════════════════════════

def generate_pack_streaming(product, formats_selected, card_plan, style_key, ref_paths,
                             *, image_provider="veononstop", video_provider="fastgen_flower",
                             palette_override=None, style_ref_paths=None,
                             write_description=True, generate_videos=False,
                             video_effect="auto", cfg=None):
    cfg = cfg or load_config()
    yield from _stream_pack(
        cfg, "advanced",
        payload=(product, formats_selected, card_plan),
        style_key=style_key, ref_paths=ref_paths, style_ref_paths=style_ref_paths,
        palette_override=palette_override,
        image_provider=image_provider, video_provider=video_provider,
        write_description=write_description, generate_videos=generate_videos,
        formats_selected=formats_selected, video_effect=video_effect,
    )


def generate_pack_brief_streaming(brief, formats_selected, card_count, style_key, ref_paths,
                                   *, image_provider="veononstop", video_provider="fastgen_flower",
                                   palette_override=None, style_ref_paths=None,
                                   write_description=True, generate_videos=False,
                                   video_effect="auto", cfg=None):
    cfg = cfg or load_config()
    yield from _stream_pack(
        cfg, "brief",
        payload=(brief, formats_selected, card_count),
        style_key=style_key, ref_paths=ref_paths, style_ref_paths=style_ref_paths,
        palette_override=palette_override,
        image_provider=image_provider, video_provider=video_provider,
        write_description=write_description, generate_videos=generate_videos,
        formats_selected=formats_selected, video_effect=video_effect,
    )


def generate_pack_competitor_streaming(
    ref_paths: list,
    brief: str,
    competitor_analysis: dict,
    formats_selected: list,
    card_count: int,
    style_key: str,
    category_confirmed: str = "",
    *,
    image_provider: str = "veononstop",
    video_provider: str = "fastgen_flower",
    palette_override: dict = None,
    style_ref_paths: list = None,
    write_description: bool = True,
    generate_videos: bool = False,
    video_effect: str = "auto",
    user_facts: dict = None,
    cfg: dict = None,
):
    """Competitor-aware pack generation.

    Streaming generator (same yield shape as generate_pack_brief_streaming).

    Args:
        ref_paths:            User's product photos (sent to both Claude vision and image API).
        brief:                Free-form text description of the user's product (optional but helpful).
        competitor_analysis:  Output of wb_analyzer.analyze_competitors() — structured competitor intel.
        formats_selected:     List of format keys (e.g. ["wb_main"]).
        card_count:           How many cards per format to generate.
        style_key:            Style preset name (ignored when style_ref_paths provided).
        category_confirmed:   Confirmed product category string (shown to Claude for context).
        image_provider:       Image generation backend.
        video_provider:       Video generation backend.
        palette_override:     {"accent": "#HEX", "secondary": "#HEX"} override.
        style_ref_paths:      Optional reference card images for style extraction.
        write_description:    Also generate marketplace listing description.
        generate_videos:      Animate each card as it finishes.
        video_effect:         Motion effect for videos.
        cfg:                  Config dict (loaded from config.json if None).
    """
    cfg = cfg or load_config()
    yield from _stream_pack(
        cfg,
        "competitor",
        payload=(brief or "", formats_selected, card_count,
                 competitor_analysis or {}, category_confirmed or ""),
        style_key=style_key,
        ref_paths=ref_paths,
        style_ref_paths=style_ref_paths,
        palette_override=palette_override,
        image_provider=image_provider,
        video_provider=video_provider,
        write_description=write_description,
        generate_videos=generate_videos,
        formats_selected=formats_selected,
        video_effect=video_effect,
        user_facts=user_facts,
    )


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC: two-phase API (design → user edits → render)
# ═══════════════════════════════════════════════════════════════════════════

def prepare_competitor_design(
    ref_paths: list,
    brief: str,
    competitor_analysis: dict,
    formats_selected: list,
    card_count: int,
    style_key: str,
    category_confirmed: str = "",
    *,
    image_provider: str = "veononstop",
    video_provider: str = "fastgen_flower",
    palette_override: dict = None,
    style_ref_paths: list = None,
    write_description: bool = True,
    generate_videos: bool = False,
    video_effect: str = "auto",
    cfg: dict = None,
) -> dict:
    """
    Run ONLY the design phase: Claude generates the pack plan with headlines,
    sub-lines, badges, scene_overrides, uncertainties[] etc. Does NOT render images.

    Returns the `prepared` dict (suitable for render_pack_from_design()).
    Edit prepared["design"]["cards"][i].headline_ru / sub_ru / badges_ru / footer_ru
    before passing to render_pack_from_design.

    Returns:
      {
        "design":          dict — full design JSON (cards, uncertainties, concept, …),
        "inferred":        dict | None — inferred product (brief mode).
        "run_dir":         str — output folder already created.
        "log_lines":       list[str] — preliminary log to show in UI.
        ... + opaque internal fields needed by render_pack_from_design.
      }
    """
    cfg = cfg or load_config()
    return _prepare_design(
        cfg, "competitor",
        payload=(brief or "", formats_selected, card_count,
                 competitor_analysis or {}, category_confirmed or ""),
        style_key=style_key,
        ref_paths=ref_paths,
        style_ref_paths=style_ref_paths,
        palette_override=palette_override,
        image_provider=image_provider,
        video_provider=video_provider,
        write_description=write_description,
        generate_videos=generate_videos,
        formats_selected=formats_selected,
        video_effect=video_effect,
    )


def render_pack_from_design(prepared: dict, *, cfg: dict = None):
    """
    Streaming generator that takes a `prepared` dict from prepare_*_design()
    (possibly with user edits to design["cards"][i].headline_ru / sub_ru / badges_ru)
    and renders all cards + videos + description.

    Same yield shape as generate_pack_*_streaming().
    """
    cfg = cfg or load_config()
    yield from _stream_render_from_prepared(cfg, prepared)


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC: blocking shims (return last yield)
# ═══════════════════════════════════════════════════════════════════════════

def generate_pack(product, formats_selected, card_plan, style_key, ref_paths,
                  *, image_provider="veononstop", video_provider="fastgen_flower",
                  palette_override=None, style_ref_paths=None,
                  write_description=True, generate_videos=False,
                  video_effect="auto",
                  progress_cb=None, cfg=None,
                  provider=None):
    if provider is not None:
        image_provider = provider
    last = None
    for partial in generate_pack_streaming(
        product, formats_selected, card_plan, style_key, ref_paths,
        image_provider=image_provider, video_provider=video_provider,
        palette_override=palette_override, style_ref_paths=style_ref_paths,
        write_description=write_description, generate_videos=generate_videos,
        video_effect=video_effect, cfg=cfg,
    ):
        last = partial
        if progress_cb and partial.get("log"):
            try:
                # send only the latest line
                last_line = partial["log"][-1] if partial["log"] else ""
                progress_cb(last_line)
            except Exception:
                pass
    return last or {"files": [], "videos": [], "description": "", "log": [],
                    "run_dir": "", "design": None}


def generate_pack_brief(brief, formats_selected, card_count, style_key, ref_paths,
                        *, image_provider="veononstop", video_provider="fastgen_flower",
                        palette_override=None, style_ref_paths=None,
                        write_description=True, generate_videos=False,
                        video_effect="auto",
                        progress_cb=None, cfg=None,
                        provider=None):
    if provider is not None:
        image_provider = provider
    last = None
    for partial in generate_pack_brief_streaming(
        brief, formats_selected, card_count, style_key, ref_paths,
        image_provider=image_provider, video_provider=video_provider,
        palette_override=palette_override, style_ref_paths=style_ref_paths,
        write_description=write_description, generate_videos=generate_videos,
        video_effect=video_effect, cfg=cfg,
    ):
        last = partial
        if progress_cb and partial.get("log"):
            try:
                last_line = partial["log"][-1] if partial["log"] else ""
                progress_cb(last_line)
            except Exception:
                pass
    return last or {"files": [], "videos": [], "description": "", "log": [],
                    "run_dir": "", "design": None, "inferred_product": None}


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC: variations
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_VARIATION_PROVIDERS = ["fastgen_openai", "fastgen_nanopro", "fastgen_nanobanana2"]


def generate_style_variations(brief_or_product, n_variations=6, format_key="wb_main",
                              *, providers=None, ref_paths=None, style_ref_paths=None,
                              palette_override=None, progress_cb=None, cfg=None):
    cfg = cfg or load_config()
    log_lines = []

    def step(msg):
        log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        logger.info(msg)
        if progress_cb:
            try: progress_cb(msg)
            except Exception: pass

    palette = {"accent": "#FF4D6D", "secondary": "#1B1B2F"}
    if palette_override:
        palette = {**palette, **{k: v for k, v in palette_override.items() if v}}

    product_refs, style_refs = _prepare_refs(ref_paths, style_ref_paths)

    if providers is None or not providers:
        providers = DEFAULT_VARIATION_PROVIDERS

    name_for_slug = (brief_or_product.get("name") if isinstance(brief_or_product, dict)
                     else "variations")
    slug = _slugify(name_for_slug or "variations")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    run_dir = os.path.join(cfg["output_dir"], f"{slug}_{run_id}")
    raw_dir = os.path.join(cfg["tmp_dir"], run_id)
    os.makedirs(run_dir, exist_ok=True)

    step(f"📦 Variations · {n_variations}× · format={format_key} · providers={providers} · "
         f"refs={len(product_refs)}, style_refs={len(style_refs)}")

    step("🧠 Claude проектирует разные художественные подачи…")
    user_prompt = P.build_variations_user_prompt(
        brief_or_product=brief_or_product, formats=[format_key],
        n_variations=n_variations,
        has_refs=bool(product_refs), ref_names=[r["name"] for r in product_refs],
        palette=palette, has_style_ref=bool(style_refs),
    )
    # Vision-вариант (даунскейл) — как в _design_pack: дешевле, анализ тот же.
    imgs = [{"name": r["name"], "image": r.get("image_vision") or r["image"]}
            for r in (product_refs + style_refs)]
    design = None
    for source, retries in [("attempt-1", 1), ("attempt-2", 2)]:
        try:
            text = _claude_chat(cfg, P.VARIATIONS_SYSTEM_PROMPT, user_prompt,
                                images=imgs, purpose="variations",
                                retries_per_model=retries, dead_endpoint_threshold=1)
            design = P.extract_json(text)
            if "variants" not in design:
                raise ValueError("Ответ не содержит variants[]")
            break
        except (ValueError, RuntimeError) as e:
            logger.warning(f"[variations] {source} failed: {e}")
            design = None
    if design is None:
        raise ValueError("Все попытки (Claude + OpenAI) не вернули валидный JSON")
    step(f"✓ Концепт: {design.get('concept', '—')}")

    variants = design["variants"][:n_variations]
    aspect = fmt.FORMATS.get(format_key, {}).get("aspect", "3:4")
    size = fmt.FORMATS.get(format_key, {}).get("size", (900, 1200))

    step(f"🎨 Рендер {len(variants)} вариантов · циклически по провайдерам {providers}…")

    variations_tracker = _ProviderTracker()
    _rec_ctx = get_llm_context()  # снять в потоке-владельце для воркеров рендера

    def render_variant(variant, idx):
        provider = providers[idx % len(providers)]
        prompt = (variant.get("image_prompt") or "").strip()
        if not prompt:
            raise ValueError(f"variant {variant.get('id')} без image_prompt")
        prompt = _sanitize_tech_tokens(prompt) + _VARIATIONS_FORBID_TAIL
        raw_path = _render_card_with_fallback(
            cfg, provider, prompt, aspect,
            product_refs, style_refs, raw_dir, idx,
            tracker=variations_tracker, log_fn=step, rec_ctx=_rec_ctx,
        )
        return variant, provider, raw_path

    rendered = {}
    failures = []
    with ThreadPoolExecutor(max_workers=min(4, len(variants) or 1)) as ex:
        futs = {ex.submit(render_variant, v, i): (i, v) for i, v in enumerate(variants)}
        for fut in __import__("concurrent.futures", fromlist=["as_completed"]).as_completed(futs):
            i, v = futs[fut]
            try:
                variant, provider, raw_path = fut.result()
                rendered[i] = (variant, provider, raw_path)
                step(f"  ✓ [{i+1}/{len(variants)}] {provider} — {variant.get('style_label','?')}")
            except Exception as e:
                err = str(e)[:200]
                failures.append((i, v, err))
                step(f"  ✗ [{i+1}/{len(variants)}] ОШИБКА: {err}")

    step("✂️ Финализирую…")
    files = []
    for i in sorted(rendered.keys()):
        variant, provider, raw_path = rendered[i]
        out_name = f"var_{i+1:02d}__{provider.replace('fastgen_','fg-')}__{_slugify(variant.get('style_label','x'))}.jpg"
        out_path = os.path.join(run_dir, out_name)
        try:
            overlay.finalize_card(raw_path, size, out_path)
            files.append(out_path)
            step(f"  ✓ {out_name}")
        except Exception as e:
            step(f"  ✗ {out_name}: {e}")

    meta = {
        "mode": "variations",
        "input": brief_or_product if isinstance(brief_or_product, dict) else {"brief": brief_or_product},
        "format": format_key, "n_variations": n_variations,
        "providers": providers, "palette": palette, "design": design,
        "files": files,
        "failures": [{"idx": i, "variant": v, "error": e} for i, v, e in failures],
        "run_dir": run_dir, "raw_dir": raw_dir,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(os.path.join(run_dir, "_meta.json"), "w", encoding="utf-8") as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(run_dir, "_log.txt"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(log_lines))

    step(f"✅ Готово. {run_dir} ({len(files)} вариантов)")
    return {"run_dir": run_dir, "files": files, "log": log_lines, "design": design}


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC: standalone description / videos
# ═══════════════════════════════════════════════════════════════════════════

def generate_description(product_or_design, *, competitor_analysis=None,
                         user_facts=None, cfg=None):
    cfg = cfg or load_config()
    user_prompt = P.build_description_user_prompt(product_or_design, competitor_analysis)
    text = _claude_chat(cfg, P.DESCRIPTION_SYSTEM_PROMPT, user_prompt,
                        images=None, max_tokens=2500, temperature=0.5,
                        purpose="description").strip()
    # Same post-filter как в стриминговой ветке: выкидывает выдуманные числа
    # и hedge-слова. Если user_facts не передан — фильтр снимет ВСЕ числа
    # с единицами (правильно: без подтверждённых фактов их быть не должно).
    text, _ = P.sanitize_description_numbers(text, user_facts or {})
    return text


def regenerate_description(current_description, user_feedback, *,
                           product_or_design=None, competitor_analysis=None,
                           user_facts=None, cfg=None):
    """Переписать SEO-описание с учётом правки пользователя.

    Зеркалит логику правки картинки (regenerate-card): на вход — текущий текст
    и пожелание «что не нравится / что поменять», на выход — новый цельный текст.
    Контекст товара (design/inferred_product) и конкуренты сохраняются, чтобы
    описание осталось предметным, а не «водой».
    """
    cfg = cfg or load_config()
    base = product_or_design or {}
    base_prompt = P.build_description_user_prompt(base, competitor_analysis)
    feedback = (user_feedback or "").strip()
    current = (current_description or "").strip()
    edit_block = (
        "\n\n━━━ ПРАВКА ОТ ПОЛЬЗОВАТЕЛЯ ━━━\n"
        "Ниже — текущее описание. Перепиши его, ВЫПОЛНИВ пожелание пользователя, "
        "сохранив фактуру о товаре и не выдумывая новых числовых характеристик.\n\n"
        f"ТЕКУЩЕЕ ОПИСАНИЕ:\n\"\"\"\n{current or '—'}\n\"\"\"\n\n"
        f"ЧТО ИЗМЕНИТЬ: «{feedback}»\n\n"
        "Верни ТОЛЬКО новый текст описания (без преамбулы). Лимит — 1000 символов."
    )
    text = _claude_chat(cfg, P.DESCRIPTION_SYSTEM_PROMPT, base_prompt + edit_block,
                        images=None, max_tokens=2500, temperature=0.6,
                        purpose="description").strip()
    text, _ = P.sanitize_description_numbers(text, user_facts or {})
    return text


def generate_videos_for_files(files, *, run_dir=None, card_metas=None,
                               video_provider="fastgen_flower",
                               video_effect="auto",
                               effects_per_file=None,
                               progress_cb=None, cfg=None):
    """Animate a list of card images. Each card gets its own motion prompt.

    Aspect auto-detected from card_meta.format_key if available, else 9:16.

    Per-card effect override:
      effects_per_file: dict {file_path: effect_key} — if provided, each path uses
      its own effect from this map; falls back to global `video_effect` for paths
      not in the map.
    """
    cfg = cfg or load_config()
    log_lines = []

    def step(msg):
        log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        logger.info(msg)
        if progress_cb:
            try: progress_cb(msg)
            except Exception: pass

    out_dir = run_dir or os.path.join(
        cfg["output_dir"], "_videos_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    effects_per_file = effects_per_file or {}
    if effects_per_file:
        unique_effects = sorted(set(effects_per_file.values()) | {video_effect})
        step(f"🎬 Анимирую {len(files)} карточек через {video_provider} · "
             f"эффекты per-card: {unique_effects}")
    else:
        step(f"🎬 Анимирую {len(files)} карточек через {video_provider} · "
             f"эффект: {video_effect}")

    def _resolve_meta(path):
        if not card_metas: return None
        bn = os.path.basename(path)
        for cid, meta in card_metas.items():
            if cid and cid in bn:
                return meta
        return None

    def _aspect_for(meta):
        if meta and meta.get("format_key"):
            return fmt.FORMATS.get(meta["format_key"], {}).get("aspect", "9:16")
        return "9:16"

    def animate_one(path):
        meta = _resolve_meta(path)
        eff = effects_per_file.get(path) or effects_per_file.get(os.path.basename(path)) or video_effect
        try:
            motion = _make_motion_prompt(cfg, meta or {}, video_effect=eff)
        except Exception:
            motion = _VIDEO_EFFECT_FALLBACKS.get(eff, _VIDEO_EFFECT_FALLBACKS["auto"])
        out_mp4 = os.path.splitext(path)[0] + ".mp4"
        return _render_card_video(cfg, video_provider, path, motion,
                                   _aspect_for(meta), out_mp4)

    videos = []
    failures = []
    from concurrent.futures import as_completed
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="vid") as ex:
        futs = {ex.submit(animate_one, p): p for p in files}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                out_mp4 = fut.result()
                videos.append(out_mp4)
                step(f"  ✓ {os.path.basename(out_mp4)}")
            except Exception as e:
                err = str(e)[:200]
                failures.append((p, err))
                step(f"  ✗ {os.path.basename(p)}: {err}")

    return {"videos": videos, "failures": failures, "log": log_lines}


def generate_videos_for_run(run_dir, *, video_provider="fastgen_flower",
                             video_effect="auto",
                             effects_per_file=None,
                             progress_cb=None, cfg=None):
    cfg = cfg or load_config()
    meta_path = os.path.join(run_dir, "_meta.json")
    if not os.path.isfile(meta_path):
        raise FileNotFoundError(f"Нет _meta.json в {run_dir}")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    files = [p for p in meta.get("files", []) if os.path.isfile(p)]
    if not files:
        raise RuntimeError("В этой папке нет файлов для анимации")
    cards = (meta.get("design") or {}).get("cards") or (meta.get("design") or {}).get("variants") or []
    card_metas = {c.get("id"): c for c in cards if c.get("id")}
    return generate_videos_for_files(
        files, run_dir=run_dir, card_metas=card_metas,
        video_provider=video_provider,
        video_effect=video_effect,
        effects_per_file=effects_per_file,
        progress_cb=progress_cb, cfg=cfg,
    )


def identify_pool_products(brief: str, photo_paths: list, cfg: dict = None) -> tuple:
    """Vision-aware identification: send N photos + 1 brief to Claude, get back
    list of {photo_index, name, category, brief}. Falls back to positional matching
    (first photo = first product mentioned, etc.) if vision is unavailable or fails.

    Returns: (list[dict], is_fallback: bool)
      list[dict] keys: photo_indices, photo_paths, name, category, brief, color_keywords
      is_fallback=True when positional fallback was used or anti-collage split was forced
    """
    cfg = cfg or load_config()

    # Filter to actually-existing files
    valid_paths = []
    for p in photo_paths or []:
        if p and os.path.isfile(p):
            valid_paths.append(p)
        elif p:
            logger.warning(f"[pool] photo missing: {p}")
    if not valid_paths:
        raise RuntimeError("Нет валидных фото для Pool-режима — проверь, что файлы загрузились")

    # Build vision payload
    photo_uris = []
    for i, p in enumerate(valid_paths):
        try:
            photo_uris.append({"name": f"photo{i+1}",
                               "image": _file_to_data_uri(p, max_side=_VISION_MAX_SIDE)})
        except Exception as e:
            logger.warning(f"[pool] failed to read {p}: {e}")

    if not photo_uris:
        raise RuntimeError("Не удалось прочитать ни одно фото — проверь файлы")

    # Try vision identification
    products = []
    try:
        user_prompt = P.build_pool_identify_user_prompt(brief, len(photo_uris))
        text = _claude_chat(cfg, P.POOL_IDENTIFY_SYSTEM_PROMPT, user_prompt,
                            images=photo_uris, max_tokens=4000, temperature=0.3,
                            purpose="pool_identify")
        data = P.extract_json(text)
        products = data.get("products") or []
    except Exception as e:
        logger.warning(f"[pool] vision identify failed: {str(e)[:200]} — fallback на positional")
        products = []

    # Validate vision output. Support both new schema (photo_indices: list)
    # and old schema (photo_index: int). Group multi-photo products.
    valid = []
    used = set()
    for entry in products:
        if not isinstance(entry, dict):
            continue
        idxs = entry.get("photo_indices")
        if not isinstance(idxs, list):
            idx = entry.get("photo_index")
            idxs = [idx] if isinstance(idx, int) else []
        # Filter to valid + dedupe
        filtered = []
        for i in idxs:
            if isinstance(i, int) and 0 <= i < len(valid_paths) and i not in used:
                filtered.append(i)
                used.add(i)
        if not filtered:
            continue
        paths = [valid_paths[i] for i in filtered]
        valid.append({
            "photo_indices": filtered,
            "photo_paths": paths,
            "name": (entry.get("name") or f"product_{filtered[0]+1}").strip(),
            "category": (entry.get("category") or "Прочее").strip(),
            "brief": (entry.get("brief") or brief).strip(),
            "color_keywords": entry.get("color_keywords") or [],
        })

    if valid:
        n_photos = len(valid_paths)
        # Anti-collage guard: if Claude merged ALL photos into one product, force split.
        # Sending N different items as refs to one image render causes multi-item collages.
        if len(valid) == 1 and len(valid[0]["photo_indices"]) == n_photos and n_photos > 1:
            logger.warning(
                f"[pool] anti-collage: all {n_photos} photos merged into 1 product — "
                f"forcing positional split to prevent multi-item collage render"
            )
            return [
                {"photo_indices": [i], "photo_paths": [p],
                 "name": f"Товар {i+1}", "category": "Прочее",
                 "brief": brief, "color_keywords": []}
                for i, p in enumerate(valid_paths)
            ], True
        # Cap refs per product to 4 — prevents partial collages from excessive angle shots.
        for entry in valid:
            if len(entry["photo_indices"]) > 4:
                entry["photo_indices"] = entry["photo_indices"][:4]
                entry["photo_paths"] = entry["photo_paths"][:4]
        return valid, False

    # POSITIONAL FALLBACK: each photo gets the full brief as-is.
    logger.warning(f"[pool] vision empty/failed — positional fallback ({len(valid_paths)} photos)")
    return [
        {
            "photo_indices": [i],
            "photo_paths": [p],
            "name": f"Товар {i+1}",
            "category": "Прочее",
            "brief": brief,
            "color_keywords": [],
        }
        for i, p in enumerate(valid_paths)
    ], True


def generate_pack_pool_streaming(brief: str, photo_paths: list,
                                 *, formats_selected=None, card_count=4,
                                 style_key="premium",
                                 style_ref_paths=None,
                                 image_provider="veononstop",
                                 video_provider="fastgen_keyframes",
                                 palette_override=None,
                                 write_description=True,
                                 generate_videos=False,
                                 video_effect="rotate",
                                 cfg=None):
    """Multi-product pool. Identifies which photo is which product, then runs a separate
    pack for each. Yields the same shape as generate_pack_brief_streaming, but with an
    additional 'product_idx' / 'product_name' tag per yield, and files accumulate across
    all packs in one flat list.
    """
    cfg = cfg or load_config()
    formats_selected = formats_selected or ["wb_main"]

    log_lines = []
    def step(msg):
        log_lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        logger.info(msg)

    yield {"files": [], "videos": [], "description": "",
           "log": list(log_lines), "info": "🔍 Распознаю товары на фото…",
           "run_dir": "", "design": None, "inferred_product": None, "done": False}

    # Validate inputs early — clear error to UI
    photo_paths = photo_paths or []
    valid_photo_paths = [p for p in photo_paths if p and os.path.isfile(p)]
    if not valid_photo_paths:
        msg = (f"❌ Загружено {len(photo_paths)} фото, но валидных файлов 0. "
               f"Перезагрузи фото в форму.")
        step(msg)
        yield {"files": [], "videos": [], "description": "",
               "log": list(log_lines), "info": msg,
               "run_dir": "", "design": None, "inferred_product": None, "done": True}
        return

    step(f"🔍 Идентифицирую товары: {len(valid_photo_paths)} фото + бриф ({len(brief)} сим.)")
    try:
        products, _pool_fallback = identify_pool_products(brief, valid_photo_paths, cfg=cfg)
    except Exception as e:
        step(f"❌ Идентификация упала: {type(e).__name__}: {str(e)[:300]}")
        yield {"files": [], "videos": [], "description": "",
               "log": list(log_lines), "info": f"❌ Идентификация: {str(e)[:200]}",
               "run_dir": "", "design": None, "inferred_product": None, "done": True}
        return

    if not products:
        step("⚠ Ни одного товара не распознано")
        yield {"files": [], "videos": [], "description": "",
               "log": list(log_lines), "info": "⚠ Не распознано ни одного товара",
               "run_dir": "", "design": None, "inferred_product": None, "done": True}
        return

    if _pool_fallback:
        step("⚠️ Авторасклад фото→товар не дал точного результата — использован позиционный расклад (1 фото = 1 товар). Убедись, что на каждом фото отдельный товар.")

    step(f"✓ Распознано {len(products)} товаров: " +
         ", ".join(f"«{p.get('name','?')}» ({len(p.get('photo_paths',[]))} фото)"
                   for p in products))

    # ─── PARALLEL execution ──────────────────────────────────────────────────
    # Each product runs in its own thread; partial results stream into a shared
    # queue. The main loop merges them into a single yield to the UI.
    import threading
    from queue import Queue, Empty

    state_queue: Queue = Queue()

    def _run_product(prod_i, prod):
        try:
            for partial in generate_pack_brief_streaming(
                brief=prod["brief"],
                formats_selected=formats_selected,
                card_count=card_count,
                style_key=style_key,
                ref_paths=prod["photo_paths"],     # ALL photos for this product
                image_provider=image_provider,
                video_provider=video_provider,
                palette_override=palette_override,
                style_ref_paths=style_ref_paths,
                write_description=write_description,
                generate_videos=generate_videos,
                video_effect=video_effect,
                cfg=cfg,
            ):
                state_queue.put(("partial", prod_i, partial))
        except Exception as e:
            state_queue.put(("error", prod_i, str(e)[:300]))
        finally:
            state_queue.put(("done", prod_i, None))

    threads = []
    for i, prod in enumerate(products):
        t = threading.Thread(target=_run_product, args=(i, prod),
                             daemon=True, name=f"pool-prod-{i}")
        t.start()
        threads.append(t)

    step(f"⚡ Запущено {len(products)} товаров параллельно (по своему run_dir каждый)")

    # Per-product accumulators
    files_per = {i: [] for i in range(len(products))}
    videos_per = {i: [] for i in range(len(products))}
    desc_per = {i: "" for i in range(len(products))}
    run_dir_per = {i: "" for i in range(len(products))}
    completed = 0

    def _aggregate():
        all_f = []
        for i in range(len(products)):
            for f in files_per[i]:
                if f not in all_f:
                    all_f.append(f)
        all_v = []
        for i in range(len(products)):
            for v in videos_per[i]:
                if v not in all_v:
                    all_v.append(v)
        # Format descriptions with product headers
        desc_blocks = []
        for i, p in enumerate(products):
            d = desc_per[i].strip()
            if d:
                title = p.get("name", f"Товар {i+1}")
                rd = run_dir_per[i]
                hdr = f"╔════════════════════════════════════════════\n║  📦 {title}\n"
                if rd:
                    hdr += f"║  📁 {rd}\n"
                hdr += "╚════════════════════════════════════════════"
                desc_blocks.append(hdr + "\n\n" + d)
        all_desc = "\n\n\n".join(desc_blocks)
        # info
        ready_count = sum(1 for i in range(len(products))
                          if files_per[i] and len(files_per[i]) >= 1)
        last_run = next((run_dir_per[i] for i in range(len(products)-1, -1, -1)
                         if run_dir_per[i]), "")
        info = (f"📦 {completed}/{len(products)} товаров готово · "
                f"{len(all_f)} карточек, {len(all_v)} видео")
        return all_f, all_v, all_desc, last_run, info

    yield {"files": [], "videos": [], "description": "",
           "log": list(log_lines),
           "info": f"📦 0 / {len(products)} товаров",
           "run_dir": "", "design": None,
           "inferred_product": None, "done": False}

    while completed < len(products):
        try:
            evt, prod_i, data = state_queue.get(timeout=2.0)
        except Empty:
            # Heartbeat — still emit the current aggregate state
            all_f, all_v, all_desc, last_run, info = _aggregate()
            yield {"files": all_f, "videos": all_v, "description": all_desc,
                   "log": list(log_lines), "info": info, "run_dir": last_run,
                   "design": None, "inferred_product": None, "done": False}
            continue

        if evt == "partial":
            files_per[prod_i] = data.get("files") or files_per[prod_i]
            videos_per[prod_i] = data.get("videos") or videos_per[prod_i]
            if data.get("description"):
                desc_per[prod_i] = data["description"]
            if data.get("run_dir"):
                run_dir_per[prod_i] = data["run_dir"]
            # Merge per-product log into pool log only on `done` (avoid noise)
        elif evt == "error":
            step(f"  ✗ Товар «{products[prod_i].get('name','?')}» упал: {data}")
        elif evt == "done":
            completed += 1
            files_n = len(files_per[prod_i])
            videos_n = len(videos_per[prod_i])
            step(f"✓ Товар {prod_i+1}/{len(products)} «{products[prod_i].get('name','?')}» "
                 f"готов: {files_n} карточек, {videos_n} видео")

        all_f, all_v, all_desc, last_run, info = _aggregate()
        yield {"files": all_f, "videos": all_v, "description": all_desc,
               "log": list(log_lines), "info": info, "run_dir": last_run,
               "design": None, "inferred_product": None, "done": False}

    # All done
    all_f, all_v, all_desc, last_run, info = _aggregate()
    step(f"✅ Pool готов: {len(all_f)} карточек, {len(all_v)} видео из "
         f"{len(products)} товаров (параллельно)")
    yield {"files": all_f, "videos": all_v, "description": all_desc,
           "log": list(log_lines),
           "info": f"✅ {len(products)} товаров · {len(all_f)} карточек, {len(all_v)} видео",
           "run_dir": last_run, "design": None,
           "inferred_product": None, "done": True}


def list_run_files(run_dir):
    """Returns sorted list of (file_path, basename) for cards in a run_dir.
    UI calls this to populate the per-card effect picker.
    """
    if not run_dir or not os.path.isdir(run_dir):
        return []
    files = []
    for f in sorted(os.listdir(run_dir)):
        full = os.path.join(run_dir, f)
        if os.path.isfile(full) and f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            files.append((full, f))
    return files


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("product_json")
    ap.add_argument("--mode", choices=("advanced", "brief", "variations", "videos-for-run"),
                    default="brief")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--videos", action="store_true")
    ap.add_argument("--no-description", action="store_true")
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--video-provider", default="fastgen_flower")
    args = ap.parse_args()

    if args.mode == "videos-for-run":
        res = generate_videos_for_run(args.run_dir, video_provider=args.video_provider,
                                      progress_cb=lambda m: print(m))
        print(f"\n✅ {len(res['videos'])} видео")
    else:
        with open(args.product_json, "r", encoding="utf-8") as f:
            spec = json.load(f)
        if args.mode == "advanced":
            res = generate_pack(
                product=spec["product"],
                formats_selected=spec.get("formats", ["wb_main"]),
                card_plan=spec.get("card_plan", {"main": 1, "feature": 3}),
                style_key=spec.get("style", "premium"),
                ref_paths=spec.get("photos", []),
                style_ref_paths=spec.get("style_photos", []),
                image_provider=spec.get("image_provider", spec.get("provider", "veononstop")),
                video_provider=spec.get("video_provider", args.video_provider),
                palette_override=spec.get("palette"),
                write_description=not args.no_description,
                generate_videos=args.videos,
                progress_cb=lambda m: print(m),
            )
        elif args.mode == "brief":
            res = generate_pack_brief(
                brief=spec["brief"],
                formats_selected=spec.get("formats", ["wb_main"]),
                card_count=spec.get("card_count", 4),
                style_key=spec.get("style", "premium"),
                ref_paths=spec.get("photos", []),
                style_ref_paths=spec.get("style_photos", []),
                image_provider=spec.get("image_provider", spec.get("provider", "veononstop")),
                video_provider=spec.get("video_provider", args.video_provider),
                palette_override=spec.get("palette"),
                write_description=not args.no_description,
                generate_videos=args.videos,
                progress_cb=lambda m: print(m),
            )
        elif args.mode == "variations":
            inp = spec.get("brief") or spec.get("product")
            res = generate_style_variations(
                brief_or_product=inp,
                n_variations=args.n,
                format_key=spec.get("format_key", "wb_main"),
                providers=spec.get("providers"),
                ref_paths=spec.get("photos", []),
                style_ref_paths=spec.get("style_photos", []),
                palette_override=spec.get("palette"),
                progress_cb=lambda m: print(m),
            )
        print(f"\nГотово: {res['run_dir']} ({len(res['files'])} файлов)")
