"""Speech-to-text for the brief input.

Tries (in order, first that works):
  1. Local OpenAI Whisper Python package (`whisper` or `faster-whisper`)
  2. Claude API multimodal audio (api.stepanovikov.uno) — if proxy supports
     OpenAI-style `input_audio` content blocks
  3. Gemini API multimodal audio (raw key, /v1beta/models/.../generateContent)

Returns the recognized text or raises RuntimeError with a clear message.

The user's environment is Vidinator-style: they have whisper config in
gui_config.json but the package isn't installed in this Python — so we expect
a remote-API fallback to be the working path most of the time.
"""

import os
import json
import base64
import logging
import requests
import urllib3
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("wbnator.transcribe")


def _try_local_whisper(audio_path: str, language: str = "ru") -> str:
    try:
        import whisper  # openai-whisper
    except Exception:
        try:
            import faster_whisper
            model = faster_whisper.WhisperModel("medium", device="auto", compute_type="int8")
            segments, _ = model.transcribe(audio_path, language=language)
            return " ".join(s.text.strip() for s in segments).strip()
        except Exception as e:
            raise ImportError("no local whisper") from e
    model = whisper.load_model("medium")
    result = model.transcribe(audio_path, language=language)
    return (result.get("text") or "").strip()


def _audio_to_base64(audio_path: str) -> tuple[str, str]:
    """Returns (base64_data, format_string)."""
    ext = Path(audio_path).suffix.lower().lstrip(".")
    fmt = {"wav": "wav", "mp3": "mp3", "ogg": "ogg",
           "m4a": "mp4", "webm": "webm", "flac": "flac"}.get(ext, "wav")
    with open(audio_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return data, fmt


def _try_claude_audio(cfg: dict, audio_path: str, language: str = "ru") -> str:
    """OpenAI-style audio content. Many Claude proxies on api.stepanovikov.uno
    support `input_audio` blocks; some don't. Try, fall back on 4xx."""
    data, fmt = _audio_to_base64(audio_path)
    headers = {
        "Authorization": f"Bearer {cfg['claude_api_key']}",
        "Content-Type": "application/json",
    }
    sys_prompt = ("Ты транскрибируешь аудио на русском языке. Верни ТОЛЬКО распознанный "
                  "текст — без вступления, без кавычек, без markdown. Если речь невнятна — "
                  "верни пустую строку.")
    payload = {
        "model": cfg.get("claude_model_primary", "claude-opus-4.6"),
        "max_tokens": 1500,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": data, "format": fmt}},
                {"type": "text", "text": f"Транскрибируй на русском (lang={language})."},
            ]},
        ],
    }
    r = requests.post(cfg["claude_base_url"], json=payload, headers=headers,
                      timeout=(10, 180), verify=False)
    if r.status_code == 200:
        body = r.json()
        text = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return (text or "").strip()
    raise RuntimeError(f"Claude audio HTTP {r.status_code}: {(r.text or '')[:200]}")


def _try_gemini_audio(cfg: dict, audio_path: str, language: str = "ru") -> str:
    """Use Gemini API directly with raw key from secrets/keys.txt (Vidinator's pool).
    Sends audio as inline_data to gemini-2.5-flash-lite, asks for transcription."""
    # Load a key from Vidinator's pool
    keys_file = os.path.join(cfg.get("vidinator_core_path", ""), "..", "secrets", "keys.txt")
    keys_file = os.path.abspath(keys_file)
    if not os.path.isfile(keys_file):
        raise FileNotFoundError("No Gemini keys.txt available")
    with open(keys_file, "r", encoding="utf-8") as f:
        keys = [k.strip() for k in f.read().splitlines() if k.strip().startswith("AIza")]
    if not keys:
        raise RuntimeError("No usable Gemini keys")

    data, fmt = _audio_to_base64(audio_path)
    mime = {"wav": "audio/wav", "mp3": "audio/mp3", "ogg": "audio/ogg",
            "mp4": "audio/mp4", "webm": "audio/webm", "flac": "audio/flac"}.get(fmt, "audio/wav")

    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": f"Транскрибируй аудио на русском языке. Верни ТОЛЬКО распознанный текст. lang={language}."},
                {"inline_data": {"mime_type": mime, "data": data}},
            ],
        }],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1500},
    }

    last_err = None
    for key in keys[:5]:  # try up to 5 keys
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.5-flash-lite:generateContent?key={key}")
        try:
            r = requests.post(url, json=payload, timeout=(10, 90), verify=False)
            if r.status_code == 200:
                body = r.json()
                text = ""
                for cand in body.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if "text" in part:
                            text += part["text"]
                return text.strip()
            last_err = f"HTTP {r.status_code}: {(r.text or '')[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
            continue
    raise RuntimeError(f"Gemini transcription failed: {last_err}")


def transcribe(audio_path: str, cfg: dict, language: str = "ru") -> str:
    """Tries: local whisper → Claude → Gemini. First successful wins."""
    if not audio_path or not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    # 1. Local whisper
    try:
        text = _try_local_whisper(audio_path, language=language)
        if text:
            logger.info(f"[transcribe] local whisper → {len(text)} chars")
            return text
    except Exception as e:
        logger.info(f"[transcribe] no local whisper ({type(e).__name__})")

    # 2. Claude multimodal audio
    try:
        text = _try_claude_audio(cfg, audio_path, language=language)
        if text:
            logger.info(f"[transcribe] Claude audio → {len(text)} chars")
            return text
    except Exception as e:
        logger.info(f"[transcribe] Claude audio fail: {str(e)[:160]}")

    # 3. Gemini multimodal audio
    try:
        text = _try_gemini_audio(cfg, audio_path, language=language)
        if text:
            logger.info(f"[transcribe] Gemini audio → {len(text)} chars")
            return text
    except Exception as e:
        logger.warning(f"[transcribe] Gemini audio fail: {str(e)[:160]}")

    raise RuntimeError(
        "Распознавание речи не удалось. Установи локальный Whisper "
        "(`pip install openai-whisper`) или используй текстовый ввод."
    )
