# app.py — Kind Coach (all-in-one FastAPI app)

import os
import io
import csv
import sys
import json
import time
import uuid
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Query, Path
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
    RedirectResponse,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from fastapi import Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# ---------- Password hashing ----------
try:
    import bcrypt

    def hash_password(p: str) -> str:
        return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()

    def check_password(p: str, h: str) -> bool:
        return bcrypt.checkpw(p.encode(), h.encode())
except Exception:
    import hashlib
    print("⚠ bcrypt not available — using sha256 (dev only)", file=sys.stderr)

    def hash_password(p: str) -> str:
        return hashlib.sha256(p.encode()).hexdigest()

    def check_password(p: str, h: str) -> bool:
        return hashlib.sha256(p.encode()).hexdigest() == h

# ---------- App setup ----------
app = FastAPI(title="Kind Coach")

SECRET_KEY = os.getenv("APP_SECRET_KEY", "dev-secret")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

# Static (optional)
if not os.path.exists("static"):
    os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

DB_PATH = os.getenv("APP_DB_PATH", "app.db")
APP_NAME = "Kind Coach"
APP_TZ = os.getenv("APP_TZ", "Europe/London")

templates = Jinja2Templates(directory="templates")

# ---------- OpenAI ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_FREE = os.getenv("OPENAI_MODEL_FREE", "gpt-4o-mini")
OPENAI_MODEL_PLUS = os.getenv("OPENAI_MODEL_PLUS", "gpt-4o-mini")
OPENAI_MODEL_PRO  = os.getenv("OPENAI_MODEL_PRO",  "gpt-4o")

if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
        LLM_READY = True
    except Exception as e:
        print("OpenAI init error:", e, file=sys.stderr)
        _client = None
        LLM_READY = False
else:
    _client = None
    LLM_READY = False
    print("⚠ OPENAI_API_KEY not set", file=sys.stderr)
# ---------- LLM helper ----------
def llm_chat(model: str, max_tokens: int, messages):
    """
    Thin wrapper around OpenAI Chat Completions.
    Returns the assistant message string or "" on failure.
    """
    if not LLM_READY or _client is None:
        return ""
    try:
        resp = _client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.7,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        # Log and degrade gracefully
        print(f"[LLM error] {e}", file=sys.stderr)
        return ""

# ---------- Stripe (optional) ----------
PUBLIC_URL             = os.getenv("PUBLIC_URL", "http://localhost:8000")
STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_PLUS      = os.getenv("STRIPE_PRICE_PLUS")  # £4.99/mo
STRIPE_PRICE_PRO       = os.getenv("STRIPE_PRICE_PRO")   # £7.99/mo

PRICE_TO_PLAN: Dict[str, str] = {}
def prices_loaded() -> bool:
    return bool(STRIPE_PRICE_PLUS and STRIPE_PRICE_PRO)

try:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    STRIPE_READY = bool(STRIPE_SECRET_KEY)
except Exception as e:
    print("Stripe init error:", e, file=sys.stderr)
    STRIPE_READY = False

def map_price_to_plan(price_id: Optional[str]) -> Optional[str]:
    if not price_id: return None
    if not PRICE_TO_PLAN:
        if STRIPE_PRICE_PLUS: PRICE_TO_PLAN[STRIPE_PRICE_PLUS] = "plus"
        if STRIPE_PRICE_PRO:  PRICE_TO_PLAN[STRIPE_PRICE_PRO]  = "pro"
    return PRICE_TO_PLAN.get(price_id)

# ---------- Plans / feature flags ----------
PLANS: Dict[str, Dict[str, Any]] = {
    "free": {
        "name": "Free", "price": "£0",
        "chat_daily": 15, "mem_limit": 100, "allow_export_csv": False,
        "context_notes": 12, "model": OPENAI_MODEL_FREE, "max_tokens": 350, "coach_daily": 1,
    },
    "plus": {
        "name": "Plus", "price": "£4.99/mo",
        "chat_daily": 200, "mem_limit": 1000, "allow_export_csv": True,
        "context_notes": 18, "model": OPENAI_MODEL_PLUS, "max_tokens": 900, "coach_daily": 3,
    },
    "pro": {
        "name": "Pro", "price": "£7.99/mo",
        "chat_daily": 2000, "mem_limit": 10000, "allow_export_csv": True,
        "context_notes": 28, "model": OPENAI_MODEL_PRO, "max_tokens": 2200, "coach_daily": 10,
    },
}

# ---------- System prompts ----------
SYSTEM_PROMPT = (
    "You are Kind Coach, a person-centred, strengths-based personal coach. "
    "You are not a therapist. If the user mentions self-harm or danger, suggest UK Samaritans (116 123), "
    "NHS 111, or 999 in an emergency. Reflect the user's language; ask short, powerful questions; "
    "offer 1–2 practical next steps; and keep answers concise."
)
COACHING_STYLE = (
    "Coaching style: solution-focused, values-led, growth mindset, SMART goals. "
    "Prefer questions to advice. Keep replies under ~200 words unless asked."
)

# ---------- DB helpers ----------
def ensure_preferences_schema():
    conn = db()
    cur = conn.cursor()
    # Create table if missing (you already have this in init, but safe to keep)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT DEFAULT 'UTC',
            dark_mode INTEGER DEFAULT 0,
            notifications INTEGER DEFAULT 1,
            save_memories INTEGER DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    # Add save_memories if it’s missing
    cols = [c[1] for c in cur.execute("PRAGMA table_info(preferences)")]
    if "save_memories" not in cols:
        cur.execute("ALTER TABLE preferences ADD COLUMN save_memories INTEGER DEFAULT 1")
    conn.commit()
    conn.close()
