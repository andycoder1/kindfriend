# app.py
import os
import io
import csv
import json
import stripe
import sqlite3
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple

from fastapi import FastAPI, Request, Form, Query, Header
from fastapi.responses import (
    HTMLResponse, RedirectResponse, PlainTextResponse,
    JSONResponse, Response
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# ============== Config & Clients ==============
APP_NAME = "Coffee Chat — A Simple Companion App"

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_FREE = os.getenv("OPENAI_MODEL_FREE", "gpt-4o-mini")
OPENAI_MODEL_PLUS = os.getenv("OPENAI_MODEL_PLUS", "gpt-4o-mini")
OPENAI_MODEL_PRO  = os.getenv("OPENAI_MODEL_PRO",  "gpt-4o")

# Stripe
STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_PLUS      = os.getenv("STRIPE_PRICE_PLUS")  # £4.99
STRIPE_PRICE_PRO       = os.getenv("STRIPE_PRICE_PRO")   # £7.99
PUBLIC_URL             = os.getenv("PUBLIC_URL", "http://localhost:8000")

stripe.api_key = STRIPE_SECRET_KEY

# FastAPI
app = FastAPI(title=APP_NAME)
SECRET_KEY = os.getenv("APP_SECRET_KEY", "dev-secret-change-me")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten for prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ============== Password hashing ==============
try:
    import bcrypt
    def hash_password(p: str) -> str:
        return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
    def check_password(p: str, h: str) -> bool:
        try:
            return bcrypt.checkpw(p.encode(), h.encode())
        except Exception:
            return False
except Exception:
    import hashlib
    print("⚠ bcrypt not found — using SHA256 fallback (dev only)")
    def hash_password(p: str) -> str:
        return hashlib.sha256(p.encode()).hexdigest()
    def check_password(p: str, h: str) -> bool:
        return hashlib.sha256(p.encode()).hexdigest() == h

# ============== DB ==============
DB_PATH = os.getenv("APP_DB_PATH", "app.db")

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def table_has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == col for c in cols)

