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
from sqlalchemy import Column, String, DateTime, Enum as SAEnum, ForeignKey, Integer, JSON
from pgvector.sqlalchemy import Vector
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

class User(Base):
    __tablename__ = "users"
    id              = Column(String, primary_key=True, default=_uuid)
    email           = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at      = Column(DateTime, default=datetime.utcnow)
    events          = relationship("Event", back_populates="owner", cascade="all, delete-orphan")

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
    photographer_id = Column(String, ForeignKey("users.id"), nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    total_photos    = Column(Integer, default=0)
    failed_files    = Column(JSON, nullable=True)
    owner           = relationship("User", back_populates="events")
    images          = relationship("Image", back_populates="event", cascade="all, delete-orphan")

class Image(Base):
    __tablename__ = "images"
    id             = Column(String, primary_key=True, default=_uuid)
    event_id       = Column(String, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    s3_url         = Column(String, nullable=False)
    thumbnail_url  = Column(String, nullable=True)
    filename       = Column(String, nullable=False)
    created_at     = Column(DateTime, default=datetime.utcnow)
    event          = relationship("Event", back_populates="images")
    faces          = relationship("Face", back_populates="image", cascade="all, delete-orphan")

class Face(Base):
    """One row per detected face — embedding only, indexable for ANN search."""
    __tablename__ = "faces"
    id             = Column(String, primary_key=True, default=_uuid)
    image_id       = Column(String, ForeignKey("images.id", ondelete="CASCADE"), nullable=False, index=True)
    embedding      = Column(Vector(512), nullable=False)
    created_at     = Column(DateTime, default=datetime.utcnow)
    image          = relationship("Image", back_populates="faces")

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)

# ── App lifespan ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("ALTER TABLE events ADD COLUMN IF NOT EXISTS username VARCHAR(255);"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_events_username ON events (username) WHERE username IS NOT NULL;"))
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
app.include_router(auth_router)
app.include_router(events_router)

@app.get("/health")
def health():
    return {"service": "photographer", "status": "healthy"}