def get_or_create_preferences(uid: int) -> sqlite3.Row:
    conn = db()
    row = conn.execute("SELECT * FROM preferences WHERE user_id=?", (uid,)).fetchone()
    if not row:
        conn.execute("INSERT INTO preferences (user_id) VALUES (?)", (uid,))
        conn.commit()
        row = conn.execute("SELECT * FROM preferences WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row

def update_preferences(uid: int, timezone: str, dark_mode: int, notifications: int, save_memories: int):
    conn = db()
    conn.execute("""
        INSERT INTO preferences (user_id, timezone, dark_mode, notifications, save_memories)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          timezone=excluded.timezone,
          dark_mode=excluded.dark_mode,
          notifications=excluded.notifications,
          save_memories=excluded.save_memories
    """, (uid, timezone, dark_mode, notifications, save_memories))
    conn.commit()
    conn.close()

# Call it during startup (where you call other ensure_* functions):
ensure_preferences_schema()

def db() -> sqlite3.Connection:
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
            created_at TEXT NOT NULL,
            stripe_customer_id TEXT
        )
    """)

    # preferences
    cur.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT DEFAULT 'UTC',
            dark_mode INTEGER DEFAULT 0,
            notifications INTEGER DEFAULT 1,
            retain_memories INTEGER DEFAULT 1,
            chat_retention_days INTEGER DEFAULT 90,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # user memories
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_memories (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # chat sessions
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT,
            created_at REAL NOT NULL,
            saved INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # chat messages
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # coaching notes
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coaching (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            reflections TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # subscriptions
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            plan TEXT NOT NULL DEFAULT 'free',
            stripe_subscription_id TEXT,
            status TEXT,
            current_period_end INTEGER,
            started_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # indices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_id, ts)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON user_memories(user_id)")

    conn.commit()
    conn.close()

init_db()

# ---------- Feature helpers ----------
def current_user_id(request: Request) -> Optional[int]:
    return request.session.get("user_id")

def ensure_subscription_row(uid: int):
    conn = db()
    r = conn.execute("SELECT user_id FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
    if not r:
        conn.execute(
            "INSERT INTO subscriptions (user_id, plan, status, started_at) VALUES (?, 'free', 'active', ?)",
            (uid, datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()

def set_plan(uid: int, plan: str, sub_id: Optional[str] = None, status: Optional[str] = None, cpe: Optional[int] = None):
    if plan not in PLANS: plan = "free"
    conn = db()
    conn.execute("""
        INSERT INTO subscriptions (user_id, plan, stripe_subscription_id, status, current_period_end, started_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          plan=excluded.plan,
          stripe_subscription_id=excluded.stripe_subscription_id,
          status=excluded.status,
          current_period_end=excluded.current_period_end
    """, (uid, plan, sub_id, status, cpe, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def get_subscription(uid: Optional[int]) -> str:
    if not uid: return "free"
    conn = db()
    r = conn.execute("SELECT plan, status FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if not r: return "free"
    plan, status = r["plan"], (r["status"] or "")
    if plan in ("plus","pro") and status not in ("active","trialing","past_due"):
        return "free"
    return plan if plan in PLANS else "free"

def plan_cfg(uid: Optional[int]) -> Tuple[str, Dict[str, Any]]:
    p = get_subscription(uid)
    return p, PLANS[p]

def memories_count(uid: Optional[int]) -> int:
    if not uid: return 0
    conn = db()
    cnt = conn.execute("SELECT COUNT(*) AS c FROM user_memories WHERE user_id=?", (uid,)).fetchone()["c"]
    conn.close()
    return int(cnt)

def day_bounds_epoch(tz_name: str) -> Tuple[float, float]:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    start = datetime(now.year, now.month, now.day, 0,0,0, tzinfo=tz)
    end   = datetime(now.year, now.month, now.day, 23,59,59, tzinfo=tz)
    return start.timestamp(), end.timestamp()

def chat_messages_today(uid: Optional[int]) -> int:
    if not uid: return 0
    start, end = day_bounds_epoch(APP_TZ)
    conn = db()
    cnt = conn.execute("""
        SELECT COUNT(*) AS c
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        WHERE s.user_id = ? AND m.role='user' AND m.ts BETWEEN ? AND ?
    """, (uid, start, end)).fetchone()["c"]
    conn.close()
    return int(cnt)

def coaching_sessions_today(uid: Optional[int]) -> int:
    if not uid: return 0
    start, end = day_bounds_epoch(APP_TZ)
    conn = db()
    cnt = conn.execute("""
        SELECT COUNT(*) AS c FROM coaching
        WHERE user_id = ? AND created_at BETWEEN ? AND ?
    """, (uid, datetime.fromtimestamp(start).isoformat(), datetime.fromtimestamp(end).isoformat())).fetchone()["c"]
    conn.close()
    return int(cnt)

def get_user_memories_text(uid: Optional[int], limit: int) -> Optional[str]:
    if not uid: return None
    conn = db()
    rows = conn.execute(
        "SELECT note FROM user_memories WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (uid, limit)
    ).fetchall()
    conn.close()
    if not rows: return None
    bullets = "\n".join(f"- {r['note']}" for r in rows)
    return "User-approved memory notes:\n" + bullets

def crisis_guard(text: str) -> Optional[str]:
    lowered = text.lower()
    for k in ["suicide","kill myself","self-harm","end my life","overdose","hurt myself"]:
        if k in lowered:
            return ("I'm really glad you reached out. You deserve support.\n\n"
                    "If you're in the UK, you can call **Samaritans 116 123** any time, "
                    "or visit A&E / call **999** in an emergency.\n"
                    "I'm here to keep you company, but I'm not a substitute for professional help.")
    return None

def current_time_note():
    now_local = datetime.now(ZoneInfo(APP_TZ))
    return f"Today is {now_local.strftime('%A %d %B %Y')} and the local time is {now_local.strftime('%H:%M')} in {APP_TZ}."

# ---------- UI (Landing + App) ----------
LANDING_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Kind Coach — Person-Centred Coaching</title>
<link rel="icon" href="/static/favicon.ico">
<style>
:root{
  --bg:#0f1221; --bg2:#0c1020; --card:#121632; --mut:#8ea1b3; --txt:#ecf3ff;
  --brand:#6ef3a5; --brand-2:#30d695; --chip:#202645; --stroke:#2a2f52; --shadow:0 12px 40px rgba(0,0,0,.35)
}
*{box-sizing:border-box} html,body{height:100%}
body{
  margin:0; background:radial-gradient(1200px 900px at 10% -20%, #1b2147 10%, transparent 60%),
             radial-gradient(900px 800px at 110% 0%, #20305d 10%, transparent 55%),
             linear-gradient(180deg, var(--bg), var(--bg2));
  color:var(--txt); font:15px/1.55 Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial;
}
header{
  background:linear-gradient(135deg, rgba(110,243,165,.18), rgba(48,214,149,.08));
  border-bottom:1px solid var(--stroke);
  padding:48px 18px; text-align:center; position:sticky; top:0; backdrop-filter:saturate(150%) blur(4px)
}
header h1{margin:0 0 4px 0; font-size:2.25rem; letter-spacing:.2px}
header p{margin:0; color:var(--mut)}
.hero{display:grid; place-items:center}
.hero img{max-width:980px; width:min(92vw,980px); height:auto; margin:26px auto; border-radius:20px; box-shadow:var(--shadow); border:1px solid var(--stroke)}
.btn{
  background:linear-gradient(180deg, var(--brand), var(--brand-2)); color:#041d14; border:none;
  padding:12px 18px; border-radius:12px; cursor:pointer; font-weight:800; letter-spacing:.2px; box-shadow:0 8px 30px rgba(48,214,149,.25)
}
.btn.alt{
  background:transparent; color:var(--txt); border:1px solid var(--stroke);
}
.grid{display:grid; grid-template-columns:repeat(3,1fr); gap:18px; max-width:1120px; margin:0 auto 28px; padding:0 18px}
.card{
  background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,.00));
  border:1px solid var(--stroke); border-radius:16px; padding:18px; box-shadow:var(--shadow)
}
@media (max-width:900px){ .grid{grid-template-columns:1fr} header h1{font-size:1.8rem} }
</style></head><body>
<header><h1>Kind Coach</h1><p>Friendly AI coaching with a person-centred touch</p></header>
<div class="hero"><img src="/static/coffee_chat.jpg" alt="Coaching conversation"></div>
<div class="grid">
  <div class="card"><h3>Guided Coaching</h3><p>Short, powerful questions to unlock momentum.</p></div>
  <div class="card"><h3>Daily Reflections</h3><p>Gentle prompts to reflect, learn and reset.</p></div>
  <div class="card"><h3>Privacy First</h3><p>You control what’s remembered. Delete anytime.</p></div>
</div>
<p>
  <button class="btn" onclick="location.href='/app?mode=signup'">Try free</button>
  <button class="btn alt" onclick="location.href='/pricing'">Pricing</button>
  <button class="btn alt" onclick="location.href='/app'">Open app</button>
</p>
<p class="small">50% of proceeds donated to Samaritans.</p>
</body></html>
"""

APP_HTML = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Kind Coach</title><link rel="icon" href="/static/favicon.ico">
# [449–490] REPLACE the entire <style>...</style> in APP_HTML with:
<style>
:root{
  --bg:#0f1221; --panel:#121632; --panel-2:#0f1431; --stroke:#2a2f52; --txt:#eaf2ff; --mut:#8ea1b3;
  --brand:#6ef3a5; --brand-2:#30d695; --chip:#1c2246; --me:#0e2d1f; --bot:#141a3a; --bubble:#1b2147;
  --shadow:0 10px 40px rgba(0,0,0,.35)
}
*{box-sizing:border-box} html,body{height:100%}
body{margin:0; background:radial-gradient(1200px 900px at 10% -20%, #1b2147 10%, transparent 60%),
                      radial-gradient(900px 800px at 110% 0%, #20305d 10%, transparent 55%),
                      linear-gradient(180deg, var(--bg), #0c1020); color:var(--txt); font:15px/1.55 Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial}
.topbar{
  height:64px; display:flex; align-items:center; gap:12px; padding:0 16px;
  background:linear-gradient(135deg, rgba(110,243,165,.12), rgba(48,214,149,.06));
  border-bottom:1px solid var(--stroke); color:var(--txt); box-shadow:var(--shadow); position:sticky; top:0; z-index:20; backdrop-filter:saturate(140%) blur(6px)
}
.logo{width:36px;height:36px;border-radius:10px;background:linear-gradient(180deg,var(--brand),var(--brand-2));display:grid;place-items:center;color:#041d14;font-weight:900}
.brand{font-weight:900; letter-spacing:.3px}
.grow{flex:1}
.chip{font-size:12px;color:#cfe6ff;background:var(--chip);padding:6px 10px;border-radius:999px;border:1px solid var(--stroke)}
.tb-btn{background:transparent;color:var(--txt);border:1px solid var(--stroke);padding:8px 12px;border-radius:12px;cursor:pointer}
.tb-btn.primary{background:linear-gradient(180deg,var(--brand),var(--brand-2)); color:#042216; border:none; font-weight:800}
.app{display:grid;grid-template-columns:320px 1fr;height:calc(100svh - 64px);overflow:hidden}
@media (max-width:1000px){.app{grid-template-columns:1fr}.sidebar{display:none}}

.sidebar{
  display:flex;flex-direction:column;height:100%;border-right:1px solid var(--stroke);
  background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,.00))
}
.side-head{display:flex;gap:8px;align-items:center;padding:14px;border-bottom:1px solid var(--stroke)}
.side-actions{display:flex;gap:8px;padding:12px;flex-wrap:wrap}
.list{overflow:auto;padding:10px 12px;display:flex;flex-direction:column;gap:10px}
.item{
  padding:10px 12px;background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,.00));
  border:1px solid var(--stroke);border-radius:12px;cursor:pointer;transition:transform .08s ease, border-color .12s ease
}
.item:hover{transform:translateY(-1px); border-color:#354070}
.item.active{outline:2px solid var(--brand)}

.main{display:flex;flex-direction:column;height:100%;position:relative;background:radial-gradient(1200px 900px at 20% -20%, #171e43 5%, transparent 60%)}
.chatbar{
  display:flex;gap:8px;align-items:center;border-bottom:1px solid var(--stroke);
  background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,.00));padding:12px 14px;flex-wrap:wrap
}
.chat{
  flex:1; min-height:0; overflow:auto; padding:22px 18px; display:grid; gap:10px;
  background:linear-gradient(180deg, rgba(255,255,255,.00), rgba(255,255,255,.02));
}
.row{display:grid;grid-template-columns:auto 1fr;gap:10px;align-items:end}
.row.user{grid-template-columns:1fr auto}.row.user .avatar{display:none}
.avatar{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;color:#042216;background:linear-gradient(180deg,var(--brand),var(--brand-2));font-weight:900}
.bubble{
  max-width:72ch;padding:12px 14px;border-radius:14px;color:var(--txt);white-space:pre-wrap;word-wrap:anywhere;
  background:var(--bubble); border:1px solid var(--stroke); box-shadow:var(--shadow)
}
.row.user .bubble{background:linear-gradient(180deg, #102a1d, #0d2018); border-color:#1e4b36}
.meta{display:flex;gap:8px;align-items:center;color:var(--mut);font-size:11px;margin-top:4px}
.composer{display:grid;grid-template-columns:1fr auto;gap:10px;padding:12px;border-top:1px solid var(--stroke);background:linear-gradient(180deg,rgba(255,255,255,.00),rgba(255,255,255,.02))}
.input{padding:12px 14px;border-radius:12px;border:1px solid var(--stroke);background:#0d1233;color:var(--txt)}
.send{background:linear-gradient(180deg,var(--brand),var(--brand-2));color:#042216;border:none;padding:10px 16px;border-radius:12px;cursor:pointer;font-weight:800}

.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(3,6,20,.55);backdrop-filter:blur(4px);z-index:40}
.modal{display:none;position:fixed;inset:0;z-index:50;place-items:center}
.modal.on,.modal-backdrop.on{display:grid}
.modal-card{
  width:min(640px,94vw);background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,.00));
  color:var(--txt);border:1px solid var(--stroke);border-radius:18px;box-shadow:var(--shadow);padding:18px
}
.modal-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.modal-title{font-weight:900;font-size:16px}
.xbtn{border:1px solid var(--stroke);background:transparent;color:var(--txt);border-radius:10px;padding:6px 10px;cursor:pointer}
.form-row{display:grid;gap:6px;margin:10px 0}
.form-row input{padding:10px 12px;border-radius:12px;border:1px solid var(--stroke);background:#0d1233;color:var(--txt)}
.form-actions{display:flex;gap:8px;justify-content:flex-end}
.badge{background:var(--chip);color:#cfe6ff;border:1px solid var(--stroke);border-radius:999px;padding:4px 10px;font-size:12px}
</style>
</head><body>
  <div class="topbar">
    <div class="logo">KC</div>
    <div class="brand">Kind Coach</div>
    <div class="grow"></div>
    <span class="chip" id="plan-chip">Free</span>
    <span class="chip" id="me">Not signed in</span>
    <button id="download-txt" class="tb-btn">.txt</button>
    <button id="download-csv" class="tb-btn">.csv</button>
    <button id="download-json" class="tb-btn">.json</button>
    <button id="open-pricing" class="tb-btn">Pricing</button>
    <button id="open-settings" class="tb-btn">Account</button>
  </div>

  <div class="app">
    <aside class="sidebar">
      <div class="side-head"><div style="font-weight:700;">Your Space</div></div>
      <div class="side-actions">
        <button id="new-chat" class="tb-btn primary">New chat</button>
        <button id="coach-daily" class="tb-btn">Daily reflection</button>
        <button id="coach-start" class="tb-btn">Start coaching</button>
      </div>
      <div class="list" id="sessions"></div>
    </aside>

    <main class="main">
      <div class="chatbar">
        <div class="auth" id="auth" style="display:flex;gap:8px;align-items:center;">
          <button id="open-auth" class="tb-btn primary">Sign in / up</button>
          <button id="logout" class="tb-btn" style="display:none;">Log out</button>
          <span id="limits" class="badge"></span>
        </div>
      </div>
      <section class="chat" id="chat"></section>
      <div class="composer">
        <input id="message" class="input" autocomplete="off" placeholder="Sign in to start" disabled />
        <button id="send" class="send" disabled>Send</button>
      </div>
    </main>
  </div>

  <!-- Auth Modal -->
  <div class="modal-backdrop" id="auth-backdrop"></div>
  <div class="modal" id="auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-title">
    <div class="modal-card">
      <div class="modal-header"><div class="modal-title" id="auth-title">Welcome</div><button class="xbtn" id="auth-close">Close</button></div>
      <div class="tabs" style="display:flex;gap:8px;margin-bottom:8px;">
        <button id="tab-login"  class="tb-btn" aria-selected="true">Log in</button>
        <button id="tab-signup" class="tb-btn">Sign up</button>
      </div>
      <div id="pane-login">
        <div class="form-row"><label>Email</label><input id="login-email" placeholder="you@example.com"/></div>
        <div class="form-row"><label>Password</label><input id="login-password" type="password" placeholder="••••••••"/></div>
        <div class="form-actions"><button class="xbtn" id="login-cancel">Cancel</button><button class="tb-btn primary" id="login-submit">Log in</button></div>
      </div>
      <div id="pane-signup" style="display:none;">
        <div class="form-row"><label>Email</label><input id="signup-email" placeholder="you@example.com"/></div>
        <div class="form-row"><label>Password</label><input id="signup-password" type="password" placeholder="Create a password"/></div>
        <div class="form-actions"><button class="xbtn" id="signup-cancel">Cancel</button><button class="tb-btn primary" id="signup-submit">Create account</button></div>
      </div>
    </div>
  </div>

  <!-- Account / Preferences Modal -->
  <div class="modal-backdrop" id="settings-backdrop"></div>
  <div class="modal" id="settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title">
    <div class="modal-card">
      <div class="modal-header"><div class="modal-title" id="settings-title">Account & Preferences</div><button class="xbtn" id="settings-close">Close</button></div>
      <div class="form-row"><label>Display name</label><input id="display-name" placeholder="e.g., Alex"/></div>
      <div class="form-row"><label>Timezone</label><input id="pref-timezone" placeholder="e.g., Europe/London"/></div>
      <div class="form-row"><label>Retain memories</label><input id="pref-retain" type="checkbox"/></div>
      <div class="form-row"><label>Chat retention (days)</label><input id="pref-retention" type="number" value="90" min="1"/></div>
      <div class="form-actions" style="justify-content:flex-start;margin-bottom:12px;"><button class="tb-btn primary" id="save-prefs">Save</button></div>

      <hr style="border:none;border-top:1px solid #d1d7db;margin:8px 0 12px;">
      <div class="form-row"><label>Add a memory</label><input id="new-memory" placeholder="e.g., I prefer short replies"/></div>
      <div class="form-actions" style="justify-content:flex-start"><button class="tb-btn primary" id="add-memory">Add memory</button></div>
      <div class="form-row"><label>Saved memories</label><div id="memories-list" style="display:flex;flex-direction:column;gap:6px;"></div></div>
    </div>
  </div>

<script>
const chat=document.getElementById('chat'); const input=document.getElementById('message'); const send=document.getElementById('send');
const sessionsEl=document.getElementById('sessions'); const meSpan=document.getElementById('me'); const planChip=document.getElementById('plan-chip'); const limits=document.getElementById('limits');
function md(x){const esc=x.replace(/[&<>]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch])); const withLinks=esc.replace(/(https?:\/\/\\S+)/g,'<a href="$1" target="_blank" rel="noopener">$1</a>'); return withLinks.replace(/\\n/g,'<br/>');}
function addBubble(text, who){const row=document.createElement('div'); row.className='row '+who; const av=document.createElement('div'); av.className='avatar'; av.textContent=(who==='bot'?'KC':'You'); const b=document.createElement('div'); b.className='bubble'; b.innerHTML=md(text); const meta=document.createElement('div'); meta.className='meta'; meta.textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); if(who==='bot'){row.appendChild(av); const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta); row.appendChild(wrap);} else {const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta); row.appendChild(wrap); row.style.gridTemplateColumns='1fr auto';} chat.appendChild(row); chat.scrollTop=chat.scrollHeight;}
function makeBotBubble(initial='…'){const row=document.createElement('div'); row.className='row bot'; const av=document.createElement('div'); av.className='avatar'; av.textContent='KC'; const b=document.createElement('div'); b.className='bubble'; b.textContent=initial; const meta=document.createElement('div'); meta.className='meta'; meta.textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); row.appendChild(av); const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta); row.appendChild(wrap); chat.appendChild(row); chat.scrollTop=chat.scrollHeight; return b;}

async function refreshMe(){const r=await fetch('/api/me'); const d=await r.json(); if(d.user){ input.disabled=false; send.disabled=false; input.placeholder="Type a message"; meSpan.textContent=d.user.display_name||d.user.email; } else { input.disabled=true; send.disabled=true; input.placeholder="Sign in to start"; meSpan.textContent="Not signed in"; }}
async function refreshLimits(){const r=await fetch('/api/limits'); const d=await r.json(); planChip.textContent=`${d.plan_name}`; limits.textContent=`Chat ${d.used_today}/${d.chat_daily} • Coach ${d.coach_used}/${d.coach_daily}`; document.getElementById('download-csv').disabled=!d.allow_export_csv;}

document.getElementById('open-pricing').onclick=()=>location.href='/pricing';
document.getElementById('download-txt').onclick=()=>{location.href='/api/export?fmt=txt';};
document.getElementById('download-csv').onclick=()=>{location.href='/api/export?fmt=csv';};
document.getElementById('download-json').onclick=()=>{location.href='/api/chat/export';};

async function loadSessions(){const r=await fetch('/api/sessions'); const data=await r.json(); sessionsEl.innerHTML=''; (data.sessions||[]).forEach(s=>{ const el=document.createElement('div'); el.className='item'+(data.active===s.id?' active':''); el.textContent=s.title||'Untitled'; el.onclick=async()=>{ await fetch('/api/session/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:s.id})}); await loadHistory(); await loadSessions(); }; const del=document.createElement('button'); del.className='xbtn'; del.textContent='Delete'; del.style.marginLeft='8px'; del.onclick=async(e)=>{e.stopPropagation(); if(!confirm('Delete this chat?'))return; await fetch('/api/sessions/'+s.id,{method:'DELETE'}); await loadSessions(); await loadHistory();}; el.appendChild(del); sessionsEl.appendChild(el); });}
async function loadHistory(){const r=await fetch('/api/history'); const data=await r.json(); chat.innerHTML=''; (data.messages||[]).forEach(m=>addBubble(m.content, m.role==='assistant'?'bot':'user'));}

async function sendMessage(){const msg=input.value.trim(); if(!msg) return; input.value=''; addBubble(msg,'user'); const res=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})}); if(res.status===401){ openAuth('login'); return; } if(!res.ok){ addBubble('Error: '+(await res.text()),'bot'); return; } const reader=res.body.getReader(); const decoder=new TextDecoder(); let buf='', acc=''; const bubbleEl=makeBotBubble('…'); while(true){ const {value,done}=await reader.read(); if(done) break; buf+=decoder.decode(value,{stream:true}); const parts=buf.split("\\n\\n"); buf=parts.pop()||''; for(const part of parts){ if(!part.startsWith('data:')) continue; const raw=part.slice(5).trim(); if(raw==='[DONE]') continue; const chunk=raw.replace(/\\n/g,'\\n'); acc+=chunk; bubbleEl.innerHTML=md(acc); chat.scrollTop=chat.scrollHeight; }} await refreshLimits(); }
send.onclick=sendMessage; input.addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); sendMessage(); } });

