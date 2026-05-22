from __future__ import annotations

import json
import re
from typing import Dict, List
from urllib import error as urllib_error
from urllib import request as urllib_request

from sqlalchemy.orm import Session, joinedload

from . import crud, models

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\u00C0-\u1EF9]+", re.U)
_NON_WORD_RE = re.compile(r"[^a-zA-Z0-9\u00C0-\u1EF9]+", re.U)


def _tokenize(text: str) -> set:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _score_similarity(query: str, candidate: str) -> float:
    q = _tokenize(query)
    c = _tokenize(candidate)
    if not q or not c:
        return 0.0
    return len(q.intersection(c)) / max(len(q.union(c)), 1)


def _normalize_text(text: str) -> str:
    return _NON_WORD_RE.sub(" ", (text or "").lower()).strip()


def _find_best_class_name_match(db: Session, question: str) -> str | None:
    q_norm = _normalize_text(question)
    classes = db.query(models.Class).order_by(models.Class.name.asc(), models.Class.id.asc()).all()
    if not classes:
        return None

    best_name = None
    best_score = 0.0
    for c in classes:
        class_name = (c.name or "").strip()
        if not class_name:
            continue
        class_norm = _normalize_text(class_name)
        # Ưu tiên match chứa trực tiếp tên lớp sau khi chuẩn hóa.
        if class_norm and class_norm in q_norm:
            return class_name
        score = _score_similarity(q_norm, class_norm)
        if score > best_score:
            best_score = score
            best_name = class_name

    return best_name if best_score >= 0.35 else None


def _count_students_in_class(db: Session, class_name: str) -> int:
    class_row = db.query(models.Class).filter(models.Class.name == class_name).first()
    if not class_row:
        return 0
    return (
        db.query(models.User)
        .filter(models.User.class_id == class_row.id, models.User.role == "student")
        .count()
    )


def _direct_class_count_reply(db: Session, question: str) -> str | None:
    q = (question or "").lower()
    asks_count = any(k in q for k in ["bao nhiêu sinh viên", "sĩ số", "số lượng sinh viên"])
    if not asks_count:
        return None

    matched_class = _find_best_class_name_match(db, question)
    if not matched_class:
        return None

    total = _count_students_in_class(db, matched_class)
    return f"Số liệu hiện tại: lớp {matched_class} có {total} sinh viên."


def _direct_student_class_reply(db: Session, question: str) -> str | None:
    q = (question or "").lower()
    asks_student_class = any(k in q for k in ["thuộc lớp nào", "học lớp nào"])
    if not asks_student_class:
        return None

    id_match = re.search(r"(?:user_id|id)\s*(\d+)", q)
    if id_match:
        uid = int(id_match.group(1))
        user = (
            db.query(models.User)
            .options(joinedload(models.User.class_))
            .filter(models.User.id == uid, models.User.role == "student")
            .first()
        )
        if not user:
            return f"Không tìm thấy sinh viên với id {uid}."
        display_name = (user.fullname or "").strip() or user.username
        class_name = user.class_.name if user.class_ else "Chưa gán lớp"
        return f"Số liệu hiện tại: sinh viên {display_name} (id {uid}) thuộc lớp {class_name}."

    # Tìm theo fullname hoặc username nằm trong câu hỏi.
    candidates = (
        db.query(models.User)
        .options(joinedload(models.User.class_))
        .filter(models.User.role == "student")
        .all()
    )
    q_norm = _normalize_text(question)
    hits = []
    for user in candidates:
        fullname = (user.fullname or "").strip()
        username = (user.username or "").strip()
        keys = [_normalize_text(fullname), _normalize_text(username)]
        if any(k and k in q_norm for k in keys):
            hits.append(user)

    if len(hits) == 1:
        user = hits[0]
        display_name = (user.fullname or "").strip() or user.username
        class_name = user.class_.name if user.class_ else "Chưa gán lớp"
        return f"Số liệu hiện tại: sinh viên {display_name} (id {user.id}) thuộc lớp {class_name}."

    if len(hits) > 1:
        lines = []
        for user in hits[:8]:
            display_name = (user.fullname or "").strip() or user.username
            class_name = user.class_.name if user.class_ else "Chưa gán lớp"
            lines.append(f"- {display_name} (id {user.id}): {class_name}")
        return "Tìm thấy nhiều sinh viên khớp tên, vui lòng chỉ rõ id.\n" + "\n".join(lines)

    return None


