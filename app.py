import os, sys, time, uuid, io, csv, json, datetime, sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ---------- Password hashing ----------
try:
    import bcrypt
    def hash_password(p: str) -> str:
        return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
    def check_password(p: str, h: str) -> bool:
        return bcrypt.checkpw(p.encode(), h.encode())
except Exception:
    import hashlib
    print("⚠ bcrypt not found — using SHA256 fallback (dev only).", file=sys.stderr)
    def hash_password(p: str) -> str:
        return hashlib.sha256(p.encode()).hexdigest()
    def check_password(p: str, h: str) -> bool:
        return hashlib.sha256(p.encode()).hexdigest() == h

from itsdangerous import URLSafeSerializer, BadSignature

# ---------- OpenAI ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
if not OPENAI_API_KEY:
    print("❌ OPENAI_API_KEY not set.", file=sys.stderr)
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

# ---------- App config ----------
DATA_DIR = os.getenv("DATA_DIR", "")
DB_FILE = os.path.join(DATA_DIR, "kindfriend.db") if DATA_DIR else "kindfriend.db"
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
AUTH_COOKIE = "kf_auth"

# ---------- System prompt ----------
SYSTEM_PROMPT = (
    "You are Kind Friend: a warm, respectful companion whose display name is set by the user. "
    "You are not a therapist. If the user mentions self-harm or danger, suggest UK Samaritans (116 123), "
    "NHS 111, or 999 in an emergency. Consider local time and recent context (e.g., acknowledge late hours). "
    "Be kind, concise, and supportive."
)

