import os
import sys
import time
import uuid
import io
import csv
import json
import datetime
import sqlite3
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# ----------------------------
# Password hashing (bcrypt -> hashlib fallback)
# ----------------------------
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

# ----------------------------
# Config
# ----------------------------
DATA_DIR = os.getenv("DATA_DIR", "")  # e.g. /opt/data on Render (must be mounted)
DB_FILE = os.path.join(DATA_DIR, "kindfriend.db") if DATA_DIR else "kindfriend.db"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
AUTH_COOKIE = "kf_auth"
APP_TZ = os.getenv("APP_TZ", "Europe/London")

# Stripe env
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
BILLING_RETURN_URL = os.getenv("BILLING_RETURN_URL", "")

DONATION_NOTE = "Kind Friend donates 50% of all subscription fees to Samaritans (UK)."
DONATION_LINK = "https://www.samaritans.org/"

# OpenAI client (graceful if missing)
if not OPENAI_API_KEY:
    print("❌ OPENAI_API_KEY not set. Set it in environment.", file=sys.stderr)
    API_AVAILABLE = False
    client = None
else:
    from openai import OpenAI
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        API_AVAILABLE = True
    except Exception as e:
        print(f"❌ Failed to init OpenAI client: {e}", file=sys.stderr)
        API_AVAILABLE = False
        client = None

# Stripe client
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

SYSTEM_PROMPT = (
    "You are Kind Friend: a warm, respectful companion. You are not a therapist. "
    "If the user mentions self-harm or immediate danger, kindly suggest contacting UK Samaritans (116 123), "
    "NHS 111, or emergency services (999). Be concise and kind."
)

signer = URLSafeSerializer(SECRET_KEY, salt="kf-auth")

# ----------------------------
# Database
# ----------------------------
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

        # sessions
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
                id                  TEXT PRIMARY KEY,
                username            TEXT UNIQUE NOT NULL,
                password_hash       TEXT NOT NULL,
                display_name        TEXT,
                bio                 TEXT,
                created_at          REAL NOT NULL,
                stripe_customer_id  TEXT,
                subscription_status TEXT
            )
        """)

        # rate limit
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rate_limit (
                key     TEXT PRIMARY KEY,
                tokens  REAL NOT NULL,
                updated REAL NOT NULL
            )
        """)

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

def get_recent_messages(session_id: str, limit: int = 20) -> List[Dict[str, str]]:
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

def create_user(username: str, password: str) -> str:
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

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, created_at, stripe_customer_id, subscription_status FROM users WHERE username = ?",
                    (username,))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3], "bio": row[4],
            "created_at": row[5], "stripe_customer_id": row[6], "subscription_status": row[7]}

def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, created_at, stripe_customer_id, subscription_status FROM users WHERE id = ?",
                    (user_id,))
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
            cur.execute("UPDATE users SET subscription_status = ?, stripe_customer_id = ? WHERE id = ?",
                        (status, stripe_customer_id, user_id))
        else:
            cur.execute("UPDATE users SET subscription_status = ? WHERE id = ?",
                        (status, user_id))
        conn.commit()

# ----------------------------
# Rate limit (token bucket)
# ----------------------------
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "20"))

def _now() -> float:
    return time.time()

def _rate_key(user_id: Optional[str], ip: str) -> str:
    return f"user:{user_id}" if user_id else f"ip:{ip}"

def check_rate_limit(user_id: Optional[str], ip: str) -> bool:
    if RATE_LIMIT_RPM <= 0:
        return True
    max_tokens = RATE_LIMIT_RPM
    refill_per_sec = RATE_LIMIT_RPM / 60.0
    key = _rate_key(user_id, ip)
    now = _now()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT tokens, updated FROM rate_limit WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO rate_limit (key, tokens, updated) VALUES (?, ?, ?)", (key, max_tokens - 1, now))
            conn.commit()
            return True
        tokens, updated = float(row[0]), float(row[1])
        tokens = min(max_tokens, tokens + (now - updated) * refill_per_sec)
        if tokens < 1.0:
            cur.execute("UPDATE rate_limit SET tokens = ?, updated = ? WHERE key = ?", (tokens, now, key))
            conn.commit()
            return False
        tokens -= 1.0
        cur.execute("UPDATE rate_limit SET tokens = ?, updated = ? WHERE key = ?", (tokens, now, key))
        conn.commit()
        return True

