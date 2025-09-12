# bot.py
# Телеграм-бот (боевой функционал):
# - кнопки (Обо мне / Скачать резюме / FAQ / LinkedIn)
# - кэш ответов (About/FAQ)
# - свободные вопросы: Assistants API (File Search) + универсальные tools web_search/web_fetch
# - если ассистент не справился — общий веб-fallback и/или контакты
# ВАЖНО: запуск/токены не трогаем — этим занимается polling_worker.py

import asyncio
import os
import json
import re
from typing import List, Tuple, Dict, Optional, Any

from aiogram import F, types
from aiogram.types import InlineKeyboardMarkup, FSInputFile, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from openai import OpenAI
from ddgs import DDGS
import httpx
from lxml import html as lxml_html

from config import settings

# ========= Глобальные кэши =========
ABOUT_TEXT: Optional[str] = None
# ACTIVE_FAQ_TOPICS: List[Tuple[key, label, full]]
ACTIVE_FAQ_TOPICS: List[Tuple[str, str, str]] = []
# FAQ_CACHE: key -> reply
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

# ========= Загрузка кэша about/faq из файлов =========
def _slug(s: str) -> str:
    s = re.sub(r"\s+", "-", (s or "").strip().lower())
    s = re.sub(r"[^a-z0-9\-а-яё]", "", s)
    return s[:64] or "topic"

def load_cache():
    """Грузим data/about_cache.txt и data/faq_cache.json (поддерживаем разные форматы)."""
    global ABOUT_TEXT, ACTIVE_FAQ_TOPICS, FAQ_CACHE
    ABOUT_TEXT, ACTIVE_FAQ_TOPICS, FAQ_CACHE = None, [], {}

    # about
    try:
        with open("data/about_cache.txt", "r", encoding="utf-8") as f:
            ABOUT_TEXT = f.read().strip()
    except FileNotFoundError:
        ABOUT_TEXT = None

    # faq (универсальный парсер)
    try:
        with open("data/faq_cache.json", "r", encoding="utf-8") as f:
            raw = f.read().strip()
        payload: Any = json.loads(raw) if raw else {}
        topics = []
        if isinstance(payload, dict):
            topics = payload.get("topics", []) or []
        elif isinstance(payload, list):
            topics = payload
        for item in topics:
            if isinstance(item, dict):
                key = item.get("key") or _slug(item.get("label") or item.get("q") or item.get("title") or "")
                label = item.get("label") or item.get("q") or item.get("title") or "Вопрос"
                full = item.get("full") or item.get("question") or item.get("q") or label
                reply = item.get("reply") or item.get("answer") or item.get("a") or item.get("text")
            elif isinstance(item, (list, tuple)) and len(item) >= 4:
                key, label, full, reply = item[0], item[1], item[2], item[3]
            else:
                continue

            if key and label and full and reply and not is_empty_message(reply):
                ACTIVE_FAQ_TOPICS.append((str(key), str(label), str(full)))
                FAQ_CACHE[str(key)] = str(reply)
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
        return (resp.choices[0].message.content or "").strip().lower().startswith("y")
    except Exception:
        return True

async def is_question_relevant(question: str) -> bool:
    return rule_based_interview_relevance(question) or await classify_interview_relevance(question)

# ========= Вспомогательное: вытащить текущую компанию из локального RAG =========
def _retrieve_any(query: str) -> List[str]:
    """
    Универсальный адаптер к rag.retrieve: поддерживаем и List[dict], и List[(text, score)].
    Возвращаем список текстов фрагментов.
    """
    try:
        from rag import retrieve as _retrieve
    except Exception:
        return []
    try:
        res = _retrieve(query)
    except Exception:
        return []
    out = []
    if isinstance(res, list):
        for it in res:
            if isinstance(it, dict):
                t = (it.get("text") or "").strip()
            elif isinstance(it, (list, tuple)) and it:
                t = str(it[0]).strip()
            else:
                t = ""
            if t:
                out.append(t)
    return out

