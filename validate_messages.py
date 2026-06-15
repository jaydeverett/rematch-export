"""Confidence check for the reconstructed messages.py against the real chat.db.

The original messages.py was lost and rebuilt. The ONE real risk in a rebuild is
attributedBody recovery: on modern macOS the message.text column is often NULL and
the real string lives in the attributedBody typedstream blob. If the decoder were
wrong, recent messages would silently vanish from exports.

This script tests exactly that. Run it on a Mac with Full Disk Access granted to the
terminal (System Settings > Privacy & Security > Full Disk Access > add Terminal):

    python3 validate_messages.py

What to look for:
  - Chat counts/names match what the running RematchExport app shows.
  - "from attributedBody" is a meaningful chunk recovered (not lost).
  - "still empty" is small and is plausibly attachments/reactions (not real text).
  - The sample messages read as real, correct text (not garbage bytes).
"""

import sys
import messages


def main():
    try:
        db = messages.get_db()
        chats = db.chats()
    except Exception as e:
        print(f"Could not open chat.db: {type(e).__name__}: {e}")
        print("Grant Full Disk Access to the terminal and re-run.")
        sys.exit(1)

    ranked = []
    for c in chats:
        msgs = db.messages(chat_id=c.id)
        ranked.append((len(msgs), c, msgs))
    ranked.sort(key=lambda x: x[0], reverse=True)

    print(f"chats: {len(chats)}\n")
    print(f"{'count':>7}  {'kind':>5}  name")
    for n, c, _ in ranked[:8]:
        is_group = len(db.chat(c.id).participants) > 1
        print(f"{n:>7}  {('grp' if is_group else 'dm'):>5}  {c.display_name or c.identifier}")

    total = from_text = from_ab = empty = 0
    for _, c, _ in ranked:
        rows = db.con.execute(
            "SELECT m.text, m.attributedBody FROM message m "
            "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID "
            "WHERE cmj.chat_id = ?",
            (c.id,),
        ).fetchall()
        for text, ab in rows:
            total += 1
            if text:
                from_text += 1
            elif ab is not None and messages._decode_attributed_body(ab):
                from_ab += 1
            else:
                empty += 1

    pct = lambda x: f"{100 * x / max(total, 1):.1f}%"
    print(f"\nrecovery across {total} messages:")
    print(f"  from text column   : {from_text} ({pct(from_text)})")
    print(f"  from attributedBody: {from_ab} ({pct(from_ab)})   <- recovered by the decoder, would be lost without it")
    print(f"  still empty        : {empty} ({pct(empty)})   <- expected: attachments / reactions / tapbacks")

    print("\nsample decoded messages (largest chat) — confirm these read as real text:")
    shown = 0
    for m in ranked[0][2]:
        if m.text:
            who = "me  " if m.is_from_me else "them"
            when = m.date.strftime("%Y-%m-%d") if m.date else "????-??-??"
            print(f"  [{when}] {who}: {m.text[:72]}")
            shown += 1
            if shown >= 6:
                break


if __name__ == "__main__":
    main()
