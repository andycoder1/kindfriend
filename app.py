import os
import sys
import time
import uuid
import io
import csv
import json
import datetime
import sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# --- Password hashing (bcrypt preferred; hashlib fallback for dev) ---
try:
    import bcrypt
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    def check_password(password: str, hashed: str) -> bool:
        return bcrypt.checkpw(password.encode(), hashed.encode())
except Exception:
    import hashlib
    print("⚠ bcrypt not found — using SHA256 fallback (dev only). Install bcrypt for production.", file=sys.stderr)
    def hash_password(password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()
    def check_password(password: str, hashed: str) -> bool:
        return hashlib.sha256(password.encode()).hexdigest() == hashed

from itsdangerous import URLSafeSerializer, BadSignature

# Optional Stripe
try:
    import stripe  # type: ignore
except Exception:
    stripe = None

# =========================
# Config & Persistent Paths
# =========================
DATA_DIR = os.getenv("DATA_DIR", "")  # e.g. /opt/data on Render
DB_FILE = os.path.join(DATA_DIR, "kindfriend.db") if DATA_DIR else "kindfriend.db"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")  # set in production
AUTH_COOKIE = "kf_auth"

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")  # You said you added this ✔
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
BILLING_RETURN_URL = os.getenv("BILLING_RETURN_URL", "")  # e.g. https://kindfriend.onrender.com

DONATION_NOTE = "Kind Friend donates 50% of all subscription fees to Samaritans (UK)."
DONATION_LINK = "https://www.samaritans.org/"

# OpenAI
if not OPENAI_API_KEY:
    print("❌ OPENAI_API_KEY not set. Set it in Render → Environment.", file=sys.stderr)
    API_AVAILABLE = False
    client = None
else:
    API_AVAILABLE = True
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"❌ Failed to init OpenAI client: {e}", file=sys.stderr)
        API_AVAILABLE = False
        client = None

# Stripe
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

SYSTEM_PROMPT = (
    "You are Kind Friend: a warm, respectful companion. You are not a therapist. "
    "If the user mentions self-harm or immediate danger, kindly suggest contacting UK Samaritans (116 123), "
    "NHS 111, or emergency services (999). Be concise and kind."
)

signer = URLSafeSerializer(SECRET_KEY, salt="kf-auth")

# =========================
# Database (SQLite)
# =========================
def init_db():
    if DATA_DIR:
        os.makedirs(DATA_DIR, exist_ok=True)
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            ts         REAL NOT NULL,
            archived   INTEGER NOT NULL DEFAULT 0,
            user_id    TEXT
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id         TEXT PRIMARY KEY,
            user_id    TEXT,
            title      TEXT,
            created_at REAL NOT NULL
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name  TEXT,
            bio           TEXT,
            created_at    REAL NOT NULL,
            stripe_customer_id TEXT,
            subscription_status TEXT
        )""")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit (
            key     TEXT PRIMARY KEY,
            tokens  REAL NOT NULL,
            updated REAL NOT NULL
        )""")

        # migrations
        try:
            cur.execute("SELECT user_id FROM messages LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE messages ADD COLUMN user_id TEXT")

        try:
            cur.execute("SELECT stripe_customer_id FROM users LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")

        try:
            cur.execute("SELECT subscription_status FROM users LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT")

        conn.commit()

def save_message(session_id: str, role: str, content: str, user_id: Optional[str]):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (session_id, role, content, ts, archived, user_id) VALUES (?, ?, ?, ?, 0, ?)",
            (session_id, role, content, time.time(), user_id),
        )
        conn.commit()

def get_recent_messages(session_id: str, limit: int = 20):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY ts DESC LIMIT ?",
            (session_id, limit),
        )
        rows = cur.fetchall()
    rows.reverse()
    return [{"role": r, "content": c} for (r, c) in rows]

def get_all_messages(session_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, content, ts, archived FROM messages WHERE session_id = ? ORDER BY ts ASC",
            (session_id,),
        )
        return cur.fetchall()

def create_session(user_id: Optional[str], title: str = "New chat") -> str:
    sid = str(uuid.uuid4())
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id, user_id, title, created_at) VALUES (?, ?, ?, ?)",
                    (sid, user_id, title, time.time()))
        conn.commit()
    return sid