document.getElementById('new-chat').onclick=async()=>{ const title=prompt('Name your chat (optional):','New chat')||'New chat'; const r=await fetch('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})}); if(r.ok){ await loadSessions(); await loadHistory(); }};
document.getElementById('coach-daily').onclick=async()=>{ const r=await fetch('/api/coaching/daily'); const d=await r.json(); addBubble('Daily reflection:\\n\\n'+d.prompt,'bot'); };
document.getElementById('coach-start').onclick=async()=>{ const topic=prompt('What would you like coaching on today?'); if(!topic) return; const r=await fetch('/api/coaching/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({topic})}); const d=await r.json(); addBubble(d.opening,'bot'); };

const authModal=document.getElementById('auth-modal'); const authBackdrop=document.getElementById('auth-backdrop'); const authClose=document.getElementById('auth-close');
const tabLogin=document.getElementById('tab-login'); const tabSignup=document.getElementById('tab-signup'); const paneLogin=document.getElementById('pane-login'); const paneSignup=document.getElementById('pane-signup');
function showLogin(){paneLogin.style.display=''; paneSignup.style.display='none'; tabLogin.classList.add('primary'); tabSignup.classList.remove('primary');}
function showSignup(){paneLogin.style.display='none'; paneSignup.style.display=''; tabSignup.classList.add('primary'); tabLogin.classList.remove('primary');}
function openAuth(which='login'){authModal.classList.add('on'); authBackdrop.classList.add('on'); (which==='signup'?showSignup():showLogin());}
function closeAuth(){authModal.classList.remove('on'); authBackdrop.classList.remove('on');}
document.getElementById('open-auth').onclick=()=>openAuth('login'); authClose.onclick=closeAuth; authBackdrop.onclick=closeAuth;
document.getElementById('login-cancel').onclick=closeAuth; document.getElementById('signup-cancel').onclick=closeAuth;

const le=document.getElementById('login-email'); const lp=document.getElementById('login-password');
const se=document.getElementById('signup-email'); const sp=document.getElementById('signup-password');

document.getElementById('login-submit').onclick=async()=>{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:le.value,password:lp.value})}); const d=await r.json(); if(!r.ok){alert(d.error||'Login failed'); return;} closeAuth(); addBubble('Signed in.','bot'); await refreshMe(); await refreshLimits(); await loadSessions(); await loadHistory();};
document.getElementById('signup-submit').onclick=async()=>{const r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:se.value,password:sp.value})}); const d=await r.json(); if(!r.ok){alert(d.error||'Signup failed'); return;} closeAuth(); addBubble('Account created. 👋','bot'); await refreshMe(); await refreshLimits(); await loadSessions(); await loadHistory();};
document.getElementById('logout').onclick=async()=>{await fetch('/api/logout',{method:'POST'}); addBubble('Signed out.','bot'); await refreshMe(); await refreshLimits(); await loadSessions(); chat.innerHTML='';};

