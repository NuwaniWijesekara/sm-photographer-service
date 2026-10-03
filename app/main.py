import socket
# Force IPv4 to prevent connection timeouts on systems with broken IPv6 routing
orig_getaddrinfo = socket.getaddrinfo
def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = patched_getaddrinfo

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import redis as redis_lib
from .config.settings import settings

# ── DB setup ──────────────────────────────────────────────────
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, String, DateTime, Enum as SAEnum, ForeignKey, Integer, JSON, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
import uuid, enum
from datetime import datetime

Base = declarative_base()

def _uuid(): return str(uuid.uuid4())

class EventStatus(str, enum.Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    READY      = "ready"
    FAILED     = "failed"

class EventAccessMode(str, enum.Enum):
    """Who may open an event's guest gallery (enforced in sm-guest-service)."""
    PUBLIC      = "public"       # anyone with the link / QR / username
    INVITE_ONLY = "invite_only"  # owner + collaborators, Google-verified email

class CollaboratorPermission(str, enum.Enum):
    VIEW_ONLY  = "VIEW_ONLY"
    CAN_UPLOAD = "CAN_UPLOAD"

class User(Base):
    """The single, unified account table — every user (event creator or
    collaborator) is a row here. `email`/`password_hash` are nullable to
    accommodate anonymous instant-access sessions, which get a row with
    neither set."""
    __tablename__ = "users"
    id             = Column(String, primary_key=True, default=_uuid)
    name           = Column(String, nullable=True)
    email          = Column(String, unique=True, index=True, nullable=True)
    password_hash  = Column(String, nullable=True)
    is_anonymous   = Column(Boolean, default=False, nullable=False)
    created_at     = Column(DateTime, default=datetime.utcnow)
    events         = relationship("Event", back_populates="owner", cascade="all, delete-orphan")

class Event(Base):
    __tablename__ = "events"
    id              = Column(String, primary_key=True, default=_uuid)
    name            = Column(String, nullable=False)
    date            = Column(DateTime, nullable=False)
    drive_url       = Column(String, nullable=True)
    cover_photo_url = Column(String, nullable=True)
    qr_token        = Column(String, unique=True, nullable=False, index=True)
    username        = Column(String, unique=True, nullable=True, index=True)
    status          = Column(SAEnum(EventStatus), default=EventStatus.PENDING, nullable=False)
    owner_id        = Column(String, ForeignKey("users.id"), nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    total_photos    = Column(Integer, default=0)
    failed_files    = Column(JSON, nullable=True)
    # Snapshot of the owner's package `has_watermark` at creation time — the
    # ingestion worker reads this to decide whether display copies get a
    # bottom-right watermark.
    is_watermarked  = Column(Boolean, default=False, nullable=False, server_default="false")
    # Package's watermark logo (S3 URL) snapshotted alongside is_watermarked;
    # null means the worker's default logo / text watermark.
    watermark_logo_url = Column(String, nullable=True)
    # Stored as a plain string (not a PG enum) so sm-guest-service can mirror
    # it without sharing a type. New events default to invite-only; the
    # server default only backfills rows that existed before this column.
    access_mode     = Column(String, nullable=False, default=EventAccessMode.INVITE_ONLY.value, server_default=EventAccessMode.PUBLIC.value)
    owner           = relationship("User", back_populates="events")
    images          = relationship("Image", back_populates="event", cascade="all, delete-orphan")
    collaborators   = relationship("EventCollaborator", back_populates="event", cascade="all, delete-orphan")

class EventCollaborator(Base):
    """Shared access to an event — the owner (see Event.owner_id) can add
    other users here with a permission level instead of ownership."""
    __tablename__ = "event_collaborators"
    id         = Column(String, primary_key=True, default=_uuid)
    event_id   = Column(String, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id    = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    permission = Column(SAEnum(CollaboratorPermission), nullable=False, default=CollaboratorPermission.VIEW_ONLY)
    created_at = Column(DateTime, default=datetime.utcnow)
    event      = relationship("Event", back_populates="collaborators")
    user       = relationship("User")

    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_event_collaborator"),)

class Image(Base):
    __tablename__ = "images"
    id             = Column(String, primary_key=True, default=_uuid)
    event_id       = Column(String, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    s3_url         = Column(String, nullable=False)
    thumbnail_url  = Column(String, nullable=True)
    # Display version: watermarked when the event is_watermarked, otherwise a
    # plain copy of the original. s3_url stays the untouched original that
    # AWS Rekognition indexes.
    enhanced_url   = Column(String, nullable=True)
    filename       = Column(String, nullable=False)
    created_at     = Column(DateTime, default=datetime.utcnow)
    event          = relationship("Event", back_populates="images")
    faces          = relationship("Face", back_populates="image", cascade="all, delete-orphan")

class Face(Base):
    """One row per detected face — stores AWS Rekognition FaceId."""
    __tablename__ = "faces"
    id                  = Column(String, primary_key=True, default=_uuid)
    image_id            = Column(String, ForeignKey("images.id", ondelete="CASCADE"), nullable=False, index=True)
    rekognition_face_id = Column(String, nullable=False, index=True)
    created_at          = Column(DateTime, default=datetime.utcnow)
    image               = relationship("Image", back_populates="faces")

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)

# ── App lifespan ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS username VARCHAR(255);"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_events_username ON events (username) WHERE username IS NOT NULL;"))
        conn.execute(text("ALTER TABLE faces ADD COLUMN IF NOT EXISTS rekognition_face_id VARCHAR(255);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_faces_rekognition_face_id ON faces (rekognition_face_id);"))
        conn.execute(text("ALTER TABLE faces DROP COLUMN IF EXISTS embedding;"))

        # ── Unified User Model migration (Photographers/Guests merge) ──
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(255);"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_anonymous BOOLEAN NOT NULL DEFAULT FALSE;"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='hashed_password')
                THEN ALTER TABLE users RENAME COLUMN hashed_password TO password_hash; END IF;
            END $$;
        """))
        conn.execute(text("ALTER TABLE users ALTER COLUMN email DROP NOT NULL;"))
        conn.execute(text("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;"))
        conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='events' AND column_name='photographer_id')
                THEN ALTER TABLE events RENAME COLUMN photographer_id TO owner_id; END IF;
            END $$;
        """))

        # ── Account-level reference face feature removed — back to strict
        # per-event selfie upload. Drops the column for anyone who already
        # ran the migration that added it.
        conn.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS reference_face_url;"))

        # ── Free-tier watermarking ──
        conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS is_watermarked BOOLEAN NOT NULL DEFAULT FALSE;"))
        conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS enhanced_url VARCHAR;"))
        # Same as scripts/add_event_watermark_logo_url_column.py.
        conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS watermark_logo_url VARCHAR;"))

        # ── Guest gallery access control ──
        # Existing events stay public so links/QR codes already handed out
        # keep working; new events default to invite-only (see Event model).
        conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS access_mode VARCHAR NOT NULL DEFAULT 'public';"))

        # ── ADMIN collaborator role removed — only owners manage access now,
        # so VIEW_ONLY/CAN_UPLOAD is the whole story. Postgres has no
        # "ALTER TYPE ... DROP VALUE", so this rebuilds the enum type:
        # downgrade any existing ADMIN rows to CAN_UPLOAD first (the closest
        # remaining role to what ADMIN could still do — manage content),
        # then swap in a type that no longer has ADMIN as a valid value.
        conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_type t JOIN pg_enum e ON t.oid = e.enumtypid
                    WHERE t.typname = 'collaboratorpermission' AND e.enumlabel = 'ADMIN'
                ) THEN
                    UPDATE event_collaborators SET permission = 'CAN_UPLOAD' WHERE permission = 'ADMIN';
                    ALTER TYPE collaboratorpermission RENAME TO collaboratorpermission_old;
                    CREATE TYPE collaboratorpermission AS ENUM ('VIEW_ONLY', 'CAN_UPLOAD');
                    ALTER TABLE event_collaborators
                        ALTER COLUMN permission TYPE collaboratorpermission
                        USING permission::text::collaboratorpermission;
                    DROP TYPE collaboratorpermission_old;
                END IF;
            END $$;
        """))
        conn.commit()
    Base.metadata.create_all(bind=engine)
    print("✓ Photographer service running on :8001")
    yield

app = FastAPI(title="ScanMe — Photographer BFF", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from .api.auth import router as auth_router
from .api.events import router as events_router
from .api.collaborators import router as collaborators_router
app.include_router(auth_router)
app.include_router(events_router)
app.include_router(collaborators_router)

@app.get("/health")
def health():
    return {"service": "photographer", "status": "healthy"}