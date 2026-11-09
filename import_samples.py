"""
import_samples.py — turn exported sent-mail into clean voice samples.

INPUT  (any mix, pass paths on the command line):
  *.mbox   Google Takeout "Sent" export from a Gmail account
  *.eml    single messages saved from Outlook / Apple Mail
  *.json   Microsoft Graph Explorer output of /me/mailFolders/sentitems/messages
           (needs fields: subject, body{contentType,content}, sentDateTime)
  a folder containing any of the above (searched recursively)

OUTPUT voice/samples.jsonl — one JSON object per line:
  {"id": ..., "register": "formal|professional|casual", "subject": ..., "body": ...}

PRIVACY: recipient addresses are never written to the output, and phone numbers /
email addresses inside the text are masked. Only the file you keep (after
skimming it) is used, and only ~20-40 examples are ever sent to the Claude API.
"""
from __future__ import annotations

import email
import hashlib
import html
import json
import mailbox
import re
import sys
from email import policy
from html.parser import HTMLParser
from pathlib import Path

import config

# --------------------------------------------------------------------------- #
# Text cleaning helpers
# --------------------------------------------------------------------------- #

class _TextExtractor(HTMLParser):
    """Minimal HTML -> text converter (keeps line breaks for <br> and <p>)."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0  # depth inside <style>/<script>

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self._skip = max(0, self._skip - 1)
        if tag in ("p", "div"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    p = _TextExtractor()
    p.feed(raw)
    return html.unescape("".join(p.parts))


# Lines that begin the quoted/previous-message part of a reply: everything after is dropped.
_QUOTE_STARTS = [
    re.compile(r"^On .{5,120} wrote:\s*$", re.I),            # Gmail / Apple Mail
    re.compile(r"^-{2,}\s*(Original Message|Forwarded message)", re.I),
    re.compile(r"^From:\s.+", re.I),                         # Outlook header block
    re.compile(r"^_{5,}\s*$"),                               # Outlook divider
    re.compile(r"^Sent from my ", re.I),
    re.compile(r"^Get Outlook for ", re.I),
]
_SIGNOFF = re.compile(
    r"^(best|best regards|regards|kind regards|thanks|thank you|sincerely|cheers|"
    r"warmly|respectfully|best wishes|many thanks|thanks so much|thanks again)[,!.]?\s*$",
    re.I,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(\+?\d[\d\s().-]{8,}\d)")
_URL = re.compile(r"https?://\S+")


def strip_quotes_and_signature(text: str) -> str:
    """Keep only what the user actually wrote in this message."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):            # quoted line
            continue
        if any(rx.match(stripped) for rx in _QUOTE_STARTS):
            break                               # rest is the quoted thread
        if stripped == "--" or stripped == "-- ":
            break                               # classic signature delimiter
        out.append(line.rstrip())
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def trim_signature_block(text: str) -> str:
    """After a sign-off like 'Best,' keep the sign-off + name line, drop contact blocks."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if _SIGNOFF.match(line.strip()):
            return "\n".join(lines[: i + 2]).strip()  # sign-off and the name under it
    return text


def mask_private(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    return text


def guess_register(subject: str, body: str) -> str:
    """Rough heuristic. You can hand-edit the 'register' field in samples.jsonl afterwards."""
    low = body.lower()
    if re.search(r"\bdear (prof|professor|dr\.?|mr|ms|mrs)", low) or any(
        w in low for w in ("sincerely", "respectfully", "i am writing to")
    ):
        return "formal"
    if len(body.split()) < 45 and not re.match(r"^\s*(dear|hello)\b", low):
        return "casual"
    return "professional"


def is_good_sample(subject: str, body: str) -> bool:
    words = len(body.split())
    if not (30 <= words <= 350):                # too short = "ok thanks", too long = essays
        return False
    if re.match(r"^\s*(fw|fwd|automatic reply|out of office)", subject, re.I):
        return False
    # Bounce notices, invitations and other mail you didn't actually write yourself.
    if re.search(r"delivery status|undeliverable|mail delivery|returned mail|invited|invitation"
                 r"|newsletter|receipt|your order", subject, re.I):
        return False
    if re.search(r"mailer-daemon|delivery has failed|message wasn't delivered", body, re.I):
        return False
    if len(_URL.findall(body)) > 3:             # mostly links = newsletter-ish
        return False
    return True


# --------------------------------------------------------------------------- #
# Readers for each input format. Each yields (subject, plain_text_body).
# --------------------------------------------------------------------------- #

def _body_of(msg) -> str:
    """Pull the best text body out of an email.message.EmailMessage."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    return html_to_text(content) if part.get_content_type() == "text/html" else content


def read_mbox(path: Path):
    box = mailbox.mbox(str(path))
    try:
        for m in box:
            msg = email.message_from_bytes(m.as_bytes(), policy=policy.default)
            yield str(msg.get("subject", "")), _body_of(msg)
    finally:
        box.close()


def read_eml(path: Path):
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    yield str(msg.get("subject", "")), _body_of(msg)


def read_graph_json(path: Path):
    data = json.loads(path.read_text())
    for item in data.get("value", data if isinstance(data, list) else []):
        body = item.get("body", {})
        content = body.get("content", "")
        if str(body.get("contentType", "")).lower() == "html":
            content = html_to_text(content)
        yield item.get("subject", ""), content


def iter_messages(paths: list[str]):
    for p in map(Path, paths):
        files = [p] if p.is_file() else sorted(p.rglob("*"))
        for f in files:
            suffix = f.suffix.lower()
            try:
                if suffix == ".mbox":
                    yield from read_mbox(f)
                elif suffix == ".eml":
                    yield from read_eml(f)
                elif suffix == ".json":
                    yield from read_graph_json(f)
            except Exception as exc:  # one bad file shouldn't abort the whole import
                print(f"  ! skipped {f.name}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def run(paths: list[str], limit: int = 300) -> int:
    seen: set[str] = set()
    topic_counts: dict[str, int] = {}
    kept: list[dict] = []
    total = 0
    for subject, raw in iter_messages(paths):
        total += 1
        body = trim_signature_block(strip_quotes_and_signature(raw))
        body = mask_private(body)
        if not is_good_sample(subject, body):
            continue
        digest = hashlib.sha1(body.encode()).hexdigest()[:12]
        if digest in seen:                      # identical text sent to many people
            continue
        # A long back-and-forth ("Re: Add Course Error" x 6) would flood the samples with one
        # topic, so keep at most 2 messages per conversation subject.
        topic = re.sub(r"^((re|fwd?):\s*)+", "", subject.strip().lower())
        if topic_counts.get(topic, 0) >= 2:
            continue
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        seen.add(digest)
        kept.append({
            "id": digest,
            "register": guess_register(subject, body),
            "subject": subject.strip(),
            "body": body,
        })
    kept = kept[:limit]

    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    # Append to any existing samples so you can import Gmail then Outlook separately.
    existing = []
    if config.SAMPLES_PATH.exists():
        existing = [json.loads(l) for l in config.SAMPLES_PATH.read_text().splitlines() if l.strip()]
    known = {e["id"] for e in existing}
    new = [k for k in kept if k["id"] not in known]
    with config.SAMPLES_PATH.open("a") as f:
        for k in new:
            f.write(json.dumps(k) + "\n")

    print(f"Read {total} messages, kept {len(kept)}, added {len(new)} new "
          f"-> {config.SAMPLES_PATH}")
    print("NEXT: open that file, delete any lines you don't want, then run: "
          "python cli.py build-style")
    return len(new)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python import_samples.py <file-or-folder> [...]")
    run(sys.argv[1:])
