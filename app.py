import os
import sys
import time
import uuid
import io
import csv
import json
import hmac
import math
import datetime
import sqlite3
from zoneinfo import ZoneInfo
from typing import Optional

from fastapi import FastAPI, Request, Query, Header
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# ---- Password hashing (bcrypt if available; fallback to hashlib for dev) ----
try:
    import bcrypt
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    def check_password(password: str, hashed: str) -> bool:
        return bcrypt.checkpw(password.encode(), hashed.encode())
except ImportError:
    import hashlib
    print("⚠ bcrypt not found — using SHA256 fallback (dev only). Install bcrypt for production.", file=sys.stderr)
    def hash_password(password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()
    def check_password(password: str, hashed: str) -> bool:
        return hashlib.sha256(password.encode()).hexdigest() == hashed

from itsdangerous import URLSafeSerializer, BadSignature

# Optional Stripe (only used if keys are present)
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
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")  # recurring price id (e.g., price_***)
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
BILLING_RETURN_URL = os.getenv("BILLING_RETURN_URL", "")  # your site URL (e.g., https://kindfriend.onrender.com)

DONATION_NOTE = "Kind Friend donates 50% of all subscription fees to Samaritans (UK)."
DONATION_LINK = "https://www.samaritans.org/"

# Graceful API key check
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

        # messages
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                ts         REAL NOT NULL,
                archived   INTEGER NOT NULL DEFAULT 0,
                user_id    TEXT
            )
        """)
        # sessions (saved threads)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                user_id    TEXT,
                title      TEXT,
                created_at REAL NOT NULL
            )
        """)
        # users
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
            )
        """)
        # rate limit
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rate_limit (
                key       TEXT PRIMARY KEY,
                tokens    REAL NOT NULL,
                updated   REAL NOT NULL
            )
        """)
        # add any missing columns
        try:
            cur.execute("SELECT stripe_customer_id FROM users LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        try:
            cur.execute("SELECT subscription_status FROM users LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT")
        try:
            cur.execute("SELECT user_id FROM messages LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE messages ADD COLUMN user_id TEXT")
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
        cur.execute(
            "INSERT INTO sessions (id, user_id, title, created_at) VALUES (?, ?, ?, ?)",
            (sid, user_id, title, time.time()),
        )
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
            # anonymous sessions (no user)
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
        return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3], "bio": row[4], "created_at": row[5],
                "stripe_customer_id": row[6], "subscription_status": row[7]}

def get_user_by_id(user_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, created_at, stripe_customer_id, subscription_status FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3], "bio": row[4], "created_at": row[5],
                "stripe_customer_id": row[6], "subscription_status": row[7]}

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
# 20 requests/min per user (if logged in) else per IP
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "20"))

def now_ts() -> float:
    return time.time()

def rate_key(user_id: Optional[str], ip: str) -> str:
    return f"user:{user_id}" if user_id else f"ip:{ip}"

def check_rate_limit(user_id: Optional[str], ip: str) -> bool:
    # returns True if allowed, False if over limit
    if RATE_LIMIT_RPM <= 0:
        return True
    max_tokens = RATE_LIMIT_RPM
    refill_per_sec = RATE_LIMIT_RPM / 60.0
    key = rate_key(user_id, ip)
    t = now_ts()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT tokens, updated FROM rate_limit WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO rate_limit (key, tokens, updated) VALUES (?, ?, ?)", (key, max_tokens-1, t))
            conn.commit()
            return True
        tokens, updated = row
        tokens = float(tokens)
        elapsed = max(0.0, t - float(updated))
        tokens = min(max_tokens, tokens + elapsed * refill_per_sec)
        if tokens < 1.0:
            # not enough tokens
            cur.execute("UPDATE rate_limit SET tokens = ?, updated = ? WHERE key = ?", (tokens, t, key))
            conn.commit()
            return False
        tokens -= 1.0
        cur.execute("UPDATE rate_limit SET tokens = ?, updated = ? WHERE key = ?", (tokens, t, key))
        conn.commit()
        return True

