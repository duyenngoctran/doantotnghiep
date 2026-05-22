# app/main.py
from fastapi import FastAPI, Depends, Form, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from passlib.context import CryptContext
from pathlib import Path
import shutil
import uuid
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Tuple
import io, csv, json, os, re, time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib import error as urllib_error
from urllib import request as urllib_request

from .database import Base, engine, get_db, DATABASE_URL, SessionLocal
from . import crud, models, sentiment_model
from .negative_support_vector_rag_chatbot import (
    build_negative_support_reply_vector_rag,
    init_negative_support_vector_store,
    upsert_feedback_embedding,
)
from .admin_rag_chatbot import build_admin_rag_reply

# ===== Groq API
from dotenv import load_dotenv
import traceback

# ======================================================
# ✅ App & DB khởi tạo
# ======================================================
Base.metadata.create_all(bind=engine)


def _ensure_news_columns():
    """Best-effort schema patch for existing DBs without migration tooling."""
    statements = [
        "ALTER TABLE news_posts ADD COLUMN media_url TEXT",
        "ALTER TABLE news_posts ADD COLUMN media_type VARCHAR(16)",
        "ALTER TABLE news_comments ADD COLUMN sentiment_label VARCHAR(8)",
        "ALTER TABLE news_comments ADD COLUMN prob_neg FLOAT",
        "ALTER TABLE news_comments ADD COLUMN prob_pos FLOAT",
        "ALTER TABLE news_comments ADD COLUMN prob_neu FLOAT",
    ]
    try:
        with engine.begin() as conn:
            for sql in statements:
                try:
                    conn.execute(text(sql))
                except Exception:
                    pass
    except Exception:
        pass


_ensure_news_columns()
try:
    init_negative_support_vector_store(engine)
except Exception:
    pass
app = FastAPI(title="🎓 Student Feedback System")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ======================================================
# ✅ Cấu hình CORS (Dev)
# ======================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Dev: cho phép tất cả domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================================================
# ✅ Mount frontend tĩnh
# ======================================================
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
UPLOADS_DIR = Path(__file__).resolve().parent.parent / "uploads"
NEWS_UPLOAD_DIR = UPLOADS_DIR / "news"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
NEWS_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


def _save_uploaded_news_media(media_file: UploadFile) -> Tuple[str, str]:
    """Lưu file media vào uploads/news và trả về (media_url, media_type)."""
    content_type = (media_file.content_type or "").lower()
    media_type = None
    if content_type.startswith("image/"):
        media_type = "image"
    elif content_type.startswith("video/"):
        media_type = "video"
    else:
        ext = Path(media_file.filename or "").suffix.lower()
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            media_type = "image"
        elif ext in {".mp4", ".webm", ".mov", ".m4v", ".avi"}:
            media_type = "video"
    if not media_type:
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ ảnh hoặc video")

    NEWS_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    original_suffix = Path(media_file.filename or "").suffix.lower()
    if not original_suffix:
        original_suffix = ".jpg" if media_type == "image" else ".mp4"

    safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", Path(media_file.filename or "media").stem)[:40] or "media"
    unique_name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_name}{original_suffix}"
    target_path = NEWS_UPLOAD_DIR / unique_name

    with target_path.open("wb") as buffer:
        shutil.copyfileobj(media_file.file, buffer)

    return f"/uploads/news/{unique_name}", media_type

@app.get("/", response_class=HTMLResponse)
def home_page():
    # hoặc: return RedirectResponse(url="/frontend/login.html", status_code=307)
    return FileResponse(FRONTEND_DIR / "login.html")

@app.get("/login.html", response_class=HTMLResponse)
def login_page():
    return FileResponse(FRONTEND_DIR / "login.html")

@app.get("/admin_dashboard.html", response_class=HTMLResponse)
def admin_dashboard_page():
    return FileResponse(FRONTEND_DIR / "admin_dashboard.html")

@app.get("/student_feedback.html", response_class=HTMLResponse)
def student_feedback_page():
    return FileResponse(FRONTEND_DIR / "student_feedback.html")


