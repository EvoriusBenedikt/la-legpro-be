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

_SESSION_LAST_SEEN_CACHE: dict = {}

def _should_update_last_seen(uid: str) -> bool:
    now = time.time()
    last = _SESSION_LAST_SEEN_CACHE.get(uid, 0.0)
    if (now - last) >= 60.0:
        if len(_SESSION_LAST_SEEN_CACHE) > 5000:
            _SESSION_LAST_SEEN_CACHE.clear()
        _SESSION_LAST_SEEN_CACHE[uid] = now
        return True
    return False

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
    """Seed the dummy document templates and ensure account preference columns exist (idempotent)."""
    try:
        pg_service.execute(
            "ALTER TABLE users "
            "ADD COLUMN IF NOT EXISTS display_name TEXT, "
            "ADD COLUMN IF NOT EXISTS avatar_color_index INT NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS preferred_governing_lang VARCHAR(8) NOT NULL DEFAULT 'id'"
        )
    except Exception:
        pass
    try:
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
    except Exception:
        pass

if not os.environ.get("PYTEST_CURRENT_TEST"):
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
        # FR-31: Refresh last_seen for active session tracking (throttled to 60s per user)
        if _should_update_last_seen(user_id):
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


class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    avatar_color_index: Optional[int] = None
    preferred_governing_lang: Optional[str] = None