# ----------------------------
# Helpers
# ----------------------------
def set_auth_cookie(resp, user_id: str):
    token = signer.dumps({"user_id": user_id, "ts": time.time()})
    resp.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="Lax", max_age=60 * 60 * 24 * 180)

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

def current_time_note() -> str:
    now_local = datetime.datetime.now(ZoneInfo(APP_TZ))
    date_str = now_local.strftime("%A %d %B %Y")
    time_str = now_local.strftime("%H:%M")
    return f"Today is {date_str} and the local time is {time_str} in {APP_TZ}. Answer date/time questions using this."

# ----------------------------
# Frontend (modern responsive UI)
# ----------------------------
INDEX_HTML = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Kind Friend</title>
<link rel="icon" href='data:image/svg+xml;utf8,
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="%237d7bff"/><stop offset="100%" stop-color="%235fe1d9"/></linearGradient></defs>
  <circle cx="32" cy="32" r="30" fill="url(%23g)"/><text x="32" y="38" font-size="24" font-family="Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial" text-anchor="middle" fill="white">KF</text>
</svg>'/>
<style>
:root{--bg:#0b1020;--bg-2:#0e142c;--text:#e8ecf7;--muted:#a9b0c5;--card:rgba(255,255,255,.08);--card2:rgba(255,255,255,.06);--border:rgba(255,255,255,.12);--brand:#7d7bff;--brand2:#5fe1d9;--radius:16px;--g:16px;--sidebar:300px;}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{min-height:100svh;color:var(--text);font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;background:linear-gradient(180deg,#0b1020 0%,#101735 100%)}
.shell{display:grid;grid-template-columns:var(--sidebar) 1fr;gap:var(--g);padding:var(--g);min-height:100svh}
.card{background:var(--card);backdrop-filter:blur(10px);border:1px solid var(--border);border-radius:var(--radius)}
.sidebar{padding:12px;display:flex;flex-direction:column;gap:10px}
.logo{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;font-weight:800}
.sessions{overflow:auto;display:flex;flex-direction:column;gap:6px;padding-right:4px}
.session{padding:8px 10px;border:1px solid var(--border);border-radius:12px;background:var(--card2);cursor:pointer}
.session.active{outline:2px solid var(--brand2)}
.btn{border:1px solid var(--border);background:var(--card2);color:var(--text);padding:8px 10px;border-radius:999px;cursor:pointer}
.btn.primary{background:linear-gradient(135deg,var(--brand),var(--brand2));border-color:transparent;color:#fff}
.chip{font-size:12px;color:var(--muted);padding:6px 10px;border-radius:999px;border:1px dashed var(--border)}
.main{display:flex;flex-direction:column;gap:var(--g)}
.header{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:12px 16px}
.auth input{padding:8px 10px;border-radius:10px;border:1px solid var(--border);background:rgba(255,255,255,.05);color:var(--text);min-width:120px}
#logout,#edit-profile{display:none}
.chat-card{display:flex;flex-direction:column;min-height:0;height:calc(100svh - 180px)}
.chat{flex:1 1 auto;min-height:0;overflow:auto;padding:16px;display:grid;gap:12px;border-top-left-radius:var(--radius);border-top-right-radius:var(--radius)}
.row{display:grid;grid-template-columns:auto 1fr;gap:10px;align-items:start}
.row.user{grid-template-columns:1fr auto}
.avatar{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:800;background:linear-gradient(135deg,var(--brand),var(--brand2))}
.row.user .avatar{display:none}
.bubble{max-width:80ch;padding:12px 14px;border-radius:14px;border:1px solid var(--border);background:rgba(255,255,255,.06);word-wrap:anywhere}
.row.user .bubble{background:rgba(125,123,255,.14)}
.row.bot .bubble{background:rgba(95,225,217,.14)}
.composer{display:grid;grid-template-columns:1fr auto;gap:10px;padding:12px;border-top:1px solid var(--border);background:var(--card2);border-bottom-left-radius:var(--radius);border-bottom-right-radius:var(--radius)}
.input{padding:14px;border-radius:12px;border:1px solid var(--border);background:rgba(255,255,255,.06);color:var(--text)}
.hint{text-align:center;color:var(--muted);font-size:12px}
.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40}
.modal{display:none;position:fixed;inset:0;z-index:50;place-items:center}
.modal.on,.modal-backdrop.on{display:grid}
.modal-card{width:min(520px,94vw);background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:18px;padding:16px}
.form-row{display:grid;gap:6px;margin:10px 0}
@media(max-width:900px){.shell{grid-template-columns:1fr}.chat-card{height:calc(100svh - 240px)}}
</style>
</head>
<body>
<div class="shell">
  <aside class="card sidebar">
    <div style="display:flex;align-items:center;gap:8px"><div class="logo">KF</div><h2 style="margin:0">Kind Friend</h2></div>
    <div style="font-size:12px;color:#a9b0c5">💚 <strong>50% donated</strong> to <a href="https://www.samaritans.org/" target="_blank">Samaritans</a>.</div>
    <div style="display:flex;gap:6px"><button id="new-chat" class="btn primary">New chat</button><button id="large" class="btn">A A</button></div>
    <div id="sessions" class="sessions"></div>
  </aside>
  <main class="main">
    <div class="card header">
      <span id="me" class="chip">Not signed in</span>
      <div id="auth" class="auth">
        <input id="u" placeholder="username"/><input id="p" placeholder="password" type="password"/>
        <button id="signup" class="btn">Sign up</button><button id="login" class="btn primary">Log in</button>
        <button id="logout" class="btn">Log out</button><button id="edit-profile" class="btn">Profile</button>
        <button id="upgrade" class="btn primary">Upgrade</button><button id="billing" class="btn">Billing</button>
      </div>
      <div style="margin-left:auto;display:flex;gap:8px"><button id="theme" class="btn">Theme</button><button id="download-txt" class="btn">.txt</button><button id="download-csv" class="btn">.csv</button></div>
    </div>
    <section class="card chat-card">
      <div id="chat" class="chat"></div>
      <div class="composer"><input id="message" class="input" placeholder="Share what's on your mind…"/><button id="send" class="btn primary">Send</button></div>
    </section>
    <div class="hint">Kind Friend is a supportive companion, not a therapist. In crisis, call 999 or Samaritans 116 123 (UK).</div>
  </main>
</div>

<div id="modal-backdrop" class="modal-backdrop"></div>
<div id="profile-modal" class="modal" role="dialog" aria-modal="true" aria-labelledby="profile-title">
  <div class="modal-card">
    <div style="display:flex;justify-content:space-between;align-items:center"><div id="profile-title" style="font-weight:800">Edit profile</div><button id="close-modal" class="btn">Close</button></div>
    <div class="form-row"><label for="display_name">Display name</label><input id="display_name" /></div>
    <div class="form-row"><label for="bio">Bio</label><textarea id="bio" rows="4"></textarea></div>
    <div style="display:flex;gap:8px;justify-content:flex-end"><button id="cancel-profile" class="btn">Cancel</button><button id="save-profile" class="btn primary">Save</button></div>
  </div>
</div>

<script>
const root=document.documentElement;const saved=localStorage.getItem('kf-theme');if(saved)root.setAttribute('data-theme',saved);
document.getElementById('theme').onclick=()=>{const next=root.getAttribute('data-theme')==='light'?'dark':'light';root.setAttribute('data-theme',next);localStorage.setItem('kf-theme',next);}
document.getElementById('large').onclick=()=>{document.body.classList.toggle('large');}

const chat=document.getElementById('chat');const input=document.getElementById('message');const send=document.getElementById('send');
const sessionsEl=document.getElementById('sessions');

function md(x){const esc=x.replace(/[&<>]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]));let out=esc.replace(/(https?:\\/\\/\\S+)/g,'<a href="$1" target="_blank">$1</a>');out=out.replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>').replace(/\\*(.+?)\\*/g,'<em>$1</em>').replace(/`([^`]+?)`/g,'<code>$1</code>');out=out.split(/\\n\\n+/).map(p=>'<p>'+p.replace(/\\n/g,'<br/>')+'</p>').join('');return out;}
function addBubble(text,who){const row=document.createElement('div');row.className='row '+who;const av=document.createElement('div');av.className='avatar';av.textContent=(who==='bot')?'KF':'You';const b=document.createElement('div');b.className='bubble';b.innerHTML=md(text);if(who==='bot')row.appendChild(av);row.appendChild(b);if(who==='user')row.style.gridTemplateColumns='1fr auto';chat.appendChild(row);chat.scrollTop=chat.scrollHeight;return b;}

