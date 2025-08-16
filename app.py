import os, sys, time, uuid, io, csv, json, datetime, sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# --- Password hashing ---
try:
    import bcrypt
    def hash_password(p: str) -> str: return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
    def check_password(p: str, h: str) -> bool: return bcrypt.checkpw(p.encode(), h.encode())
except Exception:
    import hashlib
    print("⚠ bcrypt not found — using SHA256 fallback (dev only).", file=sys.stderr)
    def hash_password(p: str) -> str: return hashlib.sha256(p.encode()).hexdigest()
    def check_password(p: str, h: str) -> bool: return hashlib.sha256(p.encode()).hexdigest() == h

from itsdangerous import URLSafeSerializer, BadSignature

# Optional Stripe
try:
    import stripe  # type: ignore
except Exception:
    stripe = None

# =========================
# Config & Paths
# =========================
DATA_DIR = os.getenv("DATA_DIR", "")
DB_FILE = os.path.join(DATA_DIR, "kindfriend.db") if DATA_DIR else "kindfriend.db"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
AUTH_COOKIE = "kf_auth"

TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
BILLING_RETURN_URL = os.getenv("BILLING_RETURN_URL", "")

DONATION_NOTE = "Kind Friend donates 50% of all subscription fees to Samaritans (UK)."
DONATION_LINK = "https://www.samaritans.org/"

# OpenAI client
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

# Stripe client
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

SYSTEM_PROMPT = (
    "You are Kind Friend: a warm, respectful companion with the display name set by the user. "
    "Use the provided name (e.g., Tony/Jane) when speaking as yourself. You are not a therapist. "
    "If the user mentions self-harm or immediate danger, suggest contacting UK Samaritans (116 123), "
    "NHS 111, or emergency services (999). Be concise and kind."
)

signer = URLSafeSerializer(SECRET_KEY, salt="kf-auth")

# =========================
# Database
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
            bot_name      TEXT,
            created_at    REAL NOT NULL,
            stripe_customer_id TEXT,
            subscription_status TEXT
        )""")
        # migrations
        for col, ddl in [
            ("user_id", "ALTER TABLE messages ADD COLUMN user_id TEXT"),
            ("stripe_customer_id", "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT"),
            ("subscription_status", "ALTER TABLE users ADD COLUMN subscription_status TEXT"),
            ("bot_name", "ALTER TABLE users ADD COLUMN bot_name TEXT"),
        ]:
            try:
                cur.execute(f"SELECT {col} FROM {'messages' if col=='user_id' else 'users'} LIMIT 1")
            except sqlite3.OperationalError:
                cur.execute(ddl)
        conn.commit()

def create_user(username: str, password: str, bot_name: str = "Kind Friend"):
    uid = str(uuid.uuid4())
    pw_hash = hash_password(password)
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (id, username, password_hash, display_name, bio, bot_name, created_at, stripe_customer_id, subscription_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (uid, username, pw_hash, username, "", bot_name.strip() or "Kind Friend", time.time()),
        )
        conn.commit()
    return uid

def get_user_by_username(username: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, bot_name, created_at, stripe_customer_id, subscription_status FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
    if not row: return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3],
            "bio": row[4], "bot_name": row[5] or "Kind Friend", "created_at": row[6],
            "stripe_customer_id": row[7], "subscription_status": row[8]}

def get_user_by_id(user_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, bot_name, created_at, stripe_customer_id, subscription_status FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
    if not row: return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3],
            "bio": row[4], "bot_name": row[5] or "Kind Friend", "created_at": row[6],
            "stripe_customer_id": row[7], "subscription_status": row[8]}

def update_user_profile(user_id: str, display_name: Optional[str], bio: Optional[str], bot_name: Optional[str]):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        if display_name is None and bio is None and bot_name is None:
            return
        sets, vals = [], []
        if display_name is not None: sets.append("display_name = ?"); vals.append(display_name)
        if bio is not None: sets.append("bio = ?"); vals.append(bio)
        if bot_name is not None: sets.append("bot_name = ?"); vals.append(bot_name.strip() or "Kind Friend")
        vals.append(user_id)
        cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()

def create_session(user_id: Optional[str], title: str = "New chat") -> str:
    sid = str(uuid.uuid4())
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO sessions (id, user_id, title, created_at) VALUES (?, ?, ?, ?)",
                    (sid, user_id, title, time.time()))
        conn.commit()
    return sid

def list_sessions(user_id: Optional[str]):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        if user_id:
            cur.execute("SELECT id, title, created_at FROM sessions WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        else:
            cur.execute("SELECT id, title, created_at FROM sessions WHERE user_id IS NULL ORDER BY created_at DESC")
        rows = cur.fetchall()
    return [{"id": r[0], "title": r[1], "created_at": r[2]} for r in rows]

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
        cur.execute("SELECT role, content FROM messages WHERE session_id = ? ORDER BY ts DESC LIMIT ?", (session_id, limit))
        rows = cur.fetchall()
    rows.reverse()
    return [{"role": r, "content": c} for (r, c) in rows]

def get_all_messages(session_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT role, content, ts, archived FROM messages WHERE session_id = ? ORDER BY ts ASC", (session_id,))
        return cur.fetchall()

# --- Rate limit (token bucket) ---
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "20"))
def check_rate_limit(user_id: Optional[str], ip: str) -> bool:
    if RATE_LIMIT_RPM <= 0: return True
    max_tokens = float(RATE_LIMIT_RPM); refill = max_tokens / 60.0
    key = f"user:{user_id}" if user_id else f"ip:{ip}"
    now = time.time()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT tokens, updated FROM rate_limit WHERE key = ?", (key,))
        except sqlite3.OperationalError:
            cur.execute("CREATE TABLE IF NOT EXISTS rate_limit (key TEXT PRIMARY KEY, tokens REAL, updated REAL)")
            cur.execute("SELECT tokens, updated FROM rate_limit WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO rate_limit (key, tokens, updated) VALUES (?, ?, ?)", (key, max_tokens - 1.0, now))
            conn.commit(); return True
        tokens, updated = float(row[0]), float(row[1])
        elapsed = max(0.0, now - updated)
        tokens = min(max_tokens, tokens + elapsed * refill)
        if tokens < 1.0:
            cur.execute("UPDATE rate_limit SET tokens=?, updated=? WHERE key=?", (tokens, now, key))
            conn.commit(); return False
        tokens -= 1.0
        cur.execute("UPDATE rate_limit SET tokens=?, updated=? WHERE key=?", (tokens, now, key))
        conn.commit(); return True

init_db()

# =========================
# Helpers
# =========================
def crisis_guard(text: str) -> Optional[str]:
    lowered = text.lower()
    for k in ["suicide","kill myself","self-harm","end my life","overdose","hurt myself"]:
        if k in lowered:
            return ("I'm really glad you reached out. You deserve support.\n\n"
                    "If you're in the UK, you can call **Samaritans 116 123** any time, or visit a local A&E / call **999** in an emergency.\n"
                    "If you're elsewhere, please contact your local emergency services or a trusted crisis line.\n\n"
                    "I'm here to keep you company, but I'm not a substitute for professional help.")
    return None

def current_time_note():
    tz_name = os.getenv("APP_TZ", "Europe/London")
    now_local = datetime.datetime.now(ZoneInfo(tz_name))
    return f"Today is {now_local.strftime('%A %d %B %Y')} and the local time is {now_local.strftime('%H:%M')} in {tz_name}."

def trial_info(user: dict):
    if user.get("subscription_status") == "active": return True, None
    end_ts = user["created_at"] + TRIAL_DAYS*86400
    now = time.time()
    if now < end_ts:
        days_left = max(1, int((end_ts - now + 86399)//86400))
        return True, days_left
    return False, 0

# =========================
# HTML: Landing + App
# =========================
LANDING_HTML = """<!doctype html>
<html lang="en" data-theme="light">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Kind Friend — A kinder AI companion</title>
  <link rel="icon" href='data:image/svg+xml;utf8,
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="%23075E54"/><stop offset="100%" stop-color="%2325D366"/>
  </linearGradient></defs>
  <circle cx="32" cy="32" r="30" fill="url(%23g)"/>
  <text x="32" y="38" font-size="24" font-family="Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial" text-anchor="middle" fill="white">KF</text>
