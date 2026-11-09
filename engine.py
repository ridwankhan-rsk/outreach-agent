"""
engine.py — decides what to send next, and sends it.

`send_cycle()` is called every couple of minutes by the daemon. Each call sends AT MOST
ONE email. It does nothing unless every safety rule in guard.py passes, and after a send it
sets a random "next allowed send" time so emails are spread out like a person's would be.

Order of work each cycle:
  1. paused? bounce problems?              -> stop
  2. is the next-send timer still running? -> wait
  3. any follow-up due?                    -> send it (follow-ups come first)
  4. else the oldest queued new lead       -> write, check, send
"""
from __future__ import annotations

import json
import logging
import random

from googleapiclient.errors import HttpError

import config
import db
import gmail_client
import guard
import notify
import resume
import voice

log = logging.getLogger("outreach.engine")

# "later:" reasons that apply to EVERY lead (so there's no point trying the next lead).
_GLOBAL_WAIT = {
    "later: paused", "later: outside send hours",
    "later: daily cap reached", "later: weekly cap reached",
}
MAX_DRAFTS_PER_CYCLE = 3   # don't burn API calls if several leads in a row get blocked


def draft_with_retry(campaign: dict, data: dict, attempts: int = 2):
    """
    Write an email and run all checks. If it fails, give the model the reasons and let it
    rewrite once. Returns (draft, ok, reason). This saves most leads that would otherwise be
    blocked over one fixable slip (e.g. a doubled sign-off).
    """
    signature = (voice.load_profile().get("email_signature") or "").strip()
    feedback, draft, why = "", None, ""
    for _ in range(attempts):
        draft = voice.generate_email(campaign, data, feedback=feedback)
        ok, why = guard.draft_checks(campaign, data, draft)
        if ok and signature and not draft["body"].rstrip().endswith(signature):
            # The model sometimes adds a blank line or rewords the sign-off; it must be exact.
            ok, why = False, ("the email must end with the sender's sign-off exactly as given in "
                              "email_signature, with no extra blank line or changed wording")
        if ok:
            return draft, True, ""
        feedback = why
    return draft, False, why


def _next_followup(conn, campaigns):
    """Find a lead whose initial email got no reply within `followup_after_days`."""
    for name, c in campaigns.items():
        if c.get("followups", 0) < 1:
            continue
        cutoff = db.now() - c.get("followup_after_days", 14) * 86400
        row = conn.execute(
            """SELECT l.* , s.thread_id AS t_id, s.message_id AS m_id, s.subject AS s_subject,
                      s.body AS s_body
               FROM leads l JOIN sends s ON s.lead_id=l.id AND s.kind='initial'
               WHERE l.campaign=? AND l.status='sent' AND s.sent_at<?
                 AND NOT EXISTS (SELECT 1 FROM sends f WHERE f.lead_id=l.id AND f.kind='followup')
               ORDER BY s.sent_at LIMIT 1""",
            (name, cutoff),
        ).fetchone()
        if row:
            return row
    return None


def _record_send(conn, lead_id, kind, sent, subject, body) -> None:
    conn.execute(
        "INSERT INTO sends(lead_id,kind,thread_id,message_id,subject,body,sent_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (lead_id, kind, sent["threadId"], sent["id"], subject, body, db.now()),
    )
    if db.get_state(conn, "first_send_at") is None:
        db.set_state(conn, "first_send_at", str(db.now()))
    gap = random.randint(config.MIN_GAP_MINUTES, config.MAX_GAP_MINUTES) * 60
    db.set_state(conn, "next_send_at", str(db.now() + gap))


