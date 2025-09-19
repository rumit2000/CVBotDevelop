#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
talk_maker.py — минималистичный генератор видео-речи через D-ID API.
Поддерживает:
 - CLI режим: python talk_maker.py -t "Привет! Я цифровой аватар Тимура."
 - Импорт из кода: make_talk_video(text, image="avatar.png", out="out.mp4")

Ожидает:
 - переменная окружения DID_API_KEY (или DID_API_USERNAME/DID_API_PASSWORD как base auth)
 - локальный файл avatar.png в корне (или source_url прямо на картинку)

Примечание: этот файл не тянет .env сам — рендер окружения делает Render.
"""

import os
import sys
import time
import json
import argparse
import pathlib
from typing import Optional, Tuple

import httpx

API_BASE = "https://api.d-id.com/v1"  # public REST
DEFAULT_VOICE = "ru-RU-DmitryNeural"  # дефолт синтез

# ====== Вспомогательные ======
def _sanitize_line(x: str) -> str:
    return (x or "").strip().replace("\r", "").replace("\n", "")

def _auth_headers(raw_key: str) -> dict:
    # D-ID поддерживает api-key в заголовке Authorization: Basic <base64> или bearer;
    # в practice: "Authorization": f"Basic {base64_user_pass}" или "Bearer <token>".
    # Здесь используем простой вариант X-API-KEY, который их public SDK также принимает.
    return {"Authorization": f"Bearer {raw_key}", "Content-Type": "application/json"}

def get_key_from_env_or_fail() -> str:
    k = _sanitize_line(os.getenv("DID_API_KEY", ""))
    if k:
        return k
    # Fallback на пару username/password (если используется Basic)
    user = _sanitize_line(os.getenv("DID_API_USERNAME", ""))
    pwd  = _sanitize_line(os.getenv("DID_API_PASSWORD", ""))
    if user and pwd:
        import base64
        return "Basic " + base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("utf-8")
    raise RuntimeError("DID_API_KEY не задан в окружении")

def _abs(p: str) -> str:
    return str(pathlib.Path(p).absolute())

# ====== API вызовы ======
def create_talk(raw_key: str, source_url: str, text: str, voice: str = DEFAULT_VOICE, stitch: bool = True) -> str:
    """
    Создаёт talk и возвращает его id.
    """
    payload = {
        "source_url": source_url,
        "script": {
            "type": "text",
            "input": text,
            "provider": {"type": "microsoft", "voice_id": voice}
        },
        "stitch": stitch
    }
    headers = _auth_headers(raw_key)
    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{API_BASE}/talks", headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        return data["id"]

def get_talk(raw_key: str, talk_id: str) -> dict:
    headers = _auth_headers(raw_key)
    with httpx.Client(timeout=30.0) as c:
        r = c.get(f"{API_BASE}/talks/{talk_id}", headers=headers)
        r.raise_for_status()
        return r.json()

def wait_until_ready(raw_key: str, talk_id: str, timeout: float = 120.0, interval: float = 2.0) -> Tuple[str, dict]:
    """
    Дождаться готовности, вернуть (result_url, full_json).
    """
    start = time.time()
    last = {}
    while True:
        info = get_talk(raw_key, talk_id)
        last = info
        status = info.get("status")
        if status == "done":
            result_url = info.get("result_url") or (info.get("result", {}) or {}).get("url")
            if not result_url:
                raise RuntimeError("result_url отсутствует в ответе D-ID")
            return result_url, info
        if status in ("error", "failed"):
            raise RuntimeError(f"D-ID error: {info}")
        if time.time() - start > timeout:
            raise TimeoutError(f"Ожидание результата превысило {timeout} секунд")
        time.sleep(interval)

def download_file(url: str, out_path: str) -> str:
    out = _abs(out_path)
    with httpx.Client(timeout=None, follow_redirects=True) as c:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    return out

# ====== Источники (аватар) ======
def file_to_data_url(path: str) -> str:
    """
    Простой data-url для картинок. Некоторые тарифы D-ID разрешают source_url=data:...
    Если тариф не поддерживает — нужно загрузить файл на внешний статиκ (S3/Render static) и передавать ссылку.
    """
    import base64
    abspath = _abs(path)
    if not os.path.exists(abspath):
        raise FileNotFoundError(f"Avatar not found: {abspath}")
    with open(abspath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    # попробуем image/png; можно заменить, если у вас другой формат
    return f"data:image/png;base64,{b64}"

# ====== Публичная функция для использования из бота ======
def make_talk_video(text: str,
                    image: str = "avatar.png",
                    out: Optional[str] = None,
                    voice: Optional[str] = None,
                    stitch: bool = True,
                    raw_key: Optional[str] = None) -> str:
    """
    Синхронно делает ролик с озвучкой текста и возвращает путь к mp4.
    Используется из Telegram-бота (импортом модуля).
    """
    if not text or not text.strip():
        raise ValueError("Пустой текст для озвучки")
    raw_key = raw_key or get_key_from_env_or_fail()

    source_url = file_to_data_url(image)  # data: URL, чтобы не заморачиваться с внешним хостингом
    voice_id = (voice or DEFAULT_VOICE)

    talk_id = create_talk(raw_key, source_url, text.strip(), voice=voice_id, stitch=stitch)

    # имя файла по умолчанию — из первых символов текста
    out_file = out or ("".join(ch for ch in text[:40] if ch.isalnum() or ch in (" ", "_", "-"))
                       .strip().replace(" ", "_") or "talk") + ".mp4"
    out_path = _abs(out_file)

    result_url, _info = wait_until_ready(raw_key, talk_id, timeout=180.0, interval=2.0)
    saved = download_file(result_url, out_path)
    return saved

# ====== CLI ======
def parse_args(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("-t", "--text", required=True, help="Текст для озвучки")
    p.add_argument("-i", "--image", default="avatar.png", help="Путь к аватару (PNG/JPG)")
    p.add_argument("-o", "--out", default=None, help="Файл вывода .mp4")
    p.add_argument("--voice", default=DEFAULT_VOICE, help="Идентификатор голоса (TTS)")
    return p.parse_args(argv)

def main():
    args = parse_args()
    saved = make_talk_video(args.text, image=args.image, out=args.out, voice=args.voice)
    print(f"Saved to: {saved}")

if __name__ == "__main__":
    main()
