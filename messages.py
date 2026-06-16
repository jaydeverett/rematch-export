"""Minimal read-only wrapper around the macOS Messages database (~/Library/Messages/chat.db).

Reconstructed module (the original was lost: never committed, only embedded as
bytecode in a shipped build). It exposes exactly the surface messages_desktop_app.py
relies on:

    db = get_db()
    for c in db.chats():            # -> Chat(.id, .display_name, .identifier)
        for m in db.messages(chat_id=c.id, limit=N):   # -> Message(.date, .is_from_me, .text)
            ...
    full = db.chat(chat_id)         # -> Chat(... .participants)

Notes on fidelity:
- Modern macOS leaves message.text NULL and stores the real string in the
  attributedBody typedstream blob. We decode it with the well-established
  split(b"NSString") + length-prefix algorithm (same one imessage_reader uses),
  so recent messages are not silently dropped.
- message.date is Apple "Mac absolute time": nanoseconds since 2001-01-01 on
  modern DBs (seconds on very old ones). Converted to a naive local datetime.
"""

import os
import re
import glob
import sqlite3
from datetime import datetime, timedelta

DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")

# Mac absolute time epoch.
_APPLE_EPOCH = datetime(2001, 1, 1)

# macOS Contacts (AddressBook) — one sqlite db per account source. DM chats are
# keyed by raw handle (phone/email); the person's name lives here.
_ABOOK_GLOB = os.path.expanduser(
    "~/Library/Application Support/AddressBook/**/AddressBook-v22.abcddb"
)


# -------------------------
# Value objects
# -------------------------

class Chat:
    def __init__(self, rowid, display_name, identifier, participants=None):
        self.id = rowid
        self.display_name = display_name
        self.identifier = identifier
        self.participants = participants if participants is not None else []


class Message:
    def __init__(self, date, is_from_me, text):
        self.date = date              # datetime | None
        self.is_from_me = bool(is_from_me)
        self.text = text              # str | None


# -------------------------
# Helpers
# -------------------------

def _apple_time_to_dt(raw):
    """Convert a chat.db `message.date` value to a datetime (or None)."""
    if raw is None:
        return None
    try:
        # Modern DBs store nanoseconds (~6e17); legacy store seconds (~4e8).
        seconds = raw / 1_000_000_000 if raw > 1_000_000_000_000 else raw
        return _APPLE_EPOCH + timedelta(seconds=seconds)
    except Exception:
        return None


def _decode_attributed_body(blob):
    """Extract the plain message text from an attributedBody typedstream blob.

    Mirrors the proven imessage_reader algorithm: the user-visible string follows
    the b"NSString" class marker, after a short preamble (typically
    b'\\x01\\x94\\x84\\x01+'), and is length-prefixed (0x81 => 2-byte little-endian
    length, otherwise a single length byte). Returns None on any malformed blob
    rather than raising, so one bad row never breaks an export.
    """
    if not blob:
        return None
    try:
        parts = bytes(blob).split(b"NSString", 1)
        if len(parts) < 2:
            return None
        data = parts[1][5:]  # strip the preamble after the class marker
        if not data:
            return None
        if data[0] == 0x81:
            length = int.from_bytes(data[1:3], "little")
            content = data[3:3 + length]
        else:
            length = data[0]
            content = data[1:1 + length]
        text = content.decode("utf-8", errors="replace")
        return text or None
    except Exception:
        return None


# -------------------------
# Contacts (AddressBook) — resolve a phone/email handle to a person's name
# -------------------------