# ---------- DB init ----------
def init_db():
    if DATA_DIR:
        os.makedirs(DATA_DIR, exist_ok=True)
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            created_at REAL NOT NULL
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT,
            created_at REAL NOT NULL
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL
        )""")
        conn.commit()
init_db()

# ---------- DB helpers ----------
def create_user(username: str, password: str):
    uid = str(uuid.uuid4())
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (id, username, password_hash, display_name, created_at) VALUES (?,?,?,?,?)",
            (uid, username, hash_password(password), username, time.time()),
        )
        conn.commit()
    return uid

def get_user_by_username(username: str):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT id, username, password_hash, display_name FROM users WHERE username=?", (username,))
        r = c.fetchone()
    if not r: return None
    return {"id": r[0], "username": r[1], "password_hash": r[2], "display_name": r[3]}

def get_user_by_id(uid: str):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT id, username, password_hash, display_name FROM users WHERE id=?", (uid,))
        r = c.fetchone()
    if not r: return None
    return {"id": r[0], "username": r[1], "password_hash": r[2], "display_name": r[3]}

def create_session(user_id: Optional[str], title: str = "New chat") -> str:
    sid = str(uuid.uuid4())
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO sessions (id, user_id, title, created_at) VALUES (?,?,?,?)",
                  (sid, user_id, title, time.time()))
        conn.commit()
    return sid

def list_sessions(user_id: Optional[str]):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        if user_id:
            c.execute("SELECT id, title, created_at FROM sessions WHERE user_id=? ORDER BY created_at DESC", (user_id,))
        else:
            c.execute("SELECT id, title, created_at FROM sessions WHERE user_id IS NULL ORDER BY created_at DESC")
        rows = c.fetchall()
    return [{"id": i, "title": t, "created_at": ts} for (i, t, ts) in rows]

def save_message(session_id: str, role: str, content: str):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO messages (session_id, role, content, ts) VALUES (?,?,?,?)",
                  (session_id, role, content, time.time()))
        conn.commit()

def get_recent_messages(session_id: str, limit: int = 20):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT role, content FROM messages WHERE session_id=? ORDER BY ts DESC LIMIT ?", (session_id, limit))
        rows = c.fetchall()
    rows.reverse()
    return [{"role": r, "content": c} for (r, c) in rows]

def get_all_messages(session_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT role, content, ts FROM messages WHERE session_id=? ORDER BY ts ASC", (session_id,))
        return c.fetchall()

# ---------- Auth cookie ----------
signer = URLSafeSerializer(SECRET_KEY, salt="kf-auth")

def set_auth_cookie(resp: Response, user_id: str):
    token = signer.dumps({"user_id": user_id, "ts": time.time()})
    resp.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="Lax", max_age=60*60*24*180)

def clear_auth_cookie(resp: Response):
    resp.delete_cookie(AUTH_COOKIE)

def get_current_user_id(request: Request) -> Optional[str]:
    token = request.cookies.get(AUTH_COOKIE)
    if not token: return None
    try:
        data = signer.loads(token)
        return data.get("user_id")
    except BadSignature:
        return None

# ---------- HTML ----------
LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Kind Friend — A kinder AI companion</title>
  <link rel="icon" href="/static/favicon.ico">
  <style>
    body { margin:0; font-family: 'Segoe UI', system-ui, -apple-system, Roboto, Helvetica, Arial; background:#f4fdf6; color:#333; text-align:center; }
    header { background:#25d366; color:#fff; padding:2rem 1rem; }
    header h1 { margin:0; font-size:2.4rem; }
    header p { margin:.5rem 0 0; font-size:1.15rem; }
    .hero img { max-width:820px; width:92%; height:auto; margin:2rem auto; border-radius:16px; box-shadow:0 4px 12px rgba(0,0,0,0.1); }
    .features { display:flex; flex-wrap:wrap; justify-content:center; margin:1rem auto 2rem; gap:1rem; max-width:1100px; }
    .feature { flex:1 1 260px; background:#fff; border-radius:16px; padding:1.2rem; box-shadow:0 4px 8px rgba(0,0,0,0.05); }
    .feature img { width:48px; height:48px; margin-bottom: .6rem; }
    .cta { margin:2rem 0 3rem; display:flex; gap:.8rem; justify-content:center; flex-wrap:wrap; }
    .btn { background:#25d366; border:none; color:#fff; padding:1rem 1.6rem; border-radius:999px; cursor:pointer; font-weight:700; }
    .btn.alt { background:#fff; color:#0b1b21; border:1px solid #d1d7db; }
    footer { margin: 2rem 0; color:#666; font-size:.95rem; }
    footer img { height:24px; vertical-align:middle; margin-left:8px; }
  </style>
</head>
<body>
<header>
  <h1>Kind Friend</h1>
  <p>Your friendly chat companion, always here to listen</p>
</header>

<div class="hero">
  <img src="/static/coffee_chat.jpg" alt="People chatting over coffee">
</div>

<section class="features">
  <div class="feature">
    <img src="/static/icon_chat.svg" alt="Chat bubble">
    <h3>Natural Conversations</h3>
    <p>A familiar, friendly chat interface that feels easy and welcoming.</p>
  </div>
  <div class="feature">
    <img src="/static/icon_lock.svg" alt="Privacy">
    <h3>Private & Secure</h3>
    <p>You control what’s remembered. View and delete memories any time.</p>
  </div>
  <div class="feature">
    <img src="/static/icon_heart.svg" alt="Support">
    <h3>Supporting Good Causes</h3>
    <p>We donate 50% of all subscription fees directly to Samaritans.</p>
  </div>
</section>

<div class="cta">
  <button class="btn" onclick="window.location.href='/app?mode=signup'">Try free for 7 days</button>
  <button class="btn alt" onclick="window.location.href='/app?mode=login'">Log in</button>
</div>

<footer>
  © 2025 Kind Friend • Bringing kindness to every conversation
  <br>
  <span>In proud support of</span>
  <img src="/static/samaritans_logo.png" alt="Samaritans">
</footer>
</body>
</html>
"""

