from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from .events import get_db, get_current_user_id
from ..services.s3 import s3_service
from ..schemas.schemas import ReferenceFaceResponse

router = APIRouter(prefix="/api/v1/users", tags=["User Profile"])

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_REFERENCE_FACE_BYTES = 10 * 1024 * 1024  # 10MB


@router.post("/me/face", response_model=ReferenceFaceResponse)
async def upload_reference_face(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    """Uploads (or replaces) the caller's account-level reference face —
    the "Privacy-First AI Face Matching" feature: one photo stored once
    under the user's own control, used to search any event they have
    access to, instead of re-uploading a selfie for every event."""
    from ..main import User

    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Invalid image type. Use JPEG, PNG, or WEBP.")

    image_bytes = await file.read()
    if len(image_bytes) > _MAX_REFERENCE_FACE_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (max 10MB).")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Deterministic key — re-uploading overwrites the previous reference
    # face in place rather than accumulating orphaned objects in S3.
    key = f"users/{user_id}/reference-face.jpg"
    url = s3_service.upload_bytes(image_bytes, key)

    user.reference_face_url = url
    db.commit()

    return ReferenceFaceResponse(
        message="Reference face updated.",
        reference_face_url=s3_service.generate_presigned_url(url, expiration=3600),
    )
