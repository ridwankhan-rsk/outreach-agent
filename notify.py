"""
notify.py — alerts on your Mac.

Both functions post a normal macOS banner in the top-right corner (like Messages), with a sound.

  priority_alert()  a reply that needs YOU. Clicking the banner opens the Gmail thread; the
                    banner's "Copy suggested reply" action puts the suggested reply on the
                    clipboard.
  quiet_notice()    everything else (declines, auto-pauses): same banner, no reply action.

How: a tiny native app, Outreach.app (source in notifier/, built by `cli.py build-notifier`),
posts the banner. It gets its own "Outreach" entry in System Settings > Notifications. If the
app isn't built yet we fall back to osascript, whose banners are attributed to Script Editor
and are often blocked.

To make banners stay until you dismiss them: System Settings > Notifications > Outreach >
set the style to "Alerts". To get through Focus modes, allow Outreach in the Focus settings.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Optional

import config

log = logging.getLogger("outreach.notify")
APP_PATH = config.DATA_DIR / "Outreach.app"


def _esc(s: str) -> str:
    """Escape text for use inside an AppleScript "..." string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _post(title: str, message: str, url: str = "", reply: str = "") -> None:
    if APP_PATH.exists():
        cmd = ["open", "-n", "-a", str(APP_PATH), "--args",
               "--title", title, "--body", message[:300]]
        if url:
            cmd += ["--url", url]
        if reply:
            cmd += ["--reply", reply]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            return
        log.error("could not launch notifier app: %s", r.stderr.strip())
    # Fallback: plain osascript banner
    r = subprocess.run(
        ["osascript", "-e",
         f'display notification "{_esc(message[:200])}" with title "{_esc(title)}" sound name "Glass"'],
        capture_output=True, text=True)
    if r.returncode != 0:
        log.error("osascript notification failed: %s", r.stderr.strip())


def quiet_notice(title: str, message: str) -> None:
    _post(title, message)


def priority_alert(
    title: str,
    message: str,
    thread_id: Optional[str] = None,
    suggested_reply: Optional[str] = None,
) -> None:
    url = (f"https://mail.google.com/mail/?authuser={config.SENDER_ADDRESS}#all/{thread_id}"
           if thread_id else "")
    _post(title, message, url=url, reply=suggested_reply or "")
