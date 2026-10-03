from pydantic import BaseModel, EmailStr
from typing import Literal, Optional
from datetime import datetime
from enum import Enum

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None

class GoogleLoginRequest(BaseModel):
    id_token: str

class Token(BaseModel):
    access_token: str
    token_type: str

# Mirrors app.main.EventAccessMode.
AccessMode = Literal["public", "invite_only"]

class EventCreate(BaseModel):
    name: str
    drive_url: str
    username: str
    access_mode: AccessMode = "invite_only"

class EventUpdate(BaseModel):
    name: str
    drive_url: str
    username: str
    # Omitted = leave unchanged. Changing it is owner-only.
    access_mode: Optional[AccessMode] = None

class EventResponse(BaseModel):
    id: str
    name: str
    date: datetime
    drive_url: Optional[str] = None
    cover_photo_url: Optional[str] = None
    qr_token: str
    username: Optional[str] = None
    status: str
    total_photos: int
    created_at: datetime
    owner_id: Optional[str] = None
    is_watermarked: bool = False
    watermark_logo_url: Optional[str] = None
    access_mode: str = "public"

# Mirrors app.main.CollaboratorPermission (the SQLAlchemy enum) — kept as an
# independent definition here, the same way this module avoids importing
# from ..main at module scope elsewhere in the service.
class CollaboratorPermission(str, Enum):
    VIEW_ONLY = "VIEW_ONLY"
    CAN_UPLOAD = "CAN_UPLOAD"

class SharedEventResponse(BaseModel):
    id: str
    name: str
    date: datetime
    drive_url: Optional[str] = None
    cover_photo_url: Optional[str] = None
    qr_token: str
    username: Optional[str] = None
    status: str
    total_photos: int
    created_at: datetime
    owner_name: Optional[str] = None
    owner_email: Optional[str] = None
    permission: CollaboratorPermission

class BulkCollaboratorResult(BaseModel):
    email: str
    status: str  # "added" | "already_collaborator" | "is_owner" | "invalid_email" | "error"
    permission: Optional[CollaboratorPermission] = None
    detail: Optional[str] = None

class BulkImportResponse(BaseModel):
    total_rows: int
    added: int
    skipped: int
    results: list[BulkCollaboratorResult]

class AddCollaboratorRequest(BaseModel):
    email: EmailStr
    permission: CollaboratorPermission = CollaboratorPermission.VIEW_ONLY

class AddCollaboratorResponse(BaseModel):
    user_id: str
    email: str
    permission: CollaboratorPermission

class CollaboratorResponse(BaseModel):
    user_id: str
    email: Optional[str] = None
    name: Optional[str] = None
    permission: CollaboratorPermission

class UpdateCollaboratorRequest(BaseModel):
    permission: CollaboratorPermission