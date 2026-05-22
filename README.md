
# XÂY DỰNG HỆ THỐNG PHÂN TÍCH CẢM XÚC TRONG PHẢN HỒI SINH VIÊN ĐỂ NÂNG CAO CHẤT LƯỢNG ĐÀO TẠO

## 1. Tổng quan
Hệ thống này là một mạng xã hội nội bộ dành cho sinh viên, được thiết kế để vừa phục vụ giao tiếp (bài viết, bình luận, khảo sát), vừa phát hiện sớm các trạng thái cảm xúc tiêu cực và nội dung có nguy cơ. Các chức năng chính:

- Đăng ký / Đăng nhập: xác thực người dùng, phân quyền `student`/`admin`.
- Đăng bài, like, comment, báo cáo: hỗ trợ nội dung text (và file media), cho phép báo cáo bài không phù hợp.
- Quản lý người dùng & Admin Dashboard: admin có thể quản lý users, lớp/môn, tin tức, khảo sát, và xem dashboard phân tích.
- Phân tích cảm xúc & cảnh báo: mọi feedback/post được gửi sẽ được phân tích sentiment (model local); nếu phát hiện tiêu cực/đáng lo ngại, hệ thống tạo alert và kích hoạt luồng hỗ trợ.
- Thông báo realtime: push notification qua WebSocket tới người dùng và admin.
- Chat AI tiếng Việt & RAG hỗ trợ: chatbot nội bộ dùng model local hoặc RAG (retrieval + LLM) để trả lời/suggest hướng hỗ trợ; có thể kết hợp retriever từ vectorstore.
- Admin review & phân tích nâng cao: giao diện admin cho phép duyệt các alert, xem phân tích toxic/vision/RAG, và đưa ra quyết định can thiệp.


# Student Feedback System — README

Tài liệu ngắn gọn, chính xác theo mã nguồn hiện có trong workspace `student_feedback_system`.

## 1. Mục tiêu

Hệ thống cho phép sinh viên nộp phản hồi (feedback) về môn học/giảng viên, lưu trữ và phân tích cảm xúc; cung cấp cơ chế cảnh báo và hỗ trợ thông qua chatbot/RAG khi phát hiện nội dung tiêu cực.

## 2. Thư mục & file chính

- `app/`:
	- `main.py` — entrypoint FastAPI, khai báo các route và background tasks
	- `models.py` — định nghĩa SQLAlchemy models
	- `database.py` — tạo engine, `SessionLocal`, đọc `DB_URL` từ env
	- `crud.py` — hàm CRUD dùng bởi route
	- `sentiment_model.py` — tải model local và hàm phân tích sentiment
	- `negative_support_rag_chatbot.py` — RAG chatbot cho hỗ trợ khi phát hiện tiêu cực
	- `negative_support_vector_rag_chatbot.py` — xử lý vectorstore, upsert/retrieve embeddings
	- `admin_rag_chatbot.py` — các helper admin cho RAG

- `frontend/`:
	- `login.html`, `student_feedback.html`, `admin_dashboard.html` — trang tĩnh giao diện

- `phobert_student_feedback_sentiment/` — tokenizer và model files dùng cho phân tích sentiment (`vocab.txt`, `model.safetensors`, ...)
- `inspect_models.py` — script tiện ích để kiểm tra model/tokenizer
- `requirements.txt` — danh sách package Python
- `diagrams/` — (nếu có) sơ đồ ERD hoặc flowchart Mermaid
- `uploads/` — nơi chứa tệp tải lên (thư mục và ví dụ subfolders như `news/`)

Xem nhanh source chính: `app/main.py`, `app/models.py`, `app/sentiment_model.py`, `app/negative_support_vector_rag_chatbot.py`.

## 3. Luồng hoạt động chính (theo mã nguồn)

1) Sinh viên gửi feedback
	 - Frontend gửi `POST /feedback` tới backend.
	 - Backend gọi `sentiment_model.predict_sentiment()` (nội bộ) để phân loại `POS`/`NEG`.
	 - Lưu feedback vào bảng `feedbacks` (trong `app/models.py`).
	 - Nếu feedback có nhãn `NEG`, background task sẽ tạo embedding (sentence-transformers) và gọi `negative_support_vector_rag_chatbot.upsert_feedback_embedding()` để lưu vào vectorstore (Postgres+pgvector nếu cấu hình hoặc local fallback).
	 - Có thể khởi tạo RAG-retrieval để trả lời hỗ trợ tự động (xem `negative_support_rag_chatbot.py`).

2) Chatbot endpoint
	 - `POST /api/chat` (hoặc route tương đương trong `app/main.py`) nhận câu hỏi, lưu `chat_messages`, gọi module RAG/LLM để sinh phản hồi.

