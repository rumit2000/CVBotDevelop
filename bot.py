# bot.py
# Телеграм-бот:
# - кнопки (Обо мне / CV / FAQ / CV OnePage — всегда видна)
# - FAQ: свой каталог тем, на старте ответы буферизуются из резюме (локальный RAG)
# - свободные вопросы: OpenAI Assistants API (File Search) + кастомные tools web_search/web_fetch
# - если ассистент не справился — общий веб-фолбэк или контакты
# - доп.кнопка "видео ответ" у GPT-ответов: по клику шлём краткое саммари этого ответа

import asyncio
import os
import json
import re
from typing import List, Tuple, Dict, Optional, Any
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, FSInputFile, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from openai import OpenAI
from ddgs import DDGS
import httpx
from lxml import html as lxml_html

from config import settings

# ========= Текстовые константы =========
CTA = "Вы также можете задать вопрос на естественном языке."

SHORT_ABOUT = (
    "Опытный технический лидер с более чем 20-летним стажем в разработке и управлении "
    "высокотехнологичными продуктами. Специализируюсь на исследованиях и внедрении решений в области "
    "искусственного интеллекта (AI), включая крупные языковые модели (LLM: GigaChat, GPT, DeepSeek) и AI-агентов. "
    "Руководил проектами по созданию AI-ассистентов, развертыванию LLM на маломощных платформах (Raspberry Pi, МКС), "
    "внедрению нейросетей в embedded-устройства и трансформации бизнес-процессов через AI. "
    "Лауреат премии CES 2022 за инновации."
)

# ========= Глобальные кэши =========
ABOUT_TEXT: Optional[str] = None
ACTIVE_FAQ_TOPICS: List[Tuple[str, str, str]] = []  # (key, label, full)
FAQ_CACHE: Dict[str, str] = {}

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
    r"\bno\s*answer\b",
]
BAD_HOSTS = [
    "oshibok-net.ru", "obrazovaka.ru", "gramota.ru",
    "rus.stackexchange.com", "stackexchange.com",
]
BAD_URL_PARTS = ["login", "signin", "auth", "callback", "account", "microsoftonline", "oauth", "sso"]
EMPLOYEE_KEYWORDS = [
    "employees","employee count","headcount","staff","team size",
    "сотрудник","сотрудников","численность","штат","размер компании",
]

def is_empty_message(text: Optional[str]) -> bool:
    if not text or not text.strip():
        return True
    t = text.lower().replace("ё", "е")
    return any(re.search(p, t) for p in EMPTY_PATTERNS)

# ========= Каталог FAQ =========
def hr_faq_catalog() -> List[Dict[str, str]]:
    return [
        {"key": "who_now", "label": "Кто вы сейчас?", "full":
         "Кто вы сейчас? Опишите текущую роль и целевые роли (CEO / COO / CTO / CPO / Head of R&D и т.п.)."},
        {"key": "industry", "label": "Отрасль/домен", "full":
         "Отрасль и домен экспертизы. Примеры: FinTech, Telco, Retail, Industrial, AI/ML, GovTech и пр."},
        {"key": "company_scale", "label": "Масштаб компании", "full":
         "Масштаб компании: выручка/EBITDA, стадия (startup/scale-up/корпорация), число сотрудников, география."},
        {"key": "scope", "label": "Зона ответственности", "full":
         "Масштаб ответственности: P&L, CAPEX/OPEX, бюджет, зона (продукт/технологии/операции/продажи)."},
        {"key": "team", "label": "Команда", "full":
         "Команда: сколько прямых репортов, общий размер функции, уровни (VP/Director/Lead)."},
        {"key": "skills", "label": "Ключ. компетенции", "full":
         "Ключевые компетенции: стратегия, трансформация, M&A, оргдизайн, выход на новые рынки и т.д."},
        {"key": "achievements", "label": "Топ-достижения", "full":
         "Топ-достижения с цифрами (2–4 bullets): рост %, экономия $, time-to-market, NPS, SLA, ROI."},
        {"key": "location", "label": "Локация/мобильность", "full":
         "Локация и мобильность: город, готовность к релокации/командировкам; языки и уровень."},
        {"key": "legal", "label": "Правовой статус", "full":
         "Правовой статус: рабочее разрешение/гражданство, non-compete/notice period (если критично)."},
        {"key": "pivots", "label": "Страт. развороты", "full":
         "Стратегические развороты: какие трансформации (digital/операционная/продуктовая) и к чему привели."},
        {"key": "from_scratch", "label": "С нуля", "full":
         "Построение функций «с нуля»: архитектура процессов, метрики, управление (OKR/KPI, governance)."},
        {"key": "global", "label": "Глобальный контекст", "full":
         "Международные рынки/мультикультура, распределённые команды."},
        {"key": "stakeholders", "label": "Стейкхолдеры", "full":
         "Совет директоров, акционеры, регуляторы, ключевые клиенты/партнёры."},
        {"key": "reputation", "label": "Репутация", "full":
         "Публичные кейсы, награды, публикации, патенты, борд-роль."},
        {"key": "pnl", "label": "P&L и результат", "full":
         "Например: «Отвечал за P&L $120M; EBITDA +4.2 п.п. за 12 мес»."},
        {"key": "system", "label": "Системность", "full":
         "Оргдизайн, cadence, OKR, риск-менеджмент; как именно управляли."},
        {"key": "leadership", "label": "Лидерство/резерв", "full":
         "Кого вырастили, текучесть, bench strength."},
        {"key": "change", "label": "Управление изменениями", "full":
         "Какие барьеры снимали, как измеряли эффект."},
    ]