# ======================================================
# ✅ Đăng nhập
# ======================================================
@app.post("/login")
def login(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = crud.get_user_by_username(db, username)
    if not user:
        raise HTTPException(status_code=401, detail="Tài khoản không tồn tại")
    if not pwd_context.verify(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Sai mật khẩu")

    class_name = None
    if user.class_id and user.class_:
        class_name = user.class_.name

    return {
        "msg": "OK",
        "role": user.role,
        "user_id": user.id,
        "class_id": user.class_id,
        "class_name": class_name,
        "fullname": user.fullname,
    }


@app.get("/user/me/{user_id}")
def get_user_info(user_id: int, db: Session = Depends(get_db)):
    """Lấy thông tin user mới nhất"""
    user = db.query(crud.models.User).options(crud.joinedload(crud.models.User.class_)).filter(crud.models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return {
        "user_id": user.id,
        "username": user.username,
        "fullname": user.fullname,
        "role": user.role,
        "class_id": user.class_id,
        "class_name": user.class_.name if user.class_ else None,
    }


# ======================================================
# ✅ Admin: Class / Subject / Student / Feedback / Stats
# ======================================================
@app.get("/admin/classes")
def list_classes(db: Session = Depends(get_db)):
    return crud.get_all_classes(db)

@app.post("/admin/create_class")
def create_class(
    name: str = Form(...),
    homeroom_teacher_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    if crud.get_class_by_name(db, name):
        raise HTTPException(status_code=400, detail="Tên lớp đã tồn tại")
    teacher_ids = _parse_id_list(homeroom_teacher_ids)
    c = crud.create_class(db, name, teacher_ids)
    return {"msg": f"Đã tạo lớp {c.name}", "id": c.id}

@app.post("/admin/import-classes")
def import_classes(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Import classes from Excel file. Expected column: 'Tên lớp' or 'name'"""
    try:
        import pandas as pd
        
        # Đọc file Excel
        content = file.file.read()
        df = pd.read_excel(io.BytesIO(content))
        
        # Tìm cột tên lớp (có thể là "Tên lớp", "name", "Class Name")
        col_name = None
        for possible_col in ["Tên lớp", "name", "Class Name", "tên lớp"]:
            if possible_col in df.columns:
                col_name = possible_col
                break
        
        if not col_name:
            raise HTTPException(status_code=400, detail=f"File phải có cột 'Tên lớp' hoặc 'name'. Các cột hiện có: {list(df.columns)}")
        
        results = {"created": [], "skipped": [], "errors": []}
        
        for idx, row in df.iterrows():
            class_name = str(row[col_name]).strip()
            if not class_name or class_name.lower() == "nan":
                results["skipped"].append(f"Hàng {idx+2}: tên lớp trống")
                continue
            
            if crud.get_class_by_name(db, class_name):
                results["skipped"].append(f"{class_name}: đã tồn tại")
                continue
            
            try:
                c = crud.create_class(db, class_name)
                results["created"].append(class_name)
            except Exception as e:
                results["errors"].append(f"{class_name}: {str(e)}")
        
        db.commit()
        return {
            "msg": f"Import hoàn tất. Tạo: {len(results['created'])}, Bỏ qua: {len(results['skipped'])}, Lỗi: {len(results['errors'])}",
            "results": results
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Lỗi xử lý file: {str(e)}")

@app.put("/admin/classes/{class_id}")
def update_class(
    class_id: int,
    name: str = Form(...),
    homeroom_teacher_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    teacher_ids = _parse_id_list(homeroom_teacher_ids)
    c = crud.update_class(db, class_id, name, teacher_ids)
    if not c:
        raise HTTPException(status_code=404, detail="Không tìm thấy lớp")
    return {"msg": "Đã cập nhật", "id": c.id, "name": c.name}

@app.delete("/admin/classes/{class_id}")
def delete_class(class_id: int, db: Session = Depends(get_db)):
    ok, reason = crud.delete_class(db, class_id)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    return {"msg": reason}


# ========================== SEMESTER (KÌ HỌC) ==========================

@app.get("/admin/semesters")
def list_semesters(db: Session = Depends(get_db)):
    return crud.get_all_semesters(db)

@app.post("/admin/create_semester")
def create_semester(
    name: str = Form(...),
    start_date: str = Form(None),
    end_date: str = Form(None),
    db: Session = Depends(get_db),
):
    from datetime import datetime
    start = None
    end = None
    if start_date:
        try:
            start = datetime.fromisoformat(start_date)
        except:
            pass
    if end_date:
        try:
            end = datetime.fromisoformat(end_date)
        except:
            pass
    s = crud.create_semester(db, name, start, end)
    return {"msg": f"Đã tạo kì học {s.name}", "id": s.id}

@app.put("/admin/semesters/{semester_id}")
def update_semester(
    semester_id: int,
    name: str = Form(None),
    start_date: str = Form(None),
    end_date: str = Form(None),
    db: Session = Depends(get_db),
):
    from datetime import datetime
    start = None
    end = None
    if start_date:
        try:
            start = datetime.fromisoformat(start_date)
        except:
            pass
    if end_date:
        try:
            end = datetime.fromisoformat(end_date)
        except:
            pass
    s = crud.update_semester(db, semester_id, name, start, end)
    if not s:
        raise HTTPException(status_code=404, detail="Không tìm thấy kì học")
    return {"msg": "Đã cập nhật", "id": s.id}

@app.delete("/admin/semesters/{semester_id}")
def delete_semester(semester_id: int, db: Session = Depends(get_db)):
    ok, reason = crud.delete_semester(db, semester_id)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    return {"msg": reason}

@app.get("/admin/semesters/{semester_id}/subjects")
def get_subjects_by_semester(semester_id: int, db: Session = Depends(get_db)):
    """Lấy danh sách môn học theo kì học"""
    return crud.get_subjects_by_semester(db, semester_id)


@app.get("/admin/subjects")
def list_subjects(db: Session = Depends(get_db)):
    return crud.get_all_subjects(db)

@app.get("/subjects/by-class/{class_id}")
def get_subjects_for_class(class_id: int, db: Session = Depends(get_db)):
    """Lấy danh sách môn học theo lớp (cho sinh viên)"""
    return crud.get_subjects_by_class(db, class_id)

@app.post("/admin/create_subject")
def create_subject(
    name: str = Form(...),
    teacher: str = Form(""),
    teacher_ids: str = Form(""),
    class_ids: str = Form(""),  # Gửi dưới dạng string "1,2,3"
    description: str = Form(""),
    semester_id: int = Form(None),
    db: Session = Depends(get_db),
):
    # Parse class_ids từ string
    class_list = [int(x.strip()) for x in class_ids.split(",") if x.strip().isdigit()] if class_ids else []
    teacher_list = [int(x.strip()) for x in teacher_ids.split(",") if x.strip().isdigit()] if teacher_ids else []
    s = crud.create_subject(db, name, description, teacher, class_list, semester_id, teacher_list)
    return {"msg": f"Đã tạo môn {s.name}", "id": s.id}

@app.put("/admin/subjects/{subject_id}")
def update_subject(
    subject_id: int,
    name: str = Form(None),
    teacher: str = Form(None),
    teacher_ids: str = Form(None),
    class_ids: str = Form(None),  # Gửi dưới dạng string "1,2,3"
    description: str = Form(None),
    semester_id: int = Form(None),
    db: Session = Depends(get_db),
):
    # Parse class_ids từ string
    class_list = None
    if class_ids:
        class_list = [int(x.strip()) for x in class_ids.split(",") if x.strip().isdigit()]
    teacher_list = None
    if teacher_ids is not None:
        teacher_list = [int(x.strip()) for x in teacher_ids.split(",") if x.strip().isdigit()] if teacher_ids else []
    s = crud.update_subject(db, subject_id, name, teacher, teacher_list, class_list, description, semester_id)
    if not s:
        raise HTTPException(status_code=404, detail="Không tìm thấy môn học")
    return {"msg": "Đã cập nhật", "id": s.id}

@app.delete("/admin/subjects/{subject_id}")
def delete_subject(subject_id: int, db: Session = Depends(get_db)):
    ok, reason = crud.delete_subject(db, subject_id)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    return {"msg": reason}


# ========================== NEWS (TIN TỨC) ==========================

@app.get("/admin/news")
def list_news_posts(db: Session = Depends(get_db)):
    return crud.get_all_news_posts(db)


@app.get("/admin/news/comments")
def list_all_news_comments(
    limit: int = 1000,
    offset: int = 0,
    post_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    comments = crud.get_all_news_comments(db, limit=limit, offset=offset, post_id=post_id)
    return {
        "items": comments,
        "count": len(comments),
        "limit": limit,
        "offset": offset,
    }


@app.post("/admin/news")
def create_news_post(
    content: str = Form(...),
    title: str = Form(""),
    media_url: str = Form(""),
    media_type: str = Form(""),
    media_file: UploadFile = File(None),
    is_published: bool = Form(True),
    db: Session = Depends(get_db),
):
    if not content.strip():
        raise HTTPException(status_code=400, detail="Nội dung không được để trống")
    upload_media_url = None
    upload_media_type = None
    if media_file is not None and media_file.filename:
        upload_media_url, upload_media_type = _save_uploaded_news_media(media_file)
    post = crud.create_news_post(
        db,
        title=title,
        content=content,
        media_url=upload_media_url or media_url,
        media_type=upload_media_type or media_type,
        is_published=is_published,
    )
    return {"msg": "Đã tạo bài viết", "id": post.id}


@app.put("/admin/news/{post_id}")
def update_news_post(
    post_id: int,
    title: str = Form(None),
    content: str = Form(None),
    media_url: str = Form(None),
    media_type: str = Form(None),
    media_file: UploadFile = File(None),
    is_published: bool = Form(True),
    db: Session = Depends(get_db),
):
    upload_media_url = None
    upload_media_type = None
    if media_file is not None and media_file.filename:
        upload_media_url, upload_media_type = _save_uploaded_news_media(media_file)
    post = crud.update_news_post(
        db,
        post_id=post_id,
        title=title,
        content=content,
        media_url=upload_media_url or media_url,
        media_type=upload_media_type or media_type,
        is_published=is_published,
    )
    if not post:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài viết")
    return {"msg": "Đã cập nhật bài viết", "id": post.id}


@app.delete("/admin/news/{post_id}")
def delete_news_post(post_id: int, db: Session = Depends(get_db)):
    ok, reason = crud.delete_news_post(db, post_id)
    if not ok:
        raise HTTPException(status_code=404, detail=reason)
    return {"msg": reason}


@app.get("/news")
def list_published_news_posts(anon_id: Optional[str] = None, db: Session = Depends(get_db)):
    return crud.get_published_news_posts(db, anon_id=anon_id)


@app.get("/news/{post_id}/comments")
def list_news_comments(post_id: int, limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    post = crud.get_news_post_by_id(db, post_id)
    if not post or not post.is_published:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài viết")
    comments = crud.get_news_comments(db, post_id=post_id, limit=limit, offset=offset)
    return {"items": comments, "count": len(comments)}


@app.post("/news/{post_id}/like")
def toggle_news_like(
    post_id: int,
    anon_id: str = Form(...),
    db: Session = Depends(get_db),
):
    anon_id = anon_id.strip()
    if not anon_id:
        raise HTTPException(status_code=400, detail="Thiếu mã ẩn danh")
    try:
        result = crud.toggle_news_like(db, post_id=post_id, anonymous_id=anon_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return result


@app.post("/news/{post_id}/comments")
def create_news_comment(
    post_id: int,
    content: str = Form(...),
    anon_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    anon_id = (anon_id or "").strip()
    content = content.strip()
    if not anon_id:
        anon_id = f"anon-{uuid.uuid4().hex[:16]}"
    if not content:
        raise HTTPException(status_code=400, detail="Nội dung bình luận không được để trống")
    try:
        sentiment_label, prob_neg, prob_pos, prob_neu = sentiment_model.predict_sentiment_full(content)
        comment = crud.add_news_comment(
            db,
            post_id=post_id,
            anonymous_id=anon_id,
            content=content,
            sentiment_label=sentiment_label,
            prob_neg=prob_neg,
            prob_pos=prob_pos,
            prob_neu=prob_neu,
        )
        return comment
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        # Log full traceback on server and return a JSON HTTP error so frontend can parse it
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal server error")


# ========================== SURVEY (KHẢO SÁT) ==========================

@app.get("/admin/surveys")
def list_admin_surveys(db: Session = Depends(get_db)):
    return crud.get_all_surveys(db)


@app.post("/admin/surveys")
def create_admin_survey(
    title: str = Form(...),
    class_id: int = Form(...),
    db: Session = Depends(get_db),
):
    try:
        survey = crud.create_survey(
            db,
            title=title,
            class_id=class_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"msg": "Đã tạo khảo sát", "survey": survey}


@app.delete("/admin/surveys/{survey_id}")
def delete_admin_survey(survey_id: int, db: Session = Depends(get_db)):
    ok, reason = crud.delete_survey(db, survey_id)
    if not ok:
        raise HTTPException(status_code=404, detail=reason)
    return {"msg": reason}


@app.get("/admin/surveys/{survey_id}/details")
def get_admin_survey_details(survey_id: int, db: Session = Depends(get_db)):
    try:
        return crud.get_survey_detail_for_admin(db, survey_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/surveys")
def list_student_surveys(
    class_id: Optional[int] = None,
    user_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    if class_id is None:
        return {"items": [], "count": 0}
    items = crud.get_surveys_for_class(db, class_id=class_id, user_id=user_id)
    return {"items": items, "count": len(items)}


@app.get("/surveys/{survey_id}/sentiment")
def get_survey_sentiment_summary(survey_id: int, db: Session = Depends(get_db)):
    survey = (
        db.query(models.Survey)
          .options(joinedload(models.Survey.class_))
          .filter(models.Survey.id == survey_id)
          .first()
    )
    if not survey or not survey.is_published:
        raise HTTPException(status_code=404, detail="Không tìm thấy khảo sát")

    rows = (
        db.query(models.SurveyTextResponse.response_text)
          .filter(models.SurveyTextResponse.survey_id == survey_id)
          .all()
    )

    counts = {"POS": 0, "NEG": 0, "NEU": 0}
    analyzed = 0
    for (response_text,) in rows:
        text_value = (response_text or "").strip()
        if not text_value:
            continue
        analyzed += 1
        label, _, _, prob_neu = sentiment_model.predict_sentiment_full(text_value)
        normalized_label = str(label or "NEU").upper()
        if normalized_label not in counts:
            normalized_label = "NEU"
        counts[normalized_label] += 1

    total = sum(counts.values())
    return {
        "survey": {
            "id": survey.id,
            "title": survey.title,
            "class_id": survey.class_id,
            "class_name": survey.class_.name if survey.class_ else None,
        },
        "counts": counts,
        "total": total,
        "analyzed_text_count": analyzed,
    }


@app.post("/surveys/{survey_id}/response")
def submit_survey_response(
    survey_id: int,
    user_id: int = Form(...),
    option_id: Optional[int] = Form(None),
    response_text: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    try:
        survey = crud.submit_survey_response(
            db,
            survey_id=survey_id,
            user_id=user_id,
            option_id=option_id,
            response_text=response_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"msg": "Đã ghi nhận khảo sát", "survey": survey}


# ========================== TEACHER (GIẢNG VIÊN) ==========================

VALID_TEACHER_ROLES = {"subject_teacher", "homeroom_teacher"}


def _parse_id_list(raw_ids: Optional[str]) -> List[int]:
    if not raw_ids:
        return []
    return [int(x.strip()) for x in raw_ids.split(",") if x.strip().isdigit()]


def _send_negative_feedback_email(
    recipients: List[str],
    student_name: str,
    class_name: str,
    subject_name: str,
    feedback_text: str,
) -> Tuple[bool, str]:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port_raw = os.getenv("SMTP_PORT", "587").strip()
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", smtp_username).strip()
    smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"}

    if not smtp_host or not smtp_from:
        return False, "Thiếu cấu hình SMTP_HOST hoặc SMTP_FROM"

    try:
        smtp_port = int(smtp_port_raw)
    except ValueError:
        smtp_port = 587

    subject = f"[Cảnh báo] Phản hồi tiêu cực - {student_name} ({class_name})"
    text_body = (
        "Hệ thống phản hồi sinh viên vừa ghi nhận một phản hồi tiêu cực.\n\n"
        f"Sinh viên: {student_name}\n"
        f"Lớp: {class_name}\n"
        f"Môn học: {subject_name}\n"
        f"Nội dung phản hồi: {feedback_text}\n"
    )

    html_body = f"""
    <p>Hệ thống phản hồi sinh viên vừa ghi nhận một phản hồi <strong>tiêu cực</strong>.</p>
    <ul>
      <li><strong>Sinh viên:</strong> {student_name}</li>
      <li><strong>Lớp:</strong> {class_name}</li>
      <li><strong>Môn học:</strong> {subject_name}</li>
    </ul>
    <p><strong>Nội dung phản hồi:</strong></p>
    <blockquote style=\"margin:0;padding-left:12px;border-left:3px solid #e74c3c;\">{feedback_text}</blockquote>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            if smtp_use_tls:
                server.starttls()
            if smtp_username and smtp_password:
                server.login(smtp_username, smtp_password)
            server.sendmail(smtp_from, recipients, msg.as_string())
        return True, "OK"
    except Exception as e:
        return False, str(e)


@app.get("/admin/teachers")
def list_teachers(db: Session = Depends(get_db)):
    return crud.get_all_teachers(db)


@app.post("/admin/create_teacher")
def create_teacher(
    teacher_code: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(""),
    role: str = Form(...),
    subject_ids: str = Form(""),
    class_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    role = (role or "").strip()
    if role not in VALID_TEACHER_ROLES:
        raise HTTPException(status_code=400, detail="Vai trò không hợp lệ")

    if crud.get_teacher_by_code(db, teacher_code.strip()):
        raise HTTPException(status_code=400, detail="Mã giảng viên đã tồn tại")

    email_value = (email or "").strip()
    if email_value:
        existed_email = crud.get_teacher_by_email(db, email_value)
        if existed_email:
            raise HTTPException(status_code=400, detail="Email giảng viên đã tồn tại")

    class_subject_ids = _parse_id_list(subject_ids)
    homeroom_class_ids = _parse_id_list(class_ids)
    if role != "subject_teacher":
        class_subject_ids = []
    if role != "homeroom_teacher":
        homeroom_class_ids = []

    teacher = crud.create_teacher(
        db,
        teacher_code=teacher_code,
        full_name=full_name,
        email=email_value,
        role=role,
        subject_ids=class_subject_ids,
        class_ids=homeroom_class_ids,
    )
    return {"msg": f"Đã tạo giảng viên {teacher.full_name}", "id": teacher.id}


@app.put("/admin/teachers/{teacher_id}")
def update_teacher(
    teacher_id: int,
    teacher_code: str = Form(None),
    full_name: str = Form(None),
    email: str = Form(None),
    role: str = Form(None),
    subject_ids: str = Form(None),
    class_ids: str = Form(None),
    db: Session = Depends(get_db),
):
    teacher = db.get(models.Teacher, teacher_id)
    if not teacher:
        raise HTTPException(status_code=404, detail="Không tìm thấy giảng viên")

    role_value = None
    if role is not None:
        role_value = role.strip()
        if role_value not in VALID_TEACHER_ROLES:
            raise HTTPException(status_code=400, detail="Vai trò không hợp lệ")

    if teacher_code is not None:
        existed = crud.get_teacher_by_code(db, teacher_code.strip())
        if existed and existed.id != teacher_id:
            raise HTTPException(status_code=400, detail="Mã giảng viên đã tồn tại")

    if email is not None and email.strip():
        existed_email = crud.get_teacher_by_email(db, email.strip())
        if existed_email and existed_email.id != teacher_id:
            raise HTTPException(status_code=400, detail="Email giảng viên đã tồn tại")

    class_subject_ids = None
    if subject_ids is not None:
        class_subject_ids = _parse_id_list(subject_ids)

    homeroom_class_ids = None
    if class_ids is not None:
        homeroom_class_ids = _parse_id_list(class_ids)

    final_role = role_value if role_value is not None else teacher.role
    if final_role != "subject_teacher":
        class_subject_ids = []
    if final_role != "homeroom_teacher":
        homeroom_class_ids = []

    updated = crud.update_teacher(
        db,
        teacher_id=teacher_id,
        teacher_code=teacher_code,
        full_name=full_name,
        email=email,
        role=role_value,
        subject_ids=class_subject_ids,
        class_ids=homeroom_class_ids,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy giảng viên")
    return {"msg": "Đã cập nhật", "id": updated.id}


@app.delete("/admin/teachers/{teacher_id}")
def delete_teacher(teacher_id: int, db: Session = Depends(get_db)):
    ok, reason = crud.delete_teacher(db, teacher_id)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    return {"msg": reason}


@app.get("/admin/students")
def list_students(db: Session = Depends(get_db)):
    return crud.get_all_students(db)

@app.post("/admin/create_user")
def create_user(
    username: str = Form(...),
    password: str = Form(...),
    fullname: str = Form(...),
    class_id: int = Form(None),
    date_of_birth: str = Form(None),
    role: str = Form("student"),
    db: Session = Depends(get_db),
):
    if crud.get_user_by_username(db, username):
        raise HTTPException(status_code=400, detail="Tên người dùng đã tồn tại")
    pwh = pwd_context.hash(password)
    u = crud.create_user(db, username, pwh, fullname, class_id, role, date_of_birth)
    return {"msg": f"Đã tạo tài khoản {u.username}", "id": u.id}

@app.put("/admin/students/{user_id}")
def update_student(
    user_id: int,
    fullname: str = Form(None),
    class_id: int = Form(None),
    date_of_birth: str = Form(None),
    password: str = Form(None),
    db: Session = Depends(get_db),
):
    pwh = None
    if password not in (None, ""):
        pwh = pwd_context.hash(password)
    u = crud.update_student(db, user_id, fullname, class_id, pwh, date_of_birth)
    if not u:
        raise HTTPException(status_code=404, detail="Không tìm thấy học sinh")
    return {"msg": "Đã cập nhật", "id": u.id}


def _delete_all_student_related_data(db: Session) -> Dict[str, int]:
    """Xóa toàn bộ sinh viên và dữ liệu liên quan trực tiếp trong DB."""
    student_ids = [sid for (sid,) in db.query(models.User.id).filter(models.User.role == "student").all()]
    if not student_ids:
        return {"students": 0, "feedbacks": 0, "chat_messages": 0, "alerts": 0}

    student_id_strs = [str(sid) for sid in student_ids]

    deleted_feedbacks = (
        db.query(models.Feedback)
        .filter(models.Feedback.user_id.in_(student_ids))
        .delete(synchronize_session=False)
    )
    deleted_chat_messages = (
        db.query(models.ChatMessage)
        .filter(
            (models.ChatMessage.user_id.in_(student_ids))
            | (models.ChatMessage.student_id.in_(student_id_strs))
        )
        .delete(synchronize_session=False)
    )
    deleted_alerts = (
        db.query(models.Alert)
        .filter(
            (models.Alert.user_id.in_(student_ids))
            | (models.Alert.student_id.in_(student_id_strs))
        )
        .delete(synchronize_session=False)
    )
    deleted_students = (
        db.query(models.User)
        .filter(models.User.id.in_(student_ids))
        .delete(synchronize_session=False)
    )

    db.commit()

    if DATABASE_URL.startswith("postgresql"):
        db.execute(text("ALTER SEQUENCE users_id_seq RESTART WITH 1"))
        db.commit()

    return {
        "students": deleted_students,
        "feedbacks": deleted_feedbacks,
        "chat_messages": deleted_chat_messages,
        "alerts": deleted_alerts,
    }

@app.post("/admin/import-students")
def import_students(
    replace_all: bool = Form(False),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Import students from Excel file. 
    Expected columns: student ID (required, e.g. 'Mã sinh viên' or 'ID'),
    plus optional 'Họ tên', 'Lớp', 'Ngày sinh'.
    Username and password will be set to the student ID.
    """
    try:
        import pandas as pd
        
        # Đọc toàn bộ workbook để chọn đúng sheet chứa danh sách sinh viên.
        content = file.file.read()
        workbook = pd.read_excel(io.BytesIO(content), dtype=str, sheet_name=None)
        if not workbook:
            raise HTTPException(status_code=400, detail="File Excel không có sheet nào để import.")
        
        # Chuẩn hóa tên cột để nhận nhiều biến thể (không phân biệt hoa/thường, khoảng trắng, dấu gạch dưới)
        def normalize_col_name(col_name: str) -> str:
            return re.sub(r"[\s_]+", "", str(col_name).strip().lower())

        def find_col(normalized_to_real_col: Dict[str, str], possible_names: List[str]) -> Optional[str]:
            for name in possible_names:
                key = normalize_col_name(name)
                if key in normalized_to_real_col:
                    return normalized_to_real_col[key]
            return None

        def non_empty_cell(raw_value: Any) -> str:
            if raw_value is None:
                return ""
            text_value = str(raw_value).strip()
            return "" if not text_value or text_value.lower() == "nan" else text_value

        def analyze_sheet(sheet_name: str, sheet_df):
            cleaned_df = sheet_df.dropna(how="all").copy()
            normalized_to_real_col = {normalize_col_name(col): col for col in cleaned_df.columns}

            account_col = find_col(normalized_to_real_col, [
                "Tài khoản", "tai khoan", "username", "user_name", "account", "login"
            ])
            id_col = find_col(normalized_to_real_col, [
                "Mã sinh viên", "mã sinh viên", "masinhvien", "mssv", "student_id", "ID", "id", "mã sv", "ma sv"
            ])
            fullname_col = find_col(normalized_to_real_col, [
                "Họ và tên", "Họ và Tên", "ho va ten", "hovaten",
                "Họ tên", "ho ten", "fullname", "full_name", "student_name", "ten sinh vien"
            ])
            surname_col = find_col(normalized_to_real_col, ["Họ", "ho", "last_name", "surname", "family_name"])
            middle_name_col = find_col(normalized_to_real_col, ["Tên đệm", "ten dem", "middle_name", "middle"])
            given_name_col = find_col(normalized_to_real_col, ["Tên", "ten", "first_name", "given_name"])
            dob_col = find_col(normalized_to_real_col, ["Ngày sinh", "ngay sinh", "date_of_birth", "dob", "birth_date", "birthday"])
            class_col = find_col(normalized_to_real_col, ["Lớp", "lop", "class", "class_name", "tên lớp", "ten lop"])

            valid_id_count = 0
            sample_col = account_col or id_col
            if sample_col:
                for val in cleaned_df[sample_col].head(200):
                    if non_empty_cell(val):
                        valid_id_count += 1

            has_name_signal = bool(fullname_col or (surname_col and given_name_col))
            score = valid_id_count + (25 if sample_col else 0) + (10 if has_name_signal else 0) + (3 if class_col else 0) + (2 if dob_col else 0)
            return {
                "sheet_name": sheet_name,
                "df": cleaned_df,
                "normalized_to_real_col": normalized_to_real_col,
                "account_col": account_col,
                "id_col": id_col,
                "fullname_col": fullname_col,
                "surname_col": surname_col,
                "middle_name_col": middle_name_col,
                "given_name_col": given_name_col,
                "dob_col": dob_col,
                "class_col": class_col,
                "score": score,
            }

        sheet_analyses = [analyze_sheet(sheet_name, sheet_df) for sheet_name, sheet_df in workbook.items()]
        sheet_analyses = [item for item in sheet_analyses if item["account_col"] or item["id_col"]]
        if not sheet_analyses:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Không tìm thấy sheet nào có cột tài khoản hoặc mã sinh viên. "
                    f"Các sheet hiện có: {list(workbook.keys())}"
                ),
            )

        selected_sheet = max(sheet_analyses, key=lambda item: item["score"])
        df = selected_sheet["df"]
        normalized_to_real_col = selected_sheet["normalized_to_real_col"]

        # Chuẩn hóa tên lớp để map linh hoạt (vd: "CNTT 16 - 01" == "CNTT 16-01")
        def normalize_class_name(class_name: str) -> str:
            text_name = str(class_name).strip().lower()
            # Chuẩn hóa các loại dấu gạch Unicode về '-'
            text_name = re.sub(r"[‐‑‒–—―]", "-", text_name)
            text_name = re.sub(r"\s*-\s*", "-", text_name)
            text_name = re.sub(r"\s+", " ", text_name)
            return text_name

        def canonical_class_key(class_name: str) -> str:
            # Dùng key không khoảng trắng/dấu câu để tăng tỷ lệ match tên lớp từ Excel
            return re.sub(r"[^a-z0-9]", "", normalize_class_name(class_name))

        # Ưu tiên cột tài khoản đăng nhập nếu có
        account_col = selected_sheet["account_col"]

        # Cột mã sinh viên (fallback nếu không có cột tài khoản)
        id_col = selected_sheet["id_col"]

        # Cần ít nhất một trong hai: tài khoản hoặc mã sinh viên
        if not account_col and not id_col:
            raise HTTPException(
                status_code=400,
                detail=(
                    "File phải có cột tài khoản hoặc mã sinh viên "
                    "(ví dụ: 'Tài khoản', 'Mã sinh viên', 'ID', 'mssv', 'student_id'). "
                    f"Sheet đang dùng: {selected_sheet['sheet_name']}. Các cột hiện có: {list(df.columns)}"
                ),
            )
        
        # Tìm các cột optional
        fullname_col = selected_sheet["fullname_col"]
        surname_col = selected_sheet["surname_col"]
        middle_name_col = selected_sheet["middle_name_col"]
        given_name_col = selected_sheet["given_name_col"]
        dob_col = selected_sheet["dob_col"]

        # Map lớp theo tên chuẩn hóa để gán class_id chính xác khi import sinh viên
        class_lookup = {}
        class_lookup_canonical = {}
        class_lookup_by_id = {}
        for c in db.query(models.Class).all():
            normalized = normalize_class_name(c.name)
            canonical = canonical_class_key(c.name)
            if normalized and normalized not in class_lookup:
                class_lookup[normalized] = c.id
            if canonical and canonical not in class_lookup_canonical:
                class_lookup_canonical[canonical] = c.id
            class_lookup_by_id[str(c.id)] = c.id

        def resolve_class_id(raw_value: str) -> Optional[int]:
            normalized = normalize_class_name(raw_value)
            if normalized in class_lookup:
                return class_lookup[normalized]

            canonical = canonical_class_key(raw_value)
            if canonical in class_lookup_canonical:
                return class_lookup_canonical[canonical]

            # Cho phép file nhập trực tiếp id lớp
            if raw_value in class_lookup_by_id:
                return class_lookup_by_id[raw_value]

            return None

        class_col = selected_sheet["class_col"]

        # Fallback thông minh: chọn cột có nhiều giá trị khớp tên lớp nhất (hữu ích khi header cột lớp bị trống)
        if not class_col:
            used_cols = {c for c in [account_col, id_col, fullname_col, surname_col, middle_name_col, given_name_col, dob_col] if c}
            candidate_cols = [c for c in df.columns if c not in used_cols]

            best_col = None
            best_score = 0
            for col in candidate_cols:
                score = 0
                for val in df[col].head(200):
                    if pd.isna(val):
                        continue
                    text_val = str(val).strip()
                    if not text_val or text_val.lower() == "nan":
                        continue
                    if resolve_class_id(text_val) is not None:
                        score += 1
                if score > best_score:
                    best_score = score
                    best_col = col

            if best_col and best_score > 0:
                class_col = best_col
            elif len(candidate_cols) == 1:
                class_col = candidate_cols[0]

        deleted_before_import = None
        if replace_all:
            deleted_before_import = _delete_all_student_related_data(db)
        
        results = {"created": [], "skipped": [], "errors": []}
        seen_student_ids = set()
        
        for idx, row in df.iterrows():
            # Ưu tiên tài khoản từ cột "Tài khoản"; nếu không có thì fallback sang mã sinh viên/ID
            account_val = ""
            if account_col and pd.notna(row[account_col]):
                account_val = str(row[account_col]).strip()

            id_val = ""
            if id_col and pd.notna(row[id_col]):
                id_val = str(row[id_col]).strip()

            student_id = account_val or id_val
            
            # Xóa "nan" nếu Excel cell trống
            if not student_id or student_id.lower() == "nan":
                results["skipped"].append(f"Hàng {idx+2}: tài khoản/mã sinh viên trống")
                continue

            # Bỏ qua nếu trùng ngay trong chính file Excel
            if student_id in seen_student_ids:
                results["skipped"].append(f"Hàng {idx+2}: {student_id} bị trùng trong file import")
                continue
            seen_student_ids.add(student_id)
            
            try:
                # Lấy thông tin từ Excel
                fullname = ""
                if fullname_col:
                    fullname = non_empty_cell(row[fullname_col])
                else:
                    fullname_parts = []
                    for col in [surname_col, middle_name_col, given_name_col]:
                        if col:
                            piece = non_empty_cell(row[col])
                            if piece:
                                fullname_parts.append(piece)
                    fullname = " ".join(fullname_parts).strip()
                
                class_id = None
                if class_col:
                    class_name = non_empty_cell(row[class_col])
                    if class_name:
                        class_id = resolve_class_id(class_name)
                
                date_of_birth = None
                if dob_col:
                    dob_val = row[dob_col]
                    if pd.notna(dob_val):
                        # Xử lý ngày sinh - có thể là datetime hoặc string
                        if hasattr(dob_val, 'strftime'):
                            date_of_birth = dob_val.strftime("%Y-%m-%d")
                        else:
                            date_of_birth = str(dob_val).strip()

                # Nếu sinh viên đã tồn tại thì cập nhật thông tin lớp/ngày sinh/họ tên (khi có)
                existing_user = crud.get_user_by_username(db, student_id)
                if existing_user:
                    updated = False
                    if class_id and existing_user.class_id != class_id:
                        existing_user.class_id = class_id
                        updated = True
                    if date_of_birth and existing_user.date_of_birth != date_of_birth:
                        existing_user.date_of_birth = date_of_birth
                        updated = True
                    # Nếu file có Họ tên thì đồng bộ lại để sửa các bản ghi cũ bị import sai trước đó
                    if fullname and existing_user.fullname != fullname:
                        existing_user.fullname = fullname
                        updated = True

                    if updated:
                        db.commit()
                        results["skipped"].append(f"{student_id}: đã tồn tại (đã cập nhật thông tin)")
                    else:
                        results["skipped"].append(f"{student_id}: đã tồn tại")
                    continue
                
                # Username và password đều lấy theo giá trị định danh đã chọn (Tài khoản ưu tiên, fallback mã sinh viên/ID)
                username = student_id
                password_hash = pwd_context.hash(student_id)
                
                u = crud.create_user(
                    db,
                    username=username,
                    password_hash=password_hash,
                    fullname=fullname or student_id,
                    class_id=class_id,
                    role="student",
                    date_of_birth=date_of_birth
                )
                results["created"].append(student_id)
            except IntegrityError:
                # Phiên giao dịch phải rollback sau lỗi ràng buộc để tiếp tục xử lý các dòng sau
                db.rollback()
                results["skipped"].append(f"{student_id}: đã tồn tại")
            except Exception as e:
                db.rollback()
                results["errors"].append(f"{student_id}: {str(e)}")
        
        db.commit()
        return {
            "msg": f"Import hoàn tất. Tạo: {len(results['created'])}, Bỏ qua: {len(results['skipped'])}, Lỗi: {len(results['errors'])}",
            "results": results,
            "selected_sheet": selected_sheet["sheet_name"],
            "detected_columns": {
                "account": account_col,
                "student_id": id_col,
                "fullname": fullname_col,
                "surname": surname_col,
                "middle_name": middle_name_col,
                "given_name": given_name_col,
                "class": class_col,
                "date_of_birth": dob_col,
            },
            "deleted_before_import": deleted_before_import,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Lỗi xử lý file: {str(e)}")


@app.delete("/admin/students/{user_id}")
def delete_student(user_id: int, db: Session = Depends(get_db)):
    try:
        ok = crud.delete_student(db, user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Không tìm thấy sinh viên")
        return {"msg": "Đã xóa học sinh"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Lỗi xóa sinh viên: {str(e)}")

@app.post("/admin/reset-students-id")
def reset_students_id(db: Session = Depends(get_db)):
    """Xóa toàn bộ sinh viên + dữ liệu liên quan và reset ID về 1."""
    try:
        deleted = _delete_all_student_related_data(db)

        if not any(deleted.values()):
            return {
                "msg": "Không có sinh viên để xóa.",
                "deleted": deleted,
            }

        return {
            "msg": "✅ Đã xóa toàn bộ sinh viên và dữ liệu liên quan.",
            "deleted": deleted,
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Lỗi: {str(e)}")


# ======================================================
# ✅ Feedback
# ======================================================
@app.post("/feedback")
def submit_feedback(
    background_tasks: BackgroundTasks,
    user_id: int = Form(...),
    subject_id: int = Form(...),
    text: str = Form(...),
    db: Session = Depends(get_db),
):
    label, p_neg, p_pos = sentiment_model.predict_sentiment(text)
    try:
        fb = crud.create_feedback(db, user_id, subject_id, text, label, p_neg, p_pos)
        def _upsert_feedback_embedding_background(fb_id: int, uid: int, sid: int, fb_text: str) -> None:
            bg_db = SessionLocal()
            try:
                upsert_feedback_embedding(
                    db=bg_db,
                    feedback_id=fb_id,
                    user_id=uid,
                    subject_id=sid,
                    feedback_text=fb_text,
                )
            except Exception:
                try:
                    bg_db.rollback()
                except Exception:
                    pass
            finally:
                bg_db.close()

        background_tasks.add_task(
            _upsert_feedback_embedding_background,
            fb.id,
            user_id,
            subject_id,
            text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    alert_email_sent = False
    alert_email_error = ""
    alert_email_receivers: List[str] = []
    negative_support_reply = ""
    negative_support_conversation_id = ""

    if (label or "").upper() == "NEG":
        try:
            negative_support_reply = build_negative_support_reply_vector_rag(
                db=db,
                user_id=user_id,
                latest_feedback_text=text,
                api_key=NEGATIVE_SUPPORT_GROQ_API_KEY,
                model=NEGATIVE_SUPPORT_GROQ_MODEL,
                max_tokens=NEGATIVE_SUPPORT_MAX_TOKENS,
                exclude_feedback_id=fb.id,
            )
        except Exception:
            negative_support_reply = (
                "Mình hiểu bạn đang gặp khó khăn. Bạn thử nghỉ ngắn 5 phút, "
                "ghi rõ 1-2 vấn đề chính đang vướng và mình sẽ cùng bạn gỡ từng bước nhé."
            )

        negative_support_conversation_id = f"neg-feedback-{fb.id}"
        if hasattr(crud, "save_chat_message"):
            try:
                crud.save_chat_message(
                    db=db,
                    conversation_id=negative_support_conversation_id,
                    student_id=str(user_id),
                    role="user",
                    text=text,
                    sentiment="neg",
                    escalated=False,
                )
                crud.save_chat_message(
                    db=db,
                    conversation_id=negative_support_conversation_id,
                    student_id=str(user_id),
                    role="assistant",
                    text=negative_support_reply,
                    sentiment="neg",
                    escalated=False,
                )
            except Exception:
                pass

        student = (
            db.query(models.User)
              .options(joinedload(models.User.class_))
              .filter(models.User.id == user_id)
              .first()
        )
        subject = db.get(models.Subject, subject_id)

        if student and student.class_id:
            teachers = crud.get_homeroom_teachers_by_class(db, student.class_id)
            recipient_set = {
                (t.email or "").strip()
                for t in teachers
                if (t.email or "").strip()
            }
            recipients = sorted(recipient_set)
            if recipients:
                ok, err = _send_negative_feedback_email(
                    recipients=recipients,
                    student_name=student.fullname or student.username,
                    class_name=student.class_.name if student.class_ else (str(student.class_id) if student.class_id else "Không rõ"),
                    subject_name=subject.name if subject else f"Môn #{subject_id}",
                    feedback_text=text,
                )
                alert_email_sent = ok
                alert_email_error = "" if ok else err
                alert_email_receivers = recipients

    return {
        "feedback_id": fb.id,
        "label": label,
        "prob_neg": p_neg,
        "prob_pos": p_pos,
        "negative_support_reply": negative_support_reply,
        "negative_support_conversation_id": negative_support_conversation_id,
        "alert_email_sent": alert_email_sent,
        "alert_email_receivers": alert_email_receivers,
        "alert_email_error": alert_email_error,
    }

@app.get("/feedbacks")
def list_feedbacks(limit: int = 50, db: Session = Depends(get_db)):
    return crud.get_feedbacks(db, limit=limit)

@app.get("/survey-feedbacks")
def list_survey_feedbacks(limit: int = 50, db: Session = Depends(get_db)):
    """Lấy tất cả survey text responses kèm sentiment analysis"""
    return crud.get_survey_responses_with_sentiment(db, limit=limit)

@app.get("/feedbacks/class/{class_id}")
def feedbacks_by_class(class_id: int, limit: int = 100, db: Session = Depends(get_db)):
    return crud.get_feedbacks_by_class(db, class_id, limit=limit)

@app.get("/feedbacks/user/{user_id}")
def feedbacks_by_user(
    user_id: int,
    limit: int = 100,
    order: str = "desc",
    db: Session = Depends(get_db),
):
    return crud.get_feedbacks_by_user(db, user_id, limit=limit, order=order)


@app.put("/feedbacks/{feedback_id}")
def update_feedback_by_id(
    feedback_id: int,
    user_id: int = Form(...),
    subject_id: int = Form(...),
    text: str = Form(...),
    db: Session = Depends(get_db),
):
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Nội dung đánh giá không được để trống")
    try:
        updated = crud.update_feedback(db, feedback_id, user_id, subject_id, text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy đánh giá")
    try:
        upsert_feedback_embedding(
            db=db,
            feedback_id=feedback_id,
            user_id=user_id,
            subject_id=subject_id,
            feedback_text=text,
        )
    except Exception:
        db.rollback()
        pass
    return {"msg": "Đã cập nhật đánh giá", "feedback": updated}


@app.delete("/feedbacks/{feedback_id}")
def delete_feedback_by_id(
    feedback_id: int,
    user_id: int = Form(...),
    db: Session = Depends(get_db),
):
    ok = crud.delete_feedback(db, feedback_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Không tìm thấy đánh giá")
    return {"msg": "Đã xóa đánh giá"}

@app.get("/stats")
def stats(db: Session = Depends(get_db)):
    return crud.get_stats(db)


# ======================================================
# ✅ Facebook CSV Analysis — đọc 1 cột text, phân tích toàn bộ
# ======================================================
@app.post("/fb/analyze")
async def analyze_facebook_csv(file: UploadFile = File(...)):
    raw = await file.read()
    s = raw.decode("utf-8-sig", errors="ignore")
    if not s.strip():
        raise HTTPException(status_code=400, detail="File trống hoặc không đọc được.")

    # Tự động nhận delimiter
    try:
        dialect = csv.Sniffer().sniff(s[:2048], delimiters=",;")
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ","

    stream = io.StringIO(s)
    reader = csv.reader(stream, delimiter=delimiter)

    # Nhận dạng có header hay không
    first_row = next(reader, None)
    if not first_row:
        raise HTTPException(status_code=400, detail="File không có dữ liệu.")
    header_detected = all(ch.isalpha() or ch.isspace() for ch in "".join(first_row))
    if header_detected:
        rows = list(reader)
    else:
        rows = [first_row] + list(reader)

    total = pos = neg = 0
    results = []

    for row in rows:
        if not row:
            continue
        text = (row[0] or "").strip()
        if not text:
            continue
        label, p_neg, p_pos = sentiment_model.predict_sentiment(text)
        total += 1
        if label == "POS":
            pos += 1
        elif label == "NEG":
            neg += 1
        results.append({
            "text": text,
            "label": label,
            "prob_neg": round(p_neg, 4),
            "prob_pos": round(p_pos, 4)
        })

    return {
        "total": total,
        "POS": pos,
        "NEG": neg,
        "NEG_rate": (neg / total) if total else 0,
        "rows": results
    }


# ======================================================
# ✅ Chatbot Groq — POST /api/chat
# ======================================================
# 1) Cấu hình Groq
load_dotenv(override=True)
# Key/model dùng chung (fallback)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant").strip()

# 2 key phục vụ 2 mục đích khác nhau trên giao diện sinh viên:
# - Negative support bot: chỉ hỗ trợ khi cảm xúc tiêu cực
# - General bot: hỏi gì cũng được
#
# Backward compatibility:
# - CHATBOT_GROQ_API_KEY/CHATBOT_GROQ_MODEL được map vào nhánh general chat
LEGACY_CHATBOT_GROQ_API_KEY = os.getenv("CHATBOT_GROQ_API_KEY", "").strip()
LEGACY_CHATBOT_GROQ_MODEL = os.getenv("CHATBOT_GROQ_MODEL", GROQ_MODEL).strip() or GROQ_MODEL

NEGATIVE_SUPPORT_GROQ_API_KEY = os.getenv("NEGATIVE_SUPPORT_GROQ_API_KEY", "").strip()
NEGATIVE_SUPPORT_GROQ_MODEL = os.getenv("NEGATIVE_SUPPORT_GROQ_MODEL", "llama-3.1-8b-instant").strip() or "llama-3.1-8b-instant"

GENERAL_CHATBOT_GROQ_API_KEY = os.getenv("GENERAL_CHATBOT_GROQ_API_KEY", "").strip() or LEGACY_CHATBOT_GROQ_API_KEY
GENERAL_CHATBOT_GROQ_MODEL = os.getenv("GENERAL_CHATBOT_GROQ_MODEL", LEGACY_CHATBOT_GROQ_MODEL).strip() or LEGACY_CHATBOT_GROQ_MODEL
ADMIN_RAG_CHATBOT_GROQ_API_KEY = os.getenv("ADMIN_RAG_CHATBOT_GROQ_API_KEY", "").strip()
ADMIN_RAG_CHATBOT_GROQ_MODEL = os.getenv("ADMIN_RAG_CHATBOT_GROQ_MODEL", "llama-3.1-8b-instant").strip() or "llama-3.1-8b-instant"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
NEGATIVE_SUPPORT_MAX_TOKENS = int(os.getenv("NEGATIVE_SUPPORT_MAX_TOKENS", "220"))
GENERAL_CHATBOT_MAX_TOKENS = int(os.getenv("GENERAL_CHATBOT_MAX_TOKENS", "700"))
ADMIN_RAG_CHATBOT_MAX_TOKENS = int(os.getenv("ADMIN_RAG_CHATBOT_MAX_TOKENS", "900"))
SYSTEM_INSTRUCTION = (
    "Bạn là trợ lý hỗ trợ sinh viên: ấm áp, rõ ràng, thực tế. "
    "Luôn trả lời ngắn gọn (≤120 từ), tiếng Việt tự nhiên, xưng 'mình'. "
    "Khi là khó khăn học tập: đưa 2–3 gợi ý bullet ngắn + 1 nguồn tham khảo. "
    "QUAN TRỌNG: Nếu người dùng chỉ chào hỏi (vd: 'xin chào', 'hello'), "
    "hãy đáp thân thiện như người thật, 1–2 câu: "
    "'Chào bạn 👋 Mình là trợ lý sinh viên. Mình giúp gì được cho bạn?'; "
    "KHÔNG phân tích hay phán xét tiêu cực/tích cực trong trường hợp này."
)

GENERAL_SYSTEM_INSTRUCTION = (
    "Bạn là trợ lý học tập cho sinh viên đại học. "
    "Trả lời tiếng Việt tự nhiên, rõ ràng, có cấu trúc ngắn gọn. "
    "Có thể trả lời đa lĩnh vực học thuật/phổ thông và câu hỏi đời sống lành mạnh. "
    "Khi không chắc chắn, hãy nói rõ mức độ chắc chắn và gợi ý cách kiểm chứng."
)

DEFAULT_SCHOOL_RULES_FILE = Path(__file__).resolve().parent.parent / "school_rules.md"
SCHOOL_RULES_FILE = Path(os.getenv("SCHOOL_RULES_FILE", str(DEFAULT_SCHOOL_RULES_FILE)))
GENERAL_RULES_MAX_CHARS = int(os.getenv("GENERAL_RULES_MAX_CHARS", "8000"))


def _load_school_rules_markdown() -> str:
    """Đọc file nội quy trường học dạng markdown cho chatbot general."""
    try:
        if not SCHOOL_RULES_FILE.exists() or not SCHOOL_RULES_FILE.is_file():
            return ""
        text = SCHOOL_RULES_FILE.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        # Tránh prompt quá dài gây tốn token và giảm chất lượng trả lời
        return text[:GENERAL_RULES_MAX_CHARS]
    except Exception:
        return ""


def _build_general_system_instruction() -> str:
    rules_md = _load_school_rules_markdown()
    if not rules_md:
        return (
            GENERAL_SYSTEM_INSTRUCTION
            + "\n\nHiện chưa có file nội quy trường học, hãy trả lời theo kiến thức chung và nhắc người dùng kiểm tra thông báo chính thức nếu cần."
        )

    return (
        GENERAL_SYSTEM_INSTRUCTION
        + "\n\nBạn phải ưu tiên tham chiếu nội quy trường học dưới đây khi câu hỏi liên quan đến quy định, kỷ luật, điểm danh, học phí, thi cử, hành vi và các thủ tục hành chính."
        + "\nNếu nội dung không có trong nội quy, hãy nói rõ 'không thấy trong nội quy hiện tại' và đưa hướng kiểm tra thêm."
        + "\n\n--- NOI QUY TRUONG HOC (MARKDOWN) ---\n"
        + rules_md
    )

# 2) Heuristic sentiment & risk (có thể thay bằng model của bạn)
NEG_HINTS = ["chán","mệt","stress","nản","khó chịu","áp lực","trầm cảm","bế tắc"]
RISK_RE = re.compile(r"(tự\s*hại|tự\s*tử|muốn\s*chết|suicide|kill myself)", re.I)

# ——— NEW: nhận diện lời chào ———
GREET_RE = re.compile(r"^\s*(xin chào|chào|hello|hi|hey)\b", re.I | re.U)
def is_greeting(text: str) -> bool:
    return bool(GREET_RE.search(text or ""))

def infer_sentiment_simple(t: str) -> str:
    t = (t or "").lower()
    if any(w in t for w in NEG_HINTS): return "neg"
    if any(w in t for w in ["tuyệt","vui","ổn","ok","hiểu rồi"]): return "pos"
    return "neu"

def build_prompt(msg: str, senti: str) -> str:
    # ——— NEW: prompt riêng cho lời chào ———
    if is_greeting(msg):
        return (
            "Ngữ cảnh: Người dùng vừa chào bạn.\n"
            "Yêu cầu: Đáp lại lời chào thân thiện như người thật (≤25 từ), "
            "xưng 'mình', KHÔNG nhắc tiêu cực/tích cực. "
            "Kết câu bằng gợi mở: “Mình giúp gì được cho bạn?”.\n"
        )
    tone = {
        "neg": "Giọng đồng cảm, trấn an; đề xuất 2–3 bước cụ thể.",
        "pos": "Giọng tích cực, khích lệ, gợi mở nâng cao.",
        "neu": "Giọng trung tính, thực dụng, rõ ràng."
    }[senti]
    return f"""Vai trò: Trợ lý hỗ trợ sinh viên. {tone}
Ràng buộc: ≤120 từ, tiếng Việt tự nhiên, kèm bước tiếp theo cụ thể.
Tin nhắn sinh viên: \"\"\"{msg}\"\"\"\n"""

# === NGƯỠNG GATE: chỉ gọi Groq khi NEG (theo model bạn train),
#     nhưng LUÔN cho phép gọi nếu là lời chào.
NEG_GATE_THRESHOLD = getattr(sentiment_model, "NEG_THRESHOLD", 0.60)

class ChatIn(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    student_id: Optional[str] = None

class ChatOut(BaseModel):
    reply: str
    sentiment: str
    escalated: bool
    took_ms: int


class GeneralChatOut(BaseModel):
    reply: str
    took_ms: int


class AdminRagChatIn(BaseModel):
    message: str


class AdminRagChatOut(BaseModel):
    reply: str
    took_ms: int


def _resolve_negative_support_groq_api_key() -> str:
    return NEGATIVE_SUPPORT_GROQ_API_KEY


def _resolve_general_chatbot_groq_api_key() -> str:
    return GENERAL_CHATBOT_GROQ_API_KEY


def _call_groq_chat(messages: List[Dict[str, str]], api_key: str, model: str, max_tokens: int = 220) -> str:
    if not api_key:
        raise RuntimeError("Thiếu API key Groq trong .env")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
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
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
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
        raise RuntimeError(f"Không thể gọi Groq API: {exc}") from exc

    choices = response_data.get("choices") or []
    if not choices:
        raise RuntimeError("Groq API không trả về nội dung")

    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        raise RuntimeError("Groq API trả về nội dung rỗng")
    return content

@app.post("/api/chat", response_model=ChatOut)
def api_chat(payload: ChatIn, db: Session = Depends(get_db)):
    chatbot_api_key = _resolve_negative_support_groq_api_key()
    if not chatbot_api_key:
        raise HTTPException(
            status_code=500,
            detail="Thiếu NEGATIVE_SUPPORT_GROQ_API_KEY trong .env"
        )

    start = time.time()
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Empty message")

    # 1) Dự đoán sentiment bằng model bạn train
    try:
        label, p_neg, p_pos = sentiment_model.predict_sentiment(msg)
        label_u = (label or "").upper()
        senti = "pos" if label_u == "POS" else ("neg" if label_u == "NEG" else "neu")
    except Exception:
        # Fallback: heuristic nếu model lỗi
        p_neg, p_pos = 0.0, 1.0
        senti = infer_sentiment_simple(msg)

    # 2) Gate: CHỈ gọi Groq nếu NEG hoặc p_neg vượt ngưỡng,
    #    NGOẠI LỆ: nếu là lời chào → vẫn gọi để đáp thân thiện.
    call_groq = is_greeting(msg) or (senti == "neg" or p_neg >= NEG_GATE_THRESHOLD)
    if not call_groq:
        took_ms = int((time.time() - start) * 1000)
        return ChatOut(
            reply="Nội dung của bạn hiện không có dấu hiệu tiêu cực. Nếu vẫn cần hỗ trợ chi tiết, hãy mô tả rõ khó khăn để mình gợi ý từng bước nhé.",
            sentiment=senti,
            escalated=False,
            took_ms=took_ms
        )

    # 3) Gọi Groq
    risky = bool(RISK_RE.search(msg))
    prompt = build_prompt(msg, senti)

    try:
        text = _call_groq_chat([
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ], api_key=chatbot_api_key, model=NEGATIVE_SUPPORT_GROQ_MODEL, max_tokens=NEGATIVE_SUPPORT_MAX_TOKENS)
    except Exception:
        text = "Chào bạn 👋 Mình là trợ lý sinh viên. Mình giúp gì được cho bạn?"

    if risky:
        text += (
            "\n\n⚠️ Nếu bạn đang thấy không an toàn, hãy liên hệ ngay người thân, tư vấn học đường "
            "hoặc đường dây nóng tại địa phương."
        )
        if hasattr(crud, "create_alert"):
            try:
                crud.create_alert(
                    db,
                    student_id=str(payload.student_id or ""),
                    conversation_id=str(payload.conversation_id or ""),
                    trigger_text=msg,
                )
            except Exception:
                pass

    # 4) Lưu log hội thoại nếu CRUD có hàm (không tác động hệ thống nếu chưa có)
    if hasattr(crud, "save_chat_message"):
        try:
            crud.save_chat_message(
                db, str(payload.conversation_id or ""), str(payload.student_id or ""),
                "user", msg, senti, risky
            )
            crud.save_chat_message(
                db, str(payload.conversation_id or ""), str(payload.student_id or ""),
                "assistant", text, senti, risky
            )
        except Exception:
            pass

    took_ms = int((time.time() - start) * 1000)
    return ChatOut(reply=text, sentiment=senti, escalated=risky, took_ms=took_ms)


@app.post("/api/chat/general", response_model=GeneralChatOut)
def api_chat_general(payload: ChatIn, db: Session = Depends(get_db)):
    chatbot_api_key = _resolve_general_chatbot_groq_api_key()
    if not chatbot_api_key:
        raise HTTPException(status_code=500, detail="Thiếu GENERAL_CHATBOT_GROQ_API_KEY (hoặc CHATBOT_GROQ_API_KEY cũ) trong .env")

    start = time.time()
    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Empty message")

    try:
        text = _call_groq_chat([
            {"role": "system", "content": _build_general_system_instruction()},
            {"role": "user", "content": msg},
        ], api_key=chatbot_api_key, model=GENERAL_CHATBOT_GROQ_MODEL, max_tokens=GENERAL_CHATBOT_MAX_TOKENS)
    except Exception:
        text = "Hiện tại chatbot hỏi đáp đang bận. Bạn thử lại sau ít phút nhé."

    if hasattr(crud, "save_chat_message"):
        try:
            conv_id = str(payload.conversation_id or "")
            student_id = str(payload.student_id or "")
            crud.save_chat_message(db, conv_id, student_id, "user", msg, "neu", False)
            crud.save_chat_message(db, conv_id, student_id, "assistant", text, "neu", False)
        except Exception:
            pass

    took_ms = int((time.time() - start) * 1000)
    return GeneralChatOut(reply=text, took_ms=took_ms)


@app.post("/api/chat/admin", response_model=AdminRagChatOut)
def api_chat_admin(payload: AdminRagChatIn, db: Session = Depends(get_db)):
    if not ADMIN_RAG_CHATBOT_GROQ_API_KEY:
        raise HTTPException(status_code=500, detail="Thiếu ADMIN_RAG_CHATBOT_GROQ_API_KEY trong .env")

    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="Empty message")

    start = time.time()
    try:
        text = build_admin_rag_reply(
            db=db,
            question=msg,
            api_key=ADMIN_RAG_CHATBOT_GROQ_API_KEY,
            model=ADMIN_RAG_CHATBOT_GROQ_MODEL,
            max_tokens=ADMIN_RAG_CHATBOT_MAX_TOKENS,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Admin RAG chatbot error: {exc}")

    took_ms = int((time.time() - start) * 1000)
    return AdminRagChatOut(reply=text, took_ms=took_ms)
