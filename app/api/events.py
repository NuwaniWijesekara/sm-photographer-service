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

    subscription-service now always resolves to an active subscription for
    any authenticated user — a real one if they bought a package, otherwise a
    virtual subscription against whichever package is configured as the Free
    tier there (see GET /api/v1/subscriptions/{user_id}). So there is no
    hardcoded numeric fallback here anymore — the Free tier's limits are
    driven entirely by that package's `limits` in the database, and we just
    trust whatever comes back.

    Returns None for "unlimited". If the subscription-service can't be
    reached, times out, or returns something malformed, this fails open
    (returns None / unlimited) rather than blocking event creation over an
    unrelated infrastructure hiccup — this check is a soft business-rule
    limit, not a security boundary.
    """
    url = f"{settings.api_gateway_url}/api/v1/subscriptions/{user_id}"
    try:
        async with httpx.AsyncClient(timeout=_SUBSCRIPTION_LOOKUP_TIMEOUT) as client:
            response = await client.get(url, headers={"Authorization": authorization})
    except httpx.HTTPError as e:
        logger.warning(f"Subscription lookup failed for user {user_id}: {e}")
        return None

    if response.status_code != 200:
        logger.warning(
            f"Subscription lookup for user {user_id} returned {response.status_code}: {response.text[:200]}"
        )
        return None

    try:
        subscriptions = response.json()
    except ValueError:
        logger.warning(f"Subscription lookup for user {user_id} returned non-JSON body")
        return None

    active_subscription = next(
        (s for s in subscriptions if isinstance(s, dict) and s.get("status") == "active"),
        None,
    )
    if not active_subscription:
        # Shouldn't happen anymore — subscription-service always returns an
        # active (real or virtual Free) subscription — but if it somehow
        # doesn't, fail open rather than guess at a number.
        logger.warning(f"No active subscription (real or virtual) returned for user {user_id}")
        return None

    package = active_subscription.get("package") or {}
    limits = package.get("limits") or {}
    photographer_limits = limits.get("photographer_limits") or {}
    # Missing key or explicit `null` both mean "unlimited" — dict.get with no
    # default returns None for either.
    return photographer_limits.get("max_events")

from ..services.s3 import s3_service

def _event_to_response(e) -> EventResponse:
    cover_url = s3_service.generate_presigned_url(e.cover_photo_url, expiration=3600) if e.cover_photo_url else None
    return EventResponse(
        id=e.id, name=e.name, date=e.date, drive_url=e.drive_url,
        cover_photo_url=cover_url, qr_token=e.qr_token, username=e.username,
        status=e.status.value, total_photos=e.total_photos, created_at=e.created_at,
        owner_id=e.owner_id
    )

def _get_collaborator_permission(db: Session, event_id: str, user_id: str):
    """The caller's EventCollaborator.permission for this event, or None if
    they aren't a collaborator on it at all (owners aren't collaborator
    rows — check event.owner_id separately)."""
    from ..main import EventCollaborator
    link = (
        db.query(EventCollaborator)
        .filter(EventCollaborator.event_id == event_id, EventCollaborator.user_id == user_id)
        .first()
    )
    return link.permission if link else None

def _require_upload_access(db: Session, event, user_id: str) -> None:
    """Owner, or a collaborator with CAN_UPLOAD or ADMIN. Raises 403 otherwise.

    This is the closest thing this service has to an "upload photos"
    permission today: there's no direct photo-upload endpoint yet — photos
    are ingested by sm-ingestion-worker-service whenever this Drive URL
    changes (see update_event below), so that's the action CAN_UPLOAD gates.
    """
    from ..main import CollaboratorPermission
    if event.owner_id == user_id:
        return
    permission = _get_collaborator_permission(db, event.id, user_id)
    if permission in (CollaboratorPermission.CAN_UPLOAD, CollaboratorPermission.ADMIN):
        return
    raise HTTPException(status_code=403, detail="You don't have upload access to this event.")

def _require_admin_access(db: Session, event, user_id: str) -> None:
    """Owner, or a collaborator with ADMIN. Raises 403 otherwise — gates
    destructive actions (deleting the event and everything in it)."""
    from ..main import CollaboratorPermission
    if event.owner_id == user_id:
        return
    permission = _get_collaborator_permission(db, event.id, user_id)
    if permission == CollaboratorPermission.ADMIN:
        return
    raise HTTPException(status_code=403, detail="You don't have admin access to this event.")

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
        current_event_count = db.query(Event).filter(Event.owner_id == user_id).count()
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
        owner_id=user_id, status=EventStatus.PENDING, total_photos=0
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
    events = db.query(Event).filter(Event.owner_id == user_id).all()
    return [_event_to_response(e) for e in events]

@router.get("/{event_id}", response_model=EventResponse)
def get_event(event_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    from ..main import Event
    event = db.query(Event).filter(Event.id == event_id, Event.owner_id == user_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_to_response(event)

@router.put("/{event_id}", response_model=EventResponse)
def update_event(
    event_id: str, event_data: EventUpdate,
    db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)
):
    from ..main import Event, EventStatus, Image, redis_client
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_upload_access(db, event, user_id)

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

    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_admin_access(db, event, user_id)

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