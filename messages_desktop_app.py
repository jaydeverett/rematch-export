'''
Build the signed, notarizable .app from RematchExport.spec (NOT a one-liner):

    ./build.sh            # clean -> PyInstaller (onedir .app, custom icon) -> codesign -> DMG

The committed spec uses --onedir (a proper .app bundle), which launches far faster
on repeat opens than --onefile (which unpacks to a temp dir every launch). Notarize
+ staple steps print at the end of build.sh. See build.sh / RematchExport.spec.

Process model (v1.8.2): the icon-click process IS the app — Flask on a daemon
thread + an AppKit run loop on the main thread, so the Dock icon persists while
it runs, Dock clicks reopen the page, and Quit quits. Falls back to the v1.8.1
launcher/detached-server split if AppKit is missing from a build (see __main__).
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
APP_VERSION = "1.10.0"

# Rematch backend — the pairing ingest endpoint the phone authorizes with a code.
INGEST_URL = "https://rematch-app-orpin.vercel.app/api/imessage/ingest"
# Signed-URL minting for the storage upload path (v1.8.0): the payload goes
# straight to Supabase Storage, one gzipped object per conversation, and only
# a tiny manifest rides the ingest commit. Vercel caps request bodies at
# ~4.5 MB and returns a bare 413 BEFORE our function — a full-corpus send hit
# that cap even gzipped (the "code isn't valid" phantom, twice). Storage PUTs
# have no such cap, and per-conversation objects give retries + progress.
UPLOAD_URLS_URL = "https://rematch-app-orpin.vercel.app/api/imessage/upload-urls"
# QR pairing (reverse direction): this Mac mints an unbound code, shows it as a
# QR, the phone scans + links it, and the send then uses that code via /ingest.
PAIR_MAC_URL = "https://rematch-app-orpin.vercel.app/api/imessage/pair/mac"
PAIR_PEEK_URL = "https://rematch-app-orpin.vercel.app/api/imessage/pair/peek"

# PyObjC gives the app a real AppKit run loop so it lives in the Dock like a
# normal Mac app (icon persists, Dock clicks reopen the page, Quit quits). If a
# build ever misses it, we fall back to the v1.8.1 launcher/detached-server
# model — everything still works, the icon just doesn't stay in the Dock.
try:
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyRegular,
        NSMenu,
        NSMenuItem,
        NSObject,
    )
    APPKIT_AVAILABLE = True
except Exception:
    APPKIT_AVAILABLE = False

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
        # Aggregate-only (no message rows materialized) — fetching every message
        # here made the conversation list take ~10s to populate on a big chat.db.
        count, earliest, latest = db.summary_union(lc.member_ids)

        if not count or earliest is None:
            continue

        summaries.append({
            "id": lc.id,
            "name": db.logical_name(lc),
            "count": count,
            "earliest": earliest.strftime("%Y-%m-%d"),
            "latest": latest.strftime("%Y-%m-%d"),
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


def build_conversation_exports(selected_ids):
    """Build the {chat, date, from_me, text} rows for each selected chat,
    grouped per conversation: [{"name": …, "rows": […]}, …]. The send-to-phone
    path uploads one storage object per conversation; the file download
    flattens the groups back into the classic single array."""
    db = messages.get_db()
    conversations = []

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

        rows = []
        for msg in db.messages_union(member_ids, limit=VERY_LARGE_LIMIT):
            rows.append({
                "chat": chat_name,
                "date": msg.date.strftime("%Y-%m-%d") if msg.date else None,
                "from_me": msg.is_from_me,
                "sender": msg.sender,
                "text": msg.text
            })

        if rows:
            conversations.append({"name": chat_name, "rows": rows})

    return conversations


def build_export_data(selected_ids):
    """The flat single-array export (file download path)."""
    export_data = []
    for conv in build_conversation_exports(selected_ids):
        export_data.extend(conv["rows"])
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


@app.route("/api/ping")
def api_ping():
    """Local identity probe for the LAUNCHER (see __main__): answers instantly
    with no outbound network call — /api/health can block up to 10s probing the
    Rematch backend, which would stall every icon click on an offline Mac. An
    older server (≤1.8.0) 404s here and gets replaced; a foreign process on the
    port fails the app-name check."""
    return jsonify({"app": APP_NAME, "version": APP_VERSION})


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
    QR the phone scans (payload "rematch://pair?code=CODE&s=LINKSECRET" — a real
    deep link, so the SYSTEM camera opens the Rematch app directly; the in-app
    scanner accepts it too, plus the legacy forms). The code authorizes nothing
    until a signed-in phone links it.

    v1.9.0 channel binding: the backend mints two one-time secrets with the
    code. linkSecret rides INSIDE the QR — the phone must echo it to bind, so a
    shoulder-surfed six-character code can't be hijacked. depositSecret never
    leaves this Mac except on the send itself: /upload-urls and /ingest require
    it, so a code alone can't authorize writing conversations."""
    if not QR_AVAILABLE:
        return jsonify({"ok": False, "error": "QR unavailable in this build."})
    try:
        req = urllib.request.Request(PAIR_MAC_URL, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        code = data.get("code")
        if not code:
            return jsonify({"ok": False, "error": "Couldn't get a code. Try again."})
        link_secret = data.get("linkSecret")
        payload = f"rematch://pair?code={code}"
        if link_secret:
            payload += f"&s={link_secret}"
        img = qrcode.make(payload, image_factory=qrcode.image.svg.SvgPathImage)
        svg = img.to_string().decode("utf-8")
        return jsonify({
            "ok": True,
            "code": code,
            "expiresAt": data.get("expiresAt"),
            "svg": svg,
            "depositSecret": data.get("depositSecret"),
        })
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


def _post_json(url, obj, timeout=60):
    """POST a JSON body and return the parsed JSON response (raises HTTPError)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_error_message(e):
    """A server-provided error message, or an HONEST status-code fallback.

    Never guess a cause the response didn't state: a platform 413 mislabeled as
    "that code isn't valid" burned two full debugging sessions (pt-8, pt-15)."""
    try:
        err = json.loads(e.read().decode("utf-8")).get("error")
    except Exception:
        err = None
    return err or (
        f"Send failed (HTTP {e.code}). Try again — and if it keeps happening, "
        "download the newest Mac app from letsrematch.vercel.app/mac."
    )


# Live progress for the current send, polled by the UI (/send-progress) so a
# long multi-conversation upload shows movement instead of a frozen button.
SEND_PROGRESS = {"active": False, "done": 0, "total": 0}


@app.route("/send-progress")
def send_progress():
    return jsonify(SEND_PROGRESS)


@app.route("/send-to-phone", methods=["POST"])
def send_to_phone():
    """Send the selected conversations straight to the user's Rematch account via
    the 6-char pairing code shown in the phone app — no file handling.

    v1.8.0 storage path: (1) ask Rematch for one signed upload URL per selected
    conversation (the code authorizes this), (2) PUT each conversation's gzipped
    rows straight to storage — no size ceiling, per-conversation retry —
    (3) commit a tiny manifest to the ingest endpoint. The phone then claims the
    conversation index and downloads only what the user confirms."""
    data = request.get_json()
    code = (data.get("code") or "").strip().upper()
    # v1.9.0: present on QR-paired sends; the backend requires it for
    # QR-minted codes and ignores it for typed (phone-minted) ones.
    deposit_secret = data.get("deposit_secret") or None
    if not code:
        return jsonify({"ok": False, "error": "Enter the code from your Rematch app."}), 400

    conversations = build_conversation_exports(data.get("chat_ids", []))
    if not conversations:
        return jsonify({"ok": False, "error": "No messages found in the selected conversations."}), 400

    SEND_PROGRESS.update(active=True, done=0, total=len(conversations))
    try:
        # 1. Signed upload URLs, one per conversation.
        upload_req = {
            "code": code,
            "conversations": [{"name": c["name"], "count": len(c["rows"])} for c in conversations],
        }
        if deposit_secret:
            upload_req["depositSecret"] = deposit_secret
        minted = _post_json(UPLOAD_URLS_URL, upload_req)
        uploads = minted.get("uploads") or []
        if len(uploads) != len(conversations):
            return jsonify({"ok": False, "error": "Rematch didn't accept the upload. Try again."})

        # 2. PUT each conversation's gzipped rows to storage (one retry each).
        manifest = []
        for conv, upload in zip(conversations, uploads):
            blob = gzip.compress(json.dumps(conv["rows"]).encode("utf-8"))
            for attempt in (1, 2):
                try:
                    put = urllib.request.Request(
                        upload["url"],
                        data=blob,
                        headers={"Content-Type": "application/gzip", "x-upsert": "true"},
                        method="PUT",
                    )
                    with urllib.request.urlopen(put, timeout=180, context=SSL_CONTEXT):
                        pass
                    break
                except Exception:
                    if attempt == 2:
                        raise
            dates = [r["date"] for r in conv["rows"] if r["date"]]
            manifest.append({
                "path": upload["path"],
                "chat": conv["name"],
                "count": len(conv["rows"]),
                "earliest": min(dates) if dates else None,
                "latest": max(dates) if dates else None,
            })
            SEND_PROGRESS["done"] += 1

        # 3. Commit the manifest — flips the pairing slot to "received".
        ingest_req = {"code": code, "manifest": manifest}
        if deposit_secret:
            ingest_req["depositSecret"] = deposit_secret
        result = _post_json(INGEST_URL, ingest_req)
        return jsonify({"ok": True, "conversationCount": result.get("conversationCount")})
    except urllib.error.HTTPError as e:
        return jsonify({"ok": False, "error": _http_error_message(e)})
    except Exception:
        return jsonify({"ok": False, "error": "Couldn't reach Rematch. Check your connection and try again."})
    finally:
        SEND_PROGRESS["active"] = False


# -------------------------
# App Lifecycle
# -------------------------
#
# DOCK-APP MODE (v1.8.2, the normal path): the clicked process hosts BOTH the
# Flask server (daemon thread) and a real AppKit run loop (main thread). The run
# loop is what makes it a first-class Mac app: the Dock icon appears and STAYS
# while the app runs, clicking it fires applicationShouldHandleReopen (we open
# the page again), and Quit actually quits — the daemon server thread dies with
# the process. History here, so nobody regresses it:
#   - pre-1.8.1 ran Flask directly with NO run loop: the process never checked
#     in with the Dock (icon bounced until macOS gave up) and reopen events went
#     unhandled — the app was one-shot per boot.
#   - v1.8.1 fixed reopen with a launcher/detached-server split, but the
#     launcher exiting meant the Dock icon vanished seconds after every click.
#
# FALLBACK (no AppKit in the build): exactly the v1.8.1 split — a short-lived
# launcher reuses/spawns a detached --serve child and exits. Works, minus the
# persistent icon.

def kill_port(port: int) -> bool:
    """Kill any process LISTENING on the given port. -sTCP:LISTEN matters: a bare
    `lsof -ti :port` also matches browsers holding ESTABLISHED connections to the
    old server, and SIGKILLing the user's browser is not a great relaunch UX."""
    killed = False
    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
        capture_output=True, text=True
    )
    for pid in result.stdout.strip().splitlines():
        if pid:
            os.kill(int(pid), signal.SIGKILL)
            killed = True
    return killed


def server_version():
    """Version of the RematchExport server already on PORT, else None."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/api/ping", timeout=2
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("version") if data.get("app") == APP_NAME else None
    except Exception:
        return None


def spawn_server():
    """Start the Flask server as a DETACHED child (its own session, no inherited
    stdio) so it survives this launcher process exiting. The child is this same
    binary re-run with --serve."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--serve"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--serve"]
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch_browser():
    # Cache-bust the URL with a per-launch token. The bare 127.0.0.1:PORT/ may
    # already hold a stale app.html in the browser cache from a PRIOR version of
    # this app (older builds sent no cache headers) — and the browser can replay it
    # without revalidating, which is what left the FDA waiting page spinning. A
    # never-before-seen URL forces a real fetch; no_cache() keeps it fresh after.
    token = os.urandom(4).hex()
    webbrowser.open(f"http://127.0.0.1:{PORT}/?v={token}")


_LAST_BROWSER_OPEN = 0.0


def open_browser_when_ready():
    """Open the page once OUR server answers /api/ping (or after 15s regardless,
    so a startup hiccup surfaces as a visible error page instead of silence).

    Debounced: LaunchServices can deliver a small burst of launch/reopen events
    for ONE user gesture (observed: an icon click yielding two reopen fires) —
    one gesture should mean one tab."""
    global _LAST_BROWSER_OPEN
    deadline = time.time() + 15
    while time.time() < deadline and server_version() != APP_VERSION:
        time.sleep(0.25)
    if time.time() - _LAST_BROWSER_OPEN < 2:
        return
    _LAST_BROWSER_OPEN = time.time()
    launch_browser()


if APPKIT_AVAILABLE:
    class _DockAppDelegate(NSObject):
        def applicationDidFinishLaunching_(self, note):
            # Never block the run loop — Dock/menu stay responsive while we wait.
            threading.Thread(target=open_browser_when_ready, daemon=True).start()

        def applicationShouldHandleReopen_hasVisibleWindows_(self, sender, has_windows):
            # Dock icon clicked while running: open the page again (instant —
            # the server is already up, so the ping poll returns immediately).
            threading.Thread(target=open_browser_when_ready, daemon=True).start()
            return False

        def applicationSupportsSecureRestorableState_(self, sender):
            return True  # no windows to restore; silences the macOS 14 warning


_DELEGATE = None  # strong ref — NSApplication holds its delegate weakly


def run_dock_app():
    """Block on the AppKit run loop until the user quits (Dock ▸ Quit / Cmd-Q)."""
    global _DELEGATE
    nsapp = NSApplication.sharedApplication()
    # Regular = Dock icon + reopen events. The bundled app is Regular anyway;
    # this makes dev runs from a terminal behave the same.
    nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    _DELEGATE = _DockAppDelegate.alloc().init()
    nsapp.setDelegate_(_DELEGATE)
    # Minimal menu so Cmd-Q works when the app is frontmost.
    menubar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    menubar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"Quit {APP_NAME}", "terminate:", "q"))
    app_item.setSubmenu_(app_menu)
    nsapp.setMainMenu_(menubar)
    nsapp.run()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        # Detached server child (fallback mode only): just run Flask.
        app.run(port=PORT, debug=False)
        sys.exit(0)

    if APPKIT_AVAILABLE:
        # DOCK-APP MODE: take over the port unconditionally — anything on it is
        # a leftover (pre-split one-shot, a v1.8.1 detached server, or foreign);
        # this process is the server for as long as its icon is in the Dock.
        if kill_port(PORT):
            time.sleep(0.4)  # let the SIGKILL'd socket release before we rebind
        threading.Thread(
            target=lambda: app.run(port=PORT, debug=False), daemon=True
        ).start()
        run_dock_app()
    else:
        # FALLBACK LAUNCHER (v1.8.1 model): reuse a matching live server;
        # replace a stale/foreign one; exit after opening the browser.
        if server_version() != APP_VERSION:
            if kill_port(PORT):
                time.sleep(0.4)
            spawn_server()
        open_browser_when_ready()
