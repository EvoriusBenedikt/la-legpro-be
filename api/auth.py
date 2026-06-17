import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import bcrypt
import jwt

# --- Config ---
SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-lintasarta-key-2026")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 # 7 days

security = HTTPBearer()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "users.db")

router = APIRouter()

# --- Database Setup ---
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Users
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE,
            password_hash TEXT,
            email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Chats
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            sources_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Compliance
    c.execute('''
        CREATE TABLE IF NOT EXISTS compliance_history (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            filename TEXT,
            results_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Document Templates
    c.execute('''
        CREATE TABLE IF NOT EXISTS document_templates (
            id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            content_template TEXT,
            category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Migrations for compliance_history
    try:
        c.execute('ALTER TABLE compliance_history ADD COLUMN company_name TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE compliance_history ADD COLUMN expiration_date TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE users ADD COLUMN role TEXT DEFAULT "pengguna"')
    except sqlite3.OperationalError:
        pass

    # Seed dummy templates if empty
    c.execute('SELECT COUNT(*) FROM document_templates')
    if c.fetchone()[0] == 0:
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
        c.executemany("INSERT INTO document_templates (id, title, description, content_template, category) VALUES (?, ?, ?, ?, ?)", dummy_templates)

    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
        return {
            "id": user_id, 
            "username": payload.get("username"), 
            "email": payload.get("email"),
            "role": payload.get("role", "pengguna")
        }
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
class UserCreate(BaseModel):
    username: str
    email: str
    password: str
    role: Optional[str] = "pengguna"

class UserLogin(BaseModel):
    username: str
    password: str

# --- Endpoints ---
import uuid

@router.post("/register")
def register(user: UserCreate):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM users WHERE username = ?", (user.username,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Username already registered")
    
    user_id = str(uuid.uuid4())
    hashed_password = get_password_hash(user.password)
    
    c.execute("INSERT INTO users (id, username, password_hash, email, role) VALUES (?, ?, ?, ?, ?)", 
              (user_id, user.username, hashed_password, user.email, user.role.lower()))
    conn.commit()
    conn.close()
    
    access_token = create_access_token(
        data={"sub": user_id, "username": user.username, "email": user.email, "role": user.role.lower()}, 
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer", "user": {"id": user_id, "username": user.username, "email": user.email, "role": user.role.lower()}}

@router.post("/login")
def login(user: UserLogin):
    conn = get_db_connection()
    c = conn.cursor()
    
    # Also fetch role, but fallback safely if column missing due to incomplete migration
    try:
        c.execute("SELECT id, username, password_hash, email, role FROM users WHERE username = ?", (user.username,))
    except sqlite3.OperationalError:
        c.execute("SELECT id, username, password_hash, email, 'pengguna' as role FROM users WHERE username = ?", (user.username,))
        
    row = c.fetchone()
    conn.close()
    
    if not row or not verify_password(user.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    
    role = row["role"] if "role" in row.keys() else "pengguna"
    
    access_token = create_access_token(
        data={"sub": row["id"], "username": row["username"], "email": row["email"], "role": role}, 
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer", "user": {"id": row["id"], "username": row["username"], "email": row["email"], "role": role}}

@router.get("/me")
def read_users_me(current_user: dict = Depends(get_current_user)):
    return current_user

@router.get("/users")
def get_users(current_user: dict = Depends(get_current_user)):
    user_level = get_role_level(current_user.get("role", "pengguna"))
    if user_level < 2:  # Only managers and above can list users for ACL
        raise HTTPException(status_code=403, detail="Not authorized")
        
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("SELECT id, username, email, role FROM users")
    except sqlite3.OperationalError:
        c.execute("SELECT id, username, email, 'pengguna' as role FROM users")
        
    users = []
    for row in c.fetchall():
        row_level = get_role_level(row["role"])
        # Can only see users of equal or lower rank to grant access to (FR-21/FR-22)
        if row_level <= user_level and row["id"] != current_user["id"]:
            users.append({
                "id": row["id"],
                "username": row["username"],
                "email": row["email"],
                "role": row["role"]
            })
    conn.close()
    return {"users": users}
