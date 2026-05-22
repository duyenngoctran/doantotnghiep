# app/models.py
from datetime import datetime, timezone, timedelta
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey,
    Boolean, Text, func, Index, UniqueConstraint
)
from sqlalchemy.orm import relationship
from .database import Base

# Múi giờ Việt Nam (UTC+7)
VIETNAM_TZ = timezone(timedelta(hours=7))

def vietnam_now():
    """Trả về thời gian hiện tại theo múi giờ Việt Nam"""
    return datetime.now(VIETNAM_TZ).replace(tzinfo=None)

# ======================= LỚP HỌC =======================
class Class(Base):
    __tablename__ = "classes"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)

    students = relationship("User", back_populates="class_")
    # Relationship với môn học được định nghĩa ở cuối (many-to-many)
    subjects = relationship(
        "Subject",
        secondary="subject_classes",
        back_populates="classes"
    )
    homeroom_teachers = relationship(
        "Teacher",
        secondary="teacher_classes",
        back_populates="homeroom_classes",
    )
    surveys = relationship("Survey", back_populates="class_")


# ======================= NGƯỜI DÙNG =======================
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    fullname = Column(String, nullable=True)            # có thể rỗng
    role = Column(String, default="student")            # 'admin' | 'student'
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=True)
    date_of_birth = Column(String, nullable=True)       # YYYY-MM-DD format

    class_ = relationship("Class", back_populates="students")
    feedbacks = relationship("Feedback", back_populates="user")

    # liên hệ với nhật ký chat & cảnh báo (không bắt buộc có user_id)
    chat_messages = relationship("ChatMessage", back_populates="user", cascade="all,delete-orphan", passive_deletes=True)
    alerts = relationship("Alert", back_populates="user", cascade="all,delete-orphan", passive_deletes=True)


# ======================= KÌ HỌC =======================
class Semester(Base):
    __tablename__ = "semesters"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)  # VD: "Kì 1", "Kì 2", "2024-2025 Kì 1"
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    
    subjects = relationship("Subject", back_populates="semester")


# ======================= BẢNG TRUNG GIAN: MÔN HỌC - LỚP =======================
class SubjectClass(Base):
    __tablename__ = "subject_classes"
    subject_id = Column(Integer, ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True)
    class_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), primary_key=True)


# ======================= BẢNG TRUNG GIAN: GIẢNG VIÊN - MÔN HỌC =======================
class TeacherSubject(Base):
    __tablename__ = "teacher_subjects"
    teacher_id = Column(Integer, ForeignKey("teachers.id", ondelete="CASCADE"), primary_key=True)
    subject_id = Column(Integer, ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True)


# ======================= BẢNG TRUNG GIAN: GIẢNG VIÊN CHỦ NHIỆM - LỚP =======================
class TeacherClass(Base):
    __tablename__ = "teacher_classes"
    teacher_id = Column(Integer, ForeignKey("teachers.id", ondelete="CASCADE"), primary_key=True)
    class_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), primary_key=True)


# ======================= MÔN HỌC =======================
class Subject(Base):
    __tablename__ = "subjects"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    teacher = Column(String, nullable=True)
    semester_id = Column(Integer, ForeignKey("semesters.id"), nullable=True)
    class_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), nullable=True)

    # Relationship với lớp học (many-to-many)
    classes = relationship(
        "Class",
        secondary="subject_classes",
        back_populates="subjects",
    )
    semester = relationship("Semester", back_populates="subjects")
    feedbacks = relationship("Feedback", back_populates="subject")
    teachers = relationship(
        "Teacher",
        secondary="teacher_subjects",
        back_populates="subjects",
    )


# ======================= GIẢNG VIÊN =======================
class Teacher(Base):
    __tablename__ = "teachers"
    id = Column(Integer, primary_key=True, index=True)
    teacher_code = Column(String, unique=True, nullable=False, index=True)
    full_name = Column(String, nullable=False)
    email = Column(String, nullable=True)
    role = Column(String, nullable=False, default="subject_teacher")

    subjects = relationship(
        "Subject",
        secondary="teacher_subjects",
        back_populates="teachers",
    )
    homeroom_classes = relationship(
        "Class",
        secondary="teacher_classes",
        back_populates="homeroom_teachers",
    )


# ======================= PHẢN HỒI =======================
class Feedback(Base):
    __tablename__ = "feedbacks"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    text = Column(String, nullable=False)
    label = Column(String, nullable=False)              # 'POS' | 'NEG' | ...
    prob_neg = Column(Float)
    prob_pos = Column(Float)
    created_at = Column(DateTime, default=vietnam_now)

    user = relationship("User", back_populates="feedbacks")
    subject = relationship("Subject", back_populates="feedbacks")


