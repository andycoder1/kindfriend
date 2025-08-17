import os, sys, time, uuid, io, csv, json, datetime, sqlite3
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles  # <-- serve /static

# ----- Password hashing -----
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

# ----- Stripe (optional) -----
try:
    import stripe  # type: ignore
except Exception:
    stripe = None

# ----- Pydantic -----
from pydantic import BaseModel

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

# ----- OpenAI client -----
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

# ----- Stripe client -----
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

signer = URLSafeSerializer(SECRET_KEY, salt="kf-auth")

# =========================
# System Prompt
# =========================
SYSTEM_PROMPT = (
    "You are Kind Friend: a warm, respectful companion with the display name set by the user. "
    "Use the provided name (e.g., Tony/Jane) when speaking as yourself. You are not a therapist. "
    "If the user mentions self-harm or immediate danger, suggest contacting UK Samaritans (116 123), "
    "NHS 111, or emergency services (999). Be concise and kind. "
    "Consider the user's local time and recent context when replying (e.g., acknowledge late hours, mornings, weekends). "
    "When the user shares long-term personal facts or preferences (e.g., name/nickname, pronouns, hobbies, likes, goals), "
    "briefly acknowledge them and ask: \"Would you like me to remember that?\" "
    "Do not store anything yourself; the app will handle memory with consent."
)

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
            bot_avatar    TEXT,
            created_at    REAL NOT NULL,
            stripe_customer_id TEXT,
            subscription_status TEXT,
            checkin_enabled INTEGER,
            checkin_hour   INTEGER,
            checkin_minute INTEGER,
            checkin_last_date TEXT
        )""")
        # Migrations (idempotent)
        def ensure_col(table, col, ddl):
            try:
                cur.execute(f"SELECT {col} FROM {table} LIMIT 1")
            except sqlite3.OperationalError:
                cur.execute(ddl)
        ensure_col("messages", "user_id", "ALTER TABLE messages ADD COLUMN user_id TEXT")
        ensure_col("users", "stripe_customer_id", "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        ensure_col("users", "subscription_status", "ALTER TABLE users ADD COLUMN subscription_status TEXT")
        ensure_col("users", "bot_name", "ALTER TABLE users ADD COLUMN bot_name TEXT")
        ensure_col("users", "bot_avatar", "ALTER TABLE users ADD COLUMN bot_avatar TEXT")
        ensure_col("users", "checkin_enabled", "ALTER TABLE users ADD COLUMN checkin_enabled INTEGER")
        ensure_col("users", "checkin_hour", "ALTER TABLE users ADD COLUMN checkin_hour INTEGER")
        ensure_col("users", "checkin_minute", "ALTER TABLE users ADD COLUMN checkin_minute INTEGER")
        ensure_col("users", "checkin_last_date", "ALTER TABLE users ADD COLUMN checkin_last_date TEXT")
        # Rate-limit bucket
        cur.execute("CREATE TABLE IF NOT EXISTS rate_limit (key TEXT PRIMARY KEY, tokens REAL, updated REAL)")
        conn.commit()

def init_memories_table():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            type        TEXT NOT NULL,
            content     TEXT NOT NULL,
            confidence  REAL NOT NULL DEFAULT 0.9,
            pinned      INTEGER NOT NULL DEFAULT 0,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL,
            expires_at  REAL
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id)")
        conn.commit()

init_db()
init_memories_table()

# =========================
# DB helpers
# =========================
def create_user(username: str, password: str, bot_name: str = "Kind Friend", bot_avatar: Optional[str] = None):
    uid = str(uuid.uuid4())
    pw_hash = hash_password(password)
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (id, username, password_hash, display_name, bio, bot_name, bot_avatar, created_at, stripe_customer_id, subscription_status, checkin_enabled, checkin_hour, checkin_minute, checkin_last_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, NULL, NULL)",
            (uid, username, pw_hash, username, "", (bot_name or "Kind Friend").strip(), bot_avatar, time.time()),
        )
        conn.commit()
    return uid

def get_user_by_username(username: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, bot_name, bot_avatar, created_at, stripe_customer_id, subscription_status, checkin_enabled, checkin_hour, checkin_minute, checkin_last_date FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
    if not row: return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3],
            "bio": row[4], "bot_name": row[5] or "Kind Friend", "bot_avatar": row[6],
            "created_at": row[7], "stripe_customer_id": row[8], "subscription_status": row[9],
            "checkin_enabled": row[10], "checkin_hour": row[11], "checkin_minute": row[12], "checkin_last_date": row[13]}

def get_user_by_id(user_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, display_name, bio, bot_name, bot_avatar, created_at, stripe_customer_id, subscription_status, checkin_enabled, checkin_hour, checkin_minute, checkin_last_date FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
    if not row: return None
    return {"id": row[0], "username": row[1], "password_hash": row[2], "display_name": row[3],
            "bio": row[4], "bot_name": row[5] or "Kind Friend", "bot_avatar": row[6],
            "created_at": row[7], "stripe_customer_id": row[8], "subscription_status": row[9],
            "checkin_enabled": row[10], "checkin_hour": row[11], "checkin_minute": row[12], "checkin_last_date": row[13]}

def update_user_profile(user_id: str, display_name: Optional[str], bio: Optional[str], bot_name: Optional[str], bot_avatar: Optional[str]):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        sets, vals = [], []
        if display_name is not None: sets.append("display_name = ?"); vals.append(display_name)
        if bio is not None: sets.append("bio = ?"); vals.append(bio)
        if bot_name is not None: sets.append("bot_name = ?"); vals.append((bot_name or "Kind Friend").strip())
        if bot_avatar is not None: sets.append("bot_avatar = ?"); vals.append(bot_avatar)
        if not sets: return
        vals.append(user_id)
        cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()

def set_checkin(user_id: str, enabled: bool, hour: Optional[int], minute: Optional[int]):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET checkin_enabled=?, checkin_hour=?, checkin_minute=? WHERE id=?",
                    (1 if enabled else 0, hour, minute, user_id))
        conn.commit()

def mark_checkin_sent_today(user_id: str, tz: str):
    today = datetime.datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET checkin_last_date=? WHERE id=?", (today, user_id))
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

# ----- Rate limit (token bucket) -----
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "20"))
def check_rate_limit(user_id: Optional[str], ip: str) -> bool:
    if RATE_LIMIT_RPM <= 0: return True
    max_tokens = float(RATE_LIMIT_RPM); refill = max_tokens / 60.0
    key = f"user:{user_id}" if user_id else f"ip:{ip}"
    now = time.time()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
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

# =========================
# Memory helpers
# =========================
def add_memory(user_id: str, mtype: str, content: str, confidence: float = 0.9, pinned: bool = False, ttl_days: int | None = None) -> str:
    mid = str(uuid.uuid4())
    now = time.time()
    exp = now + ttl_days*86400 if ttl_days else None
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO memories (id, user_id, type, content, confidence, pinned, created_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, user_id, mtype, content.strip(), confidence, 1 if pinned else 0, now, now, exp),
        )
        conn.commit()
    return mid

def list_memories(user_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, type, content, confidence, pinned, created_at, updated_at, expires_at FROM memories WHERE user_id=? ORDER BY pinned DESC, updated_at DESC", (user_id,))
        rows = cur.fetchall()
    return [
        {"id": r[0], "type": r[1], "content": r[2], "confidence": r[3], "pinned": bool(r[4]),
         "created_at": r[5], "updated_at": r[6], "expires_at": r[7]}
        for r in rows
    ]

def delete_memory(user_id: str, mem_id: str) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM memories WHERE id=? AND user_id=?", (mem_id, user_id))
        n = cur.rowcount
        conn.commit()
    return n > 0

def get_context_memories(user_id: str, max_items: int = 8):
    now = time.time()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT type, content FROM memories
            WHERE user_id=? AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY pinned DESC, confidence DESC, updated_at DESC
            LIMIT ?
        """, (user_id, now, max_items))
        rows = cur.fetchall()
    return [f"{t}: {c}" for (t, c) in rows]

