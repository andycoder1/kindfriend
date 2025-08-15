import os
import sys
import time
import uuid
import io
import csv
import datetime
import sqlite3
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
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

# =========================
# Config & Persistent Paths
# =========================
DATA_DIR = os.getenv("DATA_DIR", "")  # e.g. /opt/data on Render
DB_FILE = os.path.join(DATA_DIR, "kindfriend.db") if DATA_DIR else "kindfriend.db"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")  # set in production
AUTH_COOKIE = "kf_auth"

# Graceful API key check (don’t crash container if missing)
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
        # users
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name  TEXT,
                bio           TEXT,
                created_at    REAL NOT NULL
            )
        """)
        # add user_id if missing (migrate older DBs)
        try:
            cur.execute("SELECT user_id FROM messages LIMIT 1")
        except sqlite3.OperationalError:
            cur.execute("ALTER TABLE messages ADD COLUMN user_id TEXT")
        conn.commit()

def save_message(session_id: str, role: str, content: str, user_id: str | None):
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

def delete_session(session_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.commit()

def create_user(username: str, password: str):
    uid = str(uuid.uuid4())
    pw_hash = hash_password(password)
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (id, username, password_hash, display_name, bio, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, username, pw_hash, username, "", time.time()),
        )
        conn.commit()
    return uid

def get_user_by_username(username: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, created_at FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3], "bio": row[4], "created_at": row[5]}

def get_user_by_id(user_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, created_at FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3], "bio": row[4], "created_at": row[5]}

def update_user_profile(user_id: str, display_name: str | None, bio: str | None):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        if display_name is not None and bio is not None:
            cur.execute("UPDATE users SET display_name = ?, bio = ? WHERE id = ?", (display_name, bio, user_id))
        elif display_name is not None:
            cur.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))
        elif bio is not None:
            cur.execute("UPDATE users SET bio = ? WHERE id = ?", (bio, user_id))
        conn.commit()

init_db()

# =========================
# Auth helpers
# =========================
def set_auth_cookie(resp, user_id: str):
    token = signer.dumps({"user_id": user_id, "ts": time.time()})
    resp.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="Lax", max_age=60*60*24*180)

def clear_auth_cookie(resp):
    resp.delete_cookie(AUTH_COOKIE)

def get_current_user_id(request: Request) -> str | None:
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return None
    try:
        data = signer.loads(token)
        return data.get("user_id")
    except BadSignature:
        return None

# =========================
# Frontend (Contemporary UI + Profile Modal)
# =========================
INDEX_HTML = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kind Friend</title>

  <!-- Brand favicon (inline) -->
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
</svg>?v=10'>

  <style>
    :root {
      --bg: #0b1020; --bg-accent: #101735; --bg-2: #0e142c;
      --text: #e8ecf7; --muted: #a9b0c5;
      --card: rgba(255,255,255,0.08); --card-2: rgba(255,255,255,0.06);
      --border: rgba(255,255,255,0.12);
      --brand: #7d7bff; --brand-2: #5fe1d9; --danger: #ef4444;
      --shadow: 0 20px 60px rgba(0,0,0,0.35);
      --radius: 16px;
    }
    [data-theme="light"] {
      --bg: #f3f6ff; --bg-accent: #eaf0ff; --bg-2: #ffffff;
      --text: #0b1020; --muted: #465170;
      --card: rgba(255,255,255,0.9); --card-2: rgba(255,255,255,0.85);
      --border: rgba(0,15,40,0.12);
      --shadow: 0 20px 60px rgba(0,0,0,0.10);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      color: var(--text);
      font: 15px/1.5 Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      background:
        radial-gradient(1200px 600px at -10% -20%, #22264a 0%, transparent 60%),
        radial-gradient(1200px 600px at 110% 120%, #113a3a 0%, transparent 60%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg-accent) 100%);
      display: grid; place-items: center; padding: 18px;
    }
    .app { width: min(980px, 100%); display: grid; grid-template-rows: auto 1fr auto; gap: 14px; }
    .card { background: var(--card); backdrop-filter: blur(12px); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); }
    .header { display: flex; align-items: center; gap: 14px; padding: 14px 16px; }
    .logo { width: 44px; height: 44px; border-radius: 14px; display: grid; place-items: center;
            background: linear-gradient(135deg, var(--brand), var(--brand-2)); color: white; font-weight: 800; letter-spacing: 0.3px; }
    .title-wrap { flex: 1; min-width: 0; }
    .title { margin: 0; font-size: 18px; font-weight: 800; letter-spacing: .2px; }
    .subtitle { margin: 3px 0 0; font-size: 12px; color: var(--muted); }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: flex-end; }
    .btn { border: 1px solid var(--border); background: var(--card-2); color: var(--text);
           padding: 8px 12px; border-radius: 999px; cursor: pointer; font-weight: 600; transition: transform .08s ease, background .2s ease; }
    .btn:hover { transform: translateY(-1px); }
    .btn.primary { background: linear-gradient(135deg, var(--brand), var(--brand-2)); border-color: transparent; color: white; }
    .chip { font-size: 12px; color: var(--muted); padding: 6px 10px; border-radius: 999px; border: 1px dashed var(--border); }
    .auth { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
    .auth input { padding: 8px 10px; border-radius: 10px; border: 1px solid var(--border);
                  background: rgba(255,255,255,0.05); color: var(--text); min-width: 120px; }
    #logout { display:none; }
    .chat-wrap { position: relative; }
    .chat {
      padding: 16px; display: grid; gap: 12px; overflow: auto; height: min(60vh, 560px);
      background:
        radial-gradient(800px 400px at 20% 0%, rgba(125,123,255,0.10) 0%, transparent 60%),
        radial-gradient(800px 400px at 80% 100%, rgba(95,225,217,0.10) 0%, transparent 60%);
      border-top-left-radius: var(--radius); border-top-right-radius: var(--radius);
    }
    .row { display: grid; grid-template-columns: auto 1fr; gap: 10px; align-items: start; animation: pop .18s ease; }
    .row.user { grid-template-columns: 1fr auto; }
    @keyframes pop { from { transform: translateY(6px); opacity: 0 } to { transform: translateY(0); opacity: 1 } }
    .avatar { width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center; font-size: 13px; font-weight: 800; color: white;
              background: linear-gradient(135deg, var(--brand), var(--brand-2)); box-shadow: 0 6px 16px rgba(0,0,0,.18); }
    .row.user .avatar { display: none; }
    .bubble { max-width: 76ch; padding: 12px 14px; border-radius: 14px; border: 1px solid var(--border); background: rgba(255,255,255,0.06); }
    .row.user .bubble { background: rgba(125,123,255,0.14); }
    .row.bot .bubble  { background: rgba(95,225,217,0.14); }
    .bubble p { margin: 0 0 .6em; } .bubble p:last-child { margin-bottom: 0; }
    .bubble strong { font-weight: 800; } .bubble em { font-style: italic; opacity: .95; }
    .bubble code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.92em; padding: 2px 6px; border-radius: 8px; background: rgba(0,0,0,.25); }
    .bubble a { color: var(--brand-2); text-decoration: none; border-bottom: 1px dashed var(--brand-2); }
    .bubble ul { margin: .6em 0 .6em 1.2em; padding: 0; } .bubble li { margin: .2em 0; }
    .typing { display: none; padding: 8px 16px 14px; color: var(--muted); font-size: 13px; }
    .typing.on::after { content: " •••"; animation: blink 1.2s infinite steps(1); }
    @keyframes blink { 50% { opacity: .4 } }
    .composer { display: grid; grid-template-columns: 1fr auto; gap: 10px; padding: 12px; border-top: 1px solid var(--border); background: var(--card-2);
                border-bottom-left-radius: var(--radius); border-bottom-right-radius: var(--radius); }
    .input { padding: 14px 14px; border-radius: 12px; border: 1px solid var(--border); color: var(--text); background: rgba(255,255,255,0.06); }
    .send { display: inline-flex; align-items: center; gap: 8px; }
    .hint { margin-top: 4px; color: var(--muted); font-size: 12px; text-align: center; }

    /* Profile Modal */
    .modal-backdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:40; }
    .modal { display:none; position:fixed; inset:0; z-index:50; place-items:center; }
    .modal.on, .modal-backdrop.on { display:grid; }
    .modal-card { width:min(520px, 94vw); background:var(--bg-2); color:var(--text); border:1px solid var(--border); border-radius:18px; box-shadow:var(--shadow); padding:16px; }
    .modal-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
    .modal-title { font-weight:800; font-size:16px; }
    .xbtn { border:1px solid var(--border); background:var(--card-2); color:var(--text); border-radius:10px; padding:6px 10px; cursor:pointer; }
    .form-row { display:grid; gap:6px; margin:10px 0; }
    .form-row input, .form-row textarea { padding:10px 12px; border-radius:12px; border:1px solid var(--border); background:rgba(255,255,255,0.06); color:var(--text); }
    .form-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:12px; }
  </style>
</head>
<body>
  <div class="app">
    <div class="card header">
      <div class="logo">KF</div>
      <div class="title-wrap">
        <h1 class="title">Kind Friend</h1>
        <p class="subtitle">A calm, privacy-first companion. Your chats stay on your server.</p>
      </div>
      <div class="toolbar">
        <span class="chip" id="me">Not signed in</span>
        <div class="auth" id="auth">
          <input id="u" placeholder="username" />
          <input id="p" placeholder="password" type="password" />
          <button id="signup" class="btn">Sign up</button>
          <button id="login"  class="btn primary">Log in</button>
          <button id="logout" class="btn">Log out</button>
          <button id="edit-profile" class="btn" style="display:none;">Profile</button>
        </div>
        <button id="theme" class="btn" title="Toggle theme">Theme</button>
        <button id="download-txt" class="btn" title="Download .txt">.txt</button>
        <button id="download-csv" class="btn" title="Download .csv">.csv</button>
        <button id="new-chat" class="btn primary" title="Start new chat">New chat</button>
      </div>
    </div>

    <div class="card chat-wrap">
      <div class="chat" id="chat"></div>
      <div id="typing" class="typing">Kind Friend is typing</div>
      <div class="composer">
        <input id="message" class="input" autocomplete="off" placeholder="Share what's on your mind…" />
        <button id="send" class="btn primary send" aria-label="Send">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M3 11l18-8-8 18-2-7-8-3z" stroke="white" stroke-width="2" fill="none" stroke-linejoin="round"/>
          </svg>
          Send
        </button>
      </div>
    </div>

    <div class="hint">Kind Friend is a supportive companion, not a therapist. In crisis, call 999 or Samaritans 116 123 (UK).</div>
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
      <div class="form-actions">
        <button class="btn" id="cancel-profile">Cancel</button>
        <button class="btn primary" id="save-profile">Save</button>
      </div>
    </div>
  </div>

  <script>
    /* Theme */
    const root   = document.documentElement;
    const saved  = localStorage.getItem('kf-theme');
    if (saved) root.setAttribute('data-theme', saved);
    const themeBtn = document.getElementById('theme');
    themeBtn.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      localStorage.setItem('kf-theme', next);
    });

    /* Elements */
    const chat   = document.getElementById('chat');
    const input  = document.getElementById('message');
    const send   = document.getElementById('send');
    const typing = document.getElementById('typing');
    const newBtn = document.getElementById('new-chat');
    const dlTxt  = document.getElementById('download-txt');
    const dlCsv  = document.getElementById('download-csv');

    const meSpan = document.getElementById('me');
    const u = document.getElementById('u');
    const p = document.getElementById('p');
    const signupBtn = document.getElementById('signup');
    const loginBtn  = document.getElementById('login');
    const logoutBtn = document.getElementById('logout');
    const editProfileBtn = document.getElementById('edit-profile');

    // Profile modal
    const modalBackdrop = document.getElementById('modal-backdrop');
    const profileModal  = document.getElementById('profile-modal');
    const closeModalBtn = document.getElementById('close-modal');
    const cancelProfile = document.getElementById('cancel-profile');
    const saveProfile   = document.getElementById('save-profile');
    const displayNameEl = document.getElementById('display_name');
    const bioEl         = document.getElementById('bio');

    const setTyping = (on) => typing.classList.toggle('on', on);

    // Tiny Markdown renderer
    function renderMarkdown(text) {
      const esc = text.replace(/[&<>]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]));
      let out = esc.replace(/(https?:\\/\\/\\S+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
      out = out.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
      out = out.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
      out = out.replace(/`([^`]+?)`/g, '<code>$1</code>');
      out = out.split(/\\n\\n+/).map(p => {
        if (/^\\s*[-*]\\s+/.test(p)) {
          const items = p.split(/\\n/).map(li => li.replace(/^\\s*[-*]\\s+/, '').trim()).filter(Boolean);
          return '<ul>' + items.map(i => '<li>' + i + '</li>').join('') + '</ul>';
        }
        return '<p>' + p.replace(/\\n/g, '<br/>') + '</p>';
      }).join('');
      return out;
    }

    function addBubble(text, who) {
      const row = document.createElement('div');
      row.className = 'row ' + who;
      const avatar = document.createElement('div');
      avatar.className = 'avatar';
      avatar.textContent = (who === 'bot') ? 'KF' : 'You';
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      bubble.innerHTML = renderMarkdown(text);
      if (who === 'bot') row.appendChild(avatar);
      row.appendChild(bubble);
      if (who === 'user') row.style.gridTemplateColumns = '1fr auto';
      chat.appendChild(row);
      chat.scrollTop = chat.scrollHeight;
    }

    async function refreshMe() {
      try {
        const r = await fetch('/api/me');
        const data = await r.json();
        if (data.user) {
          meSpan.textContent = `Signed in as ${data.user.display_name || data.user.username}`;
          u.style.display = 'none'; p.style.display = 'none';
          signupBtn.style.display = 'none'; loginBtn.style.display = 'none';
          logoutBtn.style.display = ''; editProfileBtn.style.display = '';
          // prefill modal fields
          displayNameEl.value = data.user.display_name || '';
          bioEl.value = data.user.bio || '';
        } else {
          meSpan.textContent = 'Not signed in';
          u.style.display = ''; p.style.display = '';
          signupBtn.style.display = ''; loginBtn.style.display = '';
          logoutBtn.style.display = 'none'; editProfileBtn.style.display = 'none';
          displayNameEl.value = ''; bioEl.value = '';
        }
      } catch {}
    }

    async function doSignup() {
      const username = (u.value || '').trim();
      const password = p.value || '';
      if (!username || !password) { alert('Enter username and password'); return; }
      const r = await fetch('/api/register', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username, password})});
      const data = await r.json(); if (!r.ok) { alert(data.error || 'Signup failed'); return; }
      await refreshMe(); addBubble('Account created and signed in. 👋', 'bot');
    }
    async function doLogin() {
      const username = (u.value || '').trim();
      const password = p.value || '';
      if (!username || !password) { alert('Enter username and password'); return; }
      const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username, password})});
      const data = await r.json(); if (!r.ok) { alert(data.error || 'Login failed'); return; }
      await refreshMe(); addBubble('Signed in.', 'bot');
    }
    async function doLogout() {
      await fetch('/api/logout', {method:'POST'}); await refreshMe(); addBubble('Signed out.', 'bot');
    }

    function openModal() { profileModal.classList.add('on'); modalBackdrop.classList.add('on'); }
    function closeModal() { profileModal.classList.remove('on'); modalBackdrop.classList.remove('on'); }

    signupBtn.addEventListener('click', doSignup);
    loginBtn.addEventListener('click', doLogin);
    logoutBtn.addEventListener('click', doLogout);
    editProfileBtn.addEventListener('click', () => { openModal(); });

    document.getElementById('close-modal').addEventListener('click', closeModal);
    document.getElementById('cancel-profile').addEventListener('click', closeModal);
    modalBackdrop.addEventListener('click', closeModal);

    document.getElementById('save-profile').addEventListener('click', async () => {
      const display_name = displayNameEl.value.trim();
      const bio = bioEl.value.trim();
      try {
        const r = await fetch('/api/profile', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ display_name, bio })
        });
        const data = await r.json();
        if (!r.ok) { alert(data.error || 'Unable to save'); return; }
        await refreshMe();
        closeModal();
        addBubble('Profile updated.', 'bot');
      } catch (e) {
        alert('Network error: ' + e.message);
      }
    });

    async function sendMessage() {
      const msg = input.value.trim();
      if (!msg) return;
      addBubble(msg, 'user');
      input.value = '';
      setTyping(true);
      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: msg })
        });
        const ct = res.headers.get('content-type') || '';
        if (ct.includes('application/json')) {
          const data = await res.json();
          if (data.error) addBubble('Error: ' + (data.error_detail || data.error), 'bot');
          else addBubble(data.reply, 'bot');
        } else {
          addBubble('Server error: ' + (await res.text()), 'bot');
        }
      } catch (err) {
        addBubble('Network error: ' + err.message, 'bot');
      } finally {
        setTyping(false);
      }
    }

    send.addEventListener('click', sendMessage);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });

    newBtn.addEventListener('click', async () => {
      try {
        const res = await fetch('/api/new', { method: 'POST' });
        const ok  = (res.headers.get('content-type') || '').includes('application/json') && (await res.json()).ok;
        chat.innerHTML = '';
        addBubble(ok ? 'New chat started.' : 'Error starting new chat.', 'bot');
      } catch (e) { addBubble('Network error: ' + e.message, 'bot'); }
    });

    dlTxt.addEventListener('click', () => { window.location = '/api/export?fmt=txt'; });
    dlCsv.addEventListener('click', () => { window.location = '/api/export?fmt=csv'; });

    refreshMe();
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
# Routes
# =========================
@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(INDEX_HTML)

@app.get("/health")
async def health():
    db_exists = os.path.exists(DB_FILE)
    return JSONResponse({"ok": True, "api_available": API_AVAILABLE, "db_path": DB_FILE, "db_exists": db_exists})

# ---- Auth API ----
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
    resp = JSONResponse({"ok": True, "user_id": uid})
    set_auth_cookie(resp, uid)
    return resp

@app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "")
    user = get_user_by_username(username)
    if not user:
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    if not check_password(password, user["password_hash"]):
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
    safe = {"id": user["id"], "username": user["username"], "display_name": user["display_name"], "bio": user["bio"]}
    return JSONResponse({"user": safe})

@app.post("/api/profile")
async def api_profile(request: Request):
    uid = get_current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    display_name = data.get("display_name")
    bio = data.get("bio")
    update_user_profile(uid, display_name, bio)
    return JSONResponse({"ok": True})

# ---- Chat API ----
@app.post("/api/chat")
async def api_chat(request: Request):
    if not API_AVAILABLE:
        return JSONResponse({"error": "Service is not configured with an API key."}, status_code=500)

    data = await request.json()
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    uid = get_current_user_id(request)
    session_id = request.cookies.get("session_id") or str(uuid.uuid4())
    save_message(session_id, "user", user_message, uid)

    # Live date/time context (Europe/London default; override with APP_TZ)
    tz_name = os.getenv("APP_TZ", "Europe/London")
    now_local = datetime.datetime.now(ZoneInfo(tz_name))
    date_str = now_local.strftime("%A %d %B %Y")
    time_str = now_local.strftime("%H:%M")
    time_note = f"Today is {date_str} and the local time is {time_str} in {tz_name}. Answer date/time questions using this."

    history = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": time_note},
    ]
    history.extend(get_recent_messages(session_id, limit=20))

    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=history,
            temperature=0.7,
        )
        reply = resp.choices[0].message.content
    except Exception as e:
        return JSONResponse({"error": "OpenAI error", "error_detail": str(e)}, status_code=502)

    save_message(session_id, "assistant", reply, uid)

    out = JSONResponse({"reply": reply})
    out.set_cookie("session_id", session_id, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return out

@app.post("/api/new")
async def api_new(request: Request):
    old_session = request.cookies.get("session_id")
    if old_session:
        delete_session(old_session)
    new_session = str(uuid.uuid4())
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session_id", new_session, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.get("/api/export")
async def api_export(request: Request, fmt: str = Query("txt", pattern="^(txt|csv)$")):
    session_id = request.cookies.get("session_id")
    if not session_id:
        return JSONResponse({"error": "No session"}, status_code=400)

    msgs = get_all_messages(session_id)
    now = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")

    if fmt == "txt":
        lines = []
        for role, content, ts, archived in msgs:
            t = datetime.datetime.utcfromtimestamp(ts).isoformat() + "Z"
            who = "User" if role == "user" else ("Assistant" if role == "assistant" else role)
            tag = " (archived)" if archived else ""
            lines.append(f"[{t}] {who}{tag}: {content}")
        text = "\n".join(lines) + "\n"
        return PlainTextResponse(
            text,
            headers={
                "Content-Disposition": f"attachment; filename=kindfriend_{now}.txt",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )

    # CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["time_utc", "role", "content", "archived"])
    for role, content, ts, archived in msgs:
        t = datetime.datetime.utcfromtimestamp(ts).isoformat() + "Z"
        writer.writerow([t, role, content, archived])
    csv_data = output.getvalue()
    return PlainTextResponse(
        csv_data,
        headers={
            "Content-Disposition": f"attachment; filename=kindfriend_{now}.csv",
            "Content-Type": "text/csv; charset=utf-8",
        },
    )

