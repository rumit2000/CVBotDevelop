# ingestion.py
import os
import json
from pathlib import Path

print("[INGEST] start")

# 1) Построение RAG-индекса (не падаем, если что-то изменилось)
try:
    import rag
    paths = [str(Path("data") / "CVTimurAsyaev.pdf")]

    called = False
    for fname in ["ingest", "build_index", "build_index_from_files", "build", "index_files"]:
        if hasattr(rag, fname):
            try:
                info = getattr(rag, fname)(paths)
                print(f"[INGEST] RAG index: {info}")
                called = True
                break
            except Exception as e:
                print(f"[INGEST] RAG '{fname}' error: {e}")
    if not called:
        print("[INGEST] RAG: suitable function not found, skipped.")
except Exception as e:
    print(f"[INGEST] RAG import/build error: {e}")

# 2) about/faq: гарантированный формат и устойчивость
about_path = Path("data/about_cache.txt")
faq_path = Path("data/faq_cache.json")
about_path.parent.mkdir(parents=True, exist_ok=True)

def _extract_pdf_snippet(pdf_path: Path, limit: int = 500) -> str:
    try:
        from pypdf import PdfReader
        if not pdf_path.exists():
            return ""
        reader = PdfReader(str(pdf_path))
        buf = []
        for page in reader.pages[:3]:
            t = (page.extract_text() or "").strip()
            if t:
                buf.append(t)
        text = "\n".join(buf).strip()
        if len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text
    except Exception:
        return ""

snippet = _extract_pdf_snippet(Path("data") / "CVTimurAsyaev.pdf")
default_about = (
    "Краткая выжимка профиля из резюме Тимура Асяева. "
    "Этот текст используется ботом как 'about' до уточнения моделью."
)
about_text = snippet or default_about

try:
    about_path.write_text(about_text, encoding="utf-8")
    print(f"[INGEST] wrote: {about_path} ({len(about_text)} bytes)")
except Exception as e:
    print(f"[INGEST] about write error: {e}")

faq_payload = {"topics": []}

api_key = os.getenv("OPENAI_API_KEY")
if api_key:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        system = (
            "Ты пишешь JSON-объект с часто задаваемыми вопросами по резюме. "
            'Строго верни JSON-объект формата {"topics":[{"q":"...","a":"..."}]} без пояснений.'
        )
        user = (
            "Сгенерируй 5 лаконичных Q&A на русском по резюме ниже. "
            "Короткие вопросы и короткие ответы (1–3 предложения). Текст резюме:\n\n"
            + (snippet or about_text)
        )

        # Используем Chat Completions (надёжнее в разных версиях SDK)
        chat = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL_JSON", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        raw = (chat.choices[0].message.content or "").strip()

        # Пытаемся аккуратно извлечь JSON даже если модель вернула с ```json
        candidate = raw
        if "```" in raw:
            parts = raw.split("```")
            # ищем блок с json
            for i in range(len(parts) - 1):
                block = parts[i + 1]
                if block.strip().startswith("json"):
                    candidate = block.split("\n", 1)[1] if "\n" in block else ""
                    break

        parsed = json.loads(candidate) if candidate else {}
        if isinstance(parsed, list):
            parsed = {"topics": parsed}
        topics = parsed.get("topics", []) if isinstance(parsed, dict) else []
        if isinstance(topics, list):
            faq_payload = {"topics": topics}
        else:
            print("[INGEST] model returned non-list topics, keeping empty list")
    except Exception as e:
        print(f"[INGEST] JSON parse error from model: {e}")
else:
    print("[INGEST] OPENAI_API_KEY not set; skipping FAQ generation")

try:
    serialized = json.dumps(faq_payload, ensure_ascii=False, indent=2)
    faq_path.write_text(serialized, encoding="utf-8")
    print(f"[INGEST] wrote: {faq_path} ({len(serialized)} bytes)")
except Exception as e:
    print(f"[INGEST] faq write error: {e}")

print("[INGEST] done")
