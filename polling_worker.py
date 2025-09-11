#!/usr/bin/env python3
"""
Polling worker for Telegram.

- Токен берём из переменной окружения TELEGRAM_BOT_TOKEN.
- Перед стартом удаляем вебхук (чтобы не конфликтовал с polling).
- Если задан $PORT (Render Web Service) — поднимаем / и /healthz, чтобы Render видел открытый порт.
- Однократно гоняем ingestion, если кешей ещё нет.
- Подключаем базовые хэндлеры: /start и ответ на любой текст (для диагностики).
"""

import os
import sys
import asyncio
import logging
import subprocess

from aiogram import Bot, types, Router, F
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties

# Используем тот же Dispatcher, что и в webhook.py (если там что-то подключено)
from webhook import dp  # noqa: F401

# ---------- Мини HTTP-сервер для Render (нужен только если это Web Service с $PORT) ----------
from fastapi import FastAPI, Response
import uvicorn

_health_app = FastAPI()

@_health_app.get("/")
def root():
    return {"status": "ok", "service": "polling-worker"}

@_health_app.head("/")
def head_root():
    # Render часто шлёт HEAD /
    return Response(status_code=200)

@_health_app.get("/healthz")
def healthz():
    return {"ok": True}

async def run_health_server_if_needed() -> None:
    """
    Если $PORT задан (Render Web Service) — поднимем маленький HTTP-сервер,
    чтобы Render видел открытый порт и не перезапускал процесс.
    Если $PORT нет (Background Worker) — ничего не делаем.
    """
    port = os.getenv("PORT")
    if not port:
        logging.info("[POLL] No $PORT -> health server is not started (worker mode).")
        return
    port = int(port)
    logging.info(f"[POLL] Starting health server on :{port}")
    config = uvicorn.Config(_health_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
# ---------------------------------------------------------------------------------------------


def _run_ingestion_if_needed() -> None:
    """
    Прогоним ingestion один раз, если кешей ещё нет.
    """
    about_ok = os.path.exists("data/about_cache.txt")
    faq_ok = os.path.exists("data/faq_cache.json")
    if about_ok and faq_ok:
        logging.info("[POLL] Cache detected. Skipping ingestion.")
        return
    logging.info("[POLL] No cache detected. Running ingestion...")
    try:
        subprocess.run([sys.executable, "ingestion.py"], check=False)
    except Exception as e:
        logging.warning("[POLL] ingestion failed: %s", e)


# ----------------- БАЗОВЫЕ ХЭНДЛЕРЫ (диагностические, чтобы бот точно отвечал) ----------------
basic_router = Router()

@basic_router.message(CommandStart())
async def on_start(message: types.Message):
    await message.answer(
        "Привет! Я на связи 👋\n"
        "Напиши мне любой вопрос — отвечу. Если это тест, просто пришли текст."
    )

@basic_router.message(F.text)
async def on_any_text(message: types.Message):
    # Простой ответ-эхо, чтобы сразу увидеть, что обработчик работает
    await message.answer(f"Принял: «{message.text}». Сейчас всё работает ✅")
# ------------------------------------------------------------------------------------------------


async def run_polling() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    # aiogram >= 3.7: parse_mode задаём через DefaultBotProperties
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))

    # ВАЖНО: удаляем вебхук, иначе Telegram не будет слать сообщения в long polling
    drop = os.getenv("DROP_UPDATES_ON_START", "true").lower() in ("1", "true", "yes", "y")
    try:
        await bot.delete_webhook(drop_pending_updates=drop)
        logging.info("[POLL] delete_webhook ok (drop=%s)", drop)
    except Exception as e:
        logging.warning("[POLL] delete_webhook failed: %s", e)

    # Подключаем базовые хэндлеры (и любые другие, которые уже подключены в webhook.py к dp)
    try:
        from webhook import dp as _dp  # тот же объект, что импортирован выше
        _dp.include_router(basic_router)
    except Exception as e:
        logging.warning("[POLL] include_router(basic_router) failed: %s", e)

    logging.info("[POLL] Starting dp.start_polling() ...")
    # Не ограничиваем allowed_updates — пусть приходят все типы
    from webhook import dp as _dp
    await _dp.start_polling(bot)


async def main() -> None:
    # Параллельно поднимем health-сервер (если нужен) и запустим polling
    health_task = asyncio.create_task(run_health_server_if_needed())
    polling_task = asyncio.create_task(run_polling())

    done, pending = await asyncio.wait(
        {health_task, polling_task},
        return_when=asyncio.FIRST_EXCEPTION,
    )

    for t in pending:
        t.cancel()
    for t in done:
        exc = t.exception()
        if exc:
            raise exc


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    _run_ingestion_if_needed()
    asyncio.run(main())
