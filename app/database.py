from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from .config import settings

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)

# Enable WAL mode for better concurrent reads
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401 – registers all models
    Base.metadata.create_all(bind=engine)

    # Migrate: add columns introduced after initial schema
    with engine.connect() as conn:
        for col_def in (
            "ALTER TABLE projects ADD COLUMN video_type VARCHAR(50) DEFAULT 'top10'",
            "ALTER TABLE projects ADD COLUMN duration VARCHAR(20) DEFAULT '6-8'",
            "ALTER TABLE projects ADD COLUMN outline TEXT",
            "ALTER TABLE projects ADD COLUMN reference_transcripts TEXT",
            "ALTER TABLE projects ADD COLUMN script_approved BOOLEAN DEFAULT 0",
            "ALTER TABLE projects ADD COLUMN script_final TEXT",
            "ALTER TABLE projects ADD COLUMN target_chunk_size INTEGER DEFAULT 1500",
            "ALTER TABLE projects ADD COLUMN tts_provider VARCHAR(50)",
            "ALTER TABLE projects ADD COLUMN tts_api_key TEXT",
            "ALTER TABLE projects ADD COLUMN tts_voice_id VARCHAR(255)",
            "ALTER TABLE projects ADD COLUMN tts_config TEXT",
            "ALTER TABLE projects ADD COLUMN voiceover_path VARCHAR(512)",
        ):
            try:
                conn.execute(__import__("sqlalchemy").text(col_def))
                conn.commit()
            except Exception:
                pass  # column already exists

        # Migrate: Chunk tables
        for col_def in (
            "ALTER TABLE chunks ADD COLUMN motion_prompt TEXT",
        ):
            try:
                conn.execute(__import__("sqlalchemy").text(col_def))
                conn.commit()
            except Exception:
                pass  # column already exists

        # Migrate: ensure settings table exists (created by Base.metadata.create_all above,
        # but add explicit guard for existing DBs that may not have run create_all again)
        try:
            conn.execute(__import__("sqlalchemy").text(
                "CREATE TABLE IF NOT EXISTS settings (key VARCHAR(100) PRIMARY KEY, value TEXT)"
            ))
            conn.commit()
        except Exception:
            pass