INDEX_HTML = """<!doctype html>
<html lang="en" data-theme="light">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kind Friend</title>
  <link rel="icon" href="/static/favicon.ico">
  <style>
    :root{--g:#128C7E;--gd:#075E54;--acc:#25D366;--txt:#111b21;--mut:#54656f;--bg:#f0f2f5;--chatbg:#e7f0ea;--me:#d9fdd3;--bot:#ffffff;--br:#d1d7db;--panel:#ffffff;--shadow:0 6px 24px rgba(0,0,0,.12)}
    *{box-sizing:border-box} html,body{height:100%;margin:0}
    body{color:var(--txt);font:15px/1.45 Inter,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;background:var(--bg)}
    .topbar{height:60px;display:flex;align-items:center;gap:12px;padding:0 16px;background:linear-gradient(0deg,var(--gd),var(--g));color:#fff;box-shadow:var(--shadow)}
    .logo{width:36px;height:36px;border-radius:10px;background:var(--acc);display:grid;place-items:center;color:#073;font-weight:800}
    .brand{font-weight:800;letter-spacing:.2px}.grow{flex:1 1 auto}
    .chip{font-size:12px;color:#eafff0;background:rgba(255,255,255,.15);padding:6px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.25)}
    #trial-chip{display:none}
    .tb-btn{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.2);padding:8px 10px;border-radius:999px;cursor:pointer}
    .tb-btn.primary{background:#fff;color:#073}
    .app{display:grid;grid-template-columns:360px 1fr;height:100svh;overflow:hidden}
    @media (max-width:1000px){.app{grid-template-columns:1fr}.sidebar{display:none}}
    .sidebar{display:flex;flex-direction:column;height:calc(100svh - 60px);border-right:1px solid var(--br);background:var(--panel)}
    .side-head{display:flex;gap:8px;align-items:center;padding:12px;border-bottom:1px solid var(--br)}
    .side-actions{display:flex;gap:8px;padding:12px}
    .list{overflow:auto;padding:8px 12px;display:flex;flex-direction:column;gap:8px}
    .item{padding:10px 12px;background:#fff;border:1px solid var(--br);border-radius:12px;cursor:pointer}
    .item.active{outline:2px solid var(--acc)}
    .main{display:flex;flex-direction:column;height:calc(100svh - 60px);background:var(--chatbg);position:relative}
    .chatbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-bottom:1px solid var(--br);background:var(--panel);padding:10px 12px}
    .chat{flex:1 1 auto;min-height:0;overflow:auto;padding:18px 16px;display:grid;gap:8px}
    .row{display:grid;grid-template-columns:auto 1fr;gap:8px;align-items:end}
    .row.user{grid-template-columns:1fr auto}.row.user .avatar{display:none}
    .avatar{width:28px;height:28px;border-radius:50%;overflow:hidden;display:grid;place-items:center;color:#fff;background:var(--g);font-weight:800}
    .avatar img{width:100%;height:100%;object-fit:cover}
    .bubble{max-width:70ch;padding:10px 12px;border-radius:16px;color:var(--txt);position:relative;white-space:pre-wrap;word-wrap:anywhere;box-shadow:0 1px 0 rgba(0,0,0,.08);border:1px solid var(--br)}
    .row.user .bubble{background:var(--me)} .row.bot .bubble{background:var(--bot)}
    .row.user .bubble::after{content:"";position:absolute;right:-6px;bottom:0;width:12px;height:12px;background:var(--me);clip-path:polygon(0 0,100% 100%,0 100%);border-right:1px solid var(--br);border-bottom:1px solid var(--br)}
    .row.bot .bubble::before{content:"";position:absolute;left:-6px;bottom:0;width:12px;height:12px;background:var(--bot);clip-path:polygon(0 100%,100% 0,100% 100%);border-left:1px solid var(--br);border-bottom:1px solid var(--br)}
    .meta{display:flex;gap:8px;align-items:center;color:var(--mut);font-size:11px;margin-top:4px}
    .composer{display:grid;grid-template-columns:1fr auto;gap:8px;padding:10px;border-top:1px solid var(--br);background:var(--panel)}
    .input{padding:12px 14px;border-radius:999px;border:1px solid var(--br);background:#fff;color:var(--txt)}
    .send{background:var(--g);color:#fff;border:none;padding:10px 16px;border-radius:999px;cursor:pointer}
    .modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40}
    .modal{display:none;position:fixed;inset:0;z-index:50;place-items:center}
    .modal.on,.modal-backdrop.on{display:grid}
    .modal-card{width:min(560px,94vw);background:#fff;color:var(--txt);border:1px solid var(--br);border-radius:18px;box-shadow:0 6px 24px rgba(0,0,0,.12);padding:16px}
    .modal-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
    .modal-title{font-weight:800;font-size:16px}
    .xbtn{border:1px solid var(--br);background:#fff;color:var(--txt);border-radius:10px;padding:6px 10px;cursor:pointer}
    .form-row{display:grid;gap:6px;margin:10px 0}
    .form-row input{padding:10px 12px;border-radius:12px;border:1px solid var(--br);background:#fff;color:var(--txt)}
    .form-actions{display:flex;gap:8px;justify-content:flex-end}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="logo">KF</div>
    <div class="brand">Kind Friend · <span id="botname-head">Chatting with Kind Friend</span></div>
    <div class="grow"></div>
    <span class="chip" id="me">Not signed in</span>
    <span class="chip" id="trial-chip" style="display:none;"></span>
    <button id="download-txt" class="tb-btn">.txt</button>
    <button id="download-csv" class="tb-btn">.csv</button>
  </div>

  <div class="app">
    <aside class="sidebar">
      <div class="side-head"><div style="font-weight:700;">Chats</div></div>
      <div class="side-actions">
        <button id="new-chat" class="tb-btn primary">New chat</button>
      </div>
      <div class="list" id="sessions"></div>
    </aside>

    <main class="main">
      <div class="chatbar">
        <div class="auth" id="auth" style="display:flex;gap:8px;align-items:center;">
          <button id="open-auth" class="tb-btn primary">Sign in / up</button>
          <button id="logout" class="tb-btn" style="display:none;">Log out</button>
        </div>
      </div>

      <section class="chat" id="chat"></section>

      <div class="composer">
        <input id="message" class="input" autocomplete="off" placeholder="Sign in to start chatting" disabled />
        <button id="send" class="send" disabled>Send</button>
      </div>
    </main>
  </div>

  <!-- Auth Modal -->
  <div class="modal-backdrop" id="auth-backdrop"></div>
  <div class="modal" id="auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-title">
    <div class="modal-card">
      <div class="modal-header">
        <div class="modal-title" id="auth-title">Welcome to Kind Friend</div>
        <button class="xbtn" id="auth-close">Close</button>
      </div>
      <div class="tabs" style="display:flex;gap:8px;margin-bottom:8px;">
        <button id="tab-login"  class="tb-btn" aria-selected="true">Log in</button>
        <button id="tab-signup" class="tb-btn">Sign up</button>
      </div>
      <div id="pane-login">
        <div class="form-row"><label for="login-username">Username</label><input id="login-username" placeholder="yourname"/></div>
        <div class="form-row"><label for="login-password">Password</label><input id="login-password" type="password" placeholder="••••••••"/></div>
        <div class="form-actions">
          <button class="xbtn" id="login-cancel">Cancel</button>
          <button class="tb-btn primary" id="login-submit">Log in</button>
        </div>
      </div>
      <div id="pane-signup" style="display:none;">
        <div class="form-row"><label for="signup-username">Username</label><input id="signup-username" placeholder="yourname"/></div>
        <div class="form-row"><label for="signup-password">Password</label><input id="signup-password" type="password" placeholder="Create a password"/></div>
        <div class="form-actions">
          <button class="xbtn" id="signup-cancel">Cancel</button>
          <button class="tb-btn primary" id="signup-submit">Create account</button>
        </div>
      </div>
    </div>
  </div>

  <script>
  // ---------- chat helpers ----------
  const chat    = document.getElementById('chat');
  const input   = document.getElementById('message');
  const send    = document.getElementById('send');
  const sessionsEl = document.getElementById('sessions');
  const meSpan  = document.getElementById('me');
  const trialChip = document.getElementById('trial-chip');

  function md(x){
    // Basic HTML escape
    const esc = x.replace(/[&<>]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]));
    // Linkify URLs
    const withLinks = esc.replace(/(https?:\/\/\S+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    // Preserve newlines
    return withLinks.replace(/\n/g, '<br/>');
  }

  function addBubble(text, who){
    const row = document.createElement('div');
    row.className = 'row ' + who;

    const av = document.createElement('div');
    av.className = 'avatar';
    av.textContent = (who === 'bot' ? 'KF' : 'You');

    const b = document.createElement('div');
    b.className = 'bubble';
    b.innerHTML = md(text);

    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

    if (who === 'bot') {
      row.appendChild(av);
      const wrap = document.createElement('div');
      wrap.appendChild(b); wrap.appendChild(meta);
      row.appendChild(wrap);
    } else {
      const wrap = document.createElement('div');
      wrap.appendChild(b); wrap.appendChild(meta);
      row.appendChild(wrap);
      row.style.gridTemplateColumns = '1fr auto';
    }

    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
  }

  function makeBotBubble(initial='…'){
    const row = document.createElement('div'); row.className='row bot';
    const av  = document.createElement('div'); av.className='avatar'; av.textContent='KF';
    const b   = document.createElement('div'); b.className='bubble'; b.textContent=initial;
    const meta= document.createElement('div'); meta.className='meta';
    meta.textContent = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    row.appendChild(av);
    const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta);
    row.appendChild(wrap);
    chat.appendChild(row); chat.scrollTop=chat.scrollHeight;
    return b;
  }

  // ---------- Auth modal wiring (single declarations; no duplicates) ----------
  const authModal     = document.getElementById('auth-modal');
  const authBackdrop  = document.getElementById('auth-backdrop');
  const authClose     = document.getElementById('auth-close');
  const tabLogin      = document.getElementById('tab-login');
  const tabSignup     = document.getElementById('tab-signup');
  const paneLogin     = document.getElementById('pane-login');
  const paneSignup    = document.getElementById('pane-signup');

  const loginUsername = document.getElementById('login-username');
  const loginPassword = document.getElementById('login-password');
  const loginSubmit   = document.getElementById('login-submit');
  const loginCancel   = document.getElementById('login-cancel');

  const signupUsername = document.getElementById('signup-username');
  const signupPassword = document.getElementById('signup-password');
  const signupSubmit   = document.getElementById('signup-submit');
  const signupCancel   = document.getElementById('signup-cancel');

  function showLogin(){
    paneLogin.style.display=''; paneSignup.style.display='none';
    tabLogin.classList.add('primary'); tabSignup.classList.remove('primary');
    tabLogin.setAttribute('aria-selected','true'); tabSignup.setAttribute('aria-selected','false');
  }
  function showSignup(){
    paneLogin.style.display='none'; paneSignup.style.display='';
    tabSignup.classList.add('primary'); tabLogin.classList.remove('primary');
    tabLogin.setAttribute('aria-selected','false'); tabSignup.setAttribute('aria-selected','true');
  }
  function openAuth(which='login'){
    authModal.classList.add('on'); authBackdrop.classList.add('on');
    (which==='signup' ? showSignup() : showLogin());
    setTimeout(()=>{ (which==='signup' ? signupUsername : loginUsername).focus(); }, 50);
  }
  function closeAuth(){ authModal.classList.remove('on'); authBackdrop.classList.remove('on'); }

  document.getElementById('open-auth').onclick = ()=>openAuth('login');
  authClose.onclick = closeAuth; authBackdrop.onclick = closeAuth;
  loginCancel.onclick = closeAuth; signupCancel.onclick = closeAuth;
  tabLogin.onclick = showLogin; tabSignup.onclick = showSignup;

  async function refreshMe(){
    const r = await fetch('/api/me'); const data = await r.json();
    if(data.user){
      input.disabled=false; send.disabled=false; input.placeholder="Type a message";
      meSpan.textContent = `Signed in as ${data.user.display_name || data.user.username}`;
      document.getElementById('open-auth').style.display='none';
      document.getElementById('logout').style.display='';
    } else {
      input.disabled=true; send.disabled=true; input.placeholder="Sign in to start chatting";
      meSpan.textContent = "Not signed in";
      document.getElementById('open-auth').style.display='';
      document.getElementById('logout').style.display='none';
    }
  }

  loginSubmit.onclick = async ()=>{
    const username = loginUsername.value.trim();
    const password = loginPassword.value;
    if(!username || !password) return alert('Enter username & password');
    const r = await fetch('/api/login',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username,password})
    });
    const d = await r.json();
    if(!r.ok) return alert(d.error||'Login failed');
    await refreshMe(); addBubble('Signed in.','bot'); loadSessions(); loadHistory(); closeAuth();
  };

  signupSubmit.onclick = async ()=>{
    const username = signupUsername.value.trim();
    const password = signupPassword.value;
    if(!username || !password) return alert('Enter username & password');
    const r = await fetch('/api/register',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username,password})
    });
    const d = await r.json();
    if(!r.ok) return alert(d.error||'Signup failed');
    await refreshMe(); addBubble('Account created and signed in. 👋','bot'); loadSessions(); loadHistory(); closeAuth();
  };

  document.getElementById('logout').onclick = async ()=>{
    await fetch('/api/logout',{method:'POST'});
    await refreshMe(); addBubble('Signed out.','bot'); loadSessions(); chat.innerHTML='';
  };

  // ---------- sessions & history ----------
  async function loadSessions(){
    const r = await fetch('/api/sessions'); const data = await r.json();
    sessionsEl.innerHTML='';
    (data.sessions || []).forEach(s=>{
      const el = document.createElement('div');
      el.className = 'item' + (data.active === s.id ? ' active' : '');
      el.textContent = s.title || 'Untitled';
      el.onclick = async ()=>{
        await fetch('/api/session/select',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({session_id:s.id})
        });
        await loadHistory(); await loadSessions();
      };
      sessionsEl.appendChild(el);
    });
  }

  async function loadHistory(){
    const r = await fetch('/api/history'); const data = await r.json();
    chat.innerHTML='';
    (data.messages || []).forEach(m => addBubble(m.content, m.role === 'assistant' ? 'bot' : 'user'));
  }

  // ---------- send message (SSE stream) ----------
  async function sendMessage(){
    const msg = input.value.trim(); if(!msg) return;
    input.value=''; addBubble(msg,'user');

    const res = await fetch('/api/chat/stream', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message: msg})
    });

    if(res.status === 401){ openAuth('login'); return; }
    if(!res.ok){ addBubble('Error: ' + (await res.text()), 'bot'); return; }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf='', acc='';
    const bubbleEl = makeBotBubble('…');

    while(true){
      const {value, done} = await reader.read();
      if(done) break;
      buf += decoder.decode(value, {stream:true});
      const parts = buf.split("\n\n");  // SSE chunks
      buf = parts.pop() || '';
      for(const part of parts){
        if(!part.startsWith('data:')) continue;
        const raw = part.slice(5);
        if(raw.trim() === '[DONE]') continue;
        const chunk = raw.replace(/\\n/g, '\n');
        acc += chunk;
        bubbleEl.innerHTML = md(acc);
        chat.scrollTop = chat.scrollHeight;
      }
    }
  }

  send.onclick = sendMessage;
  input.addEventListener('keydown', e=>{
    if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(); }
  });

  // ---------- new chat & export ----------
  document.getElementById('new-chat').onclick = async ()=>{
    const title = prompt('Name your chat (optional):','New chat') || 'New chat';
    const r = await fetch('/api/sessions', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({title})
    });
    if(r.ok){ await loadSessions(); await loadHistory(); }
  };

  document.getElementById('download-txt').onclick = ()=>{ window.location='/api/export?fmt=txt'; };
  document.getElementById('download-csv').onclick = ()=>{ window.location='/api/export?fmt=csv'; };

  // ---------- boot (supports /app?mode=signup|login) ----------
  document.addEventListener('DOMContentLoaded', async () => {
    await refreshMe();
    await loadSessions();
    await loadHistory();

    const params = new URLSearchParams(window.location.search);
    const mode = params.get('mode');
    if (mode === 'signup' || mode === 'login') {
      openAuth(mode);
      // Clean the URL so refresh/back doesn’t reopen the modal
      if (window.history && window.history.replaceState) {
        window.history.replaceState({}, '', '/app');
      }
    }
  });
</script>

</body>
</html>
"""

