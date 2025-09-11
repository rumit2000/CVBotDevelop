# rag.py
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Dict, Tuple, Optional

import numpy as np
from openai import OpenAI


DATA_DIR = Path("data")
INDEX_DIR = DATA_DIR / "rag_index"
EMBEDDINGS_FILE = INDEX_DIR / "embeddings.npy"
CHUNKS_FILE = INDEX_DIR / "chunks.jsonl"
META_FILE = INDEX_DIR / "meta.json"

# Модели по умолчанию можно переопределить переменными окружения
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
# компактная и дешевая — если хочешь потолще, поставь text-embedding-3-large
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")


@dataclass
class Chunk:
    id: str
    source: str
    page: Optional[int]
    text: str


# ----------------------------
# Вспомогалки
# ----------------------------

def _ensure_dirs():
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _clean_text(txt: str) -> str:
    # лёгкая чистка PDF-текста
    txt = txt.replace("\r", "\n")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _split_into_chunks(
    text: str,
    max_chars: int = 1200,
    overlap: int = 200,
) -> List[str]:
    """
    Грубая, но практичная нарезка: сначала по пустым строкам,
    затем склейка в куски до max_chars с overlap.
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    buff: List[str] = []
    size = 0

    def flush():
        nonlocal buff, size
        if buff:
            chunks.append("\n".join(buff).strip())
            buff = []
            size = 0

    for p in paras:
        # если параграф очень большой — ещё дробим по предложениям
        if len(p) > max_chars:
            sents = re.split(r"(?<=[.!?])\s+", p)
            for s in sents:
                if size + len(s) + 1 > max_chars and size > 0:
                    flush()
                buff.append(s)
                size += len(s) + 1
        else:
            if size + len(p) + 1 > max_chars and size > 0:
                flush()
            buff.append(p)
            size += len(p) + 1

    flush()

    # Добавим overlap (символьный) при сборке финального списка
    if overlap <= 0 or not chunks:
        return chunks

    overlapped: List[str] = []
    for i, ch in enumerate(chunks):
        if i == 0:
            overlapped.append(ch)
        else:
            prev_tail = chunks[i - 1][-overlap:]
            overlapped.append((prev_tail + "\n" + ch).strip())

    return overlapped


def _extract_text_from_pdf(path: Path) -> List[Tuple[int, str]]:
    """
    Возвращает список (page_number, text). Нумерация страниц с 1.
    """
    from pypdf import PdfReader  # лёгкая зависимость

    reader = PdfReader(str(path))
    pages: List[Tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        txt = page.extract_text() or ""
        pages.append((i, _clean_text(txt)))
    return pages


def _batched(iterable: Iterable, batch_size: int):
    batch = []
    for x in iterable:
        batch.append(x)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    # ожидается, что векторы уже L2-нормированы => косинус = матричный dot
    return a @ b.T


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12
    return mat / norms


def _load_index() -> Tuple[np.ndarray, List[Chunk], Dict]:
    if not (EMBEDDINGS_FILE.exists() and CHUNKS_FILE.exists() and META_FILE.exists()):
        return np.empty((0, 0), dtype=np.float32), [], {}

    embs = np.load(EMBEDDINGS_FILE).astype(np.float32)
    chunks: List[Chunk] = []
    with open(CHUNKS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            chunks.append(Chunk(**obj))
    with open(META_FILE, "r", encoding="utf-8") as f:
        meta = json.load(f)
    return embs, chunks, meta


def _save_index(embs: np.ndarray, chunks: List[Chunk], meta: Dict):
    _ensure_dirs()
    np.save(EMBEDDINGS_FILE, embs.astype(np.float32))
    with open(CHUNKS_FILE, "w", encoding="utf-8") as f:
        for ch in chunks:
            f.write(json.dumps(ch.__dict__, ensure_ascii=False) + "\n")
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _embed_texts(texts: List[str], model: str = EMBEDDING_MODEL) -> np.ndarray:
    client = OpenAI()
    out_vecs: List[np.ndarray] = []
    # без фанатизма — батчи по 128
    for batch in _batched(texts, 128):
        resp = client.embeddings.create(model=model, input=batch)
        vecs = [np.array(d.embedding, dtype=np.float32) for d in resp.data]
        out_vecs.append(np.stack(vecs, axis=0))
    embs = np.vstack(out_vecs)
    return _l2_normalize(embs)


# ----------------------------
# Публичное API
# ----------------------------

def ingest(
    sources: Iterable[str] | str,
    *,
    chunk_size: int = 1200,
    chunk_overlap: int = 200,
    embedding_model: Optional[str] = None,
) -> Dict:
    """
    Построить/перестроить индекс из PDF/TXT-файлов.
    Сохраняет:
      - data/rag_index/embeddings.npy
      - data/rag_index/chunks.jsonl
      - data/rag_index/meta.json
    """
    _ensure_dirs()
    if isinstance(sources, str):
        sources = [sources]

    all_chunks: List[Chunk] = []
    for src in sources:
        path = Path(src)
        if not path.exists():
            print(f"[RAG] WARNING: source not found: {path}")
            continue

        if path.suffix.lower() in [".pdf"]:
            pages = _extract_text_from_pdf(path)
            for page_num, page_text in pages:
                for i, piece in enumerate(_split_into_chunks(page_text, chunk_size, chunk_overlap)):
                    ch_id = f"{path.name}-p{page_num}-c{i+1}"
                    all_chunks.append(Chunk(id=ch_id, source=str(path), page=page_num, text=piece))
        elif path.suffix.lower() in [".txt", ".md"]:
            txt = _clean_text(path.read_text(encoding="utf-8"))
            for i, piece in enumerate(_split_into_chunks(txt, chunk_size, chunk_overlap)):
                ch_id = f"{path.name}-c{i+1}"
                all_chunks.append(Chunk(id=ch_id, source=str(path), page=None, text=piece))
        else:
            print(f"[RAG] WARNING: unsupported file type: {path.suffix} ({path})")

    if not all_chunks:
        # если нечего индексировать — сохраним пустоту
        _save_index(np.empty((0, 0), dtype=np.float32), [], {
            "embedding_model": embedding_model or EMBEDDING_MODEL,
            "built_at": int(time.time()),
            "size": 0,
            "dim": 0,
        })
        return {"chunks": 0, "dim": 0}

    texts = [c.text for c in all_chunks]
    embs = _embed_texts(texts, embedding_model or EMBEDDING_MODEL)
    meta = {
        "embedding_model": embedding_model or EMBEDDING_MODEL,
        "built_at": int(time.time()),
        "size": int(embs.shape[0]),
        "dim": int(embs.shape[1]),
        "sources": sorted({c.source for c in all_chunks}),
    }
    _save_index(embs, all_chunks, meta)
    print(f"[RAG] built: {meta['size']} chunks, dim={meta['dim']}")
    return {"chunks": meta["size"], "dim": meta["dim"]}


def retrieve(
    query: str,
    *,
    top_k: int = 6,
) -> List[Dict]:
    """
    Возвращает top-k сниппетов: [{score, id, source, page, text}, ...]
    """
    embs, chunks, meta = _load_index()
    if embs.size == 0:
        return []

    q = _embed_texts([query])[0:1, :]
    sims = _cosine_sim(q, embs)[0]  # (N,)
    idx = np.argpartition(sims, -top_k)[-top_k:]
    # сортируем по похожести
    idx = idx[np.argsort(-sims[idx])]
    out = []
    for i in idx:
        ch = chunks[int(i)]
        out.append({
            "score": float(sims[int(i)]),
            "id": ch.id,
            "source": ch.source,
            "page": ch.page,
            "text": ch.text,
        })
    return out


def build_messages(
    user_query: str,
    *,
    top_k: int = 6,
    system_prompt: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Собирает сообщения для chat.completions: system + user (включая RAG-контекст).
    Используй как:
        msgs = build_messages("Во что я силен?")
        client = OpenAI()
        client.chat.completions.create(model="gpt-4o-mini", messages=msgs)
    """
    hits = retrieve(user_query, top_k=top_k)

    if not system_prompt:
        system_prompt = (
            "Ты — ассистент по резюме Тимура Асяева. Отвечай кратко, на русском. "
            "Используй только предоставленные сниппеты контекста; если ответа нет — честно скажи, что в резюме это не указано. "
            "Если вопрос про опыт/навыки — структурируй ответ маркированными пунктами."
        )

    header = "Контекст (фрагменты резюме):\n"
    ctx_lines = []
    for i, h in enumerate(hits, start=1):
        loc = f"{Path(h['source']).name}"
        if h.get("page"):
            loc += f", стр. {h['page']}"
        ctx_lines.append(f"[{i}] {loc} (score={h['score']:.3f})\n{h['text']}\n")

    user_block = (
        f"{header}"
        + "\n---\n".join(ctx_lines)
        + "\n\nВопрос: " + user_query.strip()
        + "\n\nОтвечай, опираясь на эти фрагменты."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_block},
    ]


# совместимость с прежним импортом: from rag import build_messages, retrieve as rag_retrieve
rag_retrieve = retrieve


def dump_all_text() -> str:
    """
    Для служебных задач (суммаризация в ingestion): вернуть весь склеенный текст корпуса.
    """
    _, chunks, _ = _load_index()
    return "\n\n".join(c.text for c in chunks)