def rename_session(session_id: str, title: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
        conn.commit()

def delete_session_and_messages(session_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        cur.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()

def list_sessions(user_id: Optional[str]):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        if user_id:
            cur.execute("SELECT id, title, created_at FROM sessions WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        else:
            cur.execute("SELECT id, title, created_at FROM sessions WHERE user_id IS NULL ORDER BY created_at DESC")
        rows = cur.fetchall()
    return [{"id": r[0], "title": r[1], "created_at": r[2]} for r in rows]

def create_user(username: str, password: str):
    uid = str(uuid.uuid4())
    pw_hash = hash_password(password)
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (id, username, password_hash, display_name, bio, created_at, stripe_customer_id, subscription_status) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
            (uid, username, pw_hash, username, "", time.time()),
        )
        conn.commit()
    return uid

def get_user_by_username(username: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, created_at, stripe_customer_id, subscription_status FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3], "bio": row[4],
            "created_at": row[5], "stripe_customer_id": row[6], "subscription_status": row[7]}

def get_user_by_id(user_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, created_at, stripe_customer_id, subscription_status FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3], "bio": row[4],
            "created_at": row[5], "stripe_customer_id": row[6], "subscription_status": row[7]}

def update_user_profile(user_id: str, display_name: Optional[str], bio: Optional[str]):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        if display_name is not None and bio is not None:
            cur.execute("UPDATE users SET display_name = ?, bio = ? WHERE id = ?", (display_name, bio, user_id))
        elif display_name is not None:
            cur.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))
        elif bio is not None:
            cur.execute("UPDATE users SET bio = ? WHERE id = ?", (bio, user_id))
        conn.commit()

def upsert_user_subscription(user_id: str, status: str, stripe_customer_id: Optional[str]):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        if stripe_customer_id:
            cur.execute("UPDATE users SET subscription_status = ?, stripe_customer_id = ? WHERE id = ?", (status, stripe_customer_id, user_id))
        else:
            cur.execute("UPDATE users SET subscription_status = ? WHERE id = ?", (status, user_id))
        conn.commit()

# -------- Rate limiting (SQLite token bucket) --------
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "20"))

def check_rate_limit(user_id: Optional[str], ip: str) -> bool:
    if RATE_LIMIT_RPM <= 0:
        return True
    max_tokens = float(RATE_LIMIT_RPM)
    refill_per_sec = max_tokens / 60.0
    key = f"user:{user_id}" if user_id else f"ip:{ip}"
    now = time.time()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT tokens, updated FROM rate_limit WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO rate_limit (key, tokens, updated) VALUES (?, ?, ?)", (key, max_tokens - 1.0, now))
            conn.commit()
            return True
        tokens, updated = float(row[0]), float(row[1])
        elapsed = max(0.0, now - updated)
        tokens = min(max_tokens, tokens + elapsed * refill_per_sec)
        if tokens < 1.0:
            cur.execute("UPDATE rate_limit SET tokens = ?, updated = ? WHERE key = ?", (tokens, now, key))
            conn.commit()
            return False
        tokens -= 1.0
        cur.execute("UPDATE rate_limit SET tokens = ?, updated = ? WHERE key = ?", (tokens, now, key))
        conn.commit()
        return True

init_db()

# =========================
# Safety & time utilities
# =========================
def crisis_guard(text: str) -> Optional[str]:
    lowered = text.lower()
    keywords = ["suicide", "kill myself", "self-harm", "end my life", "overdose", "hurt myself"]
    if any(k in lowered for k in keywords):
        return (
            "I'm really glad you reached out. You deserve support.\n\n"
            "If you're in the UK, you can call **Samaritans 116 123** any time, or visit a local A&E / call **999** in an emergency.\n"
            "If you're elsewhere, please contact your local emergency services or a trusted crisis line.\n\n"
            "I'm here to keep you company, but I'm not a substitute for professional help."
        )
    return None

def current_time_note():
    tz_name = os.getenv("APP_TZ", "Europe/London")
    now_local = datetime.datetime.now(ZoneInfo(tz_name))
    date_str = now_local.strftime("%A %d %B %Y")
    time_str = now_local.strftime("%H:%M")
    return f"Today is {date_str} and the local time is {time_str} in {tz_name}. Answer date/time questions using this."

# =========================
# Frontend (single file UI)
# =========================
INDEX_HTML = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kind Friend</title>
  <link rel="icon" href='data:image/svg+xml;utf8,
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="%237d7bff"/>
      <stop offset="100%" stop-color="%235fe1d9"/>
    </linearGradient>
  </defs>
  <circle cx="32" cy="32" r="30" fill="url(%23g)" />
  <text x="32" y="38" font-size="24" font-family="Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial" text-anchor="middle" fill="white">KF</text>
</svg