# ========= Кэш загрузка/сохранение =========
def load_cache():
    global ABOUT_TEXT, ACTIVE_FAQ_TOPICS, FAQ_CACHE
    ABOUT_TEXT, ACTIVE_FAQ_TOPICS, FAQ_CACHE = None, [], {}
    # about
    try:
        with open("data/about_cache.txt", "r", encoding="utf-8") as f:
            ABOUT_TEXT = f.read().strip()
    except FileNotFoundError:
        ABOUT_TEXT = None
    # faq
    try:
        with open("data/faq_cache.json", "r", encoding="utf-8") as f:
            payload = json.load(f)
        topics = payload.get("topics", []) if isinstance(payload, dict) else (payload if isinstance(payload, list) else [])
        for item in topics:
            key   = (item.get("key") or "").strip()
            label = (item.get("label") or "").strip()
            full  = (item.get("full") or "").strip()
            reply = (item.get("reply") or "").strip()
            if key and label and full and reply and not is_empty_message(reply):
                ACTIVE_FAQ_TOPICS.append((key, label, full))
                FAQ_CACHE[key] = reply
    except FileNotFoundError:
        pass
    print(f"[CACHE] Loaded: about={'OK' if ABOUT_TEXT else 'MISSING'}, faq_topics={len(ACTIVE_FAQ_TOPICS)}")

def save_faq_cache(topics: List[Dict[str, str]]):
    os.makedirs("data", exist_ok=True)
    with open("data/faq_cache.json", "w", encoding="utf-8") as f:
        json.dump({"topics": topics}, f, ensure_ascii=False, indent=2)

# ========= Нормализация результата RAG =========
def _norm_ctx(ctx: Any, limit: int = 5) -> List[str]:
    out: List[str] = []
    if ctx is None: return out
    for item in ctx:
        text = None
        if isinstance(item, str):
            text = item
        elif isinstance(item, (list, tuple)) and item and isinstance(item[0], str):
            text = item[0]
        elif isinstance(item, dict):
            for k in ("text", "chunk", "content", "fragment"):
                v = item.get(k)
                if isinstance(v, str) and v.strip():
                    text = v; break
        if text:
            t = text.strip()
            if t and t not in out:
                out.append(t)
        if len(out) >= limit: break
    return out

def _prefer_company(frags: List[str], company: str) -> List[str]:
    if not company: return frags
    kw = company.lower()
    extra = ["сбербанк", "сбер", "sber", "sberbank"] if kw in ("сбер","сбербанк") else []
    keys = set([kw] + extra)
    picked = [f for f in frags if any(k in f.lower() for k in keys)]
    return picked if picked else frags

