# Student Feedback System — README

Tài liệu này mô tả ngắn gọn công nghệ, luồng hoạt động và các bước chạy hệ thống `student_feedback_system`.

**Tóm tắt:** Hệ thống cho phép sinh viên gửi phản hồi về môn học/giảng viên, phân tích sentiment bằng mô hình PhoBERT, lưu trữ và cung cấp trợ giúp/gợi ý tự động (RAG) khi cần. Backend viết bằng `FastAPI`; ORM dùng `SQLAlchemy`. Mặc định sử dụng SQLite cho phát triển; khuyến nghị PostgreSQL + `pgvector` cho môi trường production khi cần lưu vector embeddings.

**Thư mục chính:**
- `app/` — backend (API, models, CRUD, chatbots, sentiment)
- `frontend/` — trang tĩnh (login, student feedback, admin)
- `phobert_student_feedback_sentiment/` — tokenizer + model local dùng để phân tích sentiment
- `uploads/` — nơi lưu file tải lên (đã thêm `.gitkeep`)

## Công nghệ chính
- Python 3.10+
- FastAPI — web API
- Uvicorn — ASGI server
- SQLAlchemy — ORM
- SQLite (dev) hoặc PostgreSQL (production)
- pgvector (Postgres) — lưu và truy vấn vector embeddings
- sentence-transformers — sinh embedding
- Transformers + PyTorch — mô hình PhoBERT local (`phobert_student_feedback_sentiment`)
- Groq API — LLM / RAG completions (dùng cho chatbot)
- BackgroundTasks (FastAPI) — xử lý bất đồng bộ (upsert embeddings, gửi email alert)

## Luồng hoạt động chính (tóm tắt)
1. Sinh viên gửi phản hồi qua `POST /feedback` (hoặc frontend):
	 - Backend gọi `sentiment_model.predict_sentiment()` để phân loại `POS`/`NEG` và trả xác suất.
	 - Lưu `Feedback` vào bảng `feedbacks` (liên kết `users`, `subjects`).
	 - Nếu phản hồi tiêu cực (`NEG`), background task sẽ sinh embedding (sentence-transformers) và upsert vào vectorstore (Postgres+pgvector) hoặc vào local index.
	 - Với feedback `NEG`, hệ thống gọi `negative_support_rag_chatbot` để gửi trả lời hỗ trợ và có thể tạo `Alert` (cảnh báo) cho admin/giáo viên.

2. Chatbot (endpoint `/api/chat`):
	 - Tiếp nhận message, tính sentiment, lưu `ChatMessage`.
	 - Nếu có dấu hiệu nguy cơ/tiêu cực, gắn `escalated=True` và tạo `Alert`.
	 - Sử dụng Groq API để sinh phản hồi (kết hợp retrieval từ vector store nếu cần).

3. Admin: có các endpoint quản lý người dùng, lớp, môn, tin tức, khảo sát, và xem `alerts` để can thiệp.

## Database (tóm tắt schema)
- Bảng chính: `users`, `classes`, `subjects`, `teachers`, `semesters`.
- Phản hồi: `feedbacks` liên kết `users` và `subjects` (chứa `label`, `prob_neg`, `prob_pos`, `created_at`).
- Chat logs: `chat_messages` (lưu `message_text`, `sentiment`, `conversation_id`, `created_at`).
- Alerts: `alerts` (trigger_text, risk_level, liên kết optional tới `user_id`).
- Tin tức: `news_posts`, `news_comments`, `news_likes`.
- Khảo sát: `surveys`, `survey_options`, `survey_responses`, `survey_text_responses`.

Xem sơ đồ ERD ngay dưới đây (Mermaid ER diagram). GitHub sẽ render trực tiếp block Mermaid khi hiển thị README.

