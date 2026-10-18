import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from psycopg import errors as pg_errors
import bcrypt
import jwt

from services import pg_service
from services.db_service import log_audit
from services.request_meta import get_client_ip, get_user_agent, sanitize_username

# --- Config ---
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET environment variable is not set. Cannot start without a secure key.")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 24 hours

security = HTTPBearer()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

router = APIRouter()

# --- Database Setup ---
# Migration M3 (cutover): the users.db DDL and ALTER-based column migrations
# init_db() used to carry are owned by migrations/001-004 in PostgreSQL
# (applied by the postgres container on first init, and by the M2/M5
# migration tooling on existing installs). Only the idempotent dummy
# template seed remains, still run at import time exactly as before.
def init_db():
    """Seed the dummy document templates on a fresh database (idempotent)."""
    if pg_service.query_one("SELECT 1 FROM document_templates LIMIT 1") is not None:
        return
    dummy_templates = [
        (
            "tpl_pks_01",
            "Perjanjian Kerja Sama (PKS) Standar",
            "Template PKS standar untuk kerja sama B2B umum dengan penyedia layanan teknologi.",
            "# PERJANJIAN KERJA SAMA\n\nPada hari ini, dibuat kesepakatan antara:\n1. PIHAK PERTAMA: [Nama Perusahaan 1]\n2. PIHAK KEDUA: [Nama Perusahaan 2]\n\n## PASAL 1 - RUANG LINGKUP\nKerja sama ini mencakup [Deskripsi Layanan].\n\n## PASAL 2 - JANGKA WAKTU\nPerjanjian ini berlaku selama [Durasi Bulan/Tahun].",
            "PKS"
        ),
        (
            "tpl_nda_01",
            "Non-Disclosure Agreement (NDA)",
            "Template perjanjian kerahasiaan dua arah untuk diskusi awal komersial.",
            "# NON-DISCLOSURE AGREEMENT\n\nPerjanjian kerahasiaan ini ditandatangani oleh:\n1. PIHAK PENGUNGKAP: [Nama Pengungkap]\n2. PIHAK PENERIMA: [Nama Penerima]\n\n## PASAL 1 - INFORMASI RAHASIA\nInformasi yang dilindungi adalah [Jenis Informasi Rahasia].",
            "NDA"
        )
    ]
    pg_service.execute_many(
        "INSERT INTO document_templates (id, title, description, content_template, category) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        dummy_templates,
    )

init_db()

# --- Security Utils & RBAC ---
ROLE_LEVELS = {
    "pengguna": 1,
    "manajer": 2,
    "direktur": 3,
    "admin": 4,
    "sekretaris perusahaan": 5,
    "insinyur ti": 6,
    # Unofficial developer role (2026-10-07): sees and operates every feature
    # surface, from engineer monitoring down to regular-user views. Never
    # offered or assignable through the app (register defaults to pengguna;
    # admin_change_role rejects any new_role at/above the actor's level).
    # Granted only via scripts/seed_demo_users.py --role dewa or direct SQL.
    "dewa": 7
}

def get_role_level(role: str) -> int:
    return ROLE_LEVELS.get(role.lower(), 1)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except ValueError:
        return False

