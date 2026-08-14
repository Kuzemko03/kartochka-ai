"""OpenAI **GPT Image 2** direct client.

Drop-in рядом с FastGenV5Client — экспонирует ТЕ ЖЕ
`generate_image(...) -> (ok, path_or_err, etype)` и `is_available()`,
чтобы generator.py подключил его одной веткой в _render_card_image_raw.

Зачем отдельный клиент, а не fastgen_openai (ChatGPT-flow через FastGen):
  • FastGen берёт за ChatGPT-картинку 1 кредит и не пропускает quality/size
    как «рычаги» — только aspect_ratio. Прямой OpenAI bills по quality/size
    напрямую, и админ может реально выкрутить low/medium/high.
  • Это «фолбэк-toggle»: в админке переключатель default_provider даёт юзеру
    выбор между fastgen_* и openai_direct в реал-тайме (через _reload_server_cfg).

Эндпоинты:
  • images/edits         — multipart с полем image[] (1–16 рефов, png/jpeg/webp).
    Используется КОГДА есть хоть один реф (а у нас почти всегда есть product1).
  • images/generations   — JSON. Используется когда рефов нет.

Параметры:
  • model:   "gpt-image-2"  (или snapshot, читается из cfg.openai_image_model)
  • size:    "1024x1024" | "1024x1536" | "1536x1024" | "auto"
  • quality: "low" | "medium" | "high" | "auto"

Refs: те же объекты, что приходят к FastGen V5 (dict {name,image,image_vision}
или строка-data-URI / 32-hex storage id / http URL / локальный путь). storage id
не поддерживается OpenAI (это id на стороне fast-gen.ai); такие refs пропускаем.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("wbnator.openai_image")


def _safe_err(e: Exception, max_len: int = 200) -> str:
    s = f"{type(e).__name__}: {str(e)}"
    if len(s) > max_len:
        s = s[:max_len] + f"... <truncated {len(s) - max_len} chars>"
    return s


_HEX32_RE = re.compile(r"^[a-f0-9]{32}$")

# Какие size принимает gpt-image-2 в наших UI-пресетах. auto → модель решит.
ALLOWED_SIZES = ("1024x1024", "1024x1536", "1536x1024", "auto")
ALLOWED_QUALITIES = ("low", "medium", "high", "auto")
# input_fidelity у gpt-image-2 НЕ поддерживается — модель отвечает 400
# "does not support the 'input_fidelity' parameter". При этом она и так
# держит рефы в режиме high fidelity автоматически (см. fal.ai release notes).
# Поэтому параметр не передаём вообще.

# Маппинг aspect_ratio (как у FastGen) → фиксированный size, когда юзер
# в админке оставил size=auto и хочет, чтобы клиент выбрал сам.
_ASPECT_TO_SIZE: Dict[str, str] = {
    "1:1":  "1024x1024",
    "3:4":  "1024x1536",
    "9:16": "1024x1536",
    "2:3":  "1024x1536",
    "4:3":  "1536x1024",
    "16:9": "1536x1024",
    "3:2":  "1536x1024",
}


def _sniff_image_mime(raw: bytes) -> str:
    """MIME по magic bytes (расширения и Content-Type врут; WB CDN шлёт webp под .jpg)."""
    if len(raw) < 12:
        return "application/octet-stream"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] == b"BM":
        return "image/bmp"
    if raw[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if raw[4:8] == b"ftyp" and raw[8:12] in (b"avif", b"avis", b"heic", b"heix",
                                              b"mif1", b"msf1", b"hevc", b"hevx"):
        return "image/heic" if raw[8:12] not in (b"avif", b"avis") else "image/avif"
    return "application/octet-stream"


def _convert_to_png_bytes(raw: bytes) -> Optional[bytes]:
    """Любой неподдерживаемый формат → PNG-байты для multipart."""
    try:
        from PIL import Image  # type: ignore
        img = Image.open(io.BytesIO(raw))
        try:
            img.seek(0)  # первый кадр у GIF/WEBP/AVIF
        except Exception:
            pass
        if img.mode == "P":
            img = img.convert("RGBA")
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as e:
        logger.warning(f"[openai-image] ref convert failed: {_safe_err(e)}")
        return None


def _aspect_to_size(aspect: str, fallback: str = "1024x1024") -> str:
    return _ASPECT_TO_SIZE.get(aspect or "", fallback)


def _resolve_size(requested: Optional[str], aspect: str) -> str:
    """size из cfg → один из ALLOWED_SIZES. 'auto' оставляем как есть (модель выберет)."""
    if requested in ALLOWED_SIZES:
        if requested == "auto":
            return "auto"
        return requested
    return _aspect_to_size(aspect, "1024x1024")


def _resolve_quality(requested: Optional[str]) -> str:
    if requested in ALLOWED_QUALITIES:
        return requested
    return "high"  # дефолт для платного прямого OpenAI — самое сильное качество


class OpenAIImageClient:
    """Клиент к /v1/images/* для gpt-image-2.

    Совместим с FastGenV5Client по `generate_image(...)` и `is_available()`.
    Параметры quality/size читаются из cfg на КАЖДЫЙ вызов — чтобы переключатель
    в админке (PUT /api/admin/settings + _reload_server_cfg) применялся сразу.
    """

    def __init__(self,
                 api_key: str,
                 base_url: str = "https://api.openai.com/v1",
                 model: str = "gpt-image-2",
                 proxy_url: Optional[str] = None,
                 organization: Optional[str] = None,
                 timeout: int = 90):   # per-attempt READ timeout (s).
        # ВАЖНО: timeout (read) ДОЛЖЕН быть < max_wait в _post_with_retry,
        # иначе единственная попытка зависшего gpt-image-запроса блокирует
        # весь пак на ~timeout секунд и ретрай НИКОГДА не срабатывает.
        # Было 600 (10 мин) при max_wait=240 → зависший запрос морозил
        # генерацию на 10 минут без единого ретрая. 90 < 240 → зависшая
        # попытка обрывается за 90с и перезапускается на ТОЙ ЖЕ модели/
        # качестве (до ~3 попыток в бюджете). NB: успешный medium-рендер
        # бывает ~85-90с — если погонишь quality=high, подними этот таймаут.
        self.api_key = api_key or ""
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.model = model or "gpt-image-2"
        self.proxy_url = proxy_url or None
        self.organization = organization or None
        self.timeout = timeout
        self.session = requests.Session()

    # ── infra ────────────────────────────────────────────────────────────
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if self.organization:
            h["OpenAI-Organization"] = self.organization
        return h

    def _proxies(self) -> Optional[dict]:
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    # ── ref normalisation: → (filename, bytes, mime) или None ────────────
    def _ref_to_bytes(self, ref: Any) -> Optional[Tuple[str, bytes, str]]:
        """Разрешаем data URI / локальный путь / http URL / сырые bytes →
        (filename, raw_bytes, mime). storage id (fast-gen) пропускаем."""
        try:
            name = None
            raw_input = ref
            if isinstance(ref, dict):
                raw_input = ref.get("image") or ref.get("input") or ref.get("path")
                name = ref.get("name") or ref.get("filename")
            if isinstance(raw_input, (bytes, bytearray)):
                raw = bytes(raw_input)
            elif isinstance(raw_input, str):
                s = raw_input.strip()
                if not s:
                    return None
                if _HEX32_RE.match(s):
                    # storage id живёт на стороне fast-gen.ai → к OpenAI напрямую
                    # его не подключить. Пропускаем (чтобы не уронить весь рендер).
                    logger.info("[openai-image] ref skipped: storage id (only valid in FastGen)")
                    return None
                if s.startswith("data:"):
                    try:
                        _, b64 = s.split(",", 1)
                        raw = base64.b64decode(b64)
                    except Exception:
                        return None
                elif s.startswith(("http://", "https://")):
                    r = requests.get(s, timeout=60, proxies=self._proxies())
                    if r.status_code != 200 or len(r.content) < 100:
                        return None
                    raw = r.content
                else:
                    p = Path(s)
                    if not (p.exists() and p.is_file()):
                        return None
                    raw = p.read_bytes()
                    name = name or p.name
            else:
                return None

            mime = _sniff_image_mime(raw)
            if mime not in ("image/png", "image/jpeg", "image/webp"):
                # OpenAI принимает png/jpeg/webp; всё прочее перегоняем в PNG.
                converted = _convert_to_png_bytes(raw)
                if converted is None:
                    return None
                raw = converted
                mime = "image/png"

            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
            if not name:
                name = f"ref.{ext}"
            elif "." not in name:
                name = f"{name}.{ext}"
            else:
                stem, _, _ = name.rpartition(".")
                name = f"{stem}.{ext}"
            return name, raw, mime
        except Exception as e:
            logger.warning(f"[openai-image] ref resolve failed: {_safe_err(e)}")
            return None

    def _build_image_files(self, reference_images: Optional[List],
                            max_refs: int = 16) -> List[Tuple[str, bytes, str]]:
        out: List[Tuple[str, bytes, str]] = []
        if not reference_images:
            return out
        for ref in reference_images:
            if len(out) >= max_refs:
                break
            triple = self._ref_to_bytes(ref)
            if triple is None:
                continue
            out.append(triple)
        return out

    # ── HTTP retry helper ────────────────────────────────────────────────
    def _post_with_retry(self, url: str, *, data=None, files=None, json_body=None,
                         max_wait: int = 240) -> Tuple[bool, Any, str]:
        """Возвращает (ok, response_json_or_err_str, etype).
        etype ∈ {none, rate_limit, server, content, fatal}.
        """
        start = time.time()
        a429 = a5xx = 0
        while True:
            try:
                if json_body is not None:
                    r = self.session.post(url, headers={**self._headers(),
                                                         "Content-Type": "application/json"},
                                          json=json_body, timeout=(15, self.timeout),
                                          proxies=self._proxies())
                else:
                    r = self.session.post(url, headers=self._headers(),
                                          data=data, files=files,
                                          timeout=(15, self.timeout), proxies=self._proxies())
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                a5xx += 1
                if time.time() - start > max_wait:
                    return False, _safe_err(e) + " (after retries)", "server"
                time.sleep(min(2 ** a5xx, 30))
                continue
            except Exception as e:
                return False, _safe_err(e), "server"

            if r.status_code == 200:
                try:
                    return True, r.json(), "none"
                except Exception as e:
                    return False, f"Bad JSON: {e}", "server"

            # Распаковываем сообщение об ошибке (OpenAI шлёт {error:{message,code,type}})
            err_msg, err_code, err_type = "", "", ""
            try:
                payload = r.json()
                err = payload.get("error") or {}
                err_msg = (err.get("message") or "")[:300]
                err_code = err.get("code") or ""
                err_type = err.get("type") or ""
            except Exception:
                err_msg = (r.text or "")[:300]

            up = (err_msg + " " + err_code + " " + err_type).upper()

            if r.status_code in (401, 403):
                return False, f"Auth {r.status_code}: {err_msg}", "fatal"
            if r.status_code == 429:
                a429 += 1
                if time.time() - start > max_wait or a429 > 6:
                    return False, f"Rate limit: {err_msg}", "rate_limit"
                time.sleep(min(1.5 + a429 * 1.5, 20.0))
                continue
            if r.status_code == 400:
                # safety / content_policy — отдельная категория, чтобы fallback
                # переписал промпт мягче, не валил рендер фаталом
                if any(x in up for x in ("CONTENT_POLICY", "SAFETY", "MODERATION",
                                          "MODERATION_BLOCKED", "VIOLATION", "POLICY")):
                    return False, f"Content policy: {err_msg}", "content"
                return False, f"Validation: {err_msg}", "fatal"
            if r.status_code in (500, 502, 503, 504):
                a5xx += 1
                if time.time() - start > max_wait or a5xx > 4:
                    return False, f"HTTP {r.status_code}: {err_msg}", "server"
                time.sleep(min(2 ** a5xx, 20))
                continue
            return False, f"HTTP {r.status_code}: {err_msg}", "server"

    # ── public: V5-compatible generate_image ─────────────────────────────
    def generate_image(self,
                       prompt: str,
                       provider: str = "openai_direct",
                       aspect_ratio: str = "3:4",
                       reference_images: Optional[List] = None,
                       output_dir: Optional[Path] = None,
                       index: int = 0,
                       seed: Optional[int] = None,
                       progress_cb=None,
                       named_refs: bool = True,
                       # переопределения из админки (если cfg-driven обёртка их передаёт)
                       quality: Optional[str] = None,
                       size: Optional[str] = None,
                       output_format: str = "png",
                       **_ignored) -> Tuple[bool, str, str]:
        """Возвращает (ok, path_or_err, etype) — точно тот же контракт,
        что и FastGenV5Client.generate_image, чтобы _render_card_image_raw
        не различал клиентов."""
        t0 = time.time()
        if not self.is_available():
            return False, "OpenAI image client unavailable (no api key)", "fatal"

        resolved_size = _resolve_size(size, aspect_ratio)
        resolved_quality = _resolve_quality(quality)
        files_payload = self._build_image_files(reference_images)
        out_format = output_format if output_format in ("png", "jpeg", "webp") else "png"

        if progress_cb:
            try:
                progress_cb(f"openai gpt-image-2 {resolved_size} q={resolved_quality} "
                            f"refs={len(files_payload)}")
            except Exception:
                pass

        if files_payload:
            url = f"{self.base_url}/images/edits"
            multipart_files: List[Tuple[str, Tuple[str, bytes, str]]] = []
            for fname, raw, mime in files_payload:
                multipart_files.append(("image[]", (fname, raw, mime)))
            data = {
                "model": self.model,
                "prompt": prompt,
                "n": "1",
                "size": resolved_size,
                "quality": resolved_quality,
                "output_format": out_format,
            }
            ok, payload, etype = self._post_with_retry(url, data=data, files=multipart_files)
        else:
            url = f"{self.base_url}/images/generations"
            body = {
                "model": self.model,
                "prompt": prompt,
                "n": 1,
                "size": resolved_size,
                "quality": resolved_quality,
                "output_format": out_format,
            }
            ok, payload, etype = self._post_with_retry(url, json_body=body)

        if not ok:
            return False, str(payload)[:200], etype

        try:
            items = payload.get("data") or []
            if not items:
                return False, "Empty data[] in OpenAI response", "server"
            item = items[0]
            b64 = item.get("b64_json")
            img_url = item.get("url")
            if b64:
                raw = base64.b64decode(b64)
            elif img_url:
                r = self.session.get(img_url, timeout=120, proxies=self._proxies())
                if r.status_code != 200 or len(r.content) < 1000:
                    return False, f"Image download failed HTTP {r.status_code}", "server"
                raw = r.content
            else:
                return False, "No b64_json / url in response item", "server"
        except Exception as e:
            return False, _safe_err(e), "server"

        # Сохраняем рядом с output_dir — используем то же имя-шаблон, что и FastGen,
        # чтобы downstream (overlay/cropper) не пришлось спецслучать.
        out_dir = Path(output_dir) if output_dir else Path("./tmp_openai_image")
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in (prompt or "")[:40] if c.isalnum() or c in " _-").strip().replace(" ", "_") or "card"
        ext = {"png": "png", "jpeg": "jpg", "webp": "webp"}.get(out_format, "png")
        fp = out_dir / f"{index:04d}_openai_gptimg2_{safe}.{ext}"
        try:
            with open(fp, "wb") as f:
                f.write(raw)
        except Exception as e:
            return False, _safe_err(e), "server"

        elapsed = time.time() - t0
        logger.info(f"[openai-image] ✓ #{index} {self.model} {resolved_size} "
                    f"{resolved_quality} {elapsed:.1f}s refs={len(files_payload)}")
        return True, str(fp), "none"
