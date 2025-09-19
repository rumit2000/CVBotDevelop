#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
talk_maker.py — минималистичный генератор видео-речи через D-ID API.
Поддерживает:
 - CLI режим: python talk_maker.py -t "Привет! Я цифровой аватар Тимура."
 - Импорт из кода: make_talk_video(text, image="avatar.png", out="out.mp4")

Ожидает:
 - переменная окружения DID_API_KEY (или DID_API_USERNAME/DID_API_PASSWORD для basic)
 - локальный файл avatar.png в корне (или HTTPS-ссылка в DID_SOURCE_URL / явный source_url)

Если тариф D-ID не принимает source_url=data:..., положите картинку на HTTPS
и укажите переменную окружения DID_SOURCE_URL=https://.../avatar.png
"""

import os
import time
import argparse
import pathlib
from typing import Optional, Tuple

import httpx

API_BASE = "https://api.d-id.com/v1"
DEFAULT_VOICE = "ru-RU-DmitryNeural"

# ====== Вспомогательные ======
def _sanitize_line(x: str) -> str:
    return (x or "").strip().replace("\r", "").replace("\n", "")

def _abs(p: str) -> str:
    return str(pathlib.Path(p).absolute())

# ====== Авторизация ======
def get_key_from_env_or_fail() -> str:
    """
    Возвращает сырой ключ/токен для D-ID:
    - DID_API_KEY (рекомендуется)
    - или пара DID_API_USERNAME + DID_API_PASSWORD (будет использован как Basic)
    """
    k = _sanitize_line(os.getenv("DID_API_KEY", ""))
    if k:
        return k
    user = _sanitize_line(os.getenv("DID_API_USERNAME", ""))
    pwd  = _sanitize_line(os.getenv("DID_API_PASSWORD", ""))
    if user and pwd:
        import base64
        # вернём уже base64, чтобы _auth_headers мог подставить как Basic <base64>
        return base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("utf-8")
    raise RuntimeError("DID_API_KEY не задан в окружении (или укажите DID_API_USERNAME/DID_API_PASSWORD)")

def _auth_headers(raw_key: str, mode: str = "bearer") -> dict:
    """
    Возвращает заголовки авторизации для D-ID.
    Поддерживаемые режимы: bearer | basic | xapikey
    """
    h = {"Content-Type": "application/json"}
    k = (raw_key or "").strip()
    if not k:
        return h
    if mode == "bearer":
        # Authorization: Bearer <token>
        h["Authorization"] = f"Bearer {k}"
    elif mode == "basic":
        # Authorization: Basic <base64(user:pass)> или Basic <api-key> (на некоторых аккаунтах)
        h["Authorization"] = f"Basic {k}"
    else:
        # X-API-KEY: <token>
        h["X-API-KEY"] = k
    return h

def _request_json(method: str, url: str, json_body, raw_key: str):
    """
    Делает запрос к D-ID, пробуя несколько вариантов авторизации:
    1) Bearer
    2) Basic
    3) X-API-KEY
    Если 401/403 — пробуем следующий способ. Иначе — поднимаем исключение при ошибке.
    """
    modes = ["bearer", "basic", "xapikey"]
    last_exc = None
    for m in modes:
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as c:
                r = c.request(method, url, headers=_auth_headers(raw_key, m), json=json_body)
                if r.status_code in (401, 403):
                    # пробуем другой тип авторизации
                    continue
                r.raise_for_status()
                if r.content and r.headers.get("Content-Type", "").lower().startswith("application/json"):
                    return r.json()
                # не JSON — вернём как текст
                return {"raw": r.text}
        except Exception as e:
            last_exc = e
    # все способы не сработали
    if last_exc:
        raise last_exc
    raise RuntimeError("Authorization to D-ID failed")

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
    data = _request_json("POST", f"{API_BASE}/talks", payload, raw_key)
    if "id" not in data:
        raise RuntimeError(f"Unexpected D-ID response: {data}")
    return data["id"]

def get_talk(raw_key: str, talk_id: str) -> dict:
    return _request_json("GET", f"{API_BASE}/talks/{talk_id}", None, raw_key)

def wait_until_ready(raw_key: str, talk_id: str, timeout: float = 180.0, interval: float = 2.0) -> Tuple[str, dict]:
    """
    Дождаться готовности, вернуть (result_url, full_json).
    """
    start = time.time()
    last = {}
    while True:
        info = get_talk(raw_key, talk_id)
        last = info
        status = (info.get("status") or "").lower()
        if status == "done":
            result_url = info.get("result_url") or (info.get("result", {}) or {}).get("url")
            if not result_url:
                raise RuntimeError("result_url отсутствует в ответе D-ID")
            return result_url, info
        if status in ("error", "failed"):
            # Частый кейс: некоторые тарифы запрещают source_url=data:...
            err = (info.get("error") or info.get("message") or str(info) or "").strip()
            hint = ""
            if "source_url" in err.lower():
                hint = (
                    "\nПодсказка: Ваш тариф D-ID может не принимать source_url как data: URL. "
                    "Загрузите avatar.png на доступный по HTTPS хост (S3/статик Render) и "
                    "установите переменную окружения DID_SOURCE_URL=https://.../avatar.png."
                )
            raise RuntimeError(f"D-ID error: {err}{hint}")
        if time.time() - start > timeout:
            raise TimeoutError(f"Ожидание результата превысило {timeout} секунд. Последний статус: {last}")
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
    Простой data: URL для картинки. Если тариф не поддерживает — используйте DID_SOURCE_URL.
    """
    import base64
    abspath = _abs(path)
    if not os.path.exists(abspath):
        raise FileNotFoundError(f"Avatar not found: {abspath}")
    with open(abspath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
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

    # Если задан DID_SOURCE_URL — используем его. Иначе попробуем data: из локального файла.
    env_src = _sanitize_line(os.getenv("DID_SOURCE_URL", ""))
    if env_src:
        source_url = env_src
    else:
        source_url = file_to_data_url(image)

    voice_id = (voice or DEFAULT_VOICE)

    talk_id = create_talk(raw_key, source_url, text.strip(), voice=voice_id, stitch=stitch)

    # имя файла по умолчанию — из первых символов текста
    safe_head = "".join(ch for ch in text[:40] if ch.isalnum() or ch in (" ", "_", "-")).strip().replace(" ", "_")
    out_file = out or (safe_head or "talk") + ".mp4"
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
