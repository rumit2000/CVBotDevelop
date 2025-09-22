#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
talk_maker.py — генератор видео-речи через D-ID API.

Что нового vs локальной версии:
- Ключ берём СНАЧАЛА из переменных окружения (Render): DID_API_KEY="USER:PASS"
  или пара DID_API_USERNAME / DID_API_PASSWORD. Если их нет — как раньше, пытаемся
  найти .env рядом со скриптом или файл 'apikey'.
- Если задан DID_SOURCE_URL=https://.../avatar.png, используем его напрямую
  (без upload на /images). Иначе — как раньше, загружаем локальный avatar.png на /images.
- Экспортирована функция make_talk_video(text, image="avatar.png", out=..., voice=..., stitch=True)
  — её удобно вызывать из Telegram-бота.
- Оставлен CLI-режим: python talk_maker.py -t "Привет!"

Эндпоинты:
  POST https://api.d-id.com/images
  GET  https://api.d-id.com/tts/voices
  POST https://api.d-id.com/talks
  GET  https://api.d-id.com/talks/{id}
Авторизация: Basic (base64(user:pass)).
"""

import os
import sys
import time
import json
import base64
import pathlib
import argparse
from typing import Optional, List

# --- optional dotenv (для локального поиска .env). На Render не обязателен.
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # если модуля нет — просто не используем .env
    load_dotenv = None  # type: ignore

# HTTP-клиент — оставляем requests как в рабочей локальной версии
try:
    import requests  # type: ignore
except Exception as e:
    print("❌ Не найден модуль 'requests'. Установите 'requests' или добавьте в requirements.txt", file=sys.stderr)
    raise

DID_BASE = "https://api.d-id.com"
DEFAULT_IMAGE_URL = "https://create-images-results.d-id.com/DefaultPresenters/Emma_f/image.jpeg"

# ----------------------- helpers: logging -----------------------

def log(msg: str, *, flush=True):
    print(msg, flush=flush)

def err(msg: str):
    print(msg, file=sys.stderr, flush=True)

# ----------------------- .env загрузка (локальная папка) -----------------------

ENV_NAMES = [".env", ".env.local", ".env.i", ".env.txt", ".env.sample"]

def _debug_listdir(dirpath: pathlib.Path):
    try:
        names = [p.name for p in dirpath.iterdir()]
        log("📁 Содержимое папки со скриптом: " + ", ".join(sorted(names)))
    except Exception as e:
        err(f"⚠️  Не удалось перечислить файлы в {dirpath}: {e}")

def _find_env_candidates(script_dir: pathlib.Path) -> List[pathlib.Path]:
    """Ищем файлы, похожие на .env, именно в ЭТОЙ папке (без подъёма наверх)."""
    candidates = []
    for p in script_dir.iterdir():
        if not p.is_file():
            continue
        name = p.name.strip().replace("\u00A0", "")  # убираем невидимые пробелы
        if name in ENV_NAMES or name.startswith(".env"):
            candidates.append(p)
    return candidates

def _sanitize_line(raw: str) -> str:
    """Обрезаем кавычки, комментарии, невидимые пробелы."""
    raw = (raw or "").strip().strip('"').strip("'")
    if "#" in raw:
        raw = raw.split("#", 1)[0].strip()
    return raw.replace("\u00A0", "")

def _read_key_from_apikey_file(script_dir: pathlib.Path) -> Optional[str]:
    """Фолбэк: если есть файл 'apikey', читаем ключ из первой строки."""
    apikey_path = script_dir / "apikey"
    if not apikey_path.exists():
        return None
    try:
        with open(apikey_path, "r", encoding="utf-8") as f:
            line = f.readline()
        key = _sanitize_line(line)
        return key or None
    except Exception as e:
        err(f"⚠️  Не удалось прочитать 'apikey': {e}")
        return None

# ----------------------- Ключи (Render ENV → .env рядом → apikey) -----------------------

def get_key_from_env_first() -> Optional[str]:
    """Сначала читаем из ПЕРЕМЕННЫХ ОКРУЖЕНИЯ (Render), без .env."""
    key = _sanitize_line(os.getenv("DID_API_KEY", ""))
    if key:
        return key
    user = _sanitize_line(os.getenv("DID_API_USERNAME", ""))
    pwd  = _sanitize_line(os.getenv("DID_API_PASSWORD", ""))
    if user and pwd:
        return f"{user}:{pwd}"
    return None

def load_env_local_and_get_key() -> Optional[str]:
    """Грузим .env из папки скрипта, иначе читаем 'apikey'. Возвращаем сырой ключ или None."""
    script_dir = pathlib.Path(__file__).resolve().parent
    log("🚀 Старт talk_maker")
    log(f"🔎 Ищу .env в директории скрипта: {script_dir}")
    _debug_listdir(script_dir)

    env_candidates = _find_env_candidates(script_dir)
    if env_candidates and load_dotenv:
        env_path = sorted(env_candidates)[0]
        log(f"🧩 Нашёл env-файл: {env_path.name}")
        load_dotenv(dotenv_path=str(env_path), override=True)
        key = _sanitize_line(os.getenv("DID_API_KEY", ""))
        if not key:
            user = _sanitize_line(os.getenv("DID_API_USERNAME", ""))
            pwd  = _sanitize_line(os.getenv("DID_API_PASSWORD", ""))
            if user and pwd:
                key = f"{user}:{pwd}"
        if key:
            log(f"🔐 Ключ взят из {env_path.name} (длина: {len(key)} символов).")
            return key
        else:
            err("⚠️  Env-файл найден, но переменные DID_API_KEY или DID_API_USERNAME/DID_API_PASSWORD не заданы.")
    else:
        if env_candidates and not load_dotenv:
            err("⚠️  Найден .env, но модуль python-dotenv не установлен. Пропускаю загрузку .env.")
        else:
            err("ℹ️  Env-файл не найден в директории скрипта.")

    key_from_file = _read_key_from_apikey_file(script_dir)
    if key_from_file:
        log(f"🗝️  Ключ взят из файла 'apikey' (длина: {len(key_from_file)}).")
        return key_from_file

    return None

def get_api_key_strict() -> str:
    # 1) Render/системное окружение
    key = get_key_from_env_first()
    if key:
        return key
    # 2) Локальные файлы (как в исходной версии)
    key = load_env_local_and_get_key()
    if key:
        return key
    # 3) Ошибка
    sys.exit(
        "❌ Не найден ключ D-ID.\n"
        "Варианты:\n"
        "  • Задайте переменные окружения: DID_API_KEY=USER:PASS\n"
        "    или DID_API_USERNAME=USER и DID_API_PASSWORD=PASS\n"
        "  • Или положите .env рядом со скриптом (см. README) / файл 'apikey' с ключом."
    )

# ----------------------- Авторизация и заголовки -----------------------

def _basic_from_userpass(raw_key: str) -> str:
    """
    Если ключ 'user:pass' — кодируем в base64 (классический HTTP Basic).
    Если ключ уже выглядит как base64 — передаём как есть.
    """
    if ":" in raw_key:
        token = base64.b64encode(raw_key.encode("utf-8")).decode("ascii")
        return f"Basic {token}"
    # на случай, если пользователь положил уже base64-строку
    return f"Basic {raw_key}"

def headers_json(raw_key: str) -> dict:
    return {
        "Authorization": _basic_from_userpass(raw_key),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def headers_multipart(raw_key: str) -> dict:
    return {
        "Authorization": _basic_from_userpass(raw_key),
        "Accept": "application/json",
    }

# ----------------------- Ввод текста -----------------------

def read_text(args) -> str:
    # 1) CLI аргумент -t/--text
    if getattr(args, "text", None):
        text = args.text.strip()
        if text:
            log(f"📝 Текст получен из аргумента (-t), длина: {len(text)}")
            return text

    # 2) Файл с текстом
    if getattr(args, "text_file", None):
        p = pathlib.Path(args.text_file)
        if not p.exists():
            sys.exit(f"❌ Файл не найден: {p}")
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            sys.exit("❌ Файл пуст.")
        log(f"📝 Текст прочитан из файла ({p}), длина: {len(text)}")
        return text

    # 3) Интерактивный ввод до пустой строки
    log("Введите текст (на русском). Пустая строка — конец ввода.")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == "":
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        sys.exit("❌ Пустой ввод — нечего озвучивать.")
    log(f"📝 Текст получен из stdin, длина: {len(text)}")
    return text

# ----------------------- Работа с D-ID -----------------------

def upload_image_if_exists(raw_key: str, local_path: str) -> str:
    """
    Возвращает HTTPS-URL изображения для source_url:
    1) Если задан DID_SOURCE_URL — используем его напрямую.
    2) Если есть локальный файл — загружаем на /images и берём их ссылку.
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