# ----- Tone prefs -----
TONE_PRESETS = {
    "warm_concise": "warm, kind, and concise",
    "cheerful": "cheerful, upbeat, and supportive",
    "practical": "practical, straightforward, and solution-focused",
    "empathetic": "gentle, empathetic, and validating",
    "brief": "very brief and to the point",
    "encouraging": "positive, encouraging, and motivating",
}

def _find_tone_memory(user_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, content FROM memories
            WHERE user_id=? AND type='preference' AND pinned=1
            ORDER BY updated_at DESC
            LIMIT 20
        """, (user_id,))
        for mid, content in cur.fetchall():
            if content.lower().startswith("tone:"):
                return mid, content
    return None, None

def get_user_tone(user_id: str) -> str:
    mid, content = _find_tone_memory(user_id)
    if not content:
        return "warm_concise"
    key = content.split(":", 1)[1].strip().lower().replace(" ", "_")
    return key if key in TONE_PRESETS else "warm_concise"

def set_user_tone(user_id: str, tone_key: str):
    if tone_key not in TONE_PRESETS:
        raise ValueError("invalid tone")
    now = time.time()
    label = f"tone: {tone_key}"
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM memories WHERE user_id=? AND type='preference' AND content LIKE 'tone:%'", (user_id,))
        mid = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO memories (id, user_id, type, content, confidence, pinned, created_at, updated_at, expires_at) VALUES (?, ?, 'preference', ?, 1.0, 1, ?, ?, NULL)",
            (mid, user_id, label, now, now)
        )
        conn.commit()
    return tone_key

# ----- Memory suggestions (consent-first) -----
def suggest_memories_from_text(text: str):
    s = text.strip()
    low = s.lower()
    suggestions = []

    for pat in ["call me ", "my name is ", "you can call me "]:
        if pat in low:
            after = s[low.index(pat)+len(pat):].strip().strip(".! ").split()[0:6]
            val = " ".join(after).strip(",. ")
            if val:
                suggestions.append({"type":"profile","content":f"preferred name: {val}", "pinned": True, "ttl_days": None})
                break

    if "my pronouns are" in low:
        val = s[low.index("my pronouns are")+len("my pronouns are"):].strip(" .!").split()[0:6]
        val = " ".join(val).strip(",. ")
        if val:
            suggestions.append({"type":"preference","content":f"pronouns: {val}", "pinned": True, "ttl_days": None})

    if any(x in low for x in ["i like ", "i love ", "my favourite", "my favorite"]):
        suggestions.append({"type":"preference","content":s, "pinned": False, "ttl_days": 365})

    if "i work " in low or "my job" in low or "i study" in low:
        suggestions.append({"type":"fact","content":s, "pinned": False, "ttl_days": 365})

    if low.startswith(("remember ", "please remember ", "can you remember ")):
        payload = s.split(None, 1)[1] if len(s.split(None, 1))>1 else ""
        if payload:
            suggestions.append({"type":"note","content":payload.strip(), "pinned": False, "ttl_days": 365})

    try:
        tz_name = os.getenv("APP_TZ", "Europe/London")
        hour = datetime.datetime.now(ZoneInfo(tz_name)).hour
        if (hour >= 23 or hour <= 5) and any(k in low for k in ["finished work", "just finished", "off work", "night shift"]):
            suggestions.append({"type": "fact", "content": "often works late or night shifts", "pinned": False, "ttl_days": 180})
    except Exception:
        pass

    uniq, seen = [], set()
    for m in suggestions:
        k = (m["type"], m["content"].lower())
        if k not in seen:
            uniq.append(m); seen.add(k)
    return uniq[:3]

# =========================
# Time helpers
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

def time_context_hint(user_message: str) -> Optional[str]:
    tz_name = os.getenv("APP_TZ", "Europe/London")
    now = datetime.datetime.now(ZoneInfo(tz_name))
    hour = now.hour
    low = user_message.lower()

    late_triggers = ["finished work", "just finished", "off work", "can't sleep", "cant sleep", "up late", "long day", "night shift"]
    if any(t in low for t in late_triggers):
        if hour >= 23 or hour <= 5:
            return (f"It's currently {now.strftime('%H:%M')} local time. "
                    "Acknowledge it's late and offer gentle wind-down support (rest, hydration, brief check-in).")

    if "morning" in low and hour >= 12:
        return (f"The user said 'morning' but it's {now.strftime('%H:%M')} local time. "
                "Optionally clarify time zones kindly.")
    if "good night" in low and 8 <= hour <= 18:
        return (f"The user said 'good night' but it's {now.strftime('%H:%M')} local time. "
                "Be flexible; they might be on a different schedule.")
    return None

def trial_info(user: dict):
    if user.get("subscription_status") == "active": return True, None
    end_ts = user["created_at"] + TRIAL_DAYS*86400
    now = time.time()
    if now < end_ts:
        days_left = max(1, int((end_ts - now + 86399)//86400))
        return True, days_left
    return False, 0

def checkin_due_and_text(user: dict) -> tuple[bool, Optional[str]]:
    if not user or not user.get("checkin_enabled"): return False, None
    tz_name = os.getenv("APP_TZ", "Europe/London")
    now = datetime.datetime.now(ZoneInfo(tz_name))
    if user.get("checkin_hour") is None or user.get("checkin_minute") is None: return False, None
    scheduled = now.replace(hour=int(user["checkin_hour"]), minute=int(user["checkin_minute"]), second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    last = user.get("checkin_last_date")
    if now >= scheduled and last != today:
        tod = "morning" if 5 <= now.hour < 12 else ("afternoon" if 12 <= now.hour < 18 else "evening")
        text = f"Kind check-in this {tod}: how are you feeling right now?"
        return True, text
    return False, None

# =========================
# HTML: Landing + App
# =========================
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
    .hero img { max-width:92%; height:auto; margin:2rem auto; border-radius:16px; box-shadow:0 4px 12px rgba(0,0,0,0.1); }
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

# ---- Chat app UI (light theme default, with avatars, consent bar, tone & check-in) ----
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
    .app{display:grid;grid-template-columns:380px 1fr;height:100svh;overflow:hidden}
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
    .form-row input,.form-row textarea,.form-row select{padding:10px 12px;border-radius:12px;border:1px solid var(--br);background:#fff;color:var(--txt)}
    .link{color:var(--g)}
    #mem-bar{display:none; position:sticky; bottom:0; left:0; right:0; margin:8px; padding:10px; background:#fff3cd; color:#664d03; border:1px solid #ffecb5; border-radius:12px;}
    .avatar-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
    .avatar-choice{border:2px solid transparent;border-radius:12px;overflow:hidden;cursor:pointer}
    .avatar-choice img{width:100%;height:72px;object-fit:cover;display:block}
    .avatar-choice.selected{border-color:var(--acc)}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="logo">KF</div>
    <div class="brand">Kind Friend · <span id="botname-head">Chatting with Kind Friend</span></div>
    <div class="grow"></div>
    <img id="bot-avatar-top" alt="Bot avatar" style="width:32px;height:32px;border-radius:50%;object-fit:cover;display:none;border:2px solid rgba(255,255,255,.6)">
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
      <div class="chatbar">
        <div class="auth" id="auth" style="display:flex;gap:8px;align-items:center;">
          <button id="open-auth" class="tb-btn primary">Sign in / up</button>
          <button id="logout" class="tb-btn" style="display:none;">Log out</button>
          <button id="edit-profile" class="tb-btn" style="display:none;">Profile</button>
          <button id="upgrade" class="tb-btn primary" style="display:none;">Upgrade</button>
          <button id="billing" class="tb-btn" style="display:none;">Billing</button>
          <button id="donation-note" class="tb-btn" title="We donate half of all fees">50% to Samaritans</button>
        </div>
      </div>

      <section class="chat" id="chat"></section>

      <div id="mem-bar">
        <div id="mem-text" style="margin-bottom:8px; font-size:14px;"></div>
        <div>
          <button id="mem-yes" class="tb-btn" style="background:#25d366;color:#fff;border:none;">Yes, remember</button>
          <button id="mem-no" class="tb-btn" style="background:#eee;color:#333;border:1px solid #ccc;">No thanks</button>
        </div>
      </div>

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
        <div class="form-row">
          <label>Choose an avatar</label>
          <div class="avatar-grid" id="signup-avatars"></div>
        </div>
        <div class="form-actions" style="display:flex;gap:8px;justify-content:flex-end;">
          <button class="xbtn" id="signup-cancel">Cancel</button>
          <button class="tb-btn primary" id="signup-submit">Create account</button>
        </div>
      </div>
      <div style="margin-top:10px;color:#54656f">
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
      <div class="form-row"><label for="bio">Bio</label><textarea id="bio" rows="4" placeholder="Anything you'd like remembered (non-sensitive)."></textarea></div>
      <div class="form-row"><label for="bot_name">Bot name</label><input id="bot_name" placeholder="e.g., Tony, Jane" value="Kind Friend"/></div>
      <div class="form-row">
        <label>Bot avatar</label>
        <div class="avatar-grid" id="profile-avatars"></div>
      </div>
      <div class="form-row">
        <label for="tone_select">Tone</label>
        <select id="tone_select">
          <option value="warm_concise">Warm & concise (default)</option>
          <option value="cheerful">Cheerful</option>
          <option value="practical">Practical</option>
          <option value="empathetic">Empathetic</option>
          <option value="brief">Brief</option>
          <option value="encouraging">Encouraging</option>
        </select>
      </div>
      <div class="form-row">
        <label>Daily check-in</label>
        <div style="display:flex;gap:8px;align-items:center;">
          <input type="checkbox" id="checkin_enabled">
          <input type="time" id="checkin_time" value="20:00">
          <span style="color:#54656f;font-size:12px;">(local time)</span>
        </div>
      </div>
      <div class="form-actions" style="display:flex;gap:8px;justify-content:flex-end;">
        <button class="xbtn" id="cancel-profile">Cancel</button>
        <button class="tb-btn primary" id="save-profile">Save</button>
      </div>
    </div>
  </div>

  <script>
    const USE_TEXTCONTENT = false; // keep exact spacing via innerHTML with <br>
    const AVATARS = [
      "https://images.unsplash.com/photo-1527980965255-d3b416303d12?q=80&w=240&auto=format&fit=crop",
      "https://images.unsplash.com/photo-1544005313-94ddf0286df2?q=80&w=240&auto=format&fit=crop",
      "https://images.unsplash.com/photo-1547425260-76bcadfb4f2c?q=80&w=240&auto=format&fit=crop",
      "https://images.unsplash.com/photo-1544005316-04ce1f2b5333?q=80&w=240&auto=format&fit=crop"
    ];

    const root = document.documentElement;
    const chat = document.getElementById('chat');
    const input = document.getElementById('message');
    const send  = document.getElementById('send');
    const sessionsEl = document.getElementById('sessions');
    const botnameHead = document.getElementById('botname-head');
    const botAvatarTop = document.getElementById('bot-avatar-top');

    const memBar = document.getElementById('mem-bar');
    const memText = document.getElementById('mem-text');
    const memYes = document.getElementById('mem-yes');
    const memNo = document.getElementById('mem-no');
    let memPending = null;

    document.getElementById('theme').onclick = () => {
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next); localStorage.setItem('kf-theme', next);
    };
    const savedTheme = localStorage.getItem('kf-theme'); if (savedTheme) root.setAttribute('data-theme', savedTheme);

    function md(x){
      const esc = x.replace(/[&<>]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]));
      const withLinks = esc.replace(/(https?:\\/\\/\\S+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
      return withLinks.replace(/\\n/g, '<br/>');
    }
    function setBubbleContent(el, text){ if(USE_TEXTCONTENT){ el.textContent=text; } else { el.innerHTML=md(text); } }

    function initials(name){
      const parts = (name||'').trim().split(/\\s+/).slice(0,2);
      return parts.map(s=>s[0]||'').join('').toUpperCase() || 'KF';
    }

    function createAvatarNode(url, fallbackInitials){
      const av=document.createElement('div'); av.className='avatar';
      if(url){ const img=document.createElement('img'); img.src=url; img.alt='Bot avatar'; av.appendChild(img); }
      else { av.textContent=fallbackInitials; }
      return av;
    }

    function makeBotBubble(initial='…'){
      const row=document.createElement('div'); row.className='row bot';
      const av=createAvatarNode(BOT_AVATAR_URL, initials(BOT_NAME));
      const b=document.createElement('div'); b.className='bubble'; setBubbleContent(b, initial);
      const meta=document.createElement('div'); meta.className='meta'; meta.textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      row.appendChild(av); const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta); row.appendChild(wrap);
      chat.appendChild(row); chat.scrollTop=chat.scrollHeight; return b;
    }
    function addBubble(text, who){
      const row=document.createElement('div'); row.className='row '+who;
      const av=createAvatarNode(who==='bot'?BOT_AVATAR_URL:null, who==='bot'?initials(BOT_NAME):'You');
      const b=document.createElement('div'); b.className='bubble'; setBubbleContent(b, text);
      const meta=document.createElement('div'); meta.className='meta'; meta.textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      if(who==='bot'){ row.appendChild(av); const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta); row.appendChild(wrap); }
      else { const wrap=document.createElement('div'); wrap.appendChild(b); wrap.appendChild(meta); row.appendChild(wrap); row.style.gridTemplateColumns='1fr auto'; }
      chat.appendChild(row); chat.scrollTop=chat.scrollHeight;
    }

    const meSpan = document.getElementById('me');
    const trialChip = document.getElementById('trial-chip');
    const logoutBtn = document.getElementById('logout');
    const editProfileBtn = document.getElementById('edit-profile');
    const upgradeBtn = document.getElementById('upgrade');
    const billingBtn = document.getElementById('billing');
    const openAuthBtn = document.getElementById('open-auth');
    const largeBtn = document.getElementById('large');
    const donationBtn = document.getElementById('donation-note');
    donationBtn.onclick = () => alert('💚 ' + 'We donate 50% of fees to Samaritans.\\nLearn more: https://www.samaritans.org/');

    largeBtn.onclick = () => document.body.classList.toggle('large');

    let BOT_NAME = 'Kind Friend';
    let BOT_AVATAR_URL = '';
    let isAuthed = false;

    function setComposerEnabled(on){
      input.disabled = !on; send.disabled = !on;
      input.placeholder = on ? "Type a message" : "Sign in to start chatting";
    }
    setComposerEnabled(false);

    // Auth modal wiring
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

    const signupAvGrid = document.getElementById('signup-avatars');
    const profileAvGrid = document.getElementById('profile-avatars');
    let signupAvatar = '';
    let profileAvatar = '';

    function renderAvGrid(container, current, onPick){
      container.innerHTML='';
      ["https://images.unsplash.com/photo-1527980965255-d3b416303d12?q=80&w=240&auto=format&fit=crop",
       "https://images.unsplash.com/photo-1544005313-94ddf0286df2?q=80&w=240&auto=format&fit=crop",
       "https://images.unsplash.com/photo-1547425260-76bcadfb4f2c?q=80&w=240&auto=format&fit=crop",
       "https://images.unsplash.com/photo-1544005316-04ce1f2b5333?q=80&w=240&auto=format&fit=crop"]
      .forEach(url=>{
        const cell=document.createElement('div'); cell.className='avatar-choice'+(current===url?' selected':'');
        const img=document.createElement('img'); img.src=url; img.alt='Avatar';
        cell.appendChild(img);
        cell.onclick=()=>{
          [...container.children].forEach(c=>c.classList.remove('selected'));
          cell.classList.add('selected');
          onPick(url);
        };
        container.appendChild(cell);
      });
      const none=document.createElement('div'); none.className='avatar-choice'+(!current?' selected':'');
      none.style.display='grid'; none.style.placeItems='center'; none.style.height='72px'; none.style.border='2px dashed #d1d7db';
      none.textContent='No avatar';
      none.onclick=()=>{
        [...container.children].forEach(c=>c.classList.remove('selected'));
        none.classList.add('selected');
        onPick('');
      };
      container.appendChild(none);
    }

    function openAuth(which='login'){ authModal.classList.add('on'); authBackdrop.classList.add('on'); (which==='signup'?showSignup():showLogin()); setTimeout(()=>{ (which==='signup'?signupUsername:loginUsername).focus(); },50); }
    function closeAuth(){ authModal.classList.remove('on'); authBackdrop.classList.remove('on'); }
    function showLogin(){ paneLogin.style.display=''; paneSignup.style.display='none'; tabLogin.classList.add('primary'); tabSignup.classList.remove('primary'); tabLogin.setAttribute('aria-selected','true'); tabSignup.setAttribute('aria-selected','false'); }
    function showSignup(){ paneLogin.style.display='none'; paneSignup.style.display=''; tabSignup.classList.add('primary'); tabLogin.classList.remove('primary'); tabLogin.setAttribute('aria-selected','false'); tabSignup.setAttribute('aria-selected','true'); renderAvGrid(signupAvGrid, signupAvatar, (url)=>{ signupAvatar=url; }); }

    openAuthBtn.onclick = ()=>openAuth('login'); authClose.onclick=closeAuth; authBackdrop.onclick=closeAuth; loginCancel.onclick=closeAuth; tabLogin.onclick=showLogin; tabSignup.onclick=showSignup;

    async function refreshMe(){
      const r = await fetch('/api/me'); const data = await r.json();
      if(data.user){
        isAuthed = true;
        setComposerEnabled(true);
        BOT_NAME = (data.user.bot_name || 'Kind Friend');
        BOT_AVATAR_URL = data.user.bot_avatar || '';
        botnameHead.textContent = 'Chatting with ' + BOT_NAME;
        if(BOT_AVATAR_URL){ botAvatarTop.style.display=''; botAvatarTop.src = BOT_AVATAR_URL; } else { botAvatarTop.style.display='none'; }

        meSpan.textContent = `Signed in as ${data.user.display_name||data.user.username}`;
        openAuthBtn.style.display='none'; logoutBtn.style.display=''; editProfileBtn.style.display=''; upgradeBtn.style.display=''; billingBtn.style.display='';

        if(data.trial && data.trial.days_remaining !== null){
          trialChip.style.display=''; trialChip.textContent = `Free trial: ${data.trial.days_remaining} day(s) left`;
        } else if (data.subscription_status === 'active'){
          trialChip.style.display=''; trialChip.textContent = 'Subscription: active';
        } else { trialChip.style.display='none'; }

        if (data.checkin_due_text) { addBubble(data.checkin_due_text, 'bot'); }
      } else {
        isAuthed = false; setComposerEnabled(false);
        BOT_NAME = 'Kind Friend'; BOT_AVATAR_URL=''; botnameHead.textContent = 'Chatting with Kind Friend'; botAvatarTop.style.display='none';
        meSpan.textContent = 'Not signed in';
        openAuthBtn.style.display=''; logoutBtn.style.display='none'; editProfileBtn.style.display='none'; upgradeBtn.style.display='none'; billingBtn.style.display='none'; trialChip.style.display='none';
      }
    }

    const loginSubmit = document.getElementById('login-submit');
    const signupSubmit = document.getElementById('signup-submit');

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
      const r = await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password,bot_name,bot_avatar:signupAvatar})});
      const d = await r.json(); if(!r.ok) return alert(d.error||'Signup failed');
      await refreshMe(); addBubble('Account created and signed in. 👋','bot'); loadSessions(); loadHistory(); closeAuth();
    };
    logoutBtn.onclick=async()=>{ await fetch('/api/logout',{method:'POST'}); await refreshMe(); addBubble('Signed out.','bot'); loadSessions(); chat.innerHTML=''; };

    // Profile modal
    const profileModal = document.getElementById('profile-modal'), back2 = document.getElementById('modal-backdrop');
    const saveProfile = document.getElementById('save-profile'); const cancelProfile = document.getElementById('cancel-profile'); const closeModalBtn = document.getElementById('close-modal');
    const displayNameEl = document.getElementById('display_name'); const bioEl = document.getElementById('bio'); const botNameEl = document.getElementById('bot_name');
    const toneSelect = document.getElementById('tone_select');
    const checkinEnabledEl = document.getElementById('checkin_enabled'); const checkinTimeEl = document.getElementById('checkin_time');

    document.getElementById('edit-profile').onclick = async ()=>{
      profileModal.classList.add('on'); back2.classList.add('on');
      renderAvGrid(profileAvGrid, BOT_AVATAR_URL, (url)=>{ profileAvatar=url; });
      const [meR, toneR, chkR] = await Promise.all([fetch('/api/me'), fetch('/api/tone'), fetch('/api/checkin')]);
      const me = await meR.json(); const t = await toneR.json(); const ck = await chkR.json();
      if(me.user){
        displayNameEl.value = me.user.display_name || '';
        bioEl.value = me.user.bio || '';
        botNameEl.value = me.user.bot_name || 'Kind Friend';
        profileAvatar = me.user.bot_avatar || '';
        renderAvGrid(profileAvGrid, profileAvatar, (url)=>{ profileAvatar=url; });
      }
      if(t && t.tone){ toneSelect.value = t.tone; }
      if(ck && ck.enabled !== undefined){
        checkinEnabledEl.checked = !!ck.enabled;
        if(ck.hour !== null && ck.minute !== null){
          const hh = String(ck.hour).padStart(2,'0'); const mm = String(ck.minute).padStart(2,'0');
          checkinTimeEl.value = `${hh}:${mm}`;
        }
      }
    };
    function closeProfile(){ profileModal.classList.remove('on'); back2.classList.remove('on'); }
    closeModalBtn.onclick=closeProfile; cancelProfile.onclick=closeProfile; back2.onclick=closeProfile;

    saveProfile.onclick = async ()=>{
      const display_name = displayNameEl.value; const bio = bioEl.value; const bot_name = (botNameEl.value || 'Kind Friend').trim();
      const tone = toneSelect.value;
      const enabled = checkinEnabledEl.checked;
      const [h, m] = checkinTimeEl.value.split(':').map(x=>parseInt(x||'0',10));

      const [r1, r2, r3] = await Promise.all([
        fetch('/api/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_name,bio,bot_name,bot_avatar:profileAvatar})}),
        fetch('/api/tone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tone})}),
        fetch('/api/checkin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,hour:h,minute:m})}),
      ]);
      const d1 = await r1.json(); const d2 = await r2.json(); const d3 = await r3.json();
      if(!r1.ok) return alert(d1.error||'Could not save profile');
      if(!r2.ok) return alert(d2.error||'Could not save tone');
      if(!r3.ok) return alert(d3.error||'Could not save check-in');

      BOT_NAME = bot_name; botnameHead.textContent = 'Chatting with ' + BOT_NAME;
      BOT_AVATAR_URL = profileAvatar || ''; if(BOT_AVATAR_URL){ botAvatarTop.style.display=''; botAvatarTop.src = BOT_AVATAR_URL; } else { botAvatarTop.style.display='none'; }
      closeProfile(); addBubble('Profile updated. Tone & check-in saved.','bot');
    };

    // Sessions & history
    async function loadSessions(){ const r=await fetch('/api/sessions'); const data=await r.json(); sessionsEl.innerHTML=''; (data.sessions||[]).forEach(s=>{ const el=document.createElement('div'); el.className='item'+(data.active===s.id?' active':''); el.textContent=s.title||'Untitled'; el.onclick=async()=>{ await fetch('/api/session/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:s.id})}); await loadHistory(); await loadSessions(); }; sessionsEl.appendChild(el); }); }
    async function loadHistory(){ const r=await fetch('/api/history'); const data=await r.json(); chat.innerHTML=''; (data.messages||[]).forEach(m=>addBubble(m.content, m.role==='assistant'?'bot':'user')); }

    document.getElementById('new-chat').onclick=async()=>{
      if(!isAuthed){ openAuth('signup'); return; }
      const title=prompt('Name your chat (optional):','New chat')||'New chat';
      const r=await fetch('/api/sessions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});
      if(r.ok){ await loadSessions(); await loadHistory(); }
    };

    // Consent bar
    function showMemConsent(items){
      if(!items || !items.length) return;
      memPending = items;
      if(items.length === 1){
        memText.innerHTML = `Shall I remember: <strong>${items[0].content}</strong>?`;
      }else{
        const list = items.map(x => `• ${x.content}`).join('<br>');
        memText.innerHTML = `Shall I remember:<br>${list}`;
      }
      memBar.style.display = '';
    }
    function hideMemConsent(){ memBar.style.display='none'; memPending=null; }
    memYes.onclick = async ()=>{
      if(!memPending) return hideMemConsent();
      const r = await fetch('/api/memory/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ suggestions: memPending, accept: true })});
      hideMemConsent();
      addBubble(r.ok ? "Got it — I'll remember that." : "Couldn't save just now.", 'bot');
    };
    memNo.onclick = async ()=>{
      hideMemConsent();
      await fetch('/api/memory/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ suggestions: memPending, accept: false })});
      addBubble("No problem — I won’t remember it.", 'bot');
    };

    // Slash commands
    async function handleSlash(msg){
      if (msg.startsWith('/remember ')) {
        const payload = msg.slice(10).trim();
        if (payload) {
          await fetch('/api/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:'note',content:payload,pinned:false,ttl_days:365})});
          addBubble("I'll remember that. You can manage memories in Profile.", 'bot');
        }
        return true;
      }
      if (msg === '/memories') {
        const r = await fetch('/api/memory'); const d = await r.json();
        if (!r.ok) { addBubble('Couldn’t load memories.', 'bot'); return true; }
        if (!d.memories || !d.memories.length) { addBubble('You have no saved memories yet.', 'bot'); return true; }
        const lines = d.memories.slice(0,12).map(m => `• [${m.type}] ${m.content}`);
        addBubble('Your memories:\\n' + lines.join('\\n'), 'bot'); return true;
      }
      if (msg.startsWith('/forget ')) {
        const needle = msg.slice(8).trim().toLowerCase();
        const r = await fetch('/api/memory'); const d = await r.json();
        if (r.ok && d.memories) {
          let removed = 0;
          for (const m of d.memories) {
            if ((m.content||'').toLowerCase().includes(needle)) {
              await fetch('/api/memory/'+encodeURIComponent(m.id), { method:'DELETE' });
              removed++;
            }
          }
          addBubble(removed ? `Forgot ${removed} item(s).` : 'Nothing matched to forget.', 'bot');
        } else {
          addBubble('Couldn’t load memories to forget.', 'bot');
        }
        return true;
      }
      if (msg.startsWith('/correct ')) {
        const rest = msg.slice(9); const m = rest.split('->');
        if (m.length === 2) {
          const oldTxt = m[0].trim().toLowerCase(); const newTxt = m[1].trim();
          const r = await fetch('/api/memory'); const d = await r.json();
          if (r.ok && d.memories) {
            let changed = 0;
            for (const item of d.memories) {
              if ((item.content||'').toLowerCase().includes(oldTxt)) {
                await fetch('/api/memory/'+encodeURIComponent(item.id), { method:'DELETE' });
                await fetch('/api/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:item.type||'note',content:newTxt,pinned:item.pinned,ttl_days:365})});
                changed++;
              }
            }
            addBubble(changed ? `Updated ${changed} item(s).` : 'Nothing matched to correct.', 'bot');
          } else {
            addBubble('Couldn’t load memories to correct.', 'bot');
          }
        } else {
          addBubble('Use: /correct old -> new', 'bot');
        }
        return true;
      }
      if (msg.startsWith('/tone ')) {
        const tone = msg.slice(6).trim().toLowerCase().replace(/\\s+/g,'_');
        const ok = ["warm_concise","cheerful","practical","empathetic","brief","encouraging"].includes(tone);
        if(!ok){ addBubble('Unknown tone. Try: warm_concise, cheerful, practical, empathetic, brief, encouraging.', 'bot'); return true; }
        const r = await fetch('/api/tone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tone})});
        const d = await r.json();
        if(r.ok){ addBubble('Tone updated to '+tone.replace('_',' ')+'.', 'bot'); } else { addBubble(d.error||'Could not update tone.','bot'); }
        return true;
      }
      return false;
    }

    async function sendMessage(){
      if(!isAuthed){ openAuth('signup'); return; }
      const msg=input.value.trim(); if(!msg) return;
      if(await handleSlash(msg)) { input.value=''; return; }

      input.value=''; addBubble(msg,'user');
      const res=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
      if(res.status===401){ openAuth('login'); return; }
      if(res.status===402){ const d=await res.json(); addBubble(d.error || 'Your free trial has ended. Please upgrade to continue.', 'bot'); return; }
      if(!res.ok){ addBubble('Error: '+(await res.text()),'bot'); return; }

      const reader=res.body.getReader(); const decoder=new TextDecoder(); let buf='', acc=''; const bubbleEl=makeBotBubble('…');
      while(true){ const {value,done}=await reader.read(); if(done) break;
        buf+=decoder.decode(value,{stream:true});
        const parts=buf.split("\\n\\n"); buf=parts.pop()||'';
        for(const part of parts){
          if(!part.startsWith('data:')) continue;
          const raw = part.slice(5);

          if (raw.startsWith('__KF_MEM__:')) {
            try { const payload = JSON.parse(raw.replace('__KF_MEM__:', '')); if (payload && payload.type === 'mem_suggest') { showMemConsent(payload.items); } } catch(e){}
            continue;
          }
          if (raw.startsWith('__KF_NOTE__:')) {
            try { const payload = JSON.parse(raw.replace('__KF_NOTE__:', '')); if (payload && payload.text) { addBubble(payload.text, 'bot'); } } catch(e){}
            continue;
          }

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

    // open auth from landing links
    (function(){ const p=new URLSearchParams(location.search); const m=p.get('mode'); const b=p.get('billing');
      if(b==='success'){ addBubble('Thank you! Your subscription is active. 💚 50% is donated to Samaritans.', 'bot'); }
      else if(b==='cancel'){ addBubble('No problem — you can upgrade any time. 💚 50% goes to Samaritans.', 'bot'); }
      if(m){ openAuth(m); }
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

# Serve local images and assets from ./static at /static
app.mount("/static", StaticFiles(directory="static"), name="static")

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
    bot_avatar = (d.get("bot_avatar") or "").strip() or None
    if not username or not password:
        return JSONResponse({"error": "Username and password required"}, status_code=400)
    if get_user_by_username(username):
        return JSONResponse({"error": "Username already taken"}, status_code=409)
    uid = create_user(username, password, bot_name=bot_name, bot_avatar=bot_avatar)
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

    # update last_seen on each load (lightweight analytics)
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), user["id"]))
        conn.commit()

    allowed, days_left = trial_info(user)
    due, note = checkin_due_and_text(user)
    if due:
        mark_checkin_sent_today(user["id"], os.getenv("APP_TZ", "Europe/London"))
    safe = {"id": user["id"], "username": user["username"], "display_name": user["display_name"],
            "bio": user["bio"], "bot_name": user["bot_name"], "bot_avatar": user["bot_avatar"],
            "subscription_status": user["subscription_status"]}
    trial = {"is_allowed": allowed, "days_remaining": days_left if days_left is not None else None}
    return JSONResponse({"user": safe, "trial": trial, "subscription_status": user["subscription_status"], "checkin_due_text": note})

@app.post("/api/profile")
async def api_profile(request: Request):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    d = await request.json()
    update_user_profile(uid, d.get("display_name"), d.get("bio"), d.get("bot_name"), d.get("bot_avatar"))
    return JSONResponse({"ok": True})

# ---- Tone APIs ----
class ToneIn(BaseModel):
    tone: str

@app.get("/api/tone")
async def api_get_tone(request: Request):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    return JSONResponse({"tone": get_user_tone(uid), "presets": TONE_PRESETS})

@app.post("/api/tone")
async def api_set_tone(request: Request, body: ToneIn):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    try:
        key = set_user_tone(uid, body.tone)
    except ValueError:
        return JSONResponse({"error": "Invalid tone"}, status_code=400)
    return JSONResponse({"ok": True, "tone": key})

# ---- Check-in APIs ----
class CheckInIn(BaseModel):
    enabled: bool
    hour: Optional[int] = None
    minute: Optional[int] = None

@app.get("/api/checkin")
async def api_get_checkin(request: Request):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    user = get_user_by_id(uid)
    return JSONResponse({"enabled": bool(user.get("checkin_enabled") or 0),
                         "hour": user.get("checkin_hour"),
                         "minute": user.get("checkin_minute")})

@app.post("/api/checkin")
async def api_set_checkin(request: Request, body: CheckInIn):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    h = body.hour if body.enabled else None
    m = body.minute if body.enabled else None
    if body.enabled:
        if h is None or m is None or not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            return JSONResponse({"error":"Invalid time"}, status_code=400)
    set_checkin(uid, body.enabled, h, m)
    return JSONResponse({"ok": True})

# ---- Memory APIs ----
class MemoryIn(BaseModel):
    type: str = "note"
    content: str
    pinned: bool = False
    ttl_days: int | None = None

class MemoryConfirmIn(BaseModel):
    suggestions: list[dict]
    accept: bool = True

@app.get("/api/memory")
async def api_memory_list(request: Request):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    return JSONResponse({"memories": list_memories(uid)})

@app.post("/api/memory")
async def api_memory_add(request: Request, m: MemoryIn):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    lowered = m.content.lower()
    if any(x in lowered for x in ["password", "credit card", "card number", "ssn", "nhs number"]):
        return JSONResponse({"error":"For your safety, we don't store sensitive credentials."}, status_code=400)
    mid = add_memory(uid, m.type, m.content, pinned=m.pinned, ttl_days=m.ttl_days)
    return JSONResponse({"ok": True, "id": mid})

@app.delete("/api/memory/{mem_id}")
async def api_memory_delete(request: Request, mem_id: str):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error": "Sign in required"}, status_code=401)
    ok = delete_memory(uid, mem_id)
    return JSONResponse({"ok": ok})

@app.post("/api/memory/confirm")
async def api_memory_confirm(request: Request, body: MemoryConfirmIn):
    uid = get_current_user_id(request)
    if not uid: return JSONResponse({"error":"Sign in required"}, status_code=401)
    if not body.accept:
        return JSONResponse({"ok": True, "saved": 0})
    saved = 0
    for m in body.suggestions:
        t = str(m.get("type") or "note")
        c = (m.get("content") or "").strip()
        pinned = bool(m.get("pinned") or False)
        ttl = m.get("ttl_days", None)
        if c and len(c) <= 300:
            try:
                add_memory(uid, t, c, pinned=pinned, ttl_days=ttl)
                saved += 1
            except Exception:
                pass
    return JSONResponse({"ok": True, "saved": saved})

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

    # mark seen
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), uid))
        conn.commit()

    guard = crisis_guard(user_message)
    if guard:
        save_message(sid, "user", user_message, uid); save_message(sid, "assistant", guard, uid)
        def gen_safe():
            yield "data: " + guard.replace("\\n","\\\\n") + "\\n\\n"
            yield "data: [DONE]\\n\\n"
        resp = StreamingResponse(gen_safe(), media_type="text/event-stream"); resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90); return resp

    save_message(sid, "user", user_message, uid)

    tz_note = current_time_note()
    tone_key = get_user_tone(uid)
    tone_text = TONE_PRESETS.get(tone_key, TONE_PRESETS["warm_concise"])
    history = [
        {"role":"system","content": SYSTEM_PROMPT},
        {"role":"system","content": f"Adopt a {tone_text} tone in all replies."},
        {"role":"system","content": f"The bot's display name is '{user.get('bot_name') or 'Kind Friend'}'."},
        {"role":"system","content": tz_note},
    ]
    hint = time_context_hint(user_message)
    if hint:
        history.append({"role": "system", "content": hint})
    mem_lines = get_context_memories(uid, max_items=8)
    if mem_lines:
        history.append({"role":"system","content": "Known user context:\\n" + "\\n".join(mem_lines)})
    history.extend(get_recent_messages(sid, 20))
    history.append({"role":"user","content":user_message})

    mem_suggestions = suggest_memories_from_text(user_message)

    due, note = checkin_due_and_text(user)
    if due:
        mark_checkin_sent_today(user["id"], os.getenv("APP_TZ", "Europe/London"))

    def event_stream():
        try:
            if mem_suggestions:
                payload = json.dumps({"type":"mem_suggest","items":mem_suggestions})
                yield "data: __KF_MEM__:" + payload + "\\n\\n"
            if due and note:
                yield "data: __KF_NOTE__:" + json.dumps({"text": note}) + "\\n\\n"

            stream = client.chat.completions.create(model=MODEL_NAME, messages=history, temperature=0.7, stream=True)
            parts = []
            for chunk in stream:
                delta = None
                try:
                    delta = chunk.choices[0].delta.content
                except Exception:
                    try: delta = chunk.choices[0].message.content
                    except Exception: delta = None
                if not delta: continue
                parts.append(delta)
                yield "data: " + delta.replace("\\n","\\\\n") + "\\n\\n"
            final = "".join(parts)
            save_message(sid, "assistant", final, uid)
            yield "data: [DONE]\\n\\n"
        except Exception as e:
            yield "data: " + ("[Error] " + str(e)).replace("\\n","\\\\n") + "\\n\\n"
            yield "data: [DONE]\\n\\n"

    resp = StreamingResponse(event_stream(), media_type="text/event-stream")
    resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90)
    return resp

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

    # mark seen
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), uid))
        conn.commit()

    guard = crisis_guard(user_message)
    if guard:
        save_message(sid, "user", user_message, uid); save_message(sid, "assistant", guard, uid)
        resp = JSONResponse({"reply": guard}); resp.set_cookie("session_id", sid, httponly=True, samesite="Lax", max_age=60*60*24*90); return resp

    save_message(sid, "user", user_message, uid)

    tz_note = current_time_note()
    tone_key = get_user_tone(uid)
    tone_text = TONE_PRESETS.get(tone_key, TONE_PRESETS["warm_concise"])
    history = [
        {"role":"system","content": SYSTEM_PROMPT},
        {"role":"system","content": f"Adopt a {tone_text} tone in all replies."},
        {"role":"system","content": f"The bot's display name is '{user.get('bot_name') or 'Kind Friend'}'."},
        {"role":"system","content": tz_note},
    ]
    hint = time_context_hint(user_message)
    if hint:
        history.append({"role": "system", "content": hint})
    mem_lines = get_context_memories(uid, max_items=8)
    if mem_lines:
        history.append({"role":"system","content": "Known user context:\\n" + "\\n".join(mem_lines)})
    history.extend(get_recent_messages(sid, 20))
    history.append({"role":"user","content": user_message})

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
    if not BILLING_RETURN_URL: return "BILLING_RETURN_URL is required (e.g., https://kindfriend.onrender.com/app)."
    return None
def _get_or_create_customer(user):
    if user.get("stripe_customer_id"): return user["stripe_customer_id"]
    cust = stripe.Customer.create(email=f"{user['username']}@example.local", metadata={"kf_user_id": user["id"]})
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

