'''
Build the signed, notarizable .app from RematchExport.spec (NOT a one-liner):

    ./build.sh            # clean -> PyInstaller (onedir .app, custom icon) -> codesign -> DMG

The committed spec uses --onedir (a proper .app bundle), which launches far faster
on repeat opens than --onefile (which unpacks to a temp dir every launch). Notarize
+ staple steps print at the end of build.sh. See build.sh / RematchExport.spec.
'''

import os
import sqlite3
import json
import time
import gzip
import tempfile
import threading
import webbrowser
from datetime import datetime
import sys
import subprocess
import signal
import urllib.request
import urllib.error
import urllib.parse
import ssl
import certifi

from flask import Flask, render_template, request, jsonify, send_file
import messages

DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")
VERY_LARGE_LIMIT = 10_000_000
PORT = 5050
APP_NAME = 'RematchExport'
# Shown in the UI footer and returned by /api/health — bump with each release so
# support can tell at a glance which build a tester is running (a v1.6.x debugging
# session burned hours because no build was identifiable from screenshots).
APP_VERSION = "1.7.0"

# Rematch backend — the pairing ingest endpoint the phone authorizes with a code.
INGEST_URL = "https://rematch-app-orpin.vercel.app/api/imessage/ingest"
# QR pairing (reverse direction): this Mac mints an unbound code, shows it as a
# QR, the phone scans + links it, and the send then uses that code via /ingest.
PAIR_MAC_URL = "https://rematch-app-orpin.vercel.app/api/imessage/pair/mac"
PAIR_PEEK_URL = "https://rematch-app-orpin.vercel.app/api/imessage/pair/peek"

# qrcode is a pure-python dep bundled by PyInstaller. If a build ever misses it,
# the QR panel silently stays hidden and the typed-code path still works.
try:
    import qrcode
    import qrcode.image.svg
    QR_AVAILABLE = True
except Exception:
    QR_AVAILABLE = False

# Trust roots for the HTTPS call above. A PyInstaller-bundled Python ships its own
# OpenSSL but NO CA certificate bundle, so a bare urlopen() fails verification with
# SSL: CERTIFICATE_VERIFY_FAILED — which surfaced to users as "Couldn't reach
# Rematch" on every send-to-phone from the packaged .app. certifi (bundled into the
# build, see RematchExport.spec) provides the roots so HTTPS works in the bundle.
try:
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CONTEXT = ssl.create_default_context()

