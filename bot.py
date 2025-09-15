# bot.py
# Кнопки в меню:
#  - "Обо мне" (всегда показывает фиксированный краткий текст)
#  - "CV" (полное резюме)
#  - "CVOnePage" (one-pager)
#  - "FAQ от HR" (меню тем; ответы буферизуются из резюме через локальный RAG)
#
# Остальной функционал — как прежде.

import asyncio
import os
import sys
import json
import re
from typing import List, Tuple, Dict, Optional, Any

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, FSInputFile, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from openai import OpenAI
from ddgs import DDGS
import httpx
from lxml import html as lxml_html

from config import settings

# ========= Фиксированный "Обо мне" (всегда этот текст) =========
ABOUT_FIXED = (
    "Опытный технический лидер с более чем 20-летним стажем в разработке и управлении "
    "высокотехнологичными продуктами. Специализируюсь на исследованиях и внедрении решений в "
    "области искусственного интеллекта (AI), включая крупные языковые модели (LLM: GigaChat, GPT, "
    "DeepSeek) и AI-агентов. Руководил проектами по созданию AI-ассистентов, развертыванию LLM на "
    "маломощных платформах (Raspberry Pi, МКС), внедрению нейросетей в embedded-устройства и "
    "трансформации бизнес-процессов через AI. Лауреат премии CES 2022 за инновации."
)

CTA = "Вы также можете задать вопрос на естественном языке."

# ========= Кэш =========
# (ABOUT_TEXT больше не используем для вывода «Обо мне», но оставим загрузку,
#  чтобы не ломать другие места; показывать будем именно ABOUT_FIXED)
ABOUT_TEXT: Optional[str] = None
ACTIVE_FAQ_TOPICS: List[Tuple[str, str, str]] = []  # (key, label, full)
FAQ_CACHE: Dict[str, str] = {}  # key -> reply

# ========= Эвристики =========
EMPTY_PATTERNS = [
    r"нет\s+(конкретной\s+)?информаци",
    r"в\s+(резюме|контексте|фрагмент(ах)?|предоставленных\s+фрагмент(ах)?)\s+нет",
    r"не\s+наш(лось|елось)",
    r"не\s+найдено",
    r"данных\s+нет",
    r"не\s+содержится",
    r"контекст\s+не\s+найден",
    r"релевантн(ых|ые)\s+фрагмент",
    r"контекст\s+из\s+резюме\s+не\s+найден",
]
BAD_HOSTS = ["oshibok-net.ru", "obrazovaka.ru", "gramota.ru", "rus.stackexchange.com", "stackexchange.com"]
BAD_URL_PARTS = ["login", "signin", "auth", "callback", "account", "microsoftonline", "oauth", "sso"]

def is_empty_message(text: Optional[str]) -> bool:
    if not text or not text.strip():
        return True
    t = text.lower().replace("ё", "е")
    return any(re.search(p, t) for p in EMPTY_PATTERNS)

