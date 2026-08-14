"""FastGen **V5** image API client (https://googler.fast-gen.ai/api/v5).

Drop-in replacement for fastgen_client.FastGenClient — exposes the SAME
`generate_image(...) -> (ok, path_or_err, etype)` signature and `is_available()`,
so generator.py's fallback chains / ref handling keep working unchanged.

V5 protocol (one unified endpoint for every provider):
  1. POST /api/v5/generations          → {id, status:"queued", operation, ...}
  2. GET  /api/v5/generations/{id}      poll until status in {succeeded, failed}
  3. result item → download_path (resolve against STORAGE origin) OR inline data URI

Key V5 differences vs V4 (all handled here):
  - one endpoint, model is chosen by `model` (or `operation`) in the body
  - inputs[] carries references (data URI, 32-char storage id, or {filename,input})
    → EVERY image model now supports refs (openai included!)
  - statuses are queued/running/succeeded/failed (v4 used pending/processing/success/error)
  - download_path is a *storage* path (/v2/file/<id>?expires_at=...) and must be
    fetched from https://storage.fast-gen.ai, NOT the api base.

Provider keys accepted (so generator.py / saved configs keep working):
  fastgen_nanopro            → nano-banana-pro   (flow GEM_PIX_2, refs 0-10, 4 cr)
  fastgen_nanobanana2,
    fastgen_gemini (alias)   → nano-banana-2     (flow NARWHAL,  refs 0-10, 4 cr)
  fastgen_openai             → openai-image      (ChatGPT web,   refs 0-10, 1 cr)
  fastgen_imagen             → imagen-4          (flow,          refs 0-10, 4 cr)
  fastgen_flower             → flower-image      (refs 0-1,      1 cr; 1 ref = edit)
  fastgen_grok               → grok-image        (refs 0-5,      1 cr)
  fastgen_whisk_remix (alias)→ nano-banana-pro   (multi-ref)
Raw v5 ids (nano-banana-pro, ...) are also accepted directly.
"""

import os
import re
import time
import base64
import logging
import mimetypes
import requests
from pathlib import Path
from typing import Tuple, Optional, List, Dict, Any

logger = logging.getLogger("wbnator.fastgen_v5")


def _safe_err(e: Exception, max_len: int = 200) -> str:
    """Short error string — never let a base64 data URI leak into logs."""
    s = f"{type(e).__name__}: {str(e)}"
    if len(s) > max_len:
        s = s[:max_len] + f"... <truncated {len(s) - max_len} chars>"
    return s


# ── Model catalog (verified live against /api/v5/models) ─────────────────────
# aspects = the exact enum the model accepts; None ⇒ free-form n:n (no clamp).
_MODELS: Dict[str, Dict[str, Any]] = {
    "nano-banana-pro": {"provider": "flow",   "max_refs": 10, "credits": 4,
                         "aspects": ("16:9", "4:3", "1:1", "3:4", "9:16")},
    "nano-banana-2":   {"provider": "flow",   "max_refs": 10, "credits": 4,
                         "aspects": ("16:9", "4:3", "1:1", "3:4", "9:16")},
    "imagen-4":        {"provider": "flow",   "max_refs": 10, "credits": 4,
                         "aspects": ("16:9", "1:1", "9:16")},
    "openai-image":    {"provider": "openai", "max_refs": 10, "credits": 1,
                         "aspects": None},
    "flower-image":    {"provider": "flower", "max_refs": 1,  "credits": 1,
                         "aspects": ("16:9", "9:16", "1:1")},
    "grok-image":      {"provider": "grok",   "max_refs": 5,  "credits": 1,
                         "aspects": ("1:1", "2:3", "3:2", "9:16", "16:9")},
}

