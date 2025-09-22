#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
talk_maker.py — генератор видео-речи через D-ID API.

Запуск:
  CLI:  python talk_maker.py -t "Привет!"
  Код:  make_talk_video("Привет!", image="avatar.png", out="talk.mp4")

Ожидает:
  DID_API_KEY  (или DID_API_USERNAME/DID_API_PASSWORD для basic)
  avatar.png в корне ИЛИ DID_SOURCE_URL=https://.../avatar.png

ENV (опционально):
  DID_AUTH_MODE=basic|xapikey|bearer   # зафиксировать режим авторизации
"""

import os
import time
import argparse
import pathlib
import base64
from typing import Optional, Tuple

import httpx

API_BASE = "https://api.d-id.com/v1"
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
    }

# ====== Авторизация ======
def get_key_from_env_or_fail() -> str:
    k = _sanitize(os.getenv("DID_API_KEY", ""))
    if k:
        return k
    user = _sanitize(os.getenv("DID_API_USERNAME", ""))
    pwd  = _sanitize(os.getenv("DID_API_PASSWORD", ""))
    if user and pwd:
        return base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("utf-8")
    raise RuntimeError("DID_API_KEY не задан (или укажите DID_API_USERNAME/DID_API_PASSWORD)")

def _auth_headers(raw_key: str, mode: str) -> dict:
    h = _headers_common()
    if mode == "basic":
        # большинство аккаунтов D-ID принимают ключ как Basic <api_key> ИЛИ Basic <base64(user:pass)>
        h["Authorization"] = f"Basic {raw_key}"
    elif mode == "xapikey":
        h["x-api-key"] = raw_key  # регистр в HTTP несущественен, но используем нижний
    else:  # bearer
        h["Authorization"] = f"Bearer {raw_key}"
    return h

def _request_json(method: str, url: str, json_body, raw_key: str):
    """
    Делаем запрос с перебором режимов авторизации.
    Порядок:
      1) фиксированный из DID_AUTH_MODE, если задан
      2) иначе: basic -> xapikey -> bearer
    На 401/403 пытаемся следующий режим, но если сервер вернул осмысленное тело — поднимаем
    понятное исключение с текстом ошибки.
    """
    fixed = _sanitize(os.getenv("DID_AUTH_MODE", "")).lower()
    modes = [fixed] if fixed in ("basic", "xapikey", "bearer") else ["basic", "xapikey", "bearer"]

    last_text = None
    last_status = None
    for m in modes:
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as c:
                r = c.request(method, url, headers=_auth_headers(raw_key, m), json=json_body)
                last_status = r.status_code
                ct = (r.headers.get("Content-Type") or "").lower()
                last_text = r.text

                if r.status_code in (401, 403):
                    # Если сервер прислал понятное JSON-объяснение — пробросим его сразу.
                    try:
                        data = r.json()
                        msg = (data.get("message") or data.get("error") or str(data)).strip()
                        # Если это не похоже на авторизацию, выкинем сразу, не переключаясь
                        if "key" in msg.lower() or "auth" in msg.lower() or "forbidden" in msg.lower():
                            # это авторизация — попробуем следующий режим
                            pass
                        else:
                            raise RuntimeError(f"D-ID error: {msg}")
                    except Exception:
                        # не json — просто попробуем следующий режим
                        pass
                    continue

                r.raise_for_status()
                if ct.startswith("application/json"):
                    return r.json()
                return {"raw": r.text}
        except Exception as e:
            # пробуем следующий режим
            last_text = f"{type(e).__name__}: {e}"
            continue

    # все режимы не сработали — покажем последнюю ошибку подробнее
    raise RuntimeError(f"Authorization to D-ID failed (last_status={last_status}): {last_text}")

# ====== API ======
def create_talk(raw_key: str, source_url: str, text: str, voice: str = DEFAULT_VOICE, stitch: bool = True) -> str:
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
            err = (info.get("error") or info.get("message") or str(info) or "").strip()
            hint = ""
            if "source_url" in err.lower():
                hint = (
                    "\nПодсказка: Ваш тариф D-ID может не принимать source_url=data:... "
                    "Загрузите avatar.png на публичный HTTPS и установите "
                    "DID_SOURCE_URL=https://.../avatar.png в переменных окружения."
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
                    stitch: bool = True,
                    raw_key: Optional[str] = None) -> str:
    if not text or not text.strip():
        raise ValueError("Пустой текст для озвучки")

    raw_key = raw_key or get_key_from_env_or_fail()

    # Если задан DID_SOURCE_URL — используем его. Иначе data: из файла.
    env_src = _sanitize(os.getenv("DID_SOURCE_URL", ""))
    source_url = env_src if env_src else file_to_data_url(image)

    voice_id = (voice or DEFAULT_VOICE)

    talk_id = create_talk(raw_key, source_url, text.strip(), voice=voice_id, stitch=stitch)

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
