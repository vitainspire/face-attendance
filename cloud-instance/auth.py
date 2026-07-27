"""Authentication helpers: password hashing, JWT, and role-based dependencies."""
import ipaddress
import os
import secrets
import string
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

import database
import models

# --- Configuration ---
# The real secret lives only in the server's protected env file (/etc/webapp.env),
# never in source. Unlike a fallback value (which would let anyone forge a valid JWT
# using this exact string, since it's public in the repo), a missing env var must fail
# the whole process at startup — same pattern as CONTROL_PLANE_ENCRYPTION_KEY.
SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "JWT_SECRET_KEY is not set — generate one with "
        "`python -c \"import secrets; print(secrets.token_hex(32))\"` "
        "and put it in the server's env file. Refusing to sign tokens without it."
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 day

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


# --- Password policy: min 8 chars, 1 upper, 1 lower, 1 digit, 1 special ---
SPECIAL_CHARS = "!@#$%^&*()-_=+"


def validate_password_policy(password: str) -> Optional[str]:
    """Returns an error message if the password fails policy, else None."""
    if len(password) < 8:
        return "Password must be at least 8 characters long"
    if not any(c.isupper() for c in password):
        return "Password must contain at least 1 uppercase letter"
    if not any(c.islower() for c in password):
        return "Password must contain at least 1 lowercase letter"
    if not any(c.isdigit() for c in password):
        return "Password must contain at least 1 number"
    if not any(c in SPECIAL_CHARS for c in password):
        return f"Password must contain at least 1 special character ({SPECIAL_CHARS})"
    return None


def generate_compliant_password(length: int = 10) -> str:
    """Random password that always satisfies validate_password_policy — used for
    admin-generated teacher/parent passwords instead of a plain random token."""
    length = max(length, 8)
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(SPECIAL_CHARS),
    ]
    pool = string.ascii_letters + string.digits
    required += [secrets.choice(pool) for _ in range(length - len(required))]
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


# --- Login rate limiting (in-memory; single-process deployment) ---
# Applies to every login endpoint and every account — superadmin, and admin/teacher/
# parent in every school (default tenant + every provisioned one). 5 failed attempts
# locks the account out for the rest of a 15-minute window. Successful login clears
# the counter immediately. Keyed purely by username string, so callers namespace their
# own bucket (e.g. "superadmin:bob") if they need to keep it separate from other scopes.
LOGIN_LOCKOUT_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_WINDOW_SECONDS = 15 * 60
_failed_logins = defaultdict(list)  # username -> [timestamps of recent failures]


def _prune_old(username: str):
    cutoff = time.monotonic() - LOGIN_LOCKOUT_WINDOW_SECONDS
    _failed_logins[username] = [t for t in _failed_logins[username] if t > cutoff]


def check_login_lockout(username: str):
    """Raises 429 if this account has too many recent failed attempts."""
    _prune_old(username)
    if len(_failed_logins[username]) >= LOGIN_LOCKOUT_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in a few minutes.",
        )


def record_login_failure(username: str):
    _failed_logins[username].append(time.monotonic())


def clear_login_failures(username: str):
    _failed_logins.pop(username, None)


# --- Password hashing (bcrypt directly; bcrypt has a 72-byte input limit) ---
def get_password_hash(password: str) -> str:
    pw = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    pw = plain_password.encode("utf-8")[:72]
    try:
        return bcrypt.checkpw(pw, hashed_password.encode("utf-8"))
    except ValueError:
        return False


# --- JWT ---
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decodes a JWT without looking up any user — used by the super-admin dependency,
    which checks a separate table (SuperAdminUser) than get_current_user's models.User."""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def authenticate_user(db: Session, username: str, password: str) -> Optional[models.User]:
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return None
    return user


