"""密码哈希与 JWT 签发校验。"""

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.common.config import settings
from app.common.errors import AppError

ALGORITHM = "HS256"

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """用 bcrypt 对明文密码做单向哈希。"""
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文是否与已存哈希匹配。"""
    return _pwd_context.verify(plain, hashed)


def create_access_token(
    *,
    subject: str,
    department_id: str | None = None,
    role_ids: list[str] | None = None,
    permissions: list[str] | None = None,
    expires_minutes: int | None = None,
) -> str:
    """签发登录 JWT，载荷含用户、部门、角色与权限。"""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.access_token_expire_minutes
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "exp": expire,
        "department_id": department_id,
        "role_ids": role_ids or [],
        "permissions": permissions or [],
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """解析 JWT；失败一律 401（AUTH_INVALID_TOKEN）。"""

    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise AppError.unauthorized() from exc
