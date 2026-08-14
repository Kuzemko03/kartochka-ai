"""FastGen image-to-video client (https://googler.fast-gen.ai/api/v4).

Mirrors the protocol from C:\\Vidinator_Structured\\core\\video_animator.py
(WhiskVideoClient), reduced to what WBnator uses:

  fastgen_flower   — Veo 3.1, best quality, supports aspect 16:9 / 9:16 (default)
  fastgen_flow     — Veo via Flow, supports VIDEO_ASPECT_RATIO_PORTRAIT/LANDSCAPE
  fastgen_whisk    — Whisk image-to-video, simpler
  fastgen_gemini   — Gemini Veo, no aspect param
  fastgen_grok     — Grok video fallback

Same shape as veo_client.generate_video_from_image:
  generate_video_from_image(image_path, motion_prompt, output_path,
                            aspect_ratio="9:16", provider="fastgen_flower",
                            timeout=600) -> (ok, path_or_err)
"""

import os
import time
import base64
import logging
import requests
from pathlib import Path
from typing import Tuple, Optional

logger = logging.getLogger("wbnator.fgvideo")


def _safe_err(e: Exception, max_len: int = 220) -> str:
    s = f"{type(e).__name__}: {str(e)}"
    return s if len(s) <= max_len else s[:max_len] + "... <truncated>"


def _aspect_to_flow(a: str) -> str:
    return {
        "16:9": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "9:16": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "3:4":  "VIDEO_ASPECT_RATIO_PORTRAIT",
        "4:3":  "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "1:1":  "VIDEO_ASPECT_RATIO_PORTRAIT",
    }.get(a, "VIDEO_ASPECT_RATIO_PORTRAIT")


def _aspect_to_flower(a: str) -> str:
    if a in ("16:9", "9:16"):
        return a
    if a in ("3:4", "1:1"):
        return "9:16"
    if a == "4:3":
        return "16:9"
    return "9:16"


def _file_to_data_uri(path: str) -> Optional[str]:
    try:
        ext = Path(path).suffix.lower()
        mime = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg',
                '.webp':'image/webp','.gif':'image/gif','.bmp':'image/bmp'}.get(ext, 'image/jpeg')
        with open(path, "rb") as f:
            return f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"
    except Exception as e:
        logger.error(f"data URI conversion failed for {path}: {e}")
        return None