async function refreshMe(){const r=await fetch('/api/me');const d=await r.json();const me=document.getElementById('me');const u=document.getElementById('u');const p=document.getElementById('p');const su=document.getElementById('signup');const li=document.getElementById('login');const lo=document.getElementById('logout');const ep=document.getElementById('edit-profile');if(d.user){me.textContent=`Signed in as ${d.user.display_name||d.user.username}`;u.style.display='none';p.style.display='none';su.style.display='none';li.style.display='none';lo.style.display='';ep.style.display='';document.getElementById('display_name').value=d.user.display_name||'';document.getElementById('bio').value=d.user.bio||'';}else{me.textContent='Not signed in';u.style.display='';p.style.display='';su.style.display='';li.style.display='';lo.style.display='none';ep.style.display='none';}}
async function loadSessions(){const r=await fetch('/api/sessions');const d=await r.json();sessionsEl.innerHTML='';(d.sessions||[]).forEach(s=>{const el=document.createElement('div');el.className='session'+(d.active===s.id?' active':'');el.textContent=s.title||'Untitled';el.onclick=async()=>{await fetch('/api/session/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:s.id})});chat.innerHTML='';await loadHistory();await loadSessions();};sessionsEl.appendChild(el);});}
async function loadHistory(){const r=await fetch('/api/history');const d=await r.json();chat.innerHTML='';(d.messages||[]).forEach(m=>addBubble(m.content, m.role==='assistant'?'bot':'user'));}

document.getElementById('signup').onclick=async()=>{const u=document.getElementById('u').value.trim();const p=document.getElementById('p').value;if(!u||!p)return alert('Enter username & password');const r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});const d=await r.json();if(!r.ok)return alert(d.error||'Signup failed');await refreshMe();await loadSessions();addBubble('Account created and signed in. 👋','bot');};
document.getElementById('login').onclick=async()=>{const u=document.getElementById('u').value.trim();const p=document.getElementById('p').value;if(!u||!p)return alert('Enter username & password');const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});const d=await r.json();if(!r.ok)return alert(d.error||'Login failed');await refreshMe();await loadSessions();addBubble('Signed in.','bot');};
document.getElementById('logout').onclick=async()=>{await fetch('/api/logout',{method:'POST'});await refreshMe();await loadSessions();addBubble('Signed out.','bot');};

const backdrop=document.getElementById('modal-backdrop');const modal=document.getElementById('profile-modal');
function openModal(){modal.classList.add('on');backdrop.classList.add('on');}
function closeModal(){modal.classList.remove('on');backdrop.classList.remove('on');}
document.getElementById('edit-profile').onclick=openModal;
document.getElementById('close-modal').onclick=closeModal;document.getElementById('cancel-profile').onclick=closeModal;backdrop.onclick=closeModal;
document.getElementById('save-profile').onclick=async()=>{const display_name=document.getElementById('display_name').value.trim();const bio=document.getElementById('bio').value.trim();const r=await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_name,bio})});const d=await r.json();if(!r.ok)return alert(d.error||'Unable to save');await refreshMe();closeModal();addBubble('Profile updated.','bot');};

