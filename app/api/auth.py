from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import jwt
from ..config.settings import settings
from ..utils.security import verify_password, get_password_hash
from ..schemas.schemas import UserCreate, Token

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
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(email=user_data.email, hashed_password=get_password_hash(user_data.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"message": "Account created successfully.", "user_id": user.id}

@router.post("/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    from ..main import User
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = jwt.encode(
        {"sub": user.id, "exp": datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm
    )
    return {"access_token": token, "token_type": "bearer"}