</svg>'/>
  <style>
    :root { --g:#128C7E; --gd:#075E54; --acc:#25D366; --txt:#0b1020; --mut:#54656f; --bg:#f0f2f5; }
    html,body{margin:0;height:100%} body{font:16px/1.5 Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; color:var(--txt); background:var(--bg);}
    .hero{max-width:1100px;margin:0 auto;padding:48px 20px;display:grid;grid-template-columns:1.2fr 1fr;gap:32px;align-items:center}
    @media(max-width:1000px){.hero{grid-template-columns:1fr}}
    .title{font-size:42px;line-height:1.15;margin:0 0 10px;font-weight:900;color:#0b1b21}
    .sub{color:var(--mut);margin:0 0 20px}
    .cta{display:flex;gap:12px;flex-wrap:wrap}
    .btn{border:none;border-radius:999px;padding:12px 18px;cursor:pointer;font-weight:700}
    .btn.primary{background:linear-gradient(0deg,var(--gd),var(--g));color:#fff}
    .btn.outline{background:#fff;border:1px solid #d1d7db;color:#0b1b21}
    .card{background:#fff;border:1px solid #d1d7db;border-radius:16px;padding:16px}
    .col{display:grid;gap:16px}
    .list{display:grid;gap:8px;color:#0b1b21}
    .foot{max-width:1100px;margin:0 auto;padding:24px 20px;color:var(--mut)}
    .mock{aspect-ratio:16/10;border-radius:16px;border:1px solid #d1d7db;background:
      linear-gradient(180deg,#e8f6ef, #ffffff 40%), 
      linear-gradient(0deg, #d9fdd3 0 22%, transparent 22% 30%, #ffffff 30% 52%, transparent 52% 60%, #d9fdd3 60% 82%, transparent 82% 90%, #ffffff 90% 100%);
      box-shadow:0 12px 40px rgba(0,0,0,.08);}
  </style>
</head>
<body>
  <section class="hero">
    <div class="col">
      <h1 class="title">Your kinder AI companion.</h1>
      <p class="sub">Talk to a supportive buddy you can name yourself. <strong>50% of all fees go to Samaritans (UK).</strong></p>
      <div class="cta">
        <a class="btn primary" href="/app?mode=signup">Try free for 7 days</a>
        <a class="btn outline" href="/app?mode=login">Log in</a>
      </div>
      <div class="card">
        <strong>What you get</strong>
        <div class="list">
          <div>• Friendly chat (not therapy), with crisis signposting</div>
          <div>• Name your companion (e.g. Tony, Jane)</div>
          <div>• Clean, WhatsApp-style UI</div>
          <div>• Export chats (.txt / .csv)</div>
        </div>
      </div>
      <div class="card" style="font-size:14px;color:#54656f">
        💚 We donate half of all subscription fees to <a href="https://www.samaritans.org/" target="_blank" rel="noopener">Samaritans</a>.
      </div>
    </div>
    <div class="mock"></div>
  </section>
  <footer class="foot">© Kind Friend — be kind to yourself.</footer>
</body>
</html>
"""

# WhatsApp-style app UI (LIGHT by default), auth-locked composer, spacing preserved, bot_name-aware
INDEX_HTML = """<!doctype html>
<html lang="en" data-theme="light">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kind Friend</title>
  <link rel="icon" href='data:image/svg+xml;utf8,
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="%23075E54"/><stop offset="100%" stop-color="%2325D366"/>
  </linearGradient></defs>
  <circle cx="32" cy="32" r="30" fill="url(%23g)"/>
  <text x="32" y="38" font-size="24" font-family="Inter, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial" text-anchor="middle" fill="white">KF</text>
</svg>'/>
  <style>
    :root{--wa-green-dark:#075E54;--wa-green:#128C7E;--wa-accent:#25D366;--wa-bg:#f0f2f5;--wa-chat-bg:#e7f0ea;--wa-bubble-me:#d9fdd3;--wa-bubble-you:#ffffff;--wa-text:#111b21;--wa-muted:#54656f;--wa-border:#d1d7db;--panel:#ffffff;--panel-2:#ffffff;--radius:16px;--shadow:0 6px 24px rgba(0,0,0,.12)}
    *{box-sizing:border-box} html,body{height:100%;margin:0}
    body{color:var(--wa-text);font:15px/1.45 Inter,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;background:var(--wa-bg)}
    .app{display:grid;grid-template-columns:380px 1fr;height:100svh;overflow:hidden}
    @media (max-width:1000px){.app{grid-template-columns:1fr}.sidebar{display:none}}
    .topbar{height:60px;display:flex;align-items:center;gap:10px;padding:0 16px;background:linear-gradient(0deg,var(--wa-green-dark),var(--wa-green));color:#fff;box-shadow:var(--shadow)}
    .logo{width:36px;height:36px;border-radius:10px;background:var(--wa-accent);display:grid;place-items:center;color:#073;font-weight:800}
    .brand{font-weight:800;letter-spacing:.2px}.grow{flex:1 1 auto}
    .chip{font-size:12px;color:#eafff0;background:rgba(255,255,255,.15);padding:6px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.25)}
    #trial-chip{display:none}
    .tb-btn{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.2);padding:8px 10px;border-radius:999px;cursor:pointer}
    .tb-btn.primary{background:#fff;color:#073}
    .sidebar{display:flex;flex-direction:column;height:calc(100svh - 60px);border-right:1px solid var(--wa-border);background:var(--panel)}
    .side-head{display:flex;gap:8px;align-items:center;padding:12px;border-bottom:1px solid var(--wa-border)}
    .side-actions{display:flex;gap:8px;padding:12px}
    .list{overflow:auto;padding:8px 12px;display:flex;flex-direction:column;gap:8px}
    .item{padding:10px 12px;background:var(--panel-2);border:1px solid var(--wa-border);border-radius:12px;cursor:pointer}
    .item.active{outline:2px solid var(--wa-accent)}
    .main{display:flex;flex-direction:column;height:calc(100svh - 60px);background:var(--wa-chat-bg);position:relative}
    .chatbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-bottom:1px solid var(--wa-border);background:var(--panel);padding:10px 12px}
    .auth .tb-btn{background:var(--wa-green);border-color:transparent}.auth .tb-btn.text{background:transparent;border-color:var(--wa-border);color:var(--wa-text)}
    .bg{position:absolute;inset:0;pointer-events:none;opacity:.05;background-image:radial-gradient(circle at 15% 10%,#000 1px,transparent 1px),radial-gradient(circle at 80% 30%,#000 1px,transparent 1px),radial-gradient(circle at 40% 70%,#000 1px,transparent 1px),radial-gradient(circle at 60% 90%,#000 1px,transparent 1px);background-size:240px 240px,220px 220px,260px 260px,200px 200px}
    .chat{flex:1 1 auto;min-height:0;overflow:auto;padding:18px 16px;display:grid;gap:8px}
    .row{display:grid;grid-template-columns:auto 1fr;gap:8px;align-items:end}
    .row.user{grid-template-columns:1fr auto}.row.user .avatar{display:none}
    .avatar{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;color:#fff;background:var(--wa-green);font-weight:800}
    .bubble{max-width:70ch;padding:10px 12px;border-radius:16px;color:var(--wa-text);position:relative;white-space:pre-wrap;word-wrap:anywhere;box-shadow:0 1px 0 rgba(0,0,0,.08);border:1px solid var(--wa-border)}
    .row.user .bubble{background:var(--wa-bubble-me)} .row.bot .bubble{background:var(--wa-bubble-you)}
    .row.user .bubble::after{content:"";position:absolute;right:-6px;bottom:0;width:12px;height:12px;background:var(--wa-bubble-me);clip-path:polygon(0 0,100% 100%,0 100%);border-right:1px solid var(--wa-border);border-bottom:1px solid var(--wa-border)}
    .row.bot .bubble::before{content:"";position:absolute;left:-6px;bottom:0;width:12px;height:12px;background:var(--wa-bubble-you);clip-path:polygon(0 100%,100% 0,100% 100%);border-left:1px solid var(--wa-border);border-bottom:1px solid var(--wa-border)}
    .meta{display:flex;gap:8px;align-items:center;color:var(--wa-muted);font-size:11px;margin-top:4px}
    .composer{display:grid;grid-template-columns:1fr auto;gap:8px;padding:10px;border-top:1px solid var(--wa-border);background:var(--panel)}
    .input{padding:12px 14px;border-radius:999px;border:1px solid var(--wa-border);background:var(--panel-2);color:var(--wa-text)}
    .send{background:var(--wa-green);color:#fff;border:none;padding:10px 16px;border-radius:999px;cursor:pointer}
    .modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:40}
    .modal{display:none;position:fixed;inset:0;z-index:50;place-items:center}
    .modal.on,.modal-backdrop.on{display:grid}
    .modal-card{width:min(520px,94vw);background:var(--panel);color:var(--wa-text);border:1px solid var(--wa-border);border-radius:18px;box-shadow:0 6px 24px rgba(0,0,0,.12);padding:16px}
    .modal-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
    .modal-title{font-weight:800;font-size:16px}
    .xbtn{border:1px solid var(--wa-border);background:var(--panel-2);color:var(--wa-text);border-radius:10px;padding:6px 10px;cursor:pointer}
    .form-row{display:grid;gap:6px;margin:10px 0}
    .form-row input,.form-row textarea{padding:10px 12px;border-radius:12px;border:1px solid var(--wa-border);background:var(--panel-2);color:var(--wa-text)}
    .row .bubble p{margin:0 0 6px 0}.row .bubble p:last-child{margin:0}
    .link{color:var(--wa-green)}
    body.large .bubble{font-size:17px;line-height:1.6}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="logo">KF</div>
    <div class="brand">Kind Friend · <span id="botname-head">Chatting with Kind Friend</span></div>
    <div class="grow"></div>
    <span class="chip" id="me">Not signed in</span>
    <span class="chip" id="trial-chip"></span>
    <button id="theme" class="tb-btn">Theme</button>
    <button id="download-txt" class="tb-btn">.txt</button>
    <button id="download-csv" class="tb-btn">.csv</button>
  </div>

  <div class="app">
    <aside class="sidebar">
      <div class="side-head"><div style="font-weight:700;">Chats</div></div>
      <div class="side-actions">
        <button id="new-chat" class="tb-btn primary">New chat</button>
        <button id="large" class="tb-btn">A A</button>
      </div>
      <div class="list" id="sessions"></div>
    </aside>

    <main class="main">
      <div class="bg"></div>
      <div class="chatbar">
        <div class="auth" id="auth" style="display:flex;gap:8px;align-items:center;">
          <button id="open-auth" class="tb-btn primary">Sign in / up</button>
          <button id="logout" class="tb-btn text" style="display:none;">Log out</button>
          <button id="edit-profile" class="tb-btn text" style="display:none;">Profile</button>
          <button id="upgrade" class="tb-btn primary" style="display:none;">Upgrade</button>
          <button id="billing" class="tb-btn text" style="display:none;">Billing</button>
          <button id="donation-note" class="tb-btn text" title="We donate half of all fees">50% to Samaritans</button>
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
        <div class="form-actions" style="display:flex;gap:8px;justify-content:flex-end;">
          <button class="xbtn" id="login-cancel">Cancel</button>
          <button class="tb-btn primary" id="login-submit">Log in</button>
        </div>
      </div>
      <div id="pane-signup" style="display:none;">
        <div class="form-row"><label for="signup-username">Username</label><input id="signup-username" placeholder="yourname"/></div>
        <div class="form-row"><label for="signup-password">Password</label><input id="signup-password" type="password" placeholder="Create a password"/></div>
        <div class="form-row"><label for="signup-botname">Bot name</label><input id="signup-botname" placeholder="e.g., Tony or Jane" value="Kind Friend"/></div>
        <div class="form-actions" style="display:flex;gap:8px;justify-content:flex-end;">
          <button class="xbtn" id="signup-cancel">Cancel</button>
          <button class="tb-btn primary" id="signup-submit">Create account</button>
        </div>
      </div>
      <div class="donate" style="margin-top:10px;color:#54656f">
        💚 <strong>50% donated</strong> to <a class="link" href="https://www.samaritans.org/" target="_blank" rel="noopener">Samaritans</a>.
      </div>
    </div>
  </div>

  <!-- Profile Modal -->
  <div class="modal-backdrop" id="modal-backdrop"></div>
  <div class="modal" id="profile-modal" role="dialog" aria-modal="true" aria-labelledby="profile-title">
    <div class="modal-card">
      <div class="modal-header">
        <div class="modal-title" id="profile-title">Edit profile</div>
        <button class="xbtn" id="close-modal">Close</button>
      </div>
      <div class="form-row"><label for="display_name">Display name</label><input id="display_name" placeholder="How should Kind Friend address you?"/></div>
      <div class="form-row"><label for="bio">Bio</label><textarea id="bio" rows="4" placeholder="Anything you'd like KF to remember (non-sensitive)."></textarea></div>
      <div class="form-row"><label for="bot_name">Bot name</label><input id="bot_name" placeholder="e.g., Tony, Jane" value="Kind Friend"/></div>
      <div class="form-actions" style="display:flex;gap:8px;justify-content:flex-end;">
        <button class="xbtn" id="cancel-profile">Cancel</button>
        <button class="tb-btn primary" id="save-profile">Save</button>
      </div>
    </div>
  </div>

  <script>
    // Renderer toggle: exact spacing vs links
    const USE_TEXTCONTENT = false;

    const root = document.documentElement;
    const savedTheme = localStorage.getItem('kf-theme'); if (savedTheme) root.setAttribute('data-theme', savedTheme);
    document.getElementById('theme').addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next); localStorage.setItem('kf-theme', next);
    });

    const chat = document.getElementById('chat');
    const input = document.getElementById('message');
    const send  = document.getElementById('send');
    const sessionsEl = document.getElementById('sessions');
    const largeBtn = document.getElementById('large');
    const donationBtn = document.getElementById('donation-note');
    const botnameHead = document.getElementById('botname-head');

    let BOT_NAME = 'Kind Friend';
    let isAuthed = false;

    function setComposerEnabled(on){
      input.disabled = !on; send.disabled = !on;
      input.placeholder = on ? "Type a message" : "Sign in to start chatting";
    }
    setComposerEnabled(false);

    function initials(name){
      const parts = (name||'').trim().split(/\s+/).slice(0,2);
      return parts.map(s=>s[0]||'').join('').toUpperCase() || 'KF';
    }

    donationBtn.addEventListener('click', () => {
      alert('💚 ' + '%%DONATION_NOTE%%' + '\\nLearn more: %%DONATION_LINK%%');
    });
    largeBtn.addEventListener('click', () => { document.body.classList.toggle('large'); });

    function md(x){
      const esc = x.replace(/[&<>]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]));
      const withLinks = esc.replace(/(https?:\\/\\/\\S+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
      return withLinks.replace(/\\n/g, '<br/>');
    }
    function setBubbleContent(el, text){ if(USE_TEXTCONTENT){ el.textContent=text; } else { el.innerHTML=md(text); } }

    function makeBotBubble(){
      const row=document.createElement('div'); row.className='row bot';
      const av=document.createElement('div'); av.className='avatar'; av.textContent=initials(BOT_NAME);
      const b=document.createElement('div'); b.className='bubble'; b.innerHTML='';
      const meta=document.createElement('div'); meta.className='meta'; meta.textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      row.appendChild(av); const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta); row.appendChild(wrap);
      chat.appendChild(row); chat.scrollTop=chat.scrollHeight; return b;
    }
    function addBubble(text, who){
      const row=document.createElement('div'); row.className='row '+who;
      const av=document.createElement('div'); av.className='avatar'; av.textContent=(who==='bot')?initials(BOT_NAME):'You';
      const b=document.createElement('div'); b.className='bubble'; setBubbleContent(b, text);
      const meta=document.createElement('div'); meta.className='meta'; meta.textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      if(who==='bot'){ row.appendChild(av); const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta); row.appendChild(wrap); }
      else { const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta); row.appendChild(wrap); row.style.gridTemplateColumns='1fr auto'; }
      chat.appendChild(row); chat.scrollTop=chat.scrollHeight;
    }

    // ======= Auth modal wiring (same as before, plus bot_name) =======
    const meSpan = document.getElementById('me');
    const trialChip = document.getElementById('trial-chip');
    const logoutBtn = document.getElementById('logout');
    const editProfileBtn = document.getElementById('edit-profile');
    const upgradeBtn = document.getElementById('upgrade');
    const billingBtn = document.getElementById('billing');
    const openAuthBtn = document.getElementById('open-auth');

    const authModal = document.getElementById('auth-modal');
    const authBackdrop = document.getElementById('auth-backdrop');
    const authClose = document.getElementById('auth-close');
    const tabLogin = document.getElementById('tab-login');
    const tabSignup = document.getElementById('tab-signup');
    const paneLogin = document.getElementById('pane-login');
    const paneSignup = document.getElementById('pane-signup');

    const loginUsername = document.getElementById('login-username');
    const loginPassword = document.getElementById('login-password');
    const loginSubmit   = document.getElementById('login-submit');
    const loginCancel   = document.getElementById('login-cancel');

    const signupUsername = document.getElementById('signup-username');
    const signupPassword = document.getElementById('signup-password');
    const signupBotname  = document.getElementById('signup-botname');
    const signupSubmit   = document.getElementById('signup-submit');
    const signupCancel   = document.getElementById('signup-cancel');

    function openAuth(which='login'){ authModal.classList.add('on'); authBackdrop.classList.add('on'); (which==='signup'?showSignup():showLogin()); setTimeout(()=>{ (which==='signup'?signupUsername:loginUsername).focus(); },50); }
    function closeAuth(){ authModal.classList.remove('on'); authBackdrop.classList.remove('on'); }
    function showLogin(){ paneLogin.style.display=''; paneSignup.style.display='none'; tabLogin.classList.add('primary'); tabSignup.classList.remove('primary'); tabLogin.setAttribute('aria-selected','true'); tabSignup.setAttribute('aria-selected','false'); }
    function showSignup(){ paneLogin.style.display='none'; paneSignup.style.display=''; tabSignup.classList.add('primary'); tabLogin.classList.remove('primary'); tabLogin.setAttribute('aria-selected','false'); tabSignup.setAttribute('aria-selected','true'); }

    openAuthBtn.onclick = ()=>openAuth('login'); authClose.onclick=closeAuth; authBackdrop.onclick=closeAuth; loginCancel.onclick=closeAuth; signupCancel.onclick=closeAuth; tabLogin.onclick=showLogin; tabSignup.onclick=showSignup;

    function setComposerByAuth(){
      if(isAuthed){ setComposerEnabled(true); } else { setComposerEnabled(false); }
    }

    async function refreshMe(){
      const r = await fetch('/api/me'); const data = await r.json();
      if(data.user){
        isAuthed = True = true; // keep compatibility with older code if any
        setComposerEnabled(true);
        BOT_NAME = (data.user.bot_name || 'Kind Friend');
        botnameHead.textContent = 'Chatting with ' + BOT_NAME;

        meSpan.textContent = `Signed in as ${data.user.display_name||data.user.username}`;
        openAuthBtn.style.display='none'; logoutBtn.style.display=''; editProfileBtn.style.display='';
        upgradeBtn.style.display=''; billingBtn.style.display='';
        if(data.trial && data.trial.days_remaining !== null){
          trialChip.style.display=''; trialChip.textContent = `Free trial: ${data.trial.days_remaining} day(s) left`;
        } else if (data.subscription_status === 'active'){
          trialChip.style.display=''; trialChip.textContent = 'Subscription: active';
        } else { trialChip.style.display='none'; }
      } else {
        isAuthed = false; setComposerEnabled(false);
        BOT_NAME = 'Kind Friend'; botnameHead.textContent = 'Chatting with Kind Friend';
        meSpan.textContent = 'Not signed in';
        openAuthBtn.style.display=''; logoutBtn.style.display='none'; editProfileBtn.style.display='none';
        upgradeBtn.style.display='none'; billingBtn.style.display='none'; trialChip.style.display='none';
      }
    }

    loginSubmit.onclick = async ()=>{
      const username = loginUsername.value.trim(); const password = loginPassword.value;
      if(!username || !password) return alert('Enter username & password');
      const r = await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
      const d = await r.json(); if(!r.ok) return alert(d.error||'Login failed');
      await refreshMe(); addBubble('Signed in.','bot'); loadSessions(); loadHistory(); closeAuth();
    };
    signupSubmit.onclick = async ()=>{
      const username = signupUsername.value.trim(); const password = signupPassword.value; const bot_name = signupBotname.value.trim() || 'Kind Friend';
      if(!username || !password) return alert('Enter username & password');
      const r = await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password,bot_name})});
      const d = await r.json(); if(!r.ok) return alert(d.error||'Signup failed');
      await refreshMe(); addBubble('Account created and signed in. 👋','bot'); loadSessions(); loadHistory(); closeAuth();
    };
    logoutBtn.onclick=async()=>{ await fetch('/api/logout',{method:'POST'}); await refreshMe(); addBubble('Signed out.','bot'); loadSessions(); chat.innerHTML=''; };

    // Profile save
    const profileModal = document.getElementById('profile-modal'), back2 = document.getElementById('modal-backdrop');
    const saveProfile = document.getElementById('save-profile'); const cancelProfile = document.getElementById('cancel-profile'); const closeModalBtn = document.getElementById('close-modal');
    const displayNameEl = document.getElementById('display_name'); const bioEl = document.getElementById('bio'); const botNameEl = document.getElementById('bot_name');

    document.getElementById('edit-profile').onclick = async ()=>{ profileModal.classList.add('on'); back2.classList.add('on'); const r=await fetch('/api/me'); const d=await r.json(); if(d.user){ displayNameEl.value=d.user.display_name||''; bioEl.value=d.user.bio||''; botNameEl.value=d.user.bot_name || 'Kind Friend'; } };
    function closeProfile(){ profileModal.classList.remove('on'); back2.classList.remove('on'); }
    closeModalBtn.onclick=closeProfile; cancelProfile.onclick=closeProfile; back2.onclick=closeProfile;

    saveProfile.onclick = async ()=>{
      const display_name = displayNameEl.value; const bio = bioEl.value; const bot_name = botNameEl.value.trim() || 'Kind Friend';
      const r = await fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_name,bio,bot_name})});
      const d = await r.json(); if(!r.ok) return alert(d.error||'Could not save');
      BOT_NAME = bot_name; botnameHead.textContent = 'Chatting with ' + BOT_NAME; closeProfile(); addBubble('Profile updated.','bot');
    };

    // Sessions
    async function loadSessions(){ const r=await fetch('/api/sessions'); const data=await r.json(); sessionsEl.innerHTML=''; (data.sessions||[]).forEach(s=>{ const el=document.createElement('div'); el.className='item'+(data.active===s.id?' active':''); el.textContent=s.title||'Untitled'; el.onclick=async()=>{ await fetch('/api/session/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:s.id})}); await loadHistory(); await loadSessions(); }; sessionsEl.appendChild(el); }); }
    async function loadHistory(){ const r=await fetch('/api/history'); const data=await r.json(); chat.innerHTML=''; (data.messages||[]).forEach(m=>addBubble(m.content, m.role==='assistant'?'bot':'user')); }

    document.getElementById('new-chat').onclick=async()=>{
      if(!isAuthed){ openAuth('signup'); return; }
      const title=prompt('Name your chat (optional):','New chat')||'New chat';
      const r=await fetch('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});
      if(r.ok){ await loadSessions(); await loadHistory(); }
    };

    // Streaming send (NO trimming of chunks)
    async function sendMessage(){
      if(!isAuthed){ openAuth('signup'); return; }
      const msg=input.value.trim(); if(!msg) return; input.value=''; addBubble(msg,'user');
      const res=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
      if(res.status===401){ openAuth('login'); return; }
      if(res.status===402){ const d=await res.json(); addBubble(d.error || 'Your free trial has ended. Please upgrade to continue.', 'bot'); return; }
      if(!res.ok){ addBubble('Error: '+(await res.text()),'bot'); return; }
      const reader=res.body.getReader(); const decoder=new TextDecoder(); let buf='', acc=''; const bubbleEl=makeBotBubble();
      while(true){ const {value,done}=await reader.read(); if(done) break;
        buf+=decoder.decode(value,{stream:true});
        const parts=buf.split("\\n\\n"); buf=parts.pop()||'';
        for(const part of parts){
          if(!part.startsWith('data:')) continue;
          const raw = part.slice(5);               // do not trim here (spaces matter)
          if(raw.trim()==='[DONE]') continue;
          const chunk = raw.replace(/\\\\n/g,'\\n');
          acc += chunk;
          setBubbleContent(bubbleEl, acc);
          chat.scrollTop=chat.scrollHeight;
        }
      }
    }
    send.onclick=sendMessage;
    input.addEventListener('keydown',e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); sendMessage(); } });

    document.getElementById('download-txt').onclick=()=>{ window.location='/api/export?fmt=txt'; };
    document.getElementById('download-csv').onclick=()=>{ window.location='/api/export?fmt=csv'; };

    document.getElementById('upgrade').onclick=async()=>{
      if(!confirm('Kind Friend donates 50% of all subscription fees to Samaritans. Continue to Checkout?')) return;
      const r=await fetch('/api/billing/checkout',{method:'POST'}); const d=await r.json(); if(!r.ok||!d.url) return alert(d.error||'Checkout unavailable'); window.location=d.url;
    };
    document.getElementById('billing').onclick=async()=>{
      const r=await fetch('/api/billing/portal',{method:'POST'}); const d=await r.json(); if(!r.ok||!d.url) return alert(d.error||'Portal unavailable'); window.location=d.url;
    };

    // Auto-open auth based on ?mode=login|signup from landing
    (function(){ const p=new URLSearchParams(location.search); const b=p.get('billing'); const m=p.get('mode');
      if(b==='success'){ addBubble('Thank you! Your subscription is active. 💚 We donate 50% of all fees to Samaritans.', 'bot'); }
      else if(b==='cancel'){ addBubble('No problem — you can upgrade any time. 💚 50% goes to Samaritans.', 'bot'); }
      if(m && !isAuthed){ openAuth(m); }
    })();

    (async()=>{ await refreshMe(); await loadSessions(); await loadHistory(); })();
  </script>
</body>
</html>
"""

# =========================
# FastAPI app & middleware
# =========================
app = FastAPI()

@app.exception_handler(Exception)
async def all_exception_handler(request, exc):
    return JSONResponse({"error": "Server error", "error_detail": str(exc)}, status_code=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# =========================
# Routes
# =========================
@app.get("/", response_class=HTMLResponse)
async def landing():
    return HTMLResponse(LANDING_HTML)

@app.get("/app", response_class=HTMLResponse)
async def app_index():
    html = INDEX_HTML.replace("%%DONATION_NOTE%%", DONATION_NOTE).replace("%%DONATION_LINK%%", DONATION_LINK)
    return HTMLResponse(html)

@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "api_available": API_AVAILABLE, "db_path": DB_FILE, "db_exists": os.path.exists(DB_FILE)})

# ---- Auth helpers ----
def set_auth_cookie(resp, user_id: str):
    token = signer.dumps({"user_id": user_id, "ts": time.time()})
    resp.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="Lax", max_age=60*60*24*180)
def clear_auth_cookie(resp):
    resp.delete_cookie(AUTH_COOKIE)
def get_current_user_id(request: Request) -> Optional[str]:
    token = request.cookies.get(AUTH_COOKIE)
    if not token: return None
    try:
        return signer.loads(token).get("user_id")
    except BadSignature:
        return None

# ---- Auth APIs ----
@app.post("/api/register")
async def api_register(request: Request):
    d = await request.json()
    username = (d.get("username") or "").strip().lower()
    password = (d.get("password") or "")
    bot_name = (d.get("bot_name") or "Kind Friend").strip() or "Kind Friend"
    if not username or not password:
        return JSONResponse({"error": "Username and password required"}, status_code=400)
    if get_user_by_username(username):
        return JSONResponse({"error": "Username already taken"}, status_code=409)
    uid = create_user(username, password, bot_name=bot_name)
    sid = create_session(uid, title="Welcome")
    resp = JSONResponse({"ok": True, "user_id": uid, "session_id": sid})
    set_auth_cookie(resp, uid)
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.post("/api/login")
async def api_login(request: Request):
    d = await request.json()
    username = (d.get("username") or "").strip().lower()
    password = (d.get("password") or "")
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
    resp = JSONResponse({"ok": True}); clear_auth_cookie(resp); return resp

@app.get("/api/me")
async def api_me(request: Request):
    uid = get_current_user_id(request)
    if not uid:
        return JSONResponse({"user": None})
    user = get_user_by_id(uid)
    if not user:
        resp = JSONResponse({"user": None}); clear_auth_cookie(resp); return resp
    allowed, days_left = trial_info(user)
    safe = {"id": user["id"], "username": user["username"], "display_name": user["display_name"],
            "bio": user["bio"], "bot_name": user["bot_name"], "subscription_status": user["subscription_status"]}
    trial = {"is_allowed": allowed, "days_remaining": days_left if days_left is not None else None}
    return JSONResponse({"user": safe, "trial": trial, "subscription_status": user["subscription_status"]})

@app.post("/api/profile")
async def api_profile(request: Request):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    d = await request.json()
    update_user_profile(uid, d.get("display_name"), d.get("bio"), d.get("bot_name"))
    return JSONResponse({"ok": True})

# ---- Sessions & history ----
@app.get("/api/sessions")
async def api_sessions(request: Request):
    uid = get_current_user_id(request)
    sessions = list_sessions(uid)
    active = request.cookies.get("session_id")
    return JSONResponse({"sessions": sessions, "active": active})

@app.post("/api/sessions")
async def api_sessions_create(request: Request):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error":"Sign in required"}, status_code=401)
    d = await request.json(); title = (d.get("title") or "New chat").strip() or "New chat"
    sid = create_session(uid, title=title)
    resp = JSONResponse({"ok": True, "session_id": sid})
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.post("/api/session/select")
async def api_session_select(request: Request):
    d = await request.json(); sid = d.get("session_id")
    if not sid: return JSONResponse({"error": "session_id required"}, status_code=400)
    resp = JSONResponse({"ok": True}); resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

@app.get("/api/history")
async def api_history(request: Request):
    sid = request.cookies.get("session_id")
    if not sid: return JSONResponse({"messages": [], "session_id": None})
    msgs = get_all_messages(sid)
    out = [{"role": r, "content": c, "ts": ts} for (r, c, ts, _arch) in msgs]
    return JSONResponse({"messages": out, "session_id": sid})

# ---- Chat
@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    if not API_AVAILABLE: return JSONResponse({"error": "Service is not configured with an API key."}, status_code=500)
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Please sign in to chat."}, status_code=401)
    user = get_user_by_id(uid)
    allowed, _days_left = trial_info(user)
    if not allowed: return JSONResponse({"error": "Your free trial has ended. Please upgrade to continue."}, status_code=402)
    ip = request.headers.get("x-forwarded-for", (request.client.host if request.client else "unknown")).split(",")[0].strip()
    if not check_rate_limit(uid, ip): return JSONResponse({"error": "Rate limit exceeded. Please wait a moment."}, status_code=429)

    d = await request.json(); user_message = (d.get("message") or "").strip()
    if not user_message: return JSONResponse({"error": "Empty message"}, status_code=400)
    sid = request.cookies.get("session_id") or create_session(uid, title="New chat")

    guard = crisis_guard(user_message)
    if guard:
        save_message(sid, "user", user_message, uid); save_message(sid, "assistant", guard, uid)
        def gen_safe(): yield "data: " + guard.replace("\n","\\n") + "\n\n"; yield "data: [DONE]\n\n"
        resp = StreamingResponse(gen_safe(), media_type="text/event-stream"); resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90); return resp

    save_message(sid, "user", user_message, uid)
    tz_note = current_time_note()
    history = [{"role":"system","content":SYSTEM_PROMPT},
               {"role":"system","content":f"The bot's display name is '{user.get('bot_name') or 'Kind Friend'}'."},
               {"role":"system","content":tz_note}]
    history.extend(get_recent_messages(sid, 20))
    history.append({"role":"user","content":user_message})

    def event_stream():
        try:
            stream = client.chat.completions.create(model=MODEL_NAME, messages=history, temperature=0.7, stream=True)
            parts = []
            for chunk in stream:
                delta = None
                try: delta = chunk.choices[0].delta.content
                except Exception:
                    try: delta = chunk.choices[0].message.content
                    except Exception: delta = None
                if not delta: continue
                parts.append(delta)
                yield "data: " + delta.replace("\n","\\n") + "\n\n"
            final = "".join(parts)
            save_message(sid, "assistant", final, uid)
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield "data: " + ("[Error] " + str(e)).replace("\n","\\n") + "\n\n"
            yield "data: [DONE]\n\n"

    resp = StreamingResponse(event_stream(), media_type="text/event-stream"); resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90); return resp

@app.post("/api/chat")
async def api_chat(request: Request):
    if not API_AVAILABLE: return JSONResponse({"error": "Service is not configured with an API key."}, status_code=500)
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Please sign in to chat."}, status_code=401)
    user = get_user_by_id(uid)
    allowed, _ = trial_info(user)
    if not allowed: return JSONResponse({"error": "Your free trial has ended. Please upgrade to continue."}, status_code=402)
    ip = request.headers.get("x-forwarded-for", (request.client.host if request.client else "unknown")).split(",")[0].strip()
    if not check_rate_limit(uid, ip): return JSONResponse({"error": "Rate limit exceeded. Please wait a moment."}, status_code=429)
    d = await request.json(); user_message = (d.get("message") or "").strip()
    if not user_message: return JSONResponse({"error": "Empty message"}, status_code=400)
    sid = request.cookies.get("session_id") or create_session(uid, title="New chat")
    guard = crisis_guard(user_message)
    if guard:
        save_message(sid, "user", user_message, uid); save_message(sid, "assistant", guard, uid)
        resp = JSONResponse({"reply": guard}); resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90); return resp
    save_message(sid, "user", user_message, uid)
    tz_note = current_time_note()
    history = [{"role":"system","content":SYSTEM_PROMPT},
               {"role":"system","content":f"The bot's display name is '{user.get('bot_name') or 'Kind Friend'}'."},
               {"role":"system","content":tz_note}]
    history.extend(get_recent_messages(sid, 20))
    try:
        r = client.chat.completions.create(model=MODEL_NAME, messages=history, temperature=0.7)
        reply = r.choices[0].message.content
    except Exception as e:
        return JSONResponse({"error":"OpenAI error","error_detail":str(e)}, status_code=502)
    save_message(sid, "assistant", reply, uid)
    resp = JSONResponse({"reply": reply}); resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90); return resp

# ---- Export
@app.get("/api/export")
async def api_export(request: Request, fmt: str = Query("txt", pattern="^(txt|csv)$")):
    session_id = request.cookies.get("session_id")
    if not session_id: return JSONResponse({"error": "No session"}, status_code=400)
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
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="kindfriend_{now}.txt"', "Content-Type": "text/plain; charset=utf-8"})
    output = io.StringIO(); writer = csv.writer(output); writer.writerow(["time_utc","role","content","archived"])
    for role, content, ts, archived in msgs:
        t = datetime.datetime.utcfromtimestamp(ts).isoformat() + "Z"; writer.writerow([t, role, content, archived])
    csv_data = output.getvalue()
    return PlainTextResponse(csv_data, headers={"Content-Disposition": f'attachment; filename="kindfriend_{now}.csv"', "Content-Type": "text/csv; charset=utf-8"})

# ---- Stripe
def _stripe_ready_basic():
    if not stripe or not STRIPE_SECRET_KEY: return "Stripe not configured. Set STRIPE_SECRET_KEY."
    return None
def _require_stripe_ready_for_checkout():
    if not stripe or not STRIPE_SECRET_KEY: return "Stripe not configured. Set STRIPE_SECRET_KEY."
    if not STRIPE_PRICE_ID: return "STRIPE_PRICE_ID env var is required for checkout."
    if not BILLING_RETURN_URL: return "BILLING_RETURN_URL is required (e.g., https://kindfriend.onrender.com)."
    return None
def _get_or_create_customer(user):
    if user.get("stripe_customer_id"): return user["stripe_customer_id"]
    cust = stripe.Customer.create(email=f"{user['username']}@example.local", metadata={"kf_user_id": user["id"]})
    update_user_profile(user["id"], None, None, user.get("bot_name") or "Kind Friend")
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor(); cur.execute("UPDATE users SET stripe_customer_id=? WHERE id=?", (cust["id"], user["id"])); conn.commit()
    return cust["id"]

@app.post("/api/billing/checkout")
async def api_billing_checkout(request: Request):
    err = _require_stripe_ready_for_checkout()
    if err: return JSONResponse({"error": err}, status_code=400)
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    user = get_user_by_id(uid)
    customer_id = _get_or_create_customer(user)
    session = stripe.checkout.Session.create(
        mode="subscription", customer=customer_id,
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=BILLING_RETURN_URL + "?billing=success", cancel_url=BILLING_RETURN_URL + "?billing=cancel",
        metadata={"kf_user_id": uid, "donation_note": DONATION_NOTE},
    )
    return JSONResponse({"url": session.url})

@app.post("/api/billing/portal")
async def api_billing_portal(request: Request):
    err = _stripe_ready_basic()
    if err: return JSONResponse({"error": err}, status_code=400)
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    user = get_user_by_id(uid)
    if not user or not user.get("stripe_customer_id"): return JSONResponse({"error": "No Stripe customer yet. Try Upgrade first."}, status_code=400)
    session = stripe.billing_portal.Session.create(customer=user["stripe_customer_id"], return_url=BILLING_RETURN_URL or "/")
    return JSONResponse({"url": session.url})

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not stripe: return PlainTextResponse("stripe not configured", status_code=400)
    payload = await request.body(); sig = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET) if STRIPE_WEBHOOK_SECRET else json.loads(payload)
    except Exception as e:
        return PlainTextResponse(f"Invalid payload: {e}", status_code=400)
    t = event.get("type"); data = event.get("data", {}).get("object", {}); customer_id = data.get("customer"); status = None
    if t == "checkout.session.completed": status = "active"
    elif t == "customer.subscription.updated": status = data.get("status")
    elif t == "customer.subscription.deleted": status = "canceled"
    if customer_id and status:
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor(); cur.execute("UPDATE users SET subscription_status=? WHERE stripe_customer_id=?", (status, customer_id)); conn.commit()
    return PlainTextResponse("ok", status_code=200)