def get_password_hash(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

# Computed once at import with the same default cost as real hashes. login()
# verifies against it when the username does not exist so unknown and known
# usernames cost the same bcrypt time (no timing oracle for account existence).
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-never-used", bcrypt.gensalt()).decode()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Sign a JWT. Callers put ``"tv"`` (users.token_version) in ``data``;
    get_current_user rejects the token once the DB version moves past it."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta if expires_delta else timedelta(hours=24))
    to_encode.update({"iat": now, "exp": expire, "jti": uuid.uuid4().hex})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(request: Request,
                           credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")
        user_data = {
            "id": user_id,
            "username": payload.get("username"),
            "email": payload.get("email"),
            "role": payload.get("role", "pengguna")
        }
        # Live account check (migration 007 + admin user management,
        # 2026-10-06): the JWT lives 24 h, but role changes and deactivation
        # must take effect on the NEXT request. The DB row is authoritative
        # for role; a missing or inactive row is a hard 401 (the FE axios
        # interceptor then clears the token and routes to login). Fail-OPEN
        # on infrastructure errors: a DB blip must not mass-logout every
        # session — the request's real work hits the same broken DB anyway.
        # Migration 011 (token_version): the JWT's `tv` claim must equal the
        # row's token_version. Password change/reset, force-logout,
        # deactivation and role change bump the row, which revokes every
        # token issued before. A token with no `tv` (issued before this
        # deploy) counts as version 0 and stays valid until a bump or expiry.
        try:
            acct = pg_service.query_one(
                "SELECT role, is_active, token_version FROM users WHERE id = %s", (user_id,)
            )
            if acct is None or not acct["is_active"]:
                raise HTTPException(status_code=401, detail="Account deactivated or no longer exists")
            try:
                token_tv = int(payload.get("tv", 0))
            except (TypeError, ValueError):
                token_tv = -1
            if token_tv != acct["token_version"]:
                raise HTTPException(status_code=401, detail="Session is no longer valid. Please sign in again.")
            user_data["role"] = acct["role"] or user_data["role"]
        except HTTPException:
            raise
        except Exception:
            pass
        # FR-31: Refresh last_seen for active session tracking
        try:
            pg_service.execute(
                "INSERT INTO active_sessions (user_id, username, role, last_seen, ip_address) "
                "VALUES (%s, %s, %s, NOW(), %s) "
                "ON CONFLICT (user_id) DO UPDATE SET last_seen = NOW(), "
                "role = EXCLUDED.role, username = EXCLUDED.username, "
                "ip_address = EXCLUDED.ip_address",
                (user_id, user_data["username"], user_data["role"], get_client_ip(request))
            )
        except Exception:
            pass
        return user_data
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")

def require_role(min_role: str):
    def role_dependency(current_user: dict = Depends(get_current_user)):
        user_role = current_user.get("role", "pengguna").lower()
        if user_role == "insinyur ti" and min_role != "insinyur ti":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Akses ditolak. Role Insinyur TI tidak dapat mengakses fitur bisnis."
            )
        
        min_level = get_role_level(min_role)
        user_level = get_role_level(user_role)
        if user_level < min_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Akses ditolak. Fitur ini memerlukan level {min_role} atau lebih tinggi."
            )
        return current_user
    return role_dependency

def require_exact_role(exact_role: str):
    def role_dependency(current_user: dict = Depends(get_current_user)):
        role = current_user.get("role", "pengguna").lower()
        # 'dewa' (unofficial developer role, level 7) passes every exact-role
        # gate — all of engineer.py and admin user management included.
        if role != exact_role.lower() and role != "dewa":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Akses ditolak. Fitur ini khusus untuk role {exact_role}."
            )
        return current_user
    return role_dependency

# --- Schemas ---
from pydantic import Field, field_validator

def validate_password_strength(v: str) -> str:
    """Shared rule (register + change-password) for NEW passwords: 10 to 72
    bytes (bcrypt ignores/rejects anything past 72 bytes), at least one
    uppercase letter, one number, and one symbol; whitespace does not count
    as a symbol (user decision 2026-10-05). The 10-char floor (2026-10-14)
    applies only to new passwords; login never enforces strength so legacy
    short passwords keep working."""
    if len(v) < 10:
        raise ValueError("Password must be at least 10 characters")
    if len(v.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 bytes")
    if not re.search(r"[A-Z]", v):
        raise ValueError("Password must contain at least one uppercase letter")
    if not re.search(r"\d", v):
        raise ValueError("Password must contain at least one number")
    if not re.search(r"[^A-Za-z0-9\s]", v):
        raise ValueError("Password must contain at least one symbol")
    return v

# Registration-only username charset (decision D1): ASCII letters, digits,
# '.', '_', '-', 3-64 chars, first char alphanumeric. Login accepts any
# string up to 64 chars so legacy usernames keep working.
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")

def _check_password_bytes(v: str) -> str:
    """bcrypt only uses the first 72 bytes; cap at 72 so oversized input is a
    clean 422 instead of a bcrypt ValueError / silent truncation."""
    if len(v.encode("utf-8")) > 72:
        raise ValueError("Password must be at most 72 bytes")
    return v

class UserCreate(BaseModel):
    username: str
    email: str = Field(max_length=254)
    password: str = Field(...)
    role: Optional[str] = "pengguna"

    @field_validator("username")
    @classmethod
    def username_charset(cls, v: str) -> str:
        if not USERNAME_PATTERN.fullmatch(v):
            raise ValueError(
                "Username must be 3-64 characters: letters, numbers, '.', '_' or '-', "
                "starting with a letter or number")
        return v

    @field_validator("password")
    @classmethod
    def password_must_have_symbol_and_number(cls, v: str) -> str:
        return validate_password_strength(v)

class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(...)

    @field_validator("old_password")
    @classmethod
    def old_password_bytes(cls, v: str) -> str:
        return _check_password_bytes(v)

    @field_validator("new_password")
    @classmethod
    def new_password_must_have_symbol_and_number(cls, v: str) -> str:
        return validate_password_strength(v)

class UserLogin(BaseModel):
    # No strength rule and no minimum length beyond 1: legacy short passwords
    # must still log in. Only size caps, to bound bcrypt work and DB input.
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)

    @field_validator("username")
    @classmethod
    def username_no_nul(cls, v: str) -> str:
        # PostgreSQL text cannot hold NUL; reject here instead of a 500.
        if "\x00" in v:
            raise ValueError("Invalid username")
        return v

    @field_validator("password")
    @classmethod
    def password_bytes(cls, v: str) -> str:
        return _check_password_bytes(v)