# ========= Генерация ответов FAQ из локального RAG =========
async def _answer_from_resume(full_question: str, prefer_company: Optional[str] = None) -> Optional[str]:
    try:
        from rag import retrieve as rag_retrieve
    except Exception:
        return None
    try:
        ctx_raw = rag_retrieve(full_question)
    except Exception:
        return None

    frags = _norm_ctx(ctx_raw, limit=8)
    if not frags:
        return None
    if prefer_company:
        frags = _prefer_company(frags, prefer_company)

    context_block = "\n\n".join([f"Фрагмент #{i+1}:\n{frag}" for i, frag in enumerate(frags[:5])])

    system = (
        "Ты помощник по резюме. Отвечай ТОЛЬКО на основе предоставленных фрагментов.\n"
        "Если информации недостаточно — выведи ровно: NO_ANSWER.\n"
        "Если достаточно — ответь кратко и по делу, можно 3–6 буллетов с цифрами/метриками.\n"
        "Язык ответа: русский."
    )
    user = f"Вопрос: {full_question}\n\nФрагменты резюме:\n{context_block}\n\nОтвет:"

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.2,
        )
        ans = (resp.choices[0].message.content or "").strip()
        if not ans or "NO_ANSWER" in ans.upper() or is_empty_message(ans):
            return None
        return ans
    except Exception:
        return None

async def ensure_faq_ready():
    global ACTIVE_FAQ_TOPICS, FAQ_CACHE
    if ACTIVE_FAQ_TOPICS and FAQ_CACHE:
        return
    catalog = hr_faq_catalog()
    built: List[Dict[str, str]] = []
    for item in catalog:
        key, label, full = item["key"], item["label"], item["full"]
        prefer = "Сбер" if key == "company_scale" else None
        ans = await _answer_from_resume(full, prefer_company=prefer)
        if ans:
            built.append({"key": key, "label": label, "full": full, "reply": ans})
    if built:
        save_faq_cache(built)
        load_cache()
    else:
        ACTIVE_FAQ_TOPICS, FAQ_CACHE = [], {}

# ========= Кнопочные клавиатуры =========
def main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Обо мне", callback_data="about")
    kb.button(text="CV", callback_data="resume")
    kb.button(text="FAQ от HR", callback_data="faq_menu")
    kb.button(text="CV OnePage", callback_data="onepage")
    kb.adjust(2, 2)
    return kb.as_markup()

def main_kb_with_video() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Обо мне", callback_data="about")
    kb.button(text="CV", callback_data="resume")
    kb.button(text="FAQ от HR", callback_data="faq_menu")
    kb.button(text="CV OnePage", callback_data="onepage")
    kb.button(text="видео ответ", callback_data="video_nop")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def faq_kb(page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    topics = ACTIVE_FAQ_TOPICS
    total = len(topics)
    if total == 0:
        kb = InlineKeyboardBuilder()
        kb.button(text="Закрыть", callback_data="faq_close")
        kb.adjust(1)
        return kb.as_markup()
    total_pages = (total - 1) // per_page + 1
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
    return any(re.search(p, q.lower()) for p in HR_PATTERNS)

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

# ========= Вспомогательное: текущая компания из RAG =========
async def extract_current_company_from_local_index() -> Optional[str]:
    try:
        from rag import retrieve as _retrieve
    except Exception:
        return None
    ctx_raw = _retrieve("Текущее место работы: укажи название компании и должность (если есть).")
    frags = _norm_ctx(ctx_raw, limit=3)
    if not frags:
        return None
    context_block = "\n\n".join([f"Фрагмент #{i+1}:\n{frag}" for i, frag in enumerate(frags)])
    system = ("Ты — экстрактор фактов из резюме. Верни строго JSON: "
              '{"company": "..."} без пояснений. Если не уверен — используй null.')
    user = f"Фрагменты резюме:\n\n{context_block}\n\nВерни только JSON."
    client = OpenAI(api_key=settings.openai_api_key)
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=0
        )
        m = re.search(r"\{.*\}", (resp.choices[0].message.content or "").strip(), flags=re.S)
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
                if not url: continue
                ul = url.lower()
                if any(bad in ul for bad in BAD_HOSTS): continue
                if any(part in ul for part in BAD_URL_PARTS): continue
                out.append({"title": title, "url": url, "snippet": body})
                if len(out) >= max_results: break
    except Exception:
        pass
    return out

def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()

def _web_fetch_impl(url: str, max_chars: int = 4000) -> dict:
    try:
        with httpx.Client(follow_redirects=True, timeout=12.0, headers={"User-Agent": "Mozilla/5.0"}) as c:
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

