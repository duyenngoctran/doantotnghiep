from __future__ import annotations
from typing import List, Dict, Tuple, Optional
from datetime import datetime
from sqlalchemy import func, inspect, text

from sqlalchemy.orm import Session, joinedload

from . import models
from .models import ChatMessage, Alert, vietnam_now

# ============================ KHẢO SÁT ============================




DELETED_SUBJECT_NAME = "[HỆ THỐNG] Môn đã xóa"

# ============================ KHẢO SÁT ============================

DELETED_SUBJECT_DESC = "__SYSTEM_DELETED_SUBJECT__"


def _calc_semester_is_active(start_date=None, end_date=None) -> bool:
    now = datetime.now()
    if start_date and now < start_date:
        return False

    if end_date and now > end_date:

        return False
    return True


def _is_deleted_subject(subject: models.Subject) -> bool:
    return (subject.description or "") == DELETED_SUBJECT_DESC




def _get_or_create_deleted_subject(db: Session) -> models.Subject:
    archived = (
        db.query(models.Subject)
          .filter(models.Subject.description == DELETED_SUBJECT_DESC)
          .first()
    )
    if archived:

        return archived

    archived = models.Subject(
        name=DELETED_SUBJECT_NAME,
        description=DELETED_SUBJECT_DESC,

        teacher="",
        semester_id=None,
    )
    db.add(archived)
    db.flush()
    return archived

# ======================== USER (HỌC SINH / ADMIN) ========================

def get_user_by_username(db: Session, username: str) -> Optional[models.User]:
    return db.query(models.User).options(joinedload(models.User.class_)).filter(models.User.username == username).first()


def create_user(
    db: Session,
    username: str,
    password_hash: str,

    fullname: str,
    class_id: Optional[int] = None,
    role: str = "student",
    date_of_birth: Optional[str] = None,
) -> models.User:
    user = models.User(
        username=username,
        password_hash=password_hash,
        fullname=fullname,

        role=role,
        class_id=class_id,
        date_of_birth=date_of_birth,
    )

    db.add(user)
    db.commit()
    db.refresh(user)
    return user

def get_all_students(db: Session, limit: int = 10000, offset: int = 0) -> List[Dict]:

    rows = (
        db.query(models.User)
          .options(joinedload(models.User.class_))


          .filter(models.User.role == "student")
                    .order_by(func.lower(func.coalesce(models.User.fullname, "")).asc(), models.User.id.asc())

          .offset(offset)

          .limit(limit)

          .all()
    )
    return [
        {
            "id": r.id,
            "username": r.username,


            "fullname": r.fullname,
            "class_id": r.class_id,


            "class_name": r.class_.name if r.class_ else None,
            "date_of_birth": r.date_of_birth,

        }

        for r in rows
    ]

def update_student(
    db: Session,

    user_id: int,
    fullname: Optional[str] = None,

    class_id: Optional[int] = None,
    password_hash: Optional[str] = None,
    date_of_birth: Optional[str] = None,
) -> Optional[models.User]:
    u = db.get(models.User, user_id)
    if not u:
        return None

    if fullname is not None:
        u.fullname = fullname
    if class_id is not None:

        u.class_id = class_id
    if password_hash:
        u.password_hash = password_hash
    if date_of_birth is not None:
        u.date_of_birth = date_of_birth
    db.commit()

    db.refresh(u)
    return u

def can_delete_student(db: Session, user_id: int) -> bool:

    # Không xóa nếu đã có feedback
    cnt = db.query(models.Feedback).filter(models.Feedback.user_id == user_id).count()

    return cnt == 0

def delete_student(db: Session, user_id: int) -> bool:
    u = db.get(models.User, user_id)
    if not u:

        return False
    # Thử giữ lại phản hồi bằng cách set user_id = NULL;
    # nếu cột chưa nullable (migration chưa chạy) thì xóa luôn feedback.

    try:
        db.query(models.Feedback).filter(models.Feedback.user_id == user_id).update(
            {models.Feedback.user_id: None}, synchronize_session=False
        )
        db.flush()  # phát hiện lỗi trước commit

    except Exception:

        db.rollback()

        # Fallback: xóa feedback liên quan rồi mới xóa user
        db.query(models.Feedback).filter(models.Feedback.user_id == user_id).delete(synchronize_session=False)
    db.delete(u)
    db.commit()

    return True



# ============================== CLASS (LỚP) ==============================

def create_class(
    db: Session,
    name: str,
    homeroom_teacher_ids: Optional[List[int]] = None,

) -> models.Class:
    existed = db.query(models.Class).filter(models.Class.name == name).first()
    if existed:
        return existed
    c = models.Class(name=name)
    if homeroom_teacher_ids is not None:
        teachers = (
            db.query(models.Teacher)
              .filter(models.Teacher.id.in_(homeroom_teacher_ids))

              .all()
        )
        c.homeroom_teachers = [t for t in teachers if t.role == "homeroom_teacher"]
    db.add(c)
    db.commit()
    db.refresh(c)
    return c

def get_all_classes(db: Session, limit: int = 200, offset: int = 0) -> List[Dict]:
    rows = (
        db.query(models.Class)
          .options(
              joinedload(models.Class.students),
              joinedload(models.Class.homeroom_teachers),
          )
          .order_by(func.lower(models.Class.name).asc(), models.Class.id.asc())
          .offset(offset)
          .limit(limit)
          .all()
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "student_count": len(c.students),
            "homeroom_teacher_ids": [t.id for t in c.homeroom_teachers],
            "homeroom_teacher_names": [t.full_name for t in c.homeroom_teachers],
            "homeroom_teacher_name": c.homeroom_teachers[0].full_name if c.homeroom_teachers else None,
        }
        for c in rows
    ]

def get_class_by_name(db: Session, name: str) -> Optional[models.Class]:
    return db.query(models.Class).filter(models.Class.name == name).first()