# ========= Кэш: загрузка =========
def load_cache():
    global ABOUT_TEXT, ACTIVE_FAQ_TOPICS, FAQ_CACHE
    ABOUT_TEXT, ACTIVE_FAQ_TOPICS, FAQ_CACHE = None, [], {}

    # about (не показываем пользователю, но загрузим)
    try:
        with open("data/about_cache.txt", "r", encoding="utf-8") as f:
            ABOUT_TEXT = f.read().strip()
    except FileNotFoundError:
        ABOUT_TEXT = None

    # faq
    try:
        with open("data/faq_cache.json", "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, list):
            payload = {"topics": payload}
        for item in payload.get("topics", []):
            key, label, full, reply = item.get("key"), item.get("label"), item.get("full"), item.get("reply")
            if key and label and full and reply and not is_empty_message(reply):
                ACTIVE_FAQ_TOPICS.append((key, label, full))
                FAQ_CACHE[key] = reply
    except FileNotFoundError:
        pass

    print(f"[CACHE] Loaded: about={'OK' if ABOUT_TEXT else 'MISSING'}, faq_topics={len(ACTIVE_FAQ_TOPICS)}")

# ========= Клавиатуры =========
def main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Обо мне", callback_data="about")
    kb.button(text="CV", callback_data="resume")
    kb.button(text="CVOnePage", callback_data="resume_1p")
    kb.button(text="FAQ от HR", callback_data="faq_menu")  # всегда показываем
    kb.adjust(2, 2)
    return kb.as_markup()

def faq_kb(page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    topics = ACTIVE_FAQ_TOPICS
    total = len(topics)
    total_pages = (total - 1) // per_page + 1 if total else 1
    page = max(0, min(page, total_pages - 1))
    start, end = page * per_page, min(page * per_page + per_page, total)

    kb = InlineKeyboardBuilder()
    for key, label, _ in topics[start:end]:
        kb.button(text=label, callback_data=f"faq_t:{key}")

    if total_pages > 1:
        if page > 0: kb.button(text="« Назад", callback_data=f"faq_p:{page-1}")
        kb.button(text=f"Стр. {page+1}/{total_pages}", callback_data="faq_nop")
        if page < total_pages - 1: kb.button(text="Вперёд »", callback_data=f"faq_p:{page+1}")

    kb.button(text="Закрыть", callback_data="faq_close")
    kb.adjust(1)
    return kb.as_markup()

# ========= Универсальный веб-поиск и загрузка =========
def _web_search_impl(query: str, max_results: int = 5) -> List[dict]:
    out = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, region="ru-ru", safesearch="moderate", max_results=25):
                title = r.get("title") or r.get("source") or "Источник"
                url   = (r.get("href") or r.get("url") or r.get("link") or "").strip()
                body  = r.get("body") or ""
                if not url:
                    continue
                ul = url.lower()
                if any(bad in ul for bad in BAD_HOSTS): continue
                if any(part in ul for part in BAD_URL_PARTS): continue
                out.append({"title": title, "url": url, "snippet": body})
                if len(out) >= max_results:
                    break
    except Exception:
        pass
    return out

def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

def _web_fetch_impl(url: str, max_chars: int = 4000) -> dict:
    try:
        with httpx.Client(follow_redirects=True, timeout=12.0,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            resp = c.get(url)
            resp.raise_for_status()
            if any(part in resp.url.lower() for part in BAD_URL_PARTS):
                return {"url": url, "text": ""}
            doc = lxml_html.fromstring(resp.text)
            for bad in doc.xpath('//script|//style|//noscript'):
                bad.drop_tree()
            txt = _clean_text(doc.text_content())
            return {"url": url, "text": txt[:max_chars]}
    except Exception:
        return {"url": url, "text": ""}

# ========= RAG вспомогалки =========
def _normalize_ctx(ctx: Optional[List[Any]]) -> List[str]:
    """Нормализуем вывод rag.retrieve: поддержим и [str], и [(str, score)], и [(str, meta)]."""
    out: List[str] = []
    for item in (ctx or []):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (tuple, list)) and item:
            out.append(str(item[0]))
    return out

# ========= Assistants API (свободные вопросы) =========
async def answer_via_assistant(question: str) -> Optional[str]:
    if not settings.assistant_id:
        return None

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        thread = client.beta.threads.create()
        client.beta.threads.messages.create(thread_id=thread.id, role="user", content=question)
        run = client.beta.threads.runs.create(thread_id=thread.id, assistant_id=settings.assistant_id)

        while True:
            await asyncio.sleep(0.8)
            run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)

            if run.status in ("queued", "in_progress"):
                continue

            if run.status == "requires_action":
                outputs = []
                for call in run.required_action.submit_tool_outputs.tool_calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except Exception:
                        args = {}
                    if name == "web_search":
                        q = (args.get("query") or "").strip()
                        k = int(args.get("max_results", 5))
                        results = _web_search_impl(q, max_results=k)
                        outputs.append({"tool_call_id": call.id,
                                        "output": json.dumps(results, ensure_ascii=False)})
                    elif name == "web_fetch":
                        url = (args.get("url") or "").strip()
                        max_chars = int(args.get("max_chars", 4000))
                        result = _web_fetch_impl(url, max_chars=max_chars)
                        outputs.append({"tool_call_id": call.id,
                                        "output": json.dumps(result, ensure_ascii=False)})
                    else:
                        outputs.append({"tool_call_id": call.id,
                                        "output": json.dumps({"error": "unknown tool"})})
                client.beta.threads.runs.submit_tool_outputs(
                    thread_id=thread.id, run_id=run.id, tool_outputs=outputs
                )
                continue

            if run.status == "completed":
                msgs = client.beta.threads.messages.list(thread_id=thread.id, order="desc", limit=10)
                for m in msgs.data:
                    if m.role == "assistant":
                        parts = []
                        for c in m.content:
                            if c.type == "text":
                                parts.append(c.text.value)
                        answer = "\n".join(parts).strip()
                        return answer or None
                return None

            return None
    except Exception:
        return None

