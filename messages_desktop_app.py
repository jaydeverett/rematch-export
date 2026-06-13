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
import signal
import urllib.request
import urllib.error

from flask import Flask, render_template, request, jsonify, send_file
import messages

DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
VERY_LARGE_LIMIT = 10_000_000
PORT = 5050
APP_NAME = 'RematchExport'

# Rematch backend — the pairing ingest endpoint the phone authorizes with a code.
INGEST_URL = "https://rematch-app-orpin.vercel.app/api/imessage/ingest"

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for packaged app."""
    if hasattr(sys, '_MEIPASS'):
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

    summaries.sort(key=lambda x: x["count"], reverse=True)

    individual = [c for c in summaries if not c["is_group"]]
    groups = [c for c in summaries if c["is_group"]]

    return individual, groups


# -------------------------
# Flask Routes
# -------------------------

@app.route("/")
def index():
    has_access = has_full_disk_access()
    if not has_access:
        open_full_disk_access_settings()
    return render_template('app.html', app_name=APP_NAME, has_access=has_access)


@app.route("/check-access")
def check_access():
    return jsonify({"granted": has_full_disk_access()})


@app.route("/api/conversations")
def api_conversations():
    individual, groups = get_chat_summaries()
    return jsonify({"individual": individual, "groups": groups})


def build_export_data(selected_ids):
    """Build the flat {chat, date, from_me, text} array for the selected chats.
    Shared by the file download (/export) and the phone handoff (/send-to-phone)."""
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

    return export_data


@app.route("/export", methods=["POST"])
def export():
    data = request.get_json()
    export_data = build_export_data(data.get("chat_ids", []))

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")

    with open(temp.name, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2)

    today = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        temp.name,
        as_attachment=True,
        download_name=f"Rematch Export - {today}.json"
    )


@app.route("/send-to-phone", methods=["POST"])
def send_to_phone():
    """Send the selected conversations straight to the user's Rematch account via
    the 6-char pairing code shown in the phone app — no file handling. The code
    authorizes depositing one export; the phone then claims it."""
    data = request.get_json()
    code = (data.get("code") or "").strip().upper()
    if not code:
        return jsonify({"ok": False, "error": "Enter the code from your Rematch app."}), 400

    export_data = build_export_data(data.get("chat_ids", []))
    if not export_data:
        return jsonify({"ok": False, "error": "No messages found in the selected conversations."}), 400

    body = json.dumps({"code": code, "payload": export_data}).encode("utf-8")
    req = urllib.request.Request(
        INGEST_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return jsonify({"ok": True, "conversationCount": result.get("conversationCount")})
    except urllib.error.HTTPError as e:
        # The ingest endpoint returns a friendly message (bad/expired code, etc.).
        try:
            err = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:
            err = None
        return jsonify({"ok": False, "error": err or "That code isn't valid. Grab a fresh one in the Rematch app."})
    except Exception:
        return jsonify({"ok": False, "error": "Couldn't reach Rematch. Check your connection and try again."})


# -------------------------
# App Launcher
# -------------------------

def kill_port(host: str, port: int) -> bool:
    """Kill any process listening on the given host:port."""
    killed = False
    result = subprocess.run(
        ["lsof", "-ti", f":{port}"],
        capture_output=True, text=True
    )
    pids = result.stdout.strip().splitlines()
    for pid in pids:
        if pid:
            os.kill(int(pid), signal.SIGKILL)
            killed = True
    return killed

def launch_browser():
    webbrowser.open(f"http://127.0.0.1:{PORT}")

if __name__ == "__main__":
    kill_port('127.0.0.1', PORT)
    time.sleep(2.0)
    threading.Timer(1.0, launch_browser).start()
    app.run(port=PORT, debug=False)
