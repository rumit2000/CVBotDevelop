#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
talk_maker.py — генератор видео-речи через D-ID API.

Отличия:
- Ключ берём сначала из ENV (Render): DID_API_KEY="USER:PASS" или пара DID_API_USERNAME / DID_API_PASSWORD.
- Если задан DID_SOURCE_URL=https://.../avatar.png — используем его напрямую; иначе загрузим локальный файл на /images.
- Голос + провайдер подбираются согласованно из /tts/voices (или задаются ENV: DID_TTS_PROVIDER / DID_TTS_VOICE).
- Фолбэк-голос: ru-RU-DmitryNeural (microsoft).

Эндпоинты D-ID:
  POST https://api.d-id.com/images
  GET  https://api.d-id.com/tts/voices
  POST https://api.d-id.com/talks
  GET  https://api.d-id.com/talks/{id}

Авторизация: Basic base64(user:pass)
"""

import os
import sys
import time
import json
import base64
import pathlib
import argparse
from typing import Optional, List, Tuple, Dict, Any

# optional dotenv (локально удобно)
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None  # type: ignore

# HTTP — как в локальной рабочей версии
import requests

DID_BASE = "https://api.d-id.com"
DEFAULT_IMAGE_URL = "https://create-images-results.d-id.com/DefaultPresenters/Emma_f/image.jpeg"

# Безопасный фолбэк (русский Microsoft TTS)
RU_MS_FALLBACK_VOICE = "ru-RU-DmitryNeural"
RU_MS_FALLBACK_PROVIDER = "microsoft"

# ----------------------- helpers -----------------------

def log(msg: str, *, flush=True):
    print(msg, flush=flush)

def err(msg: str):
    print(msg, file=sys.stderr, flush=True)

def _sanitize_line(raw: str) -> str:
    raw = (raw or "").strip().strip('"').strip("'")
    if "#" in raw:
        raw = raw.split("#", 1)[0].strip()
    return raw.replace("\u00A0", "")

def _abs(p: str) -> str:
    return str(pathlib.Path(p).absolute())

# ----------------------- ключи (ENV → .env → apikey) -----------------------

def _read_key_from_env() -> Optional[str]:
    key = _sanitize_line(os.getenv("DID_API_KEY", ""))
    if key:
        return key
    user = _sanitize_line(os.getenv("DID_API_USERNAME", ""))
    pwd  = _sanitize_line(os.getenv("DID_API_PASSWORD", ""))
    if user and pwd:
        return f"{user}:{pwd}"
    return None

def _load_env_local() -> None:
    if load_dotenv:
        here = pathlib.Path(__file__).resolve().parent
        env_path = here / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=str(env_path), override=True)

def _read_key_from_apikey_file() -> Optional[str]:
    here = pathlib.Path(__file__).resolve().parent
    apikey_path = here / "apikey"
    if not apikey_path.exists():
        return None
    try:
        with open(apikey_path, "r", encoding="utf-8") as f:
            return _sanitize_line(f.readline())
    except Exception:
        return None

def get_api_key_strict() -> str:
    key = _read_key_from_env()
    if not key:
        _load_env_local()
        key = _read_key_from_env() or _read_key_from_apikey_file()
    if not key:
        sys.exit(
            "❌ Не найден ключ D-ID. Задайте DID_API_KEY=USER:PASS (или DID_API_USERNAME / DID_API_PASSWORD)."
        )
    return key

# ----------------------- авторизация -----------------------

def _basic_header_value(raw_key: str) -> str:
    """
    Преобразуем 'user:pass' → 'Basic base64(user:pass)'.
    Если передали уже base64 — тоже поддержим (но предпочтительно давать user:pass).
    """
    if ":" in raw_key:
        token = base64.b64encode(raw_key.encode("utf-8")).decode("ascii")
        return f"Basic {token}"
    # возможно, уже base64:
    return f"Basic {raw_key}"

def headers_json(raw_key: str) -> dict:
    return {
        "Authorization": _basic_header_value(raw_key),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def headers_multipart(raw_key: str) -> dict:
    return {
        "Authorization": _basic_header_value(raw_key),
        "Accept": "application/json",
    }

# ----------------------- изображение (source_url) -----------------------

def upload_image_if_exists(raw_key: str, local_path: str) -> str:
    """
    Возвращает HTTPS-URL изображения для source_url:
    1) Если задан DID_SOURCE_URL — используем его напрямую.
    2) Если есть локальный файл — загружаем на /images.
    3) Иначе — DEFAULT_IMAGE_URL.
    """
    env_src = _sanitize_line(os.getenv("DID_SOURCE_URL", ""))
    if env_src.lower().startswith("http"):
        log("🌐 Использую DID_SOURCE_URL для source_url.")
        return env_src

    p = pathlib.Path(local_path)
    if not p.exists():
        log(f"ℹ️  '{local_path}' не найден. Возьмём изображение по умолчанию.")
        return DEFAULT_IMAGE_URL

    url = f"{DID_BASE}/images"
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    with open(p, "rb") as f:
        files = {"image": (p.name, f, mime)}
        r = requests.post(url, headers=headers_multipart(raw_key), files=files, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Ошибка загрузки изображения: {r.status_code} {r.text}")
    data = r.json()
    img_url = data.get("url") or data.get("image_url") or data.get("result_url") or data.get("signedUrl")
    if not img_url:
        raise RuntimeError(f"Не удалось извлечь ссылку на изображение: {json.dumps(data, ensure_ascii=False)}")
    log("🖼️  Изображение загружено, URL получен.")
    return img_url

# ----------------------- выбор голоса и провайдера -----------------------

def _extract_voice_fields(v: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], str]:
    """
    Возвращает (provider, voice_id, lang_text) из объекта голоса, максимально универсально.
    """
    provider = v.get("provider") or v.get("tts_provider") or v.get("source")
    voice_id = v.get("voice_id") or v.get("short_name") or v.get("name")
    lang = v.get("language") or v.get("locale") or ""
    # нормируем к нижнему регистру для поиска "ru"
    lang_text = f"{lang} {json.dumps(v, ensure_ascii=False)}".lower()
    return (str(provider).lower() if provider else None,
            str(voice_id) if voice_id else None,
            lang_text)

def pick_ru_voice_with_provider(raw_key: str) -> Tuple[str, str]:
    """
    Пытаемся найти русский голос и СООТВЕТСТВУЮЩЕГО провайдера.
    Приоритет: ENV → /tts/voices (microsoft, ru-*) → фолбэк (microsoft + ru-RU-DmitryNeural).
    """
    # 0) ENV override
    env_voice = _sanitize_line(os.getenv("DID_TTS_VOICE", ""))
    env_provider = _sanitize_line(os.getenv("DID_TTS_PROVIDER", ""))
    if env_voice:
        prov = env_provider if env_provider else RU_MS_FALLBACK_PROVIDER
        log(f"🔊 Голос из ENV: {env_voice} (provider={prov})")
        return prov, env_voice

    # 1) /tts/voices
    try:
        r = requests.get(f"{DID_BASE}/tts/voices", headers=headers_json(raw_key), timeout=30)
        if r.status_code < 300:
            payload = r.json()
            voices = payload.get("voices", payload) if isinstance(payload, dict) else payload
            # сначала пробуем microsoft + ru
            ms_ru: List[Tuple[str, str]] = []
            any_ru: List[Tuple[str, str]] = []
            for v in voices or []:
                provider, voice_id, lang_text = _extract_voice_fields(v)
                if not voice_id:
                    continue
                if "ru" in lang_text:
                    if provider == "microsoft":
                        ms_ru.append(("microsoft", voice_id))
                    else:
                        any_ru.append((provider or "", voice_id))
            if ms_ru:
                prov, vid = ms_ru[0]
                log(f"🔊 Голос (microsoft, ru): {vid}")
                return prov, vid
            if any_ru:
                prov, vid = any_ru[0]
                log(f"🔊 Голос (любой провайдер, ru): {vid} (provider={prov})")
                return (prov or RU_MS_FALLBACK_PROVIDER, vid)
            # если русских не нашли — ищем любой microsoft
            ms_any: List[Tuple[str, str]] = []
            for v in voices or []:
                provider, voice_id, lang_text = _extract_voice_fields(v)
                if voice_id and provider == "microsoft":
                    ms_any.append(("microsoft", voice_id))
            if ms_any:
                prov, vid = ms_any[0]
                log(f"🔊 Голос (microsoft, любой): {vid}")
                return prov, vid
            log("ℹ️  В /tts/voices не нашёл подходящих голосов, использую фолбэк.")
        else:
            err(f"⚠️  /tts/voices {r.status_code}: {r.text[:200]}")
    except Exception as e:
        err(f"⚠️  Не удалось получить /tts/voices: {e}")

    # 2) Фолбэк — надёжный ms-голос
    log(f"🔊 Голос по умолчанию: {RU_MS_FALLBACK_VOICE} (provider={RU_MS_FALLBACK_PROVIDER})")
    return RU_MS_FALLBACK_PROVIDER, RU_MS_FALLBACK_VOICE

# ----------------------- создание talk -----------------------

def create_talk(raw_key: str, image_url: str, text: str,
                voice_id: str, provider: str, stitch: bool = True) -> str:
    url = f"{DID_BASE}/talks"
    payload = {
        "source_url": image_url,
        "script": {
            "type": "text",
            "input": text,
            "provider": {"type": provider, "voice_id": voice_id}
        },
        "config": {"stitch": stitch}
    }
    r = requests.post(url, headers=headers_json(raw_key), data=json.dumps(payload), timeout=60)
    if r.status_code >= 300:
        try:
            details = r.json()
        except Exception:
            details = r.text
        raise RuntimeError(f"Ошибка создания talk: {r.status_code} {details}")
    data = r.json()
    talk_id = data.get("id")
    if not talk_id:
        raise RuntimeError(f"Нет id в ответе на создание talk: {json.dumps(data, ensure_ascii=False)}")
    log(f"🎬 Создан talk: {talk_id}")
    return talk_id

def wait_and_download_result(raw_key: str, talk_id: str, out_path: str,
                             poll_sec: float = 2.0, max_wait_sec: int = 300) -> str:
    url = f"{DID_BASE}/talks/{talk_id}"
    waited = 0.0
    while waited <= max_wait_sec:
        r = requests.get(url, headers=headers_json(raw_key), timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"Ошибка статуса: {r.status_code} {r.text}")
        data = r.json()
        status = data.get("status")
        if status == "done":
            result_url = data.get("result_url")
            if not result_url:
                raise RuntimeError(f"status=done, но нет result_url: {json.dumps(data, ensure_ascii=False)}")
            log(f"✅ Готово. Скачиваю: {result_url}")
            vid = requests.get(result_url, timeout=180)
            if vid.status_code >= 300:
                raise RuntimeError(f"Не удалось скачать видео: {vid.status_code} {vid.text[:200]}")
            with open(out_path, "wb") as f:
                f.write(vid.content)
            return out_path
        elif status in {"created", "started"}:
            log(f"⏳ Статус: {status}. Жду {poll_sec} сек...")
            time.sleep(poll_sec)
            waited += poll_sec
        else:
            raise RuntimeError(f"Неожиданный статус '{status}': {json.dumps(data, ensure_ascii=False)}")
    raise TimeoutError("Превышено время ожидания результата")

# ----------------------- публичная функция -----------------------

def make_talk_video(text: str,
                    image: str = "avatar.png",
                    out: Optional[str] = None,
                    voice: Optional[str] = None,
                    stitch: bool = True,
                    raw_key: Optional[str] = None) -> str:
    """
    Синхронно делает ролик и возвращает путь к mp4. Используется из Телеграм-бота.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой текст для озвучки")

    raw_key = raw_key or get_api_key_strict()
    image_url = upload_image_if_exists(raw_key, image)

    # provider + voice (ENV → /tts/voices → фолбэк)
    if voice:
        # если явно передали voice — используем выбранный провайдер из ENV или microsoft по умолчанию
        provider = _sanitize_line(os.getenv("DID_TTS_PROVIDER", RU_MS_FALLBACK_PROVIDER))
        voice_id = voice
        log(f"🔊 Использую явный голос: {voice_id} (provider={provider})")
    else:
        provider, voice_id = pick_ru_voice_with_provider(raw_key)

    talk_id = create_talk(raw_key, image_url, text, voice_id, provider, stitch=stitch)

    out_file = out or ("".join(ch for ch in text[:40] if ch.isalnum() or ch in (" ", "_", "-")).strip().replace(" ", "_") or "talk") + ".mp4"
    out_path = _abs(out_file)
    saved = wait_and_download_result(raw_key, talk_id, out_path)
    log(f"\n🎉 Видео сохранено: {saved}")
    return saved