async def extract_current_company_from_local_index() -> Optional[str]:
    """
    Берём из локального RAG текущего работодателя (последняя позиция).
    Возвращаем строку Company или None.
    """
    frags = _retrieve_any("Текущее место работы: укажи название компании и должность (если есть).")
    if not frags:
        return None
    context_block = "\n\n".join([f"Фрагмент #{i+1}:\n{frag}" for i, frag in enumerate(frags[:5])])
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
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, flags=re.S)
        data = json.loads(m.group(0)) if m else {}
        comp = (data.get("company") or "").strip().strip('"“”«»')
        return comp or None
    except Exception:
        return None

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

def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

def _web_fetch_impl(url: str, max_chars: int = 4000) -> dict:
    try:
        with httpx.Client(follow_redirects=True, timeout=12.0,
                          headers={"User-Agent": "Mozilla/5.0"}) as c:
            resp = c.get(url)
            resp.raise_for_status()
            if any(part in str(resp.url).lower() for part in BAD_URL_PARTS):
                return {"url": url, "text": ""}
            doc = lxml_html.fromstring(resp.text)
            for bad in doc.xpath('//script|//style|//noscript'):
                bad.drop_tree()
            txt = _clean_text(doc.text_content())
            return {"url": url, "text": txt[:max_chars]}
    except Exception:
        return {"url": url, "text": ""}

# ========= Assistants API: раннер с поддержкой tools =========
async def answer_via_assistant(question: str) -> Optional[str]:
    """
    Ассистент (File Search + tools). Если ассистент вызывает web_search без компании в вопросах headcount —
    подставим компанию, извлечённую из резюме.
    """
    if not settings.assistant_id or not settings.openai_api_key:
        return None

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        thread = client.beta.threads.create()
        client.beta.threads.messages.create(thread_id=thread.id, role="user", content=question)
        run = client.beta.threads.runs.create(thread_id=thread.id, assistant_id=settings.assistant_id)

        # заранее достанем текущую компанию из локального индекса
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
                        # если похоже на headcount, но нет названия компании — добавим его
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

# ========= Обработчики сообщений =========
async def handle_start(message: types.Message):
    if ABOUT_TEXT:
        intro = "Вы общаетесь с цифровым аватаром резюме Тимура Асяева.\n\n"
        await message.answer(intro + ABOUT_TEXT, reply_markup=main_kb())
    else:
        await message.answer("Кэш ещё не создан. Выполните /reindex или `python3 ingestion.py`.", reply_markup=main_kb())

async def handle_help(message: types.Message):
    txt = ("Команды:\n"
           "/about — краткая самопрезентация\n"
           "/resume — скачать резюме\n"
           "/linkedin — ссылка на LinkedIn\n"
           "/reindex — переиндексация резюме (только владелец)\n\n"
           f"{CTA}")
    await message.answer(txt, reply_markup=main_kb())

async def handle_about(message: types.Message):
    if ABOUT_TEXT: await message.answer(ABOUT_TEXT, reply_markup=main_kb())
    else: await message.answer("Кэш ещё не создан. Выполните /reindex.", reply_markup=main_kb())

async def handle_resume(message: types.Message):
    path = settings.resume_path or "data/CVTimurAsyaev.pdf"
    if not os.path.exists(path):
        await message.answer("Файл резюме не найден на сервере."); return
    await message.answer_document(
        FSInputFile(path, filename="CVTimurAsyaev.pdf"),
        caption="CV Тимура Асяева (PDF).\n\n" + CTA
    )

async def handle_linkedin(message: types.Message):
    if settings.linkedin_url:
        await message.answer(f"Мой LinkedIn: {settings.linkedin_url}\n\n{CTA}", reply_markup=main_kb())
    else:
        await message.answer("Ссылка на LinkedIn не настроена.\n\n" + CTA, reply_markup=main_kb())

async def handle_reindex(message: types.Message):
    if message.from_user and message.from_user.id != settings.owner_id:
        await message.answer("Команда доступна только владельцу."); return
    await message.answer("Начинаю переиндексацию…")
    try:
        # безопаснее дернуть отдельным процессом
        import sys as _sys, subprocess as _sp
        ret = _sp.run([_sys.executable, "ingestion.py"], check=False)
        load_cache()
        if ret.returncode == 0:
            await message.answer("Переиндексация и кэш обновлены ✅", reply_markup=main_kb())
        else:
            await message.answer("Переиндексация завершилась с ошибкой (см. логи), кэш обновлён по возможности.", reply_markup=main_kb())
    except Exception as e:
        await message.answer(f"Ошибка переиндексации: {e}")

