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
    def __init__(self, date, is_from_me, text, sender=None):
        self.date = date              # datetime | None
        self.is_from_me = bool(is_from_me)
        self.text = text              # str | None
        self.sender = sender          # resolved contact name | raw handle | "You" | None


class LogicalChat:
    """One logical conversation, possibly spanning several chat.db `chat` rows.

    iMessage starts a NEW chat row (new ROWID/guid/chat_identifier) whenever a group
    is renamed OR a participant's handle changes — e.g. a member switches from a
    phone number to an iCloud email. The raw `chat` table therefore fragments a
    years-long group into several rows, each with only a slice of the messages (the
    "291 messages on a chat that's run for years" symptom). We coalesce those rows
    so the user sees and exports the whole conversation.

    `id` is the smallest constituent ROWID — a stable integer that round-trips
    unchanged through the frontend's `parseInt` and re-expands to `member_ids` on
    export, so no client change is needed.
    """

    def __init__(self, member_ids, display_name, identifier, participants, is_group):
        self.member_ids = sorted(member_ids)
        self.id = self.member_ids[0]
        self.display_name = display_name
        self.identifier = identifier
        self.participants = participants
        self.is_group = is_group


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


def _resolve_sender(is_from_me, sender_handle):
    """Per-message speaker label for the export. The user's own messages are "You";
    everyone else resolves to their Contacts name, falling back to the raw handle
    (phone/email) so distinct people stay distinct even when not in Contacts.
    Returns None only when there is no handle at all (rare system rows)."""
    if is_from_me:
        return "You"
    if not sender_handle:
        return None
    return _get_contacts().name(sender_handle) or sender_handle.strip()