# generator.py / frontend provider keys → canonical v5 model id
_PROVIDER_TO_MODEL: Dict[str, str] = {
    "fastgen_nanopro":      "nano-banana-pro",
    "fastgen_nanobanana2":  "nano-banana-2",
    "fastgen_gemini":       "nano-banana-2",     # repurposed slot (no v5 "gemini")
    "fastgen_openai":       "openai-image",
    "fastgen_imagen":       "imagen-4",
    "fastgen_flower":       "flower-image",
    "fastgen_grok":         "grok-image",
    "fastgen_whisk_remix":  "nano-banana-pro",   # no v5 whisk → strongest multi-ref
}


def resolve_model(provider_or_model: str) -> str:
    """Map a generator.py provider key (or raw v5 id) to a canonical v5 model id."""
    if provider_or_model in _MODELS:
        return provider_or_model
    if provider_or_model in _PROVIDER_TO_MODEL:
        return _PROVIDER_TO_MODEL[provider_or_model]
    # tolerate a bare "openai"/"flow" style or unknown → default nano pro
    return _PROVIDER_TO_MODEL.get(provider_or_model, "nano-banana-pro")


def _clamp_aspect(model: str, aspect: str) -> str:
    """Snap a requested aspect to the model's accepted set (closest sensible match)."""
    allowed = _MODELS.get(model, {}).get("aspects")
    if not allowed or aspect in allowed:
        return aspect
    # map common WB-card aspects onto whatever the model supports
    near = {
        "3:4":  ("3:4", "2:3", "9:16", "1:1"),
        "9:16": ("9:16", "2:3", "3:4", "1:1"),
        "4:3":  ("4:3", "3:2", "16:9", "1:1"),
        "16:9": ("16:9", "3:2", "4:3", "1:1"),
        "1:1":  ("1:1", "4:3", "16:9", "9:16"),
        "2:3":  ("2:3", "9:16", "3:4", "1:1"),
        "3:2":  ("3:2", "16:9", "4:3", "1:1"),
    }.get(aspect, (aspect, "1:1"))
    for cand in near:
        if cand in allowed:
            return cand
    return allowed[0]


_HEX32_RE = re.compile(r"^[a-f0-9]{32}$")


# ── OpenAI ref normalisation (ported from Vidinator core/gemini_image_generator.py)
# OpenAI принимает референсы СТРОГО PNG или JPEG. WebP/AVIF/HEIC/GIF/BMP/TIFF
# отвергаются с validation.invalid_format, а FastGen V5 заворачивает это в
# generic 500 ("Server 500: Internal Server Error") — именно то, что мы видели
# в проде. Поэтому ДО отправки: определяем настоящий MIME по magic bytes
# (расширение и Content-Type врут — WB CDN, например, шлёт webp под .jpg URL)
# и конвертируем неподдерживаемые в JPEG через Pillow.
def _sniff_image_mime(raw: bytes) -> str:
    """MIME по magic bytes. application/octet-stream если не картинка."""
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
    # AVIF / HEIC: ISO BMFF, "ftyp" в [4:8], бренд в [8:12]
    if raw[4:8] == b"ftyp" and raw[8:12] in (b"avif", b"avis", b"heic", b"heix",
                                              b"mif1", b"msf1", b"hevc", b"hevx"):
        return "image/heic" if raw[8:12] not in (b"avif", b"avis") else "image/avif"
    return "application/octet-stream"


def _convert_to_jpeg_bytes(raw: bytes) -> Optional[bytes]:
    """webp/gif/bmp/tiff/heic/avif → jpeg байты. None если Pillow не вытянул."""
    try:
        from PIL import Image  # type: ignore
        import io as _io
        img = Image.open(_io.BytesIO(raw))
        # animated (GIF/WEBP/AVIF) — берём первый кадр
        try:
            img.seek(0)
        except Exception:
            pass
        if img.mode in ("RGBA", "LA", "P"):
            # JPEG не умеет alpha — flatten на белый фон
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        out = _io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()
    except Exception as e:
        logger.warning(f"[v5] OpenAI ref convert failed: {_safe_err(e)}")
        return None


