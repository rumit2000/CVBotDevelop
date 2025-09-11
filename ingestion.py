# ingestion.py
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List

from openai import OpenAI

import rag


DATA_DIR = Path("data")
ABOUT_FILE = DATA_DIR / "about_cache.txt"
FAQ_FILE = DATA_DIR / "faq_cache.json"
DEFAULT_SOURCE = DATA_DIR / "CVTimurAsyaev.pdf"

CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")


def _read_pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages: List[str] = []
    for p in reader.pages:
        pages.append(p.extract_text() or "")
    return "\n\n".join(pages)


def _gen_about_and_faq(full_text: str) -> dict:
    """
    Просим модель выдать:
      - краткое 'about' (400–600 символов)
      - 8–12 FAQ (вопрос+краткий ответ)
    Возвращает dict {"about": str, "faq": [{"q":..., "a":...}, ...]}
    """
    client = OpenAI()

    system = (
        "Ты помогаешь с подготовкой карьерного бота по резюме. "
        "Говори по-русски. Пиши чётко и по делу."
    )
    user = (
        "Ниже — полный текст резюме. Сформируй:\n"
        "1) Краткий блок About (400–600 символов), цельный абзац.\n"
        "2) Список из 8–12 FAQ, каждый элемент: {\"q\": \"вопрос\", \"a\": \"краткий ответ\"}.\n"
        "Требования:\n"
        "- Используй только факты из резюме, не выдумывай.\n"
        "- Возвращай строго JSON вида {\"about\": str, \"faq\": [{\"q\":..., \"a\":...}, ...]} без пояснений.\n\n"
        f"=== РЕЗЮМЕ ТЕКСТ ===\n{full_text}\n=== КОНЕЦ ==="
    )

    resp = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
    )
    raw = resp.choices[0].message.content.strip()
    try:
        data = json.loads(raw)
        assert isinstance(data, dict) and "about" in data and "faq" in data
        assert isinstance(data["about"], str)
        assert isinstance(data["faq"], list)
        # лёгкая валидация FAQ
        fixed = []
        for item in data["faq"]:
            if not isinstance(item, dict):
                continue
            q = item.get("q")
            a = item.get("a")
            if isinstance(q, str) and isinstance(a, str) and q.strip() and a.strip():
                fixed.append({"q": q.strip(), "a": a.strip()})
        data["faq"] = fixed[:12]
        return data
    except Exception as e:
        print(f"[INGEST] JSON parse error from model: {e}", file=sys.stderr)
        # запасной вариант — пустые данные
        return {"about": "About недоступен: не удалось сгенерировать.", "faq": []}


def main():
    print("[INGEST] start")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1) Построить RAG-индекс
    sources = []
    if DEFAULT_SOURCE.exists():
        sources.append(str(DEFAULT_SOURCE))
    else:
        print(f"[INGEST] WARNING: {DEFAULT_SOURCE} not found. You can upload your PDF to data/.")

    stats = rag.ingest(sources or [], chunk_size=1200, chunk_overlap=200)
    print(f"[INGEST] RAG index: {stats}")

    # 2) Для кэшей возьмём весь текст корпуса (или сыро из PDF, если индекс пуст)
    if stats.get("chunks", 0) > 0:
        full_text = rag.dump_all_text()
    else:
        full_text = _read_pdf_text(DEFAULT_SOURCE) if DEFAULT_SOURCE.exists() else ""

    # 3) Сгенерировать about и FAQ
    data = _gen_about_and_faq(full_text)

    ABOUT_FILE.write_text(data["about"].strip() + "\n", encoding="utf-8")
    FAQ_FILE.write_text(json.dumps(data["faq"], ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[INGEST] wrote: {ABOUT_FILE} ({ABOUT_FILE.stat().st_size} bytes)")
    print(f"[INGEST] wrote: {FAQ_FILE} ({FAQ_FILE.stat().st_size} bytes)")
    print("[INGEST] done")


if __name__ == "__main__":
    main()