def _participant_key(handle):
    """A stable identity for a participant, used to decide whether two chat rows are
    the same logical conversation. Resolve the handle to a contact NAME when possible
    so the same person under a phone number AND an iCloud email collapses to one
    identity (exactly why iMessage spawns a second chat row for a "renamed" group);
    otherwise fall back to a normalized phone / lowercased email / raw handle."""
    name = _get_contacts().name(handle)
    if name:
        return "name:" + name.strip().lower()
    h = (handle or "").strip()
    if not h:
        return "raw:"
    if "@" in h:
        return "email:" + h.lower()
    digits = re.sub(r"\D", "", h)
    if digits:
        return "phone:" + (digits[-10:] if len(digits) >= 10 else digits)
    return "raw:" + h.lower()


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
            "       m.text AS text, m.attributedBody AS attributed_body, "
            "       h.id AS sender_handle "
            "FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "LEFT JOIN handle h ON h.ROWID = m.handle_id "
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
            out.append(Message(
                _apple_time_to_dt(r["date"]), r["is_from_me"], text,
                _resolve_sender(r["is_from_me"], r["sender_handle"]),
            ))
        return out

    def logical_chats(self):
        """All conversations, with fragmented `chat` rows coalesced (see LogicalChat).

        Two chat rows are the same logical conversation when:
          - DM (one participant): they resolve to the same single person.
          - Group: they share the same custom display name (and ≥1 member), OR their
            participant rosters are near-identical (≥3 people, differing by ≤1 — which
            catches one member's phone→email handle switch). A DM never merges with a
            group.
        """
        rows = self.con.execute(
            "SELECT ROWID, display_name, chat_identifier FROM chat"
        ).fetchall()

        meta = {}
        for r in rows:
            rid = r["ROWID"]
            handles = [h["handle"] for h in self.con.execute(
                "SELECT h.id AS handle FROM chat_handle_join chj "
                "JOIN handle h ON h.ROWID = chj.handle_id WHERE chj.chat_id = ?",
                (rid,),
            ).fetchall()]
            keys = frozenset(_participant_key(h) for h in handles)
            meta[rid] = {
                "row": r,
                "handles": handles,
                "keys": keys,
                "name": (r["display_name"] or "").strip().lower() or None,
                "is_group": len(keys) >= 2,
            }

        ids = list(meta.keys())

        # Union-find over chat rows.
        parent = {i: i for i in ids}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        def same_conversation(a, b):
            ma, mb = meta[a], meta[b]
            if ma["is_group"] != mb["is_group"]:
                return False  # never merge a DM with a group
            inter = ma["keys"] & mb["keys"]
            if not ma["is_group"]:
                return ma["keys"] == mb["keys"]  # DM: identical single participant
            if ma["name"] and ma["name"] == mb["name"] and inter:
                return True  # same custom group name, sharing ≥1 member
            if (min(len(ma["keys"]), len(mb["keys"])) >= 3
                    and len(inter) >= max(len(ma["keys"]), len(mb["keys"])) - 1):
                return True  # near-identical roster (handle swap)
            return False

        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                if same_conversation(ids[i], ids[j]):
                    union(ids[i], ids[j])

        comps = {}
        for i in ids:
            comps.setdefault(find(i), []).append(i)

        out = []
        for members in comps.values():
            members = sorted(members)
            display = None
            for m in members:
                dn = meta[m]["row"]["display_name"]
                if dn and dn.strip():
                    display = dn.strip()
                    break
            identifier = meta[members[0]]["row"]["chat_identifier"]
            # Union participant handles, de-duped by resolved identity so one
            # person's two handles don't both appear.
            seen, participants = set(), []
            for m in members:
                for h in meta[m]["handles"]:
                    k = _participant_key(h)
                    if k not in seen:
                        seen.add(k)
                        participants.append(h)
            is_group = any(meta[m]["is_group"] for m in members)
            out.append(LogicalChat(members, display, identifier, participants, is_group))
        return out

    def messages_union(self, member_ids, limit=10_000_000):
        """All messages across the given chat rows, oldest first, with attributedBody
        fallback, de-duped by message ROWID (a message can in principle be joined to
        more than one of the merged rows)."""
        if not member_ids:
            return []
        placeholders = ",".join("?" for _ in member_ids)
        rows = self.con.execute(
            "SELECT m.ROWID AS rid, m.date AS date, m.is_from_me AS is_from_me, "
            "       m.text AS text, m.attributedBody AS attributed_body, "
            "       h.id AS sender_handle "
            "FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "LEFT JOIN handle h ON h.ROWID = m.handle_id "
            f"WHERE cmj.chat_id IN ({placeholders}) "
            "ORDER BY m.date ASC "
            "LIMIT ?",
            (*[int(i) for i in member_ids], limit),
        ).fetchall()
        out, seen = [], set()
        for r in rows:
            if r["rid"] in seen:
                continue
            seen.add(r["rid"])
            text = r["text"]
            if not text and r["attributed_body"] is not None:
                text = _decode_attributed_body(r["attributed_body"])
            out.append(Message(
                _apple_time_to_dt(r["date"]), r["is_from_me"], text,
                _resolve_sender(r["is_from_me"], r["sender_handle"]),
            ))
        return out

    def summary_union(self, member_ids):
        """(count, earliest_dt, latest_dt) for the given chat rows — WITHOUT
        materializing any message.

        The conversation list only needs a count and a date range, but computing
        them by fetching every row (messages_union) meant reading + typedstream-
        decoding the user's entire message history on every load — ~10s of
        spinner on a big chat.db. chat_message_join carries a message_date
        mirror of message.date with a covering (chat_id, message_date,
        message_id) index, so this aggregate never touches the message table.

        Fallbacks, so odd databases degrade to correct-but-slower instead of
        wrong: a chat.db too old to have cmj.message_date aggregates against
        message.date directly; one where message_date is present but zeroed
        (DEFAULT 0) recovers the date range the same way. Zero/NULL dates are
        excluded from the range — messages_union-based summaries would have
        rendered them as a bogus 2001-01-01."""
        if not member_ids:
            return 0, None, None
        ids = [int(i) for i in member_ids]
        placeholders = ",".join("?" for _ in ids)
        join_sql = (
            "SELECT COUNT(DISTINCT m.ROWID) AS n, "
            "       MIN(CASE WHEN m.date > 0 THEN m.date END) AS lo, "
            "       MAX(CASE WHEN m.date > 0 THEN m.date END) AS hi "
            "FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            f"WHERE cmj.chat_id IN ({placeholders})"
        )
        try:
            r = self.con.execute(
                "SELECT COUNT(DISTINCT message_id) AS n, "
                "       MIN(CASE WHEN message_date > 0 THEN message_date END) AS lo, "
                "       MAX(CASE WHEN message_date > 0 THEN message_date END) AS hi "
                f"FROM chat_message_join WHERE chat_id IN ({placeholders})",
                ids,
            ).fetchone()
            if r["n"] and r["lo"] is None:  # dates zeroed — recover from message table
                slow = self.con.execute(join_sql, ids).fetchone()
                r = {"n": r["n"], "lo": slow["lo"], "hi": slow["hi"]}
        except sqlite3.OperationalError:  # pre-message_date chat.db
            r = self.con.execute(join_sql, ids).fetchone()
        return (r["n"] or 0,
                _apple_time_to_dt(r["lo"]),
                _apple_time_to_dt(r["hi"]))

    def logical_name(self, lc):
        """Friendly label for a logical conversation: its given name, else the
        resolved contact name(s), else the raw identifier."""
        if lc.display_name:
            return lc.display_name
        contacts = _get_contacts()
        direct = contacts.name(lc.identifier)
        if direct:
            return direct
        if lc.participants:
            return ", ".join(contacts.name(p) or p for p in lc.participants)
        return lc.identifier


def get_db():
    return DB()