def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for packaged app."""
    if hasattr(sys, '_MEIPASS'):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)

app = Flask(__name__, template_folder=resource_path('templates'))


@app.after_request
def no_cache(resp):
    """Disable HTTP caching on every response.

    This is a localhost tool served from a long-lived URL (127.0.0.1:5050) that the
    browser reuses across app versions and launches. A cached app.html means the
    CURRENT page's JS never runs (the browser replays an old page); a cached
    /check-access masks a just-granted permission. Both are what left the FDA
    "Waiting for permission" page spinning until a manual refresh. Nothing here
    benefits from caching, so turn it off everywhere.
    """
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


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

    # Iterate LOGICAL conversations: chat.db fragments a long group across several
    # `chat` rows (rename / member handle change), so we coalesce them first — else
    # a years-long group shows up as multiple entries each with a partial count.
    for lc in db.logical_chats():
        msgs = db.messages_union(lc.member_ids, limit=VERY_LARGE_LIMIT)

        if not msgs:
            continue

        dates = [m.date for m in msgs if m.date]

        if not dates:
            continue

        summaries.append({
            "id": lc.id,
            "name": db.logical_name(lc),
            "count": len(msgs),
            "earliest": min(dates).strftime("%Y-%m-%d"),
            "latest": max(dates).strftime("%Y-%m-%d"),
            "is_group": lc.is_group
        })

    summaries.sort(key=lambda x: x["count"], reverse=True)

    individual = [c for c in summaries if not c["is_group"]]
    groups = [c for c in summaries if c["is_group"]]

    return individual, groups


# -------------------------
# Flask Routes
# -------------------------

def app_bundle_container() -> str:
    """The folder that holds RematchExport.app — shown in the Full Disk Access
    steps so the user can point the "+" file picker straight at it. Leading with
    "+" (instead of waiting for macOS to auto-list the app) makes granting access
    deterministic and instant regardless of TCC's auto-populate timing. Empty when
    running unfrozen (dev)."""
    if not getattr(sys, "frozen", False):
        return ""
    # sys.executable: .../RematchExport.app/Contents/MacOS/RematchExport
    exe = os.path.realpath(sys.executable)
    bundle = os.path.dirname(os.path.dirname(os.path.dirname(exe)))  # RematchExport.app
    return os.path.dirname(bundle)


@app.route("/")
def index():
    has_access = has_full_disk_access()
    if not has_access:
        open_full_disk_access_settings()
    return render_template(
        "app.html",
        app_name=APP_NAME,
        has_access=has_access,
        app_dir=app_bundle_container(),
    )


@app.route("/check-access")
def check_access():
    # Polled while the user grants Full Disk Access; no-cache is handled globally
    # by the after_request hook so a stale {granted: false} can't be replayed.
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

    # The UI sends back logical-conversation ids (each a min member ROWID). Expand
    # each to all of its constituent chat rows so a selected group exports its FULL
    # history even when iMessage fragmented it across several rows.
    logical = {lc.id: lc for lc in db.logical_chats()}

    for selected_id in selected_ids:
        selected_id = int(selected_id)
        lc = logical.get(selected_id)
        if lc is not None:
            member_ids = lc.member_ids
            chat_name = db.logical_name(lc)
        else:
            # Defensive fallback: an id we didn't surface — export just that row.
            member_ids = [selected_id]
            chat_name = db.name_for(db.chat(selected_id))

        for msg in db.messages_union(member_ids, limit=VERY_LARGE_LIMIT):
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


@app.route("/api/health")
def api_health():
    """Reachability probe the UI runs on load: can this Mac reach the Rematch
    backend? Any HTTP response from the ingest endpoint (405 for GET is expected —
    it's POST-only) proves connectivity end-to-end through whatever network/TLS
    path send-to-phone will use; only a transport failure means unreachable. This
    surfaces "your network blocks Rematch" BEFORE the user invests in selecting
    conversations, instead of as a cryptic send failure."""
    reachable = True
    try:
        req = urllib.request.Request(INGEST_URL, method="GET")
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT):
            pass
    except urllib.error.HTTPError:
        pass  # Server answered (any status) — reachable.
    except Exception:
        reachable = False
    return jsonify({"version": APP_VERSION, "reachable": reachable})


@app.route("/qr-pair/start", methods=["POST"])
def qr_pair_start():
    """Mint an unbound pairing code from the Rematch backend and render it as a
    QR the phone scans (payload "rematch-pair:CODE"). The code authorizes
    nothing until a signed-in phone links it."""
    if not QR_AVAILABLE:
        return jsonify({"ok": False, "error": "QR unavailable in this build."})
    try:
        req = urllib.request.Request(PAIR_MAC_URL, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        code = data.get("code")
        if not code:
            return jsonify({"ok": False, "error": "Couldn't get a code. Try again."})
        img = qrcode.make(
            f"rematch-pair:{code}",
            image_factory=qrcode.image.svg.SvgPathImage,
        )
        svg = img.to_string().decode("utf-8")
        return jsonify({"ok": True, "code": code, "expiresAt": data.get("expiresAt"), "svg": svg})
    except Exception:
        return jsonify({"ok": False, "error": "Couldn't reach Rematch."})


@app.route("/qr-pair/status")
def qr_pair_status():
    """Poll whether the phone has scanned + linked the displayed QR code."""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"status": "unknown"})
    try:
        with urllib.request.urlopen(
            f"{PAIR_PEEK_URL}?code={urllib.parse.quote(code)}",
            timeout=10,
            context=SSL_CONTEXT,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return jsonify({"status": data.get("status", "unknown")})
    except Exception:
        return jsonify({"status": "network-error"})


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

    # Gzip the body. Vercel serverless caps the request at ~4.5 MB and a real
    # iMessage export easily exceeds that raw; compressing (~5-10x on this very
    # repetitive JSON) keeps us under the wire limit. The ingest endpoint detects
    # the gzip magic bytes and inflates.
    raw = json.dumps({"code": code, "payload": export_data}).encode("utf-8")
    body = gzip.compress(raw)
    req = urllib.request.Request(
        INGEST_URL,
        data=body,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CONTEXT) as resp:
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
    # Cache-bust the URL with a per-launch token. The bare 127.0.0.1:PORT/ may
    # already hold a stale app.html in the browser cache from a PRIOR version of
    # this app (older builds sent no cache headers) — and the browser can replay it
    # without revalidating, which is what left the FDA waiting page spinning. A
    # never-before-seen URL forces a real fetch; no_cache() keeps it fresh after.
    token = os.urandom(4).hex()
    webbrowser.open(f"http://127.0.0.1:{PORT}/?v={token}")

if __name__ == "__main__":
    # Only pause if we actually had to kill a stale instance — a fresh launch
    # (the common case for a just-downloaded app) needn't wait at all. This drops
    # ~3s of dead time off cold start (was: unconditional 2.0s sleep + 1.0s timer).
    if kill_port('127.0.0.1', PORT):
        time.sleep(0.4)  # let the SIGKILL'd socket release before we rebind
    threading.Timer(0.4, launch_browser).start()
    app.run(port=PORT, debug=False)
