"""
daemon.py — the always-on loop. Started at login by launchd (see com.ridwan.outreach.plist).

Every few seconds it checks two timers:
  * every POLL_REPLIES_SECONDS  -> look for replies (and alert you)
  * every SEND_CHECK_SECONDS    -> see whether it's time to send the next email

"Active whenever you use the laptop": launchd starts this at login, and the loop simply stops
making progress while the Mac sleeps (time passes, then the next tick catches up). It does not
send anything outside weekday 9-5 local time (see config.py), so waking the laptop at 2 am
won't cause a burst of mail.
"""
from __future__ import annotations

import fcntl
import logging
import sys
import threading
import time

import config
import engine
import intake
import notify
import replies

_intake_running = threading.Event()


def _intake_job() -> None:
    """Find new leads on Hacker News. Runs in its own thread so a slow pass never delays
    reply checks or sends."""
    log = logging.getLogger("outreach.daemon")
    try:
        s = intake.run_once()
        log.info("intake: %s scanned=%d added=%d skipped=%s",
                 s["thread"], s["scanned"], s["added"], s["skipped"])
        if s["added"]:
            notify.quiet_notice(
                "Outreach intake",
                f"Added {s['added']} new lead(s) from Hacker News. They send after a "
                f"{config.INTAKE_GRACE_HOURS_MIN}-{config.INTAKE_GRACE_HOURS_MAX}h grace period; "
                "remove any in the portal.")
    except Exception:
        log.exception("intake failed; will retry")
    finally:
        _intake_running.clear()


def _setup_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(config.LOG_PATH), logging.StreamHandler(sys.stdout)],
    )


def _single_instance():
    """Hold an exclusive file lock so two daemons can never run (and double-send)."""
    fh = open(config.LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another outreach daemon is already running.")
    return fh  # keep a reference so the lock stays held


def run_forever(dry_run: bool = False) -> None:
    _setup_logging()
    log = logging.getLogger("outreach.daemon")
    _lock = _single_instance()  # noqa: F841
    log.info("daemon started (dry_run=%s)", dry_run)
    import portal
    portal.start_in_background()       # local-only upload page; open it with `cli.py portal`

    last_poll = last_send = last_intake = 0.0
    while True:
        now = time.time()
        try:
            if (config.INTAKE_ENABLED and not _intake_running.is_set()
                    and now - last_intake >= config.INTAKE_CHECK_SECONDS):
                last_intake = now
                _intake_running.set()
                threading.Thread(target=_intake_job, daemon=True).start()
            if now - last_poll >= config.POLL_REPLIES_SECONDS:
                last_poll = now
                n = replies.poll_once()
                if n:
                    log.info("processed %d new inbound messages", n)
            if now - last_send >= config.SEND_CHECK_SECONDS:
                last_send = now
                result = engine.send_cycle(dry_run=dry_run)
                log.info("send cycle: %s", result)
        except Exception:
            # Never let one bad cycle (network down, API hiccup) kill the daemon.
            log.exception("cycle failed; will retry")
        time.sleep(5)


if __name__ == "__main__":
    run_forever()