# ========= Assistants API: раннер с tools =========
async def answer_via_assistant(question: str) -> Optional[str]:
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
                        outputs.append({"tool_call_id": call.id, "output": json.dumps(results, ensure_ascii=False)})
                    elif name == "web_fetch":
                        url = (args.get("url") or "").strip()
                        max_chars = int(args.get("max_chars", 4000))
                        result = _web_fetch_impl(url, max_chars=max_chars)
                        outputs.append({"tool_call_id": call.id, "output": json.dumps(result, ensure_ascii=False)})
                    else:
                        outputs.append({"tool_call_id": call.id, "output": json.dumps({"error": "unknown tool"})})
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
            return None
    except Exception:
        return None

# ========= Саммари по кнопке «видео ответ» =========
async def summarize_answer_text(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None
    # убираем html-теги, чтобы не попадали в промпт
    plain = re.sub(r"<[^>]+>", "", raw)
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content":
                 "Ты пишешь очень короткое саммари ответа для Telegram. "
                 "Дай 2–4 лаконичных пункта или 2–3 предложения, без вводных и воды. Язык: русский."},
                {"role": "user", "content": f"Сделай краткую выжимку этого ответа:\n\n{plain}"}
            ],
            temperature=0.2,
            max_tokens=300,
        )
        s = (resp.choices[0].message.content or "").strip()
        return s or None
    except Exception:
        return None

# ========= Команды и обработчики сообщений =========
async def handle_onepage(message: types.Message):
    onepage_path = "data/CVTimurAsyaevOnePage.pdf"
    if not os.path.exists(onepage_path):
        await message.answer("Файл one-page резюме не найден на сервере.")
        return
    await message.answer_document(
        FSInputFile(onepage_path, filename="CVTimurAsyaevOnePage.pdf"),
        caption="One-page CV Тимура Асяева (PDF).\n\n" + CTA
    )

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
           "/reindex — переиндексация резюме (только владелец)\n\n" + CTA)
    await message.answer(txt, reply_markup=main_kb())

async def handle_about(message: types.Message):
    await message.answer(SHORT_ABOUT, reply_markup=main_kb())

async def handle_resume(message: types.Message):
    if not os.path.exists(settings.resume_path):
        await message.answer("Файл резюме не найден на сервере.")
        return
    await message.answer_document(
        FSInputFile(settings.resume_path, filename="CVTimurAsyaev.pdf"),
        caption="CV Тимура Асяева (PDF).\n\n" + CTA
    )

async def handle_linkedin(message: types.Message):
    link = (settings.linkedin_url or "").strip()
    if link:
        if not link.startswith(("http://", "https://")):
            link = "https://" + link
        await message.answer(f"Мой LinkedIn: {link}\n\n{CTA}", reply_markup=main_kb())
    else:
        await message.answer("Ссылка на LinkedIn не настроена.\n\n" + CTA, reply_markup=main_kb())

async def handle_reindex(message: types.Message):
    if message.from_user.id != settings.owner_id:
        await message.answer("Команда доступна только владельцу.")
        return
    await message.answer("Начинаю переиндексацию…")
    try:
        import ingestion
        await asyncio.get_running_loop().run_in_executor(None, ingestion.main)
        load_cache()
        await ensure_faq_ready()
        await message.answer("Переиндексация и кэш обновлены ✅", reply_markup=main_kb())
    except Exception as e:
        await message.answer(f"Ошибка переиндексации: {e}")

async def handle_free_text(message: types.Message):
    question = (message.text or "").strip()
    if not question:
        await message.answer("Пришлите текст вопроса, пожалуйста.", reply_markup=main_kb())
        return

    # 1) фильтр релевантности интервью
    if not await is_question_relevant(question):
        msg = ("Этот вопрос не относится к тематике собеседования и моему резюме. "
               f"По организационным или личным вопросам лучше связаться со мной: {settings.contact_info}")
        await message.answer(msg, reply_markup=main_kb())
        return

    # 2) основной ответ ассистента
    ans = await answer_via_assistant(question)
    if ans and not is_empty_message(ans):
        not_relevant_msg = ("Этот вопрос не относится к тематике собеседования и моему резюме. "
                            f"По организационным или личным вопросам лучше связаться со мной: {settings.contact_info}")
        markup = main_kb() if ans.strip() == not_relevant_msg else main_kb_with_video()
        await message.answer(ans, reply_markup=markup)
        return

    # 3) веб-фолбэк
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

# ========= Callback-хендлеры =========
@contextmanager
def contextlib_sup():
    try:
        yield
    except Exception:
        pass