3) Admin
	 - Các route quản trị đọc từ `crud.py`/`models.py` để quản lý users, subjects, news, surveys, alerts.

## 4. Cách chạy local (chi tiết)

1) Tạo virtualenv và cài dependency:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

2) Tạo file `.env` ở thư mục gốc với tối thiểu:

- `DB_URL=sqlite:///./feedback_system.db`  (phát triển nhanh)
- `SENTIMENT_MODEL_REF=phobert_student_feedback_sentiment`
- (tuỳ chọn) `GROQ_API_KEY` nếu dùng Groq dịch vụ RAG/toxic

3) Khởi chạy backend:

```powershell
uvicorn app.main:app --reload --port 8000
```

4) Mở các trang tĩnh trong `frontend/` hoặc tích hợp frontend dev server nếu bạn có cấu trúc React.

## 5. Database & Vectorstore

- Dev: sử dụng SQLite (file local) theo `DB_URL` mặc định.
- Production: khuyến nghị PostgreSQL + `pgvector` để lưu embedding và truy vấn vector hiệu quả.
	- Thiết lập extension trong Postgres (quyền superuser):

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

	- Cập nhật `.env`: `DB_URL=postgresql+psycopg2://user:pass@host:5432/dbname`
	- `app/negative_support_vector_rag_chatbot.py` đã có logic để dùng Postgres khi `DB_URL` trỏ tới Postgres.

## 6. Models & files lớn

- `phobert_student_feedback_sentiment/` chứa tokenizer và model; tránh commit các file weights lớn (`.safetensors`) lên Git nếu không dùng Git LFS.
- `inspect_models.py` là script hỗ trợ kiểm tra model/tokenizer hiện có.

## 7. Diagrams

- Nếu cần ERD/flow, xem `diagrams/` trong repo (nếu tồn tại). Nếu muốn mình xuất PNG từ Mermaid, mình có thể làm (cần `mmdc` hoặc Docker).

## 8. Ghi chú vận hành

- Không commit `.env` hay API keys.
- Nếu bạn muốn dùng GitHub Actions / CI để deploy, mình có thể tạo workflow mẫu.

## 9. Tiếp theo?

- Mình đã chỉnh `README.md` để sát với `Student_feedback_system` trong repo. Muốn mình commit và push thay đổi này lên `origin/main` bây giờ không? (Bạn sẽ cần xác thực nếu Git yêu cầu PAT).

---

File này được viết lại trực tiếp theo mã nguồn của bạn, không sao chép từ nguồn khác.
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

### Thêm: các đối tượng DB và ràng buộc đặc biệt
- `negative_support_feedback_vectors` (Postgres + pgvector): bảng bổ sung được tạo khi dùng vector RAG. Chứa `feedback_id` (PK, FK → `feedbacks.id`), `user_id`, `subject_id`, `feedback_text`, `embedding` (vector) và `created_at`.
- `news_likes` có ràng buộc duy nhất `(post_id, anonymous_id)` để tránh like trùng lặp từ cùng anonymous id.
- `survey_responses` và `survey_text_responses` có `UNIQUE(survey_id, user_id)` để một user chỉ vote/submit 1 lần cho khảo sát.
- Một số index được thêm cho performance (ví dụ index theo `(conversation_id, created_at)` trên `chat_messages`, index cho `alerts.user_id`, và index/vindex cho `negative_support_feedback_vectors.embedding`).

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

## Luồng hoạt động (Flow)

Sơ đồ luồng hoạt động đơn giản dưới đây mô tả dòng chính: nộp phản hồi → phân tích sentiment → xử lý RAG cho phản hồi tiêu cực → cảnh báo cho admin.

```mermaid
%% See diagrams/system_flow.mmd for source
flowchart TD
	S[Student] -->|submit feedback| F["POST /feedback"]
	F --> M["Sentiment model (PhoBERT)"]
	M -->|POS| Store["Store feedback"]
	Store --> Ack["Send acknowledgement"]
	M -->|NEG| StoreNeg["Store feedback"]
	StoreNeg --> BG["Background task"]
	BG --> Emb["Generate embedding<br/>(sentence-transformers)"]
	Emb --> Upsert["Upsert embedding to vector store<br/>(Postgres + pgvector)"]
	Upsert --> RAG["Negative-support RAG<br/>(retrieval + Groq)"]
	RAG --> Reply["Reply to student (support message)"]
	RAG -->|escalated| Alert["Create Alert"]
	Alert --> Admin["Admin dashboard / Email alert"]

	Chat["Student chat"] --> ChatAPI["POST /api/chat"]
	ChatAPI --> ChatModel["Sentiment check & save"]
	ChatModel -->|risk| Alert

	Admin --> Manage["Manage users / surveys / news"]
```

Hoặc xem file sơ đồ luồng tại `diagrams/system_flow.mmd`.