# ---------- FastAPI app ----------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def landing():
    return HTMLResponse(LANDING_HTML)

@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.get("/app", response_class=HTMLResponse)
async def app_index():
    return HTMLResponse(INDEX_HTML)

@app.head("/app")
async def head_app():
    return Response(status_code=200)

@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "api_available": API_AVAILABLE, "db_path": DB_FILE, "db_exists": os.path.exists(DB_FILE)})

# ---------- Auth APIs ----------
@app.post("/api/register")
async def api_register(request: Request):
    d = await request.json()
    username = (d.get("username") or "").strip().lower()
    password = d.get("password") or ""
    if not username or not password:
        return JSONResponse({"error": "Username and password required"}, status_code=400)
    if get_user_by_username(username):
        return JSONResponse({"error": "Username already taken"}, status_code=409)
    uid = create_user(username, password)
    sid = create_session(uid, title="Welcome")
    resp = JSONResponse({"ok": True, "user_id": uid, "session_id": sid})
    set_auth_cookie(resp, uid)
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.post("/api/login")
async def api_login(request: Request):
    d = await request.json()
    username = (d.get("username") or "").strip().lower()
    password = d.get("password") or ""
    user = get_user_by_username(username)
    if not user or not check_password(password, user["password_hash"]):
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    sessions = list_sessions(user["id"])
    sid = sessions[0]["id"] if sessions else create_session(user["id"], title="New chat")
    resp = JSONResponse({"ok": True, "session_id": sid})
    set_auth_cookie(resp, user["id"])
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
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
        resp = JSONResponse({"user": None}); clear_auth_cookie(resp); return resp
    return JSONResponse({"user": {"id": user["id"], "username": user["username"], "display_name": user["display_name"]}})