document.getElementById('new-chat').onclick=async()=>{const title=prompt('Name your chat (optional):','New chat')||'New chat';const r=await fetch('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});if(r.ok){chat.innerHTML='';addBubble('New chat created.','bot');await loadSessions();}};

async function sendMessage(){const msg=input.value.trim();if(!msg)return;input.value='';addBubble(msg,'user');const res=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});if(!res.ok){addBubble('Error: '+(await res.text()),'bot');return;}const reader=res.body.getReader();const decoder=new TextDecoder();let buf='',acc='';const row=document.createElement('div');row.className='row bot';const av=document.createElement('div');av.className='avatar';av.textContent='KF';const b=document.createElement('div');b.className='bubble';row.appendChild(av);row.appendChild(b);chat.appendChild(row);while(true){const {value,done}=await reader.read();if(done)break;buf+=decoder.decode(value,{stream:true});const parts=buf.split("\\n\\n");buf=parts.pop()||'';for(const part of parts){if(!part.startsWith('data:'))continue;const chunk=part.slice(5).trim();if(chunk==='[DONE]')continue;acc+=chunk.replace(/\\\\n/g,'\\n');b.innerHTML=md(acc);chat.scrollTop=chat.scrollHeight;}}}
send.onclick=sendMessage;input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage();}});
document.getElementById('download-txt').onclick=()=>{window.location='/api/export?fmt=txt';};document.getElementById('download-csv').onclick=()=>{window.location='/api/export?fmt=csv';};

