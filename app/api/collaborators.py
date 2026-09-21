import csv
import io
import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from .events import get_db, get_current_user_id
from ..services.s3 import s3_service
from ..schemas.schemas import (
    SharedEventResponse,
    BulkImportResponse,
    BulkCollaboratorResult,
    CollaboratorPermission as CollaboratorPermissionSchema,
)

router = APIRouter(prefix="/api/v1/events", tags=["Collaborators"])
logger = logging.getLogger(__name__)

_VALID_PERMISSIONS = {p.value for p in CollaboratorPermissionSchema}


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
    from ..main import Event, EventCollaborator, User, CollaboratorPermission as ModelPermission

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
    resolved_this_request: dict[str, "User"] = {}

    for row in rows:
        email = row["email"].lower()
        if not email or "@" not in email:
            results.append(BulkCollaboratorResult(email=row["email"] or "(blank)", status="invalid_email"))
            continue

        if owner_email and email == owner_email:
            results.append(BulkCollaboratorResult(email=email, status="is_owner"))
            continue

        permission = _parse_permission(row["permission"])

        target_user = resolved_this_request.get(email)
        if target_user is None:
            target_user = db.query(User).filter(User.email == email).first()
        if target_user is None:
            # No account with this email yet — create a placeholder so the
            # share is already waiting for them when they do sign up.
            target_user = User(email=email, is_anonymous=False)
            db.add(target_user)
            db.flush()  # assigns target_user.id without committing yet
        resolved_this_request[email] = target_user

        existing_link = (
            db.query(EventCollaborator)
            .filter(EventCollaborator.event_id == event.id, EventCollaborator.user_id == target_user.id)
            .first()
        )
        if existing_link:
            results.append(BulkCollaboratorResult(email=email, status="already_collaborator", permission=existing_link.permission.value))
            continue

        db.add(EventCollaborator(event_id=event.id, user_id=target_user.id, permission=ModelPermission(permission)))
        added += 1
        results.append(BulkCollaboratorResult(email=email, status="added", permission=permission))

    db.commit()

    return BulkImportResponse(
        total_rows=len(rows),
        added=added,
        skipped=len(rows) - added,
        results=results,
    )