const settingsModal=document.getElementById('settings-modal'); const settingsBackdrop=document.getElementById('settings-backdrop'); const settingsClose=document.getElementById('settings-close'); const openSettingsBtn=document.getElementById('open-settings');
const displayNameInput=document.getElementById('display-name'); const tzInput=document.getElementById('pref-timezone'); const retainInput=document.getElementById('pref-retain'); const retentionInput=document.getElementById('pref-retention');
const savePrefsBtn=document.getElementById('save-prefs'); const newMemoryInput=document.getElementById('new-memory'); const addMemoryBtn=document.getElementById('add-memory'); const memoriesList=document.getElementById('memories-list');

function openSettings(){settingsModal.classList.add('on'); settingsBackdrop.classList.add('on'); fetch('/api/me').then(r=>r.json()).then(d=>{ if(d.user){ displayNameInput.value=d.user.display_name||''; } }); fetch('/api/preferences').then(r=>r.json()).then(p=>{ if(p && !p.error){ tzInput.value=p.timezone||''; retainInput.checked=!!p.retain_memories; retentionInput.value=p.chat_retention_days||90; }}); loadMemories();}
function closeSettings(){settingsModal.classList.remove('on'); settingsBackdrop.classList.remove('on');}
openSettingsBtn.onclick=openSettings; settingsClose.onclick=closeSettings; settingsBackdrop.onclick=closeSettings;