def init_db():
    conn = db(); cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            stripe_customer_id TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS preferences (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT DEFAULT 'UTC',
            dark_mode INTEGER DEFAULT 0,
            notifications INTEGER DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            mood TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    # Subscriptions
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            plan TEXT NOT NULL DEFAULT 'free',              -- free | plus | pro
            stripe_subscription_id TEXT,
            status TEXT,                                    -- active, trialing, past_due, canceled, unpaid, incomplete, incomplete_expired
            current_period_end TEXT,
            started_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    conn.commit(); conn.close()
init_db()

# ============== Plans ==============
PLANS = {
    "free": {
        "name": "Free",
        "price": "£0",
        "chat_daily": 15,
        "memory_limit": 100,
        "allow_search": False,
        "allow_export": False,
        "allow_mood": False,
        "model": OPENAI_MODEL_FREE,
        "max_tokens": 300,
        "context_notes": 4,
    },
    "plus": {
        "name": "Plus",
        "price": "£4.99/mo",
        "chat_daily": 200,
        "memory_limit": 1000,
        "allow_search": True,
        "allow_export": True,
        "allow_mood": True,
        "model": OPENAI_MODEL_PLUS,
        "max_tokens": 800,
        "context_notes": 8,
    },
    "pro": {
        "name": "Pro",
        "price": "£7.99/mo",
        "chat_daily": 2000,
        "memory_limit": 10000,
        "allow_search": True,
        "allow_export": True,
        "allow_mood": True,
        "model": OPENAI_MODEL_PRO,
        "max_tokens": 2000,
        "context_notes": 12,
    },
}
PRICE_TO_PLAN = {}  # filled after startup from env

def env_prices_loaded() -> bool:
    return bool(STRIPE_PRICE_PLUS and STRIPE_PRICE_PRO)

# ============== Session / helpers ==============
def current_user_id(request: Request) -> Optional[int]:
    return request.session.get("user_id")

def require_login(request: Request) -> Optional[RedirectResponse]:
    if not current_user_id(request):
        return RedirectResponse(url="/login", status_code=303)
    return None

def get_user(uid: int) -> Optional[sqlite3.Row]:
    if not uid: return None
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return user

def ensure_subscription_row(uid: int):
    conn = db()
    row = conn.execute("SELECT user_id FROM subscriptions WHERE user_id = ?", (uid,)).fetchone()
    if not row:
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
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET plan=excluded.plan,
                      stripe_subscription_id=excluded.stripe_subscription_id,
                      status=excluded.status,
                      current_period_end=excluded.current_period_end
    """, (uid, plan, sub_id, status, (str(cpe) if cpe else None), datetime.utcnow().isoformat()))
    conn.commit(); conn.close()

def get_subscription(uid: int) -> str:
    conn = db()
    row = conn.execute("SELECT plan, status FROM subscriptions WHERE user_id = ?", (uid,)).fetchone()
    conn.close()
    # Treat inactive/ended subs as free
    if not row: return "free"
    plan, status = row["plan"], (row["status"] or "")
    if plan in ("plus", "pro") and status not in ("active", "trialing", "past_due"):
        return "free"
    return plan if plan in PLANS else "free"

def plan_cfg(uid: int) -> Tuple[str, Dict[str, Any]]:
    plan = get_subscription(uid)
    return plan, PLANS[plan]

def todays_bounds_utc() -> Tuple[str, str]:
    today = date.today().isoformat()
    return f"{today}T00:00:00", f"{today}T23:59:59"

def chat_messages_today(uid: int) -> int:
    start, end = todays_bounds_utc()
    conn = db()
    cnt = conn.execute("""
        SELECT COUNT(*) as c
        FROM memory
        WHERE user_id = ? AND title = 'Chat note'
          AND created_at BETWEEN ? AND ?
    """, (uid, start, end)).fetchone()["c"]
    conn.close()
    return int(cnt)

def memory_count(uid: int) -> int:
    conn = db()
    cnt = conn.execute("SELECT COUNT(*) as c FROM memory WHERE user_id = ?", (uid,)).fetchone()["c"]
    conn.close()
    return int(cnt)

# ============== LLM ==============
_client = None
_llm_ready = bool(OPENAI_API_KEY)
if _llm_ready:
    try:
        from openai import OpenAI  # type: ignore
        _client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print("⚠ Could not init OpenAI client:", e)
        _llm_ready = False

def fetch_recent_memories(uid: int, limit: int) -> List[Dict[str, Any]]:
    conn = db()
    rows = conn.execute(
        "SELECT title, content, updated_at FROM memory WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (uid, limit),
    ).fetchall()
    conn.close()
    return [{"title": r["title"], "content": r["content"], "updated_at": r["updated_at"]} for r in rows]

def llm_reply(uid: int, user_text: str) -> str:
    plan, cfg = plan_cfg(uid)
    name = (get_user(uid) or {}).get("display_name") or "friend"
    memories = fetch_recent_memories(uid, limit=cfg["context_notes"])
    mem_bullets = "\n".join(f"- {m['title']}: {m['content'][:180].strip()}" for m in memories) or "- (no saved notes yet)"

    system = (
        "You are Coffee Chat, a gentle, supportive companion. "
        "Be concise, warm, and practical. If the user asks for help, suggest one small next step."
    )
    context = (
        f"User goes by: {name}\nRecent notes:\n{mem_bullets}\n"
        "Only use these notes if they help; otherwise ignore."
    )

    if not _llm_ready or _client is None:
        return (
            "I’m running in local/dev mode without an LLM key. "
            "Here’s an echo while we get set up:\n\n"
            f"“{user_text}”\n\nTip: set OPENAI_API_KEY / OPENAI_MODEL_* for real replies."
        )
    try:
        resp = _client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "system", "content": context},
                {"role": "user", "content": user_text},
            ],
            temperature=0.7,
            max_tokens=cfg["max_tokens"],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Sorry, I couldn’t reach the chat service ({e}). Please try again shortly."

# ============== Layout ==============
def layout(body: str, user: Optional[sqlite3.Row] = None, title: str = "Coffee Chat") -> str:
    dark = False
    plan_badge = ""
    if user:
        uid = user["id"]
        conn = db()
        pref = conn.execute("SELECT dark_mode FROM preferences WHERE user_id = ?", (uid,)).fetchone()
        conn.close()
        if pref and pref["dark_mode"]:
            dark = True
        plan = get_subscription(uid)
        plan_badge = f"<span class='badge'>{PLANS[plan]['name']}</span>"
    return f"""<!doctype html>
<html lang="en" {'data-theme="dark"' if dark else ''}>
<head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>
:root {{
  --bg:#f6f7fb; --card:#fff; --text:#222; --muted:#666; --accent:#6f4e37; --link:#3b82f6; --border:#e5e7eb;
}}
[data-theme="dark"] {{
  --bg:#0f1115; --card:#151821; --text:#eaeff7; --muted:#a5adba; --accent:#c7a17a; --link:#8ab4f8; --border:#2a2f3a;
}}
html,body {{ background:var(--bg); color:var(--text); margin:0; padding:0; font-family:system-ui,-apple-system,Segoe UI,Inter,Arial; }}
.container {{ max-width:980px; margin:0 auto; padding:24px; }}
.nav {{ display:flex; gap:14px; align-items:center; justify-content:space-between; margin-bottom:16px; }}
.nav a {{ color:var(--link); text-decoration:none; font-weight:600; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:20px; box-shadow:0 6px 20px rgba(0,0,0,0.04); }}
h1,h2,h3 {{ margin-top:0.2em; }}
label {{ display:block; margin-top:12px; font-weight:600; }}
input[type="text"],input[type="email"],input[type="password"],textarea,select {{
  width:100%; padding:10px; border-radius:10px; border:1px solid var(--border); background:transparent; color:var(--text);
}}
button {{ margin-top:14px; padding:10px 14px; border-radius:10px; border:1px solid var(--border); background:var(--accent); color:#fff; font-weight:700; cursor:pointer; }}
.small {{ color:var(--muted); font-size:.9em; }}
.hero {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; align-items:center; }}
.hero img {{ width:100%; border-radius:14px; border:1px solid var(--border); }}
nav .right a {{ margin-left:12px; }}
ul.clean {{ list-style:none; padding-left:0; }}
li.item {{ padding:10px 0; border-bottom:1px dashed var(--border); }}
a.btn-link {{ display:inline-block; margin-top:10px; text-decoration:none; color:#fff; background:var(--link); padding:8px 12px; border-radius:8px; font-weight:700; }}
.msg {{ white-space:pre-wrap; border:1px solid var(--border); border-radius:12px; padding:12px; margin:8px 0; }}
.msg.user {{ background:rgba(59,130,246,.08); }}
.msg.bot  {{ background:rgba(111,78,55,.08); }}
.badge {{ display:inline-block; margin-left:8px; padding:2px 8px; border-radius:10px; border:1px solid var(--border); font-size:12px; color:var(--muted); }}
.grid3 {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }}
.pricecard .price {{ font-size:28px; font-weight:800; }}
.notice {{ background:rgba(255,193,7,.12); border:1px solid rgba(255,193,7,.3); padding:10px; border-radius:10px; }}
</style>
</head>
<body>
<div class="container">
  <div class="nav">
    <div class="left"><a href="/">☕ Coffee Chat</a> {plan_badge}</div>
    <div class="right">
      {"<a href='/chat'>Chat</a> <a href='/memory'>Memory</a> <a href='/preferences'>Preferences</a> <a href='/pricing'>Pricing</a> <a href='/logout'>Log out</a>" if user else "<a href='/pricing'>Pricing</a> <a href='/signup'>Sign up</a> <a href='/login'>Log in</a>"}
    </div>
  </div>
  {body}
</div>
</body>
</html>"""

# ============== Auth & Core Pages ==============
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    uid = current_user_id(request); user = get_user(uid) if uid else None
    name = user["display_name"] if user and user["display_name"] else "friend"
    body = f"""
    <div class="card">
      <div class="hero">
        <div>
          <h1>Welcome {name} 👋</h1>
          <p class="small">A simple space to say how you feel, jot down thoughts, and keep gentle track of your days.</p>
          {"<a class='btn-link' href='/chat'>Open Chat</a> <a class='btn-link' href='/memory'>Your Memory</a>" if user else "<a class='btn-link' href='/signup'>Create an account</a> <a class='btn-link' href='/login'>I already have an account</a>"}
          <a class='btn-link' href='/pricing' style='background:#10b981'>See pricing</a>
        </div>
        <img src="/static/coffee-chat.png" alt="Coffee chat" onerror="this.style.display='none'">
      </div>
    </div>"""
    return layout(body, user)

@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    if current_user_id(request): return RedirectResponse("/", status_code=303)
    body = """
    <div class="card">
      <h2>Create your account</h2>
      <form method="post" action="/signup">
        <label>Email</label><input type="email" name="email" required />
        <label>Password</label><input type="password" name="password" minlength="6" required />
        <button type="submit">Sign up</button>
      </form>
      <p class="small">Already registered? <a href="/login">Log in</a></p>
    </div>"""
    return layout(body, None)

@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = db()
    try:
        conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email.strip().lower(), hash_password(password), datetime.utcnow().isoformat()),
        )
        uid = conn.execute("SELECT id FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO preferences (user_id, timezone, dark_mode, notifications) VALUES (?, 'UTC', 0, 1)", (uid,))
        conn.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan, status, started_at) VALUES (?, 'free', 'active', ?)", (uid, datetime.utcnow().isoformat()))
        conn.commit()
        request.session["user_id"] = uid
        return RedirectResponse("/name", status_code=303)
    except sqlite3.IntegrityError:
        conn.rollback()
        return HTMLResponse(layout("<div class='card'><h3>That email is already registered.</h3><p><a href='/login'>Log in</a> or try another email.</p></div>", None), status_code=400)
    finally:
        conn.close()

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user_id(request): return RedirectResponse("/", status_code=303)
    body = """
    <div class="card">
      <h2>Log in</h2>
      <form method="post" action="/login">
        <label>Email</label><input type="email" name="email" required />
        <label>Password</label><input type="password" name="password" required />
        <button type="submit">Log in</button>
      </form>
      <p class="small">New here? <a href="/signup">Create an account</a></p>
    </div>"""
    return layout(body, None)

@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    conn.close()
    if not user or not check_password(password, user["password_hash"]):
        return HTMLResponse(layout("<div class='card'><h3>Invalid email or password.</h3><p><a href='/login'>Try again</a></p></div>", None), status_code=401)
    request.session["user_id"] = user["id"]
    ensure_subscription_row(user["id"])
    if not user["display_name"]:
        return RedirectResponse("/name", status_code=303)
    return RedirectResponse("/", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)

@app.get("/name", response_class=HTMLResponse)
def name_form(request: Request):
    redir = require_login(request); 
    if redir: return redir
    uid = current_user_id(request); user = get_user(uid)
    body = f"""
    <div class="card">
      <h2>Choose how I address you</h2>
      <form method="post" action="/name">
        <label>Display name</label>
        <input type="text" name="display_name" value="{(user['display_name'] or '') if user else ''}" maxlength="50" />
        <button type="submit">Save name</button>
      </form>
    </div>"""
    return layout(body, user)

@app.post("/name")
def set_name(request: Request, display_name: str = Form("")):
    redir = require_login(request); 
    if redir: return redir
    uid = current_user_id(request)
    conn = db()
    conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name.strip() or None, uid))
    conn.commit(); conn.close()
    return RedirectResponse("/", status_code=303)

# ============== Pricing & Billing ==============
@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    uid = current_user_id(request); user = get_user(uid) if uid else None
    current = get_subscription(uid) if uid else "free"
    warn = "" if env_prices_loaded() else "<div class='notice small'>Stripe price IDs not configured. Set STRIPE_PRICE_PLUS and STRIPE_PRICE_PRO.</div>"
    cards = []
    for key in ["free", "plus", "pro"]:
        p = PLANS[key]
        features = [
            f"{p['chat_daily']} chat messages/day",
            f"{p['memory_limit']} memory notes",
            ("Memory search" if p['allow_search'] else "No memory search"),
            ("CSV export" if p['allow_export'] else "No export"),
            ("Mood on notes" if p['allow_mood'] else "No mood field"),
        ]
        feat_html = "".join(f"<li>• {f}</li>" for f in features)
        if uid:
            if key == current:
                cta = "<div class='small'>Current plan</div>"
            else:
                if key == "free":
                    cta = "<div class='small'>You can downgrade in the Billing Portal.</div>"
                else:
                    cta = f"<form method='post' action='/billing/checkout'><input type='hidden' name='plan' value='{key}' /><button type='submit'>Subscribe to {p['name']}</button></form>"
        else:
            cta = "<a class='btn-link' href='/signup'>Create an account</a>"
        cards.append(f"""
        <div class="card pricecard">
          <h3>{p['name']}</h3>
          <div class="price">{p['price']}</div>
          <ul class="clean">{feat_html}</ul>
          {cta}
        </div>""")
    portal_btn = ""
    if uid:
        portal_btn = "<form method='post' action='/billing/portal'><button type='submit'>Open Billing Portal</button></form>"
    body = f"""
    <div class="card">
      <h2>Choose your plan</h2>
      <p class="small">Manage your subscription any time in the Billing Portal.</p>
      {portal_btn}
      {warn}
    </div>
    <div class="grid3">{''.join(cards)}</div>"""
    return layout(body, user, title="Pricing — Coffee Chat")

def get_or_create_stripe_customer(user: sqlite3.Row) -> str:
    # reuse if stored
    if user["stripe_customer_id"]:
        return user["stripe_customer_id"]
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("Stripe not configured")
    # create customer
    cust = stripe.Customer.create(email=user["email"], metadata={"app_user_id": str(user["id"])})
    conn = db()
    conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", (cust.id, user["id"]))
    conn.commit(); conn.close()
    return cust.id

def plan_to_price_id(plan: str) -> Optional[str]:
    if plan == "plus":
        return STRIPE_PRICE_PLUS
    if plan == "pro":
        return STRIPE_PRICE_PRO
    return None

def price_id_to_plan(price_id: str) -> Optional[str]:
    if not PRICE_TO_PLAN:
        if STRIPE_PRICE_PLUS: PRICE_TO_PLAN[STRIPE_PRICE_PLUS] = "plus"
        if STRIPE_PRICE_PRO:  PRICE_TO_PLAN[STRIPE_PRICE_PRO]  = "pro"
    return PRICE_TO_PLAN.get(price_id)

@app.post("/billing/checkout")
def billing_checkout(request: Request, plan: str = Form(...)):
    redir = require_login(request); 
    if redir: return redir
    uid = current_user_id(request); user = get_user(uid)
    if plan not in ("plus", "pro"):
        return HTMLResponse(layout("<div class='card'><h3>Unknown plan.</h3></div>", user), status_code=400)
    price_id = plan_to_price_id(plan)
    if not (STRIPE_SECRET_KEY and price_id and PUBLIC_URL):
        return HTMLResponse(layout("<div class='card'><h3>Stripe not configured. Set STRIPE_SECRET_KEY, PUBLIC_URL and STRIPE_PRICE_*.</h3></div>", user), status_code=500)
    customer_id = get_or_create_stripe_customer(user)

    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{PUBLIC_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{PUBLIC_URL}/pricing",
        metadata={"app_user_id": str(uid), "app_plan": plan},
    )
    return RedirectResponse(session.url, status_code=303)

@app.get("/billing/success", response_class=HTMLResponse)
def billing_success(request: Request, session_id: str = Query(None)):
    uid = current_user_id(request); user = get_user(uid) if uid else None
    info = "<p class='small'>If your payment succeeded, your plan will update automatically.</p>"
    if STRIPE_SECRET_KEY and session_id:
        try:
            sess = stripe.checkout.Session.retrieve(session_id, expand=["subscription", "line_items"])
            if sess and sess.subscription:
                info = "<p class='small'>Subscription created. It may take a few seconds to reflect here.</p>"
        except Exception:
            pass
    body = f"""
    <div class="card">
      <h2>Thanks!</h2>
      {info}
      <p><a href="/pricing">Back to pricing</a></p>
    </div>"""
    return layout(body, user)

@app.post("/billing/portal")
def billing_portal(request: Request):
    redir = require_login(request); 
    if redir: return redir
    uid = current_user_id(request); user = get_user(uid)
    if not STRIPE_SECRET_KEY or not PUBLIC_URL:
        return HTMLResponse(layout("<div class='card'><h3>Stripe not configured.</h3></div>", user), status_code=500)
    customer_id = get_or_create_stripe_customer(user)
    portal = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{PUBLIC_URL}/pricing",
    )
    return RedirectResponse(portal.url, status_code=303)

# ============== Webhook ==============
@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        # Unsafe: accept all (dev only)
        payload = await request.body()
        event = None
        try:
            event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)
        except Exception as e:
            return PlainTextResponse(f"Invalid payload: {e}", status_code=400)
    else:
        sig = request.headers.get("stripe-signature")
        payload = await request.body()
        try:
            event = stripe.Webhook.construct_event(
                payload=payload, sig_header=sig, secret=STRIPE_WEBHOOK_SECRET
            )
        except Exception as e:
            return PlainTextResponse(f"Webhook error: {e}", status_code=400)

    # Handle
    etype = event["type"]
    data = event["data"]["object"]

    # Helper: map Stripe customer -> app user
    def find_user_by_customer(customer_id: str) -> Optional[sqlite3.Row]:
        conn = db()
        row = conn.execute("SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)).fetchone()
        conn.close()
        return row

    try:
        if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            # On completed checkout, get subscription, map to plan, set subscription row
            sess = data
            customer_id = sess.get("customer")
            sub_id = sess.get("subscription") if isinstance(sess.get("subscription"), str) else (sess.get("subscription") or {}).get("id")
            if customer_id and sub_id:
                # fetch subscription for price
                subscription = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
                price_id = subscription["items"]["data"][0]["price"]["id"]
                plan = price_id_to_plan(price_id) or "free"
                user = find_user_by_customer(customer_id)
                if user:
                    set_plan(
                        uid=user["id"],
                        plan=plan,
                        sub_id=subscription["id"],
                        status=subscription["status"],
                        cpe=int(subscription["current_period_end"]),
                    )

        elif etype in ("customer.subscription.created", "customer.subscription.updated"):
            sub = data
            customer_id = sub.get("customer")
            price_id = None
            try:
                price_id = sub["items"]["data"][0]["price"]["id"]
            except Exception:
                pass
            plan = price_id_to_plan(price_id) if price_id else None
            user = find_user_by_customer(customer_id) if customer_id else None
            if user:
                set_plan(
                    uid=user["id"],
                    plan=(plan or "free") if sub["status"] in ("active", "trialing", "past_due") else "free",
                    sub_id=sub["id"],
                    status=sub["status"],
                    cpe=int(sub["current_period_end"]) if sub.get("current_period_end") else None,
                )

        elif etype in ("customer.subscription.deleted", "customer.subscription.cancelled"):
            sub = data
            customer_id = sub.get("customer")
            user = find_user_by_customer(customer_id) if customer_id else None
            if user:
                # revert to free on cancel
                set_plan(uid=user["id"], plan="free", sub_id=None, status="canceled", cpe=None)

    except Exception as e:
        # Log error but return 200 so Stripe retries only if genuine failure
        print("Webhook handling error:", e)

    return JSONResponse({"received": True})

# ============== Memory & Chat with limits ==============
@app.get("/memory", response_class=HTMLResponse)
def memory_list(request: Request, q: Optional[str] = Query(None)):
    redir = require_login(request)
    if redir: return redir
    uid = current_user_id(request); ensure_subscription_row(uid)
    plan, cfg = plan_cfg(uid); user = get_user(uid)

    conn = db()
    if q:
        if not cfg["allow_search"]:
            conn.close()
            return HTMLResponse(layout("<div class='card'><h3>Search is available on Plus & Pro.</h3><p><a href='/pricing'>Upgrade</a></p></div>", user), status_code=403)
        like = f"%{q.strip()}%"
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at, mood FROM memory WHERE user_id = ? AND (title LIKE ? OR content LIKE ?) ORDER BY updated_at DESC",
            (uid, like, like),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at, mood FROM memory WHERE user_id = ? ORDER BY updated_at DESC",
            (uid,),
        ).fetchall()
    conn.close()

    items = "".join(
        f"<li class='item'><a href='/memory/{r['id']}'>{r['title']}</a> "
        f"<span class='small'> — updated {r['updated_at']}{(' • mood: ' + r['mood']) if r['mood'] else ''}</span></li>"
        for r in rows
    ) or "<p class='small'>No entries yet.</p>"

    searchbar = ""
    exportbtn = ""
    if cfg["allow_search"]:
        val = q or ""
        searchbar = f"""
        <form method="get" action="/memory">
          <input type="text" name="q" value="{val}" placeholder="Search your notes..." />
          <button type="submit">Search</button>
        </form>"""
    if cfg["allow_export"]:
        exportbtn = "<a class='btn-link' href='/memory/export'>Export CSV</a>"

    usage = f"<p class='small'>Notes: {memory_count(uid)}/{cfg['memory_limit']}</p>"

    body = f"""
    <div class="card">
      <h2>Your Memory</h2>
      <p class="small">Write short notes, thoughts, or anything you want to remember.</p>
      <a class='btn-link' href='/memory/new'>New entry</a>
      {exportbtn}
      {usage}
      {searchbar}
      <ul class="clean">{items}</ul>
    </div>"""
    return layout(body, user)

@app.get("/memory/export")
def memory_export(request: Request):
    redir = require_login(request)
    if redir: return redir
    uid = current_user_id(request); plan, cfg = plan_cfg(uid)
    if not cfg["allow_export"]:
        return HTMLResponse(layout("<div class='card'><h3>Export is available on Plus & Pro.</h3><p><a href='/pricing'>Upgrade</a></p></div>", get_user(uid)), status_code=403)

    conn = db()
    rows = conn.execute("SELECT id, title, content, mood, created_at, updated_at FROM memory WHERE user_id = ? ORDER BY created_at ASC", (uid,)).fetchall()
    conn.close()

    buf = io.StringIO(); writer = csv.writer(buf)
    writer.writerow(["id", "title", "content", "mood", "created_at", "updated_at"])
    for r in rows:
        writer.writerow([r["id"], r["title"], r["content"], r["mood"] or "", r["created_at"], r["updated_at"]])
    data = buf.getvalue().encode("utf-8-sig")
    headers = {"Content-Disposition": "attachment; filename=coffee_chat_export.csv"}
    return Response(content=data, media_type="text/csv; charset=utf-8", headers=headers)

@app.get("/memory/new", response_class=HTMLResponse)
def memory_new_form(request: Request):
    redir = require_login(request)
    if redir: return redir
    uid = current_user_id(request); user = get_user(uid)
    plan, cfg = plan_cfg(uid)
    if memory_count(uid) >= cfg["memory_limit"]:
        return HTMLResponse(layout("<div class='card'><h3>You've reached your note limit.</h3><p><a href='/pricing'>Upgrade</a> to add more.</p></div>", user), status_code=403)
    mood_field = ""
    if cfg["allow_mood"]:
        mood_field = """
        <label>Mood (optional)</label>
        <select name="mood">
          <option value="">--</option>
          <option value="😊">😊 Positive</option>
          <option value="😐">😐 Neutral</option>
          <option value="😟">😟 Low</option>
        </select>"""
    body = f"""
    <div class="card">
      <h2>New memory</h2>
      <form method="post" action="/memory/new">
        <label>Title</label><input type="text" name="title" required maxlength="120" />
        <label>Content</label><textarea name="content" rows="8" required></textarea>
        {mood_field}
        <button type="submit">Save</button>
      </form>
    </div>"""
    return layout(body, user)

@app.post("/memory/new")
def memory_create(request: Request, title: str = Form(...), content: str = Form(...), mood: str = Form("")):
    redir = require_login(request)
    if redir: return redir
    uid = current_user_id(request); plan, cfg = plan_cfg(uid)
    if memory_count(uid) >= cfg["memory_limit"]:
        return HTMLResponse(layout("<div class='card'><h3>Note limit reached. Upgrade to add more.</h3><p><a href='/pricing'>Pricing</a></p></div>", get_user(uid)), status_code=403)
    now = datetime.utcnow().isoformat()
    conn = db()
    conn.execute(
        "INSERT INTO memory (user_id, title, content, mood, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (uid, title.strip(), content.strip(), (mood or None), now, now),
    )
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    return RedirectResponse(f"/memory/{new_id}", status_code=303)

@app.get("/memory/{mid}", response_class=HTMLResponse)
def memory_detail(request: Request, mid: int):
    redir = require_login(request)
    if redir: return redir
    uid = current_user_id(request); plan, cfg = plan_cfg(uid)
    user = get_user(uid)
    conn = db()
    row = conn.execute("SELECT * FROM memory WHERE id = ? AND user_id = ?", (mid, uid)).fetchone()
    conn.close()
    if not row:
        return HTMLResponse(layout("<div class='card'><h3>Not found.</h3></div>", user), status_code=404)
    mood_field = ""
    if cfg["allow_mood"]:
        mv = row["mood"] or ""
        mood_field = f"""
        <label>Mood (optional)</label>
        <select name="mood">
          <option value="" {'selected' if mv == '' else ''}>--</option>
          <option value="😊" {'selected' if mv == '😊' else ''}>😊 Positive</option>
          <option value="😐" {'selected' if mv == '😐' else ''}>😐 Neutral</option>
          <option value="😟" {'selected' if mv == '😟' else ''}>😟 Low</option>
        </select>"""
    body = f"""
    <div class="card">
      <h2>Edit memory</h2>
      <form method="post" action="/memory/{row['id']}">
        <label>Title</label><input type="text" name="title" value="{row['title']}" required maxlength="120" />
        <label>Content</label><textarea name="content" rows="10" required>{row['content']}</textarea>
        {mood_field}
        <button type="submit" name="action" value="save">Save changes</button>
        <button type="submit" name="action" value="delete" style="background:#b91c1c">Delete</button>
      </form>
      <p class="small">Created {row['created_at']} • Updated {row['updated_at']}{(' • mood: ' + (row['mood'] or '')) if row['mood'] else ''}</p>
    </div>"""
    return layout(body, user)

@app.post("/memory/{mid}")
def memory_update(request: Request, mid: int, action: str = Form(...), title: str = Form(""), content: str = Form(""), mood: str = Form("")):
    redir = require_login(request)
    if redir: return redir
    uid = current_user_id(request); plan, cfg = plan_cfg(uid)
    conn = db()
    if action == "delete":
        conn.execute("DELETE FROM memory WHERE id = ? AND user_id = ?", (mid, uid))
        conn.commit(); conn.close()
        return RedirectResponse("/memory", status_code=303)
    now = datetime.utcnow().isoformat()
    if cfg["allow_mood"]:
        conn.execute("UPDATE memory SET title=?, content=?, mood=?, updated_at=? WHERE id=? AND user_id=?", (title.strip(), content.strip(), (mood or None), now, mid, uid))
    else:
        conn.execute("UPDATE memory SET title=?, content=?, updated_at=? WHERE id=? AND user_id=?", (title.strip(), content.strip(), now, mid, uid))
    conn.commit(); conn.close()
    return RedirectResponse(f"/memory/{mid}", status_code=303)

# ============== Chat UI (limits per plan) ==============
@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    redir = require_login(request)
    if redir: return redir
    uid = current_user_id(request); ensure_subscription_row(uid)
    user = get_user(uid); plan, cfg = plan_cfg(uid)
    used = chat_messages_today(uid); remaining = max(cfg["chat_daily"] - used, 0)
    hint = "" if _llm_ready else "<p class='small'>LLM is in local/dev mode. Set <code>OPENAI_API_KEY</code>.</p>"
    cap_notice = f"<div class='notice small'>Daily messages: {used}/{cfg['chat_daily']}. Remaining: {remaining}.</div>"
    disabled = "disabled" if remaining <= 0 else ""
    reached = "<p class='small'>You’ve hit today’s chat limit. It resets at midnight. <a href='/pricing'>Upgrade</a> for more.</p>" if remaining <= 0 else ""
    body = f"""
    <div class="card">
      <h2>Coffee Chat</h2>
      {cap_notice}{hint}
      <form method="post" action="/chat">
        <label>Your message</label>
        <textarea name="message" rows="5" placeholder="What's on your mind?" required {disabled}></textarea>
        <button type="submit" {disabled}>Send</button>
      </form>
      {reached}
    </div>"""
    return layout(body, user)

@app.post("/chat", response_class=HTMLResponse)
def chat_post(request: Request, message: str = Form(...)):
    redir = require_login(request)
    if redir: return redir
    uid = current_user_id(request); plan, cfg = plan_cfg(uid)
    used = chat_messages_today(uid)
    if used >= cfg["chat_daily"]:
        return HTMLResponse(layout("<div class='card'><h3>Daily chat limit reached.</h3><p><a href='/pricing'>Upgrade</a> for more.</p></div>", get_user(uid)), status_code=403)
    now = datetime.utcnow().isoformat()
    conn = db()
    conn.execute("INSERT INTO memory (user_id, title, content, created_at, updated_at) VALUES (?, 'Chat note', ?, ?, ?)", (uid, message.strip(), now, now))
    conn.commit(); conn.close()
    reply = llm_reply(uid, message)
    conn = db()
    conn.execute("INSERT INTO memory (user_id, title, content, created_at, updated_at) VALUES (?, 'Assistant reply', ?, ?, ?)", (uid, reply, now, datetime.utcnow().isoformat()))
    conn.commit(); conn.close()
    user = get_user(uid)
    used_after = chat_messages_today(uid); remaining = max(cfg["chat_daily"] - used_after, 0)
    cap_notice = f"<div class='notice small'>Daily messages: {used_after}/{cfg['chat_daily']}. Remaining: {remaining}.</div>"
    body = f"""
    <div class="card">
      <h2>Coffee Chat</h2>
      {cap_notice}
      <div class="msg user"><strong>You:</strong><br>{message}</div>
      <div class="msg bot"><strong>Assistant:</strong><br>{reply}</div>
      <form method="post" action="/chat" style="margin-top:16px">
        <label>Say more</label>
        <textarea name="message" rows="4" placeholder="Add a follow-up…" required></textarea>
        <button type="submit">Send</button>
      </form>
      <p class="small">This exchange was saved to your <a href="/memory">Memory</a>.</p>
    </div>"""
    return layout(body, user)

# JSON chat API if you build a front-end later
@app.post("/api/chat")
def api_chat(request: Request, payload: Dict[str, Any] = None):
    uid = current_user_id(request)
    if not uid: return JSONResponse({"error": "not_authenticated"}, status_code=401)
    plan, cfg = plan_cfg(uid)
    used = chat_messages_today(uid)
    if used >= cfg["chat_daily"]:
        return JSONResponse({"error": "limit_reached"}, status_code=403)
    data = payload or {}
    message = (data.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message_required"}, status_code=400)
    now = datetime.utcnow().isoformat()
    conn = db()
    conn.execute("INSERT INTO memory (user_id, title, content, created_at, updated_at) VALUES (?, 'Chat note', ?, ?, ?)", (uid, message, now, now))
    conn.commit(); conn.close()
    reply = llm_reply(uid, message)
    conn = db()
    conn.execute("INSERT INTO memory (user_id, title, content, created_at, updated_at) VALUES (?, 'Assistant reply', ?, ?, ?)", (uid, reply, now, datetime.utcnow().isoformat()))
    conn.commit(); conn.close()
    return {"reply": reply, "saved": True}

# ============== Health ==============
@app.get("/health")
def health():
    try:
        conn = db(); conn.execute("SELECT 1"); conn.close()
        return {"ok": True, "llm_ready": _llm_ready, "stripe": bool(STRIPE_SECRET_KEY)}
    except Exception as e:
        return PlainTextResponse(str(e), status_code=500)