def _infer_question_scope(question: str) -> str:
    q = (question or "").lower()

    survey_keywords = [
        "khảo sát", "survey", "phiếu", "bảng hỏi", "câu hỏi", "lựa chọn", "option",
        "survey_text", "survey response", "trả lời khảo sát",
    ]
    feedback_keywords = [
        "đánh giá môn", "đánh giá học phần", "feedback", "phản hồi môn", "môn học",
        "học phần", "subject", "sentiment", "tiêu cực", "tích cực",
    ]
    class_student_keywords = [
        "lớp", "class", "sĩ số", "bao nhiêu sinh viên", "số lượng sinh viên",
        "thuộc lớp nào", "học lớp nào", "student", "sinh viên",
    ]

    has_survey = any(k in q for k in survey_keywords)
    has_feedback = any(k in q for k in feedback_keywords)
    has_class_student = any(k in q for k in class_student_keywords)

    if has_class_student and not has_survey and not has_feedback:
        return "class_student"

    if has_survey and not has_feedback:
        return "survey"
    if has_feedback and not has_survey:
        return "feedback"
    return "mixed"


def _build_overview_snippets(db: Session) -> List[str]:
    total_students = db.query(models.User).filter(models.User.role == "student").count()
    total_classes = db.query(models.Class).count()
    total_feedbacks = db.query(models.Feedback).count()
    total_surveys = db.query(models.Survey).count()
    total_survey_text_responses = db.query(models.SurveyTextResponse).count()
    total_survey_option_responses = db.query(models.SurveyResponse).count()

    return [
        "Thống kê tách bạch hiện tại: "
        f"{total_students} sinh viên | "
        f"{total_classes} lớp | "
        f"Đánh giá môn học: {total_feedbacks} bản ghi feedback học phần | "
        f"Khảo sát: {total_surveys} khảo sát, "
        f"{total_survey_option_responses} phản hồi trắc nghiệm, "
        f"{total_survey_text_responses} phản hồi tự do."
    ]


def _build_class_student_snippets(db: Session) -> List[str]:
    snippets: List[str] = []

    classes = (
        db.query(models.Class)
        .options(joinedload(models.Class.students))
        .order_by(models.Class.name.asc(), models.Class.id.asc())
        .all()
    )

    for c in classes:
        students = [u for u in (c.students or []) if (u.role or "").lower() == "student"]
        snippets.append(
            f"Sĩ số lớp: lớp {c.name} có {len(students)} sinh viên."
        )

    students = (
        db.query(models.User)
        .options(joinedload(models.User.class_))
        .filter(models.User.role == "student")
        .order_by(models.User.id.desc())
        .all()
    )
    for s in students:
        display_name = (s.fullname or "").strip() or s.username
        class_name = s.class_.name if s.class_ else "Chưa gán lớp"
        snippets.append(
            "Thông tin lớp sinh viên: "
            f"{display_name} (username: {s.username}, user_id: {s.id}) thuộc lớp {class_name}."
        )

    return snippets


def _build_feedback_snippets(db: Session) -> List[str]:
    snippets: List[str] = []

    stats = crud.get_stats(db)
    snippets.append(
        "Tổng quan ĐÁNH GIÁ MÔN HỌC (Feedback học phần): "
        f"tổng {stats.get('total', 0)}, "
        f"POS {stats.get('POS', 0)}, "
        f"NEG {stats.get('NEG', 0)}, "
        f"tỷ lệ NEG {stats.get('NEG_rate', 0)}."
    )

    recent_feedbacks = (
        db.query(models.Feedback)
        .options(joinedload(models.Feedback.user), joinedload(models.Feedback.subject))
        .order_by(models.Feedback.created_at.desc(), models.Feedback.id.desc())
        .limit(25)
        .all()
    )
    for fb in recent_feedbacks:
        student_name = fb.user.fullname if fb.user else "Không rõ"
        subject_name = fb.subject.name if fb.subject else f"Môn #{fb.subject_id}"
        snippets.append(
            "Đánh giá môn học gần đây: "
            f"sinh viên {student_name}, học phần {subject_name}, "
            f"nhãn {fb.label}, nội dung: {fb.text}"
        )

    return snippets


def _build_survey_snippets(db: Session) -> List[str]:
    snippets: List[str] = []

    recent_surveys = (
        db.query(models.Survey)
        .options(joinedload(models.Survey.class_))
        .order_by(models.Survey.created_at.desc(), models.Survey.id.desc())
        .limit(15)
        .all()
    )
    for survey in recent_surveys:
        class_name = survey.class_.name if survey.class_ else f"Lớp #{survey.class_id}"
        option_count = (
            db.query(models.SurveyResponse)
            .filter(models.SurveyResponse.survey_id == survey.id)
            .count()
        )
        text_count = (
            db.query(models.SurveyTextResponse)
            .filter(models.SurveyTextResponse.survey_id == survey.id)
            .count()
        )
        snippets.append(
            "Khảo sát gần đây: "
            f"{survey.title} | lớp: {class_name} | published: {bool(survey.is_published)} | "
            f"số phản hồi trắc nghiệm: {option_count} | phản hồi tự do: {text_count}"
        )

    recent_survey_texts = (
        db.query(models.SurveyTextResponse)
        .order_by(models.SurveyTextResponse.created_at.desc(), models.SurveyTextResponse.id.desc())
        .limit(25)
        .all()
    )
    survey_title_map: Dict[int, str] = {
        row.id: row.title
        for row in db.query(models.Survey.id, models.Survey.title).all()
    }
    for row in recent_survey_texts:
        survey_title = survey_title_map.get(row.survey_id, f"Khảo sát #{row.survey_id}")
        snippets.append(
            "Phản hồi tự do của KHẢO SÁT gần đây: "
            f"{survey_title} | user #{row.user_id} | nội dung: {(row.response_text or '').strip()}"
        )

    return snippets


