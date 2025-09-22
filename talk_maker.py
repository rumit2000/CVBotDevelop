# лордлордлотдлот

import os
import asyncio
import logging
from contextlib import suppress

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError

from config import settings

# ----------------- логирование -----------------
logger = logging.getLogger("polling_worker")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# ----------------- health server -----------------
app = FastAPI()

@app.get("/")
async def root():
    return {"ok": True, "service": "tg-polling-worker"}

@app.head("/")
async def root_head():
    return Response(status_code=200)

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

@app.head("/healthz")
async def healthz_head():
    return Response(status_code=200)

# --- публичная раздача аватара для D-ID ---
@app.get("/avatar.png")
async def avatar_png():
    path = "avatar.png"
    if not os.path.exists(path):
        return JSONResponse({"error": "avatar.png not found"}, status_code=404)
    return FileResponse(path, media_type="image/png")

async def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

# ----------------- polling -----------------
from bot import register_handlers  # наши хэндлеры/маршруты aiogram

async def delete_webhook_safely(bot: Bot):
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("[polling_worker] delete_webhook ok (drop=True)")

async def run_polling():
    token = settings.telegram_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    register_handlers(dp)

    # на всякий случай гасим webhook
    await delete_webhook_safely(bot)

    allowed = ["message", "callback_query"]
    logger.info("[polling_worker] [POLL] Starting dp.start_polling() ...")
    try:
        # В aiogram 3.x можно задать таймаут long polling
        await dp.start_polling(
            bot,
            allowed_updates=allowed,
            polling_timeout=30,        # держим соединение подольше
            close_bot_session=True,
        )
    finally:
        # чистое завершение
        with suppress(Exception):
            await dp.storage.close()
        with suppress(Exception):
            await bot.session.close()

async def ensure_cache():
    """
    Если нет кэшей — запустим ingestion один раз перед поллингом.
    """
    about = os.path.exists("data/about_cache.txt")
    faq = os.path.exists("data/faq_cache.json")
    if about and faq:
        logger.info("[polling_worker] Кэш найден — ingestion пропущен")
        return
    logger.info("[polling_worker] [POLL] No cache detected. Running ingestion...")
    # импорт локальный, чтобы не тянуть при каждом импорте модуля
    import ingestion
    try:
        await asyncio.get_running_loop().run_in_executor(None, ingestion.main)
    except Exception as e:
        logger.warning("[polling_worker] ingestion error: %s", e)

async def main():
    # Параллельно поднимем health-сервер
    health_task = asyncio.create_task(start_health_server())

    # Готовим кэш
    await ensure_cache()

    # Бесконечный цикл поллинга с авто-перезапуском
    backoff = 1.0
    while True:
        try:
            await run_polling()
            # если вышли «нормально» (например SIGTERM), то просто break
            break
        except TelegramNetworkError as e:
            logger.error("Polling network error: %s", e)
        except Exception as e:
            logger.error("Polling crashed: %s", e, exc_info=True)

        # Бэкофф, но ограничим
        await asyncio.sleep(backoff)
        backoff = min(backoff * 1.5, 15)

    # корректно завершим health-сервер
    with suppress(asyncio.CancelledError):
        health_task.cancel()
        await health_task

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[polling_worker] Stopped by KeyboardInterrupt")