# ========= FAQ: список тем (короткие подписи и развёрнутые формулировки) =========
DEFAULT_FAQ_TOPICS = [
    {"key": "who_now",          "label": "Кто вы сейчас?",            "full": "Кто вы сейчас? Текущая должность и целевая роль (CEO / COO / CTO / CPO / Head of R&D и т.п.)."},
    {"key": "industry",         "label": "Отрасль/домен",            "full": "Отрасль и домен: FinTech, Telco, Retail, Industrial, AI/ML, GovTech и др."},
    {"key": "company_scale",    "label": "Масштаб компании",         "full": "Масштаб компании: выручка/EBITDA, стадия (startup/scale-up/корпорация), число сотрудников, география."},
    {"key": "scope",            "label": "Масштаб ответственности",  "full": "Масштаб ответственности: P&L, CAPEX/OPEX, бюджет; зона (продукт/технологии/операции/продажи)."},
    {"key": "team",             "label": "Команда",                   "full": "Команда: сколько прямых репортов, общий размер функции, уровни (VP/Director/Lead)."},
    {"key": "skills",           "label": "Ключевые компетенции",      "full": "Ключевые компетенции: стратегия, трансформация, M&A, оргструктура, выход на новые рынки и т.д."},
    {"key": "top_achievements", "label": "Топ-достижения (цифры)",    "full": "2–4 bullets: рост %, экономия $, time-to-market, NPS, SLA, ROI."},
    {"key": "location",         "label": "Локация/мобильность",       "full": "Город, готовность к релокации/командировкам; языки (уровень)."},
    {"key": "legal",            "label": "Правовой статус",           "full": "Рабочее разрешение/гражданство, non-compete/notice period (если критично)."},
    {"key": "transform",        "label": "Стратег. развороты",        "full": "Какие трансформации вы вели (digital/операционная/продуктовая) и к чему привели."},
    {"key": "from_scratch",     "label": "Построение с нуля",         "full": "Архитектура процессов, метрики, системы управления (OKR/KPI, governance)."},
    {"key": "crisis",           "label": "Кризисы/антикризис",        "full": "Что именно починили: издержки, отток клиентов, инциденты безопасности."},
    {"key": "global",           "label": "Глобальный контекст",       "full": "Международные рынки/мультикультура, распределённые команды."},
    {"key": "stakeholders",     "label": "Стейкхолдеры",              "full": "Совет директоров, акционеры, регуляторы, ключевые клиенты/партнёры."},
    {"key": "reputation",       "label": "Репутация/референсы",       "full": "Публичные кейсы, награды, публикации, патенты, борд-роль."},
    {"key": "pnl",              "label": "P&L и результат",           "full": "«Отвечал за P&L $…; EBITDA +… п.п. за 12 мес» и т.п."},
    {"key": "system",           "label": "Системность",               "full": "Как именно управляли: оргдизайн, cadence, OKR, риск-менеджмент."},
    {"key": "leadership",       "label": "Лидерство/преемств.",       "full": "Кого вырастили, текучесть, bench strength."},
    {"key": "change",           "label": "Управление изменениями",    "full": "Какие барьеры сняли, как мерили эффект."},
]