def _norm_phone(raw):
    """Reduce a phone string to comparable digits (last 10, to ignore +country)."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    return digits[-10:] if len(digits) >= 10 else digits


class Contacts:
    """Read-only handle -> name index built from the macOS Contacts DBs.

    Requires Full Disk Access (already needed for chat.db). Any locked or
    schema-variant source is skipped rather than allowed to break an export.
    """

    def __init__(self):
        self._by_phone = {}
        self._by_email = {}
        for path in glob.glob(_ABOOK_GLOB, recursive=True):
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                try:
                    self._index(con)
                finally:
                    con.close()
            except Exception:
                continue

    @staticmethod
    def _name(row):
        full = ((row["ZFIRSTNAME"] or "").strip() + " "
                + (row["ZLASTNAME"] or "").strip()).strip()
        return (full
                or (row["ZORGANIZATION"] or "").strip()
                or (row["ZNICKNAME"] or "").strip()
                or None)

    def _index(self, con):
        for row in con.execute(
            "SELECT p.ZFULLNUMBER AS v, r.ZFIRSTNAME, r.ZLASTNAME, "
            "       r.ZORGANIZATION, r.ZNICKNAME "
            "FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON r.Z_PK = p.ZOWNER"
        ):
            key, name = _norm_phone(row["v"]), self._name(row)
            if key and name:
                self._by_phone.setdefault(key, name)
        for row in con.execute(
            "SELECT e.ZADDRESS AS v, r.ZFIRSTNAME, r.ZLASTNAME, "
            "       r.ZORGANIZATION, r.ZNICKNAME "
            "FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER"
        ):
            addr, name = (row["v"] or "").strip().lower(), self._name(row)
            if addr and name:
                self._by_email.setdefault(addr, name)

    def name(self, handle):
        if not handle:
            return None
        h = handle.strip()
        if "@" in h:
            return self._by_email.get(h.lower())
        key = _norm_phone(h)
        return self._by_phone.get(key) if key else None


_CONTACTS = None


def _get_contacts():
    """Load the Contacts index once per process."""
    global _CONTACTS
    if _CONTACTS is None:
        _CONTACTS = Contacts()
    return _CONTACTS


# -------------------------
# Database
# -------------------------

class DB:
    def __init__(self, path=DB_PATH):
        # Read-only; never mutate the user's Messages database.
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row

    def chats(self):
        """All chats (lightweight: id + names, no participants)."""
        rows = self.con.execute(
            "SELECT ROWID, display_name, chat_identifier FROM chat"
        ).fetchall()
        return [Chat(r["ROWID"], r["display_name"], r["chat_identifier"]) for r in rows]

    def chat(self, chat_id):
        """A single chat with its participant handles resolved."""
        r = self.con.execute(
            "SELECT ROWID, display_name, chat_identifier FROM chat WHERE ROWID = ?",
            (chat_id,),
        ).fetchone()
        if r is None:
            return Chat(chat_id, None, None, [])
        handles = self.con.execute(
            "SELECT h.id AS handle "
            "FROM chat_handle_join chj "
            "JOIN handle h ON h.ROWID = chj.handle_id "
            "WHERE chj.chat_id = ?",
            (chat_id,),
        ).fetchall()
        participants = [h["handle"] for h in handles]
        return Chat(r["ROWID"], r["display_name"], r["chat_identifier"], participants)

    def name_for(self, chat):
        """Friendly label: the chat's given name, else the resolved contact
        name(s), else the raw handle/identifier."""
        if chat.display_name:
            return chat.display_name
        contacts = _get_contacts()
        direct = contacts.name(chat.identifier)
        if direct:
            return direct
        if chat.participants:
            return ", ".join(contacts.name(p) or p for p in chat.participants)
        return chat.identifier

    def messages(self, chat_id, limit=10_000_000):
        """All messages in a chat, oldest first, with attributedBody fallback."""
        rows = self.con.execute(
            "SELECT m.date AS date, m.is_from_me AS is_from_me, "
            "       m.text AS text, m.attributedBody AS attributed_body "
            "FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "WHERE cmj.chat_id = ? "
            "ORDER BY m.date ASC "
            "LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            text = r["text"]
            if not text and r["attributed_body"] is not None:
                text = _decode_attributed_body(r["attributed_body"])
            out.append(Message(_apple_time_to_dt(r["date"]), r["is_from_me"], text))
        return out


def get_db():
    return DB()