def update_class(
    db: Session,
    class_id: int,
    name: str,
    homeroom_teacher_ids: Optional[List[int]] = None,
) -> Optional[models.Class]:
    c = db.get(models.Class, class_id)
    if not c:
        return None
    c.name = name
    if homeroom_teacher_ids is not None:
        teachers = (
            db.query(models.Teacher)
              .filter(models.Teacher.id.in_(homeroom_teacher_ids))
              .all()
        )
        c.homeroom_teachers = [t for t in teachers if t.role == "homeroom_teacher"]
    db.commit()
    db.refresh(c)
    return c

def can_delete_class(db: Session, class_id: int) -> Tuple[bool, str]:
    stu_cnt = db.query(models.User).filter(models.User.class_id == class_id).count()
    if stu_cnt > 0:
        return False, "Lớp còn học sinh — hãy chuyển lớp hoặc xóa học sinh trước"
    return True, ""

def delete_class(db: Session, class_id: int) -> Tuple[bool, str]:
    c = db.get(models.Class, class_id)
    if not c:
        return False, "Không tìm thấy lớp"
    ok, reason = can_delete_class(db, class_id)
    if not ok:
        return False, reason
    db.delete(c)
    db.commit()
    return True, "Đã xóa lớp"


# ============================ SEMESTER (KÌ HỌC) ==============================

