import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from ..config.settings import settings
from ..schemas.schemas import EventCreate, EventUpdate, EventResponse, OwnerGalleryResponse, OwnerPhoto
from ..services.s3 import s3_service
from ..utils.security import get_current_user_id, oauth2_scheme

router = APIRouter(prefix="/events", tags=["Event Management"])
logger = logging.getLogger(__name__)

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

def _publish_ingest(redis_client, event_id: str, drive_url: str):
    """Publish photo.ingest event to Redis Stream."""
    try:
        redis_client.xadd("photo.ingest", {"event_id": event_id, "drive_url": drive_url})
    except Exception as e:
        logger.error(f"Failed to publish ingest to Redis Stream: {e}")

async def _get_active_subscription(user_id: str, authorization: str) -> dict:
    """
    Look up the caller's active subscription — its package is the source of
    their `max_events` cap and `has_watermark` flag, and its billing period
    scopes which events count against that cap — via the API Gateway (never the subscription-service directly —
    it's not on this service's network in every deployment, the gateway is).

    subscription-service now always resolves to an active subscription for
    any authenticated user — a real one if they bought a package, otherwise a
    virtual subscription against whichever package is configured as the Free
    tier there (see GET /api/v1/subscriptions/{user_id}). So there is no
    hardcoded numeric fallback here anymore — the Free tier's limits are
    driven entirely by that package's `limits` in the database, and we just
    trust whatever comes back.

    Returns the subscription dict, or {} if the subscription-service can't be
    reached, times out, or returns something malformed. Callers fail open on
    {} (unlimited events, no watermark) rather than blocking event creation
    over an unrelated infrastructure hiccup — these are soft business rules,
    not a security boundary.
    """
    url = f"{settings.api_gateway_url}/api/v1/subscriptions/{user_id}"
    try:
        async with httpx.AsyncClient(timeout=_SUBSCRIPTION_LOOKUP_TIMEOUT) as client:
            response = await client.get(url, headers={"Authorization": authorization})
    except httpx.HTTPError as e:
        logger.warning(f"Subscription lookup failed for user {user_id}: {e}")
        return {}

    if response.status_code != 200:
        logger.warning(
            f"Subscription lookup for user {user_id} returned {response.status_code}: {response.text[:200]}"
        )
        return {}

    try:
        subscriptions = response.json()
    except ValueError:
        logger.warning(f"Subscription lookup for user {user_id} returned non-JSON body")
        return {}

    active_subscription = next(
        (s for s in subscriptions if isinstance(s, dict) and s.get("status") == "active"),
        None,
    )
    if not active_subscription:
        # Shouldn't happen anymore — subscription-service always returns an
        # active (real or virtual Free) subscription — but if it somehow
        # doesn't, fail open rather than guess at a number.
        logger.warning(f"No active subscription (real or virtual) returned for user {user_id}")
        return {}

    return active_subscription

def _package_of(subscription: dict) -> dict:
    package = subscription.get("package")
    return package if isinstance(package, dict) else {}

def _parse_utc(value) -> Optional[datetime]:
    """An ISO timestamp from the subscription API as a naive UTC datetime
    (Event.created_at is naive UTC), or None if missing/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed

def _quota_period_start(subscription: dict) -> Optional[datetime]:
    """Start of the window whose events count against `max_events`, or None
    to count every event the user has.

    A paid subscription counts from its current billing period's start
    (falling back to when the subscription was created, e.g. one activated
    by an admin without a period), so upgrading starts the count fresh
    instead of the new package inheriting events from the old one. The
    synthesized Free-tier subscription has no billing period — and its
    created_at is just "now" — so the Free limit counts all events."""
    if not subscription:
        return None
    start = _parse_utc(subscription.get("current_period_start"))
    if start is None and subscription.get("is_virtual") is False:
        start = _parse_utc(subscription.get("created_at"))
    return start

def _max_events_limit(package: dict) -> Optional[int]:
    """The package's `max_events` cap, or None for "unlimited"."""
    limits = package.get("limits") or {}
    photographer_limits = limits.get("photographer_limits") or {}
    # Missing key or explicit `null` both mean "unlimited" — dict.get with no
    # default returns None for either.
    return photographer_limits.get("max_events")