def _normalize_ref_for_openai(media: str) -> Optional[str]:
    """data:URI с любым реальным форматом → data:URI с PNG/JPEG.

    Storage ID (32-hex) пропускаем как есть — байты лежат на стороне FastGen,
    мы их пересжать не можем. Если ref некодируем — возвращаем None и вызывающий
    его выкинет (лучше потерять один ref, чем уронить весь рендер 500-кой).
    """
    if not isinstance(media, str) or not media:
        return None
    if _HEX32_RE.match(media):
        return media  # storage id — доверяемся серверу
    if not media.startswith("data:"):
        return None
    try:
        header, b64 = media.split(",", 1)
        declared = header[5:].split(";")[0].strip().lower()
        raw = base64.b64decode(b64)
    except Exception:
        return None
    sniffed = _sniff_image_mime(raw)
    if sniffed in ("image/png", "image/jpeg"):
        # уже валидный по факту — переотдадим со сниффнутым MIME (а не объявленным)
        return f"data:{sniffed};base64,{base64.b64encode(raw).decode('ascii')}"
    # webp/heic/avif/gif/bmp/tiff — пережимаем
    jpg = _convert_to_jpeg_bytes(raw)
    if jpg:
        return f"data:image/jpeg;base64,{base64.b64encode(jpg).decode('ascii')}"
    # последний шанс: вдруг magic не сработал, а MIME из заголовка валидный
    if declared in ("image/png", "image/jpeg"):
        return media
    return None


