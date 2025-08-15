import os
import sys
import time
import uuid
import io
import csv
import datetime
import sqlite3

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# =========================
# Config & Persistent Paths
# =========================
DATA_DIR = os.getenv("DATA_DIR", "")  # e.g. /opt/data on Render
DB_FILE = os.path.join(DATA_DIR, "kindfriend.db") if DATA_DIR else "kindfriend.db"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")

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
    "You are KindFriend: a warm, respectful companion. You are not a therapist. "
    "If the user mentions self-harm or immediate danger, kindly suggest contacting UK Samaritans (116 123), "
    "NHS 111, or emergency services (999). Be concise and kind."
)

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
                archived   INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()

def save_message(session_id: str, role: str, content: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (session_id, role, content, ts, archived) VALUES (?, ?, ?, ?, 0)",
            (session_id, role, content, time.time()),
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

init_db()

# =========================
# Frontend (HTML/JS/CSS)
# =========================
INDEX_HTML = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>KindFriend</title>

  <!-- KF favicon with cache-buster -->
  <link rel="icon" href='data:image/svg+xml;utf8,
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="%237c9cff"/>
      <stop offset="100%" stop-color="%235fe1d9"/>
    </linearGradient>
  </defs>
  <circle cx="32" cy="32" r="30" fill="url(%23g)" />
  <text x="32" y="38" font-size="24" font-family="Arial Rounded MT Bold, Helvetica, sans-serif" text-anchor="middle" fill="white">KF</text>
  <path d="M22 44 q10 8 20 0" stroke="white" stroke-width="2" fill="none" stroke-linecap="round"/>
</svg>?v=6'>

  <style>
    :root {
      --bg: #0b1020; --bg2: #0f152c;
      --card: rgba(255,255,255,0.08); --border: rgba(255,255,255,0.10);
      --text: #e7eaf3; --muted: #a7b0c4;
      --brand: #7c9cff; --brand-2: #5fe1d9;
      --user: #4456ff; --bot: #16c79a;
      --shadow: 0 10px 25px rgba(0,0,0,0.25); --radius: 16px;
    }
    [data-theme="light"] {
      --bg: #eef2ff; --bg2: #e8ecff;
      --card: rgba(255,255,255,0.9); --border: rgba(0,10,40,0.1);
      --text: #0b1020; --muted: #3d4966;
      --brand: #2b4cff; --brand-2: #05bdb0;
      --user: #2b4cff; --bot: #059669;
      --shadow: 0 10px 25px rgba(0,0,0,0.08);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      color: var(--text);
      background:
        radial-gradient(1200px 600px at -10% -20%, #223 0%, transparent 60%),
        radial-gradient(1200px 600px at 110% 120%, #133 0%, transparent 60%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg2) 100%);
      font: 16px/1.5 system-ui, sans-serif;
      display: grid; place-items: center; padding: 24px;
    }
    .app { width: min(900px, 100%); display: grid; grid-template-rows: auto 1fr auto; gap: 16px; }
    .card { background: var(--card); backdrop-filter: blur(10px); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); }
    .header { display: flex; align-items: center; gap: 12px; padding: 14px 16px; }
    .logo { width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; background: linear-gradient(135deg, var(--brand), var(--brand-2)); color: white; font-weight: 700; }
    .title-wrap { flex: 1; }
    .title { margin: 0; font-size: 18px; font-weight: 800; }
    .subtitle { margin: 2px 0 0; font-size: 12px; color: var(--muted); }
    .toolbar { display: flex; gap: 8px; }
    .btn { border: 1px solid var(--border); background: transparent; color: var(--text); padding: 8px 12px; border-radius: 999px; cursor: pointer; font-weight: 600; }
    .btn.primary { background: linear-gradient(135deg, var(--brand), var(--brand-2)); border-color: transparent; color: white; }
    .chat { padding: 12px; display: grid; gap: 10px; overflow: auto; height: min(62vh, 560px); }
    .row { display: flex; gap: 10px; }
    .row.user { justify-content: flex-end; }
    .row.bot { justify-content: flex-start; }
    .bubble { max-width: 72%; padding: 10px 12px; border-radius: 14px; border: 1px solid var(--border); white-space: pre-wrap; }
    .user .bubble { background: rgba(68,86,255,0.18); }
    .bot .bubble  { background: rgba(22,199,154,0.14); }
    .avatar { width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; font-size: 14px; font-weight: 700; background: rgba(255,255,255,0.08); }
    .row.user .avatar { display: none; }
    .row.bot .avatar { background: linear-gradient(135deg, var(--brand), var(--bot)); color: white; }
    .typing { display: none; padding: 0 16px 12px; color: var(--muted); font-size: 13px; }
    .typing.on { display: block; }
    .input-wrap { display: grid; grid-template-columns: 1fr auto; gap: 8px; padding: 12px; }
    .input { padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border); color: var(--text); background: rgba(255,255,255,0.06); }
    .hint { padding: 0 16px 16px; color: var(--muted); font-size: 12px; }
  </style>
</head>
<body>
  <div class="app">
    <div class="card header">
      <div class="logo">KF</div>
      <div class="title-wrap">
        <h1 class="title">KindFriend</h1>
        <p class="subtitle">A gentle, privacy-first companion. Your chats stay on your server.</p>
      </div>
      <div class="toolbar">
        <button id="theme" class="btn">Theme</button>
        <button id="download-txt" class="btn">.txt</button>
        <button id="download-csv" class="btn">.csv</button>
        <button id="new-chat" class="btn primary">New chat</button>
      </div>
    </div>

    <div class="card chat" id="chat"></div>
    <div id="typing" class="typing">KindFriend is typing…</div>

    <div class="card input-wrap">
      <input id="message" class="input" autocomplete="off" placeholder="Type a message…" />
      <button id="send" class="btn primary">Send</button>
    </div>
    <div class="hint">KindFriend is a supportive companion, not a therapist. In crisis, call 999 or Samaritans 116 123 (UK).</div>
  </div>

  <script>
    const root   = document.documentElement;
    const saved  = localStorage.getItem('kf-theme');
    if (saved) root.setAttribute('data-theme', saved);

    const chat   = document.getElementById('chat');
    const input  = document.getElementById('message');
    const send   = document.getElementById('send');
    const typing = document.getElementById('typing');
    const newBtn = document.getElementById('new-chat');
    const dlTxt  = document.getElementById('download-txt');
    const dlCsv  = document.getElementById('download-csv');
    const themeBtn = document.getElementById('theme');

    themeBtn.addEventListener('click', () => {
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      localStorage.setItem('kf-theme', next);
    });

    const addBubble = (text, who) => {
      const row = document.createElement('div');
      row.className = 'row ' + who;
      const avatar = document.createElement('div');
      avatar.className = 'avatar';
      avatar.textContent = (who === 'bot') ? 'KF' : 'You';
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      bubble.textContent = text;
      if (who === 'bot') row.appendChild(avatar);
      row.appendChild(bubble);
      chat.appendChild(row);
      chat.scrollTop = chat.scrollHeight;
    };

    const setTyping = (on) => typing.classList.toggle('on', on);

    const sendMessage = async () => {
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
          if (data.error) {
            addBubble('Error: ' + (data.error_detail || data.error), 'bot');
          } else {
            addBubble(data.reply, 'bot');
          }
        } else {
          const text = await res.text();
          addBubble('Server error: ' + text, 'bot');
        }
      } catch (err) {
        addBubble('Network error: ' + err.message, 'bot');
      } finally {
        setTyping(false);
      }
    };

    send.addEventListener('click', sendMessage);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    newBtn.addEventListener('click', async () => {
      try {
        const res = await fetch('/api/new', { method: 'POST' });
        const ok  = (res.headers.get('content-type') || '').includes('application/json') && (await res.json()).ok;
        chat.innerHTML = '';
        addBubble(ok ? 'New chat started.' : 'Error starting new chat.', 'bot');
      } catch (e) {
        addBubble('Network error: ' + e.message, 'bot');
      }
    });

    dlTxt.addEventListener('click', () => { window.location = '/api/export?fmt=txt'; });
    dlCsv.addEventListener('click', () => { window.location = '/api/export?fmt=csv'; });
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
    # Basic health info: DB path and API availability (no secrets)
    db_exists = os.path.exists(DB_FILE)
    return JSONResponse({
        "ok": True,
        "api_available": API_AVAILABLE,
        "db_path": DB_FILE,
        "db_exists": db_exists,
    })

@app.post("/api/chat")
async def api_chat(request: Request):
    if not API_AVAILABLE:
        return JSONResponse({"error": "Service is not configured with an API key."}, status_code=500)

    data = await request.json()
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    session_id = request.cookies.get("session_id") or str(uuid.uuid4())
    save_message(session_id, "user", user_message)

    history = [{"role": "system", "content": SYSTEM_PROMPT}]
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

    save_message(session_id, "assistant", reply)

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

