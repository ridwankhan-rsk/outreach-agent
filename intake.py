"""
intake.py — automatic lead intake from Hacker News "Ask HN: Who is hiring?".

Every month companies post short hiring notes there, and many include a contact email on
purpose. This module turns those posts into queued leads, with no human in the loop:

    fetch newest thread (free HN API)  ->  skip posts already seen / too old / without an email
      ->  Claude (Haiku) extracts company, contact, note   ->  CODE re-checks everything
      ->  db.add_lead(...)  ->  the normal send pipeline takes over

Trust model: Claude only *proposes*. The code then verifies that
  * the email appears word-for-word in the post (so nothing is guessed),
  * a contact name, if used, appears word-for-word in the post,
  * the address is not a no-reply/role mailbox, and the company is not already queued or contacted.
Everything else (caps, cooldowns, suppression, fact-check, bounce auto-pause) is the existing
pipeline. New leads also wait INTAKE_GRACE_HOURS_MIN..MAX before their first email, so you can remove one
in the portal.

Only the public HN API is used (two requests per run); it respects the site's terms.
"""
from __future__ import annotations

import html
import json
import logging
import random
import re
import time
import urllib.request
from collections import Counter
from typing import Optional

import config
import db
import voice

log = logging.getLogger("outreach.intake")

SOURCE = "hn-hiring"
API = "https://hn.algolia.com/api/v1"
_UA = "outreach-agent/1.0 (personal job search; reads the public HN API)"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ROLE_MAILBOX = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer-daemon|postmaster|abuse|privacy|"
    r"legal|press|support|agency|agencies|staffing|recruitment)@", re.I)   # agencies aren't startups


# Country-code domains that mean "company is not in the US". (.ai/.io/.co/.dev/.app are used by
# plenty of US startups, so they are NOT listed.)
_NON_US_TLDS = {
    "uk", "de", "fr", "nl", "se", "no", "dk", "fi", "es", "it", "pt", "pl", "cz", "at", "be", "ie",
    "ch", "eu", "in", "jp", "kr", "cn", "hk", "tw", "sg", "au", "nz", "ca", "br", "mx", "ar", "cl",
    "il", "tr", "ru", "ua", "za", "ng", "ke", "ae", "sa", "pk", "bd", "id", "ph", "vn", "th", "my",
    "gr", "hu", "ro", "bg", "lt", "lv", "ee", "is", "lu", "li", "sk", "si", "hr", "rs",
}


# --------------------------------------------------------------------------- #
# Fetching (public HN API through Algolia)
# --------------------------------------------------------------------------- #

def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def latest_thread() -> Optional[dict]:
    """The newest 'Ask HN: Who is hiring?' story, or None."""
    hits = _get_json(f"{API}/search_by_date?tags=story,author_whoishiring&hitsPerPage=6")["hits"]
    for h in hits:
        if re.match(r"^Ask HN: Who is hiring\?", h.get("title", "")):
            return {"id": str(h["objectID"]), "title": h["title"]}
    return None


def to_text(html_s: str) -> str:
    """HN comment HTML -> plain text. Keeps full link URLs instead of HN's shortened labels."""
    s = re.sub(r"<\s*(?:p|br)\s*/?>", "\n", html_s, flags=re.I)
    s = re.sub(r"<a [^>]*href=\"([^\"]+)\"[^>]*>.*?</a>", r"\1", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def fetch_post_text(post_id: str) -> str:
    """One hiring post's text by id (used to re-check leads that are already queued)."""
    return to_text(_get_json(f"{API}/items/{post_id}").get("text") or "")


def fetch_posts(thread_id: str) -> list[dict]:
    """Top-level comments of the thread (one hiring post each), newest first."""
    item = _get_json(f"{API}/items/{thread_id}")
    posts = []
    for c in item.get("children", []):
        if c.get("text") and not c.get("deleted"):
            posts.append({"id": str(c["id"]), "ts": int(c.get("created_at_i", 0)),
                          "text": to_text(c["text"])})
    return sorted(posts, key=lambda p: p["ts"], reverse=True)


# --------------------------------------------------------------------------- #
# Claude extraction (proposes only; code re-checks below)
# --------------------------------------------------------------------------- #

EXTRACT_TOOL = {
    "name": "extract_post",
    "description": "Facts extracted from one Hacker News hiring post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_startup": {"type": "boolean", "description":
                "True for an early-stage or small company. False for large/public companies, "
                "staffing or recruiting agencies, or consultancies."},
            "has_technical_roles": {"type": "boolean", "description":
                "True if any role is software engineering, data science, or machine learning."},
            "company": {"type": "string", "description": "Company name exactly as written."},
            "contact_email": {"type": "string", "description":
                "The email address applicants should use, copied EXACTLY from the post. "
                "Empty string if there is none."},
            "contact_name": {"type": "string", "description":
                "Full name of a person, ONLY if the post explicitly names them as the contact "
                "or hiring person. Otherwise an empty string. Never infer it from the email."},
            "note": {"type": "string", "description":
                "One factual sentence (max 200 characters) on what the company does and/or is "
                "hiring for, using only the post's own words. No opinions, no guesses."},
            "mentions_interns": {"type": "boolean"},
            "explicitly_non_us": {"type": "boolean", "description":
                "True ONLY if the post explicitly places the company or its roles outside the "
                "United States (a non-US city or country, e.g. 'Zurich', 'London', 'EU only', "
                "'Canada'). False if a US location is stated, if it is remote/worldwide/anywhere, "
                "or if no location is stated. If both US and non-US locations are listed, False."},
        },
        "required": ["is_startup", "has_technical_roles", "company", "contact_email",
                     "contact_name", "note", "mentions_interns", "explicitly_non_us"],
    },
}

