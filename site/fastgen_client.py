"""Minimal FastGen image API client (https://googler.fast-gen.ai/api/v4).

Same flow for every provider:
  1. POST /api/v4/<provider>/image/generate  → operation_id
  2. GET  /api/v4/operations/<id>            poll until status=success
  3. download URL from result[]

Mirrors the protocol used in C:\\Vidinator_Structured\\core\\gemini_image_generator.py
(_submit_generation + _poll_operation), reduced to what WBnator needs:
  - nanopro     (Flow / GEM_PIX_2, supports up to 10 reference images, supports aspect)
  - gemini      (gemini/image/generate, supports refs, NO aspect — use squarish prompts)
  - imagen      (Flow / IMAGEN_3_5, no refs, supports aspect)
  - flower      (flower/image/generate, single ref, aspect ∈ {16:9, 9:16, 1:1})
  - grok        (no refs, supports aspect)

Returns same (ok, path, etype) tuple shape as VeoNonStop client so generator.py
can swap clients with one if-branch.
"""

import os
import time
import base64
import logging
import requests
from pathlib import Path
from typing import Tuple, Optional, List, Dict


def _safe_err(e: Exception, max_len: int = 200) -> str:
    """Return a short error string. Crucial: never propagate base64 data URIs into log messages."""
    s = f"{type(e).__name__}: {str(e)}"
    if len(s) > max_len:
        s = s[:max_len] + f"... <truncated {len(s) - max_len} chars>"
    return s

logger = logging.getLogger("wbnator.fastgen")


# Aspect helpers — Flow accepts "IMAGE_ASPECT_RATIO_*" enum, others accept "16:9" form
def _aspect_to_flow_enum(a: str) -> str:
    m = {
        "16:9": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "9:16": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "1:1":  "IMAGE_ASPECT_RATIO_LANDSCAPE",  # Flow has no square — use landscape and crop
        "3:4":  "IMAGE_ASPECT_RATIO_PORTRAIT",
        "4:3":  "IMAGE_ASPECT_RATIO_LANDSCAPE",
    }
    return m.get(a, "IMAGE_ASPECT_RATIO_PORTRAIT")


def _aspect_to_simple(a: str) -> str:
    """Flower wants 16:9 / 9:16 / 1:1 only."""
    if a in ("16:9", "9:16", "1:1"):
        return a
    if a in ("3:4",):
        return "9:16"
    if a in ("4:3",):
        return "16:9"
    return "9:16"


PROVIDERS = {
    # key                : (endpoint_path,                    flow_model,     aspect_kind,  supports_refs, max_refs)
    "fastgen_nanopro":     ("/flow/image/generate",           "GEM_PIX_2",    "flow_enum",  True,           10),
    "fastgen_imagen":      ("/flow/image/generate",           "IMAGEN_3_5",   "flow_enum",  False,          0),
    "fastgen_gemini":      ("/gemini/image/generate",         None,           "none",       True,           10),
    "fastgen_flower":      ("/flower/image/generate",         None,           "simple",     True,           1),
    "fastgen_grok":        ("/grok/image/generate",           None,           "simple",     False,          0),
    "fastgen_openai":      ("/openai/image/generate",         None,           "openai",     False,          0),
    "fastgen_whisk_remix": ("/whisk/image/remix",             None,           "whisk_remix",True,           3),
}


# Whisk remix categories — determine the ROLE of each reference image:
#   MEDIA_CATEGORY_SUBJECT — actual product/object to render
#   MEDIA_CATEGORY_STYLE   — visual style guide (palette, typography, mood)
#   MEDIA_CATEGORY_SCENE   — scene/environment (rare for our cards)
WHISK_CATEGORY_SUBJECT = "MEDIA_CATEGORY_SUBJECT"
WHISK_CATEGORY_STYLE   = "MEDIA_CATEGORY_STYLE"
WHISK_CATEGORY_SCENE   = "MEDIA_CATEGORY_SCENE"


def _aspect_to_openai_size(a: str) -> str:
    """OpenAI gpt-image-1 / DALL-E 3 supports a fixed set of sizes."""
    return {
        "1:1":  "1024x1024",
        "3:4":  "1024x1536",   # closest to 3:4 (gpt-image-1 native portrait)
        "9:16": "1024x1536",
        "4:3":  "1536x1024",
        "16:9": "1536x1024",
    }.get(a, "1024x1536")