savePrefsBtn.onclick=async()=>{const body={ display_name:displayNameInput.value.trim(), timezone:tzInput.value.trim(), retain_memories:retainInput.checked, chat_retention_days:parseInt(retentionInput.value||'90',10)}; const r=await fetch('/api/preferences',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); if(!r.ok){alert('Failed to save');return;} alert('Saved!'); await refreshMe();};

async function loadMemories(){memoriesList.innerHTML='Loading...'; const r=await fetch('/api/memories'); if(r.status===401){memoriesList.innerHTML='Sign in to manage memories.'; return;} const d=await r.json(); memoriesList.innerHTML=''; (d.memories||[]).forEach(m=>{ const row=document.createElement('div'); row.style.display='flex'; row.style.gap='8px'; row.style.alignItems='center'; const span=document.createElement('span'); span.textContent=m.note; const del=document.createElement('button'); del.className='xbtn'; del.textContent='Delete'; del.onclick=async()=>{ if(!confirm('Delete this memory?')) return; const rr=await fetch('/api/memories/'+m.id,{method:'DELETE'}); const dd=await rr.json(); if(dd.ok){ loadMemories(); } }; row.appendChild(span); row.appendChild(del); memoriesList.appendChild(row); }); if(!memoriesList.innerHTML){ memoriesList.textContent='No memories yet.'; }}
addMemoryBtn.onclick=async()=>{const note=newMemoryInput.value.trim(); if(!note) return; const r=await fetch('/api/memories',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note})}); const d=await r.json(); if(!r.ok){alert(d.error||'Failed to add'); return;} newMemoryInput.value=''; loadMemories(); refreshLimits();};

document.addEventListener('DOMContentLoaded', async ()=>{ await refreshMe(); await refreshLimits(); await loadSessions(); await loadHistory(); const params=new URLSearchParams(location.search); const mode=params.get('mode'); if(mode==='signup'||mode==='login'){openAuth(mode);} });
</script>
</body></html>
"""

# ---------- Pricing (HTML wrapper) ----------
PRICING_TOP = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Pricing — Kind Coach</title><link rel="icon" href="/static/favicon.ico">
# [759–772] REPLACE the entire <style>...</style> in PRICING_TOP with:
<style>
:root{
  --bg:#0f1221; --txt:#eaf2ff; --mut:#8ea1b3; --stroke:#2a2f52; --card:#121632; --brand:#6ef3a5; --brand-2:#30d695; --chip:#1c2246; --shadow:0 10px 40px rgba(0,0,0,.35)
}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,var(--bg),#0c1020);color:var(--txt);font:15px/1.55 Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial}
.topbar{
  height:64px;display:flex;align-items:center;gap:12px;padding:0 16px;
  background:linear-gradient(135deg, rgba(110,243,165,.12), rgba(48,214,149,.06));color:var(--txt);
  border-bottom:1px solid var(--stroke);box-shadow:var(--shadow)
}
.logo{width:36px;height:36px;border-radius:10px;background:linear-gradient(180deg,var(--brand),var(--brand-2));display:grid;place-items:center;color:#042216;font-weight:900}
.brand{font-weight:900}.grow{flex:1}
.tb-btn{background:transparent;color:var(--txt);border:1px solid var(--stroke);padding:8px 12px;border-radius:12px;cursor:pointer}
.wrap{max-width:1120px;margin:22px auto;padding:0 16px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{
  background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,.00));
  border:1px solid var(--stroke);border-radius:16px;padding:18px;box-shadow:var(--shadow)
}
.notice{background:rgba(255,214,102,.08);border:1px solid #ffe69c;border-radius:12px;padding:12px;margin:10px 0;color:#f8d37a}
.small{color:var(--mut);font-size:13px}
.price{font-size:24px;font-weight:900;margin-bottom:8px}
</style>
</head><body>
<div class="topbar"><div class="logo">KC</div><div class="brand">Kind Coach · Pricing</div><div class="grow"></div>
<button onclick="location.href='/'" class="tb-btn">Home</button>
<button onclick="location.href='/app'" class="tb-btn">Open App</button></div>
<div class="wrap">
<div class="card" style="margin-bottom:14px"><h2 style="margin:0 0 6px 0">Choose your plan</h2><p class="small">Secure billing by Stripe. Change or cancel anytime in the Billing Portal.</p>{portal}{warn}</div>
<div class="grid">
"""
PRICING_BOTTOM = r"""</div><p class="small" style="margin-top:14px">50% of proceeds donated to Samaritans.</p></div></body></html>"""

def pricing_card_html(plan_key: str, current: str, signed_in: bool) -> str:
    p = PLANS[plan_key]
    feats = [
        f"{p['chat_daily']} chat messages/day",
        f"{p['mem_limit']} memories",
        ("CSV export" if p["allow_export_csv"] else "No CSV export"),
        f"Daily coaching prompts: {p['coach_daily']}/day",
        f"Model tuned for {p['name']}",
    ]
    ul = "".join(f"<li>• {f}</li>" for f in feats)
    if not signed_in:
        cta = "<button class='tb-btn' onclick=\"location.href='/app?mode=signup'\">Sign up to choose</button>"
    elif plan_key == current:
        cta = "<span class='small'>Current plan</span>"
    else:
        if plan_key == "free":
            cta = "<form method='post' action='/billing/portal'><button class='tb-btn'>Open Billing Portal</button></form>"
        else:
            cta = f"<form method='post' action='/billing/checkout'><input type='hidden' name='plan' value='{plan_key}'/><button class='tb-btn'>Subscribe to {p['name']}</button></form>"
    return f"<div class='card'><h3>{p['name']}</h3><div class='price'>{p['price']}</div><ul class='small' style='list-style:none;padding-left:0'>{ul}</ul>{cta}</div>"

# ---------- Routes: pages ----------
@app.get("/", response_class=HTMLResponse)
def landing():
    return HTMLResponse(LANDING_HTML)

@app.get("/app", response_class=HTMLResponse)
def app_page():
    return HTMLResponse(APP_HTML)

# ---------- Auth ----------
@app.post("/api/register")
async def api_register(request: Request):
    d = await request.json()
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    if not email or not password:
        return JSONResponse({"error": "Email and password required"}, status_code=400)
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?,?,?,?)",
                    (email, hash_password(password), email.split("@")[0], datetime.utcnow().isoformat()))
        uid = cur.lastrowid
        cur.execute("INSERT OR IGNORE INTO preferences (user_id) VALUES (?)", (uid,))
        conn.commit()
    except sqlite3.IntegrityError:
        return JSONResponse({"error": "Email already in use"}, status_code=409)
    finally:
        conn.close()
    request.session["user_id"] = uid
    ensure_subscription_row(uid)
    # seed first chat session
    sid = str(uuid.uuid4())
    conn = db(); conn.execute("INSERT INTO sessions (id,user_id,title,created_at) VALUES (?,?,?,?)",
                              (sid, uid, "Welcome", time.time())); conn.commit(); conn.close()
    request.session["active_session"] = sid
    return JSONResponse({"ok": True})

@app.post("/api/login")
async def api_login(request: Request):
    d = await request.json()
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    conn = db()
    r = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not r or not check_password(password, r["password_hash"]):
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    request.session["user_id"] = r["id"]
    ensure_subscription_row(r["id"])
    # pick latest session or create one
    conn = db()
    s = conn.execute("SELECT id FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 1", (r["id"],)).fetchone()
    if not s:
        sid = str(uuid.uuid4())
        conn.execute("INSERT INTO sessions (id,user_id,title,created_at) VALUES (?,?,?,?)",
                     (sid, r["id"], "New chat", time.time()))
        conn.commit()
    else:
        sid = s["id"]
    conn.close()
    request.session["active_session"] = sid
    return JSONResponse({"ok": True})

@app.post("/api/logout")
async def api_logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})