# ======================= NHẬT KÝ CHATBOT =======================
class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)

    # bạn có thể lưu theo user_id hoặc chỉ theo student_id/conversation_id
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    student_id = Column(String(64), index=True, nullable=True)      # tuỳ hệ thống của bạn
    conversation_id = Column(String(64), index=True, nullable=True) # gom các lượt chat

    message_role = Column(String(16), nullable=False)   # "user" | "assistant"
    message_text = Column(Text, nullable=False)
    sentiment = Column(String(8), nullable=True)        # "pos" | "neg" | "neu"
    escalated = Column(Boolean, default=False)          # có gắn cờ rủi ro không
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="chat_messages")

# các index hữu ích cho truy vấn
Index("ix_chat_conv_time", ChatMessage.conversation_id, ChatMessage.created_at)
Index("ix_chat_user_time", ChatMessage.user_id, ChatMessage.created_at)


# ======================= CẢNH BÁO RỦI RO =======================
class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    student_id = Column(String(64), index=True, nullable=True)
    conversation_id = Column(String(64), index=True, nullable=True)

    trigger_text = Column(Text, nullable=False)
    risk_level = Column(String(16), default="high")     # "low" | "medium" | "high"
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="alerts")

Index("ix_alert_conv_time", Alert.conversation_id, Alert.created_at)
Index("ix_alert_user_time", Alert.user_id, Alert.created_at)


# ======================= TIN TỨC NHÀ TRƯỜNG =======================
class NewsPost(Base):
    __tablename__ = "news_posts"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    media_url = Column(Text, nullable=True)
    media_type = Column(String(16), nullable=True)  # image | video
    is_published = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=vietnam_now)
    updated_at = Column(DateTime, default=vietnam_now, onupdate=vietnam_now)

    likes = relationship(
        "NewsLike",
        back_populates="post",
        cascade="all,delete-orphan",
        passive_deletes=True,
    )
    comments = relationship(
        "NewsComment",
        back_populates="post",
        cascade="all,delete-orphan",
        passive_deletes=True,
        order_by="desc(NewsComment.created_at), desc(NewsComment.id)",
    )


# ======================= TIN TỨC: LIKE ẨN DANH =======================
class NewsLike(Base):
    __tablename__ = "news_likes"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("news_posts.id", ondelete="CASCADE"), nullable=False, index=True)
    anonymous_id = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime, default=vietnam_now)

    post = relationship("NewsPost", back_populates="likes")

    __table_args__ = (
        UniqueConstraint("post_id", "anonymous_id", name="uq_news_likes_post_anon"),
    )


# ======================= TIN TỨC: BÌNH LUẬN ẨN DANH =======================
class NewsComment(Base):
    __tablename__ = "news_comments"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("news_posts.id", ondelete="CASCADE"), nullable=False, index=True)
    anonymous_id = Column(String(64), nullable=False, index=True)
    content = Column(Text, nullable=False)
    sentiment_label = Column(String(8), nullable=True)
    prob_neg = Column(Float, nullable=True)
    prob_pos = Column(Float, nullable=True)
    prob_neu = Column(Float, nullable=True)
    created_at = Column(DateTime, default=vietnam_now)

    post = relationship("NewsPost", back_populates="comments")


# ======================= KHẢO SÁT =======================
class Survey(Base):
    __tablename__ = "surveys"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    class_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), nullable=False, index=True)
    is_published = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=vietnam_now)
    updated_at = Column(DateTime, default=vietnam_now, onupdate=vietnam_now)

    class_ = relationship("Class", back_populates="surveys")
    options = relationship(
        "SurveyOption",
        back_populates="survey",
        cascade="all,delete-orphan",
        passive_deletes=True,
        order_by="SurveyOption.sort_order.asc(), SurveyOption.id.asc()",
    )
    responses = relationship(
        "SurveyResponse",
        back_populates="survey",
        cascade="all,delete-orphan",
        passive_deletes=True,
    )


class SurveyOption(Base):
    __tablename__ = "survey_options"

    id = Column(Integer, primary_key=True, index=True)
    survey_id = Column(Integer, ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False, index=True)
    option_text = Column(String(255), nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)

    survey = relationship("Survey", back_populates="options")
    responses = relationship("SurveyResponse", back_populates="option")


class SurveyResponse(Base):
    __tablename__ = "survey_responses"

    id = Column(Integer, primary_key=True, index=True)
    survey_id = Column(Integer, ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False, index=True)
    option_id = Column(Integer, ForeignKey("survey_options.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=vietnam_now)

    survey = relationship("Survey", back_populates="responses")
    option = relationship("SurveyOption", back_populates="responses")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("survey_id", "user_id", name="uq_survey_responses_survey_user"),
    )


class SurveyTextResponse(Base):
    __tablename__ = "survey_text_responses"

    id = Column(Integer, primary_key=True, index=True)
    survey_id = Column(Integer, ForeignKey("surveys.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    response_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=vietnam_now)
    updated_at = Column(DateTime, default=vietnam_now, onupdate=vietnam_now)

    __table_args__ = (
        UniqueConstraint("survey_id", "user_id", name="uq_survey_text_responses_survey_user"),
    )