# --- Lockout (policy P1) ---------------------------------------------------
# 5 consecutive failures lock the account for 1 minute; every further failure
# after the lock expires doubles it (1,2,4,8,16,32,60,60... minutes). Locks are
# temporary, never permanent. Attempts made while locked neither change the
# counters nor extend the lock (and are rejected even with the right
# password). A failure arriving after 15 quiet minutes (counted from the later
# of the last failure and the lock expiry) restarts the count at 1.
# Existing users keep the counters on `users`; unknown usernames use
# `login_attempts` keyed (username_lower, ip) with identical behaviour, so the
# response never reveals whether an account exists.
LOCK_THRESHOLD = 5
_LOGIN_FAIL_MSG = "Incorrect username or password"
_LOGIN_LOCKED_MSG = "Too many attempts, try again later"

_housekeeping_lock = threading.Lock()
_last_housekeeping = 0.0

def _housekeeping():
    """Purge stale login_attempts rows at most once per 10 min per process."""
    global _last_housekeeping
    now = time.monotonic()
    with _housekeeping_lock:
        if _last_housekeeping and now - _last_housekeeping < 600:
            return
        _last_housekeeping = now
    try:
        pg_service.execute(
            "DELETE FROM login_attempts WHERE last_failed_at < now() - interval '1 day' "
            "AND (locked_until IS NULL OR locked_until < now())"
        )
    except Exception:
        pass

def _lock_minutes(count: int) -> int:
    return min(60, 2 ** min(max(count - LOCK_THRESHOLD, 0), 6))

def _locked_response(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429, detail=_LOGIN_LOCKED_MSG,
        headers={"Retry-After": str(max(1, int(retry_after or 1)))},
    )

def _record_failure_user(user_id: str) -> dict:
    """Atomically count a failure for an existing user (row-locked)."""
    rows = pg_service.execute_returning(
        "WITH cur AS ("
        "  SELECT id,"
        "         CASE WHEN GREATEST(last_failed_at, locked_until) IS NULL"
        "                OR GREATEST(last_failed_at, locked_until) < now() - interval '15 minutes'"
        "              THEN 1 ELSE failed_attempts + 1 END AS n"
        "  FROM users WHERE id = %s FOR UPDATE"
        ") "
        "UPDATE users u "
        "SET failed_attempts = cur.n,"
        "    last_failed_at  = now(),"
        "    locked_until    = CASE WHEN cur.n >= 5"
        "                           THEN now() + make_interval(mins => LEAST(60, power(2, LEAST(cur.n - 5, 6))::int))"
        "                           ELSE NULL END "
        "FROM cur WHERE u.id = cur.id "
        "RETURNING u.failed_attempts AS count, u.locked_until,"
        "          CEIL(EXTRACT(EPOCH FROM (u.locked_until - now())))::int AS retry_after",
        (user_id,),
    )
    return rows[0] if rows else {"count": 1, "locked_until": None, "retry_after": None}

def _record_failure_unknown(username_lower: str, ip: str) -> dict:
    """Same algorithm for a non-existent username, keyed (username_lower, ip).
    One transaction: the INSERT..ON CONFLICT row lock serialises concurrent
    failures until commit."""
    with pg_service.get_conn() as conn:
        conn.execute(
            "INSERT INTO login_attempts (username_lower, ip, failed_count, last_failed_at) "
            "VALUES (%s, %s, 1, now()) "
            "ON CONFLICT (username_lower, ip) DO UPDATE SET "
            "  failed_count = CASE WHEN GREATEST(login_attempts.last_failed_at, login_attempts.locked_until) IS NULL"
            "                        OR GREATEST(login_attempts.last_failed_at, login_attempts.locked_until) < now() - interval '15 minutes'"
            "                      THEN 1 ELSE login_attempts.failed_count + 1 END,"
            "  last_failed_at = now() "
            "RETURNING failed_count",
            (username_lower, ip),
        )
        cur = conn.execute(
            "UPDATE login_attempts SET locked_until = CASE WHEN failed_count >= 5"
            "  THEN now() + make_interval(mins => LEAST(60, power(2, LEAST(failed_count - 5, 6))::int))"
            "  ELSE NULL END "
            "WHERE username_lower = %s AND ip = %s "
            "RETURNING failed_count AS count, locked_until,"
            "  CEIL(EXTRACT(EPOCH FROM (locked_until - now())))::int AS retry_after",
            (username_lower, ip),
        )
        return cur.fetchone()