@app.get("/api/me")
def api_me(request: Request):
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"user": None})
    conn = db()
    u = conn.execute("SELECT id,email,display_name FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    plan = get_subscription(uid)
    return JSONResponse({"user": {"id": u["id"], "email": u["email"], "display_name": u["display_name"]}, "plan": plan})

# ---------- Account / Preferences ----------
@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    uid = current_user_id(request)
    if not uid:
        return RedirectResponse(url="/app?mode=login", status_code=302)

    user = get_user(uid)
    prefs = get_or_create_preferences(uid)

    user_view = {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "preferences": {
            "timezone": prefs["timezone"],
            "dark_mode": bool(prefs["dark_mode"]),
            "notifications": bool(prefs["notifications"]),
            "save_memories": bool(prefs["save_memories"]),
        }
    }
    return templates.TemplateResponse("account.html", {"request": request, "user": user_view})
@app.post("/account/update")
async def account_update(request: Request, display_name: str = Form(...)):
    uid = current_user_id(request)
    if not uid:
        return RedirectResponse(url="/app?mode=login", status_code=302)

    display_name = (display_name or "").strip()
    if not display_name:
        return RedirectResponse(url="/account", status_code=302)

    conn = db()
    conn.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, uid))
    conn.commit()
    conn.close()

    return RedirectResponse(url="/account", status_code=302)

@app.post("/account/preferences")
async def account_preferences(
    request: Request,
    dark_mode: Optional[str] = Form(None),
    notifications: Optional[str] = Form(None),
    save_memories: Optional[str] = Form(None),
    timezone: Optional[str] = Form("UTC"),
):
    uid = current_user_id(request)
    if not uid:
        return RedirectResponse(url="/app?mode=login", status_code=302)

    # HTML checkboxes: present = "on", missing = None
    dm = 1 if dark_mode else 0
    nt = 1 if notifications else 0
    sm = 1 if save_memories else 0
    tz = (timezone or "UTC").strip() or "UTC"

    update_preferences(uid, tz, dm, nt, sm)
    return RedirectResponse(url="/account", status_code=302)

@app.get("/api/preferences")
def api_get_preferences(request: Request):
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    conn = db()
    conn.execute("INSERT OR IGNORE INTO preferences (user_id) VALUES (?)", (uid,))
    row = conn.execute("""
        SELECT timezone, dark_mode, notifications,
               COALESCE(retain_memories,1) AS retain_memories,
               COALESCE(chat_retention_days,90) AS chat_retention_days
        FROM preferences WHERE user_id=?
    """, (uid,)).fetchone()
    conn.close()
    return JSONResponse({
        "timezone": row["timezone"], "dark_mode": row["dark_mode"], "notifications": row["notifications"],
        "retain_memories": row["retain_memories"], "chat_retention_days": row["chat_retention_days"],
    })

@app.post("/api/preferences")
async def api_update_preferences(request: Request):
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error":"Unauthorized"}, status_code=401)
    d = await request.json()
    tz  = (d.get("timezone") or "").strip() or None
    dm  = 1 if d.get("dark_mode") else 0
    nt  = 1 if d.get("notifications") else 0
    rm  = 1 if d.get("retain_memories") else 0
    crt = int(d.get("chat_retention_days") or 90)
    conn = db()
    conn.execute("INSERT OR IGNORE INTO preferences (user_id) VALUES (?)", (uid,))
    conn.execute("""
        UPDATE preferences
           SET timezone = COALESCE(?, timezone),
               dark_mode = ?,
               notifications = ?,
               retain_memories = ?,
               chat_retention_days = ?
         WHERE user_id = ?
    """, (tz, dm, nt, rm, crt, uid))
    if "display_name" in d:
        conn.execute("UPDATE users SET display_name=? WHERE id=?", ((d["display_name"] or "").strip(), uid))
    conn.commit()
    # retention enforcement (delete non-saved sessions older than N days)
    cutoff = time.time() - (crt * 86400)
    conn.execute("""
        DELETE FROM sessions WHERE user_id=? AND saved=0 AND created_at < ?
    """, (uid, cutoff))
    conn.commit(); conn.close()
    return JSONResponse({"ok": True})

# ---------- Memories ----------
@app.get("/api/memories")
def api_list_memories(request: Request):
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error":"Unauthorized"}, status_code=401)
    conn = db()
    rows = conn.execute(
        "SELECT id, note, created_at FROM user_memories WHERE user_id=? ORDER BY created_at DESC",
        (uid,)
    ).fetchall()
    conn.close()
    return JSONResponse({"memories":[{"id":r["id"],"note":r["note"],"created_at":r["created_at"]} for r in rows]})

@app.post("/api/memories")
async def api_add_memory(request: Request):
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error":"Unauthorized"}, status_code=401)
    plan, cfg = plan_cfg(uid)
    if memories_count(uid) >= cfg["mem_limit"]:
        return JSONResponse({"error":"Memory limit reached. Upgrade for more."}, status_code=403)
    d = await request.json()
    note = (d.get("note") or "").strip()
    if not note:
        return JSONResponse({"error":"Note required"}, status_code=400)
    mid = str(uuid.uuid4())
    conn = db()
    conn.execute(
        "INSERT INTO user_memories (id, user_id, note, created_at) VALUES (?,?,?,?)",
        (mid, uid, note, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": mid})

@app.delete("/api/memories/{mem_id}")
def api_delete_memory(request: Request, mem_id: str):
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error":"Unauthorized"}, status_code=401)
    conn = db()
    cur = conn.execute("DELETE FROM user_memories WHERE id=? AND user_id=?", (mem_id, uid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": cur.rowcount > 0})

# ---------- Sessions & History ----------
@app.get("/api/sessions")
def api_sessions(request: Request):
    uid = current_user_id(request)
    conn = db()
    rows = conn.execute("SELECT id, title, created_at, saved FROM sessions WHERE user_id=? ORDER BY created_at DESC", (uid or -1,)).fetchall()
    conn.close()
    active = request.session.get("active_session")
    return JSONResponse({"sessions":[{"id":r["id"],"title":r["title"],"created_at":r["created_at"],"saved":r["saved"]} for r in rows], "active": active})

@app.post("/api/sessions")
async def api_sessions_create(request: Request):
    uid = current_user_id(request)
    if not uid: return JSONResponse({"error":"Unauthorized"}, status_code=401)
    d = await request.json()
    title = (d.get("title") or "New chat").strip() or "New chat"
    sid = str(uuid.uuid4())
    conn = db(); conn.execute("INSERT INTO sessions (id,user_id,title,created_at) VALUES (?,?,?,?)",
                              (sid, uid, title, time.time())); conn.commit(); conn.close()
    request.session["active_session"] = sid
    return JSONResponse({"ok": True, "session_id": sid})

@app.post("/api/session/select")
async def api_session_select(request: Request):
    d = await request.json()
    sid = d.get("session_id")
    if not sid: return JSONResponse({"error":"session_id required"}, status_code=400)
    request.session["active_session"] = sid
    return JSONResponse({"ok": True})

@app.delete("/api/sessions/{sid}")
def api_delete_session(request: Request, sid: str):
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error":"Unauthorized"}, status_code=401)
    conn = db()
    owns = conn.execute("SELECT 1 FROM sessions WHERE id=? AND user_id=?", (sid, uid)).fetchone()
    if not owns:
        conn.close()
        return JSONResponse({"error":"Not found"}, status_code=404)
    conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
    cur = conn.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (sid, uid))
    conn.commit()
    conn.close()
    if request.session.get("active_session") == sid:
        request.session.pop("active_session", None)
    return JSONResponse({"ok": cur.rowcount > 0})

@app.get("/api/history")
def api_history(request: Request):
    uid = current_user_id(request)
    sid = request.session.get("active_session")
    if not uid or not sid: return JSONResponse({"messages": [], "session_id": None})
    conn = db()
    rows = conn.execute("SELECT role, content, ts FROM messages WHERE session_id=? ORDER BY ts ASC", (sid,)).fetchall()
    conn.close()
    return JSONResponse({"messages":[{"role":r["role"],"content":r["content"],"ts":r["ts"]} for r in rows], "session_id": sid})

# ---------- Chat (SSE stream) ----------
@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    uid = current_user_id(request)
    if not uid: return JSONResponse({"error":"Unauthorized"}, status_code=401)
    if not LLM_READY: return JSONResponse({"error":"LLM not configured"}, status_code=500)

    d = await request.json()
    msg = (d.get("message") or "").strip()
    if not msg: return JSONResponse({"error":"Empty message"}, status_code=400)

    plan, cfg = plan_cfg(uid)
    used = chat_messages_today(uid)
    if used >= cfg["chat_daily"]:
        def gen_limit():
            txt = "You've hit today's chat limit. It resets at midnight. See /pricing to upgrade."
            yield "data: " + txt.replace("\n","\\n") + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen_limit(), media_type="text/event-stream")

    sid = request.session.get("active_session")
    if not sid:
        sid = str(uuid.uuid4())
        conn = db(); conn.execute("INSERT INTO sessions (id,user_id,title,created_at) VALUES (?,?,?,?)",
                                  (sid, uid, "New chat", time.time())); conn.commit(); conn.close()
        request.session["active_session"] = sid

    guard = crisis_guard(msg)
    conn = db()
    conn.execute("INSERT INTO messages (session_id, role, content, ts) VALUES (?,?,?,?)", (sid, "user", msg, time.time()))
    conn.commit(); conn.close()
    if guard:
        conn = db(); conn.execute("INSERT INTO messages (session_id, role, content, ts) VALUES (?,?,?,?)",(sid, "assistant", guard, time.time())); conn.commit(); conn.close()
        def gen_safe():
            yield "data: " + guard.replace("\n","\\n") + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen_safe(), media_type="text/event-stream")

    mem_text = get_user_memories_text(uid, limit=cfg["context_notes"])
    messages = [
        {"role":"system","content": SYSTEM_PROMPT},
        {"role":"system","content": COACHING_STYLE},
        {"role":"system","content": current_time_note()},
    ]
    if mem_text: messages.append({"role":"system","content": mem_text})
    conn = db()
    hist = conn.execute("SELECT role, content FROM messages WHERE session_id=? ORDER BY ts DESC LIMIT 20", (sid,)).fetchall()
    conn.close()
    for r in reversed(hist):
        messages.append({"role": r["role"], "content": r["content"]})
    messages.append({"role":"user","content": msg})

    def stream():
        try:
            r = _client.chat.completions.create(model=cfg["model"], messages=messages, temperature=0.7, stream=True, max_tokens=cfg["max_tokens"])
            parts=[]
            for chunk in r:
                delta=None
                try:
                    delta = chunk.choices[0].delta.content
                except Exception:
                    try: delta = chunk.choices[0].message.content
                    except Exception: delta=None
                if not delta: continue
                parts.append(delta)
                yield "data: " + delta.replace("\n","\\n") + "\n\n"
            final = "".join(parts)
            conn = db(); conn.execute("INSERT INTO messages (session_id, role, content, ts) VALUES (?,?,?,?)",(sid, "assistant", final, time.time())); conn.commit(); conn.close()
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield "data: " + ("[Error] "+str(e)).replace("\n","\\n") + "\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")

