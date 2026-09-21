import csv
import io
import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from sqlalchemy.orm import Session

from .events import get_db, get_current_user_id, _require_owner
from ..services.s3 import s3_service
from ..schemas.schemas import (
    SharedEventResponse,
    BulkImportResponse,
    BulkCollaboratorResult,
    AddCollaboratorRequest,
    AddCollaboratorResponse,
    CollaboratorResponse,
    UpdateCollaboratorRequest,
    CollaboratorPermission as CollaboratorPermissionSchema,
)

router = APIRouter(prefix="/api/v1/events", tags=["Collaborators"])
logger = logging.getLogger(__name__)

_VALID_PERMISSIONS = {p.value for p in CollaboratorPermissionSchema}


class CollaboratorAddError(Exception):
    """Raised by _resolve_and_link_collaborator for the non-success cases
    shared by both the CSV bulk import and the single manual-add endpoint."""
    def __init__(self, reason: str, permission: str | None = None):
        self.reason = reason
        self.permission = permission
        super().__init__(reason)


def _resolve_and_link_collaborator(
    db: Session,
    event,
    owner_email: str | None,
    email: str,
    permission: str,
    resolved_cache: dict | None = None,
):
    """Core logic shared by the CSV bulk import and the single manual-add
    endpoint: find (or create a placeholder for) the user with this email,
    then link them to the event with the given permission.

    `email` must already be normalized (stripped, lowercased, non-empty).
    Does not commit — callers commit once, after their own batch/request.
    Raises CollaboratorAddError("is_owner") or ("already_collaborator").
    """
    from ..main import EventCollaborator, User, CollaboratorPermission as ModelPermission

    if owner_email and email == owner_email:
        raise CollaboratorAddError("is_owner")

    target_user = resolved_cache.get(email) if resolved_cache is not None else None
    if target_user is None:
        target_user = db.query(User).filter(User.email == email).first()
    if target_user is None:
        # No account with this email yet — create a placeholder so the
        # share is already waiting for them when they do sign up.
        target_user = User(email=email, is_anonymous=False)
        db.add(target_user)
        db.flush()  # assigns target_user.id without committing yet
    if resolved_cache is not None:
        resolved_cache[email] = target_user

    existing_link = (
        db.query(EventCollaborator)
        .filter(EventCollaborator.event_id == event.id, EventCollaborator.user_id == target_user.id)
        .first()
    )
    if existing_link:
        raise CollaboratorAddError("already_collaborator", permission=existing_link.permission.value)

    link = EventCollaborator(event_id=event.id, user_id=target_user.id, permission=ModelPermission(permission))
    db.add(link)
    return target_user, link


@router.get("/shared", response_model=list[SharedEventResponse])
def list_shared_events(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    """Events the caller has been added to as a collaborator (not owned by them) — powers the "Shared with Me" tab."""
    from ..main import Event, EventCollaborator, User

    rows = (
        db.query(Event, EventCollaborator.permission, User.name, User.email)
        .join(EventCollaborator, EventCollaborator.event_id == Event.id)
        .outerjoin(User, User.id == Event.owner_id)
        .filter(EventCollaborator.user_id == user_id)
        .order_by(EventCollaborator.created_at.desc())
        .all()
    )

    results = []
    for event, permission, owner_name, owner_email in rows:
        cover_url = (
            s3_service.generate_presigned_url(event.cover_photo_url, expiration=3600)
            if event.cover_photo_url
            else None
        )
        results.append(
            SharedEventResponse(
                id=event.id,
                name=event.name,
                date=event.date,
                drive_url=event.drive_url,
                cover_photo_url=cover_url,
                qr_token=event.qr_token,
                username=event.username,
                status=event.status.value,
                total_photos=event.total_photos,
                created_at=event.created_at,
                owner_name=owner_name,
                owner_email=owner_email,
                permission=permission.value,
            )
        )
    return results


def _parse_permission(raw: str | None) -> str:
    if not raw:
        return "VIEW_ONLY"
    normalized = raw.strip().upper().replace("-", "_").replace(" ", "_")
    return normalized if normalized in _VALID_PERMISSIONS else "VIEW_ONLY"


def _read_csv_rows(raw_bytes: bytes) -> list[dict]:
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Could not read file — expected a UTF-8 encoded CSV.")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file is empty.")

    field_map = {f.strip().lower(): f for f in reader.fieldnames if f}
    if "email" not in field_map:
        raise HTTPException(status_code=400, detail="CSV must have an 'email' column.")

    email_key = field_map["email"]
    permission_key = field_map.get("permission")

    rows = []
    for raw_row in reader:
        rows.append({
            "email": (raw_row.get(email_key) or "").strip(),
            "permission": (raw_row.get(permission_key) or "").strip() if permission_key else "",
        })
    return rows


