import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import bcrypt
import jwt

from services import pg_service

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
    "insinyur ti": 6
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

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta if expires_delta else timedelta(hours=24))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
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
        # FR-31: Refresh last_seen for active session tracking
        try:
            pg_service.execute(
                "INSERT INTO active_sessions (user_id, username, role, last_seen) "
                "VALUES (%s, %s, %s, NOW()) "
                "ON CONFLICT (user_id) DO UPDATE SET last_seen = NOW(), "
                "role = EXCLUDED.role, username = EXCLUDED.username",
                (user_id, user_data["username"], user_data["role"])
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
        if current_user.get("role", "pengguna").lower() != exact_role.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Akses ditolak. Fitur ini khusus untuk role {exact_role}."
            )
        return current_user
    return role_dependency

# --- Schemas ---
from pydantic import Field, field_validator

def validate_password_strength(v: str) -> str:
    """Shared rule: at least one number and one symbol (no length floor)."""
    import re
    if not re.search(r"\d", v):
        raise ValueError("Password must contain at least one number")
    if not re.search(r"[^A-Za-z0-9]", v):
        raise ValueError("Password must contain at least one symbol")
    return v

class UserCreate(BaseModel):
    username: str
    email: str
    password: str = Field(...)
    role: Optional[str] = "pengguna"

    @field_validator("password")
    @classmethod
    def password_must_have_symbol_and_number(cls, v: str) -> str:
        return validate_password_strength(v)

class PasswordChange(BaseModel):
    old_password: str
    new_password: str = Field(...)

    @field_validator("new_password")
    @classmethod
    def new_password_must_have_symbol_and_number(cls, v: str) -> str:
        return validate_password_strength(v)

class UserLogin(BaseModel):
    username: str
    password: str

# --- Endpoints ---
import uuid

@router.post("/register")
def register(user: UserCreate):
    if pg_service.query_one("SELECT id FROM users WHERE username = %s", (user.username,)):
        raise HTTPException(status_code=400, detail="Username already registered")

    user_id = str(uuid.uuid4())
    hashed_password = get_password_hash(user.password)

    pg_service.execute(
        "INSERT INTO users (id, username, password_hash, email, role) VALUES (%s, %s, %s, %s, %s)",
        (user_id, user.username, hashed_password, user.email, "pengguna")
    )

    access_token = create_access_token(
        data={"sub": user_id, "username": user.username, "email": user.email, "role": "pengguna"}, 
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer", "user": {"id": user_id, "username": user.username, "email": user.email, "role": "pengguna"}}

@router.post("/login")
def login(user: UserLogin):
    # Migration M3: the legacy "role column may be missing" OperationalError
    # fallbacks are gone -- the PG schema (migrations/001) always has role.
    row = pg_service.query_one(
        "SELECT id, username, password_hash, email, role FROM users WHERE username = %s",
        (user.username,)
    )

    if not row or not verify_password(user.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    role = row["role"] or "pengguna"

    access_token = create_access_token(
        data={"sub": row["id"], "username": row["username"], "email": row["email"], "role": role}, 
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer", "user": {"id": row["id"], "username": row["username"], "email": row["email"], "role": role}}

@router.post("/change-password")
def change_password(data: PasswordChange, current_user: dict = Depends(get_current_user)):
    row = pg_service.query_one(
        "SELECT password_hash FROM users WHERE id = %s", (current_user["id"],)
    )
    if not row or not verify_password(data.old_password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if verify_password(data.new_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="New password must be different from the current password")
    pg_service.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (get_password_hash(data.new_password), current_user["id"])
    )
    return {"message": "Password updated successfully"}

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
