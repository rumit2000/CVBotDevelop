# ingestion.py
import os
import re
import json
import numpy as np
from typing import List, Tuple
from config import settings
from pypdf import PdfReader
import docx2txt
import tiktoken
from openai import OpenAI

# используем RAG для генерации кэша после индексации
from rag import build_messages, retrieve as rag_retrieve

CTA = "Вы также можете задать вопрос на естественном языке."

# Полный набор FAQ (будет отфильтрован по факту наличия ответа)
FAQ_TOPICS: List[Tuple[str, str, str]] = [
    ("who", "Кто вы сейчас?",
     "Кто вы сейчас? Текущая должность и целевая роль (CEO / COO / CTO / CPO / Head of R&D и т.п.)."),
    ("domain", "Отрасль/домен",
     "Отрасль и домен. FinTech, Telco, Retail, Industrial, AI/ML, GovTech и пр."),
    ("scale_company", "Масштаб компании",
     "Масштаб компании. Выручка/EBITDA, стадия (startup/scale-up/корпорация), число сотрудников, география."),
    ("scope", "Масштаб ответственности",
     "Масштаб ответственности. P&L, CAPEX/OPEX, бюджет, зона (продукт/технологии/операции/продажи)."),
    ("team", "Команда",
     "Команда. Сколько прямых репортов / общий размер функции / уровни (VP/Director/Lead)."),
    ("skills", "Ключевые компетенции",
     "Ключевые компетенции. Стратегия, трансформация, M&A, оргструктура, выход на новые рынки и т.д."),
    ("achievements", "Топ-достижения (цифры)",
     "Топ-достижения с цифрами. 2–4 bullets: рост %, экономия $, time-to-market, NPS, SLA, ROI."),
    ("location", "Локация/мобильность",
     "Локация и мобильность. Город, готовность к релокации/командировкам; языки (уровень)."),
    ("legal", "Правовой статус",
     "Правовой статус. Рабочее разрешение/гражданство, non-compete/notice period (если критично)."),
    ("transformations", "Стратегические развороты",
     "Стратегические развороты. Какие трансформации (digital/операционная/продуктовая) и к чему привели."),
    ("from_scratch", "С нуля (функции)",
     "Построение функций «с нуля». Архитектура процессов, метрики, системы управления (OKR/KPI, governance)."),
    ("crisis", "Кризисы/антикризис",
     "Кризисы и антикризис. Что именно починили: издержки, отток клиентов, инциденты безопасности."),
    ("global", "Глобальный контекст",
     "Глобальный контекст. Международные рынки/мультикультура, распределённые команды."),
    ("stakeholders", "Стейкхолдер-менеджмент",
     "Стейкхолдер-менеджмент. Совет директоров, акционеры, регуляторы, ключевые клиенты/партнёры."),
    ("reputation", "Репутация/референсы",
     "Репутация/референсы. Публичные кейсы, награды, публикации, патенты, борд-роль."),
    ("pnl", "P&L и результат",
     "P&L и результат. Например: «Отвечал за P&L $120M; EBITDA +4.2 п.п. за 12 мес»."),
    ("system", "Системность",
     "Системность. Как управляли: оргдизайн, cadence, OKR, риск-менеджмент."),
    ("leadership", "Лидерство/преемственность",
     "Лидерство и преемственность. Кого вырастили, текучесть, bench strength."),
    ("change", "Управление изменениями",
     "Управление изменениями. Какие барьеры сняли, как мерили эффект."),
]

def is_empty_faq_answer(text: str) -> bool:
    """
    Распознаём «пустые» ответы (нет фактов в резюме).
    Усилен набор эвристик + регэкспы.
    """
    if not text or not text.strip():
        return True
    t = text.lower().replace("ё", "е")
    patterns = [
        r"нет\s+(конкретной\s+)?информаци",
        r"в\s+(резюме|контексте|фрагмент(ах)?|предоставленных\s+фрагмент(ах)?)\s+нет",
        r"не\s+наш(лось|елось)",
        r"не\s+найдено",
        r"данных\s+нет",
        r"не\s+содержится",
        r"контекст\s+не\s+найден",
        r"релевантн(ых|ые)\s+фрагмент",
        r"контекст\s+из\s+резюме\s+не\s+найден",
        r"в\s+предоставленных\s+фрагмент(ах)?\s+резюме\s+нет\s+конкретн(ой|ых)\s+информаци",
    ]
    return any(re.search(p, t) for p in patterns)

def extract_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        reader = PdfReader(path)
        pages = [(p.extract_text() or "") for p in reader.pages]
        return "\n".join(pages)
    elif ext in (".docx", ".doc"):
        return docx2txt.process(path) or ""
    else:
        raise ValueError(f"Неподдерживаемый формат резюме: {ext}")

