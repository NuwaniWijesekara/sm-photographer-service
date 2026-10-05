from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from datetime import datetime, timedelta
import bcrypt
import jwt
from ..config.settings import settings

oauth2_scheme = HTTPBearer()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def create_access_token(user, email_verified: bool = False) -> str:
    """Builds the single, standard JWT payload issued to every user —
    whether they registered with a password or signed in via Google.
    Anonymous sessions no longer exist; `is_anonymous` stays in the payload
    (always false for new tokens) so downstream services can reject any
    still-unexpired token issued by the removed POST /auth/anonymous.

    `email_verified` is a property of *this sign-in*, not the account: only a
    Google sign-in proves the caller controls the email (password signup
    never verifies it), and guest-service requires it for invite-only
    galleries."""
    payload = {
        "sub": user.id,
        "email": user.email or "",
        "name": user.name or "",
        "is_anonymous": user.is_anonymous,
        "email_verified": bool(email_verified),
        "exp": datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

def get_current_user_id(credentials: HTTPAuthorizationCredentials = Depends(oauth2_scheme)) -> str:
    """The caller's user id (JWT `sub`). 401 on a missing, expired or invalid
    token, or a leftover anonymous-session token."""
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token") from None
    user_id = payload.get("sub")
    if not user_id or payload.get("is_anonymous"):
        raise HTTPException(status_code=401, detail="Invalid token")
    return user_id