document.getElementById('upgrade').onclick=async()=>{const r=await fetch('/api/billing/checkout',{method:'POST'});const d=await r.json();if(!r.ok||!d.url)return alert(d.error||'Checkout unavailable');window.location=d.url;};
document.getElementById('billing').onclick=async()=>{const r=await fetch('/api/billing/portal',{method:'POST'});const d=await r.json();if(!r.ok||!d.url)return alert(d.error||'Portal unavailable');window.location=d.url;};

(async()=>{await refreshMe();await loadSessions();await loadHistory();})();
</script>
</body>
</html>
"""

# ----------------------------
# FastAPI app
# ----------------------------
app = FastAPI()

@app.exception_handler(Exception)
async def all_exception_handler(request, exc):
    # Keep JSON errors consistent for the frontend
    return JSONResponse({"error": "Server error", "error_detail": str(exc)}, status_code=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ----------------------------
# Routes
# ----------------------------
@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(INDEX_HTML)

@app.get("/health")
async def health():
    db_exists = os.path.exists(DB_FILE)
    return JSONResponse({"ok": True, "api_available": API_AVAILABLE, "db_path": DB_FILE, "db_exists": db_exists})

# ---------- Auth ----------
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
    # Create default session and set cookie
    sid = create_session(uid, title="Welcome")
    resp = JSONResponse({"ok": True, "user_id": uid, "session_id": sid})
    set_auth_cookie(resp, uid)
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.post("/api/login")
async def api_login(request: Request):
    data = await request.json()
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "")
    user = get_user_by_username(username)
    if not user or not check_password(password, user["password_hash"]):
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    # Ensure a session exists
    sid = request.cookies.get("session_id") or create_session(user["id"], title="New chat")
    resp = JSONResponse({"ok": True, "session_id": sid})
    set_auth_cookie(resp, user["id"])
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    clear_auth_cookie(resp)
    resp.delete_cookie("session_id")
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

@app.post("/api/profile")
async def api_profile(request: Request):
    uid = get_current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    data = await request.json()
    update_user_profile(uid, data.get("display_name"), data.get("bio"))
    return JSONResponse({"ok": True})

# ---------- Sessions & history ----------
def _get_active_session(request: Request, uid: Optional[str]) -> str:
    sid = request.cookies.get("session_id")
    if sid:
        return sid
    sid = create_session(uid, title="New chat")
    return sid

@app.get("/api/sessions")
async def api_sessions(request: Request):
    uid = get_current_user_id(request)
    sessions = list_sessions(uid)
    active = request.cookies.get("session_id")
    return JSONResponse({"sessions": sessions, "active": active})

@app.post("/api/sessions")
async def api_sessions_create(request: Request):
    uid = get_current_user_id(request)
    data = await request.json()
    title = (data.get("title") or "New chat").strip() or "New chat"
    sid = create_session(uid, title)
    resp = JSONResponse({"ok": True, "session_id": sid})
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.post("/api/session/select")
async def api_session_select(request: Request):
    data = await request.json()
    sid = data.get("session_id")
    if not sid:
        return JSONResponse({"error": "session_id required"}, status_code=400)
    # (Optional) verify ownership here
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.get("/api/history")
async def api_history(request: Request):
    uid = get_current_user_id(request)
    sid = _get_active_session(request, uid)
    msgs = get_all_messages(sid)
    out = [{"role": r, "content": c, "ts": ts} for (r, c, ts, _arch) in msgs]
    return JSONResponse({"messages": out, "session_id": sid})

# ---------- Safety guard ----------
def crisis_guard(text: str) -> Optional[str]:
    lowered = text.lower()
    keywords = ["suicide", "kill myself", "self-harm", "end my life", "overdose", "hurt myself"]
    if any(k in lowered for k in keywords):
        return ("I'm really glad you reached out. You deserve support.\n\n"
                "If you're in the UK, you can call **Samaritans 116 123** any time, or visit A&E / call **999** in an emergency.\n"
                "If you're elsewhere, please contact local emergency services or a trusted crisis line.\n\n"
                "I'm here to keep you company, but I'm not a substitute for professional help.")
    return None

# ---------- Chat (streaming SSE) ----------
@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    if not API_AVAILABLE:
        return JSONResponse({"error": "Service is not configured with an API key."}, status_code=500)

    uid = get_current_user_id(request)
    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(uid, ip):
        return JSONResponse({"error": "Rate limit exceeded. Please wait a moment."}, status_code=429)

    data = await request.json()
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    sid = request.cookies.get("session_id") or create_session(uid, title="New chat")

    # crisis guard
    guard = crisis_guard(user_message)
    if guard:
        save_message(sid, "user", user_message, uid)
        save_message(sid, "assistant", guard, uid)

        def gen_safe():
            yield "data: " + guard.replace("\n", "\\n") + "\n\n"
            yield "data: [DONE]\n\n"
        resp = StreamingResponse(gen_safe(), media_type="text/event-stream")
        resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
        return resp

    save_message(sid, "user", user_message, uid)

    tz_note = current_time_note()
    history = [{"role": "system", "content": SYSTEM_PROMPT},
               {"role": "system", "content": tz_note}]
    history.extend(get_recent_messages(sid, limit=20))
    history.append({"role": "user", "content": user_message})

    def event_stream():
        try:
            stream = client.chat.completions.create(
                model=MODEL_NAME, messages=history, temperature=0.7, stream=True
            )
            chunks: List[str] = []
            for chunk in stream:
                delta = None
                try:
                    delta = chunk.choices[0].delta.content
                except Exception:
                    try:
                        delta = chunk.choices[0].message.content
                    except Exception:
                        delta = None
                if not delta:
                    continue
                chunks.append(delta)
                yield "data: " + delta.replace("\n", "\\n") + "\n\n"
            final = "".join(chunks)
            save_message(sid, "assistant", final, uid)
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield "data: " + ("[Error] " + str(e)).replace("\n", "\\n") + "\n\n"
            yield "data: [DONE]\n\n"

    resp = StreamingResponse(event_stream(), media_type="text/event-stream")
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

# ---------- Optional non-stream endpoint ----------
@app.post("/api/chat")
async def api_chat(request: Request):
    if not API_AVAILABLE:
        return JSONResponse({"error": "Service is not configured with an API key."}, status_code=500)

    uid = get_current_user_id(request)
    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(uid, ip):
        return JSONResponse({"error": "Rate limit exceeded. Please wait a moment."}, status_code=429)

    data = await request.json()
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    sid = request.cookies.get("session_id") or create_session(uid, title="New chat")
    guard = crisis_guard(user_message)
    if guard:
        save_message(sid, "user", user_message, uid)
        save_message(sid, "assistant", guard, uid)
        resp = JSONResponse({"reply": guard})
        resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
        return resp

    save_message(sid, "user", user_message, uid)
    tz_note = current_time_note()
    history = [{"role": "system", "content": SYSTEM_PROMPT},
               {"role": "system", "content": tz_note}]
    history.extend(get_recent_messages(sid, limit=20))

    try:
        r = client.chat.completions.create(model=MODEL_NAME, messages=history, temperature=0.7)
        reply = r.choices[0].message.content
    except Exception as e:
        return JSONResponse({"error": "OpenAI error", "error_detail": str(e)}, status_code=502)

    save_message(sid, "assistant", reply, uid)
    resp = JSONResponse({"reply": reply})
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

# ---------- Export (.txt / .csv) ----------
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

# ---------- Billing (Stripe) ----------
def _stripe_ready() -> Optional[str]:
    if not (stripe and STRIPE_SECRET_KEY and STRIPE_PRICE_ID and BILLING_RETURN_URL):
        return "Stripe is not configured. Set STRIPE_SECRET_KEY, STRIPE_PRICE_ID, BILLING_RETURN_URL."
    return None

def _get_or_create_customer(user: Dict[str, Any]) -> str:
    if user.get("stripe_customer_id"):
        return user["stripe_customer_id"]
    cust = stripe.Customer.create(email=f"{user['username']}@example.local", metadata={"kf_user_id": user["id"]})
    upsert_user_subscription(user["id"], user.get("subscription_status") or "inactive", cust["id"])
    return cust["id"]

@app.post("/api/billing/checkout")
async def api_billing_checkout(request: Request):
    err = _stripe_ready()
    if err:
        return JSONResponse({"error": err}, status_code=400)
    uid = get_current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Sign in required"}, status_code=401)
    user = get_user_by_id(uid)
    if not user:
        return JSONResponse({"error": "User missing"}, status_code=400)
    customer_id = _get_or_create_customer(user)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=BILLING_RETURN_URL + "?billing=success",
        cancel_url=BILLING_RETURN_URL + "?billing=cancel",
        metadata={"kf_user_id": uid},
    )
    return JSONResponse({"url": session.url})

@app.post("/api/billing/portal")
async def api_billing_portal(request: Request):
    err = _stripe_ready()
    if err:
        return JSONResponse({"error": err}, status_code=400)
    uid = get_current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Sign in required"}, status_code=401)
    user = get_user_by_id(uid)
    if not user or not user.get("stripe_customer_id"):
        return JSONResponse({"error": "No Stripe customer yet. Try Upgrade first."}, status_code=400)
    session = stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"],
        return_url=BILLING_RETURN_URL or "/",
    )
    return JSONResponse({"url": session.url})

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not stripe:
        return PlainTextResponse("stripe not configured", status_code=400)
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
    except Exception as e:
        return PlainTextResponse(f"Invalid payload: {e}", status_code=400)

    t = event.get("type")
    data = event.get("data", {}).get("object", {})
    customer_id = data.get("customer")
    status = None

    if t == "checkout.session.completed":
        status = "active"
    elif t == "customer.subscription.updated":
        status = data.get("status")
    elif t == "customer.subscription.deleted":
        status = "canceled"

    if customer_id and status:
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE stripe_customer_id = ?", (customer_id,))
            row = cur.fetchone()
        if row:
            upsert_user_subscription(row[0], status, customer_id)

    return PlainTextResponse("ok", status_code=200)