# ========= Генерация ответов на FAQ из резюме (RAG + LLM) =========
async def _answer_from_resume(full_question: str, key: str) -> str:
    """Собираем 3–5 релевантных фрагментов из локального индекса и просим модель дать краткий ответ."""
    try:
        from rag import retrieve as rag_retrieve
    except Exception:
        return ""

    try:
        # достанем контекст
        ctx_raw = rag_retrieve(full_question, top_k=5)
        chunks = _normalize_ctx(ctx_raw)
        if not chunks:
            return ""

        context_block = "\n\n".join([f"Фрагмент #{i+1}:\n{frag}" for i, frag in enumerate(chunks)])

        # специальная подсказка для «Масштаб компании» — приоритет контекста про Сбер
        sber_hint = ""
        if key == "company_scale":
            sber_hint = (
                "Если в резюме встречаются компании Сбер и Т8, для ответа о масштабе компании дай "
                "приоритет контексту о Сбере."
            )

        system = (
            "Ты отвечаешь кратко по резюме кандидата. Используй только факты из фрагментов ниже. "
            "Формат ответа: 3–6 лаконичных пунктов на русском, без воды. "
            "Если в фрагментах нет явного ответа — верни пустую строку."
        )
        user = f"{sber_hint}\nВопрос:\n{full_question}\n\nФрагменты резюме:\n\n{context_block}\n\nОтвет (кратко):"

        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.2,
        )
        text = (resp.choices[0].message.content or "").strip()
        # если модель промолчала/не нашла — считаем пустым
        if is_empty_message(text):
            return ""
        # ограничим до разумного размера для Telegram
        return text[:3500]
    except Exception:
        return ""

async def ensure_faq_ready():
    """Если кэш пуст — сгенерировать ответы на основные темы и записать data/faq_cache.json."""
    global ACTIVE_FAQ_TOPICS, FAQ_CACHE

    if ACTIVE_FAQ_TOPICS and FAQ_CACHE:
        return  # уже есть

    topics_out = []
    for item in DEFAULT_FAQ_TOPICS:
        key, label, full = item["key"], item["label"], item["full"]
        ans = await _answer_from_resume(full, key)
        if ans:  # только темы, по которым есть ответ в резюме
            topics_out.append({"key": key, "label": label, "full": full, "reply": ans})

    # если всё пусто — не считаем ошибкой, просто меню будет без тем
    ACTIVE_FAQ_TOPICS = [(t["key"], t["label"], t["full"]) for t in topics_out]
    FAQ_CACHE = {t["key"]: t["reply"] for t in topics_out}

    # запишем кэш на диск (как делает ingestion)
    try:
        os.makedirs("data", exist_ok=True)
        with open("data/faq_cache.json", "w", encoding="utf-8") as f:
            json.dump({"topics": topics_out}, f, ensure_ascii=False, indent=2)
        print(f"[FAQ] cached {len(topics_out)} topics -> data/faq_cache.json")
    except Exception as e:
        print(f"[FAQ] write cache error: {e}")

# ========= Обработчики =========
async def handle_start(message: types.Message):
    intro = "Вы общаетесь с цифровым аватаром резюме Тимура Асяева.\n\n"
    await message.answer(intro + ABOUT_FIXED, reply_markup=main_kb())

async def handle_help(message: types.Message):
    txt = ("Команды:\n"
           "/about — краткая самопрезентация\n"
           "/resume — скачать резюме\n"
           "/reindex — переиндексация резюме (только владелец)\n\n"
           f"{CTA}")
    await message.answer(txt, reply_markup=main_kb())

async def handle_about(message: types.Message):
    await message.answer(ABOUT_FIXED, reply_markup=main_kb())

async def handle_resume(message: types.Message):
    if not os.path.exists(settings.resume_path):
        await message.answer("Файл резюме не найден на сервере."); return
    await message.answer_document(
        FSInputFile(settings.resume_path, filename="CVTimurAsyaev.pdf"),
        caption="CV Тимура Асяева (PDF).\n\n" + CTA
    )

async def handle_resume_onepage(message: types.Message):
    if not os.path.exists(settings.resume_onepage_path):
        await message.answer("Файл OnePageCV не найден на сервере."); return
    await message.answer_document(
        FSInputFile(settings.resume_onepage_path, filename="CVTimurAsyaevOnePage.pdf"),
        caption="OnePageCV Тимура Асяева (PDF).\n\n" + CTA
    )

async def handle_reindex(message: types.Message):
    if message.from_user.id != settings.owner_id:
        await message.answer("Команда доступна только владельцу."); return
    await message.answer("Начинаю переиндексацию…")
    try:
        def _run():
            try:
                import subprocess
                subprocess.run([sys.executable, "ingestion.py"], check=True)
            except Exception:
                import importlib
                ingestion = importlib.import_module("ingestion")
                if hasattr(ingestion, "main"):
                    ingestion.main()
        await asyncio.get_running_loop().run_in_executor(None, _run)
        load_cache()
        # даже если ingestion ничего не создал — попробуем собрать FAQ через RAG
        await ensure_faq_ready()
        await message.answer("Переиндексация и кэш обновлены ✅", reply_markup=main_kb())
    except Exception as e:
        await message.answer(f"Ошибка переиндексации: {e}")