@router.get("/account-summary")
def get_account_summary(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    user_row = pg_service.query_one(
        "SELECT id, username, display_name, email, role, email_verified, "
        "avatar_color_index, preferred_governing_lang, created_at "
        "FROM users WHERE id = %s",
        (uid,),
    )
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    ws_row = pg_service.query_one(
        "SELECT COUNT(DISTINCT COALESCE(notebook_id, id)) AS c "
        "FROM compliance_history WHERE user_id = %s",
        (uid,),
    )
    analyzed_row = pg_service.query_one(
        "SELECT COUNT(*) AS c FROM compliance_history WHERE user_id = %s",
        (uid,),
    )
    collab_row = pg_service.query_one(
        "SELECT COUNT(*) AS c FROM workspace_collaborators WHERE user_id = %s",
        (uid,),
    )

    grant_rows = pg_service.query(
        "SELECT ag.id, ag.doc_id, ag.expires_at, r.judul, r.nomor, r.jenis "
        "FROM access_grants ag "
        "LEFT JOIN regulations r ON r.id = ag.doc_id "
        "WHERE ag.granted_to = %s AND (ag.expires_at IS NULL OR ag.expires_at > NOW()) "
        "ORDER BY ag.created_at DESC",
        (uid,),
    )

    mute_rows = pg_service.query(
        "SELECT nm.target_type, nm.target_id, nm.created_at, ch.filename, ch.company_name "
        "FROM notification_mutes nm "
        "LEFT JOIN compliance_history ch ON ch.id = nm.target_id "
        "WHERE nm.user_id = %s "
        "ORDER BY nm.created_at DESC",
        (uid,),
    )

    created_at = user_row.get("created_at")
    return {
        "user": {
            "id": user_row["id"],
            "username": user_row["username"],
            "display_name": user_row.get("display_name"),
            "email": user_row.get("email") or "",
            "role": user_row.get("role") or "pengguna",
            "email_verified": bool(user_row.get("email_verified")),
            "avatar_color_index": int(user_row.get("avatar_color_index") or 0),
            "preferred_governing_lang": user_row.get("preferred_governing_lang") or "id",
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        },
        "stats": {
            "workspaces_count": int((ws_row or {}).get("c") or 0),
            "contracts_analyzed_count": int((analyzed_row or {}).get("c") or 0),
            "collaborations_count": int((collab_row or {}).get("c") or 0),
        },
        "access_grants": [
            {
                "id": r["id"],
                "doc_id": r["doc_id"],
                "title": r.get("judul") or r["doc_id"],
                "nomor": r.get("nomor"),
                "jenis": r.get("jenis"),
                "expires_at": r["expires_at"].isoformat() if hasattr(r.get("expires_at"), "isoformat") else r.get("expires_at"),
            }
            for r in grant_rows
        ],
        "muted_items": [
            {
                "target_type": r["target_type"],
                "target_id": r["target_id"],
                "title": r.get("filename") or r.get("company_name") or r["target_id"],
                "muted_at": r["created_at"].isoformat() if hasattr(r.get("created_at"), "isoformat") else r.get("created_at"),
            }
            for r in mute_rows
        ],
    }


@router.patch("/profile")
def update_profile(data: ProfileUpdate, request: Request, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    existing = pg_service.query_one(
        "SELECT username, display_name, email, email_verified, avatar_color_index, preferred_governing_lang "
        "FROM users WHERE id = %s",
        (uid,),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="User not found")

    sets = []
    params = []

    if data.display_name is not None:
        cleaned_name = data.display_name.strip()[:120]
        sets.append("display_name = %s")
        params.append(cleaned_name or None)

    if data.email is not None:
        cleaned_email = data.email.strip()
        if not cleaned_email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", cleaned_email):
            raise HTTPException(status_code=400, detail="Invalid email address")
        if cleaned_email.lower() != (existing.get("email") or "").lower():
            dup = pg_service.query_one(
                "SELECT id FROM users WHERE LOWER(email) = LOWER(%s) AND id <> %s",
                (cleaned_email, uid),
            )
            if dup:
                raise HTTPException(status_code=409, detail="Email is already in use")
            sets.append("email = %s")
            params.append(cleaned_email)
            sets.append("email_verified = FALSE")

    if data.avatar_color_index is not None:
        if not (0 <= data.avatar_color_index <= 7):
            raise HTTPException(status_code=400, detail="avatar_color_index must be between 0 and 7")
        sets.append("avatar_color_index = %s")
        params.append(data.avatar_color_index)

    if data.preferred_governing_lang is not None:
        lang = data.preferred_governing_lang.strip().lower()
        if lang not in ("id", "en"):
            raise HTTPException(status_code=400, detail="preferred_governing_lang must be 'id' or 'en'")
        sets.append("preferred_governing_lang = %s")
        params.append(lang)

    if sets:
        params.append(uid)
        updated = pg_service.execute_returning(
            f"UPDATE users SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id, username, display_name, email, role, email_verified, "
            "avatar_color_index, preferred_governing_lang, created_at",
            tuple(params),
        )[0]
    else:
        updated = pg_service.query_one(
            "SELECT id, username, display_name, email, role, email_verified, "
            "avatar_color_index, preferred_governing_lang, created_at "
            "FROM users WHERE id = %s",
            (uid,),
        )

    if data.avatar_color_index is not None:
        pg_service.execute(
            "UPDATE workspace_collaborators SET color_index = %s WHERE user_id = %s",
            (data.avatar_color_index, uid),
        )

    log_audit(uid, "PROFILE_UPDATE", existing["username"],
              f'ip={get_client_ip(request)} ua="{get_user_agent(request)}"')

    created_at = updated.get("created_at")
    return {
        "user": {
            "id": updated["id"],
            "username": updated["username"],
            "display_name": updated.get("display_name"),
            "email": updated.get("email") or "",
            "role": updated.get("role") or "pengguna",
            "email_verified": bool(updated.get("email_verified")),
            "avatar_color_index": int(updated.get("avatar_color_index") or 0),
            "preferred_governing_lang": updated.get("preferred_governing_lang") or "id",
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        }
    }


@router.post("/logout-all-devices")
def logout_all_devices(request: Request, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    row = pg_service.execute_returning(
        "UPDATE users SET token_version = token_version + 1 WHERE id = %s RETURNING username",
        (uid,),
    )
    username = row[0]["username"] if row else uid
    log_audit(uid, "LOGOUT_ALL_DEVICES", username,
              f'ip={get_client_ip(request)} ua="{get_user_agent(request)}"')
    return {"status": "ok"}
