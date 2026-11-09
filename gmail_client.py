"""
gmail_client.py — the only file that talks to Gmail.

Three jobs:
  authorize()        one-time browser login; stores the token in the macOS Keychain
  send_email()       send a message (optionally inside an existing thread)
  recent_inbound()   list recent messages from other people, to spot replies/bounces

SETUP (one time, done by you in a browser — see README.md):
  1. Create a Google Cloud project, enable the Gmail API.
  2. OAuth consent screen -> publish to "In production" (so tokens don't expire in 7 days).
  3. Create an OAuth client of type "Desktop app", download the JSON,
     save it as ~/.outreach-agent/credentials.json
  4. Run:  python cli.py set-sender you@gmail.com   then   python cli.py auth
"""
from __future__ import annotations

import base64
import mimetypes
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import keyring
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config

_TOKEN_KEY = "gmail_token_json"


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #

def _require_sender() -> None:
    if config.SENDER_ADDRESS == config.SENDER_PLACEHOLDER:
        raise RuntimeError("No sender address set. Run: python cli.py set-sender you@gmail.com")


def authorize() -> Credentials:
    """Open the browser for consent once, then save the refresh token in Keychain."""
    _require_sender()
    if not config.CLIENT_SECRET_PATH.exists():
        raise RuntimeError(
            f"Missing {config.CLIENT_SECRET_PATH}. Download the OAuth 'Desktop app' client JSON "
            "from Google Cloud Console and save it there (see README.md)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.CLIENT_SECRET_PATH), config.GMAIL_SCOPES
    )
    creds = flow.run_local_server(port=0, login_hint=config.SENDER_ADDRESS)
    keyring.set_password(config.KEYRING_SERVICE, _TOKEN_KEY, creds.to_json())
    return creds


def _credentials() -> Credentials:
    raw = keyring.get_password(config.KEYRING_SERVICE, _TOKEN_KEY)
    if not raw:
        raise RuntimeError("Not authorised yet. Run: python cli.py auth")
    creds = Credentials.from_authorized_user_info(__import__("json").loads(raw), config.GMAIL_SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())                      # silently get a fresh access token
        keyring.set_password(config.KEYRING_SERVICE, _TOKEN_KEY, creds.to_json())
    return creds


def service():
    """Authenticated Gmail API client."""
    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


def my_address() -> str:
    return service().users().getProfile(userId="me").execute()["emailAddress"].lower()


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

def send_email(
    to: str,
    subject: str,
    body: str,
    attachments: Optional[list] = None,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    sender_name: str = "",
) -> dict:
    """
    Send one email. Returns {'id': message id, 'threadId': thread id}.

    To make a follow-up land in the SAME conversation, pass the original
    `thread_id` and the original RFC Message-ID as `in_reply_to`.
    """
    _require_sender()
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = f"{sender_name} <{config.SENDER_ADDRESS}>" if sender_name else config.SENDER_ADDRESS
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)

    for att in attachments or []:                     # the engine only ever passes the resume
        # an entry is either a path, or (path, filename-the-recipient-sees)
        path, shown = att if isinstance(att, tuple) else (att, None)
        p = Path(path).expanduser()
        ctype, _ = mimetypes.guess_type(shown or p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype,
                           filename=shown or p.name)

    payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    return service().users().messages().send(userId="me", body=payload).execute()


def rfc_message_id(gmail_message_id: str) -> Optional[str]:
    """Look up the RFC 'Message-ID' header of a message we sent (needed for threading)."""
    m = service().users().messages().get(
        userId="me", id=gmail_message_id, format="metadata", metadataHeaders=["Message-ID"]
    ).execute()
    for h in m.get("payload", {}).get("headers", []):
        if h["name"].lower() == "message-id":
            return h["value"]
    return None


# --------------------------------------------------------------------------- #
# Reading replies
# --------------------------------------------------------------------------- #

def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data")
    return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace") if data else ""


def _plain_text(payload: dict) -> str:
    """Find the text/plain part of a Gmail message payload (recursing into multiparts)."""
    if payload.get("mimeType") == "text/plain":
        return _decode_part(payload)
    for part in payload.get("parts", []) or []:
        text = _plain_text(part)
        if text:
            return text
    return ""


_QUOTE_HEADER = re.compile(r"^On [^\n]{5,200}(?:\n[^\n]{1,80})?\s*wrote:\s*$", re.M)


def _strip_quoted(text: str) -> str:
    """Keep only the newly written part of a reply (drop '> quoted' history).

    Gmail sometimes wraps the 'On <date> <name> <email> wrote:' header over two lines, so the
    header pattern allows one line break before 'wrote:'."""
    m = _QUOTE_HEADER.search(text)
    if m:
        text = text[:m.start()]
    out = []
    for line in text.splitlines():
        if line.strip().startswith(">"):
            break
        out.append(line)
    return "\n".join(out).strip()


def recent_inbound(newer_than_days: int = 3, max_results: int = 50) -> list[dict]:
    """
    Messages from other people received in the last few days.
    One cheap list call per poll; we fetch full text only for messages we haven't seen.
    Returns dicts: {id, threadId, sender, subject, snippet, body, internal_ts}
    """
    svc = service()
    me = config.SENDER_ADDRESS.lower()
    q = f"-from:{me} newer_than:{newer_than_days}d"
    listing = svc.users().messages().list(userId="me", q=q, maxResults=max_results).execute()
    return [
        {"id": m["id"], "threadId": m["threadId"]} for m in listing.get("messages", [])
    ]


def fetch_message(message_id: str) -> dict:
    m = service().users().messages().get(userId="me", id=message_id, format="full").execute()
    return {
        "id": m["id"],
        "threadId": m["threadId"],
        "sender": _header(m, "From"),
        "subject": _header(m, "Subject"),
        "auto": _header(m, "Auto-Submitted").lower() not in ("", "no"),
        "snippet": m.get("snippet", ""),
        "body": _strip_quoted(_plain_text(m.get("payload", {}))) or m.get("snippet", ""),
        "internal_ts": int(m.get("internalDate", "0")) // 1000,
    }
