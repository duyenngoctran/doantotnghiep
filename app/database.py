# app/database.py
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
import os

# Load biến môi trường từ file .env
load_dotenv()

# Lấy DB_URL từ .env
# - Production: PostgreSQL, ví dụ:
#   postgresql+psycopg2://feedback_user:password@localhost:5432/student_feedback
# - Nếu chưa set DB_URL -> fallback về SQLite local (cho dễ dev)
DATABASE_URL = os.getenv("DB_URL", "sqlite:///./feedback_system.db")

# Nếu là SQLite thì cần connect_args đặc biệt, PostgreSQL thì KHÔNG cần
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

# Khởi tạo engine
engine = create_engine(
    DATABASE_URL,
    **connect_args,
)

# Tạo SessionLocal dùng cho mọi chỗ trong app (Depends(get_db))
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# Base cho toàn bộ model (app.models.* sẽ kế thừa từ đây)
Base = declarative_base()


def _fix_postgres_sequences():
    """
    Fix PostgreSQL sequence mismatch for all tables.
    This prevents "duplicate key value violates unique constraint" errors
    when IDs get out of sync between sequences and actual data.
    """
    if not DATABASE_URL.startswith("postgresql"):
        return
    
    try:
        with engine.connect() as conn:
            # List of tables and their id sequences that need fixing
            tables_to_fix = [
                ("feedbacks", "feedbacks_id_seq"),
                ("users", "users_id_seq"),
                ("subjects", "subjects_id_seq"),
                ("classes", "classes_id_seq"),
                ("teachers", "teachers_id_seq"),
                ("chat_messages", "chat_messages_id_seq"),
                ("alerts", "alerts_id_seq"),
            ]
            
            for table_name, seq_name in tables_to_fix:
                try:
                    # Get the max id in the table
                    result = conn.execute(text(f"SELECT MAX(id) FROM {table_name}"))
                    max_id = result.scalar() or 0
                    
                    # Reset the sequence to max_id + 1
                    conn.execute(
                        text(f"SELECT setval('{seq_name}', {max_id + 1}, false)")
                    )
                except Exception:
                    # Table might not exist yet, which is fine during initial setup
                    pass
            
            conn.commit()
    except Exception as e:
        # Log but don't fail - might be running on SQLite or during migrations
        print(f"Note: Could not fix PostgreSQL sequences: {e}")


# Fix sequences when module is loaded (but only for PostgreSQL)
_fix_postgres_sequences()


# Hàm dependency cho FastAPI:
# Mỗi request sẽ lấy 1 session riêng, xong thì đóng lại.
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
