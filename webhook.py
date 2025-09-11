
import os
import json
from pathlib import Path
from typing import List, Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse


try:
    from bot import bot, dp  
except Exception:

    from aiogram import Bot, Dispatcher
    _token = (os.getenv("TELEGRAM_BOT_TOKEN") or "000:FAKE").strip()
    bot = Bot(_token)
    dp = Dispatcher()

from aiogram.types import Update

ABOUT_CACHE: str = ""
FAQ_TOPICS: List[Any] = []

app = FastAPI()


def load_cache() -> None:
    """Загружаем кэш; терпимо относимся к пустым/старым форматам."""
    global ABOUT_CACHE, FAQ_TOPICS

 
    ABOUT_CACHE = ""
    about_path = Path("data/about_cache.txt")
    if about_path.exists():
        try:
            ABOUT_CACHE = about_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"[CACHE] about read error: {e}")


    FAQ_TOPICS = []
    faq_path = Path("data/faq_cache.json")
    payload: Any = {}
    if faq_path.exists():
        try:
            raw = faq_path.read_text(encoding="utf-8").strip()
            if raw:
                payload = json.loads(raw)
        except Exception as e:
            print(f"[CACHE] faq json decode error: {e}")
            payload = {}

    if isinstance(payload, list):
        payload = {"topics": payload}

    topics = payload.get("topics", []) if isinstance(payload, dict) else []
    if not isinstance(topics, list):
        topics = []
    FAQ_TOPICS = topics

    print(f"[CACHE] Loaded: about={'OK' if ABOUT_CACHE else 'MISSING'}, faq_topics={len(FAQ_TOPICS)}")


@app.get("/")
async def root():

    return {
        "ok": True,
        "service": "cvbot",
        "about_cached": bool(ABOUT_CACHE),
        "faq_topics_count": len(FAQ_TOPICS),
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/cache")
async def cache_debug():
    return {
        "about_len": len(ABOUT_CACHE or ""),
        "faq_topics_len": len(FAQ_TOPICS or []),
    }


@app.on_event("startup")
async def on_startup():
    load_cache()

    base = (os.getenv("BASE_WEBHOOK_URL") or "").strip()
    secret = (os.getenv("WEBHOOK_SECRET") or "").strip()
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

    if base and secret and token:
        url = base.rstrip("/") + f"/tg/{secret}"
        try:
            await bot.set_webhook(url)
            print(f"[WEBHOOK] set: {url}")
        except Exception as e:
            print(f"[WEBHOOK] set failed: {e}")
    else:
        print("[WEBHOOK] BASE_WEBHOOK_URL/WEBHOOK_SECRET не заданы — вебхук не устанавливается.")


@app.on_event("shutdown")
async def on_shutdown():
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass


@app.post("/tg/{secret}")
async def telegram_webhook(secret: str, request: Request):
    expected = (os.getenv("WEBHOOK_SECRET") or "").strip()
    if not expected or secret != expected:
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    try:
        update = Update.model_validate(data)  
    except Exception as e:
        print(f"[WEBHOOK] bad update: {e}")
        return JSONResponse({"ok": False, "error": "bad update"}, status_code=400)

    try:
        await dp.feed_update(bot, update)
    except Exception as e:
        print(f"[WEBHOOK] handler error: {e}")

    return {"ok": True}