# ----------------------- CLI -----------------------

def main():
    parser = argparse.ArgumentParser(description="D-ID talking head CLI")
    parser.add_argument("-t", "--text", help="Текст одной строкой (без stdin).")
    parser.add_argument("-T", "--text-file", help="Путь к txt-файлу с текстом.")
    parser.add_argument("-i", "--image", default="avatar.png", help="Путь к изображению (по умолчанию avatar.png).")
    parser.add_argument("-o", "--out", help="Имя выходного MP4 (если не задано — по тексту).")
    parser.add_argument("-v", "--voice", default=None, help="voice_id (например, ru-RU-DmitryNeural).")
    parser.add_argument("--no-stitch", action="store_true", help="Отключить stitch в config.")
    args = parser.parse_args()

    raw_key = get_api_key_strict()
    # читаем текст
    txt = None
    if args.text:
        txt = args.text.strip()
    elif args.text_file:
        p = pathlib.Path(args.text_file)
        if not p.exists():
            sys.exit(f"❌ Файл не найден: {p}")
        txt = p.read_text(encoding="utf-8").strip()
    else:
        print("Введите текст (пустая строка — конец):")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "":
                break
            lines.append(line)
        txt = "\n".join(lines).strip()
    if not txt:
        sys.exit("❌ Пустой текст.")

    image_url = upload_image_if_exists(raw_key, args.image)
    provider, voice_id = pick_ru_voice_with_provider(raw_key) if not args.voice else (
        _sanitize_line(os.getenv("DID_TTS_PROVIDER", RU_MS_FALLBACK_PROVIDER)), args.voice
    )
    stitch = not args.no_stitch

    talk_id = create_talk(raw_key, image_url, txt, voice_id, provider, stitch=stitch)

    out_file = args.out or ("".join(ch for ch in txt[:40] if ch.isalnum() or ch in (" ", "_", "-")).strip().replace(" ", "_") or "talk") + ".mp4"
    out_path = _abs(out_file)
    saved = wait_and_download_result(raw_key, talk_id, out_path)
    log(f"\n🎉 Видео сохранено: {saved}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        err("🔥 Необработанная ошибка:")
        traceback.print_exc()
        sys.exit(1)