def pick_ru_voice(raw_key: str) -> str:
    """
    Пытаемся опросить /tts/voices и выбрать русский.
    Если не удаётся — возвращаем 'ru-RU-DmitryNeural'.
    """
    fallback = "ru-RU-DmitryNeural"
    try:
        r = requests.get(f"{DID_BASE}/tts/voices", headers=headers_json(raw_key), timeout=30)
        if r.status_code < 300:
            payload = r.json()
            voices = payload.get("voices", payload) if isinstance(payload, dict) else payload
            candidates = []
            for v in voices or []:
                txt = json.dumps(v, ensure_ascii=False).lower()
                if "ru-" in txt or "ru_ru" in txt or '"language":"ru' in txt or '"locale":"ru' in txt:
                    vid = v.get("voice_id") or v.get("short_name") or v.get("name")
                    if vid:
                        candidates.append(vid)
            if candidates:
                log(f"🔊 Голос: {candidates[0]}")
                return candidates[0]
            log("ℹ️  Русские голоса не найдены, использую фолбэк.")
        else:
            err(f"⚠️  /tts/voices {r.status_code}: {r.text[:200]}")
    except Exception as e:
        err(f"⚠️  Не удалось получить /tts/voices: {e}")
    log(f"🔊 Голос по умолчанию: {fallback}")
    return fallback

