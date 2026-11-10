"""
config.py — every tunable setting for the outreach agent lives here.

Nothing in this file is secret. Secrets (the Gmail OAuth token and the Anthropic
API key) are stored in the macOS Keychain via the `keyring` library, and the
Google OAuth client file lives in DATA_DIR (outside the project folder).

If you want to change *how aggressive* the agent is, this is the file to edit.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent

# Runtime data (database, logs, the OAuth client file) lives in the home folder,
# NOT in the project folder, so it can never be committed or shared by accident.
DATA_DIR = Path(os.environ.get("OUTREACH_DATA_DIR", Path.home() / ".outreach-agent"))
DB_PATH = DATA_DIR / "outreach.db"
LOG_PATH = DATA_DIR / "outreach.log"
CLIENT_SECRET_PATH = DATA_DIR / "credentials.json"  # downloaded from Google Cloud
PAUSE_FILE = DATA_DIR / "PAUSED"                    # if this file exists, nothing is sent
LOCK_PATH = DATA_DIR / "daemon.lock"                # stops two daemons running at once

VOICE_DIR = BASE_DIR / "voice"
SAMPLES_PATH = VOICE_DIR / "samples.jsonl"          # curated sent-email examples
STYLE_GUIDE_PATH = VOICE_DIR / "style_guide.md"     # generated summary of your voice
CAMPAIGNS_DIR = BASE_DIR / "campaigns"              # one YAML file per use case
PROFILE_PATH = BASE_DIR / "profile.yaml"            # facts about YOU the agent may use

# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
SENDER_PLACEHOLDER = "your.sender@gmail.com"


def _sender_address() -> str:
    """
    The Gmail address emails are sent FROM. It is kept out of the code so the project can be
    shared without publishing it. Set it with `python cli.py set-sender you@gmail.com`
    (stored in ~/.outreach-agent/sender_address) or the OUTREACH_SENDER environment variable.
    """
    env = os.environ.get("OUTREACH_SENDER", "").strip()
    if env:
        return env
    f = DATA_DIR / "sender_address"
    return f.read_text().strip() if f.exists() and f.read_text().strip() else SENDER_PLACEHOLDER


SENDER_ADDRESS = _sender_address()
KEYRING_SERVICE = "outreach-agent"                  # Keychain service name

# Gmail scopes: send mail + read mail (to find replies). No delete/modify rights.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# --------------------------------------------------------------------------- #
# Resume + local portal
# --------------------------------------------------------------------------- #
RESUME_MAX_BYTES = 5 * 1024 * 1024   # bigger PDFs hurt deliverability; Gmail's hard limit is 25 MB
PORTAL_HOST = "127.0.0.1"            # localhost only: never reachable from other machines
PORTAL_PORT = 8765

# --------------------------------------------------------------------------- #
# Automatic lead intake (Hacker News "Who is hiring")
# --------------------------------------------------------------------------- #
INTAKE_ENABLED = True
INTAKE_CHECK_SECONDS = 6 * 3600      # how often the daemon looks for new posts
INTAKE_MAX_PER_DAY = 10              # new leads auto-added per day
INTAKE_QUEUE_MAX = 40                # stop adding while this many are still waiting to be emailed
# A new auto-found lead waits a random time in this range (hours) before its first email, so you
# have a short window to remove it in the portal. Random so sends don't look machine-timed.
INTAKE_GRACE_HOURS_MIN = 1
INTAKE_GRACE_HOURS_MAX = 2
INTAKE_POST_MAX_AGE_DAYS = 45        # ignore old posts
INTAKE_MAX_POSTS_PER_RUN = 60        # cap on Claude calls per run (cost control)

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
DRAFT_MODEL = "claude-sonnet-5-5"   # writes emails (quality matters)
CHECK_MODEL = "claude-haiku-5-5"    # fact-check / classify replies (cheap + fast)

# --------------------------------------------------------------------------- #
# Sending limits — "infrequent contact" policy
# --------------------------------------------------------------------------- #
# Ramp-up for a brand-new Gmail address: (days since first send, max sends/day).
# The agent uses the last row whose day threshold has been reached.
RAMP_SCHEDULE = [(0, 10), (7, 20), (14, 30), (21, 40)]
WEEKLY_CAP = 200                     # hard cap on sends in any rolling 7 days

# Only send during "normal human" hours, local time, weekdays.
SEND_HOUR_START = 9                  # 9:00
SEND_HOUR_END = 17                   # 17:00 (no sends at/after this hour)
SEND_WEEKDAYS = {0, 1, 2, 3, 4}      # Mon=0 ... Sun=6

# Random gap between two sends, so mail doesn't go out in a robotic burst.
MIN_GAP_MINUTES = 8
MAX_GAP_MINUTES = 25

ORG_COOLDOWN_DAYS = 30               # at most one person per company/lab per 30 days
RECONTACT_COOLDOWN_DAYS = 90         # same address can't be re-added for 90 days
REPLY_WATCH_DAYS = 60                # how long we keep watching a thread for replies

# Auto-pause: if >= BOUNCE_LIMIT of the last BOUNCE_WINDOW sends bounced, stop.
BOUNCE_WINDOW = 10
BOUNCE_LIMIT = 3

# --------------------------------------------------------------------------- #
# Daemon timing
# --------------------------------------------------------------------------- #
POLL_REPLIES_SECONDS = 60            # how often to check Gmail for replies
SEND_CHECK_SECONDS = 120             # how often to see if it's time to send