_EXTRACT_SYSTEM = [{"type": "text", "text": (
    "You read ONE job post from Hacker News 'Who is hiring?' and extract facts from it. "
    "Use ONLY text that appears in the post. Never guess, infer or add anything. "
    "If a field is not stated, return an empty string (or false). Return the result by "
    "calling the tool."
)}]


def extract(post_text: str) -> dict:
    return voice._call_tool(config.CHECK_MODEL, _EXTRACT_SYSTEM,
                            f"POST:\n{post_text[:4000]}", EXTRACT_TOOL, max_tokens=1000)


# --------------------------------------------------------------------------- #
# Code-side validation of what Claude proposed
# --------------------------------------------------------------------------- #

def judge(post_text: str, found: dict) -> tuple[Optional[dict], str]:
    """
    Decide whether `found` (Claude's extraction) becomes a lead. Returns (lead_row, '') or
    (None, reason_skipped). Pure function: no network, no database.
    """
    if not found.get("is_startup"):
        return None, "not a startup"
    if not found.get("has_technical_roles"):
        return None, "no technical roles"
    if found.get("explicitly_non_us") is True:          # only an EXPLICIT non-US location excludes a lead
        return None, "explicitly non-US"

    text_low = post_text.lower()
    emails_in_post = {e.lower() for e in EMAIL_RE.findall(post_text)}
    email = (found.get("contact_email") or "").strip().lower()
    if not email or email not in emails_in_post:             # must be verbatim from the post
        return None, "email not found verbatim in post"
    if _ROLE_MAILBOX.match(email):
        return None, "no-reply/role mailbox"
    if email.rsplit(".", 1)[-1] in _NON_US_TLDS:         # a country-code domain is an explicit non-US signal
        return None, "non-US email domain"

    company = (found.get("company") or "").strip()
    note = (found.get("note") or "").strip()
    if not company or len(company) > 80:
        return None, "no usable company name"
    if len(note) < 15:
        return None, "no usable note"

    # Greet a person by name only if the name is in the post AND matches the address itself
    # (jane.doe@ <-> "Jane Doe"). A name merely mentioned in the post may not be who reads
    # careers@, and greeting the wrong person is worse than a team greeting.
    name = (found.get("contact_name") or "").strip()
    local = re.sub(r"[^a-z]", "", email.split("@")[0])
    name_in_address = any(len(t) >= 3 and t in local for t in re.findall(r"[a-z]+", name.lower()))
    if name and (name.lower() not in text_low or len(name) > 60 or re.search(r"[\d@/]", name)
                 or not name_in_address):
        name = ""

    excerpt = EMAIL_RE.sub("(email omitted)", post_text)[:600].strip()
    row = {
        "email": email, "name": name, "org": company, "note": note[:300],
        "post_excerpt": excerpt, "found_via": "Hacker News 'Who is hiring?' thread",
    }
    return row, ""


def campaign_for(row: dict) -> str:
    return "founders" if row.get("name") else "hiring_team"


# --------------------------------------------------------------------------- #
# Re-checking leads that are already queued (after the rules get stricter)
# --------------------------------------------------------------------------- #