def _send(conn, campaign, lead, draft, kind, dry_run, thread_id=None, reply_to_msg=None) -> bool:
    """Actually send (or print, in dry-run). Returns True if the email went out."""
    profile = voice.load_profile()
    # The ONLY thing we ever attach is the current resume, and only on a campaign's first
    # email. If it is supposed to be attached but the file is gone, do not send: the email text
    # may say "attached".
    attachments = None
    if resume.attaches(campaign, kind):
        path = resume.current_path()
        if path is None:
            log.error("resume missing at send time; not sending to %s", lead["email"])
            return False
        attachments = [(str(path), resume.attachment_name(profile["name"]))]
    if dry_run:
        print(f"\n--- DRY RUN [{kind}] to {lead['email']} ({campaign['name']}) ---")
        print(f"Subject: {draft['subject']}\n\n{draft['body']}")
        print(f"[attachment: {attachments[0][1] if attachments else 'none'}]\n")
        return True
    try:
        in_reply_to = gmail_client.rfc_message_id(reply_to_msg) if reply_to_msg else None
        sent = gmail_client.send_email(
            to=lead["email"], subject=draft["subject"], body=draft["body"],
            attachments=attachments, thread_id=thread_id,
            in_reply_to=in_reply_to, sender_name=profile["name"],
        )
    except HttpError as exc:
        # Any Gmail error (rate limit, account flagged, auth) => stop everything and tell the user.
        reason = f"auto-paused: Gmail error {exc.status_code}"
        guard.pause(reason)
        notify.priority_alert("Outreach paused", f"{reason}. Run `python cli.py status`.")
        log.error("Gmail send failed: %s", exc)
        return False
    _record_send(conn, lead["id"], kind, sent, draft["subject"], draft["body"])
    db.set_lead_status(conn, lead["id"], "sent", "")   # stays 'sent' until a reply/bounce changes it
    log.info("sent %s to %s (%s)", kind, lead["email"], campaign["name"])
    return True


def send_cycle(dry_run: bool = False) -> str:
    """Try to send one email. Returns a short status string (for logs / the CLI)."""
    campaigns = guard.load_campaigns()
    with db.connect() as conn:
        if (why := guard.is_paused()):
            return f"paused ({why})"
        if (why := guard.bounce_check(conn)):
            notify.priority_alert("Outreach paused", why)
            return why
        if not dry_run and db.now() < int(db.get_state(conn, "next_send_at", "0")):
            return "waiting for next send slot"

        # ---- 1. follow-ups come first ------------------------------------- #
        fu = _next_followup(conn, campaigns)
        if fu:
            campaign = campaigns[fu["campaign"]]
            ok, why = guard.pre_send_checks(conn, campaign, fu, kind="followup")
            if ok:
                data = json.loads(fu["data_json"])
                draft = voice.generate_followup(
                    campaign, data, {"subject": fu["s_subject"], "body": fu["s_body"]}
                )
                ok, why = guard.draft_checks(campaign, data, draft, kind="followup")
                if ok:
                    sent = _send(conn, campaign, fu, draft, "followup", dry_run,
                                 thread_id=fu["t_id"], reply_to_msg=fu["m_id"])
                    return "sent follow-up" if sent else "send failed"
                # Park it so we don't re-draft (and re-pay for) this follow-up every 2 minutes.
                db.set_lead_status(conn, fu["id"], "followup_blocked", why)
                log.warning("follow-up for %s blocked: %s", fu["email"], why)
            elif why in _GLOBAL_WAIT:
                return why

        # ---- 2. otherwise: the next queued lead --------------------------- #
        names = list(campaigns)
        if not names:
            return "no enabled campaigns"
        q = ",".join("?" * len(names))
        # eligible_at is the grace window for auto-found leads (time to delete one in the portal)
        leads = conn.execute(
            f"SELECT * FROM leads WHERE status='new' AND eligible_at<=? AND campaign IN ({q}) "
            "ORDER BY added_at",
            [db.now(), *names],
        ).fetchall()
        drafted = 0
        waiting_for_resume = ""
        for lead in leads:
            campaign = campaigns[lead["campaign"]]
            ok, why = guard.pre_send_checks(conn, campaign, lead)
            if not ok:
                if why in _GLOBAL_WAIT:
                    return why                       # nothing can be sent right now
                if why.startswith("later: no resume"):
                    waiting_for_resume = why         # other campaigns may still be sendable
                if not why.startswith("later:"):
                    db.set_lead_status(conn, lead["id"], "blocked", why)
                continue                             # this lead can't go now; try the next

            data = json.loads(lead["data_json"])
            draft, ok, why = draft_with_retry(campaign, data)
            drafted += 1
            if not ok:
                db.set_lead_status(conn, lead["id"], "blocked", why)
                log.warning("blocked %s: %s", lead["email"], why)
                if drafted >= MAX_DRAFTS_PER_CYCLE:
                    return "several drafts blocked; will retry next cycle"
                continue

            sent = _send(conn, campaign, lead, draft, "initial", dry_run)
            return f"sent to {lead['email']}" if sent else "send failed"
        return waiting_for_resume or "nothing eligible to send"
