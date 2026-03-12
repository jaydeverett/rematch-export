'''
pyinstaller --windowed --onefile --name "RematchExport" --add-data "templates:templates" messages_desktop_app.py
'''

import os
import sqlite3
import json
import time
import tempfile
import threading
import webbrowser
from datetime import datetime
import sys
import subprocess
import socket
import signal

from flask import Flask, render_template_string, request, send_file
from flask import render_template
import messages

DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
VERY_LARGE_LIMIT = 10_000_000
PORT = 5050
APP_NAME = 'RematchExport'

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for packaged app."""
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller unpacks to a temp folder
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)

app = Flask(__name__, template_folder=resource_path('templates'))

# -------------------------
# Permission Handling
# -------------------------

def has_full_disk_access():
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM chat LIMIT 1;")
        conn.close()
        return True
    except Exception:
        return False


def open_full_disk_access_settings():
    subprocess.Popen([
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    ])


def get_executable_name():
    if getattr(sys, 'frozen', False):
        return os.path.basename(sys.executable)
    else:
        return APP_NAME


# -------------------------
# Chat Summary
# -------------------------

def get_chat_summaries():
    db = messages.get_db()
    summaries = []

    for chat_summary in db.chats():
        msgs = list(db.messages(chat_id=chat_summary.id, limit=VERY_LARGE_LIMIT))

        if not msgs:
            continue

        dates = [m.date for m in msgs if m.date]

        if not dates:
            continue

        earliest = min(dates).strftime("%Y-%m-%d")
        latest = max(dates).strftime("%Y-%m-%d")

        chat_name = chat_summary.display_name or chat_summary.identifier

        # Check participant count to detect group chats
        full_chat = db.chat(chat_summary.id)
        is_group = len(full_chat.participants) > 1

        summaries.append({
            "id": chat_summary.id,
            "name": chat_name,
            "count": len(msgs),
            "earliest": earliest,
            "latest": latest,
            "is_group": is_group
        })

    # Sort by message count descending
    summaries.sort(key=lambda x: x["count"], reverse=True)

    # Split into individual and group
    individual = [c for c in summaries if not c["is_group"]]
    groups = [c for c in summaries if c["is_group"]]

    return individual, groups


# -------------------------
# Flask Routes
# -------------------------

@app.route("/")
def index():
    if not has_full_disk_access():
        open_full_disk_access_settings()
        app_name = get_executable_name()

        return render_template('access_template.html', app_name=APP_NAME)

    individual, groups = get_chat_summaries()
    return render_template('main_template.html', app_name=APP_NAME, individual=individual, groups=groups)

@app.route("/check-access")
def check_access():
    if has_full_disk_access():
        # Restart after a short delay so the response can be sent first
        threading.Timer(0.5, lambda: os.execv(sys.executable, [sys.executable] + sys.argv)).start()
        return {"granted": True}
    return {"granted": False}


@app.route("/export", methods=["POST"])
def export():
    selected_ids = request.form.getlist("chat_ids")

    db = messages.get_db()
    export_data = []

    for chat_id in selected_ids:
        chat_id = int(chat_id)
        chat = db.chat(chat_id)
        chat_name = chat.display_name or chat.identifier

        for msg in db.messages(chat_id=chat_id, limit=VERY_LARGE_LIMIT):
            export_data.append({
                "chat": chat_name,
                "date": msg.date.strftime("%Y-%m-%d") if msg.date else None,
                "from_me": msg.is_from_me,
                "text": msg.text
            })

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")

    with open(temp.name, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2)

    today = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        temp.name,
        as_attachment=True,
        download_name=f"Rematch Export - {today}.json"
    )


# -------------------------
# App Launcher
# -------------------------


def kill_port(host: str, port: int) -> bool:
    """Kill any process listening on the given host:port. Returns True if something was killed."""
    killed = False
    
    if sys.platform == "win32":
        # Windows
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.strip().split()[-1]
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                print(f"Killed PID {pid} on port {port}")
                killed = True
    else:
        # macOS / Linux
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().splitlines()
        for pid in pids:
            if pid:
                os.kill(int(pid), signal.SIGKILL)
                print(f"Killed PID {pid} on port {port}")
                killed = True

    return killed

def launch_browser():
    webbrowser.open(f"http://127.0.0.1:{PORT}")

if __name__ == "__main__":
    kill_port('127.0.0.1', PORT)
    time.sleep(2.0)
    threading.Timer(1.0, launch_browser).start()
    app.run(port=PORT, debug=False)