async def handle_free_text(message: types.Message):
    question = (message.text or "").strip()
    ans = await answer_via_assistant(question)
    if ans and not is_empty_message(ans):
        await message.answer(ans, reply_markup=main_kb()); return
    await message.answer("⚠️ Прямого ответа в файлах резюме нет. Задайте вопрос точнее или свяжитесь со мной напрямую.", reply_markup=main_kb())

# ========= Callback =========
async def cb_about(c: CallbackQuery):
    await handle_about(c.message); await c.answer()

async def cb_resume(c: CallbackQuery):
    await handle_resume(c.message); await c.answer()

async def cb_resume_onepage(c: CallbackQuery):
    await handle_resume_onepage(c.message); await c.answer()

async def cb_faq_menu(c: CallbackQuery):
    if not ACTIVE_FAQ_TOPICS:
        # если тем ещё нет — вежливо сообщим
        await c.message.answer("Сейчас нет доступных FAQ по резюме. " + CTA, reply_markup=main_kb())
        return await c.answer()
    await c.message.answer("Часто задаваемые вопросы от HR — выберите тему:", reply_markup=faq_kb(0))
    await c.answer()

async def cb_faq_page(c: CallbackQuery):
    try: page = int(c.data.split(":",1)[1])
    except Exception: page = 0
    kb = faq_kb(page)
    try:
        await c.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        await c.message.answer("Часто задаваемые вопросы от HR — выберите тему:", reply_markup=kb)
    await c.answer()

async def cb_faq_topic(c: CallbackQuery):
    key = c.data.split(":",1)[1]
    if key not in FAQ_CACHE:
        return await c.answer("По этой теме нет ответа в кэше.", show_alert=True)
    label = next((lbl for k,lbl,_ in ACTIVE_FAQ_TOPICS if k==key), "Ответ")
    await c.message.answer(f"{label}:\n\n{FAQ_CACHE[key]}", reply_markup=main_kb())
    await c.answer()

async def cb_faq_close(c: CallbackQuery):
    try: await c.message.delete()
    except Exception: pass
    await c.answer()

# ========= Startup =========
async def on_startup():
    load_cache()
    # если FAQ пуст — попробуем собрать из локального RAG прямо тут
    if not ACTIVE_FAQ_TOPICS:
        try:
            await ensure_faq_ready()
        except Exception as e:
            print(f"[STARTUP] ensure_faq_ready error: {e}")
    print(f"[STARTUP] Cache loaded; FAQ ready.")

def register_handlers(dp: Dispatcher):
    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command(commands=["help"]))
    dp.message.register(handle_about, Command(commands=["about"]))
    dp.message.register(handle_resume, Command(commands=["resume"]))
    dp.message.register(handle_resume_onepage, Command(commands=["onepage", "onepager", "onepagecv"]))
    dp.message.register(handle_reindex, Command(commands=["reindex"]))
    dp.message.register(handle_free_text, F.text)

    dp.callback_query.register(cb_about, F.data == "about")
    dp.callback_query.register(cb_resume, F.data == "resume")
    dp.callback_query.register(cb_resume_onepage, F.data == "resume_1p")
    dp.callback_query.register(cb_faq_menu, F.data == "faq_menu")
    dp.callback_query.register(cb_faq_close, F.data == "faq_close")
    dp.callback_query.register(cb_faq_page, lambda c: c.data and c.data.startswith("faq_p:"))
    dp.callback_query.register(cb_faq_topic, lambda c: c.data and c.data.startswith("faq_t:"))

# Локальный запуск (если нужен)
async def _main():
    if not settings.telegram_token:
        raise RuntimeError("Проверьте TELEGRAM_BOT_TOKEN в .env")
    bot = Bot(token=settings.telegram_token)
    dp = Dispatcher()
    dp.startup.register(on_startup)
    register_handlers(dp)
    print("Бот запущен (long polling). Ctrl+C для выхода.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(_main())
