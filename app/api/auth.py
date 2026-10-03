from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from ..config.settings import settings
from ..utils.security import verify_password, get_password_hash, create_access_token
from ..schemas.schemas import UserCreate, Token, GoogleLoginRequest

router = APIRouter(prefix="/auth", tags=["Authentication"])

def get_db():
    from ..main import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(user_data: UserCreate, db: Session = Depends(get_db)):
    from ..main import User
    existing = db.query(User).filter(User.email == user_data.email).first()
    if existing:
        if existing.password_hash:
            raise HTTPException(status_code=400, detail="Email already registered")
        # A placeholder row from a collaborator bulk-import (see
        # api/collaborators.py) — no password yet. Complete it in place so
        # its id (and any event_collaborators rows already pointing at it)
        # stay intact, rather than rejecting or creating a second account.
        existing.password_hash = get_password_hash(user_data.password)
        if user_data.name:
            existing.name = user_data.name
        db.commit()
        db.refresh(existing)
        return {"message": "Account created successfully.", "user_id": existing.id}
    user = User(
        email=user_data.email,
        password_hash=get_password_hash(user_data.password),
        name=user_data.name,
        is_anonymous=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"message": "Account created successfully.", "user_id": user.id}

@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    from ..main import User
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not user.password_hash or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(user)
    return {"access_token": token, "token_type": "bearer"}

@router.post("/anonymous", response_model=Token)
def login_anonymous(db: Session = Depends(get_db)):
    """Instant-access session — no email/password. Lets a QR-scanning guest
    start searching photos immediately; they can register/link an email later
    without losing their id (see `sub` in the issued token)."""
    from ..main import User
    user = User(is_anonymous=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user)
    return {"access_token": token, "token_type": "bearer"}

@router.post("/google", response_model=Token)
def login_google(data: GoogleLoginRequest, db: Session = Depends(get_db)):
    from ..main import User
    from google.oauth2 import id_token as google_id_token
    from google.auth.transport import requests as google_requests

    if not data.id_token:
        raise HTTPException(status_code=400, detail="Google id_token is required")

    try:
        idinfo = google_id_token.verify_oauth2_token(
            data.id_token, google_requests.Request(), settings.google_client_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Google token verification failed: {e}")

    email = idinfo.get("email")
    name = idinfo.get("name")
    if not email:
        raise HTTPException(status_code=400, detail="Google token does not contain email")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email, name=name, is_anonymous=False)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif not user.name and name:
        user.name = name
        db.commit()

    token = create_access_token(user, email_verified=idinfo.get("email_verified") is True)
    return {"access_token": token, "token_type": "bearer"}
