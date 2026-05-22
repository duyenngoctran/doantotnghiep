from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from . import models

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
EMBEDDING_MODEL_NAME = os.getenv(
    "NEGATIVE_SUPPORT_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
).strip()
EMBEDDING_DIM = int(os.getenv("NEGATIVE_SUPPORT_EMBEDDING_DIM", "384"))

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\u00C0-\u1EF9]+", re.U)


@lru_cache(maxsize=1)
def _get_embedding_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(EMBEDDING_MODEL_NAME)


def _embed_text(text_value: str) -> List[float]:
    model = _get_embedding_model()
    vectors = model.encode([text_value or ""], normalize_embeddings=True)
    vector = vectors[0].tolist()
    if len(vector) != EMBEDDING_DIM:
        raise RuntimeError(
            f"Embedding dim mismatch: expected {EMBEDDING_DIM}, got {len(vector)}"
        )
    return vector


def _vector_to_sql_literal(vec: List[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def init_negative_support_vector_store(engine: Engine) -> None:
    """Best-effort init for pgvector store used by negative support RAG."""
    if not str(engine.url).startswith("postgresql"):
        return

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS negative_support_feedback_vectors (
                    feedback_id BIGINT PRIMARY KEY REFERENCES feedbacks(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL,
                    subject_id BIGINT,
                    feedback_text TEXT NOT NULL,
                    embedding vector({EMBEDDING_DIM}) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_neg_support_vectors_user
                ON negative_support_feedback_vectors(user_id)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_neg_support_vectors_embedding
                ON negative_support_feedback_vectors
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
                """
            )
        )


def upsert_feedback_embedding(
    db: Session,
    feedback_id: int,
    user_id: int,
    subject_id: Optional[int],
    feedback_text: str,
) -> None:
    if not str(db.bind.url).startswith("postgresql"):
        return

    try:
        vec = _embed_text(feedback_text)
        vec_literal = _vector_to_sql_literal(vec)
        db.execute(
            text(
                """
                INSERT INTO negative_support_feedback_vectors (
                    feedback_id, user_id, subject_id, feedback_text, embedding
                ) VALUES (
                    :feedback_id, :user_id, :subject_id, :feedback_text, CAST(:embedding AS vector)
                )
                ON CONFLICT (feedback_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    subject_id = EXCLUDED.subject_id,
                    feedback_text = EXCLUDED.feedback_text,
                    embedding = EXCLUDED.embedding
                """
            ),
            {
                "feedback_id": int(feedback_id),
                "user_id": int(user_id),
                "subject_id": int(subject_id) if subject_id is not None else None,
                "feedback_text": feedback_text,
                "embedding": vec_literal,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def _tokenize(text_value: str) -> set:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text_value or "")}


def _lexical_fallback(
    db: Session,
    user_id: int,
    latest_feedback_text: str,
    top_k: int,
    exclude_feedback_id: Optional[int] = None,
) -> List[str]:
    rows = (
        db.query(models.Feedback)
        .filter(models.Feedback.user_id == user_id)
        .order_by(models.Feedback.created_at.desc(), models.Feedback.id.desc())
        .limit(120)
        .all()
    )

    q_tokens = _tokenize(latest_feedback_text)
    scored: List[tuple] = []
    for row in rows:
        if exclude_feedback_id is not None and row.id == exclude_feedback_id:
            continue
        text_value = (row.text or "").strip()
        if not text_value:
            continue
        c_tokens = _tokenize(text_value)
        if not q_tokens or not c_tokens:
            continue
        inter = len(q_tokens.intersection(c_tokens))
        union = len(q_tokens.union(c_tokens))
        score = inter / max(union, 1)
        if score > 0:
            scored.append((score, text_value))

    scored.sort(key=lambda x: x[0], reverse=True)
    selected: List[str] = []
    seen = set()
    for _, value in scored:
        key = value.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        selected.append(value)
        if len(selected) >= top_k:
            break

    if not selected:
        selected = [latest_feedback_text.strip()]
    return selected


def retrieve_student_feedback_context_vector(
    db: Session,
    user_id: int,
    latest_feedback_text: str,
    top_k: int = 4,
    exclude_feedback_id: Optional[int] = None,
) -> List[str]:
    if not str(db.bind.url).startswith("postgresql"):
        return _lexical_fallback(
            db=db,
            user_id=user_id,
            latest_feedback_text=latest_feedback_text,
            top_k=top_k,
            exclude_feedback_id=exclude_feedback_id,
        )

    try:
        query_vec = _embed_text(latest_feedback_text)
        query_vec_literal = _vector_to_sql_literal(query_vec)
        rows = db.execute(
            text(
                """
                SELECT feedback_text
                FROM negative_support_feedback_vectors
                WHERE user_id = :user_id
                  AND (:exclude_feedback_id IS NULL OR feedback_id <> :exclude_feedback_id)
                ORDER BY embedding <=> CAST(:query_vec AS vector)
                LIMIT :top_k
                """
            ),
            {
                "user_id": int(user_id),
                "exclude_feedback_id": int(exclude_feedback_id)
                if exclude_feedback_id is not None
                else None,
                "query_vec": query_vec_literal,
                "top_k": int(top_k),
            },
        ).mappings().all()

        selected = []
        seen = set()
        for row in rows:
            value = (row.get("feedback_text") or "").strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            selected.append(value)

        if not selected:
            selected = [latest_feedback_text.strip()]
        return selected
    except Exception:
        db.rollback()
        return _lexical_fallback(
            db=db,
            user_id=user_id,
            latest_feedback_text=latest_feedback_text,
            top_k=top_k,
            exclude_feedback_id=exclude_feedback_id,
        )


def _call_groq_chat(messages: List[Dict[str, str]], api_key: str, model: str, max_tokens: int) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.35,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        GROQ_API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "StudentFeedbackSystem/negative-support-vector-rag",
            "Referer": "http://localhost:8000",
        },
    )

    try:
        with urllib_request.urlopen(req, timeout=30) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Groq API error {exc.code}: {error_body}") from exc
    except Exception as exc:
        raise RuntimeError(f"Cannot call Groq API: {exc}") from exc

    choices = response_data.get("choices") or []
    if not choices:
        raise RuntimeError("Groq API returned no choices")
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        raise RuntimeError("Groq API returned empty content")
    return content


def build_negative_support_reply_vector_rag(
    db: Session,
    user_id: int,
    latest_feedback_text: str,
    api_key: str,
    model: str,
    max_tokens: int = 260,
    exclude_feedback_id: Optional[int] = None,
) -> str:
    if not api_key:
        raise RuntimeError("Missing NEGATIVE_SUPPORT_GROQ_API_KEY")

    snippets = retrieve_student_feedback_context_vector(
        db=db,
        user_id=user_id,
        latest_feedback_text=latest_feedback_text,
        top_k=4,
        exclude_feedback_id=exclude_feedback_id,
    )
    context_block = "\n".join([f"- {s}" for s in snippets])

    system_instruction = (
        "Bạn là chatbot hỗ trợ cảm xúc cho sinh viên. "
        "Giọng điệu đồng cảm, không phán xét, ngắn gọn ấm áp. "
        "Trả lời tiếng Việt tự nhiên, tối đa 120 từ. "
        "Luôn gồm: (1) công nhận cảm xúc, (2) 2-3 bước hỗ trợ cụ thể ngay hôm nay, "
        "(3) 1 câu khích lệ kết thúc."
    )

    user_prompt = (
        "Phản hồi tiêu cực mới nhất của sinh viên:\n"
        f"\"{latest_feedback_text.strip()}\"\n\n"
        "Ngữ cảnh RAG (retrieval vector từ lịch sử phản hồi liên quan của cùng sinh viên):\n"
        f"{context_block}\n\n"
        "Hãy hỗ trợ đúng trọng tâm vấn đề trong phản hồi mới nhất, không lan man."
    )

    return _call_groq_chat(
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_prompt},
        ],
        api_key=api_key,
        model=model,
        max_tokens=max_tokens,
    )