def _reset_user_counters(user_id: str, stamp_login: bool = False):
    pg_service.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_failed_at = NULL"
        + (", last_login = NOW()" if stamp_login else "")
        + " WHERE id = %s",
        (user_id,),
    )

# --- Endpoints ---


@router.post("/register")
def register(user: UserCreate, request: Request):
    # NOTE: the FE maps these exact detail strings to form fields for
    # red-border attribution (la-legpro-fe/src/components/Auth.tsx,
    # SERVER_FIELD_ERRORS). Keep both sides in sync when rewording.
    # Duplicate checks are case-insensitive (migration 011 adds matching
    # unique indexes on lower(username) / lower(email)).
    if pg_service.query_one("SELECT id FROM users WHERE lower(username) = lower(%s)", (user.username,)):
        raise HTTPException(status_code=400, detail="Username already registered")
    # Whitespace makes an email invalid — rejected, not auto-stripped (user
    # decision). The FE validates this before submit and maps the detail
    # string below to the email field; this guard covers direct API clients.
    if any(c.isspace() for c in user.email):
        raise HTTPException(status_code=400, detail="Email must not contain whitespace")
    # Emails are case-insensitive by convention (providers like Gmail treat
    # Joe@X and joe@x as the same mailbox): normalize to lowercase for the
    # duplicate check and for storage. LOWER(email) so legacy mixed-case rows
    # still match. The FE register input also lowercases as the user types.
    email = user.email.lower()
    if pg_service.query_one("SELECT id FROM users WHERE LOWER(email) = %s", (email,)):
        raise HTTPException(status_code=400, detail="Email already registered")

    user_id = str(uuid.uuid4())
    hashed_password = get_password_hash(user.password)

    # Two concurrent registrations can both pass the checks above; the unique
    # indexes then reject the loser. Turn that into the same clean 400.
    try:
        pg_service.execute(
            "INSERT INTO users (id, username, password_hash, email, role) VALUES (%s, %s, %s, %s, %s)",
            (user_id, user.username, hashed_password, email, "pengguna")
        )
    except pg_errors.UniqueViolation as e:
        constraint = (getattr(getattr(e, "diag", None), "constraint_name", None) or "").lower()
        if "email" in constraint:
            raise HTTPException(status_code=400, detail="Email already registered")
        raise HTTPException(status_code=400, detail="Username already registered")

    log_audit(user_id, "REGISTER", user.username,
              f'ip={get_client_ip(request)} ua="{get_user_agent(request)}"')

    access_token = create_access_token(
        data={"sub": user_id, "username": user.username, "email": email, "role": "pengguna", "tv": 0},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer", "user": {"id": user_id, "username": user.username, "email": email, "role": "pengguna"}}

@router.post("/login")
def login(user: UserLogin, request: Request):
    """Order matters (policy P2): look up, ALWAYS run one bcrypt verify (against
    a dummy hash for unknown usernames), then decide. That keeps timing and
    responses identical for existing and non-existent usernames."""
    ip = get_client_ip(request)
    ua = get_user_agent(request)
    attempted = sanitize_username(user.username)
    username_lower = user.username.lower()
    _housekeeping()

    # Case-insensitive lookup; if legacy case-variant duplicates exist the
    # exact-case match wins, else the oldest account.
    row = pg_service.query_one(
        "SELECT id, username, password_hash, email, role, is_active, token_version, "
        "       (locked_until IS NOT NULL AND locked_until > now()) AS is_locked, "
        "       GREATEST(1, COALESCE(CEIL(EXTRACT(EPOCH FROM (locked_until - now())))::int, 1)) AS retry_after "
        "FROM users WHERE lower(username) = lower(%s) "
        "ORDER BY (username = %s) DESC, created_at ASC LIMIT 1",
        (user.username, user.username)
    )
    attempt = None
    if row is None:
        attempt = pg_service.query_one(
            "SELECT (locked_until IS NOT NULL AND locked_until > now()) AS is_locked, "
            "       GREATEST(1, COALESCE(CEIL(EXTRACT(EPOCH FROM (locked_until - now())))::int, 1)) AS retry_after "
            "FROM login_attempts WHERE username_lower = %s AND ip = %s",
            (username_lower, ip)
        )

    pw_ok = verify_password(user.password, row["password_hash"] if row else _DUMMY_HASH)
    uid = row["id"] if row else ""
    ctx = f'ip={ip} ua="{ua}"'

    # Locked: reject without touching counters, even with the right password.
    state = row if row else attempt
    if state and state["is_locked"]:
        log_audit(uid, "LOGIN_FAILURE", attempted, f"{ctx} reason=locked")
        raise _locked_response(state["retry_after"])

    if row is None or not pw_ok:
        res = _record_failure_user(row["id"]) if row else _record_failure_unknown(username_lower, ip)
        count = res["count"]
        log_audit(uid, "LOGIN_FAILURE", attempted, f"{ctx} count={count} reason=bad_credentials")
        if count >= LOCK_THRESHOLD:
            minutes = _lock_minutes(count)
            log_audit(uid, "ACCOUNT_LOCKED", attempted, f"ip={ip} minutes={minutes} count={count}")
            raise _locked_response(res["retry_after"] or minutes * 60)
        raise HTTPException(status_code=401, detail=_LOGIN_FAIL_MSG)

    # Deactivation gate (migration 007) runs AFTER password verification so a
    # wrong-password probe cannot reveal that an account exists but is
    # disabled. 403, not 401: the FE interceptor treats 401 as token expiry.
    # The detail string is mapped to localized copy by the FE (Auth.tsx
    # MESSAGE_CODE_MAP → i18n authErrDeactivated) — keep both sides in sync.
    if not row["is_active"]:
        _reset_user_counters(row["id"])
        raise HTTPException(status_code=403, detail="Account deactivated. Contact your administrator.")

    role = row["role"] or "pengguna"

    # Success: clear lockout state and stamp last_login (migration 010).
    _reset_user_counters(row["id"], stamp_login=True)
    log_audit(row["id"], "LOGIN_SUCCESS", row["username"], ctx)

    access_token = create_access_token(
        data={"sub": row["id"], "username": row["username"], "email": row["email"], "role": role,
              "tv": row["token_version"]},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer", "user": {"id": row["id"], "username": row["username"], "email": row["email"], "role": role}}

@router.post("/change-password")
def change_password(data: PasswordChange, request: Request,
                    current_user: dict = Depends(get_current_user)):
    row = pg_service.query_one(
        "SELECT password_hash FROM users WHERE id = %s", (current_user["id"],)
    )
    if not row or not verify_password(data.old_password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if verify_password(data.new_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="New password must be different from the current password")
    # Bump token_version: every JWT issued before this change (including the
    # one used for this request) stops working. The response carries a fresh
    # token with the new version so the caller's own session continues.
    updated = pg_service.execute_returning(
        "UPDATE users SET password_hash = %s, token_version = token_version + 1 "
        "WHERE id = %s RETURNING token_version, username, email, role",
        (get_password_hash(data.new_password), current_user["id"])
    )[0]
    role = updated["role"] or "pengguna"
    log_audit(current_user["id"], "PASSWORD_CHANGE", updated["username"],
              f'ip={get_client_ip(request)} ua="{get_user_agent(request)}"')
    access_token = create_access_token(
        data={"sub": current_user["id"], "username": updated["username"], "email": updated["email"],
              "role": role, "tv": updated["token_version"]},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {
        "message": "Password updated successfully",
        "access_token": access_token,
        "token_type": "bearer",
        "user": {"id": current_user["id"], "username": updated["username"],
                 "email": updated["email"], "role": role},
    }

@router.get("/me")
def read_users_me(current_user: dict = Depends(get_current_user)):
    return current_user

@router.get("/users")
def get_users(current_user: dict = Depends(get_current_user)):
    user_level = get_role_level(current_user.get("role", "pengguna"))
    if user_level < 2:  # Only managers and above can list users for ACL
        raise HTTPException(status_code=403, detail="Not authorized")

    rows = pg_service.query("SELECT id, username, email, role FROM users")

    users = []
    for row in rows:
        row_level = get_role_level(row["role"] or "pengguna")
        # Can only see users of equal or lower rank to grant access to (FR-21/FR-22)
        if row_level <= user_level and row["id"] != current_user["id"]:
            users.append({
                "id": row["id"],
                "username": row["username"],
                "email": row["email"],
                "role": row["role"]
            })
    return {"users": users}
