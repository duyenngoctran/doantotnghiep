from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

from sqlalchemy.orm import Session

from . import models

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\u00C0-\u1EF9]+", re.U)


def _tokenize(text: str) -> set:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _score_similarity(query: str, candidate: str) -> float:
    q = _tokenize(query)
    c = _tokenize(candidate)
    if not q or not c:
        return 0.0
    inter = len(q.intersection(c))
    union = len(q.union(c))
    return inter / max(union, 1)


def retrieve_student_feedback_context(
    db: Session,
    user_id: int,
    latest_feedback_text: str,
    top_k: int = 4,
) -> List[str]:
    """Simple RAG retrieval from the same student's latest feedback history."""
    rows = (
        db.query(models.Feedback)
        .filter(models.Feedback.user_id == user_id)
        .order_by(models.Feedback.created_at.desc(), models.Feedback.id.desc())
        .limit(120)
        .all()
    )

    scored: List[Tuple[float, str]] = []
    for row in rows:
        text = (row.text or "").strip()
        if not text:
            continue
        score = _score_similarity(latest_feedback_text, text)
        if score <= 0:
            continue
        scored.append((score, text))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Deduplicate near-identical snippets and keep top-k.
    seen = set()
    selected: List[str] = []
    for _, text in scored:
        key = text.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        selected.append(text)
        if len(selected) >= top_k:
            break

    if not selected:
        selected = [latest_feedback_text.strip()]

    return selected


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
            "User-Agent": "StudentFeedbackSystem/negative-support-rag",
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


def build_negative_support_reply(
    db: Session,
    user_id: int,
    latest_feedback_text: str,
    api_key: str,
    model: str,
    max_tokens: int = 260,
) -> str:
    """
    Build a supportive reply with simple RAG grounding from this student's past feedback.
    """
    if not api_key:
        raise RuntimeError("Missing NEGATIVE_SUPPORT_GROQ_API_KEY")

    context_snippets = retrieve_student_feedback_context(db, user_id, latest_feedback_text, top_k=4)
    context_block = "\n".join([f"- {snippet}" for snippet in context_snippets])

    system_instruction = (
        "Bạn là chatbot hỗ trợ cảm xúc cho sinh viên. "
        "Giọng điệu đồng cảm, không phán xét, ngắn gọn ấm áp. "
        "Trả lời tiếng Việt tự nhiên, tối đa 120 từ. "
        "Luôn gồm: (1) công nhận cảm xúc, (2) 2-3 bước hỗ trợ cụ thể ngay hôm nay, "
        "(3) 1 câu khích lệ kết thúc."
    )

    user_prompt = (
        "Đây là phản hồi tiêu cực mới nhất của sinh viên:\n"
        f"\"{latest_feedback_text.strip()}\"\n\n"
        "Ngữ cảnh RAG từ lịch sử phản hồi liên quan của cùng sinh viên:\n"
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
