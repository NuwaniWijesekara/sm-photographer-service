from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from sqlalchemy.orm import Session
from datetime import datetime
import uuid, json
import httpx
from jose import JWTError, jwt
from ..config.settings import settings
from ..schemas.schemas import EventCreate, EventUpdate, EventResponse

router = APIRouter(prefix="/events", tags=["Event Management"])
bearer = HTTPBearer()

# Applied whenever a photographer has no active subscription (never
# subscribed, or their subscription lapsed) — same cap as the seeded "Free"
# package in sm-subscription-service.
FREE_TIER_MAX_EVENTS = 3

# Short timeout: this check sits in the POST /events critical path, so a slow
# or unreachable subscription-service should fail fast into the fallback
# below rather than hang the request.
_SUBSCRIPTION_LOOKUP_TIMEOUT = httpx.Timeout(5.0, connect=3.0)

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

import logging
logger = logging.getLogger(__name__)

def _publish_ingest(redis_client, event_id: str, drive_url: str):
    """Publish photo.ingest event to Redis Stream."""
    try:
        redis_client.xadd("photo.ingest", {"event_id": event_id, "drive_url": drive_url})
    except Exception as e:
        logger.error(f"Failed to publish ingest to Redis Stream: {e}")

async def _get_max_events_limit(user_id: str, authorization: str) -> Optional[int]:
    """
    Look up the caller's `max_events` cap from their active subscription's
    package, via the API Gateway (never the subscription-service directly —
    it's not on this service's network in every deployment, the gateway is).

    Returns None for "unlimited". Falls back to FREE_TIER_MAX_EVENTS whenever
    we can't get a confident answer — no active subscription (404), a
    malformed/unexpected response, or the subscription-service being slow or
    down — so a downstream hiccup degrades to the safe default instead of
    blocking every photographer from creating events.
    """
    url = f"{settings.api_gateway_url}/api/v1/subscriptions/{user_id}"
    try:
        async with httpx.AsyncClient(timeout=_SUBSCRIPTION_LOOKUP_TIMEOUT) as client:
            response = await client.get(url, headers={"Authorization": authorization})
    except httpx.HTTPError as e:
        logger.warning(f"Subscription lookup failed for user {user_id}: {e}")
        return FREE_TIER_MAX_EVENTS

    if response.status_code == 404:
        # No subscription on record at all — treat as Free Tier.
        return FREE_TIER_MAX_EVENTS

    if response.status_code != 200:
        logger.warning(
            f"Subscription lookup for user {user_id} returned {response.status_code}: {response.text[:200]}"
        )
        return FREE_TIER_MAX_EVENTS

    try:
        subscriptions = response.json()
    except ValueError:
        logger.warning(f"Subscription lookup for user {user_id} returned non-JSON body")
        return FREE_TIER_MAX_EVENTS

    active_subscription = next(
        (s for s in subscriptions if isinstance(s, dict) and s.get("status") == "active"),
        None,
    )
    if not active_subscription:
        return FREE_TIER_MAX_EVENTS

    package = active_subscription.get("package") or {}
    limits = package.get("limits") or {}
    photographer_limits = limits.get("photographer_limits") or {}
    # .get(..., default) only falls back when the key is *missing* — an
    # explicit `null` (unlimited) is returned as None, exactly as intended.
    return photographer_limits.get("max_events", FREE_TIER_MAX_EVENTS)

from ..services.s3 import s3_service

def _event_to_response(e) -> EventResponse:
    cover_url = s3_service.generate_presigned_url(e.cover_photo_url, expiration=3600) if e.cover_photo_url else None
    return EventResponse(
        id=e.id, name=e.name, date=e.date, drive_url=e.drive_url,
        cover_photo_url=cover_url, qr_token=e.qr_token, username=e.username,
        status=e.status.value, total_photos=e.total_photos, created_at=e.created_at
    )

@router.get("/check-username")
def check_username(
    username: str,
    exclude_event_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id)
):
    from ..main import Event
    clean_username = username.strip().lower().lstrip('@')
    if not clean_username:
        return {"available": True, "message": ""}
    
    query = db.query(Event).filter(Event.username == clean_username)
    if exclude_event_id:
        query = query.filter(Event.id != exclude_event_id)
    
    existing = query.first()
    if existing:
        return {"available": False, "message": "Username is already taken"}
    return {"available": True, "message": "Username is available!"}

@router.post("/", response_model=EventResponse)
async def create_event(
    event_data: EventCreate,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    credentials: HTTPAuthorizationCredentials = Depends(bearer),
):
    from ..main import Event, EventStatus, redis_client
    clean_username = event_data.username.strip().lower().lstrip('@') if event_data.username else None
    if not clean_username:
        raise HTTPException(status_code=400, detail="Collection username is required.")

    # Subscription-service authenticates this same JWT and requires the
    # user_id in the URL to match its own `sub` claim, so we forward the
    # caller's own token rather than minting a new one.
    max_events = await _get_max_events_limit(
        user_id, authorization=f"{credentials.scheme} {credentials.credentials}"
    )
    if max_events is not None:
        current_event_count = db.query(Event).filter(Event.photographer_id == user_id).count()
        if current_event_count >= max_events:
            raise HTTPException(
                status_code=403,
                detail="Event limit reached. Please upgrade your package to create more events.",
            )

    existing = db.query(Event).filter(Event.username == clean_username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Collection username already taken. Please choose another.")

    qr_token = f"{event_data.name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}"
    event = Event(
        name=event_data.name, date=datetime.now(),
        drive_url=event_data.drive_url, qr_token=qr_token, username=clean_username,
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

    clean_username = event_data.username.strip().lower().lstrip('@') if event_data.username else None
    if not clean_username:
        raise HTTPException(status_code=400, detail="Collection username is required.")

    if clean_username != event.username:
        existing = db.query(Event).filter(Event.username == clean_username).first()
        if existing:
            raise HTTPException(status_code=400, detail="Collection username already taken. Please choose another.")

    drive_url_changed = event.drive_url != event_data.drive_url

    event.name = event_data.name
    event.username = clean_username
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