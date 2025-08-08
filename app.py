import os
import sqlite3
import uuid
import time
import datetime
import csv
import io
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse
import openai

# --- Config ---
openai.api_key = os.environ.get("OPENAI_API_KEY")
MODEL_NAME = "gpt-4o-mini"  # change if you prefer another available model

DB_FILE = "kindfriend.db"

# --- DB init ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            session_id TEXT,
            role TEXT,
            content TEXT,
            ts REAL,
            archived INTEGER DEFAULT 0
        )
        """)
        conn.commit()

init_db()

# --- FastAPI ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- HTML / Frontend ---
INDEX_HTML = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>KindFriend</title>

  <!-- Custom KF favicon with cache-buster -->
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
</svg>?v=2'>

  <style>
    :root {
      --bg: #0b1020;
      --bg2: #0f152c;
      --card: rgba(255,255,255,0.08);
      --border: rgba(255,255,255,0.10);
      --text: #e7eaf3;
      --muted: #a7b0c4;
      --brand: #7c9cff;
      --brand-2: #5fe1d9;
      --user: #4456ff;
      --bot: #16c79a;
      --shadow: 0 10px 25px rgba(0,0,0,0.25);
      --radius: 16px;
    }
    [data-theme="light"] {
      --bg: #eef2ff;
      --bg2: #e8ecff;
      --card: rgba(255,255,255,0.9);
      --border: rgba(0,10,40,0.1);
      --text: #0b1020;
      --muted: #3d4966;
      --brand: #2b4cff;
      --brand-2: #05bdb0;
      --user: #2b4cff;
      --bot: #059669;
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
      display: grid;
      place-items: center;
      padding: 24px;
    }
    .app { width: min(900px, 100%); display: grid; grid-template-rows: auto 1fr auto; gap: 16px; }
    .card { background: var(--card); backdrop-filter: blur(10px); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); }
    .header { display: flex; align-items: center; gap: 12px; padding: 14px 16px; }
    .logo { width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; background: linear-gradient(135deg, var(--brand), var(--brand-2)); color: white; font-weight: 700; letter-spacing: 0.5px; box-shadow: var(--shadow); }
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
    const root = document.documentElement;
    const saved = localStorage.getItem('kf-theme');
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
      const cur = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', cur);
      localStorage.setItem('kf-theme', cur);
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
        const data = await res.json();
        if (data.error) {
          addBubble('Error: ' + (data.error_detail || data.error), 'bot');
        } else {
          addBubble(data.reply, 'bot');
        }
      } catch (err) {
        addBubble('Network error: ' + err.message, 'bot');
      } finally {
        setTyping(false);
      }
    };

    send.addEventListener('click', sendMessage);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); sendMessage(); } });

    newBtn.addEventListener('click', async () => {
      await fetch('/api/new', { method: 'POST' });
      chat.innerHTML = '';
      addBubble('New chat started.', 'bot');
    });

    dlTxt.addEventListener('click', () => { window.location = '/api/export?fmt=txt'; });
    dlCsv.addEventListener('click', () => { window.location = '/api/export?fmt=csv'; });
  </script>
</body>
</html>"""

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)

@app.post("/api/chat")
async def chat(request: Request, response: Response):
    data = await request.json()
    msg = data.get("message", "")

    session_id = request.cookies.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())

    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO messages (session_id, role, content, ts) VALUES (?, ?, ?, ?)",
                  (session_id, "user", msg, time.time()))
        conn.commit()
        c.execute("SELECT role, content FROM messages WHERE session_id = ? ORDER BY ts DESC LIMIT 20", (session_id,))
        history = [{"role": r, "content": c_} for r, c_ in reversed(c.fetchall())]

    try:
        res = openai.ChatCompletion.create(
            model=MODEL_NAME,
            messages=history
        )
        reply = res.choices[0].message["content"]
    except Exception as e:
        return JSONResponse({"error": "OpenAI API error", "error_detail": str(e)})

    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO messages (session_id, role, content, ts) VALUES (?, ?, ?, ?)",
                  (session_id, "bot", reply, time.time()))
        conn.commit()

    resp = JSONResponse({"reply": reply})
    resp.set_cookie("session_id", session_id, httponly=True)
    return resp

@app.post("/api/new")
async def new_chat(request: Request, response: Response):
    session_id = request.cookies.get("session_id")
    if session_id:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session_id", str(uuid.uuid4()), httponly=True)
    return resp

@app.get("/api/export")
async def export_chat(request: Request, fmt: str = "txt"):
    session_id = request.cookies.get("session_id")
    if not session_id:
        return PlainTextResponse("No session", status_code=400)

    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT role, content, ts, archived FROM messages WHERE session_id = ? ORDER BY ts", (session_id,))
        msgs = c.fetchall()

    now = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["time_utc", "role", "content", "archived"])
        for role, content, ts, archived in msgs:
            t = datetime.datetime.utcfromtimestamp(ts).isoformat() + "Z"
            writer.writerow([t, role, content, archived])
        return PlainTextResponse(output.getvalue(), headers={
            "Content-Disposition": f'attachment; filename="kindfriend_{now}.csv"',
            "Content-Type": "text/csv; charset=utf-8",
        })
    else:
        lines = []
        for role, content, ts, archived in msgs:
            t = datetime.datetime.utcfromtimestamp(ts).isoformat() + "Z"
            lines.append(f"[{t}] {role}: {content}")
        return PlainTextResponse("\n".join(lines), headers={
            "Content-Disposition": f'attachment; filename="kindfriend_{now}.txt"',
            "Content-Type": "text/plain; charset=utf-8",
        })