def chunk_text(text: str, tokens_per_chunk: int = 300, overlap: int = 40) -> List[str]:
    enc = tiktoken.get_encoding("cl100k_base")
    toks = enc.encode(text)
    chunks = []
    start = 0
    step = max(1, tokens_per_chunk - overlap)
    while start < len(toks):
        end = start + tokens_per_chunk
        chunk = enc.decode(toks[start:end])
        chunk = " ".join(chunk.split())
        if chunk.strip():
            chunks.append(chunk)
        start += step
    return chunks

def build_embeddings(texts: List[str]) -> np.ndarray:
    client = OpenAI(api_key=settings.openai_api_key)
    embs = []
    batch = 64
    for i in range(0, len(texts), batch):
        part = texts[i:i+batch]
        resp = client.embeddings.create(model=settings.embedding_model, input=part)
        embs.extend([d.embedding for d in resp.data])
    return np.array(embs, dtype="float32")

def _chat_completion(messages: list) -> str:
    client = OpenAI(api_key=settings.openai_api_key)
    resp = client.chat.completions.create(
        model=settings.openai_model,
        messages=messages,
        temperature=settings.temperature
    )
    return resp.choices[0].message.content

def _about_instruction() -> str:
    return (
        "Составь 4–6 предложений самопрезентации кандидата на основе фрагментов резюме. "
        "Подчеркни ключевые навыки, опыт и достижения. Если чего-то нет — не придумывай. "
        "В конце добавь: «Полное резюме можно получить командой /resume»."
    )

def _faq_style_instruction(topic_label: str) -> str:
    return (
        f"Ответь на HR-вопрос: «{topic_label}».\n"
        "Формат: 3–6 кратких буллетов. "
        "Включай конкретные цифры/масштабы из фрагментов (%, $, сроки, люди, география). "
        "Если информации в контексте нет — явно скажи об этом. Не придумывай. "
        "При необходимости предложи команду /resume."
    )

def build_and_save_cache():
    """Генерируем кэш: about_cache.txt и faq_cache.json (только содержательные темы)."""
    os.makedirs("data", exist_ok=True)

    # ABOUT
    about_msgs = build_messages("Краткая самопрезентация кандидата")
    about_msgs[-1]["content"] += "\n\n" + _about_instruction()
    about_text = _chat_completion(about_msgs).rstrip() + "\n\n" + CTA
    with open("data/about_cache.txt", "w", encoding="utf-8") as f:
        f.write(about_text)

    # FAQ
    print(f"[CACHE] Генерирую FAQ (всего тем: {len(FAQ_TOPICS)})…")
    topics_out = []
    for i, (key, label, full_q) in enumerate(FAQ_TOPICS, start=1):
        ctx = rag_retrieve(full_q)
        if not ctx:
            print(f"  {i:02d}. {label}: пропущено (нет контекста)")
            continue

        msgs = build_messages(full_q)
        msgs[-1]["content"] += "\n\n" + _faq_style_instruction(label)
        reply = _chat_completion(msgs).strip()

        if is_empty_faq_answer(reply):
            print(f"  {i:02d}. {label}: пропущено (пустой ответ)")
            continue

        reply = reply + "\n\n" + CTA
        topics_out.append({"key": key, "label": label, "full": full_q, "reply": reply})
        print(f"  {i:02d}. {label}: OK")

    with open("data/faq_cache.json", "w", encoding="utf-8") as f:
        json.dump({"topics": topics_out}, f, ensure_ascii=False, indent=2)

    print(f"[CACHE] Готово: тем в FAQ — {len(topics_out)}")

def main():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(settings.resume_path):
        raise FileNotFoundError(f"Файл резюме не найден: {settings.resume_path}")

    # 1) Извлечь текст
    text = extract_text(settings.resume_path)
    if not text.strip():
        raise RuntimeError("Не удалось извлечь текст из резюме. Проверьте файл.")

    with open(settings.resume_txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    # 2) Разбить на чанки и построить эмбеддинги
    chunks = chunk_text(text, tokens_per_chunk=300, overlap=40)
    print(f"Число чанков: {len(chunks)}")

    embeds = build_embeddings(chunks)
    np.savez(settings.index_path, embeddings=embeds)
    with open(settings.meta_path, "w", encoding="utf-8") as f:
        json.dump({"chunks": chunks}, f, ensure_ascii=False, indent=2)

    print("Индекс готов: data/index.npz, data/index_meta.json")

    # 3) Построить и сохранить кэш
    build_and_save_cache()

if __name__ == "__main__":
    main()