# ---------- Sessions & history ----------
@app.get("/api/sessions")
async def api_sessions(request: Request):
    uid = get_current_user_id(request)
    sessions = list_sessions(uid)
    active = request.cookies.get("session_id")
    return JSONResponse({"sessions": sessions, "active": active})

@app.post("/api/sessions")
async def api_sessions_create(request: Request):
    uid = get_current_user_id(request)
    d = await request.json()
    title = (d.get("title") or "New chat").strip() or "New chat"
    sid = create_session(uid, title=title)
    resp = JSONResponse({"ok": True, "session_id": sid})
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.post("/api/session/select")
async def api_session_select(request: Request):
    d = await request.json()
    sid = d.get("session_id")
    if not sid: return JSONResponse({"error": "session_id required"}, status_code=400)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.get("/api/history")
async def api_history(request: Request):
    sid = request.cookies.get("session_id")
    if not sid: return JSONResponse({"messages": [], "session_id": None})
    msgs = get_all_messages(sid)
    out = [{"role": r, "content": c, "ts": ts} for (r, c, ts) in msgs]
    return JSONResponse({"messages": out, "session_id": sid})

# ---------- Chat (SSE stream) ----------
def crisis_guard(text: str) -> Optional[str]:
    lowered = text.lower()
    for k in ["suicide","kill myself","self-harm","end my life","overdose","hurt myself"]:
        if k in lowered:
            return ("I'm really glad you reached out. You deserve support.\n\n"
                    "If you're in the UK, you can call **Samaritans 116 123** any time, or visit A&E / call **999** in an emergency.\n"
                    "I'm here to keep you company, but I'm not a substitute for professional help.")
    return None

