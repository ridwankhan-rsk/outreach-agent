"""
guard.py — the safety rules that make fully automatic sending acceptable.

Because no human reviews each email, EVERY rule below is enforced in code.
An email is sent only if every check passes. Anything that fails is either
skipped for now (e.g. "outside send hours") or marked 'blocked' with a reason
you can read via `python cli.py status`.

There are two stages:
  pre_send_checks()   cheap, local rules (caps, hours, cooldowns, required fields)
  draft_checks()      checks on the written email (placeholders, length, fact-check)
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

import config
import db
import resume as resume_store      # (guard.resume() is the un-pause command)
import voice

# --------------------------------------------------------------------------- #
# Campaign loading
# --------------------------------------------------------------------------- #

def load_campaigns(only_enabled: bool = True) -> dict[str, dict]:
    """Read every campaigns/*.yaml file into a dict keyed by campaign name."""
    out = {}
    for path in sorted(config.CAMPAIGNS_DIR.glob("*.yaml")):
        c = yaml.safe_load(path.read_text())
        if only_enabled and not c.get("enabled", True):
            continue
        out[c["name"]] = c
    return out


# --------------------------------------------------------------------------- #
# Pause / kill switch
# --------------------------------------------------------------------------- #

def is_paused() -> Optional[str]:
    """Returns the pause reason if sending is paused, else None."""
    if config.PAUSE_FILE.exists():
        return config.PAUSE_FILE.read_text().strip() or "paused by user"
    return None


def pause(reason: str) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.PAUSE_FILE.write_text(reason)


def resume() -> None:
    if config.PAUSE_FILE.exists():
        config.PAUSE_FILE.unlink()


# --------------------------------------------------------------------------- #
# Caps
# --------------------------------------------------------------------------- #

def daily_limit(conn) -> int:
    """Ramp-up: the daily cap grows with the number of days since the first send."""
    first = db.get_state(conn, "first_send_at")
    days = 0 if not first else (db.now() - int(first)) // 86400
    limit = config.RAMP_SCHEDULE[0][1]
    for threshold, cap in config.RAMP_SCHEDULE:
        if days >= threshold:
            limit = cap
    return limit


def in_send_window(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    return (
        now.weekday() in config.SEND_WEEKDAYS
        and config.SEND_HOUR_START <= now.hour < config.SEND_HOUR_END
    )


# --------------------------------------------------------------------------- #
# Stage 1: rules about *whether* and *to whom* we may send
# --------------------------------------------------------------------------- #

def missing_fields(campaign: dict, lead_data: dict) -> list[str]:
    required = set(campaign.get("required_fields", [])) | {"email"}
    return sorted(f for f in required if not str(lead_data.get(f, "")).strip())


def pre_send_checks(conn, campaign: dict, lead, kind: str = "initial") -> tuple[bool, str]:
    """
    Returns (ok, reason). `reason` is '' when ok.
    Reasons that start with 'later:' mean "try again later"; anything else means
    this specific lead should be blocked.
    """
    import json
    data = json.loads(lead["data_json"])

    # --- Is sending allowed at all right now? (transient: try later) -------- #
    if is_paused():
        return False, "later: paused"
    if resume_store.attaches(campaign, kind) and resume_store.current_path() is None:
        return False, "later: no resume uploaded (run: python cli.py set-resume FILE.pdf)"
    if not in_send_window():
        return False, "later: outside send hours"
    if db.sends_since(conn, db.local_midnight_epoch()) >= daily_limit(conn):
        return False, "later: daily cap reached"
    if db.sends_since(conn, db.now() - 7 * 86400) >= config.WEEKLY_CAP:
        return False, "later: weekly cap reached"
    if db.sends_since(conn, db.local_midnight_epoch(), campaign["name"]) >= campaign.get("daily_cap", 5):
        return False, "later: campaign daily cap reached"

    # --- Is this specific person OK to email? (permanent: block) ------------ #
    if db.is_suppressed(conn, lead["email"]):
        return False, "on suppression list"
    miss = missing_fields(campaign, data)
    if miss:
        return False, f"missing required fields: {', '.join(miss)}"

    # --- Org cooldown: one person per company/lab per ORG_COOLDOWN_DAYS ----- #
    if kind == "initial" and lead["org_key"]:
        cutoff = db.now() - config.ORG_COOLDOWN_DAYS * 86400
        clash = conn.execute(
            "SELECT 1 FROM sends s JOIN leads l ON l.id=s.lead_id "
            "WHERE l.org_key=? AND l.id<>? AND s.kind='initial' AND s.sent_at>?",
            (lead["org_key"], lead["id"], cutoff),
        ).fetchone()
        if clash:
            return False, "later: another person at this org was contacted recently"
    return True, ""


# --------------------------------------------------------------------------- #
# Stage 2: rules about the written email itself
# --------------------------------------------------------------------------- #

_PLACEHOLDER = re.compile(r"(\[[^\]]{1,40}\]|\{\{|\}\}|TODO|lorem ipsum|<[A-Za-z ]+>)")
_OPTOUT = re.compile(r"unsubscribe|opt[- ]out|reply stop", re.I)
_TITLE_GUESS = re.compile(r"\b(Mr|Ms|Mrs|Mx)\.\s", re.I)
_SOFT_TIME = re.compile(r"\b(last|this) (summer|spring|fall|winter|year|semester)\b|\brecently\b", re.I)
_CLASS_STANDING = re.compile(r"\b(freshman|sophomore|junior|senior)\b", re.I)
_ATTACH_WORD = re.compile(r"\battach(?:ed|ing|ment|ments)?\b", re.I)
_SIGNOFF_LINE = re.compile(
    r"^(best|best regards|regards|kind regards|sincerely|warm regards|respectfully|thanks|"
    r"thank you|cheers)[,.]?$", re.I)


def draft_checks(campaign: dict, lead_data: dict, draft: dict, kind: str = "initial") -> tuple[bool, str]:
    """Static checks first (free), then the Claude fact-check pass."""
    subject, body = draft.get("subject", "").strip(), draft.get("body", "").strip()
    if not subject or not body:
        return False, "empty subject or body"
    if _PLACEHOLDER.search(subject + body):
        return False, "contains a placeholder"
    if _OPTOUT.search(body):
        return False, "contains an opt-out line (not wanted)"

    # Free static checks for things that are easy to detect without calling Claude.
    # Words that appear in the lead's OWN data (e.g. role "Senior Recruiter", note "recently
    # launched") are fine to repeat; only flag ones the model introduced itself.
    lead_text = " ".join(str(v) for v in lead_data.values()).lower()

    def introduced(rx) -> bool:
        return any(m.group(0).lower() not in lead_text for m in rx.finditer(body))

    if _TITLE_GUESS.search(body) and not str(lead_data.get("title", "")).strip():
        return False, "uses Mr./Ms./Mrs. without a title in the lead data"   # guessed gender
    if introduced(_SOFT_TIME):
        return False, "uses relative time phrase (e.g. 'last summer')"
    if introduced(_CLASS_STANDING):
        return False, "states class standing, which is not in the profile"
    if not resume_store.attaches(campaign, kind) and introduced(_ATTACH_WORD):
        return False, "mentions an attachment, but nothing will be attached"
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    for i, l in enumerate(lines[1:], 1):                 # doubled closing
        if _SIGNOFF_LINE.match(l) and lines[i - 1].endswith(","):
            return False, "double sign-off (closing phrase above the sign-off)"

    words = len(body.split())
    if kind == "initial":
        lo, hi = campaign.get("length_words", [70, 130])
        if not (lo * 0.6 <= words <= hi * 1.5):          # generous tolerance
            return False, f"length {words} words outside {lo}-{hi}"
    elif words > 120:
        return False, "follow-up too long"

    verdict = voice.fact_check(draft, campaign, lead_data, kind=kind)
    if verdict.get("ok") is not True:
        problems = "; ".join(verdict.get("problems", []))[:300] or "checker gave no reason"
        return False, "fact-check failed: " + problems
    return True, ""


# --------------------------------------------------------------------------- #
# Auto-pause on bounces
# --------------------------------------------------------------------------- #

def bounce_check(conn) -> Optional[str]:
    """If too many of the last N sends bounced, pause everything. Returns the reason if paused."""
    recent = conn.execute(
        "SELECT lead_id FROM sends ORDER BY sent_at DESC LIMIT ?", (config.BOUNCE_WINDOW,)
    ).fetchall()
    if len(recent) < config.BOUNCE_LIMIT:
        return None
    ids = [r["lead_id"] for r in recent]
    q = ",".join("?" * len(ids))
    bounced = conn.execute(
        f"SELECT COUNT(*) c FROM leads WHERE id IN ({q}) AND status='bounced'", ids
    ).fetchone()["c"]
    if bounced >= config.BOUNCE_LIMIT:
        reason = f"auto-paused: {bounced} of the last {len(ids)} emails bounced"
        pause(reason)
        return reason
    return None
