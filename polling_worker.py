
# polling_worker.py — long-polling воркер + мини health для Render.
# Надёжно прогревает кэш (ingestion) при старте.

import os
import sys
import asyncio
import logging
import signal
import subprocess
from contextlib import suppress

from fastapi import FastAPI, Response
import uvicorn

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from config import settings
from bot import register_handlers, on_startup  # наши хэндлеры и загрузчик кэша

log = logging.getLogger("polling_worker")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# ---- мини health-сервер (опционально, если Render назначает $PORT) ----
app = FastAPI()

@app.get("/")
def health_get():
    return {"ok": True, "service": "polling_worker"}

@app.head("/")
def health_head():
    return Response(status_code=200)


async def _run_ingestion_blocking():
    """Запускаем ingestion надёжно: сперва как скрипт, затем — через импорт (fallback)."""
    try:
        # Надёжный путь: запуск отдельным процессом, чтобы не тащить зависимости в память воркера.
        subprocess.run([sys.executable, "ingestion.py"], check=True)
        log.info("[polling_worker] ingestion.py finished OK (subprocess).")
        return
    except Exception as e:
        log.warning(f"[polling_worker] ingestion.py via subprocess failed: {e}")

    # Fallback: импортом
    try:
        import importlib
        ingestion = importlib.import_module("ingestion")
        if hasattr(ingestion, "main"):
            ingestion.main()
            log.info("[polling_worker] ingestion.main() finished OK (import).")
        else:
            raise AttributeError("module 'ingestion' has no attribute 'main'")
    except Exception as e:
        log.error(f"[polling_worker] ingestion failed: {e}")


async def ensure_cache():
    """Если кэша нет — прогреваем."""
    needs_about = not os.path.exists("data/about_cache.txt")
    needs_faq = not os.path.exists("data/faq_cache.json")
    if needs_about or needs_faq:
        log.info("[polling_worker] No cache detected. Running ingestion...")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: asyncio.run(_run_ingestion_blocking()))
    else:
        log.info("[polling_worker] Cache found — skip ingestion.")


async def run_polling():
    if not settings.telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан.")

    # 1) Прогреваем кэш до старта бота
    await ensure_cache()

    # 2) Запускаем health-сервер, если есть порт (на background worker это не обязательно)
    port = int(os.getenv("PORT", "0") or 0)
    server = None
    if port:
        config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(config=config)
        asyncio.create_task(server.serve())
        log.info(f"[polling_worker] Starting health server on :{port}")

    # 3) Бот
    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    dp.startup.register(on_startup)  # внутри он ещё раз проверит/подхватит кэш при необходимости
    register_handlers(dp)

    # Удаляем вебхук на всякий случай (мы на long-polling)
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("[polling_worker] delete_webhook ok (drop=True)")

    allowed = ["message", "callback_query"]
    try:
        log.info("[polling_worker] [POLL] Starting dp.start_polling() ...")
        await dp.start_polling(bot, allowed_updates=allowed)
    finally:
        log.info("[polling_worker] Polling stopped")


async def main():
    # Корректно ловим SIGTERM/SIGINT
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _stop(*_):
        if not stop.done():
            stop.set_result(True)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    poll_task = asyncio.create_task(run_polling())
    await asyncio.wait([poll_task, stop], return_when=asyncio.FIRST_COMPLETED)

    if not poll_task.done():
        poll_task.cancel()
        with suppress(asyncio.CancelledError):
            await poll_task


if __name__ == "__main__":
    asyncio.run(main())
