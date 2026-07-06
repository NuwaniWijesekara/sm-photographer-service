from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from datetime import datetime
import uuid, json
from jose import JWTError, jwt
from ..config.settings import settings
from ..schemas.schemas import EventCreate, EventUpdate, EventResponse

router = APIRouter(prefix="/events", tags=["Event Management"])
bearer = HTTPBearer()

def get_db():
    from ..main import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user_id(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

def _publish_ingest(redis_client, event_id: str, drive_url: str):
    """Publish photo.ingest event to Redis Stream."""
    redis_client.xadd("photo.ingest", {"event_id": event_id, "drive_url": drive_url})

def _event_to_response(e) -> EventResponse:
    return EventResponse(
        id=e.id, name=e.name, date=e.date, drive_url=e.drive_url,
        cover_photo_url=e.cover_photo_url, qr_token=e.qr_token,
        status=e.status.value, total_photos=e.total_photos, created_at=e.created_at
    )

@router.post("/", response_model=EventResponse)
def create_event(
    event_data: EventCreate,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id)
):
    from ..main import Event, EventStatus, redis_client
    qr_token = f"{event_data.name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}"
    event = Event(
        name=event_data.name, date=datetime.now(),
        drive_url=event_data.drive_url, qr_token=qr_token,
        photographer_id=user_id, status=EventStatus.PENDING, total_photos=0
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    # Publish to Redis Stream (decoupled from ingestion worker)
    _publish_ingest(redis_client, event.id, event_data.drive_url)
    return _event_to_response(event)

@router.get("/", response_model=list[EventResponse])
def list_events(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    from ..main import Event
    events = db.query(Event).filter(Event.photographer_id == user_id).all()
    return [_event_to_response(e) for e in events]

@router.get("/{event_id}", response_model=EventResponse)
def get_event(event_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    from ..main import Event
    event = db.query(Event).filter(Event.id == event_id, Event.photographer_id == user_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_to_response(event)

@router.put("/{event_id}", response_model=EventResponse)
def update_event(
    event_id: str, event_data: EventUpdate,
    db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)
):
    from ..main import Event, EventStatus, Image, redis_client
    event = db.query(Event).filter(Event.id == event_id, Event.photographer_id == user_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    drive_url_changed = event.drive_url != event_data.drive_url

    event.name = event_data.name
    if drive_url_changed:
        event.drive_url = event_data.drive_url
        event.status = EventStatus.PENDING
        # Clear out existing images as we are ingesting a new folder
        db.query(Image).filter(Image.event_id == event_id).delete()

    db.commit()
    db.refresh(event)

    # Only re-trigger ingestion if the drive_url has actually changed
    if drive_url_changed and event.drive_url:
        _publish_ingest(redis_client, event.id, event.drive_url)
        
    return _event_to_response(event)


# photographer service api/events.py
@router.delete("/{event_id}")
def delete_event(event_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    from ..main import Event, Image, redis_client
    from ..services.s3 import s3_service
    import logging
    logger = logging.getLogger(__name__)

    event = db.query(Event).filter(Event.id == event_id, Event.photographer_id == user_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # 1. Grab URLs before the rows disappear
    images = db.query(Image).filter(Image.event_id == event_id).all()
    urls_to_delete = []
    for img in images:
        urls_to_delete.append(img.s3_url)
        if img.thumbnail_url:
            urls_to_delete.append(img.thumbnail_url)

    # 2. DB delete — Image/Face cascade automatically via FK ondelete="CASCADE"
    db.delete(event)
    db.commit()

    # 3. S3 cleanup — synchronous, batched, non-fatal on failure
    try:
        s3_service.delete_objects(urls_to_delete)
    except Exception as e:
        logger.error(f"S3 cleanup failed for event {event_id}: {e}")
        # Don't raise — DB delete already succeeded; a few orphaned S3
        # files cost pennies and are far better than a stuck/half-deleted event

    # 4. Notify guest service (separate DB, separate process → stream is correct here)
    redis_client.xadd("event.deleted", {"event_id": event_id})

    return {"status": "SUCCESS", "message": "Event deleted successfully"}