def _event_to_response(e) -> EventResponse:
    cover_url = s3_service.generate_presigned_url(e.cover_photo_url, expiration=3600) if e.cover_photo_url else None
    return EventResponse(
        id=e.id, name=e.name, date=e.date, drive_url=e.drive_url,
        cover_photo_url=cover_url, qr_token=e.qr_token, username=e.username,
        status=e.status.value, total_photos=e.total_photos, created_at=e.created_at,
        owner_id=e.owner_id, is_watermarked=e.is_watermarked,
        watermark_logo_url=e.watermark_logo_url, access_mode=e.access_mode,
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
    """Owner, or a collaborator with CAN_UPLOAD. Raises 403 otherwise.

    This is the closest thing this service has to an "upload photos"
    permission today: there's no direct photo-upload endpoint yet — photos
    are ingested by sm-ingestion-worker-service whenever this Drive URL
    changes (see update_event below), so that's the action CAN_UPLOAD gates.
    """
    from ..main import CollaboratorPermission
    if event.owner_id == user_id:
        return
    permission = _get_collaborator_permission(db, event.id, user_id)
    if permission == CollaboratorPermission.CAN_UPLOAD:
        return
    raise HTTPException(status_code=403, detail="You don't have upload access to this event.")

def _require_owner(event, user_id: str) -> None:
    """Strictly the event owner. There's no ADMIN collaborator role anymore —
    managing who has access, what they can do, and destructive actions like
    deleting the event are all the owner's call alone; CAN_UPLOAD only ever
    gates content (see _require_upload_access above)."""
    if event.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Only the event owner can do this.")

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
    credentials: HTTPAuthorizationCredentials = Depends(oauth2_scheme),
):
    from ..main import Event, EventStatus, redis_client
    clean_username = event_data.username.strip().lower().lstrip('@') if event_data.username else None
    if not clean_username:
        raise HTTPException(status_code=400, detail="Collection username is required.")

    # Subscription-service authenticates this same JWT and requires the
    # user_id in the URL to match its own `sub` claim, so we forward the
    # caller's own token rather than minting a new one.
    subscription = await _get_active_subscription(
        user_id, authorization=f"{credentials.scheme} {credentials.credentials}"
    )
    package = _package_of(subscription)
    max_events = _max_events_limit(package)
    is_watermarked = bool(package.get("has_watermark", False))
    if max_events is not None:
        events_in_period = db.query(Event).filter(Event.owner_id == user_id)
        period_start = _quota_period_start(subscription)
        if period_start is not None:
            events_in_period = events_in_period.filter(Event.created_at >= period_start)
        current_event_count = events_in_period.count()
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
        owner_id=user_id, status=EventStatus.PENDING, total_photos=0,
        # Inherited from the package at creation time and kept for the event's
        # lifetime, so later re-ingestions stay consistent even if the owner
        # changes plans.
        is_watermarked=is_watermarked,
        watermark_logo_url=(package.get("watermark_logo_url") or None) if is_watermarked else None,
        access_mode=event_data.access_mode,
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

@router.get("/{event_id}/photos", response_model=OwnerGalleryResponse)
def list_event_photos(event_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """The owner's view of their event's photos, served from the portal
    rather than the guest gallery — so it works for every access mode and
    doesn't depend on guest-side sign-in. Owner only: invite-only galleries
    deliberately make collaborators go through Google-verified guest access
    (sm-guest-service utils/access.py), and this must not bypass that."""
    from ..main import Event, Image
    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_owner(event, user_id)

    ttl = settings.photo_url_ttl_seconds
    images = db.query(Image).filter(Image.event_id == event.id).order_by(Image.created_at).all()
    photos = [
        OwnerPhoto(
            id=img.id,
            display_url=s3_service.generate_presigned_url(img.enhanced_url or img.s3_url, expiration=ttl),
            thumbnail_url=s3_service.generate_presigned_url(img.thumbnail_url, expiration=ttl) if img.thumbnail_url else None,
        )
        for img in images
    ]
    return OwnerGalleryResponse(event=_event_to_response(event), photos=photos)

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

    if event_data.access_mode is not None and event_data.access_mode != event.access_mode:
        # Who can see the gallery is the owner's call, like managing collaborators.
        _require_owner(event, user_id)
        event.access_mode = event_data.access_mode

    drive_url_changed = event.drive_url != event_data.drive_url

    event.name = event_data.name
    event.username = clean_username
    stale_urls: list[str] = []
    if drive_url_changed:
        event.drive_url = event_data.drive_url
        event.status = EventStatus.PENDING
        # Clear out existing images as we are ingesting a new folder — and
        # their S3 objects, which would otherwise be orphaned.
        stale_urls = _image_object_urls(db.query(Image).filter(Image.event_id == event_id).all())
        db.query(Image).filter(Image.event_id == event_id).delete()

    db.commit()
    db.refresh(event)
    # Before publishing the re-ingest, so the worker's new uploads can't be
    # deleted by this cleanup.
    _delete_s3_objects(stale_urls, event_id)

    # Only re-trigger ingestion if the drive_url has actually changed
    if drive_url_changed and event.drive_url:
        _publish_ingest(redis_client, event.id, event.drive_url)
        
    return _event_to_response(event)


def _image_object_urls(images) -> list[str]:
    """Every S3 object an Image row points at: original, thumbnail, display copy."""
    return [url for img in images for url in (img.s3_url, img.thumbnail_url, img.enhanced_url) if url]

def _delete_s3_objects(urls: list[str], event_id: str) -> None:
    """Batched, non-fatal: the DB change already succeeded, and a few
    orphaned S3 files cost pennies — far better than a half-applied request."""
    try:
        s3_service.delete_objects(urls)
    except Exception as e:
        logger.error(f"S3 cleanup failed for event {event_id}: {e}")

@router.delete("/{event_id}")
def delete_event(event_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    from ..main import Event, Image, redis_client

    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_owner(event, user_id)

    # 1. Grab URLs before the rows disappear
    urls_to_delete = _image_object_urls(db.query(Image).filter(Image.event_id == event_id).all())

    # 2. DB delete — Image/Face cascade automatically via FK ondelete="CASCADE"
    db.delete(event)
    db.commit()

    # 3. S3 cleanup
    _delete_s3_objects(urls_to_delete, event_id)

    # 4. Notify guest service (separate DB, separate process → stream is
    # correct here); it prunes search history and the Rekognition collection.
    # Non-fatal: the event is already gone, and guest-service's periodic
    # orphan sweep catches history rows if this message is lost.
    try:
        redis_client.xadd("event.deleted", {"event_id": event_id})
    except Exception as e:
        logger.error(f"Failed to publish event.deleted for {event_id}: {e}")

    return {"status": "SUCCESS", "message": "Event deleted successfully"}