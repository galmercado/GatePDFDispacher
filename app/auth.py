"""Password hashing, JWT helpers and RBAC dependencies."""
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from . import config
from .database import get_db
from .models import Role, User

pwd_context = CryptContext(schemes=["bcrypt"], bcrypt__rounds=12, deprecated="auto")
ALGORITHM = "HS256"

# Verified against when the email is unknown, to keep login timing uniform.
_DUMMY_HASH = pwd_context.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        return False


def create_access_token(user: User, minutes: int | None = None) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes or config.ACCESS_TOKEN_MINUTES)
    payload = {"sub": str(user.id), "role": user.role.value, "exp": expires}
    return jwt.encode(payload, config.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, config.SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.hashed_password) or not user.is_active:
        return None
    return user


def set_auth_cookie(response, token: str) -> None:
    response.set_cookie(
        config.COOKIE_NAME,
        token,
        max_age=config.ACCESS_TOKEN_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
    )


def clear_auth_cookie(response) -> None:
    response.delete_cookie(config.COOKIE_NAME, path="/")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(config.COOKIE_NAME)
    payload = decode_access_token(token) if token else None
    if not payload:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_role(*roles: Role):
    """Dependency factory: the current user must hold one of `roles`."""
    allowed = set(roles)

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="You do not have permission to do that")
        return user

    return dependency


require_admin = require_role(Role.admin)
require_staff = require_role(Role.admin, Role.doorman)
