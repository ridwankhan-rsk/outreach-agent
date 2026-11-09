"""
replies.py — watch the Gmail inbox for answers to our emails.

Each poll (every ~60 s) does this:
  1. Ask Gmail for messages from other people received in the last few days.
  2. Skip any we've already processed (the `replies` table remembers every message id).
  3. For each new one:
       - bounce notice?          -> mark lead 'bounced', add to suppression list
       - not in one of our threads -> record as 'unrelated' and ignore
       - auto-reply / out of office -> ignore
       - otherwise ask Claude to classify it:
            needs_response -> PRIORITY alert + a suggested reply drafted in your voice
            decline        -> quiet notice, follow-ups cancelled
            stop_request   -> sender added to suppression list, follow-ups cancelled
"""
from __future__ import annotations

import json
import logging
import re

import config
import db
import gmail_client
import guard
import notify
import voice

log = logging.getLogger("outreach.replies")

_BOUNCE_SENDER = re.compile(r"mailer-daemon|postmaster", re.I)
_AUTO_SUBJECT = re.compile(r"^(automatic reply|auto[- ]?reply|out of office|undeliverable)", re.I)


def _record(conn, msg: dict, lead_id, classification: str, suggested: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO replies(lead_id,gmail_message_id,thread_id,sender,snippet,body,"
        "classification,suggested_reply,received_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (lead_id, msg["id"], msg["threadId"], msg["sender"], msg["snippet"], msg["body"],
         classification, suggested, msg["internal_ts"] or db.now()),
    )


def _handle_bounce(conn, msg: dict) -> None:
    """A bounce names the failed address somewhere in its text; find which lead it was."""
    recent = conn.execute(
        "SELECT id,email FROM leads WHERE status='sent'"
    ).fetchall()
    text = (msg["body"] + " " + msg["snippet"]).lower()
    for lead in recent:
        if lead["email"] in text:
            db.set_lead_status(conn, lead["id"], "bounced", "delivery failed")
            db.suppress(conn, lead["email"], "bounced")
            log.info("bounce for %s", lead["email"])
            _record(conn, msg, lead["id"], "bounce")
            guard.bounce_check(conn)             # may auto-pause everything
            return
    _record(conn, msg, None, "bounce")


def poll_once() -> int:
    """Check for new replies. Returns how many new messages were processed."""
    inbound = gmail_client.recent_inbound()
    processed = 0
    with db.connect() as conn:
        known = {r["gmail_message_id"] for r in conn.execute("SELECT gmail_message_id FROM replies")}
        cutoff = db.now() - config.REPLY_WATCH_DAYS * 86400
        # thread id -> lead id, for every thread we started recently
        threads = {
            r["thread_id"]: r["lead_id"]
            for r in conn.execute("SELECT thread_id,lead_id FROM sends WHERE sent_at>?", (cutoff,))
        }
        for item in inbound:
            if item["id"] in known:
                continue
            msg = gmail_client.fetch_message(item["id"])
            processed += 1

            if _BOUNCE_SENDER.search(msg["sender"]):
                _handle_bounce(conn, msg)
                continue

            lead_id = threads.get(msg["threadId"])
            if lead_id is None:
                _record(conn, msg, None, "unrelated")     # remember it so we skip it next time
                continue

            if msg["auto"] or _AUTO_SUBJECT.search(msg["subject"]):
                _record(conn, msg, lead_id, "auto_reply")
                continue

            lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            verdict = voice.classify_reply(msg["body"])
            category = verdict["category"]
            name = lead["name"] or lead["email"]
            log.info("reply from %s classified %s (%s)", lead["email"], category, verdict["reason"])

            if category == "needs_response":
                db.set_lead_status(conn, lead_id, "replied", verdict["reason"])
                suggested = _suggest(conn, lead, msg)
                _record(conn, msg, lead_id, category, suggested)
                conn.commit()                              # save before the (slow) alert
                text = msg["body"][:500] or msg["snippet"]
                if suggested and re.search(r"\battach", suggested, re.I):
                    text = "Attach your resume before replying. " + text    # I can't attach it for you
                notify.priority_alert(
                    f"Reply from {name}" + (f" ({lead['org']})" if lead["org"] else ""),
                    text,
                    thread_id=msg["threadId"],
                    suggested_reply=suggested or None,
                )
            elif category == "stop_request":
                db.set_lead_status(conn, lead_id, "suppressed", "asked to stop")
                db.suppress(conn, lead["email"], "asked to stop")
                _record(conn, msg, lead_id, category)
                notify.quiet_notice("Removed from outreach", f"{name} asked not to be contacted.")
            elif category == "decline":
                db.set_lead_status(conn, lead_id, "declined", verdict["reason"])
                _record(conn, msg, lead_id, category)
                notify.quiet_notice(f"Declined: {name}", msg["body"][:150])
            else:                                          # auto_reply
                _record(conn, msg, lead_id, "auto_reply")
    return processed


def _suggest(conn, lead, msg: dict) -> str:
    """Draft a reply for you. Failure here must never block the alert."""
    try:
        campaign = guard.load_campaigns(only_enabled=False).get(lead["campaign"], {})
        original = conn.execute(
            "SELECT body FROM sends WHERE lead_id=? ORDER BY sent_at LIMIT 1", (lead["id"],)
        ).fetchone()
        return voice.suggest_reply(
            campaign, json.loads(lead["data_json"]), original["body"] if original else "", msg["body"]
        )
    except Exception as exc:
        log.warning("could not draft suggested reply: %s", exc)
        return ""