```mermaid
erDiagram
	USERS {
		INTEGER id PK
		STRING username
		STRING fullname
		STRING role
		INTEGER class_id FK
	}

	CLASSES {
		INTEGER id PK
		STRING name
	}

	SUBJECTS {
		INTEGER id PK
		STRING name
		INTEGER semester_id FK
	}

	TEACHERS {
		INTEGER id PK
		STRING teacher_code
		STRING full_name
	}

	SEMESTERS {
		INTEGER id PK
		STRING name
	}

	FEEDBACKS {
		INTEGER id PK
		INTEGER user_id FK
		INTEGER subject_id FK
		STRING text
		STRING label
		FLOAT prob_neg
		FLOAT prob_pos
		DATETIME created_at
	}

	CHAT_MESSAGES {
		INTEGER id PK
		INTEGER user_id FK
		STRING conversation_id
		STRING message_role
		TEXT message_text
		STRING sentiment
		BOOLEAN escalated
		DATETIME created_at
	}

	ALERTS {
		INTEGER id PK
		INTEGER user_id FK
		STRING trigger_text
		STRING risk_level
		DATETIME created_at
	}

	NEWS_POSTS {
		INTEGER id PK
		STRING title
		TEXT content
		DATETIME created_at
	}

	NEWS_COMMENTS {
		INTEGER id PK
		INTEGER post_id FK
		STRING anonymous_id
		TEXT content
		STRING sentiment_label
	}

	SURVEYS {
		INTEGER id PK
		STRING title
		INTEGER class_id FK
	}

	SURVEY_OPTIONS {
		INTEGER id PK
		INTEGER survey_id FK
		STRING option_text
	}

	SURVEY_RESPONSES {
		INTEGER id PK
		INTEGER survey_id FK
		INTEGER option_id FK
		INTEGER user_id FK
	}

	USERS }|..|| CLASSES : "belongs_to"
	SUBJECTS }|..|| SEMESTERS : "in"
	FEEDBACKS }|..|| USERS : "author"
	FEEDBACKS }|..|| SUBJECTS : "on"
	CHAT_MESSAGES }|..|| USERS : "from"
	ALERTS }|..|| USERS : "for"
	NEWS_COMMENTS }|..|| NEWS_POSTS : "on"
	SURVEYS }|..|| CLASSES : "for"
	SURVEY_OPTIONS }|..|| SURVEYS : "option_of"
	SURVEY_RESPONSES }|..|| SURVEYS : "response_to"
	SURVEY_RESPONSES }|..|| SURVEY_OPTIONS : "chooses"
```

Hoặc xem file sơ đồ tại `diagrams/erd.mmd`.

## Cách chạy (local)
1. Tạo virtualenv và cài dependencies:
```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```
2. Tạo file `.env` (tham khảo `app/database.py`) và khởi động:
```powershell
uvicorn app.main:app --reload
```

## Cấu hình Database
- Mặc định `app/database.py` đọc `DB_URL` từ `.env`. Ví dụ dev: `DB_URL=sqlite:///./feedback_system.db`.
- Để chuyển sang PostgreSQL + pgvector (production):
	- Tạo DB Postgres và bật extension `pgvector` (quyền superuser cần thiết):
		- `CREATE EXTENSION IF NOT EXISTS vector;`
	- Cập nhật `.env`: `DB_URL=postgresql+psycopg2://user:pass@host:5432/dbname`
	- Khởi động lại dịch vụ, kiểm tra `app/negative_support_vector_rag_chatbot.py` để đảm bảo kết nối vectorstore.

## Chú ý khi đẩy lên GitHub
- Không commit file `.env` hay tệp chứa thông tin nhạy cảm.
- `phobert_student_feedback_sentiment/model.safetensors` rất lớn — nên bỏ qua trong `.gitignore` hoặc dùng Git LFS.

## Các endpoint quan trọng (tóm lược)
- `POST /feedback` — nộp phản hồi, trả về phân tích sentiment
- `POST /api/chat` — tương tác chatbot (lưu chat, trả lời từ Groq)
- Admin CRUD: `/admin/*` endpoints trong `app/main.py` và `app/crud.py` để quản lý users, classes, subjects, surveys, news, alerts.

## Tài nguyên thêm
- ERD: `diagrams/erd.mmd`
- Xem mã nguồn: `app/main.py`, `app/crud.py`, `app/models.py`, `app/sentiment_model.py`, `app/negative_support_vector_rag_chatbot.py`.


