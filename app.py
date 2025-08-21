
import os
import sqlite3
import secrets
import hashlib
import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.hash import bcrypt
from fastapi import Request
from typing import Optional

def current_user_id(request: Request) -> Optional[int]:
    """Return the logged-in user_id from the session, or None if not logged in."""
    return request.session.get("user_id")

# --- App setup ---
app = FastAPI(title="Kind Friend")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- DB Setup ---
DB_PATH = os.getenv("APP_DB_PATH", "app.db")

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    # users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # memories
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()

@app.on_event("startup")
def _startup():
    init_db()

# --- Auth helpers ---
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
serializer = URLSafeTimedSerializer(SECRET_KEY)

def create_password_hash(password: str) -> str:
    return bcrypt.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.verify(password, hashed)

def get_user_by_email(email: str):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    return row

def create_user(email: str, password: str, display_name: Optional[str] = None):
    conn = db()
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
        (email, create_password_hash(password), display_name, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

# --- Memory helpers ---
def get_memories(user_id: int):
    conn = db()
    rows = conn.execute("SELECT * FROM memories WHERE user_id=? ORDER BY created_at ASC", (user_id,)).fetchall()
    conn.close()
    return rows

def add_memory(user_id: int, content: str):
    conn = db()
    conn.execute("INSERT INTO memories (user_id, content, created_at) VALUES (?, ?, ?)",
                 (user_id, content, datetime.datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def forget_memory(user_id: int, memory_id: int):
    conn = db()
    conn.execute("DELETE FROM memories WHERE id=? AND user_id=?", (memory_id, user_id))
    conn.commit()
    conn.close()

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # force login modal if not authenticated (simplified example, no full session handling here)
    return templates.TemplateResponse("home.html", {"request": request})

@app.post("/signup")
def signup(email: str = Form(...), password: str = Form(...), display_name: Optional[str] = Form(None)):
    if get_user_by_email(email):
        raise HTTPException(status_code=400, detail="Email already registered")
    create_user(email, password, display_name)
    return RedirectResponse("/", status_code=303)

@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    user = get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Invalid credentials")
    # TODO: Replace with session cookie handling
    return {"message": f"Logged in as {user['email']}"}

@app.post("/forgot-password")
def forgot_password(email: str = Form(...)):
    user = get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=400, detail="No such user")
    token = serializer.dumps(email, salt="password-reset")
    reset_link = f"/reset-password/{token}"
    # TODO: send by email in production
    return {"reset_link": reset_link}

@app.get("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_form(request: Request, token: str):
    try:
        email = serializer.loads(token, salt="password-reset", max_age=3600)
    except (SignatureExpired, BadSignature):
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})

@app.post("/reset-password/{token}")
def reset_password(token: str, new_password: str = Form(...)):
    try:
        email = serializer.loads(token, salt="password-reset", max_age=3600)
    except (SignatureExpired, BadSignature):
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    conn = db()
    conn.execute("UPDATE users SET password_hash=? WHERE email=?", (create_password_hash(new_password), email))
    conn.commit()
    conn.close()
    return {"message": "Password reset successful"}

@app.get("/memories/{user_id}")
def view_memories(user_id: int):
    return {"memories": [dict(r) for r in get_memories(user_id)]}

@app.post("/memories/{user_id}")
def add_user_memory(user_id: int, content: str = Form(...)):
    add_memory(user_id, content)
    return {"message": "Memory added"}

@app.delete("/memories/{user_id}/{memory_id}")
def delete_memory(user_id: int, memory_id: int):
    forget_memory(user_id, memory_id)
    return {"message": "Memory deleted"}
