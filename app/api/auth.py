import logging
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlalchemy.orm import Session
from ..config.settings import settings
from ..utils.security import verify_password, get_password_hash, create_access_token
from ..schemas.schemas import UserCreate, Token, GoogleLoginRequest

router = APIRouter(prefix="/auth", tags=["Authentication"])
logger = logging.getLogger(__name__)

def _find_user_by_email(db: Session, email: str):
    """Emails are matched case-insensitively everywhere (collaborator invites
    are stored lowercased; older accounts may not be), so `Jane@x.com` and
    `jane@x.com` are always the same account."""
    from ..main import User
    return db.query(User).filter(func.lower(User.email) == email.strip().lower()).first()

def get_db():
    from ..main import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(user_data: UserCreate, db: Session = Depends(get_db)):
    from ..main import EventCollaborator, User
    email = user_data.email.strip().lower()
    existing = _find_user_by_email(db, email)
    if existing:
        if existing.password_hash:
            raise HTTPException(status_code=400, detail="Email already registered")
        # Emails on any event's guest list (collaborators) can only sign in
        # with Google. Password signup never proves the caller owns the
        # email, so letting it claim an invited placeholder would hand the
        # invite (and its access) to whoever registered first.
        on_guest_list = (
            db.query(EventCollaborator.id).filter(EventCollaborator.user_id == existing.id).first()
        )
        if on_guest_list:
            raise HTTPException(
                status_code=403,
                detail="This email has been invited to an event. Please sign in with Google using this email.",
            )
        # A placeholder no longer on any guest list (e.g. its invites were
        # removed) — no password yet. Complete it in place so its id stays
        # intact, rather than rejecting or creating a second account.
        existing.password_hash = get_password_hash(user_data.password)
        if user_data.name:
            existing.name = user_data.name
        db.commit()
        db.refresh(existing)
        return {"message": "Account created successfully.", "user_id": existing.id}
    user = User(
        email=email,
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
    user = _find_user_by_email(db, form_data.username)
    if not user or not user.password_hash or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(user)
    return {"access_token": token, "token_type": "bearer"}

@router.post("/google", response_model=Token)
def login_google(data: GoogleLoginRequest, db: Session = Depends(get_db)):
    from ..main import User

    raw_token = (data.id_token or "").strip()
    if not raw_token:
        # e.g. the Google popup was blocked or closed before returning a credential
        raise HTTPException(status_code=400, detail="Google sign-in didn't complete. Please try again.")

    if not settings.google_client_id:
        # Without an audience, verify_oauth2_token would accept tokens issued
        # to *any* Google client — refuse rather than verify insecurely.
        logger.error("GOOGLE_CLIENT_ID is not set — rejecting Google sign-in")
        raise HTTPException(status_code=503, detail="Google sign-in is not configured.")

    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests
        from google.auth.exceptions import GoogleAuthError
    except ImportError:
        logger.exception("google-auth (with requests transport) is not installed")
        raise HTTPException(status_code=503, detail="Google sign-in is temporarily unavailable.")

    try:
        idinfo = google_id_token.verify_oauth2_token(
            raw_token, google_requests.Request(), settings.google_client_id
        )
    except ValueError as e:
        # Malformed, expired, wrong audience or bad signature. Details go to
        # the log only — they're not useful (or safe) to echo to the client.
        logger.info(f"Rejected Google token: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired Google sign-in. Please try again.")
    except GoogleAuthError as e:
        # e.g. couldn't fetch Google's signing certificates
        logger.warning(f"Google token verification unavailable: {e}")
        raise HTTPException(status_code=503, detail="Couldn't reach Google to verify sign-in. Please try again.")

    email = idinfo.get("email")
    name = idinfo.get("name")
    if not email:
        raise HTTPException(status_code=400, detail="Google token does not contain email")

    email = email.strip().lower()
    user = _find_user_by_email(db, email)
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
