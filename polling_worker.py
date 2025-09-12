# polling_worker.py
# Воркер для Render: health-сервер на $PORT + лонг-поллинг Telegram (без вебхука).
# Подключает боевые хэндлеры из bot.py. Токен берётся из TELEGRAM_BOT_TOKEN.

import os
import sys
import asyncio
import logging
import subprocess
from pathlib import Path

from fastapi import FastAPI
import uvicorn

from aiogram import Dispatcher, Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError

from bot import register_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("[POLL]")

DATA_DIR = Path("data")
ABOUT_FILE = DATA_DIR / "about_cache.txt"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    print("[POLL] ERROR: TELEGRAM_BOT_TOKEN не задан")
    sys.exit(1)

def run_ingestion_if_needed() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not ABOUT_FILE.exists():
        log.info("[POLL] No cache detected. Running ingestion...")
        try:
            ret = subprocess.run([sys.executable, "ingestion.py"], check=False)
            if ret.returncode != 0:
                log.warning("[POLL] ingestion.py exited with non-zero code, continue anyway")
        except Exception as e:
            log.warning(f"[POLL] ingestion failed: {e}")

def start_health_server() -> asyncio.Task:
    app = FastAPI()

    @app.get("/")
    def root():
        return {"ok": True, "service": "polling-worker"}

    port = int(os.getenv("PORT", "10000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    log.info(f"[POLL] Starting health server on :{port}")
    return asyncio.create_task(server.serve())

async def run_polling() -> None:
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    register_handlers(dp)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("[POLL] delete_webhook ok (drop=True)")
    except Exception as e:
        log.warning(f"[POLL] delete_webhook failed: {e}")

    backoff = 1.0
    tries = 0
    while True:
        try:
            log.info("[POLL] Starting dp.start_polling() ...")
            await dp.start_polling(bot)
            break
        except TelegramConflictError as e:
            log.error(f"Failed to fetch updates - {e.__class__.__name__}: {e}")
            log.warning(f"Sleep for {backoff:.6f} seconds and try again... (tryings = {tries}, bot id = {bot.id})")
            await asyncio.sleep(backoff)
            tries += 1
            backoff = min(backoff * 1.3, 5.0)

async def main() -> None:
    run_ingestion_if_needed()
    health_task = start_health_server()
    try:
        await run_polling()
    finally:
        try:
            health_task.cancel()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.warning("Interrupted by user")