# ---------- Coaching ----------
# === COACHING APIS — BEGIN ===
# Single source of truth for all coaching endpoints

from random import choice

# Rotating short prompts for reflection
DAILY_REFLECTIONS = [
    "What went well today, and why?",
    "What small win are you most proud of?",
    "What felt draining? One small tweak for tomorrow?",
    "What did you learn about yourself today?",
    "What support would make tomorrow easier?",
]

@app.get("/api/coaching/daily")
def api_coaching_daily(request: Request):
    """
    Returns one gentle reflection prompt. No auth required to read,
    but saving reflections needs auth via the /api/coaching/save route.
    """
    prompt = choice(DAILY_REFLECTIONS)
    return JSONResponse({"prompt": prompt})


@app.post("/api/coaching/start")
async def api_coaching_start(request: Request):
    """
    Starts a focused coaching thread on a user-provided topic.
    Persists a 'coaching' row so you can list/review later.
    Uses the user's plan/model settings.
    """
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    d = await request.json()
    topic = (d.get("topic") or "").strip()
    if not topic:
        return JSONResponse({"error": "Topic required"}, status_code=400)

    # Rate limit by plan (coach_daily)
    plan, cfg = plan_cfg(uid)
    used = coaching_sessions_today(uid)
    if used >= cfg["coach_daily"]:
        return JSONResponse({"error": "Coaching session limit reached for today. Upgrade for more."}, status_code=403)

    # Build an opening, short, human coaching intro via LLM
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": COACHING_STYLE},
        {"role": "system", "content": current_time_note()},
        {"role": "user", "content": f"My coaching topic today: {topic}"},
        {"role": "assistant", "content": "Thanks for sharing. What would 'good' look like 7 days from now? One or two sentences."},
    ]
    opening = llm_chat(cfg["model"], 200, messages) or \
        "Let’s begin: what would 'good' look like 7 days from now (one or two sentences)?"

    # Persist a record in 'coaching'
    conn = db()
    conn.execute(
        "INSERT INTO coaching (user_id, topic, reflections, created_at) VALUES (?,?,?,?)",
        (uid, topic, json.dumps({"opening": opening}), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

    return JSONResponse({"ok": True, "opening": opening})


@app.post("/api/coaching/save")
async def api_coaching_save(request: Request):
    """
    Save a note or reflection for a coaching topic.
    Useful for journaling or capturing next steps.
    """
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    d = await request.json()
    topic = (d.get("topic") or "").strip()
    notes = (d.get("notes") or "").strip()
    if not topic or not notes:
        return JSONResponse({"error":"topic and notes required"}, status_code=400)

    conn = db()
    conn.execute(
        "INSERT INTO coaching (user_id, topic, reflections, created_at) VALUES (?,?,?,?)",
        (uid, topic, notes, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.get("/api/coaching/list")
def api_coaching_list(request: Request):
    """
    List a user's recent coaching notes/sessions.
    """
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    conn = db()
    rows = conn.execute(
        "SELECT id, topic, reflections, created_at FROM coaching WHERE user_id=? ORDER BY created_at DESC",
        (uid,)
    ).fetchall()
    conn.close()

    out = [
        {"id": r["id"], "topic": r["topic"], "reflections": r["reflections"], "created_at": r["created_at"]}
        for r in rows
    ]
    return JSONResponse({"items": out})

# === COACHING APIS — END ===

# ---------- Limits + Export ----------
@app.get("/api/limits")
def api_limits(request: Request):
    uid = current_user_id(request)
    plan, cfg = plan_cfg(uid)
    used = chat_messages_today(uid) if uid else 0
    coach_used = coaching_sessions_today(uid) if uid else 0
    remaining = max(cfg["chat_daily"] - used, 0)
    return JSONResponse({
        "plan": plan, "plan_name": cfg["name"],
        "chat_daily": cfg["chat_daily"], "used_today": used, "remaining_today": remaining,
        "allow_export_csv": cfg["allow_export_csv"],
        "coach_daily": cfg["coach_daily"], "coach_used": coach_used
    })
import io, csv, zipfile

@app.get("/account/download-data")
def account_download_data(request: Request):
    uid = current_user_id(request)
    if not uid:
        return RedirectResponse(url="/app?mode=login", status_code=302)

    # Gather chats
    conn = db()
    chats = conn.execute("""
        SELECT s.id AS session_id, m.role, m.content, m.ts
        FROM messages m
        JOIN sessions s ON s.id=m.session_id
        WHERE s.user_id=?
        ORDER BY m.ts ASC
    """, (uid,)).fetchall()

    # Gather coaching
    coaching = conn.execute("""
        SELECT id, topic, reflections, created_at
        FROM coaching
        WHERE user_id=?
        ORDER BY created_at ASC
    """, (uid,)).fetchall()
    conn.close()

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        # chats.csv
        cbuf = io.StringIO()
        cw = csv.writer(cbuf)
        cw.writerow(["session_id", "role", "content", "time_utc"])
        for r in chats:
            cw.writerow([r["session_id"], r["role"], r["content"], datetime.utcfromtimestamp(r["ts"]).isoformat()+"Z"])
        zf.writestr("chats.csv", cbuf.getvalue())

        # coaching.csv
        kbuf = io.StringIO()
        kw = csv.writer(kbuf)
        kw.writerow(["id", "topic", "reflections", "created_at"])
        for r in coaching:
            kw.writerow([r["id"], r["topic"], r["reflections"], r["created_at"]])
        zf.writestr("coaching.csv", kbuf.getvalue())

    mem.seek(0)
    headers = {
        "Content-Disposition": 'attachment; filename="kindcoach_data.zip"',
        "Content-Type": "application/zip",
    }
    return PlainTextResponse(mem.read(), headers=headers)

@app.get("/api/export")
def api_export(request: Request, fmt: str = Query("txt", pattern="^(txt|csv)$")):
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    plan, cfg = plan_cfg(uid)
    if fmt == "csv" and not cfg["allow_export_csv"]:
        return JSONResponse(
            {"error": "CSV export is available on Plus & Pro."}, status_code=403
        )

    sid = request.session.get("active_session")
    if not sid:
        return JSONResponse({"error": "No session"}, status_code=400)

    conn = db()
    rows = conn.execute(
        "SELECT role, content, ts FROM messages WHERE session_id=? ORDER BY ts ASC",
        (sid,),
    ).fetchall()
    conn.close()

    now = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")

    if fmt == "txt":
        lines = []
        for r in rows:
            t = datetime.utcfromtimestamp(r["ts"]).isoformat() + "Z"
            who = "User" if r["role"] == "user" else (
                "Assistant" if r["role"] == "assistant" else r["role"]
            )
            lines.append(f"[{t}] {who}: {r['content']}")
        return PlainTextResponse(
            "\n".join(lines) + "\n",
            headers={
                "Content-Disposition": f'attachment; filename="kindcoach_{now}.txt"'
            },
        )

    # CSV
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["time_utc", "role", "content"])
    for r in rows:
        t = datetime.utcfromtimestamp(r["ts"]).isoformat() + "Z"
        w.writerow([t, r["role"], r["content"]])

    return PlainTextResponse(
        out.getvalue(),
        headers={
            "Content-Disposition": f'attachment; filename="kindcoach_{now}.csv"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )

@app.get("/api/chat/export")
def export_chat_json(request: Request):
    uid = current_user_id(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    sid = request.session.get("active_session")
    if not sid:
        return JSONResponse({"error": "No session"}, status_code=400)

    conn = db()
    rows = conn.execute(
        "SELECT role, content, ts FROM messages WHERE session_id=? ORDER BY ts ASC",
        (sid,),
    ).fetchall()
    conn.close()

    items = [
        {
            "time_utc": datetime.utcfromtimestamp(r["ts"]).isoformat() + "Z",
            "role": r["role"],
            "content": r["content"],
        }
        for r in rows
    ]
    return JSONResponse({"session_id": sid, "messages": items})


# ---------- Pricing page ----------
@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    uid = current_user_id(request)
    signed_in = bool(uid)
    current = get_subscription(uid)
    portal = ""
    if signed_in and STRIPE_READY:
        portal = "<form method='post' action='/billing/portal'><button class='tb-btn'>Open Billing Portal</button></form>"
    elif signed_in and not STRIPE_READY:
        portal = "<div class='notice'>Stripe is not configured.</div>"
    warn = ""
    if not prices_loaded():
        warn = "<div class='notice'>Stripe price IDs not configured (STRIPE_PRICE_PLUS / STRIPE_PRICE_PRO).</div>"

    cards = (
        pricing_card_html("free", current, signed_in) +
        pricing_card_html("plus", current, signed_in) +
        pricing_card_html("pro", current, signed_in)
    )
    return HTMLResponse(PRICING_TOP.replace("{portal}", portal).replace("{warn}", warn) + cards + PRICING_BOTTOM)

# ---------- Stripe helpers / routes ----------
def get_or_create_stripe_customer(user_row: sqlite3.Row) -> str:
    if not STRIPE_READY:
        raise RuntimeError("Stripe not configured")
    if user_row["stripe_customer_id"]:
        return user_row["stripe_customer_id"]
    cust = stripe.Customer.create(email=user_row["email"], metadata={"app_user_id": str(user_row["id"])})
    conn = db()
    conn.execute("UPDATE users SET stripe_customer_id=? WHERE id=?", (cust.id, user_row["id"]))
    conn.commit()
    conn.close()
    return cust.id

@app.post("/billing/checkout")
async def billing_checkout(request: Request):
    if not STRIPE_READY or not prices_loaded() or not PUBLIC_URL:
        return HTMLResponse("<h3>Stripe not configured</h3>", status_code=500)
    uid = current_user_id(request)
    if not uid:
        return RedirectResponse("/app?mode=login", status_code=303)
    user = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    form = await request.form()
    plan = (form.get("plan") or "").strip().lower()
    if plan not in ("plus","pro"):
        return HTMLResponse("<h3>Unknown plan</h3>", status_code=400)
    price_id = STRIPE_PRICE_PLUS if plan == "plus" else STRIPE_PRICE_PRO
    customer_id = get_or_create_stripe_customer(user)
    sess = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{PUBLIC_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_URL}/pricing",
        metadata={"app_user_id": str(uid), "app_plan": plan},
    )
    return RedirectResponse(sess.url, status_code=303)

@app.get("/billing/success", response_class=HTMLResponse)
def billing_success(session_id: str = Query(None)):
    msg = "<p class='small'>If your payment succeeded, your plan will update shortly.</p>"
    if STRIPE_READY and session_id:
        try:
            sess = stripe.checkout.Session.retrieve(session_id, expand=["subscription","line_items"])
            if sess and sess.subscription:
                msg = "<p class='small'>Subscription created. It may take a few seconds to reflect in the app.</p>"
        except Exception:
            pass
    return HTMLResponse(
        f"<div style='padding:20px;font-family:Inter,system-ui'><h2>Thanks!</h2>{msg}"
        f"<p><a href='/pricing'>Back to pricing</a> · <a href='/app'>Open app</a></p></div>"
    )

@app.post("/billing/portal")
def billing_portal(request: Request):
    if not STRIPE_READY or not PUBLIC_URL:
        return HTMLResponse("<h3>Stripe not configured</h3>", status_code=500)
    uid = current_user_id(request)
    if not uid:
        return RedirectResponse("/app?mode=login", status_code=303)
    user = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    portal = stripe.billing_portal.Session.create(
        customer=get_or_create_stripe_customer(user),
        return_url=f"{PUBLIC_URL}/pricing",
    )
    return RedirectResponse(portal.url, status_code=303)

@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_READY:
        return JSONResponse({"ok": True})
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=STRIPE_WEBHOOK_SECRET)
        else:
            event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)
    except Exception as e:
        return PlainTextResponse(f"Webhook error: {e}", status_code=400)

    etype = event["type"]
    data = event["data"]["object"]

    def find_user_by_customer(customer_id: str) -> Optional[sqlite3.Row]:
        conn = db()
        row = conn.execute("SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
        conn.close()
        return row

    try:
        if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            sess = data
            customer_id = sess.get("customer")
            sub_id = sess.get("subscription") if isinstance(sess.get("subscription"), str) else (sess.get("subscription") or {}).get("id")
            if customer_id and sub_id:
                subscription = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
                price_id = subscription["items"]["data"][0]["price"]["id"]
                plan = map_price_to_plan(price_id) or "free"
                user = find_user_by_customer(customer_id)
                if user:
                    set_plan(
                        user["id"], plan,
                        sub_id=subscription["id"], status=subscription["status"],
                        cpe=int(subscription["current_period_end"])
                    )

        elif etype in ("customer.subscription.created", "customer.subscription.updated"):
            sub = data
            customer_id = sub.get("customer")
            price_id = None
            try:
                price_id = sub["items"]["data"][0]["price"]["id"]
            except Exception:
                pass
            plan = map_price_to_plan(price_id) if price_id else None
            user = find_user_by_customer(customer_id) if customer_id else None
            if user:
                effective = (plan or "free") if sub["status"] in ("active","trialing","past_due") else "free"
                set_plan(
                    user["id"], effective,
                    sub_id=sub["id"], status=sub["status"],
                    cpe=int(sub["current_period_end"]) if sub.get("current_period_end") else None
                )

        elif etype in ("customer.subscription.deleted", "customer.subscription.cancelled"):
            sub = data
            customer_id = sub.get("customer")
            user = find_user_by_customer(customer_id) if customer_id else None
            if user:
                set_plan(user["id"], "free", sub_id=None, status="canceled", cpe=None)

    except Exception as e:
        print("Webhook handling error:", e, file=sys.stderr)

    return JSONResponse({"received": True})
    # ---------- Health ----------
@app.get("/health")
def health():
    """Health check endpoint for monitoring and uptime verification."""
    try:
        conn = db()
        conn.execute("SELECT 1")
        conn.close()
        return {
            "ok": True,
            "stripe": bool(STRIPE_READY),
            "prices_loaded": prices_loaded(),
            "time": datetime.utcnow().isoformat() + "Z"
        }
    except Exception as e:
        return PlainTextResponse(str(e), status_code=500)

@app.post("/account/clear-chats")
async def account_clear_chats(request: Request):
    uid = current_user_id(request)
    if not uid:
        return RedirectResponse(url="/app?mode=login", status_code=302)

    conn = db()
    # Delete messages belonging to user's sessions
    conn.execute("""
        DELETE FROM messages
        WHERE session_id IN (SELECT id FROM sessions WHERE user_id=?)
    """, (uid,))
    # Delete sessions
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/account", status_code=302)

# ---------- Main ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True
    )