def create_talk(raw_key: str, image_url: str, text: str, voice_id: str, stitch: bool = True) -> str:
    url = f"{DID_BASE}/talks"
    payload = {
        "source_url": image_url,
        "script": {
            "type": "text",
            "input": text,
            "provider": {"type": "microsoft", "voice_id": voice_id}
        },
        "config": {
            "stitch": stitch
            # result_format не указываем — по умолчанию mp4
        }
    }
    r = requests.post(url, headers=headers_json(raw_key), data=json.dumps(payload), timeout=60)
    if r.status_code >= 300:
        # попытаемся красиво показать детали ошибки
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

# ----------------------- Публичная функция для бота -----------------------

def make_talk_video(text: str,
                    image: str = "avatar.png",
                    out: Optional[str] = None,
                    voice: Optional[str] = None,
                    stitch: bool = True,
                    raw_key: Optional[str] = None) -> str:
    """
    Синхронно делает ролик с озвучкой текста и возвращает путь к mp4.
    Используется из Telegram-бота (импортом модуля) или из других скриптов.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой текст для озвучки")

    raw_key = raw_key or get_api_key_strict()
    img_url = upload_image_if_exists(raw_key, image)
    voice_id = voice or pick_ru_voice(raw_key) or "ru-RU-DmitryNeural"
    talk_id = create_talk(raw_key, img_url, text, voice_id, stitch=stitch)

    out_file = out or ("".join(ch for ch in text[:40] if ch.isalnum() or ch in (" ", "_", "-")).strip().replace(" ", "_") or "talk") + ".mp4"
    out_path = str(pathlib.Path(out_file).absolute())
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
    text = read_text(args)

    img_url = upload_image_if_exists(raw_key, args.image)
    voice_id = args.voice or pick_ru_voice(raw_key) or "ru-RU-DmitryNeural"
    stitch = not args.no_stitch

    talk_id = create_talk(raw_key, img_url, text, voice_id, stitch=stitch)

    out_file = args.out or ("".join(ch for ch in text[:40] if ch.isalnum() or ch in (" ", "_", "-")).strip().replace(" ", "_") or "talk") + ".mp4"
    out_path = str(pathlib.Path(out_file).absolute())
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