def create_semester(
    db: Session, name: str, start_date=None, end_date=None
) -> models.Semester:
    derived_active = _calc_semester_is_active(start_date, end_date)
    s = models.Semester(
        name=name, start_date=start_date, end_date=end_date, is_active=derived_active
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s

def get_all_semesters(db: Session, limit: int = 100, offset: int = 0) -> List[Dict]:
    rows = (
        db.query(models.Semester)
          .order_by(models.Semester.id.desc())
          .offset(offset)
          .limit(limit)
          .all()
    )
    return [
        {
            "id": s.id,
            "name": s.name,
            "start_date": s.start_date,
            "end_date": s.end_date,
            "is_active": _calc_semester_is_active(s.start_date, s.end_date),
        }
        for s in rows
    ]

def get_semester(db: Session, semester_id: int) -> Optional[models.Semester]:
    return db.get(models.Semester, semester_id)

def get_subjects_by_semester(db: Session, semester_id: int) -> List[Dict]:
    """Lấy danh sách môn học theo kì học"""
    rows = (
        db.query(models.Subject)
                    .options(joinedload(models.Subject.teachers), joinedload(models.Subject.classes))
          .filter(models.Subject.semester_id == semester_id)
                        .filter((models.Subject.description.is_(None)) | (models.Subject.description != DELETED_SUBJECT_DESC))
          .order_by(func.lower(models.Subject.name).asc(), models.Subject.id.asc())
          .all()
    )
    return [
        {
            "id": s.id,
            "name": s.name,
            "teacher": s.teacher,
            "teacher_ids": [t.id for t in s.teachers],
            "teacher_names": [t.full_name for t in s.teachers],
            "class_ids": [c.id for c in s.classes],
            "class_names": [c.name for c in s.classes],
            "description": s.description,
        }
        for s in rows
    ]

def update_semester(
    db: Session,
    semester_id: int,
    name: Optional[str] = None,
    start_date=None,
    end_date=None,
) -> Optional[models.Semester]:
    s = db.get(models.Semester, semester_id)
    if not s:
        return None
    if name is not None:
        s.name = name
    if start_date is not None:
        s.start_date = start_date
    if end_date is not None:
        s.end_date = end_date
    s.is_active = _calc_semester_is_active(s.start_date, s.end_date)
    db.commit()
    db.refresh(s)
    return s

def delete_semester(db: Session, semester_id: int) -> Tuple[bool, str]:
    s = db.get(models.Semester, semester_id)
    if not s:
        return False, "Không tìm thấy kì học"
    # Kiểm tra có môn học liên kết không
    count = db.query(models.Subject).filter(models.Subject.semester_id == semester_id).count()
    if count > 0:
        return False, f"Không thể xóa kì học này vì có {count} môn học liên kết"
    db.delete(s)
    db.commit()
    return True, "Đã xóa kì học"


# ============================ SUBJECT (MÔN) ==============================

def create_subject(
    db: Session,
    name: str,
    description: str,
    teacher: str,
    class_ids: Optional[List[int]] = None,
    semester_id: Optional[int] = None,
    teacher_ids: Optional[List[int]] = None,
) -> models.Subject:
    """Tạo môn học mới với danh sách lớp"""
    s = models.Subject(
        name=name, description=description, teacher=teacher, semester_id=semester_id
    )
    
    # Thêm lớp học
    if class_ids:
        for class_id in class_ids:
            c = db.get(models.Class, class_id)
            if c:
                s.classes.append(c)

    if teacher_ids is not None:
        teachers = (
            db.query(models.Teacher)
              .filter(models.Teacher.id.in_(teacher_ids))
              .all()
        )
        s.teachers = teachers
    
    db.add(s)
    db.commit()
    db.refresh(s)
    return s

def get_all_subjects(db: Session, limit: int = 500, offset: int = 0) -> List[Dict]:
    rows = (
        db.query(models.Subject)
          .options(
              joinedload(models.Subject.classes),
              joinedload(models.Subject.semester),
              joinedload(models.Subject.teachers),
          )
            .filter((models.Subject.description.is_(None)) | (models.Subject.description != DELETED_SUBJECT_DESC))
                    .order_by(func.lower(models.Subject.name).asc(), models.Subject.id.asc())
          .offset(offset)
          .limit(limit)
          .all()
    )
    return [
        {
            "id": s.id,
            "name": s.name,
            "teacher": s.teacher,
            "teacher_ids": [t.id for t in s.teachers],
            "teacher_names": [t.full_name for t in s.teachers],
            "class_id": s.classes[0].id if s.classes else None,
            "class_name": s.classes[0].name if s.classes else None,
            "class_ids": [c.id for c in s.classes],
            "class_names": [c.name for c in s.classes],
            "semester_id": s.semester_id,
            "semester_name": s.semester.name if s.semester else None,
        }
        for s in rows
    ]

def get_subjects_by_class(db: Session, class_id: int) -> List[Dict]:
    rows = (
        db.query(models.Subject)
          .options(joinedload(models.Subject.teachers), joinedload(models.Subject.semester))
          .join(models.Subject.classes)
          .filter(models.Class.id == class_id)
          .filter((models.Subject.description.is_(None)) | (models.Subject.description != DELETED_SUBJECT_DESC))
                    .order_by(func.lower(models.Subject.name).asc(), models.Subject.id.asc())
          .all()
    )
    return [
        {
            "id": s.id,
            "name": s.name,
            "teacher": s.teacher,
            "teacher_ids": [t.id for t in s.teachers],
            "teacher_names": [t.full_name for t in s.teachers],
            "class_ids": [c.id for c in s.classes],
            "class_names": [c.name for c in s.classes],
            "semester_id": s.semester_id,
            "semester_name": s.semester.name if s.semester else None,
        }
        for s in rows
    ]
def update_subject(
    db: Session,
    subject_id: int,
    name: Optional[str] = None,
    teacher: Optional[str] = None,
    teacher_ids: Optional[List[int]] = None,
    class_ids: Optional[List[int]] = None,
    description: Optional[str] = None,
    semester_id: Optional[int] = None,
) -> Optional[models.Subject]:
    s = db.get(models.Subject, subject_id)
    if not s:
        return None
    if name is not None:
        s.name = name
    if teacher is not None:
        s.teacher = teacher
    if teacher_ids is not None:
        teachers = (
            db.query(models.Teacher)
              .filter(models.Teacher.id.in_(teacher_ids))
              .all()
        )
        s.teachers = teachers
    if class_ids is not None:
        # Xóa tất cả lớp cũ
        s.classes = []
        # Thêm lớp mới
        for class_id in class_ids:
            c = db.get(models.Class, class_id)
            if c:
                s.classes.append(c)
    if description is not None:
        s.description = description
    if semester_id is not None:
        s.semester_id = semester_id
    db.commit()
    db.refresh(s)
    return s

def can_delete_subject(db: Session, subject_id: int) -> bool:
    s = db.get(models.Subject, subject_id)
    if not s or _is_deleted_subject(s):
        return False
    return True

def delete_subject(db: Session, subject_id: int) -> Tuple[bool, str]:
    s = db.get(models.Subject, subject_id)
    if not s:
        return False, "Không tìm thấy môn học"

    if _is_deleted_subject(s):
        return False, "Không thể xóa môn hệ thống"

    feedback_count = db.query(models.Feedback).filter(models.Feedback.subject_id == subject_id).count()
    if feedback_count > 0:
        archived_subject = _get_or_create_deleted_subject(db)
        db.query(models.Feedback).filter(models.Feedback.subject_id == subject_id).update(
            {models.Feedback.subject_id: archived_subject.id},
            synchronize_session=False,
        )

    db.delete(s)
    db.commit()
    if feedback_count > 0:
        return True, f"Đã xóa môn học và chuyển {feedback_count} phản hồi sang môn lưu trữ"
    return True, "Đã xóa môn học"


# ============================ TEACHER (GIẢNG VIÊN) ==============================

def _to_teacher_dict(t: models.Teacher) -> Dict:
    return {
        "id": t.id,
        "teacher_code": t.teacher_code,
        "full_name": t.full_name,
        "email": t.email,
        "role": t.role,
        "subject_ids": [s.id for s in t.subjects],
        "subject_names": [s.name for s in t.subjects],
        "class_ids": [c.id for c in t.homeroom_classes],
        "class_names": [c.name for c in t.homeroom_classes],
    }


def get_teacher_by_code(db: Session, teacher_code: str) -> Optional[models.Teacher]:
    return (
        db.query(models.Teacher)
                    .options(joinedload(models.Teacher.subjects), joinedload(models.Teacher.homeroom_classes))
          .filter(models.Teacher.teacher_code == teacher_code)
          .first()
    )


def get_teacher_by_email(db: Session, email: str) -> Optional[models.Teacher]:
    if not email:
        return None
    return db.query(models.Teacher).filter(func.lower(models.Teacher.email) == email.strip().lower()).first()


def create_teacher(
    db: Session,
    teacher_code: str,
    full_name: str,
    email: Optional[str],
    role: str,
    subject_ids: Optional[List[int]] = None,
    class_ids: Optional[List[int]] = None,
) -> models.Teacher:
    t = models.Teacher(
        teacher_code=teacher_code.strip(),
        full_name=full_name.strip(),
        email=(email or "").strip() or None,
        role=role,
    )

    if role == "subject_teacher" and subject_ids:
        subjects = db.query(models.Subject).filter(models.Subject.id.in_(subject_ids)).all()
        t.subjects = subjects
    if role == "homeroom_teacher" and class_ids:
        classes = db.query(models.Class).filter(models.Class.id.in_(class_ids)).all()
        t.homeroom_classes = classes

    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def get_all_teachers(db: Session, limit: int = 500, offset: int = 0) -> List[Dict]:
    rows = (
        db.query(models.Teacher)
                    .options(joinedload(models.Teacher.subjects), joinedload(models.Teacher.homeroom_classes))
          .order_by(func.lower(models.Teacher.full_name).asc(), models.Teacher.id.asc())
          .offset(offset)
          .limit(limit)
          .all()
    )
    return [_to_teacher_dict(t) for t in rows]


def update_teacher(
    db: Session,
    teacher_id: int,
    teacher_code: Optional[str] = None,
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    role: Optional[str] = None,
    subject_ids: Optional[List[int]] = None,
    class_ids: Optional[List[int]] = None,
) -> Optional[models.Teacher]:
    t = db.get(models.Teacher, teacher_id)
    if not t:
        return None

    if teacher_code is not None:
        t.teacher_code = teacher_code.strip()
    if full_name is not None:
        t.full_name = full_name.strip()
    if email is not None:
        t.email = email.strip() or None
    if role is not None:
        t.role = role

    effective_role = role if role is not None else t.role
    if effective_role == "subject_teacher":
        if subject_ids is not None:
            subjects = db.query(models.Subject).filter(models.Subject.id.in_(subject_ids)).all()
            t.subjects = subjects
        t.homeroom_classes = []
    elif effective_role == "homeroom_teacher":
        if class_ids is not None:
            classes = db.query(models.Class).filter(models.Class.id.in_(class_ids)).all()
            t.homeroom_classes = classes
        t.subjects = []
    else:
        # Giảng viên chủ nhiệm không cần gắn danh sách môn bộ môn.
        t.subjects = []
        t.homeroom_classes = []

    db.commit()
    db.refresh(t)
    return t


def delete_teacher(db: Session, teacher_id: int) -> Tuple[bool, str]:
    t = db.get(models.Teacher, teacher_id)
    if not t:
        return False, "Không tìm thấy giảng viên"

    db.delete(t)
    db.commit()
    return True, "Đã xóa giảng viên"


def get_homeroom_teachers_by_class(db: Session, class_id: int) -> List[models.Teacher]:
        return (
                db.query(models.Teacher)
                    .join(models.Teacher.homeroom_classes)
                    .filter(models.Teacher.role == "homeroom_teacher", models.Class.id == class_id)
                    .all()
        )


# =============================== FEEDBACK ================================

def create_feedback(
    db: Session,
    user_id: int,
    subject_id: int,
    text: str,
    label: str,
    prob_neg: float,
    prob_pos: float,
) -> models.Feedback:
    existing = (
        db.query(models.Feedback)
          .filter(models.Feedback.user_id == user_id, models.Feedback.subject_id == subject_id)
          .first()
    )
    if existing:
        raise ValueError("Mỗi sinh viên chỉ được đánh giá mỗi môn học 1 lần")

    fb = models.Feedback(
        user_id=user_id,
        subject_id=subject_id,
        text=text,
        label=label,
        prob_neg=prob_neg,
        prob_pos=prob_pos,
        created_at=vietnam_now(),  # Sử dụng giờ Việt Nam
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return fb

def _to_feedback_dict(f: models.Feedback) -> Dict:
    ts = f.created_at.isoformat(timespec="seconds") if f.created_at else ""
    return {
        "id": f.id,
        "user_id": f.user_id,
        "subject_id": f.subject_id,
        "student": f.user.fullname if f.user else None,
        "subject": f.subject.name if f.subject else None,
        "semester_id": f.subject.semester_id if f.subject else None,
        "semester_name": f.subject.semester.name if (f.subject and f.subject.semester) else None,
        "text": f.text,
        "label": f.label,
        "prob_neg": f.prob_neg,
        "prob_pos": f.prob_pos,
        # Derive neutral probability on read-time for older rows that only stored pos/neg
        "prob_neu": (
            f.prob_neu if (hasattr(f, "prob_neu") and f.prob_neu is not None)
            else max(0.0, 1.0 - (f.prob_pos or 0.0) - (f.prob_neg or 0.0))
        ),
        "created_at": ts,
    }

def get_feedbacks(
    db: Session, limit: int = 50, offset: int = 0, order: str = "desc"
) -> List[Dict]:
    q = db.query(models.Feedback).options(
        joinedload(models.Feedback.user),
        joinedload(models.Feedback.subject).joinedload(models.Subject.semester),
    )
    q = q.order_by(
        models.Feedback.created_at.asc() if order == "asc"
        else models.Feedback.created_at.desc()
    )
    rows = q.offset(offset).limit(limit).all()
    return [_to_feedback_dict(f) for f in rows]

def get_feedbacks_by_class(
    db: Session, class_id: int, limit: int = 100, offset: int = 0, order: str = "desc"
) -> List[Dict]:
    q = (
        db.query(models.Feedback)
          .join(models.User)
          .options(
              joinedload(models.Feedback.user), 
              joinedload(models.Feedback.subject).joinedload(models.Subject.semester)
          )
          .filter(models.User.class_id == class_id)
    )
    q = q.order_by(
        models.Feedback.created_at.asc() if order == "asc"
        else models.Feedback.created_at.desc()
    )
    rows = q.offset(offset).limit(limit).all()
    return [_to_feedback_dict(f) for f in rows]

def get_feedbacks_by_user(
    db: Session, user_id: int, limit: int = 100, offset: int = 0, order: str = "desc"
) -> List[Dict]:
    q = (
        db.query(models.Feedback)
          .options(
              joinedload(models.Feedback.user), 
              joinedload(models.Feedback.subject).joinedload(models.Subject.semester)
          )
          .filter(models.Feedback.user_id == user_id)
    )
    q = q.order_by(
        models.Feedback.created_at.asc() if order == "asc"
        else models.Feedback.created_at.desc()
    )
    rows = q.offset(offset).limit(limit).all()
    return [_to_feedback_dict(f) for f in rows]


def get_feedback_by_user_and_subject(
    db: Session,
    user_id: int,
    subject_id: int,
    exclude_feedback_id: Optional[int] = None,
) -> Optional[models.Feedback]:
    q = db.query(models.Feedback).filter(
        models.Feedback.user_id == user_id,
        models.Feedback.subject_id == subject_id,
    )
    if exclude_feedback_id is not None:
        q = q.filter(models.Feedback.id != exclude_feedback_id)
    return q.first()


def update_feedback(
    db: Session,
    feedback_id: int,
    user_id: int,
    subject_id: int,
    text: str,
) -> Optional[Dict]:
    fb = (
        db.query(models.Feedback)
          .filter(models.Feedback.id == feedback_id, models.Feedback.user_id == user_id)
          .first()
    )
    if not fb:
        return None

    duplicate = get_feedback_by_user_and_subject(db, user_id, subject_id, exclude_feedback_id=feedback_id)
    if duplicate:
        raise ValueError("Mỗi sinh viên chỉ được đánh giá mỗi môn học 1 lần")

    from . import sentiment_model

    label, prob_neg, prob_pos = sentiment_model.predict_sentiment(text)
    fb.subject_id = subject_id
    fb.text = text
    fb.label = label
    fb.prob_neg = prob_neg
    fb.prob_pos = prob_pos
    db.commit()
    db.refresh(fb)
    return _to_feedback_dict(fb)


def delete_feedback(
    db: Session,
    feedback_id: int,
    user_id: int,
) -> bool:
    fb = (
        db.query(models.Feedback)
          .filter(models.Feedback.id == feedback_id, models.Feedback.user_id == user_id)
          .first()
    )
    if not fb:
        return False
    db.delete(fb)
    db.commit()
    return True


def get_survey_responses_with_sentiment(
    db: Session, limit: int = 50, offset: int = 0
) -> List[Dict]:
    """Lấy tất cả survey text responses kèm sentiment analysis"""
    from . import sentiment_model
    
    # Query survey text responses
    rows = (
        db.query(models.SurveyTextResponse)
          .order_by(models.SurveyTextResponse.created_at.desc())
          .offset(offset)
          .limit(limit)
          .all()
    )
    
    result = []
    for response in rows:
        # Get user info
        user = db.query(models.User).filter(models.User.id == response.user_id).first() if response.user_id else None
        
        # Get survey info
        survey = db.query(models.Survey).filter(models.Survey.id == response.survey_id).first() if response.survey_id else None
        
        # Phân tích sentiment and include probabilities
        label, prob_neg, prob_pos, prob_neu = sentiment_model.predict_sentiment_full(response.response_text or "")
        normalized_label = str(label or "NEU").upper()
        
        result.append({
            "id": response.id,
            "survey_id": survey.id if survey else None,
            "survey_title": survey.title if survey else None,
            "survey_name": survey.title if survey else None,
            "user_id": response.user_id,
            "student": user.fullname if user else None,
            "student_name": user.fullname if user else None,
            "content": response.response_text,
            "text": response.response_text,
            "feedback_text": response.response_text,
            "label": normalized_label,
            "prob_neg": prob_neg,
            "prob_pos": prob_pos,
            "prob_neu": prob_neu,
            "created_at": response.created_at.isoformat(timespec="seconds") if response.created_at else "",
        })
    
    return result

# ================================ STATS =================================

def get_stats(db: Session) -> Dict:
    total = db.query(models.Feedback).count()
    pos = db.query(models.Feedback).filter(models.Feedback.label == "POS").count()
    neg = db.query(models.Feedback).filter(models.Feedback.label == "NEG").count()
    return {
        "total": total,
        "POS": pos,
        "NEG": neg,
        "NEG_rate": round(neg / total, 2) if total else 0,
    }


# ================================ NEWS =================================

def _to_news_post_dict(
    p: models.NewsPost,
    like_count: int = 0,
    comment_count: int = 0,
    liked_by_me: bool = False,
) -> Dict:
    return {
        "id": p.id,
        "title": p.title,
        "content": p.content,
        "media_url": p.media_url,
        "media_type": p.media_type,
        "is_published": p.is_published,
        "like_count": like_count,
        "comment_count": comment_count,
        "liked_by_me": liked_by_me,
        "created_at": p.created_at.isoformat(timespec="seconds") if p.created_at else "",
        "updated_at": p.updated_at.isoformat(timespec="seconds") if p.updated_at else "",
    }


def _serialize_news_posts(db: Session, rows: List[models.NewsPost], anon_id: Optional[str] = None) -> List[Dict]:
    post_ids = [row.id for row in rows]
    if not post_ids:
        return []

    like_counts = dict(
        db.query(models.NewsLike.post_id, func.count(models.NewsLike.id))
          .filter(models.NewsLike.post_id.in_(post_ids))
          .group_by(models.NewsLike.post_id)
          .all()
    )
    comment_counts = dict(
        db.query(models.NewsComment.post_id, func.count(models.NewsComment.id))
          .filter(models.NewsComment.post_id.in_(post_ids))
          .group_by(models.NewsComment.post_id)
          .all()
    )
    liked_ids = set()
    if anon_id:
        liked_ids = {
            post_id
            for (post_id,) in db.query(models.NewsLike.post_id)
                                 .filter(models.NewsLike.post_id.in_(post_ids))
                                 .filter(models.NewsLike.anonymous_id == anon_id)
                                 .all()
        }

    return [
        _to_news_post_dict(
            row,
            like_count=int(like_counts.get(row.id, 0) or 0),
            comment_count=int(comment_counts.get(row.id, 0) or 0),
            liked_by_me=row.id in liked_ids,
        )
        for row in rows
    ]


def _auto_news_title(content: str) -> str:
    plain = (content or "").strip().replace("\n", " ")
    if not plain:
        return "Tin tức"
    return plain[:80]


def _normalize_media(media_type: Optional[str], media_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    url = (media_url or "").strip() or None
    if not url:
        return None, None
    kind = (media_type or "").strip().lower()
    if kind not in {"image", "video"}:
        lower_url = url.lower()
        if any(lower_url.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"]):
            kind = "image"
        else:
            kind = "video"
    return kind, url


def create_news_post(
    db: Session,
    content: str,
    title: Optional[str] = None,
    media_url: Optional[str] = None,
    media_type: Optional[str] = None,
    is_published: bool = False,
) -> models.NewsPost:
    normalized_type, normalized_url = _normalize_media(media_type, media_url)
    post = models.NewsPost(
        title=(title or "").strip() or _auto_news_title(content),
        content=content.strip(),
        media_url=normalized_url,
        media_type=normalized_type,
        is_published=is_published,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def get_all_news_posts(db: Session, limit: int = 500, offset: int = 0) -> List[Dict]:
    rows = (
        db.query(models.NewsPost)
          .order_by(models.NewsPost.created_at.desc(), models.NewsPost.id.desc())
          .offset(offset)
          .limit(limit)
          .all()
    )
    return _serialize_news_posts(db, rows)


def get_published_news_posts(db: Session, limit: int = 200, offset: int = 0, anon_id: Optional[str] = None) -> List[Dict]:
    rows = (
        db.query(models.NewsPost)
          .filter(models.NewsPost.is_published.is_(True))
          .order_by(models.NewsPost.created_at.desc(), models.NewsPost.id.desc())
          .offset(offset)
          .limit(limit)
          .all()
    )
    return _serialize_news_posts(db, rows, anon_id=anon_id)


def get_news_post_by_id(db: Session, post_id: int) -> Optional[models.NewsPost]:
    return db.query(models.NewsPost).filter(models.NewsPost.id == post_id).first()


def _news_comment_select_columns(db: Session) -> List[str]:
    base_columns = ["c.id", "c.post_id", "c.content", "c.created_at", "p.content AS post_content"]
    try:
        bind = db.get_bind()
        if bind is None:
            return base_columns
        column_names = {column["name"] for column in inspect(bind).get_columns("news_comments")}
    except Exception:
        return base_columns

    if "sentiment_label" in column_names:
        base_columns.append("c.sentiment_label")
    if "prob_neg" in column_names:
        base_columns.append("c.prob_neg")
    if "prob_pos" in column_names:
        base_columns.append("c.prob_pos")
    if "prob_neu" in column_names:
        base_columns.append("c.prob_neu")
    return base_columns


def _serialize_news_comment_row(row) -> Dict:
    created_at = row.get("created_at")
    return {
        "id": row.get("id"),
        "post_id": row.get("post_id"),
        "post_content": row.get("post_content", ""),
        "content": row.get("content", ""),
        "sentiment_label": row.get("sentiment_label"),
        "prob_neg": row.get("prob_neg"),
        "prob_pos": row.get("prob_pos"),
        "prob_neu": row.get("prob_neu"),
        "created_at": created_at.isoformat(timespec="seconds") if created_at else "",
    }


def get_news_comments(db: Session, post_id: int, limit: int = 50, offset: int = 0) -> List[Dict]:
    columns = ", ".join(_news_comment_select_columns(db))
    sql = text(
        f"""
        SELECT {columns}
        FROM news_comments c
        JOIN news_posts p ON p.id = c.post_id
        WHERE c.post_id = :post_id
        ORDER BY c.created_at DESC, c.id DESC
        LIMIT :limit OFFSET :offset
        """
    )
    try:
        rows = db.execute(sql, {"post_id": post_id, "limit": limit, "offset": offset}).mappings().all()
    except Exception:
        return []
    return [_serialize_news_comment_row(row) for row in rows]


def get_all_news_comments(
    db: Session,
    limit: int = 1000,
    offset: int = 0,
    post_id: Optional[int] = None,
) -> List[Dict]:
    columns = ", ".join(_news_comment_select_columns(db))
    sql = [
        f"SELECT {columns}",
        "FROM news_comments c",
        "JOIN news_posts p ON p.id = c.post_id",
    ]
    params = {"limit": limit, "offset": offset}
    if post_id is not None:
        sql.append("WHERE c.post_id = :post_id")
        params["post_id"] = post_id
    sql.extend([
        "ORDER BY c.created_at DESC, c.id DESC",
        "LIMIT :limit OFFSET :offset",
    ])
    try:
        rows = db.execute(text("\n".join(sql)), params).mappings().all()
    except Exception:
        return []
    return [_serialize_news_comment_row(row) for row in rows]


def toggle_news_like(db: Session, post_id: int, anonymous_id: str) -> Dict:
    post = db.get(models.NewsPost, post_id)
    if not post:
        raise ValueError("Không tìm thấy bài viết")

    existing = (
        db.query(models.NewsLike)
          .filter(models.NewsLike.post_id == post_id)
          .filter(models.NewsLike.anonymous_id == anonymous_id)
          .first()
    )
    if existing:
        db.delete(existing)
        db.commit()
        liked = False
    else:
        db.add(models.NewsLike(post_id=post_id, anonymous_id=anonymous_id))
        db.commit()
        liked = True

    like_count = (
        db.query(models.NewsLike)
          .filter(models.NewsLike.post_id == post_id)
          .count()
    )
    return {"liked": liked, "like_count": like_count}


def add_news_comment(
    db: Session,
    post_id: int,
    anonymous_id: str,
    content: str,
    sentiment_label: Optional[str] = None,
    prob_neg: Optional[float] = None,
    prob_pos: Optional[float] = None,
    prob_neu: Optional[float] = None,
) -> Dict:
    post = db.get(models.NewsPost, post_id)
    if not post:
        raise ValueError("Không tìm thấy bài viết")
    text = (content or "").strip()
    if not text:
        raise ValueError("Nội dung bình luận không được để trống")

    normalized_label = (sentiment_label or "NEU").strip().upper()
    if normalized_label not in {"POS", "NEG", "NEU"}:
        normalized_label = "NEU"

    comment = models.NewsComment(
        post_id=post_id,
        anonymous_id=anonymous_id,
        content=text,
        sentiment_label=normalized_label,
        prob_neg=prob_neg,
        prob_pos=prob_pos,
        prob_neu=prob_neu,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)

    comment_count = (
        db.query(models.NewsComment)
          .filter(models.NewsComment.post_id == post_id)
          .count()
    )
    return {
        "id": comment.id,
        "post_id": comment.post_id,
        "content": comment.content,
        "sentiment_label": comment.sentiment_label,
        "prob_neg": comment.prob_neg,
        "prob_pos": comment.prob_pos,
        "prob_neu": comment.prob_neu,
        "created_at": comment.created_at.isoformat(timespec="seconds") if comment.created_at else "",
        "comment_count": comment_count,
    }


def update_news_post(
    db: Session,
    post_id: int,
    title: Optional[str] = None,
    content: Optional[str] = None,
    media_url: Optional[str] = None,
    media_type: Optional[str] = None,
    is_published: Optional[bool] = None,
) -> Optional[models.NewsPost]:
    post = db.get(models.NewsPost, post_id)
    if not post:
        return None
    if title is not None:
        post.title = title.strip()
    if content is not None:
        post.content = content.strip()
        if title is None:
            post.title = _auto_news_title(post.content)
    if media_url is not None or media_type is not None:
        normalized_type, normalized_url = _normalize_media(media_type, media_url)
        post.media_type = normalized_type
        post.media_url = normalized_url
    if is_published is not None:
        post.is_published = is_published
    db.commit()
    db.refresh(post)
    return post


def delete_news_post(db: Session, post_id: int) -> Tuple[bool, str]:
    post = db.get(models.NewsPost, post_id)
    if not post:
        return False, "Không tìm thấy bài viết"
    db.delete(post)
    db.commit()
    return True, "Đã xóa bài viết"
def save_chat_message(
    db: Session,
    conversation_id: str,
    student_id: str,
    role: str,                 # "user" | "assistant"
    text: str,
    sentiment: str,            # "pos" | "neg" | "neu"
    escalated: bool
) -> ChatMessage:
    obj = ChatMessage(
        conversation_id=conversation_id or None,
        student_id=student_id or None,
        message_role=role,
        message_text=text,
        sentiment=sentiment,
        escalated=escalated,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def create_alert(
    db: Session,
    student_id: str,
    conversation_id: str,
    trigger_text: str,
    risk_level: str = "high"
) -> Alert:
    alert = Alert(
        student_id=student_id or None,
        conversation_id=conversation_id or None,
        trigger_text=trigger_text,
        risk_level=risk_level,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


# (tuỳ chọn) vài hàm đọc nhanh để bạn xem trên dashboard/admin
def get_recent_chat_messages(db: Session, limit: int = 100) -> List[Dict]:
    rows = (
        db.query(ChatMessage)
          .order_by(ChatMessage.created_at.desc())
          .limit(limit).all()
    )
    return [
        {
            "id": r.id,
            "conversation_id": r.conversation_id,
            "student_id": r.student_id,
            "role": r.message_role,
            "text": r.message_text,
            "sentiment": r.sentiment,
            "escalated": r.escalated,
            "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else "",
        }
        for r in rows
    ]


def get_recent_alerts(db: Session, limit: int = 50) -> List[Dict]:
    rows = (
        db.query(Alert)
          .order_by(Alert.created_at.desc())
          .limit(limit).all()
    )
    return [
        {
            "id": a.id,
            "conversation_id": a.conversation_id,
            "student_id": a.student_id,
            "risk_level": a.risk_level,
            "trigger_text": a.trigger_text,
            "created_at": a.created_at.isoformat(timespec="seconds") if a.created_at else "",
        }
        for a in rows
    ]


# ============================ KHẢO SÁT ============================

def _parse_survey_options(raw_options: Optional[str]) -> List[str]:
    if not raw_options:
        return []
    normalized = raw_options.replace("\r", "\n").replace(";", "\n")
    items: List[str] = []
    for line in normalized.split("\n"):
        item = line.strip().lstrip("-•*").strip()
        if item:
            items.append(item)
    return items


def _survey_option_counts(db: Session, survey_id: int) -> Dict[int, int]:
    rows = (
        db.query(models.SurveyResponse.option_id, func.count(models.SurveyResponse.id))
          .filter(models.SurveyResponse.survey_id == survey_id)
          .group_by(models.SurveyResponse.option_id)
          .all()
    )
    return {option_id: count for option_id, count in rows}


def _survey_text_response_count(db: Session, survey_id: int) -> int:
        return (
                db.query(models.SurveyTextResponse)
                    .filter(models.SurveyTextResponse.survey_id == survey_id)
                    .count()
        )


def _serialize_survey(db: Session, survey: models.Survey, user_id: Optional[int] = None) -> Dict:
    counts = _survey_option_counts(db, survey.id)
    option_responses = sum(counts.values())
    text_responses = _survey_text_response_count(db, survey.id)
    total_responses = option_responses + text_responses
    selected_option_id = None
    text_response = None
    if user_id is not None:
        response = (
            db.query(models.SurveyResponse)
              .filter(models.SurveyResponse.survey_id == survey.id)
              .filter(models.SurveyResponse.user_id == user_id)
              .first()
        )
        selected_option_id = response.option_id if response else None
        text_row = (
            db.query(models.SurveyTextResponse)
              .filter(models.SurveyTextResponse.survey_id == survey.id)
              .filter(models.SurveyTextResponse.user_id == user_id)
              .first()
        )
        text_response = text_row.response_text if text_row else None

    options = []
    for option in survey.options:
        vote_count = counts.get(option.id, 0)
        options.append({
            "id": option.id,
            "option_text": option.option_text,
            "sort_order": option.sort_order,
            "vote_count": vote_count,
            "vote_percent": round((vote_count / total_responses) * 100, 1) if total_responses else 0,
        })

    return {
        "id": survey.id,
        "title": survey.title,
        "class_id": survey.class_id,
        "class_name": survey.class_.name if survey.class_ else None,
        "is_published": survey.is_published,
        "created_at": survey.created_at.isoformat(timespec="seconds") if survey.created_at else "",
        "updated_at": survey.updated_at.isoformat(timespec="seconds") if survey.updated_at else "",
        "option_count": len(options),
        "response_count": total_responses,
        "responded_by_me": selected_option_id is not None or bool((text_response or "").strip()),
        "selected_option_id": selected_option_id,
        "text_response": text_response,
        "options": options,
    }


def create_survey(
    db: Session,
    title: str,
    class_id: int,
    options_text: Optional[str] = None,
    is_published: bool = True,
) -> Dict:
    title_value = (title or "").strip()
    if not title_value:
        raise ValueError("Tiêu đề khảo sát không được để trống")

    target_class = db.get(models.Class, class_id)
    if not target_class:
        raise ValueError("Không tìm thấy lớp khảo sát")

    options = _parse_survey_options(options_text)
    if options_text and len(options) < 2:
        raise ValueError("Nếu nhập lựa chọn khảo sát thì cần ít nhất 2 lựa chọn")

    survey = models.Survey(
        title=title_value,
        class_id=class_id,
        is_published=is_published,
    )
    db.add(survey)
    db.flush()

    for index, option_text in enumerate(options):
        db.add(models.SurveyOption(survey_id=survey.id, option_text=option_text, sort_order=index))

    db.commit()
    db.refresh(survey)
    return _serialize_survey(db, survey)


def get_survey_by_id(db: Session, survey_id: int) -> Optional[models.Survey]:
    return (
        db.query(models.Survey)
          .options(joinedload(models.Survey.class_), joinedload(models.Survey.options))
          .filter(models.Survey.id == survey_id)
          .first()
    )


def get_all_surveys(db: Session, limit: int = 200, offset: int = 0) -> List[Dict]:
    rows = (
        db.query(models.Survey)
          .options(joinedload(models.Survey.class_), joinedload(models.Survey.options))
          .order_by(models.Survey.created_at.desc(), models.Survey.id.desc())
          .offset(offset)
          .limit(limit)
          .all()
    )
    return [_serialize_survey(db, survey) for survey in rows]


def get_survey_detail_for_admin(db: Session, survey_id: int) -> Dict:
    survey = (
        db.query(models.Survey)
          .options(joinedload(models.Survey.class_), joinedload(models.Survey.options))
          .filter(models.Survey.id == survey_id)
          .first()
    )
    if not survey:
        raise ValueError("Không tìm thấy khảo sát")

    students = (
        db.query(models.User)
          .filter(models.User.role == "student")
          .filter(models.User.class_id == survey.class_id)
          .order_by(func.lower(func.coalesce(models.User.fullname, "")).asc(), models.User.id.asc())
          .all()
    )

    choice_rows = (
        db.query(models.SurveyResponse)
          .options(joinedload(models.SurveyResponse.user), joinedload(models.SurveyResponse.option))
          .filter(models.SurveyResponse.survey_id == survey_id)
          .all()
    )
    text_rows = (
        db.query(models.SurveyTextResponse)
          .filter(models.SurveyTextResponse.survey_id == survey_id)
          .all()
    )

    text_user_ids = {row.user_id for row in text_rows if row.user_id is not None}
    text_users = {}
    if text_user_ids:
        user_rows = (
            db.query(models.User)
              .filter(models.User.id.in_(text_user_ids))
              .all()
        )
        text_users = {u.id: u for u in user_rows}

    responded_map: Dict[int, Dict] = {}
    for row in choice_rows:
        user = row.user
        if not user:
            continue
        responded_map[user.id] = {
            "user_id": user.id,
            "student_name": user.fullname or user.username,
            "username": user.username,
            "response_type": "choice",
            "response_content": row.option.option_text if row.option else "",
            "submitted_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
        }

    for row in text_rows:
        user = text_users.get(row.user_id)
        if not user:
            continue
        responded_map[user.id] = {
            "user_id": user.id,
            "student_name": user.fullname or user.username,
            "username": user.username,
            "response_type": "text",
            "response_content": row.response_text,
            "submitted_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else "",
        }

    responded_students = sorted(
        responded_map.values(),
        key=lambda item: ((item.get("student_name") or "").lower(), item.get("user_id") or 0),
    )
    responded_ids = {item.get("user_id") for item in responded_students}

    pending_students = []
    for user in students:
        if user.id in responded_ids:
            continue
        pending_students.append({
            "user_id": user.id,
            "student_name": user.fullname or user.username,
            "username": user.username,
        })

    return {
        "survey": _serialize_survey(db, survey),
        "responded_students": responded_students,
        "pending_students": pending_students,
        "responded_count": len(responded_students),
        "pending_count": len(pending_students),
        "total_students": len(students),
    }


def get_surveys_for_class(db: Session, class_id: int, user_id: Optional[int] = None) -> List[Dict]:
    rows = (
        db.query(models.Survey)
          .options(joinedload(models.Survey.class_), joinedload(models.Survey.options))
          .filter(models.Survey.class_id == class_id)
          .filter(models.Survey.is_published.is_(True))
          .order_by(models.Survey.created_at.desc(), models.Survey.id.desc())
          .all()
    )
    return [_serialize_survey(db, survey, user_id=user_id) for survey in rows]


def delete_survey(db: Session, survey_id: int) -> Tuple[bool, str]:
    survey = db.get(models.Survey, survey_id)
    if not survey:
        return False, "Không tìm thấy khảo sát"
    db.delete(survey)
    db.commit()
    return True, "Đã xóa khảo sát"


def submit_survey_response(
    db: Session,
    survey_id: int,
    user_id: int,
    option_id: Optional[int] = None,
    response_text: Optional[str] = None,
) -> Dict:
    survey = (
        db.query(models.Survey)
          .options(joinedload(models.Survey.class_), joinedload(models.Survey.options))
          .filter(models.Survey.id == survey_id)
          .first()
    )
    if not survey or not survey.is_published:
        raise ValueError("Không tìm thấy khảo sát")

    user = db.get(models.User, user_id)
    if not user:
        raise ValueError("Không tìm thấy sinh viên")
    if user.class_id != survey.class_id:
        raise ValueError("Khảo sát này không dành cho lớp của bạn")

    text_value = (response_text or "").strip()
    if option_id is None and not text_value:
        raise ValueError("Vui lòng chọn hoặc nhập nội dung khảo sát")

    response = None
    text_row = None

    if text_value:
        text_row = (
            db.query(models.SurveyTextResponse)
              .filter(models.SurveyTextResponse.survey_id == survey_id)
              .filter(models.SurveyTextResponse.user_id == user_id)
              .first()
        )
        if text_row:
            text_row.response_text = text_value
        else:
            text_row = models.SurveyTextResponse(
                survey_id=survey_id,
                user_id=user_id,
                response_text=text_value,
            )
            db.add(text_row)

        existing_choice = (
            db.query(models.SurveyResponse)
              .filter(models.SurveyResponse.survey_id == survey_id)
              .filter(models.SurveyResponse.user_id == user_id)
              .first()
        )
        if existing_choice:
            db.delete(existing_choice)
    else:
        option = db.get(models.SurveyOption, option_id)
        if not option or option.survey_id != survey_id:
            raise ValueError("Lựa chọn khảo sát không hợp lệ")

        response = (
            db.query(models.SurveyResponse)
              .filter(models.SurveyResponse.survey_id == survey_id)
              .filter(models.SurveyResponse.user_id == user_id)
              .first()
        )
        if response:
            response.option_id = option_id
        else:
            response = models.SurveyResponse(
                survey_id=survey_id,
                option_id=option_id,
                user_id=user_id,
            )
            db.add(response)

        existing_text = (
            db.query(models.SurveyTextResponse)
              .filter(models.SurveyTextResponse.survey_id == survey_id)
              .filter(models.SurveyTextResponse.user_id == user_id)
              .first()
        )
        if existing_text:
            db.delete(existing_text)

    db.commit()
    if response is not None:
        db.refresh(response)
    if text_row is not None:
        db.refresh(text_row)
    return _serialize_survey(db, survey, user_id=user_id)