class FastGenVideoClient:
    def __init__(self, api_key: str, base_url: str = "https://googler.fast-gen.ai/api/v4"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key, "Content-Type": "application/json"})
        self._max_concurrency_wait = 300

    def is_available(self) -> bool:
        return bool(self.api_key)

    # ─── submit per provider ─────────────────────────────────────────────────
    def _submit(self, provider: str, image_data_uri: str, prompt: str,
                aspect_ratio: str = "9:16") -> Tuple[bool, str, str]:
        if provider == "fastgen_flower":
            url = f"{self.base_url}/flower/video/from-image"
            payload = {"image": image_data_uri, "prompt": prompt,
                       "aspect_ratio": _aspect_to_flower(aspect_ratio)}
        elif provider == "fastgen_flow":
            url = f"{self.base_url}/flow/video/from-ingredients"
            payload = {"prompt": prompt, "reference_images": [image_data_uri],
                       "aspect_ratio": _aspect_to_flow(aspect_ratio)}
        elif provider == "fastgen_keyframes":
            # v4 endpoint: tighter control — input image is the FIRST frame,
            # video evolves from it without re-interpreting composition.
            # Best for preserving text overlays.
            url = f"{self.base_url}/flow/video/from-keyframes"
            payload = {"prompt": prompt, "start_image": image_data_uri,
                       "aspect_ratio": _aspect_to_flow(aspect_ratio)}
        elif provider == "fastgen_gemini":
            url = f"{self.base_url}/gemini/video/generate"
            payload = {"prompt": prompt, "reference_images": [image_data_uri]}
        elif provider == "fastgen_whisk":
            url = f"{self.base_url}/whisk/video/from-image"
            payload = {"input_image": image_data_uri, "prompt": prompt}
        elif provider == "fastgen_grok":
            url = f"{self.base_url}/grok/video/from-image"
            payload = {"input_image": image_data_uri, "prompt": prompt or ""}
        else:
            return False, f"Unknown video provider: {provider}", "fatal"

        start = time.time()
        attempts_429 = 0
        attempts_5xx = 0
        while True:
            try:
                r = self.session.post(url, json=payload, timeout=60)
            except requests.exceptions.Timeout:
                attempts_5xx += 1
                if time.time() - start > self._max_concurrency_wait:
                    return False, "Submit timeout (retries exhausted)", "server"
                wait = min(2 ** attempts_5xx, 30)
                logger.warning(f"[fgvideo] timeout — retry {attempts_5xx} in {wait}s")
                time.sleep(wait)
                continue
            except requests.exceptions.ConnectionError as e:
                attempts_5xx += 1
                if time.time() - start > self._max_concurrency_wait:
                    return False, _safe_err(e), "server"
                wait = min(2 ** attempts_5xx, 30)
                logger.warning(f"[fgvideo] conn error — retry {attempts_5xx} in {wait}s")
                time.sleep(wait)
                continue
            except Exception as e:
                return False, _safe_err(e), "server"

            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception as e:
                    return False, f"Bad JSON: {_safe_err(e)}", "server"
                if data.get("success") and data.get("operation_id"):
                    return True, data["operation_id"], "none"
                return False, str(data.get("error", "No operation_id"))[:200], "server"

            if r.status_code == 429:
                attempts_429 += 1
                if time.time() - start > self._max_concurrency_wait:
                    return False, "Concurrency wait timeout", "rate_limit"
                wait = min(1.5 + attempts_429 * 0.5, 5.0)
                time.sleep(wait)
                continue

            if r.status_code in (502, 503, 504):
                attempts_5xx += 1
                if time.time() - start > self._max_concurrency_wait:
                    return False, f"HTTP {r.status_code} (retries exhausted)", "server"
                wait = min(2 ** attempts_5xx, 30)
                logger.warning(f"[fgvideo] HTTP {r.status_code} — retry "
                               f"{attempts_5xx} in {wait}s")
                time.sleep(wait)
                continue

            if r.status_code == 500:
                body = (r.text or "")[:300].upper()
                if any(x in body for x in ("SAFETY", "BLOCKED", "FILTER", "POLICY")):
                    return False, "SAFETY_FILTER", "content"
                if any(x in body for x in ("PROMINENT", "WELL-KNOWN", "CELEBRITY")):
                    return False, "PROMINENT_PEOPLE", "content"
                attempts_5xx += 1
                if time.time() - start > self._max_concurrency_wait or attempts_5xx > 3:
                    return False, f"Server 500: {(r.text or '')[:160]}", "server"
                wait = min(2 ** attempts_5xx, 15)
                logger.warning(f"[fgvideo] HTTP 500 (transient?) — retry "
                               f"{attempts_5xx} in {wait}s")
                time.sleep(wait)
                continue

            if r.status_code in (401, 403):
                return False, f"Auth {r.status_code}", "fatal"
            if r.status_code == 422:
                return False, f"Validation: {(r.text or '')[:160]}", "fatal"
            return False, f"HTTP {r.status_code}: {(r.text or '')[:160]}", "server"

    # ─── poll ────────────────────────────────────────────────────────────────
    def _poll(self, operation_id: str, timeout: int = 600) -> Tuple[bool, Optional[str], str]:
        url = f"{self.base_url}/operations/{operation_id}"
        start = time.time()
        attempt = 0
        while time.time() - start < timeout:
            interval = 3 if attempt < 10 else 5 if attempt < 30 else 10
            try:
                r = self.session.get(url, timeout=30)
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
            if status == "success":
                result = data.get("result")
                if isinstance(result, list) and result:
                    result = result[0]
                if result:
                    return True, result, ""
                return False, None, "Empty result"
            if status == "error":
                return False, None, str(data.get("error", "Unknown error"))[:200]
            attempt += 1
            time.sleep(interval)
        return False, None, f"Polling timeout after {timeout}s"

    # ─── save (data URI / URL / bare base64) ────────────────────────────────
    @staticmethod
    def _save(result_data, output_path: str) -> Tuple[bool, str]:
        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            if not isinstance(result_data, str):
                return False, f"Bad result type: {type(result_data).__name__}"

            if result_data.startswith("data:"):
                if "," not in result_data:
                    return False, "Bad data URI"
                _, encoded = result_data.split(",", 1)
                try:
                    video_bytes = base64.b64decode(encoded)
                except Exception:
                    return False, "Base64 decode failed"
                if len(video_bytes) < 10000:
                    return False, f"Video too small ({len(video_bytes)}B)"
                with open(output_path, "wb") as f:
                    f.write(video_bytes)
                return True, output_path

            if result_data.startswith(("http://", "https://")):
                for attempt in range(3):
                    try:
                        r = requests.get(result_data, timeout=180, stream=True)
                        if r.status_code == 200:
                            total = 0
                            with open(output_path, "wb") as f:
                                for chunk in r.iter_content(65536):
                                    if chunk:
                                        f.write(chunk)
                                        total += len(chunk)
                            if total > 10000:
                                return True, output_path
                    except Exception as e:
                        logger.warning(f"DL attempt {attempt+1}: {_safe_err(e)}")
                        time.sleep(2)
                return False, "Download failed after 3 attempts"

            # Bare base64
            if len(result_data) > 5000 and all(c.isalnum() or c in "+/=\r\n" for c in result_data[:200]):
                try:
                    video_bytes = base64.b64decode(result_data)
                except Exception:
                    return False, f"Bare base64 decode failed (len={len(result_data)})"
                if len(video_bytes) < 10000:
                    return False, f"Video too small ({len(video_bytes)}B)"
                with open(output_path, "wb") as f:
                    f.write(video_bytes)
                return True, output_path

            return False, f"Unknown result format (len={len(result_data)})"
        except Exception as e:
            return False, _safe_err(e)

    # ─── public ─────────────────────────────────────────────────────────────
    def generate_video_from_image(self, image_path: str, motion_prompt: str,
                                   output_path: str,
                                   aspect_ratio: str = "9:16",
                                   provider: str = "fastgen_flower",
                                   timeout: int = 600) -> Tuple[bool, str]:
        if not self.is_available():
            return False, "FastGen video unavailable"
        uri = _file_to_data_uri(image_path)
        if not uri:
            return False, "Cannot read image"

        t0 = time.time()
        ok, op_or_err, etype = self._submit(provider, uri, motion_prompt, aspect_ratio)
        if not ok:
            return False, f"Submit [{etype}]: {op_or_err}"

        ok2, result, err = self._poll(op_or_err, timeout=timeout)
        if not ok2:
            return False, f"Poll: {err}"

        ok3, res = self._save(result, output_path)
        if not ok3:
            return False, f"Save: {res}"

        elapsed = time.time() - t0
        logger.info(f"[FGVideo] ✓ {provider} {os.path.basename(output_path)} {elapsed:.1f}s")
        return True, output_path