def current_time_note():
    tz_name = os.getenv("APP_TZ", "Europe/London")
    now_local = datetime.datetime.now(ZoneInfo(tz_name))
    return f"Today is {now_local.strftime('%A %d %B %Y')} and the local time is {now_local.strftime('%H:%M')} in {tz_name}."

@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    if not API_AVAILABLE: return JSONResponse({"error": "Service is not configured with an API key."}, status_code=500)
    d = await request.json()
    msg = (d.get("message") or "").strip()
    if not msg: return JSONResponse({"error": "Empty message"}, status_code=400)
    uid = get_current_user_id(request)
    sid = request.cookies.get("session_id") or create_session(uid, title="New chat")

    guard = crisis_guard(msg)
    save_message(sid, "user", msg)
    if guard:
        save_message(sid, "assistant", guard)
        def gen_safe():
            yield "data: " + guard.replace("\n","\\n") + "\n\n"
            yield "data: [DONE]\n\n"
        resp = StreamingResponse(gen_safe(), media_type="text/event-stream")
        resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
        return resp

    history = [{"role":"system","content":SYSTEM_PROMPT},
               {"role":"system","content":current_time_note()}]
    history.extend(get_recent_messages(sid, 20))
    history.append({"role":"user","content":msg})

    def stream():
        try:
            r = client.chat.completions.create(model=MODEL_NAME, messages=history, temperature=0.7, stream=True)
            parts=[]
            for chunk in r:
                delta = None
                try:
                    delta = chunk.choices[0].delta.content
                except Exception:
                    try: delta = chunk.choices[0].message.content
                    except Exception: delta = None
                if not delta: continue
                parts.append(delta)
                yield "data: " + delta.replace("\n","\\n") + "\n\n"
            final = "".join(parts)
            save_message(sid, "assistant", final)
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield "data: " + ("[Error] "+str(e)).replace("\n","\\n") + "\n\n"
            yield "data: [DONE]\n\n"

    resp = StreamingResponse(stream(), media_type="text/event-stream")
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

# ---------- Export ----------
@app.get("/api/export")
async def api_export(request: Request, fmt: str = Query("txt", pattern="^(txt|csv)$")):
    sid = request.cookies.get("session_id")
    if not sid: return JSONResponse({"error": "No session"}, status_code=400)
    msgs = get_all_messages(sid)
    now = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
    if fmt == "txt":
        lines=[]
        for role, content, ts in msgs:
            t = datetime.datetime.utcfromtimestamp(ts).isoformat()+"Z"
            who = "User" if role=="user" else ("Assistant" if role=="assistant" else role)
            lines.append(f"[{t}] {who}: {content}")
        text="\n".join(lines)+"\n"
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="kindfriend_{now}.txt"', "Content-Type": "text/plain; charset=utf-8"})
    # csv
    output = io.StringIO(); w = csv.writer(output); w.writerow(["time_utc","role","content"])
    for role, content, ts in msgs:
        t = datetime.datetime.utcfromtimestamp(ts).isoformat()+"Z"; w.writerow([t, role, content])
    csv_data = output.getvalue()
    return PlainTextResponse(csv_data, headers={"Content-Disposition": f'attachment; filename="kindfriend_{now}.csv"', "Content-Type": "text/csv; charset=utf-8"})
