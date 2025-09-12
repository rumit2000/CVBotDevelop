# polling_worker.py
# Long-polling воркер + мини health-сервер для Render

import asyncio
import logging
import os
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramConflictError
from fastapi import FastAPI
import uvicorn

from config import settings
from bot import register_handlers, load_cache  # регистрация хэндлеров и кэш

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("polling_worker")

# ---------- Health-сервер ----------
app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "service": "cvbot", "mode": "polling"}

@app.get("/healthz")
def health():
    return {"status": "ok"}

async def run_health_server():
    port = int(os.getenv("PORT", "10000") or "10000")
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

# ---------- Запуск polling ----------
async def delete_webhook_safe(token: str):
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)
    await bot.session.close()

async def run_polling():
    token = (settings.telegram_token or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

    # сброс вебхука и дропа подвисших апдейтов
    await delete_webhook_safe(token)

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    register_handlers(dp)
    load_cache()  # подстраховка

    allowed = ["message", "callback_query"]

    delay = 1.0
    while True:
        try:
            log.info("[POLL] Starting dp.start_polling() ...")
            await dp.start_polling(bot, allowed_updates=allowed)
        except TelegramConflictError as e:
            log.error("Failed to fetch updates - %s", e)
            log.warning("Sleep for %.6f seconds and try again...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 1.3, 5.5)
            continue
        except Exception as e:
            log.exception("Polling crashed: %s", e)
            await asyncio.sleep(2.0)
            continue
        finally:
            log.info("Polling stopped")
            with suppress(Exception):
                await bot.session.close()

async def main():
    need_about = not os.path.exists("data/about_cache.txt")
    need_faq = not os.path.exists("data/faq_cache.json")
    if need_about or need_faq:
        log.info("[POLL] No cache detected. Running ingestion...")
        with suppress(Exception):
            import ingestion
            await asyncio.get_running_loop().run_in_executor(None, ingestion.main)

    poll = asyncio.create_task(run_polling())
    health = asyncio.create_task(run_health_server())
    done, pending = await asyncio.wait({poll, health}, return_when=asyncio.FIRST_EXCEPTION)
    for t in pending:
        t.cancel()
    for t in done:
        with suppress(Exception):
            t.result()

if __name__ == "__main__":
    asyncio.run(main())
