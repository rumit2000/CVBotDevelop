#!/usr/bin/env python3
import os
import sys
import asyncio
import logging
import subprocess

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from webhook import dp  # используем те же роутеры/хэндлеры

# --- опциональный HTTP для Render (если это Web Service) ---
from fastapi import FastAPI
import uvicorn

_health_app = FastAPI()

@_health_app.get("/")
def root():
    return {"status": "ok", "service": "polling-worker"}

@_health_app.get("/healthz")
def healthz():
    return {"ok": True}


async def run_health_server_if_needed():
    """
    Если сервис запущен как Web Service и Render ждёт открытый порт,
    поднимем небольшой HTTP-сервер на $PORT.
    Если $PORT нет (Background Worker) — ничего не делаем.
    """
    port = os.getenv("PORT")
    if not port:
        logging.info("[POLL] No $PORT -> health server is not started (worker mode).")
        return  # ничего не поднимаем в worker-режиме
    port = int(port)
    logging.info(f"[POLL] Starting health server on :{port}")
    config = uvicorn.Config(_health_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
# ------------------------------------------------------------


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


async def run_polling():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))

    # Если где-то был выставлен вебхук, удалим его, иначе polling не получит апВот обновлённый файл `polling_worker.py` с исправлением для новой версии aiogram:

```python
#!/usr/bin/env python3
import os
import sys
import asyncio
import logging
import subprocess

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from webhook import dp  # используем те же роутеры/хэндлеры

# --- опциональный HTTP для Render (если это Web Service) ---
from fastapi import FastAPI
import uvicorn

_health_app = FastAPI()

@_health_app.get("/")
def root():
    return {"status": "ok", "service": "polling-worker"}

@_health_app.get("/healthz")
def healthz():
    return {"ok": True}


async def run_health_server_if_needed():
    """
    Если сервис запущен как Web Service и Render ждёт открытый порт,
    поднимем небольшой HTTP-сервер на $PORT.
    Если $PORT нет (Background Worker) — ничего не делаем.
    """
    port = os.getenv("PORT")
    if not port:
        logging.info("[POLL] No $PORT -> health server is not started (worker mode).")
        return  # ничего не поднимаем в worker-режиме
    port = int(port)
    logging.info(f"[POLL] Starting health server on :{port}")
    config = uvicorn.Config(_health_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
# ------------------------------------------------------------


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


async def run_polling():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))

    # Если где-то был выставлен вебхук, удалим его, иначе polling не получит апдейты
    drop = os.getenv("DROP_UPDATES_ON_START", "true").lower() in ("1", "true", "yes", "y")
    try:
        await bot.delete_webhook(drop_pending_updates=drop)
        logging.info("[POLL] delete_webhook ok (drop=%s)", drop)
    except Exception as e:
        logging.warning("[POLL] delete_webhook failed: %s", e)

    logging.info("[POLL] Starting dp.start_polling() ...")
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
    )


async def main():
    # Поднимем health-сервер (если требуется порт) параллельно с polling
    health_task = asyncio.create_task(run_health_server_if_needed())
    polling_task = asyncio.create_task(run_polling())

    # ждём завершения любой из задач
    done, pending = await asyncio.wait(
        {health_task, polling_task}, return_when=asyncio.FIRST_EXCEPTION
    )

    # если одна из задач упала — отменим вторую
    for t in pending:
        t.cancel()
    # пробросим исключение, если было
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