init_db()

# =========================
# Auth helpers
# =========================
def set_auth_cookie(resp, user_id: str):
    token = signer.dumps({"user_id": user_id, "ts": time.time()})
    resp.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="Lax", max_age=60*60*24*180)

def clear_auth_cookie(resp):
    resp.delete_cookie(AUTH_COOKIE)

def get_current_user_id(request: Request) -> Optional[str]:
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return None
    try:
        data = signer.loads(token)
        return data.get("user_id")
    except BadSignature:
        return None

# =========================
# Frontend (Responsive UI with sidebar, streaming, large text)
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
</svg>'/>

  <style>
    :root {
      --bg: #0b1020; --bg-accent: #101735; --bg-2: #0e142c;
      --text: #e8ecf7; --muted: #a9b0c5;
      --card: rgba(255,255,255,0.08); --card-2: rgba(255,255,255,0.06);
      --border: rgba(255,255,255,0.12);
      --brand: #7d7bff; --brand-2: #5fe1d9;
      --shadow: 0 12px 40px rgba(0,0,0,0.28);
      --radius: 16px; --gutter: 16px;
      --sidebar: 300px;
    }
    [data-theme="light"] {
      --bg: #f3f6ff; --bg-accent: #eaf0ff; --bg-2: #ffffff;
      --text: #0b1020; --muted: #465170;
      --card: rgba(255,255,255,0.9); --card-2: rgba(255,255,255,0.85);
      --border: rgba(0,15,40,0.12);
      --shadow: 0 12px 40px rgba(0,0,0,0.12);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body { min-height: 100svh; color: var(--text); font: 15px/1.5 Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      background:
        radial-gradient(1200px 600px at -10% -20%, #22264a 0%, transparent 60%),
        radial-gradient(1200px 600px at 110% 120%, #113a3a 0%, transparent 60%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg-accent) 100%); }
    .shell { display: grid; grid-template-columns: var(--sidebar) 1fr; gap: var(--gutter); padding: var(--gutter); min-height: 100svh; }
    .card { background: var(--card); backdrop-filter: blur(10px); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); }

    /* Sidebar */
    .sidebar { padding: 12px; display: flex; flex-direction: column; gap: 10px; }
    .logo { width: 44px; height: 44px; border-radius: 14px; display: grid; place-items: center;
            background: linear-gradient(135deg, var(--brand), var(--brand-2)); color: white; font-weight: 800; }
    .s-row { display:flex; align-items:center; gap:8px; }
    .title { margin: 0; font-size: 18px; font-weight: 800; }
    .donate { font-size: 12px; color: var(--muted); }
    .donate a { color: var(--brand-2); text-decoration:none; border-bottom:1px dashed var(--brand-2); }

    .sessions { overflow:auto; display:flex; flex-direction:column; gap:6px; padding-right:4px; }
    .session { padding:8px 10px; border:1px solid var(--border); border-radius:12px; cursor:pointer; background:var(--card-2); }
    .session.active { outline:2px solid var(--brand-2); }
    .s-actions { display:flex; gap:6px; }
    .btn { border: 1px solid var(--border); background: var(--card-2); color: var(--text); padding:8px 10px; border-radius:999px; cursor:pointer; }
    .btn.primary { background: linear-gradient(135deg, var(--brand), var(--brand-2)); color:white; border-color:transparent; }
    .chip { font-size: 12px; color: var(--muted); padding: 6px 10px; border-radius: 999px; border: 1px dashed var(--border); }

    /* Main */
    .main { display:flex; flex-direction:column; gap: var(--gutter); }
    .header { display:flex; flex-wrap:wrap; align-items:center; gap:12px; padding:12px 16px; }
    .toolbar { margin-left:auto; display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
    .auth input { padding: 8px 10px; border-radius: 10px; border: 1px solid var(--border); background: rgba(255,255,255,0.05); color: var(--text); min-width: 120px; }
    #logout, #edit-profile { display:none; }

    .chat-card { display:flex; flex-direction:column; min-height:0; height: calc(100svh - 180px); }
    .chat { flex:1 1 auto; min-height:0; overflow:auto; padding:16px; display:grid; gap:12px; border-top-left-radius: var(--radius); border-top-right-radius: var(--radius);
      background: radial-gradient(800px 400px at 20% 0%, rgba(125,123,255,0.10) 0%, transparent 60%), radial-gradient(800px 400px at 80% 100%, rgba(95,225,217,0.10) 0%, transparent 60%); }
    .row { display:grid; grid-template-columns:auto 1fr; gap:10px; align-items:start; }
    .row.user { grid-template-columns:1fr auto; }
    .avatar { width:36px; height:36px; border-radius:50%; display:grid; place-items:center; color:white; font-weight:800;
      background: linear-gradient(135deg, var(--brand), var(--brand-2)); }
    .row.user .avatar { display:none; }
    .bubble { max-width:80ch; padding:12px 14px; border-radius:14px; border:1px solid var(--border); background:rgba(255,255,255,0.06); word-wrap:anywhere; }
    .row.user .bubble { background:rgba(125,123,255,0.14); }
    .row.bot .bubble { background:rgba(95,225,217,0.14); }
    body.large .bubble { font-size: 18px; line-height: 1.7; }

    .composer { display:grid; grid-template-columns: 1fr auto; gap:10px; padding:12px; border-top:1px solid var(--border); background:var(--card-2);
      border-bottom-left-radius:var(--radius); border-bottom-right-radius:var(--radius); }
    .input { padding:14px; border-radius:12px; border:1px solid var(--border); background:rgba(255,255,255,0.06); color:var(--text); }

    .hint { text-align:center; color:var(--muted); font-size:12px; }

    /* Modal */
    .modal-backdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:40; }
    .modal { display:none; position:fixed; inset:0; z-index:50; place-items:center; }
    .modal.on, .modal-backdrop.on { display:grid; }
    .modal-card { width:min(520px, 94vw); background:var(--bg-2); color:var(--text); border:1px solid var(--border); border-radius:18px; box-shadow:var(--shadow); padding:16px; }
    .modal-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
    .modal-title { font-weight:800; font-size:16px; }
    .xbtn { border:1px solid var(--border); background:var(--card-2); color:var(--text); border-radius:10px; padding:6px 10px; cursor:pointer; }
    .form-row { display:grid; gap:6px; margin:10px 0; }
    .form-row input, .form-row textarea { padding:10px 12px; border-radius:12px; border:1px solid var(--border); background:rgba(255,255,255,0.06); color:var(--text); }

    @media (max-width: 900px) {
      .shell { grid-template-columns: 1fr; }
      .sidebar { order:2; }
      .main { order:1; }
      .chat-card { height: calc(100svh - 240px); }
    }
  </style>
</head>
<body>
  <div class="shell">
    <!-- Sidebar -->
    <aside class="card sidebar">
      <div class="s-row"><div class="logo">KF</div><h1 class="title" style="margin-left:6px;">Kind Friend</h1></div>
      <div class="donate">💚 <strong>50% donated</strong> to <a href="https://www.samaritans.org/" target="_blank">Samaritans</a>.</div>
      <div class="s-actions">
        <button id="new-chat" class="btn primary">New chat</button>
        <button id="large" class="btn">A A</button>
      </div>
      <div class="sessions" id="sessions"></div>
    </aside>

    <!-- Main -->
    <main class="main">
      <div class="card header">
        <span class="chip" id="me">Not signed in</span>
        <div class="auth" id="auth">
          <input id="u" placeholder="username" />
          <input id="p" placeholder="password" type="password" />
          <button id="signup" class="btn">Sign up</button>
          <button id="login"  class="btn primary">Log in</button>
          <button id="logout" class="btn">Log out</button>
          <button id="edit-profile" class="btn">Profile</button>
          <button id="upgrade" class="btn primary">Upgrade</button>
          <button id="billing" class="btn">Billing</button>
        </div>
        <div class="toolbar">
          <button id="theme" class="btn">Theme</button>
          <button id="download-txt" class="btn">.txt</button>
          <button id="download-csv" class="btn">.csv</button>
        </div>
      </div>

      <section class="card chat-card">
        <div class="chat" id="chat"></div>
        <div class="composer">
          <input id="message" class="input" autocomplete="off" placeholder="Share what's on your mind…" />
          <button id="send" class="btn primary">Send</button>
        </div>
      </section>

      <div class="hint">Kind Friend is a supportive companion, not a therapist. In crisis, call 999 or Samaritans 116 123 (UK).</div>
    </main>
  </div>

  <!-- Profile Modal -->
  <div class="modal-backdrop" id="modal-backdrop"></div>
  <div class="modal" id="profile-modal" role="dialog" aria-modal="true" aria-labelledby="profile-title">
    <div class="modal-card">
      <div class="modal-header">
        <div class="modal-title" id="profile-title">Edit profile</div>
        <button class="xbtn" id="close-modal">Close</button>
      </div>
      <div class="form-row">
        <label for="display_name">Display name</label>
        <input id="display_name" placeholder="How should Kind Friend address you?" />
      </div>
      <div class="form-row">
        <label for="bio">Bio</label>
        <textarea id="bio" rows="4" placeholder="Anything you'd like KF to remember about you (non-sensitive)."></textarea>
      </div>
      <div class="form-actions" style="display:flex;gap:8px;justify-content:flex-end;">
        <button class="btn" id="cancel-profile">Cancel</button>
        <button class="btn primary" id="save-profile">Save</button>
      </div>
    </div>
  </div>

  <script>
    const root = document.documentElement;
    const savedTheme = localStorage.getItem('kf-theme'); if (savedTheme) root.setAttribute('data-theme', savedTheme);
    document.getElementById('theme').addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next); localStorage.setItem('kf-theme', next);
    });
    const chat   = document.getElementById('chat');
    const input  = document.getElementById('message');
    const send   = document.getElementById('send');
    const sessionsEl = document.getElementById('sessions');
    const largeBtn = document.getElementById('large');
    largeBtn.addEventListener('click', () => { document.body.classList.toggle('large'); });

    function md(x){
      const esc=x.replace(/[&<>]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]));
      let out=esc.replace(/(https?:\\/\\/\\S+)/g,'<a href="$1" target="_blank" rel="noopener">$1</a>');
      out=out.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>').replace(/\\*(.+?)\\*/g,'<em>$1</em>').replace(/`([^`]+?)`/g,'<code>$1</code>');
      out=out.split(/\\n\\n+/).map(p=>'<p>'+p.replace(/\\n/g,'<br/>')+'</p>').join(''); return out;
    }
    function bubble(text, who){
      const row=document.createElement('div'); row.className='row '+who;
      const av=document.createElement('div'); av.className='avatar'; av.textContent=(who==='bot')?'KF':'You';
      const b=document.createElement('div'); b.className='bubble'; b.innerHTML=md(text);
      if(who==='bot') row.appendChild(av); row.appendChild(b);
      if(who==='user') row.style.gridTemplateColumns='1fr auto';
      chat.appendChild(row); chat.scrollTop=chat.scrollHeight;
    }

    // Auth + profile
    const meSpan=document.getElementById('me'), u=document.getElementById('u'), p=document.getElementById('p');
    const signupBtn=document.getElementById('signup'), loginBtn=document.getElementById('login'), logoutBtn=document.getElementById('logout'), editProfileBtn=document.getElementById('edit-profile');
    const modalBackdrop=document.getElementById('modal-backdrop'), profileModal=document.getElementById('profile-modal');
    const closeModalBtn=document.getElementById('close-modal'), cancelProfile=document.getElementById('cancel-profile'), saveProfile=document.getElementById('save-profile');
    const displayNameEl=document.getElementById('display_name'), bioEl=document.getElementById('bio');
    function openModal(){ profileModal.classList.add('on'); modalBackdrop.classList.add('on'); }
    function closeModal(){ profileModal.classList.remove('on'); modalBackdrop.classList.remove('on'); }

    async function refreshMe(){
      const r=await fetch('/api/me'); const data=await r.json();
      if(data.user){
        meSpan.textContent=`Signed in as ${data.user.display_name||data.user.username}`;
        u.style.display='none'; p.style.display='none'; signupBtn.style.display='none'; loginBtn.style.display='none';
        logoutBtn.style.display=''; editProfileBtn.style.display='';
        displayNameEl.value=data.user.display_name||''; bioEl.value=data.user.bio||'';
      }else{
        meSpan.textContent='Not signed in';
        u.style.display=''; p.style.display=''; signupBtn.style.display=''; loginBtn.style.display='';
        logoutBtn.style.display='none'; editProfileBtn.style.display='none';
        displayNameEl.value=''; bioEl.value='';
      }
    }
    signupBtn.onclick=async()=>{ const username=u.value.trim(), password=p.value;
      if(!username||!password) return alert('Enter username & password');
      const r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
      const d=await r.json(); if(!r.ok) return alert(d.error||'Signup failed'); await refreshMe(); bubble('Account created and signed in. 👋','bot'); loadSessions();
    };
    loginBtn.onclick=async()=>{ const username=u.value.trim(), password=p.value;
      if(!username||!password) return alert('Enter username & password');
      const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
      const d=await r.json(); if(!r.ok) return alert(d.error||'Login failed'); await refreshMe(); bubble('Signed in.','bot'); loadSessions();
    };
    logoutBtn.onclick=async()=>{ await fetch('/api/logout',{method:'POST'}); await refreshMe(); bubble('Signed out.','bot'); loadSessions(); };

    editProfileBtn.onclick=()=>openModal();
    closeModalBtn.onclick=closeModal; cancelProfile.onclick=closeModal; modalBackdrop.onclick=closeModal;
    saveProfile.onclick=async()=>{ const display_name=displayNameEl.value.trim(), bio=bioEl.value.trim();
      const r=await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_name,bio})});
      const d=await r.json(); if(!r.ok) return alert(d.error||'Unable to save'); await refreshMe(); closeModal(); bubble('Profile updated.','bot');
    };

    // Sessions
    async function loadSessions(){
      const r=await fetch('/api/sessions'); const data=await r.json();
      sessionsEl.innerHTML='';
      (data.sessions||[]).forEach(s=>{
        const el=document.createElement('div'); el.className='session'+(data.active===s.id?' active':'');
        el.textContent=s.title || 'Untitled';
        el.onclick=async()=>{ await fetch('/api/session/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:s.id})}); chat.innerHTML=''; bubble('Switched to conversation.','bot'); loadHistory(); loadSessions(); };
        sessionsEl.appendChild(el);
      });
    }

    async function loadHistory(){
      const r=await fetch('/api/history'); const data=await r.json();
      chat.innerHTML=''; (data.messages||[]).forEach(m=>bubble(m.content, m.role==='assistant'?'bot':'user'));
    }

    document.getElementById('new-chat').onclick=async()=>{
      const title=prompt('Name your chat (optional):','New chat')||'New chat';
      const r=await fetch('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});
      if(r.ok){ chat.innerHTML=''; bubble('New chat created.','bot'); loadSessions(); }
    };

    // Streaming send
    async function sendMessage(){
      const msg=input.value.trim(); if(!msg) return; input.value=''; bubble(msg,'user');
      const res=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
      if(!res.ok){ bubble('Error: '+(await res.text()),'bot'); return; }
      const reader=res.body.getReader(); let buf=''; const decoder=new TextDecoder();
      while(true){ const {value, done}=await reader.read(); if(done) break; buf+=decoder.decode(value,{stream:true});
        // server sends "data: ...\n\n"
        const parts=buf.split("\\n\\n"); buf=parts.pop()||'';
        for(const part of parts){ if(!part.startsWith('data:')) continue; const chunk=part.slice(5).trim(); if(chunk==='[DONE]') continue;
          bubble(chunk,'bot'); }
      }
    }
    send.onclick=sendMessage;
    input.addEventListener('keydown',e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(); } });

    // Exports
    document.getElementById('download-txt').onclick=()=>{ window.location='/api/export?fmt=txt'; };
    document.getElementById('download-csv').onclick=()=>{ window.location='/api/export?fmt=csv'; };

    // Billing
    document.getElementById('upgrade').onclick=async()=>{
      const r=await fetch('/api/billing/checkout',{method:'POST'}); const d=await r.json();
      if(!r.ok||!d.url) return alert(d.error||'Checkout unavailable'); window.location=d.url;
    };
    document.getElementById('billing').onclick=async()=>{
      const r=await fetch('/api/billing/portal',{method:'POST'}); const d=await r.json();
      if(!r.ok||!d.url) return alert(d.error||'Portal unavailable'); window.location=d.url;
    };

    // theme + me + sessions + history
    (async()=>{ await refreshMe(); await loadSessions(); await loadHistory(); })();
  </script>
</body>
</html>"""

# =========================
# FastAPI App
# =========================
app = FastAPI()

@app.exception_handler(Exception)
async def all_exception_handler(request, exc):
    return JSONResponse({"error": "Server error", "error_detail": str(exc)}, status_code=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# =========================
# Utility
# =========================
def crisis_guard(text: str) -> Optional[str]:
    """Simple crisis safety filter. Returns a safe reply if we should block model call."""
    lowered = text.lower()
    keywords = ["suicide", "kill myself", "self-harm", "end my life", "overdose", "hurt myself"]
    if any(k in lowered for k in keywords):
        return ("I'm really glad you reached out. You deserve support.\n\n"
                "If you're in the UK, you can call **Samaritans 116 123** any time, or visit a local A&E / call **999** in an emergency.\n"
                "If you're elsewhere, please contact your local emergency services or a trusted crisis line.\n\n"
                "I'm here to keep you company, but I'm not a substitute for professional help.")
    return None

def current_time_note():
    tz_name = os.getenv("APP_TZ", "Europe/London")
    now_local = datetime.datetime.now(ZoneInfo(tz_name))
    date_str = now_local.strftime("%A %d %B %Y")
    time_str = now_local.strftime("%H:%M")
    return f"Today is {date_str} and the local time is {time_str} in {tz_name}. Answer date/time questions using this."

# =========================
# Routes
# =========================
@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(INDEX_HTML)

@app.get("/health")
async def health():
    db_exists = os.path.exists(DB_FILE)
    return JSONResponse({"ok": True, "api_available": API_AVAILABLE, "db_path": DB_FILE, "db_exists": db_exists})

# ---- Auth ----
@app.post("/api/register")
async def api_register(request: Request):
    data = await request.json()
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "")
    if not username or not password:
        return JSONResponse({"error": "Username and password required"}, status_code=400)
    if get_user_by_username(username):
        return JSONResponse({"error": "Username already taken"}, status_code=409)
    uid = create_user(username, password)
    # create a default session for new user
    create_session(uid, title="Welcome")
    resp = JSONResponse({"ok": True, "user_id": uid})
    set_auth_cookie(resp, uid)
    return resp

@app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "")
    user = get_user_by_username(username)
    if not user or not check_password(password, user["password_hash"]):
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    resp = JSONResponse({"ok": True})
    set_auth_cookie(resp, user["id"])
    return resp

@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    clear_auth_cookie(resp)
    return resp

@app.get("/api/me")
async def api_me(request: Request):
    uid = get_current_user_id(request)
    if not uid:
        return JSONResponse({"user": None})
    user = get_user_by_id(uid)
    if not user:
        resp = JSONResponse({"user": None})
        clear_auth_cookie(resp)
        return resp
    safe = {"id": user["id"], "username": user["username"], "display_name": user["display_name"], "bio": user["bio"],
            "subscription_status": user["subscription_status"]}
    return JSONResponse({"user": safe})

@app.post("/

