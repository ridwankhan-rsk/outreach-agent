"""
resume.py — the ONE file this agent is ever allowed to attach to an email.

Why a dedicated module: attachments used to be "any path listed in a campaign YAML", which
means a typo could mail out the wrong file. Now the only attachable file is the PDF stored
here, uploaded through `cli.py set-resume` or the local portal (`cli.py portal`).

Layout (in ~/.outreach-agent/resume/):
    current.pdf            what gets attached to new emails
    history/resume-<date>-<hash>.pdf   every previous version, kept so nothing is ever lost

Uploading a new PDF replaces `current.pdf` for all emails sent AFTER the upload; emails
already sent are not changed.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Optional

import config

HISTORY_KEEP = 20
HISTORY_NAME = re.compile(r"^resume-\d{8}-\d{6}-[0-9a-f]{8}\.pdf$")


class ResumeError(ValueError):
    """The uploaded file is not an acceptable resume (message is safe to show the user)."""


def _dir() -> Path:
    return config.DATA_DIR / "resume"


def history_dir() -> Path:
    return _dir() / "history"


def current_path() -> Optional[Path]:
    p = _dir() / "current.pdf"
    return p if p.exists() else None


def validate(data: bytes) -> None:
    """Raise ResumeError unless `data` looks like a complete, reasonably sized PDF."""
    if not data:
        raise ResumeError("The file is empty.")
    if len(data) > config.RESUME_MAX_BYTES:
        raise ResumeError(f"The file is {len(data) / 1e6:.1f} MB; the limit is "
                          f"{config.RESUME_MAX_BYTES / 1e6:.0f} MB.")
    if not data.startswith(b"%PDF-"):
        raise ResumeError("That is not a PDF file.")
    if b"%%EOF" not in data[-2048:]:
        raise ResumeError("The PDF looks cut off (no end marker). Export it again and retry.")


def save(data: bytes) -> dict:
    """Validate, archive the old version, and make `data` the current resume."""
    validate(data)
    d, h = _dir(), history_dir()
    h.mkdir(parents=True, exist_ok=True)
    cur = d / "current.pdf"
    if cur.exists():
        old = cur.read_bytes()
        if hashlib.sha256(old).digest() == hashlib.sha256(data).digest():
            return info()                              # same file again: nothing to do
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(cur.stat().st_mtime))
        (h / f"resume-{stamp}-{hashlib.sha256(old).hexdigest()[:8]}.pdf").write_bytes(old)
    tmp = d / ".incoming.pdf"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, cur)                               # atomic: a send never sees a half-written file
    for extra in sorted(h.glob("resume-*.pdf"))[:-HISTORY_KEEP]:
        extra.unlink()
    return info()


def history() -> list[dict]:
    out = []
    for p in sorted(history_dir().glob("resume-*.pdf"), reverse=True):
        if HISTORY_NAME.match(p.name):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "saved_at": int(st.st_mtime)})
    return out


def info() -> Optional[dict]:
    p = current_path()
    if not p:
        return None
    st = p.stat()
    return {
        "size": st.st_size,
        "uploaded_at": int(st.st_mtime),
        "sha": hashlib.sha256(p.read_bytes()).hexdigest()[:8],
        "history": history(),
    }


def attaches(campaign: dict, kind: str = "initial") -> bool:
    """Does this campaign attach the resume to this kind of email? (first email only)"""
    return kind == "initial" and bool(campaign.get("attach_resume"))


def attachment_name(person_name: str) -> str:
    """'Sam Lee' -> 'Sam_Lee_Resume.pdf' (what the recipient sees)."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", person_name).strip("_") or "My"
    return f"{safe}_Resume.pdf"