class FastGenClient:
    def __init__(self, api_key: str, base_url: str = "https://googler.fast-gen.ai/api/v4",
                 proxy_url: Optional[str] = None,
                 proxy_for_providers: Optional[set] = None):
        """
        proxy_url:  HTTP/SOCKS URL для исходящих запросов к FastGen.
                    Например "http://user:pass@host:port".
        proxy_for_providers: множество имён провайдеров, для которых
                    использовать прокси (например {"fastgen_openai"}).
                    Если None — прокси используется для ВСЕХ запросов.
                    Если provider не в этом списке — идём напрямую без прокси.
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.proxy_url = proxy_url
        self.proxy_for_providers = proxy_for_providers
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key, "Content-Type": "application/json"})

    def _proxies_for(self, provider: Optional[str]) -> Optional[dict]:
        """Возвращает dict для requests.post(proxies=...) или None."""
        if not self.proxy_url:
            return None
        if self.proxy_for_providers is not None and provider not in self.proxy_for_providers:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _build_payload(self, provider: str, prompt: str, aspect: str,
                       reference_images: Optional[List],
                       seed: Optional[int] = None) -> Tuple[str, dict]:
        if provider not in PROVIDERS:
            raise ValueError(f"Неизвестный FastGen-провайдер: {provider}")
        endpoint, flow_model, aspect_kind, supports_refs, max_refs = PROVIDERS[provider]

        # Gemini endpoint ignores aspect_ratio — bake the orientation hint into the prompt itself.
        if aspect_kind == "none":
            orientation = {
                "3:4": "Vertical 3:4 portrait composition.",
                "9:16": "Vertical 9:16 portrait composition.",
                "1:1": "Square 1:1 composition.",
                "4:3": "Horizontal 4:3 composition.",
                "16:9": "Horizontal 16:9 composition.",
            }.get(aspect, "Vertical 3:4 portrait composition.")
            prompt = f"{orientation} {prompt}"

        if aspect_kind == "whisk_remix":
            # Whisk remix expects refs as [{"image": ..., "category": "MEDIA_CATEGORY_*"}]
            # so style + product can be sent with explicit ROLES — model knows
            # to apply style FROM ref1 to subject FROM ref2.
            refs = []
            for ref in (reference_images or [])[:max_refs]:
                if isinstance(ref, dict) and "image" in ref:
                    cat = ref.get("category") or WHISK_CATEGORY_SUBJECT
                    if not cat.startswith("MEDIA_CATEGORY_"):
                        cat = {"subject": WHISK_CATEGORY_SUBJECT,
                               "style":   WHISK_CATEGORY_STYLE,
                               "scene":   WHISK_CATEGORY_SCENE}.get(cat.lower(), WHISK_CATEGORY_SUBJECT)
                    refs.append({"image": ref["image"], "category": cat})
                elif isinstance(ref, str):
                    refs.append({"image": ref, "category": WHISK_CATEGORY_SUBJECT})
            payload = {
                "prompt": prompt,
                "reference_images": refs,
                "aspect_ratio": _aspect_to_simple(aspect),
            }
            if seed is not None:
                payload["seed"] = int(seed)
            return self.base_url + endpoint, payload

        if aspect_kind == "openai":
            # FastGen's /openai/image/generate validates BOTH `prompt` (top-level)
            # AND `messages.0.content.multimodal_text.parts.0.str` simultaneously
            # — observed by trial: sending only `prompt` failed with
            # "messages.0.content.multimodal_text.parts.0.str: Input should be a valid string",
            # sending only `messages` failed with "body.prompt: Field required".
            # Sending both satisfies both validators.
            payload = {
                "model": "gpt-image-1",
                "prompt": prompt,
                "messages": [{
                    "role": "user",
                    "content": {
                        "multimodal_text": {
                            "parts": [{"str": prompt}]
                        }
                    }
                }],
                "size": _aspect_to_openai_size(aspect),
                "quality": "high",
            }
            return self.base_url + endpoint, payload

        payload: dict = {"prompt": prompt}
        if aspect_kind == "flow_enum":
            payload["aspect_ratio"] = _aspect_to_flow_enum(aspect)
            if flow_model:
                payload["model"] = flow_model
        elif aspect_kind == "simple":
            payload["aspect_ratio"] = _aspect_to_simple(aspect)

        # Seed for visual consistency across calls in a pack
        if seed is not None and aspect_kind != "none":
            payload["seed"] = int(seed)

        if supports_refs and reference_images:
            refs = []
            for ref in reference_images[:max_refs]:
                if isinstance(ref, dict) and "image" in ref:
                    refs.append(ref["image"])
                elif isinstance(ref, str):
                    refs.append(ref)
            if refs:
                if provider == "fastgen_flower":
                    payload["reference_image"] = refs[0]
                else:
                    payload["reference_images"] = refs

        return self.base_url + endpoint, payload

    def _submit(self, url: str, payload: dict,
                max_concurrency_wait: int = 180,
                provider: Optional[str] = None) -> Tuple[bool, str, str]:
        """POST → operation_id. Retries on:
          - 429 (concurrency limit) with short backoff (1.5-5s)
          - 502/503/504 (transient server) with exp backoff (2-30s)
          - timeouts / connection errors with exp backoff
          - 500 with body-content check (SAFETY/PROMINENT → terminal,
            otherwise transient retry).
        """
        start = time.time()
        attempts_429 = 0
        attempts_5xx = 0
        proxies = self._proxies_for(provider)
        while True:
            try:
                r = self.session.post(url, json=payload, timeout=60, proxies=proxies)
            except requests.exceptions.Timeout:
                attempts_5xx += 1
                if time.time() - start > max_concurrency_wait:
                    return False, "Submit timeout (after retries)", "server"
                wait = min(2 ** attempts_5xx, 30)
                logger.warning(f"[fastgen] timeout — retry {attempts_5xx} in {wait}s")
                time.sleep(wait)
                continue
            except requests.exceptions.ConnectionError as e:
                attempts_5xx += 1
                if time.time() - start > max_concurrency_wait:
                    return False, _safe_err(e) + " (after retries)", "server"
                wait = min(2 ** attempts_5xx, 30)
                logger.warning(f"[fastgen] conn error — retry {attempts_5xx} in {wait}s")
                time.sleep(wait)
                continue
            except Exception as e:
                return False, _safe_err(e), "server"

            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception as e:
                    return False, f"Bad JSON: {e}", "server"
                if data.get("success") and data.get("operation_id"):
                    return True, data["operation_id"], "none"
                return False, str(data.get("error", "No operation_id"))[:200], "server"

            if r.status_code == 429:
                attempts_429 += 1
                if time.time() - start > max_concurrency_wait:
                    return False, "Concurrency wait timeout", "rate_limit"
                wait = min(1.5 + attempts_429 * 0.5, 5.0)
                time.sleep(wait)
                continue

            if r.status_code in (502, 503, 504):
                attempts_5xx += 1
                if time.time() - start > max_concurrency_wait:
                    return False, f"HTTP {r.status_code} (retries exhausted)", "server"
                wait = min(2 ** attempts_5xx, 30)
                logger.warning(f"[fastgen] HTTP {r.status_code} — retry "
                               f"{attempts_5xx} in {wait}s")
                time.sleep(wait)
                continue

            if r.status_code == 500:
                body = (r.text or "")[:300].upper()
                if any(x in body for x in ("SAFETY", "BLOCKED", "FILTER", "POLICY")):
                    return False, "SAFETY_FILTER", "content"
                if any(x in body for x in ("PROMINENT", "WELL-KNOWN", "CELEBRITY")):
                    return False, "PROMINENT_PEOPLE", "content"
                # Other 500 — try retry as transient
                attempts_5xx += 1
                if time.time() - start > max_concurrency_wait or attempts_5xx > 3:
                    return False, f"Server 500: {(r.text or '')[:160]}", "server"
                wait = min(2 ** attempts_5xx, 15)
                logger.warning(f"[fastgen] HTTP 500 (transient?) — retry "
                               f"{attempts_5xx} in {wait}s")
                time.sleep(wait)
                continue

            if r.status_code in (401, 403):
                return False, f"Auth {r.status_code}", "fatal"
            if r.status_code == 422:
                return False, f"Validation: {(r.text or '')[:200]}", "fatal"
            return False, f"HTTP {r.status_code}: {(r.text or '')[:200]}", "server"

    def _poll(self, operation_id: str, timeout: int = 180,
              provider: Optional[str] = None,
              progress_cb=None) -> Tuple[bool, Optional[List[str]], str]:
        url = f"{self.base_url}/operations/{operation_id}"
        start = time.time()
        attempt = 0
        proxies = self._proxies_for(provider)
        last_progress_at = 0.0
        PROGRESS_EVERY = 10.0   # секунд — частота прогресс-колбэка наружу
        while time.time() - start < timeout:
            elapsed = time.time() - start
            # Шлём прогресс наружу не чаще раза в 10 сек, чтобы пользователь
            # видел движение во время длинных waits и SSE-стрим оставался
            # «горячим» для прокси.
            if progress_cb and (elapsed - last_progress_at >= PROGRESS_EVERY):
                last_progress_at = elapsed
                try:
                    progress_cb(f"FastGen polling {int(elapsed)}s/{timeout}s")
                except Exception:
                    pass
            interval = 2 if attempt < 10 else 5
            try:
                r = self.session.get(url, timeout=30, proxies=proxies)
            except Exception as e:
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
            if status == "success":
                result = data.get("result", [])
                if isinstance(result, str):
                    result = [result]
                return True, result, ""
            if status == "error":
                err = data.get("error", "Unknown error")
                return False, None, err
            attempt += 1
            time.sleep(interval)
        return False, None, f"Polling timeout {timeout}s"

    @staticmethod
    def _download(url: str, out_path: str) -> Tuple[bool, str]:
        """Save image to disk. Handles 3 result formats:
           - data: URI (base64 inline) — decode and write
           - http(s) URL — fetch and write
           - bare base64 string — decode and write
        Never lets a data URI leak into an exception/log string.
        """
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)

            if isinstance(url, (bytes, bytearray)):
                data = bytes(url)
                if len(data) < 1000:
                    return False, f"Bytes too small ({len(data)}B)"
                with open(out_path, "wb") as f:
                    f.write(data)
                return True, out_path

            if not isinstance(url, str):
                return False, f"Bad result type: {type(url).__name__}"

            if url.startswith("data:"):
                try:
                    _, b64 = url.split(",", 1)
                except ValueError:
                    return False, "Bad data URI (no comma)"
                try:
                    data = base64.b64decode(b64)
                except Exception:
                    return False, "Base64 decode failed"
                if len(data) < 1000:
                    return False, f"Inline data too small ({len(data)}B)"
                with open(out_path, "wb") as f:
                    f.write(data)
                return True, out_path

            if url.startswith(("http://", "https://")):
                r = requests.get(url, timeout=120)
                if r.status_code != 200:
                    return False, f"DL HTTP {r.status_code}"
                data = r.content
                if len(data) < 1000:
                    return False, f"Too small ({len(data)}B)"
                with open(out_path, "wb") as f:
                    f.write(data)
                return True, out_path

            # Bare base64 (no data: prefix, no scheme) — try decoding
            if len(url) > 1000 and all(c.isalnum() or c in "+/=\n\r" for c in url[:200]):
                try:
                    data = base64.b64decode(url)
                except Exception:
                    return False, f"Bare base64 decode failed (len={len(url)})"
                if len(data) < 1000:
                    return False, f"Inline data too small ({len(data)}B)"
                with open(out_path, "wb") as f:
                    f.write(data)
                return True, out_path

            return False, f"Unknown result format (len={len(url)})"
        except Exception as e:
            return False, _safe_err(e)

    def generate_image(self, prompt: str, provider: str = "fastgen_nanopro",
                       aspect_ratio: str = "3:4",
                       reference_images: Optional[List] = None,
                       output_dir: Optional[Path] = None,
                       index: int = 0,
                       seed: Optional[int] = None,
                       progress_cb=None,
                       **_ignored) -> Tuple[bool, str, str]:
        """Mirrors VeoNonStopClient.generate_image signature shape. Returns (ok, path_or_err, etype).

        progress_cb(message: str) — опциональный коллбэк, вызывается каждые ~10s
        внутри _poll. Используется, чтобы тащить «FastGen polling 20s/180s» в
        SSE-лог и не давать стриму засохнуть от тишины.
        """
        if not self.is_available():
            return False, "FastGen unavailable", "fatal"
        try:
            url, payload = self._build_payload(provider, prompt, aspect_ratio,
                                                reference_images, seed=seed)
        except Exception as e:
            return False, f"Payload error: {e}", "fatal"

        t0 = time.time()
        ok, op_or_err, etype = self._submit(url, payload, provider=provider)
        if not ok:
            return False, op_or_err, etype

        ok2, urls, err = self._poll(op_or_err, provider=provider, progress_cb=progress_cb)
        if not ok2 or not urls:
            return False, err or "Empty result", "server"

        out_dir = Path(output_dir) if output_dir else Path("./tmp_fastgen")
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = ".jpg"
        safe = "".join(c for c in prompt[:40] if c.isalnum() or c in " _-").strip().replace(" ", "_") or "card"
        fp = out_dir / f"{index:04d}_fg_{provider.replace('fastgen_','')}_{safe}{ext}"
        ok3, res = self._download(urls[0], str(fp))
        if not ok3:
            return False, res, "server"

        elapsed = time.time() - t0
        logger.info(f"[FastGen] ✓ #{index} {provider} {elapsed:.1f}s refs={len(reference_images or [])}")
        return True, str(fp), "none"