async def handle_free_text(message: types.Message):
    question = (message.text or "").strip()
    if not question:
        await message.answer("Пришлите, пожалуйста, текст вопроса.", reply_markup=main_kb())
        return

    # 1) фильтр на релевантность собеседованию
    if not await is_question_relevant(question):
        msg = ("Этот вопрос не относится к тематике собеседования и моему резюме. "
               f"По организационным или личным вопросам лучше связаться со мной: {settings.contact_info}")
        await message.answer(msg, reply_markup=main_kb())
        return

    # 2) сначала — ассистент (File Search → при необходимости web_search/web_fetch)
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

# ========= Callback-хэндлеры =========
async def cb_about(callback: CallbackQuery):
    await handle_about(callback.message); await callback.answer()

async def cb_resume(callback: CallbackQuery):
    await handle_resume(callback.message); await callback.answer()

async def cb_linkedin(callback: CallbackQuery):
    await handle_linkedin(callback.message); await callback.answer()

async def cb_faq_menu(callback: CallbackQuery):
    if not ACTIVE_FAQ_TOPICS:
        await callback.message.answer("Сейчас нет доступных FAQ по резюме. " + CTA, reply_markup=main_kb())
        return await callback.answer()
    await callback.message.answer("Часто задаваемые вопросы от HR — выберите тему (или задайте вопрос текстом):",
                                  reply_markup=faq_kb(0))
    await callback.answer()

async def cb_faq_page(callback: CallbackQuery):
    try: page = int(callback.data.split(":",1)[1])
    except Exception: page = 0
    try:
        await callback.message.edit_reply_markup(reply_markup=faq_kb(page))
    except Exception:
        await callback.message.answer("Часто задаваемые вопросы от HR — выберите тему:",
                                      reply_markup=faq_kb(page))
    await callback.answer()

async def cb_faq_topic(callback: CallbackQuery):
    key = callback.data.split(":",1)[1]
    if key not in FAQ_CACHE:
        await callback.answer("По этой теме нет ответа в кэше.", show_alert=True); return
    label = next((lbl for k,lbl,_ in ACTIVE_FAQ_TOPICS if k==key), "Ответ")
    await callback.message.answer(f"{label}:\n\n{FAQ_CACHE[key]}", reply_markup=main_kb())
    await callback.answer()

async def cb_faq_close(callback: CallbackQuery):
    try: await callback.message.delete()
    except Exception: pass
    await callback.answer()

# ========= Startup hook =========
async def on_startup():
    load_cache()
    print("[STARTUP] Cache loaded (no heavy LLM calls).")

# ========= Экспорт для воркера =========
def register_handlers(dp) -> None:
    # стартап
    dp.startup.register(on_startup)
    # Команды/сообщения
    dp.message.register(handle_start,           F.text == "/start")  # дубль на всякий случай
    from aiogram.filters import Command, CommandStart
    dp.message.register(handle_start,           CommandStart())
    dp.message.register(handle_help,            Command(commands=["help"]))
    dp.message.register(handle_about,           Command(commands=["about"]))
    dp.message.register(handle_resume,          Command(commands=["resume"]))
    dp.message.register(handle_linkedin,        Command(commands=["linkedin"]))
    dp.message.register(handle_reindex,         Command(commands=["reindex"]))
    dp.message.register(handle_free_text,       F.text)
    # Кнопки
    dp.callback_query.register(cb_about,        F.data == "about")
    dp.callback_query.register(cb_resume,       F.data == "resume")
    dp.callback_query.register(cb_linkedin,     F.data == "linkedin")
    dp.callback_query.register(cb_faq_menu,     F.data == "faq_menu")
    dp.callback_query.register(cb_faq_close,    F.data == "faq_close")
    dp.callback_query.register(cb_faq_page,     lambda c: c.data and c.data.startswith("faq_p:"))
    dp.callback_query.register(cb_faq_topic,    lambda c: c.data and c.data.startswith("faq_t:"))
