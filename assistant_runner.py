# assistant_runner.py
import asyncio, json, re
from typing import List, Tuple
from openai import OpenAI
from duckduckgo_search import DDGS
import httpx
from lxml import html as lxml_html

from config import settings

BAD_HOSTS = [
    "oshibok-net.ru", "obrazovaka.ru", "gramota.ru",
    "rus.stackexchange.com", "stackexchange.com"
]

def _web_search_impl(query: str, max_results: int = 5) -> List[dict]:
    """Возвращает список из словарей: {title, url, snippet}."""
    out = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, region="ru-ru", safesearch="moderate", max_results=20):
                title = r.get("title") or r.get("source") or "Источник"
                url   = r.get("href")  or r.get("url")    or r.get("link") or ""
                body  = r.get("body")  or ""
                if not url:
                    continue
                if any(bad in url.lower() for bad in BAD_HOSTS):
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
    """Скачивает страницу и извлекает основной текст (очищенный)."""
    try:
        with httpx.Client(follow_redirects=True, timeout=10.0, headers={"User-Agent": "Mozilla/5.0"}) as c:
            resp = c.get(url)
            resp.raise_for_status()
            doc = lxml_html.fromstring(resp.text)
            # грубое извлечение видимого текста
            for bad in doc.xpath('//script|//style|//noscript'):
                bad.drop_tree()
            txt = _clean_text(doc.text_content())
            return {"url": url, "text": txt[:max_chars]}
    except Exception:
        return {"url": url, "text": ""}

async def answer_via_assistant(question: str) -> str | None:
    """
    Универсальный запуск ассистента:
    - Создаём thread
    - Добавляем сообщение пользователя
    - Запускаем run
    - Автоматически обрабатываем requires_action с tool-calls web_search/web_fetch
    - Возвращаем финальный текст ответа ассистента
    """
    if not settings.assistant_id:
        return None

    client = OpenAI(api_key=settings.openai_api_key)
    thread = client.beta.threads.create()
    client.beta.threads.messages.create(thread_id=thread.id, role="user", content=question)
    run = client.beta.threads.runs.create(thread_id=thread.id, assistant_id=settings.assistant_id)

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
                args = {}
                try:
                    args = json.loads(call.function.arguments or "{}")
                except Exception:
                    args = {}
                if name == "web_search":
                    q = args.get("query", "")
                    k = int(args.get("max_results", 5))
                    results = _web_search_impl(q, max_results=k)
                    outputs.append({"tool_call_id": call.id, "output": json.dumps(results, ensure_ascii=False)})
                elif name == "web_fetch":
                    url = args.get("url", "")
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
            # Берём последнее ассистентское сообщение
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

        # Ошибка/отмена/timeout
        return None