@router.post("/{event_id}/collaborators/bulk", response_model=BulkImportResponse)
async def bulk_add_collaborators(
    event_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Bulk-add collaborators to an event from an uploaded CSV (email, optional permission columns).

    Only the event's owner may do this. Emails that don't match an existing
    user get a placeholder account created for them, so the invite is
    already waiting once they sign up or log in with that email (see the
    signup "complete a placeholder" path in api/auth.py).
    """
    from ..main import Event

    event = db.query(Event).filter(Event.id == event_id, Event.owner_id == user_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    filename = (file.filename or "").lower()
    if filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=415, detail="Excel files aren't supported yet — please upload a CSV.")

    raw_bytes = await file.read()
    rows = _read_csv_rows(raw_bytes)

    owner_email = (event.owner.email or "").strip().lower() if event.owner else None

    results: list[BulkCollaboratorResult] = []
    added = 0
    # Tracks placeholder users created earlier in this same upload, so
    # duplicate emails within one file resolve to one row without needing a
    # DB flush per iteration.
    resolved_this_request: dict = {}

    for row in rows:
        email = row["email"].lower()
        if not email or "@" not in email:
            results.append(BulkCollaboratorResult(email=row["email"] or "(blank)", status="invalid_email"))
            continue

        permission = _parse_permission(row["permission"])
        try:
            _resolve_and_link_collaborator(db, event, owner_email, email, permission, resolved_cache=resolved_this_request)
        except CollaboratorAddError as e:
            results.append(BulkCollaboratorResult(email=email, status=e.reason, permission=e.permission))
            continue

        added += 1
        results.append(BulkCollaboratorResult(email=email, status="added", permission=permission))

    db.commit()

    return BulkImportResponse(
        total_rows=len(rows),
        added=added,
        skipped=len(rows) - added,
        results=results,
    )


@router.post(
    "/{event_id}/collaborators",
    response_model=AddCollaboratorResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_collaborator(
    event_id: str,
    payload: AddCollaboratorRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Add a single collaborator by email — the manual, one-at-a-time
    counterpart to the CSV bulk import above, sharing the exact same
    find-or-create-placeholder-then-link logic. Owner-only, same as bulk."""
    from ..main import Event

    event = db.query(Event).filter(Event.id == event_id, Event.owner_id == user_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    owner_email = (event.owner.email or "").strip().lower() if event.owner else None
    email = payload.email.strip().lower()
    permission = payload.permission.value

    try:
        target_user, _link = _resolve_and_link_collaborator(db, event, owner_email, email, permission)
    except CollaboratorAddError as e:
        detail = {
            "is_owner": "The event owner can't be added as a collaborator.",
            "already_collaborator": "This user is already a collaborator on this event.",
        }.get(e.reason, "Could not add collaborator.")
        raise HTTPException(status_code=400, detail=detail)

    db.commit()
    return AddCollaboratorResponse(user_id=target_user.id, email=email, permission=permission)


@router.get("/{event_id}/collaborators", response_model=list[CollaboratorResponse])
def list_collaborators(
    event_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """All collaborators on this event. Owner only — there's no ADMIN
    collaborator role anymore, so nobody but the owner manages this."""
    from ..main import Event, EventCollaborator, User

    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_owner(event, user_id)

    rows = (
        db.query(EventCollaborator, User.email, User.name)
        .join(User, User.id == EventCollaborator.user_id)
        .filter(EventCollaborator.event_id == event_id)
        .order_by(EventCollaborator.created_at.asc())
        .all()
    )
    return [
        CollaboratorResponse(user_id=link.user_id, email=email, name=name, permission=link.permission.value)
        for link, email, name in rows
    ]


@router.delete("/{event_id}/collaborators/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_collaborator(
    event_id: str,
    user_id: str,
    db: Session = Depends(get_db),
    caller_id: str = Depends(get_current_user_id),
):
    """Removes one collaborator's access. Owner only — there's no ADMIN
    collaborator role anymore; CAN_UPLOAD collaborators manage content,
    not who else has access.

    The owner is never a row in event_collaborators (see Event.owner_id),
    so there's nothing here for them to accidentally remove themselves
    from — but a caller passing the owner's own id as {user_id} gets a
    clear 400 instead of a confusing 404.
    """
    from ..main import Event, EventCollaborator

    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_owner(event, caller_id)

    if user_id == event.owner_id:
        raise HTTPException(status_code=400, detail="The event owner can't be removed as a collaborator.")

    link = (
        db.query(EventCollaborator)
        .filter(EventCollaborator.event_id == event_id, EventCollaborator.user_id == user_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Collaborator not found")

    db.delete(link)
    db.commit()


@router.patch("/{event_id}/collaborators/{user_id}", response_model=CollaboratorResponse)
def update_collaborator(
    event_id: str,
    user_id: str,
    payload: UpdateCollaboratorRequest,
    db: Session = Depends(get_db),
    caller_id: str = Depends(get_current_user_id),
):
    """Changes one collaborator's permission level. Owner only, same as remove."""
    from ..main import Event, EventCollaborator, User, CollaboratorPermission as ModelPermission

    event = db.query(Event).filter(Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _require_owner(event, caller_id)

    if user_id == event.owner_id:
        raise HTTPException(status_code=400, detail="The event owner's access can't be changed.")

    link = (
        db.query(EventCollaborator)
        .filter(EventCollaborator.event_id == event_id, EventCollaborator.user_id == user_id)
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Collaborator not found")

    link.permission = ModelPermission(payload.permission.value)
    db.commit()

    target_user = db.query(User).filter(User.id == user_id).first()
    return CollaboratorResponse(
        user_id=user_id,
        email=target_user.email if target_user else None,
        name=target_user.name if target_user else None,
        permission=payload.permission,
    )
