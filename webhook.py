# webhook.py
# FastAPI + aiogram webhook:
# - стартует веб-сервис для приема Telegram webhook
# - на старте ставит setWebhook на BASE_WEBHOOK_URL/webhook/WEBHOOK_SECRET
# - кнопки/FAQ/кэш (About/FAQ) читаются из data/*
# - свободные вопросы: Assistants API (File Search) + tools web_search/web_fetch
# - если ассистент не справился — общий веб-fallback и контакты из .env

import asyncio
import os
import json
import re
from typing import List, Tuple, Dict, Optional, Set

from fastapi import FastAPI, Request, Header, HTTPException
from aiogram import Bot, Dispatcher, F
from aiogram.types import Update, Message, CallbackQuery, InlineKeyboardMarkup, FSInputFile
from aiogram.filters import Command, CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

from openai import OpenAI
from ddgs import DDGS
import httpx
from lxml import html as lxml_html

from config import settings

app = FastAPI()

bot = Bot(token=settings.telegram_token)
dp = Dispatcher()

# ========= Глобальные кэши =========
ABOUT_TEXT: Optional[str] = None
ACTIVE_FAQ_TOPICS: List[Tuple[str, str, str]] = []
FAQ_CACHE: Dict[str, str] = {}

CTA = "Вы также можете задать вопрос на естественном языке."

# ========= Эвристики / фильтры =========
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
BAD_HOSTS = [
    "oshibok-net.ru", "obrazovaka.ru", "gramota.ru",
    "rus.stackexchange.com", "stackexchange.com"
]
BAD_URL_PARTS = ["login", "signin", "auth", "callback", "account", "microsoftonline", "oauth", "sso"]
EMPLOYEE_KEYWORDS = [
    "employees","employee count","headcount","staff","team size",
    "сотрудник","сотрудников","численность","штат","размер компании"
]

def is_empty_message(text: Optional[str]) -> bool:
    if not text or not text.strip():
        return True
    t = text.lower().replace("ё", "е")
    return any(re.search(p, t) for p in EMPTY_PATTERNS)

# ========= Кэш About/FAQ =========
def load_cache():
    global ABOUT_TEXT, ACTIVE_FAQ_TOPICS, FAQ_CACHE
    ABOUT_TEXT, ACTIVE_FAQ_TOPICS, FAQ_CACHE = None, [], {}

    try:
        with open("data/about_cache.txt", "r", encoding="utf-8") as f:
            ABOUT_TEXT = f.read().strip()
    except FileNotFoundError:
        ABOUT_TEXT = None

    try:
        with open("data/faq_cache.json", "r", encoding="utf-8") as f:
            payload = json.load(f)
        for item in payload.get("topics", []):
            key, label, full, reply = item.get("key"), item.get("label"), item.get("full"), item.get("reply")
            if key and label and full and reply and not is_empty_message(reply):
                ACTIVE_FAQ_TOPICS.append((key, label, full))
                FAQ_CACHE[key] = reply
    except FileNotFoundError:
        pass

    print(f"[CACHE] Loaded: about={'OK' if ABOUT_TEXT else 'MISSING'}, faq_topics={len(ACTIVE_FAQ_TOPICS)}")

# ========= Кнопочные клавиатуры =========
def main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Обо мне", callback_data="about")
    kb.button(text="Скачать резюме", callback_data="resume")
    if ACTIVE_FAQ_TOPICS:
        kb.button(text="FAQ от HR", callback_data="faq_menu")
    if settings.linkedin_url:
        kb.button(text="LinkedIn", url=settings.linkedin_url)
    else:
        kb.button(text="LinkedIn", callback_data="linkedin")
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

# ========= Релевантность к собеседованию =========
HR_PATTERNS = [
    r"\bкомпан[ияие]\b", r"\bгде\s+сейчас\s+работа", r"\bтекущ\w+\s+(роль|должн|позици)",
    r"\bразмер\w*\b", r"\bштат\w*\b", r"\bсотрудник\w*\b", r"\bheadcount\b",
    r"\bвыручк\w*\b", r"\bоборот\w*\b", r"\bebitda\b", r"p&l", r"\bбюджет\w*\b", r"\bbudget\b",
    r"\bкоманда\b|\bteam\b|\bрепорт\w*|\bподчиненн\w*",
    r"\bопыт\w*\b|\bresponsibilit|\bобязанност\w*|\bответственност\w*",
    r"\bиндустри\w*|\bдомен\w*|\bindustr\w*|\bdomain\b|\bfintech\b|\btelco\b|\bretail\b|\bindustrial\b|\bai/ml\b|\bgovtech\b",
    r"\bрелокац\w*|\bкомандировк\w*|\bмобильност\w*|\brelocat\w*|\bvisa\b|\bwork\s+permit\b|\bгражданств\w*",
    r"\bзарплат\w*|\bcompensation\b|\bbenefit\w*|\bequity\b|\bопцион\w*",
    r"\bokrs?\b|\bkpis?\b|\borg\s*design\b|\bgovernance\b",
]
def rule_based_interview_relevance(q: str) -> bool:
    qn = q.lower()
    return any(re.search(p, qn) for p in HR_PATTERNS)