async def cb_about(callback: CallbackQuery):
    await handle_about(callback.message)
    with contextlib_sup(): await callback.answer()

async def cb_resume(callback: CallbackQuery):
    await handle_resume(callback.message)
    with contextlib_sup(): await callback.answer()

async def cb_linkedin(callback: CallbackQuery):
    await handle_linkedin(callback.message)
    with contextlib_sup(): await callback.answer()

async def cb_faq_menu(callback: CallbackQuery):
    if not ACTIVE_FAQ_TOPICS:
        await ensure_faq_ready()
    if not ACTIVE_FAQ_TOPICS:
        await callback.message.answer("Сейчас нет доступных FAQ по резюме. " + CTA, reply_markup=main_kb())
        with contextlib_sup(): await callback.answer()
        return
    await callback.message.answer(
        "Часто задаваемые вопросы от HR — выберите тему (или задайте вопрос текстом):",
        reply_markup=faq_kb(0)
    )
    with contextlib_sup(): await callback.answer()

async def cb_faq_page(callback: CallbackQuery):
    try:
        page = int(callback.data.split(":", 1)[1])
    except Exception:
        page = 0
    try:
        await callback.message.edit_reply_markup(reply_markup=faq_kb(page))
    except Exception:
        await callback.message.answer("Часто задаваемые вопросы от HR — выберите тему:", reply_markup=faq_kb(page))
    with contextlib_sup(): await callback.answer()

async def cb_faq_topic(callback: CallbackQuery):
    key = (callback.data.split(":", 1)[1] if ":" in (callback.data or "") else "").strip()
    if not key or key not in FAQ_CACHE:
        with contextlib_sup():
            await callback.answer("По этой теме нет ответа в кэше.", show_alert=True)
        return
    label = next((lbl for k, lbl, _ in ACTIVE_FAQ_TOPICS if k == key), "Ответ")
    await callback.message.answer(f"{label}:\n\n{FAQ_CACHE[key]}", reply_markup=main_kb())
    with contextlib_sup(): await callback.answer()

async def cb_faq_close(callback: CallbackQuery):
    with contextlib_sup():
        await callback.message.delete()
        await callback.answer()

# OnePage
async def cb_onepage(callback: CallbackQuery):
    await handle_onepage(callback.message)
    with contextlib_sup(): await callback.answer()

# === Новое: саммари по кнопке «видео ответ» ===
async def cb_video_summary(callback: CallbackQuery):
    # берём текст из того сообщения, к которому прикреплена кнопка
    src = getattr(callback.message, "html_text", None) or (callback.message.text or "")
    summary = await summarize_answer_text(src)
    if summary:
        await callback.message.answer("Краткий итог ответа:\n\n" + summary, reply_markup=main_kb())
    else:
        await callback.message.answer("Не удалось построить краткий итог ответа.", reply_markup=main_kb())
    with contextlib_sup():
        await callback.answer()

# ========= Startup & registration =========
async def on_startup():
    load_cache()
    await ensure_faq_ready()
    print("[STARTUP] Cache loaded; FAQ ready.")

def register_handlers(dp: Dispatcher):
    dp.startup.register(on_startup)

    # Команды/сообщения
    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command(commands=["help"]))
    dp.message.register(handle_about, Command(commands=["about"]))
    dp.message.register(handle_resume, Command(commands=["resume"]))
    # dp.message.register(handle_linkedin, Command(commands=["linkedin"]))
    dp.message.register(handle_reindex, Command(commands=["reindex"]))
    dp.message.register(handle_onepage, Command(commands=["onepage"]))
    dp.message.register(handle_free_text, F.text)

    # Кнопки (callbacks)
    dp.callback_query.register(cb_about, F.data == "about")
    dp.callback_query.register(cb_resume, F.data == "resume")
    # dp.callback_query.register(cb_linkedin, F.data == "linkedin")
    dp.callback_query.register(cb_faq_menu, F.data == "faq_menu")
    dp.callback_query.register(cb_faq_close, F.data == "faq_close")
    dp.callback_query.register(cb_faq_page, lambda c: c.data and c.data.startswith("faq_p:"))
    dp.callback_query.register(cb_faq_topic, lambda c: c.data and c.data.startswith("faq_t:"))
    dp.callback_query.register(cb_onepage, F.data == "onepage")

    # Новая регистрация обработчика «видео ответ»
    dp.callback_query.register(cb_video_summary, F.data == "video_nop")
