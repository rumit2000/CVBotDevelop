#!/usr/bin/env python3
import os
import sys
import asyncio
import logging
import subprocess

from aiogram import Bot

# Берём тот же Dispatcher и хэндлеры, что используются веб-сервисом.
# В твоём проекте dp объявлен в webhook.py.
from webhook import dp


def _run_ingestion_if_needed() -> None:
    """
    Прогоним ingestion один раз, если кешей ещё нет — чтобы воркер
    работал автономно и не зависел от порядка старта сервисов.
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


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(token=token)

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


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    _run_ingestion_if_needed()
    asyncio.run(main())