async def classify_interview_relevance(question: str) -> bool:
    client = OpenAI(api_key=settings.openai_api_key)
    messages = [
        {"role": "system", "content": (
            "You are a strict classifier. Output only 'yes' or 'no'. "
            "Say 'yes' if the user's question is related to a job interview context: "
            "professional experience, roles, responsibilities, skills, achievements, compensation/relocation, "
            "work authorization, management, org design, strategy, product/tech/operations, leadership, "
            "stakeholders, budgets/P&L, results, industry/domain, company size, team size."
        )},
        {"role": "user", "content": question}
    ]
    try:
        resp = client.chat.completions.create(model=settings.openai_model, messages=messages, temperature=0)
        return resp.choices[0].message.content.strip().lower().startswith("y")
    except Exception:
        return True

async def is_question_relevant(question: str) -> bool:
    return rule_based_interview_relevance(question) or await classify_interview_relevance(question)

# ========= Вспомогательные: web search / fetch =========
def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

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
                if any(bad in ul for bad in BAD_HOSTS):
                    continue
                if any(part in ul for part in BAD_URL_PARTS):
                    continue
                out.append({"title": title, "url": url, "snippet": body})
                if len(out) >= max_results:
                    break
    except Exception:
        pass
    return out

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

# ========= Вытянуть текущую компанию из локального индекса (для headcount) =========
async def extract_current_company_from_local_index() -> Optional[str]:
    from rag import retrieve as _retrieve
    ctx = _retrieve("Текущее место работы: укажи название компании и должность (если есть).")
    if not ctx:
        return None
    context_block = "\n\n".join([f"Фрагмент #{i+1}:\n{frag}" for i, (frag, _) in enumerate(ctx)])
    system = (
        "Ты — экстрактор фактов из резюме. Верни строго JSON: "
        '{"company": "..."} без пояснений. Если не уверен — используй null.'
    )
    user = f"Фрагменты резюме:\n\n{context_block}\n\nВерни только JSON."
    client = OpenAI(api_key=settings.openai_api_key)
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=0
        )
        m = re.search(r"\{.*\}", resp.choices[0].message.content.strip(), flags=re.S)
        data = json.loads(m.group(0)) if m else {}
        comp = (data.get("company") or "").strip().strip('"“”«»')
        return comp or None
    except Exception:
        return None

# ========= Assistants API: универсальный раннер с обработкой tools =========
async def answer_via_assistant(question: str) -> Optional[str]:
    """
    Ассистент (File Search + tools). Если ассистент вызывает web_search без компании в headcount-сценариях —
    подставим компанию, извлечённую из резюме.
    """
    if not settings.assistant_id:
        return None

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        thread = client.beta.threads.create()
        client.beta.threads.messages.create(thread_id=thread.id, role="user", content=question)
        run = client.beta.threads.runs.create(thread_id=thread.id, assistant_id=settings.assistant_id)

        current_company = await extract_current_company_from_local_index()

        while True:
            await asyncio.sleep(0.8)
            run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)

            if run.status in ("queued", "in_progress"):
                continue

            if run.status == "requires_action":
                tool_calls = run.required_action.submit_tool_outputs.tool_calls
                outputs = []
                for call in tool_calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except Exception:
                        args = {}

                    if name == "web_search":
                        q = (args.get("query") or "").strip()
                        k = int(args.get("max_results", 5))
                        headcount_trigger = any(s in q.lower() for s in [
                            "headcount","employee","employees","численност","штат","размер компан","сколько человек"
                        ])
                        if headcount_trigger and current_company and current_company.lower() not in q.lower():
                            q = f'{current_company} employees headcount численность сотрудников штат размер компании'
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

                run = client.beta.threads.runs.submit_tool_outputs(
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

            # cancelled/failed/expired
            return None
    except Exception:
        return None

# ========= Handlers: сообщения =========
async def handle_start(message: Message):
    if ABOUT_TEXT:
        intro = "Вы общаетесь с цифровым аватаром резюме Тимура Асяева.\n\n"
        await message.answer(intro + ABOUT_TEXT, reply_markup=main_kb())
    else:
        await message.answer(
            "Кэш ещё не создан. Выполните /reindex (только владелец) или запустите `python3 ingestion.py`.",
            reply_markup=main_kb()
        )

async def handle_help(message: Message):
    txt = (
        "Команды:\n"
        "/about — краткая самопрезентация\n"
        "/resume — скачать резюме\n"
        "/linkedin — ссылка на LinkedIn\n"
        "/reindex — переиндексация резюме (только владелец)\n\n"
        f"{CTA}"
    )
    await message.answer(txt, reply_markup=main_kb())

async def handle_about(message: Message):
    if ABOUT_TEXT:
        await message.answer(ABOUT_TEXT, reply_markup=main_kb())
    else:
        await message.answer("Кэш ещё не создан. Выполните /reindex.", reply_markup=main_kb())

async def handle_resume(message: Message):
    if not os.path.exists(settings.resume_path):
        await message.answer("Файл резюме не найден на сервере.")
        return
    await message.answer_document(
        FSInputFile(settings.resume_path, filename="CVTimurAsyaev.pdf"),
        caption="CV Тимура Асяева (PDF).\n\n" + CTA
    )

async def handle_linkedin(message: Message):
    if settings.linkedin_url:
        await message.answer(f"Мой LinkedIn: {settings.linkedin_url}\n\n{CTA}", reply_markup=main_kb())
    else:
        await message.answer("Ссылка на LinkedIn не настроена.\n\n" + CTA, reply_markup=main_kb())

async def handle_reindex(message: Message):
    if message.from_user.id != settings.owner_id:
        await message.answer("Команда доступна только владельцу.")
        return
    await message.answer("Начинаю переиндексацию…")
    try:
        import ingestion
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, ingestion.main)
        load_cache()
        await message.answer("Переиндексация и кэш обновлены ✅", reply_markup=main_kb())
    except Exception as e:
        await message.answer(f"Ошибка переиндексации: {e}")