def recheck_queue() -> list[dict]:
    """
    Re-run extraction + judging for every auto-found lead that has NOT been emailed yet, using
    the current rules. Leads that no longer qualify are marked 'removed'. Leads that an EARLIER
    re-check removed (reason starts 'recheck:') are restored if they qualify now, e.g. after a
    rule is loosened. Leads you removed yourself in the portal are never restored.
    Returns one dict per lead: {org, email, kept, reason, restored}. Leads already emailed are
    never touched.
    """
    out = []
    with db.connect() as conn:
        leads = conn.execute(
            "SELECT id, email, org, data_json, status FROM leads WHERE source=? AND "
            "(status='new' OR (status='removed' AND status_reason LIKE 'recheck:%'))", (SOURCE,)
        ).fetchall()
        for lead in leads:
            data = json.loads(lead["data_json"])
            m = re.search(r"item\?id=(\d+)", data.get("source_url", ""))
            if not m:
                continue
            try:
                text = fetch_post_text(m.group(1))
                row, why = judge(text, extract(text))
            except Exception as exc:                  # can't verify right now: leave it, retry later
                log.warning("recheck failed for %s: %s", lead["email"], exc)
                out.append({"org": lead["org"], "email": lead["email"],
                            "kept": lead["status"] == "new", "restored": False,
                            "reason": f"could not re-check ({exc.__class__.__name__})"})
                continue
            passes = row is not None and row["email"] == lead["email"]
            restored = False
            if passes and lead["status"] == "removed":
                db.set_lead_status(conn, lead["id"], "new", "")
                restored = True
            elif not passes:                          # new -> removed, or refresh a stale removal reason
                why = why or "contact email changed"
                db.set_lead_status(conn, lead["id"], "removed", f"recheck: {why}")
            conn.commit()
            out.append({"org": lead["org"], "email": lead["email"], "kept": passes,
                        "restored": restored, "reason": why})
    return out


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

def _org_already_in_play(conn, org: str) -> bool:
    """Same company already queued, or emailed within the org cooldown?"""
    key = db.normalise_org(org)
    if not key:
        return False
    cutoff = db.now() - config.ORG_COOLDOWN_DAYS * 86400
    return conn.execute(
        "SELECT 1 FROM leads WHERE org_key=? AND (status='new' OR "
        "id IN (SELECT lead_id FROM sends WHERE sent_at>?)) LIMIT 1", (key, cutoff)
    ).fetchone() is not None


def run_once(dry_run: bool = False, max_posts: Optional[int] = None) -> dict:
    """
    One intake pass. With dry_run=True nothing is written (no leads, no 'seen' marks) so you can
    inspect the decisions. Returns {'thread', 'scanned', 'added', 'skipped': {reason: n}, 'leads': [...]}.
    """
    summary = {"thread": None, "scanned": 0, "added": 0, "skipped": Counter(), "leads": []}
    thread = latest_thread()
    if not thread:
        return summary
    summary["thread"] = thread["title"]
    posts = fetch_posts(thread["id"])
    max_posts = max_posts or config.INTAKE_MAX_POSTS_PER_RUN
    min_ts = time.time() - config.INTAKE_POST_MAX_AGE_DAYS * 86400

    with db.connect() as conn:
        seen = {r["post_id"] for r in conn.execute(
            "SELECT post_id FROM intake_seen WHERE source=?", (SOURCE,))}
        added_today = conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE source=? AND added_at>=?",
            (SOURCE, db.local_midnight_epoch())).fetchone()["c"]
        queued = conn.execute("SELECT COUNT(*) c FROM leads WHERE status='new'").fetchone()["c"]
        room = min(config.INTAKE_MAX_PER_DAY - added_today, config.INTAKE_QUEUE_MAX - queued)

        def mark(post_id, outcome):
            summary["skipped"][outcome] += outcome != "queued"
            if not dry_run:
                conn.execute("INSERT OR REPLACE INTO intake_seen VALUES(?,?,?,?)",
                             (SOURCE, post_id, outcome, db.now()))
                conn.commit()

        calls = 0
        planned_orgs: set[str] = set()
        for post in posts:
            if room <= 0 or calls >= max_posts:
                break
            if post["id"] in seen:
                continue
            if post["ts"] < min_ts:
                mark(post["id"], "too old")
                continue
            if not EMAIL_RE.search(post["text"]):
                mark(post["id"], "no email in post")
                continue

            summary["scanned"] += 1
            calls += 1
            try:
                found = extract(post["text"])
            except Exception as exc:                          # API hiccup: retry next run
                log.warning("extraction failed for post %s: %s", post["id"], exc)
                continue
            row, why = judge(post["text"], found)
            if row is None:
                mark(post["id"], why)
                continue
            org_key = db.normalise_org(row["org"])
            if _org_already_in_play(conn, row["org"]) or org_key in planned_orgs:
                mark(post["id"], "company already queued/contacted")
                continue

            campaign = campaign_for(row)
            row["source_url"] = f"https://news.ycombinator.com/item?id={post['id']}"
            if dry_run:
                ok, why = db.can_add_lead(conn, row["email"])
            else:
                ok, why = db.add_lead(conn, campaign, row, source=SOURCE,
                                      delay_seconds=int(random.uniform(
                                          config.INTAKE_GRACE_HOURS_MIN,
                                          config.INTAKE_GRACE_HOURS_MAX) * 3600))
            if not ok:
                mark(post["id"], why)
                continue
            mark(post["id"], "queued")
            planned_orgs.add(org_key)               # (a dry run writes nothing, so remember here)
            summary["added"] += 1
            summary["leads"].append({**row, "campaign": campaign})
            room -= 1

    summary["skipped"] = dict(summary["skipped"])
    return summary
