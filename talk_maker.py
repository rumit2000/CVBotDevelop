#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
talk_maker.py — генератор видео-речи через D-ID Talks API.

Как использовать:
  CLI:  python talk_maker.py -t "Привет!"
  Импорт: make_talk_video("Привет!", image="avatar.png", out="talk.mp4")

ENV:
  DID_API_KEY="API_USERNAME:API_PASSWORD"
    ИЛИ
  DID_API_USERNAME=API_USERNAME
  DID_API_PASSWORD=API_PASSWORD

Если тариф не принимает data:-аватар:
  DID_SOURCE_URL=https://.../avatar.png
"""

import os
import time
import argparse
import pathlib
import base64
from typing import Optional, Tuple

import httpx

# Важно: без /v1 — по текущей доке эндпоинт /talks
API_BASE = "https://api.d-id.com"
DEFAULT_VOICE = "ru-RU-DmitryNeural"

# ====== Утилиты ======
def _sanitize(x: str) -> str:
    return (x or "").strip().replace("\r", "").replace("\n", "")

def _abs(p: str) -> str:
    return str(pathlib.Path(p).absolute())

def _headers_common() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "cvbotdevelop/1.0 (+https://github.com/rumit2000/CVBotDevelop)"
    }

# ====== Авторизация (Basic USER:PASS) ======
def get_basic_user_pass_or_fail() -> str:
    """
    Возвращает 'USER:PASS' для заголовка Authorization: Basic USER:PASS.
    Берём из:
      - DID_API_KEY, если там есть двоеточие;
      - иначе из пары DID_API_USERNAME + DID_API_PASSWORD.
    """
    raw = _sanitize(os.getenv("DID_API_KEY", ""))
    if raw.lower().startswith("basic "):
        raw = raw[6:].strip()
    if ":" in raw:
        user_pass = raw
    else:
        user = _sanitize(os.getenv("DID_API_USERNAME", ""))
        pwd  = _sanitize(os.getenv("DID_API_PASSWORD", ""))
        if not (user and pwd):
            raise RuntimeError(
                "DID_API_KEY должен быть вида 'USER:PASS', либо задайте DID_API_USERNAME и DID_API_PASSWORD."
            )
        user_pass = f"{user}:{pwd}"

    # простая валидация
    if ":" not in user_pass or not user_pass.split(":", 1)[0] or not user_pass.split(":", 1)[1]:
        raise RuntimeError("Некорректная пара USER:PASS для D-ID (проверьте переменные окружения).")
    return user_pass

def _headers_basic_literal(user_pass: str) -> dict:
    """D-ID дока: Authorization: Basic API_USERNAME:API_PASSWORD (без base64)."""
    h = _headers_common().copy()
    h["Authorization"] = f"Basic {user_pass}"
    return h

def _headers_basic_b64(user_pass: str) -> dict:
    """Фолбэк: классический Basic по RFC (base64(user:pass)), на случай редких аккаунтов."""
    h = _headers_common().copy()
    token = base64.b64encode(user_pass.encode("utf-8")).decode("ascii")
    h["Authorization"] = f"Basic {token}"
    return h

def _request_json(method: str, url: str, json_body, user_pass: str):
    """
    Пробуем 2 режима авторизации по очереди:
      1) Basic USER:PASS  (как в доке D-ID)
      2) Basic base64(USER:PASS)  (фолбэк)
    Если 401/403 — собираем максимально понятное сообщение.
    """
    variants = [
        ("basic_literal", _headers_basic_literal(user_pass)),
        ("basic_b64",     _headers_basic_b64(user_pass)),
    ]

    last_status = None
    last_text = None

    for name, headers in variants:
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as c:
                r = c.request(method, url, headers=headers, json=json_body)
                last_status = r.status_code
                ct = (r.headers.get("Content-Type") or "").lower()
                last_text = r.text

                if r.status_code in (401, 403):
                    # попробуем следующий режим; если это последний — сбросим осмысленную ошибку ниже
                    continue

                r.raise_for_status()
                if ct.startswith("application/json"):
                    return r.json()
                return {"raw": r.text}
        except Exception as e:
            last_text = f"{type(e).__name__}: {e}"
            continue

    hint = ""
    if last_status in (401, 403):
        hint = (
            "\nПроверьте, что DID_API_KEY задан как 'USER:PASS' "
            "или заданы DID_API_USERNAME/DID_API_PASSWORD. "
            "Также убедитесь, что запрос идёт на https://api.d-id.com/talks."
        )
    raise RuntimeError(f"Авторизация в D-ID не прошла (HTTP {last_status}): {last_text}{hint}")

# ====== API ======
def create_talk(user_pass: str, source_url: str, text: str, voice: str = DEFAULT_VOICE, stitch: bool = True) -> str:
    payload = {
        "source_url": source_url,
        "script": {
            "type": "text",
            "input": text,
            "provider": {"type": "microsoft", "voice_id": voice}
        },
        "stitch": stitch
    }
    data = _request_json("POST", f"{API_BASE}/talks", payload, user_pass)
    if "id" not in data:
        raise RuntimeError(f"Неожиданный ответ D-ID: {data}")
    return data["id"]

def get_talk(user_pass: str, talk_id: str) -> dict:
    return _request_json("GET", f"{API_BASE}/talks/{talk_id}", None, user_pass)

def wait_until_ready(user_pass: str, talk_id: str, timeout: float = 180.0, interval: float = 2.0) -> Tuple[str, dict]:
    start = time.time()
    last = {}
    while True:
        info = get_talk(user_pass, talk_id)
        last = info
        status = (info.get("status") or "").lower()
        if status == "done":
            result_url = info.get("result_url") or (info.get("result", {}) or {}).get("url")
            if not result_url:
                raise RuntimeError("result_url отсутствует в ответе D-ID")
            return result_url, info
        if status in ("error", "failed"):
            err = (info.get("error") or info.get("message") or str(info) or "").strip()
            hint = ""
            if "source_url" in err.lower():
                hint = (
                    "\nПодсказка: тариф может не принимать source_url как data: URL. "
                    "Загрузите avatar.png на публичный HTTPS и задайте "
                    "DID_SOURCE_URL=https://.../avatar.png."
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

# ====== Источник (аватар) ======
def file_to_data_url(path: str) -> str:
    abspath = _abs(path)
    if not os.path.exists(abspath):
        raise FileNotFoundError(f"Avatar not found: {abspath}")
    with open(abspath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"

# ====== Публичная функция ======
def make_talk_video(text: str,
                    image: str = "avatar.png",
                    out: Optional[str] = None,
                    voice: Optional[str] = None,
                    stitch: bool = True) -> str:
    """
    Делает ролик с озвучкой текста и возвращает путь к mp4.
    Используется из Telegram-бота (импортом модуля).
    """
    if not text or not text.strip():
        raise ValueError("Пустой текст для озвучки")

    user_pass = get_basic_user_pass_or_fail()

    # Если задан DID_SOURCE_URL — используем его. Иначе data: из файла.
    env_src = _sanitize(os.getenv("DID_SOURCE_URL", ""))
    source_url = env_src if env_src else file_to_data_url(image)

    voice_id = (voice or DEFAULT_VOICE)

    talk_id = create_talk(user_pass, source_url, text.strip(), voice=voice_id, stitch=stitch)

    safe_head = "".join(ch for ch in text[:40] if ch.isalnum() or ch in (" ", "_", "-")).strip().replace(" ", "_")
    out_file = out or (safe_head or "talk") + ".mp4"
    out_path = _abs(out_file)

    result_url, _info = wait_until_ready(user_pass, talk_id, timeout=180.0, interval=2.0)
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