async def handle_text(message: Message):
    question = message.text.strip()

    # 1) фильтр релевантности
    if not await is_question_relevant(question):
        msg = (
            "Этот вопрос не относится к тематике собеседования и моему резюме. "
            f"По организационным или личным вопросам лучше связаться со мной: {settings.contact_info}"
        )
        await message.answer(msg, reply_markup=main_kb())
        return

    # 2) ассистент (File Search → tools web_search/web_fetch)
    ans = await answer_via_assistant(question)
    if ans and not is_empty_message(ans):
        await message.answer(ans, reply_markup=main_kb())
        return

    # 3) общий веб-fallback (на всякий случай)
    links = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(question, region="ru-ru", safesearch="moderate", max_results=10):
                title = r.get("title") or r.get("source") or "Источник"
                url   = r.get("href")  or r.get("url")    or r.get("link")
                if not url: continue
                ul = (url or "").lower()
                if any(bad in ul for bad in BAD_HOSTS): continue
                if any(part in ul for part in BAD_URL_PARTS): continue
                links.append((title, url))
                if len(links) >= 3: break
    except Exception:
        pass

    if links:
        bullets = "\n".join([f"- {t} ({u})" for t, u in links])
        txt = ("⚠️ В файлах резюме прямого ответа нет.\n\n"
               f"Нашёл полезные источники:\n{bullets}\n\n"
               f"Если потребуется уточнить детали, свяжитесь со мной: {settings.contact_info}")
    else:
        txt = ("⚠️ В файлах резюме прямого ответа нет, и в открытых источниках не нашлось надёжных данных.\n\n"
               f"Свяжитесь со мной напрямую: {settings.contact_info}")
    await message.answer(txt, reply_markup=main_kb())

# ========= Handlers: callbacks =========
async def handle_callbacks(callback: CallbackQuery):
    data = callback.data or ""
    if data == "about":
        await handle_about(callback.message);  return await callback.answer()
    if data == "resume":
        await handle_resume(callback.message); return await callback.answer()
    if data == "linkedin":
        await handle_linkedin(callback.message); return await callback.answer()

    if data == "faq_menu":
        if not ACTIVE_FAQ_TOPICS:
            await callback.message.answer("Сейчас нет доступных FAQ по резюме. " + CTA, reply_markup=main_kb())
            return await callback.answer()
        await callback.message.answer(
            "Часто задаваемые вопросы от HR — выберите тему (или задайте вопрос текстом):",
            reply_markup=faq_kb(0)
        ); return await callback.answer()

    if data == "faq_close" or data == "faq_nop":
        try: await callback.message.delete()
        except Exception: pass
        return await callback.answer()

    if data.startswith("faq_p:"):
        try: page = int(data.split(":", 1)[1])
        except Exception: page = 0
        try:
            await callback.message.edit_reply_markup(reply_markup=faq_kb(page))
        except Exception:
            await callback.message.answer(
                "Часто задаваемые вопросы от HR — выберите тему (или задайте вопрос текстом):",
                reply_markup=faq_kb(page)
            )
        return await callback.answer()

    if data.startswith("faq_t:"):
        key = data.split(":", 1)[1]
        if key not in FAQ_CACHE:
            await callback.answer("По этой теме нет ответа в кэше.", show_alert=True); return
        label = next((lbl for k, lbl, _ in ACTIVE_FAQ_TOPICS if k == key), "Ответ")
        await callback.message.answer(f"{label}:\n\n{FAQ_CACHE[key]}", reply_markup=main_kb())
        return await callback.answer()

# ========= Webhook lifecycle =========
@app.on_event("startup")
async def on_startup():
    load_cache()
    if settings.base_webhook_url and settings.webhook_secret:
        url = settings.base_webhook_url.rstrip("/") + f"/webhook/{settings.webhook_secret}"
        await bot.set_webhook(url, secret_token=settings.webhook_secret)
        print(f"[WEBHOOK] set to: {url}")
    else:
        print("[WEBHOOK] BASE_WEBHOOK_URL/WEBHOOK_SECRET не заданы — вебхук не устанавливается.")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(None)
):
    # защита по секрету в URL и заголовке
    if secret != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid secret in URL")
    if x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid secret header")

    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}