# --- Dependencies ---
def resolve_tenant(school_id: Optional[int]):
    """(db, model_url, model_key) for a school_id — the same tenant lookup
    _tenant_context uses below, extracted so it's callable standalone, outside any
    single request's dependency lifecycle. This is what lets a background worker (e.g.
    the /teacher/recognize queue) rebuild the right database session and model-service
    address for a job it's processing well after the original request has returned.
    Raises HTTPException on an inactive/missing school or an unreachable database —
    callers outside a request (like a queue worker) must catch this themselves."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    model_url, model_key = None, None  # None/None = use model_client's global default

    if school_id is None:
        return database.SessionLocal(), model_url, model_key

    central = database.SessionLocal()
    try:
        school = central.query(models.School).filter(
            models.School.id == school_id, models.School.status == "active"
        ).first()
    finally:
        central.close()
    if not school:
        raise credentials_exception
    import provisioning  # deferred: provisioning.py imports auth, so this avoids a cycle
    try:
        supabase_url = provisioning.decrypt_secret(school.supabase_db_url_encrypted)
        db = database.get_tenant_sessionmaker(supabase_url)()
        if school.elastic_ip and school.model_service_api_key_encrypted:
            # Defense-in-depth against SSRF, independent of the validation already done
            # when this address was first submitted (see main.py's submit_onboarding) —
            # this is the actual point where it's used to make an outbound request, so
            # it's checked again here regardless of how it got into the database.
            ip_obj = ipaddress.ip_address(school.elastic_ip)
            if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                    or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
                raise HTTPException(status_code=503, detail="This school's model service is misconfigured")
            model_url = f"http://{school.elastic_ip}:9100"
            model_key = provisioning.decrypt_secret(school.model_service_api_key_encrypted)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="This school's database is currently unreachable")
    return db, model_url, model_key


def _tenant_context(token: str = Depends(oauth2_scheme)):
    """Resolves (user, db_session) for the current request — the single source of truth
    that both get_current_user and get_tenant_db read from below. Sharing one dependency
    (FastAPI caches it per-request) means there's no ambiguity about which runs first;
    every dependant of this function sees the exact same session, so a route's own `db`
    param and its `current_user` are always looking at the same school's database.

    The JWT's `school_id` claim (added at login) says which tenant this session belongs
    to. No claim / null means the original/default tenant — every token issued before
    multi-tenancy existed implicitly falls into this case, so old sessions keep working
    unchanged."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_token(token)
    if not payload or not payload.get("sub"):
        raise credentials_exception
    username = payload["sub"]
    school_id = payload.get("school_id")

    db, model_url, model_key = resolve_tenant(school_id)

    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        db.close()
        raise credentials_exception
    # Tokens issued before a password reset carry the OLD token_version — comparing
    # against the CURRENT value on the row means resetting a password (or any other
    # deliberate invalidation) immediately revokes every already-issued token for that
    # account, instead of leaving them valid for up to their full 24h expiry. A token
    # with no "tv" claim (issued before this check existed) is treated as version 0,
    # matching every row's default, so it isn't broken by this rollout.
    if payload.get("tv", 0) != (user.token_version or 0):
        db.close()
        raise credentials_exception
    try:
        yield user, db, model_url, model_key, school_id
    finally:
        db.close()


def get_current_user(ctx=Depends(_tenant_context)) -> models.User:
    return ctx[0]


def get_school_id(ctx=Depends(_tenant_context)) -> Optional[int]:
    """The current logged-in user's school_id claim — None for the original/default
    tenant. Used by /teacher/recognize to capture which tenant a queued job belongs to,
    so the background worker can re-resolve the right database later via resolve_tenant."""
    return ctx[4]


def get_model_config(ctx=Depends(_tenant_context)):
    """(model_url, model_key) for the CURRENT LOGGED-IN USER's school's own dedicated
    recognition service — None/None for the original school (model_client.py then uses
    its own global env-var default, unchanged from before multi-tenancy existed)."""
    return ctx[2], ctx[3]


def get_tenant_db(ctx=Depends(_tenant_context)) -> Session:
    """DB session for the CURRENT LOGGED-IN USER's school — use this (not
    database.get_db) in any route that reads/writes per-school data."""
    return ctx[1]


def require_role(*allowed_roles: str):
    """Dependency factory: ensures the current user holds one of the allowed roles."""

    def _checker(current_user: models.User = Depends(get_current_user)) -> models.User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(allowed_roles)}",
            )
        return current_user

    return _checker
