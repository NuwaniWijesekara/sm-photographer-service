from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

class UserCreate(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class EventCreate(BaseModel):
    name: str
    drive_url: str

class EventUpdate(BaseModel):
    name: str
    drive_url: str

class EventResponse(BaseModel):
    id: str
    name: str
    date: datetime
    drive_url: Optional[str] = None
    cover_photo_url: Optional[str] = None
    qr_token: str
    status: str
    total_photos: int
    created_at: datetime