class FastGenV5Client:
    def __init__(self, api_key: str,
                 base_url: str = "https://googler.fast-gen.ai",
                 storage_url: str = "https://storage.fast-gen.ai",
                 proxy_url: Optional[str] = None,
                 proxy_for_providers: Optional[set] = None):
        """
        base_url accepts either ".../api/v4" (legacy config) or a bare host —
        we always talk to /api/v5 regardless.
        proxy_url / proxy_for_providers: same semantics as the v4 client.
        """
        self.api_key = api_key
        # strip any trailing /api/vN so we can append /api/v5 cleanly
        root = (base_url or "https://googler.fast-gen.ai").rstrip("/")
        root = re.sub(r"/api/v\d+$", "", root)
        self.base_url = root
        self.api_v5 = root + "/api/v5"
        self.storage_url = (storage_url or "https://storage.fast-gen.ai").rstrip("/")
        self.proxy_url = proxy_url or None
        self.proxy_for_providers = proxy_for_providers
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key,
                                     "Content-Type": "application/json"})

    # ── infra ────────────────────────────────────────────────────────────
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _proxies_for(self, provider: Optional[str]) -> Optional[dict]:
        if not self.proxy_url:
            return None
        if self.proxy_for_providers is not None and provider not in self.proxy_for_providers:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    # ── reference normalisation ──────────────────────────────────────────
    def _ref_to_media(self, ref: Any) -> Optional[str]:
        """Turn one reference into a v5 media string (data URI or 32-hex id).

        Accepts: data: URI, http(s) URL, 32-char storage id, local file path,
        or bytes. Returns None if it can't be resolved.
        """
        try:
            if isinstance(ref, (bytes, bytearray)):
                b64 = base64.b64encode(bytes(ref)).decode()
                return "data:image/png;base64," + b64
            if not isinstance(ref, str):
                return None
            s = ref.strip()
            if s.startswith("data:"):
                return s
            if _HEX32_RE.match(s):
                return s  # already a storage id
            if s.startswith(("http://", "https://")):
                r = requests.get(s, timeout=60)
                if r.status_code != 200 or len(r.content) < 100:
                    return None
                mime = r.headers.get("content-type", "image/png").split(";")[0]
                return f"data:{mime};base64," + base64.b64encode(r.content).decode()
            # treat as a local path
            p = Path(s)
            if p.exists() and p.is_file():
                data = p.read_bytes()
                mime = mimetypes.guess_type(str(p))[0] or "image/png"
                return f"data:{mime};base64," + base64.b64encode(data).decode()
            return None
        except Exception as e:
            logger.warning(f"[v5] ref resolve failed: {_safe_err(e)}")
            return None

    def _build_inputs(self, model: str, reference_images: Optional[List],
                      named: bool = False) -> list:
        """Build the v5 `inputs` array, clamped to the model's max_refs.

        Each ref may be: str, bytes, or dict {"image"/"input"/"path", optional
        "name"/"filename", optional "category" (ignored — no v5 whisk roles)}.

        Для openai-image дополнительно прогоняем каждый ref через
        _normalize_ref_for_openai — иначе webp/avif/heic с WB CDN и uploads
        ловят 500 (validation.invalid_format на стороне OpenAI).
        """
        if not reference_images:
            return []
        max_refs = _MODELS.get(model, {}).get("max_refs", 10)
        is_openai = (model == "openai-image")
        out: list = []
        skipped = 0
        for ref in reference_images:
            if len(out) >= max_refs:
                break
            name = None
            raw = ref
            if isinstance(ref, dict):
                raw = ref.get("image") or ref.get("input") or ref.get("path")
                name = ref.get("name") or ref.get("filename")
            media = self._ref_to_media(raw)
            if not media:
                continue
            if is_openai:
                normalized = _normalize_ref_for_openai(media)
                if not normalized:
                    skipped += 1
                    continue
                media = normalized
            if named and name:
                out.append({"filename": str(name)[:255], "input": media})
            else:
                out.append(media)
        if is_openai and skipped:
            logger.warning(f"[v5] openai-image: пропущено {skipped} реф(ов) — не PNG/JPEG и не удалось сконвертировать")
        return out

    # ── submit ───────────────────────────────────────────────────────────
    def _submit(self, body: dict, provider_key: Optional[str],
                max_wait: int = 180) -> Tuple[bool, str, str, dict]:
        """POST /api/v5/generations → (ok, generation_id_or_err, etype, meta).

        Retries on 429 (concurrency), 5xx, timeouts. 500 body sniffed for
        SAFETY/PROMINENT → terminal content error.
        """
        url = self.api_v5 + "/generations"
        proxies = self._proxies_for(provider_key)
        start = time.time()
        a429 = a5xx = 0
        while True:
            try:
                r = self.session.post(url, json=body, timeout=90, proxies=proxies)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                a5xx += 1
                if time.time() - start > max_wait:
                    return False, _safe_err(e) + " (after retries)", "server", {}
                time.sleep(min(2 ** a5xx, 30))
                continue
            except Exception as e:
                return False, _safe_err(e), "server", {}

            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception as e:
                    return False, f"Bad JSON: {e}", "server", {}
                gid = data.get("id")
                if gid:
                    return True, gid, "none", data
                return False, str(data.get("error", "No generation id"))[:200], "server", {}

            if r.status_code == 429:
                a429 += 1
                if time.time() - start > max_wait:
                    return False, "Concurrency wait timeout", "rate_limit", {}
                time.sleep(min(1.5 + a429 * 0.5, 5.0))
                continue

            if r.status_code in (502, 503, 504):
                a5xx += 1
                if time.time() - start > max_wait:
                    return False, f"HTTP {r.status_code} (retries exhausted)", "server", {}
                time.sleep(min(2 ** a5xx, 30))
                continue

            if r.status_code == 500:
                body_up = (r.text or "")[:400].upper()
                if any(x in body_up for x in ("SAFETY", "BLOCKED", "FILTER", "POLICY")):
                    return False, "SAFETY_FILTER", "content", {}
                if any(x in body_up for x in ("PROMINENT", "WELL-KNOWN", "CELEBRITY")):
                    return False, "PROMINENT_PEOPLE", "content", {}
                a5xx += 1
                if time.time() - start > max_wait or a5xx > 3:
                    return False, f"Server 500: {(r.text or '')[:160]}", "server", {}
                time.sleep(min(2 ** a5xx, 15))
                continue

            if r.status_code in (401, 403):
                return False, f"Auth {r.status_code}", "fatal", {}
            if r.status_code == 422:
                return False, f"Validation: {(r.text or '')[:200]}", "fatal", {}
            return False, f"HTTP {r.status_code}: {(r.text or '')[:200]}", "server", {}

    # ── poll ─────────────────────────────────────────────────────────────
    def _poll(self, generation_id: str, timeout: int = 300,
              provider_key: Optional[str] = None,
              progress_cb=None) -> Tuple[bool, Optional[list], str, dict]:
        """GET /api/v5/generations/{id} until succeeded/failed.
        Returns (ok, results_list, error, meta)."""
        url = self.api_v5 + "/generations/" + generation_id
        proxies = self._proxies_for(provider_key)
        start = time.time()
        attempt = 0
        last_status = ""
        while time.time() - start < timeout:
            interval = 2 if attempt < 12 else 5
            try:
                r = self.session.get(url, timeout=30, proxies=proxies)
            except Exception:
                attempt += 1
                time.sleep(interval)
                continue
            if r.status_code != 200:
                attempt += 1
                time.sleep(interval)
                continue
            try:
                data = r.json()
            except Exception:
                attempt += 1
                time.sleep(interval)
                continue
            status = data.get("status", "")
            if status != last_status and progress_cb:
                try:
                    progress_cb(f"{status} {int(time.time() - start)}s")
                except Exception:
                    pass
            last_status = status
            if status == "succeeded":
                return True, data.get("results", []) or [], "", data
            if status == "failed":
                return False, None, data.get("error") or "Generation failed", data
            if progress_cb and attempt % 5 == 0:
                try:
                    progress_cb(f"{status or 'polling'} {int(time.time() - start)}s")
                except Exception:
                    pass
            attempt += 1
            time.sleep(interval)
        return False, None, f"Polling timeout {timeout}s", {}

    # ── download ─────────────────────────────────────────────────────────
    def _download_item(self, item: dict, out_path: str,
                       provider_key: Optional[str] = None) -> Tuple[bool, str]:
        """Save one GenerationResultItem to disk.

        Order of preference: download_path (storage) → metadata.storage_id →
        inline data URI. Never lets a data URI leak into an error string.
        """
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            proxies = self._proxies_for(provider_key)

            dp = item.get("download_path")
            sid = (item.get("metadata") or {}).get("storage_id")
            data_uri = item.get("data")

            # 1) storage download_path (resolve against storage origin)
            url = None
            if dp:
                url = dp if dp.startswith(("http://", "https://")) else self.storage_url + dp
            elif sid:
                url = f"{self.storage_url}/v2/file/{sid}"
            if url:
                r = self.session.get(url, timeout=120, proxies=proxies)
                if r.status_code == 200 and len(r.content) >= 1000:
                    with open(out_path, "wb") as f:
                        f.write(r.content)
                    return True, out_path
                # fall through to inline data if storage fetch failed
                if not data_uri:
                    return False, f"Storage DL HTTP {r.status_code} (len={len(r.content)})"

            # 2) inline data URI
            if data_uri and isinstance(data_uri, str) and data_uri.startswith("data:"):
                try:
                    _, b64 = data_uri.split(",", 1)
                    raw = base64.b64decode(b64)
                except Exception:
                    return False, "Bad inline data URI"
                if len(raw) < 1000:
                    return False, f"Inline data too small ({len(raw)}B)"
                with open(out_path, "wb") as f:
                    f.write(raw)
                return True, out_path

            return False, "No download_path / storage_id / data in result item"
        except Exception as e:
            return False, _safe_err(e)

    # ── public: full structured generate ─────────────────────────────────
    def generate(self, prompt: str, provider: str = "fastgen_openai",
                 aspect_ratio: str = "3:4",
                 reference_images: Optional[List] = None,
                 output_dir: Optional[Path] = None,
                 index: int = 0,
                 seed: Optional[int] = None,
                 named_refs: bool = False,
                 quality: Optional[str] = None,
                 count: Optional[int] = None,
                 progress_cb=None,
                 poll_timeout: int = 300,
                 **_ignored) -> Dict[str, Any]:
        """Run a full V5 image generation. Returns a rich dict (used by the tester)."""
        t0 = time.time()
        model = resolve_model(provider)
        out = {
            "ok": False, "path": None, "etype": "fatal", "error": None,
            "provider": provider, "model": model, "generation_id": None,
            "operation_type": None, "credits": None, "status": None,
            "aspect_ratio": aspect_ratio, "elapsed": 0.0, "result_item": None,
        }
        if not self.is_available():
            out["error"] = "FastGen unavailable"
            return out

        aspect = _clamp_aspect(model, aspect_ratio)
        out["aspect_ratio"] = aspect
        inputs = self._build_inputs(model, reference_images, named=named_refs)
        out["ref_count"] = len(inputs)

        body: dict = {"model": model, "prompt": prompt, "aspect_ratio": aspect}
        if inputs:
            body["inputs"] = inputs
        if seed is not None:
            try:
                body["seed"] = int(seed) % 2147483647
            except Exception:
                pass
        if quality:
            body["quality"] = quality
        if count:
            body["count"] = int(count)

        ok, gid_or_err, etype, meta = self._submit(body, provider)
        if not ok:
            out.update(error=gid_or_err, etype=etype)
            return out
        out["generation_id"] = gid_or_err
        out["operation_type"] = meta.get("operation_type")

        ok2, results, err, pmeta = self._poll(gid_or_err, timeout=poll_timeout,
                                               provider_key=provider,
                                               progress_cb=progress_cb)
        out["status"] = pmeta.get("status")
        out["credits"] = (pmeta.get("usage") or {}).get("credits")
        if not ok2 or not results:
            etype2 = "content" if (err and "SAFETY" in err.upper()) else "server"
            out.update(error=err or "Empty result", etype=etype2)
            return out

        item = results[0]
        out["result_item"] = item
        out_dir = Path(output_dir) if output_dir else Path("./tmp_fastgen_v5")
        safe = "".join(c for c in prompt[:40] if c.isalnum() or c in " _-").strip().replace(" ", "_") or "card"
        fp = out_dir / f"{index:04d}_v5_{model}_{safe}.jpg"
        ok3, res = self._download_item(item, str(fp), provider_key=provider)
        if not ok3:
            out.update(error=res, etype="server")
            return out

        out.update(ok=True, etype="none", path=str(fp), elapsed=time.time() - t0)
        logger.info(f"[FastGenV5] ✓ #{index} {model} {out['elapsed']:.1f}s "
                    f"refs={len(inputs)} credits={out['credits']}")
        return out

    # ── public: v4-compatible thin wrapper (drop-in for generator.py) ────
    def generate_image(self, prompt: str, provider: str = "fastgen_openai",
                       aspect_ratio: str = "3:4",
                       reference_images: Optional[List] = None,
                       output_dir: Optional[Path] = None,
                       index: int = 0,
                       seed: Optional[int] = None,
                       progress_cb=None,
                       named_refs: bool = True,
                       **_ignored) -> Tuple[bool, str, str]:
        """Same shape as FastGenClient.generate_image → (ok, path_or_err, etype).

        named_refs=True by default: refs are sent WITH their filename (product1,
        styleRef, …) so the prompt's «identical to the product1 reference image»
        actually binds to the right image — без имени модель не связывала текст с рефом.
        """
        res = self.generate(prompt, provider=provider, aspect_ratio=aspect_ratio,
                            reference_images=reference_images, output_dir=output_dir,
                            index=index, seed=seed, progress_cb=progress_cb,
                            named_refs=named_refs)
        if res["ok"]:
            return True, res["path"], "none"
        return False, res["error"] or "Unknown error", res["etype"]

    # ── docs helper ──────────────────────────────────────────────────────
    def list_image_models(self) -> list:
        """Return curated image-model metadata for UIs (offline-safe)."""
        return [
            {"id": mid, **{k: v for k, v in m.items()}}
            for mid, m in _MODELS.items()
        ]