def _build_news_snippets(db: Session) -> List[str]:
    snippets: List[str] = []

    recent_news = (
        db.query(models.NewsPost)
        .order_by(models.NewsPost.created_at.desc(), models.NewsPost.id.desc())
        .limit(15)
        .all()
    )
    for post in recent_news:
        snippets.append(
            "Tin tức gần đây: "
            f"{(post.title or '').strip()} | {(post.content or '').strip()[:180]}"
        )

    return snippets

def retrieve_admin_context(db: Session, question: str, top_k: int = 10) -> List[str]:
    scope = _infer_question_scope(question)
    overview = _build_overview_snippets(db)
    class_student_candidates = _build_class_student_snippets(db)
    feedback_candidates = _build_feedback_snippets(db)
    survey_candidates = _build_survey_snippets(db)
    news_candidates = _build_news_snippets(db)

    if scope == "class_student":
        candidates = overview + class_student_candidates
    elif scope == "survey":
        candidates = overview + survey_candidates
    elif scope == "feedback":
        candidates = overview + feedback_candidates
    else:
        # Câu hỏi hỗn hợp/tổng quan: giữ đủ ngữ cảnh nhưng vẫn có nhãn tách bạch.
        candidates = overview + class_student_candidates + feedback_candidates + survey_candidates + news_candidates

    scored = [(_score_similarity(question, c), c) for c in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)

    selected = [c for score, c in scored if score > 0][:top_k]
    if not selected:
        selected = candidates[: min(top_k, len(candidates))]
    return selected


def _call_groq_chat(messages: List[Dict[str, str]], api_key: str, model: str, max_tokens: int) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
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
            "User-Agent": "StudentFeedbackSystem/admin-rag-chatbot",
            "Referer": "http://localhost:8000",
        },
    )

    try:
        with urllib_request.urlopen(req, timeout=35) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Groq API error {exc.code}: {error_body}") from exc
    except Exception as exc:
        raise RuntimeError(f"Cannot call Groq API: {exc}") from exc

    choices = response_data.get("choices") or []
    if not choices:
        raise RuntimeError("Groq API returned no content")

    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        raise RuntimeError("Groq API returned empty content")
    return content


def build_admin_rag_reply(
    db: Session,
    question: str,
    api_key: str,
    model: str,
    max_tokens: int = 900,
) -> str:
    if not api_key:
        raise RuntimeError("Missing ADMIN_RAG_CHATBOT_GROQ_API_KEY")

    # Các câu hỏi tra cứu dữ liệu lớp/sinh viên trả thẳng từ DB để tránh sai số do sinh ngôn ngữ.
    direct_reply = _direct_class_count_reply(db, question) or _direct_student_class_reply(db, question)
    if direct_reply:
        return direct_reply

    context_snippets = retrieve_admin_context(db, question, top_k=10)
    context_block = "\n".join([f"- {item}" for item in context_snippets])

    system_instruction = (
        "Bạn là trợ lý quản trị hệ thống phản hồi sinh viên. "
        "Bạn được phép trả lời dựa trên dữ liệu trong context RAG từ database. "
        "Phân biệt nghiêm ngặt hai khái niệm: "
        "(1) Đánh giá môn học = feedback học phần trong bảng feedbacks, "
        "(2) Khảo sát = dữ liệu survey/survey_responses/survey_text_responses. "
        "Không được gộp số liệu giữa hai nhóm nếu câu hỏi chỉ hỏi một nhóm. "
        "Trả lời tiếng Việt rõ ràng, có cấu trúc. "
        "Nếu câu hỏi vượt ngoài dữ liệu, hãy nói rõ phần nào không có trong context."
    )

    user_prompt = (
        "Context RAG từ database:\n"
        f"{context_block}\n\n"
        "Câu hỏi của admin:\n"
        f"{question.strip()}\n\n"
        "Hãy trả lời ngắn gọn, ưu tiên số liệu và tóm tắt hành động đề xuất nếu cần."
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
