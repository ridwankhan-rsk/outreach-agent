"""
cli.py — the one command you use to control the agent.

    python cli.py <command>

Setup (once):
    init                 create the data folder + profile.yaml from the example
    set-api-key          store your Anthropic API key in the macOS Keychain
    set-sender ADDRESS   the Gmail address emails are sent from (stored locally, not in the code)
    auth                 log in to Gmail (opens a browser)
    import-samples PATH… turn exported sent mail (.mbox/.eml/.json or folders) into voice samples
    build-style          have Claude write voice/style_guide.md from your samples

Every day:
    add-leads CAMPAIGN FILE.csv   queue people to contact
    preview CAMPAIGN [N]          write N sample emails (NOT sent) so you can judge the voice
    status                        counts, caps, blocked leads, pause state
    pause [reason] / resume       the kill switch
    intake [--dry-run] [--limit N]  find leads on Hacker News "Who is hiring" (the agent also does this daily)
    set-resume FILE.pdf           replace the resume attached to new emails (old ones are kept)
    portal                        open the local web page (resume upload, status, replies)
    suppress EMAIL_OR_@DOMAIN     never contact this address/domain
    replies                       show recent replies and suggested responses

Running:
    run [--dry-run]               run the agent in the foreground (what launchd runs)
    install-daemon / uninstall-daemon   start automatically at login
    test-notify                   fire a sample priority alert
"""
from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import keyring

import config
import db


# --------------------------------------------------------------------------- #
# Setup commands
# --------------------------------------------------------------------------- #

def cmd_init(_):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db.connect():
        pass                                   # creates the tables
    if not config.PROFILE_PATH.exists():
        shutil.copy(config.BASE_DIR / "profile.example.yaml", config.PROFILE_PATH)
        print(f"Created {config.PROFILE_PATH} — fill in every TODO.")
    print(f"Data folder: {config.DATA_DIR}")
    print(f"Put your Google OAuth client file at: {config.CLIENT_SECRET_PATH}")


def cmd_set_api_key(_):
    key = getpass.getpass("Anthropic API key (input hidden): ").strip()
    keyring.set_password(config.KEYRING_SERVICE, "anthropic_api_key", key)
    print("Saved to Keychain.")


def cmd_set_sender(a):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "sender_address").write_text(a.address.strip() + "\n")
    print(f"Sender address set to {a.address.strip()} (stored locally, not in the code).")


def cmd_auth(_):
    import gmail_client
    gmail_client.authorize()
    print("Authorised as:", gmail_client.my_address())


def cmd_import_samples(a):
    import import_samples
    import_samples.run(a.paths)


def cmd_build_style(_):
    import voice
    print(voice.build_style_guide())
    print(f"\nSaved to {config.STYLE_GUIDE_PATH} — edit it freely.")


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #

def cmd_add_leads(a):
    import guard
    if a.campaign not in guard.load_campaigns(only_enabled=False):
        sys.exit(f"Unknown campaign '{a.campaign}'. Files in campaigns/: "
                 f"{', '.join(guard.load_campaigns(False))}")
    added = skipped = 0
    with db.connect() as conn, open(a.csv, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            ok, why = db.add_lead(conn, a.campaign, row)
            if ok:
                added += 1
            else:
                skipped += 1
                print(f"  skipped {row.get('email', '?')}: {why}")
    print(f"Added {added}, skipped {skipped}.")


def cmd_preview(a):
    """Write sample emails for the next queued leads. Nothing is sent or saved."""
    import guard
    import voice
    campaigns = guard.load_campaigns(only_enabled=False)
    campaign = campaigns[a.campaign]
    with db.connect() as conn:
        leads = conn.execute(
            "SELECT * FROM leads WHERE campaign=? AND status='new' ORDER BY added_at LIMIT ?",
            (a.campaign, a.n),
        ).fetchall()
    if not leads:
        sys.exit("No queued leads for that campaign. Use add-leads first.")
    for lead in leads:
        data = json.loads(lead["data_json"])
        miss = guard.missing_fields(campaign, data)
        print(f"\n=== {lead['email']} ===")
        if miss:
            print(f"(would be skipped — missing: {', '.join(miss)})")
            continue
        import engine
        draft, ok, why = engine.draft_with_retry(campaign, data)   # same path the daemon uses
        print(f"Subject: {draft['subject']}\n\n{draft['body']}")
        print(f"\n[{'PASS' if ok else 'WOULD BE BLOCKED: ' + why}]")


# --------------------------------------------------------------------------- #
# Monitoring + controls
# --------------------------------------------------------------------------- #

def cmd_status(_):
    import guard
    import resume
    with db.connect() as conn:
        paused = guard.is_paused()
        print(f"State:        {'PAUSED — ' + paused if paused else 'active'}")
        r = resume.info()
        attaching = [n for n, c in guard.load_campaigns(False).items() if c.get("attach_resume")]
        print(f"Resume:       " + (f"{r['size'] // 1024} KB, uploaded "
              f"{time.strftime('%b %d %H:%M', time.localtime(r['uploaded_at']))}" if r else
              "NONE UPLOADED" + (" — these campaigns won't send: " + ", ".join(attaching)
                                 if attaching else "")))
        print(f"Send window:  {'open now' if guard.in_send_window() else 'closed now'} "
              f"(weekdays {config.SEND_HOUR_START}:00–{config.SEND_HOUR_END}:00)")
        print(f"Sent today:   {db.sends_since(conn, db.local_midnight_epoch())} "
              f"/ {guard.daily_limit(conn)} (ramp-up cap)")
        print(f"Last 7 days:  {db.sends_since(conn, db.now() - 7 * 86400)} / {config.WEEKLY_CAP}")
        print("\nLeads by status:")
        for r in conn.execute("SELECT campaign,status,COUNT(*) c FROM leads GROUP BY 1,2 ORDER BY 1,2"):
            print(f"  {r['campaign']:<12} {r['status']:<18} {r['c']}")
        blocked = conn.execute(
            "SELECT email,status_reason FROM leads WHERE status IN ('blocked','followup_blocked') LIMIT 10"
        ).fetchall()
        if blocked:
            print("\nBlocked (not sent):")
            for b in blocked:
                print(f"  {b['email']}: {b['status_reason']}")
        n = conn.execute("SELECT COUNT(*) c FROM suppression").fetchone()["c"]
        print(f"\nSuppression list: {n} entries")


def cmd_intake(a):
    """Run the automatic Hacker News lead intake once, by hand (the daemon does this on a timer)."""
    import intake
    if a.recheck:
        for r in intake.recheck_queue():
            tag = "RESTORED" if r["restored"] else ("kept    " if r["kept"] else "REMOVED ")
            print(f"  {tag} {r['org']} <{r['email']}>"
                  + ("" if r["kept"] and not r["reason"] else f" — {r['reason']}"))
        return
    s = intake.run_once(dry_run=a.dry_run, max_posts=a.limit)
    print(f"{s['thread']}: looked at {s['scanned']} post(s) with an email; "
          f"{'would add' if a.dry_run else 'added'} {s['added']}.")
    for lead in s["leads"]:
        who = lead["name"] or "(team)"
        print(f"\n  [{lead['campaign']}] {lead['org']} — {who} <{lead['email']}>\n    note: {lead['note']}")
    if s["skipped"]:
        print("\nSkipped:", ", ".join(f"{k}: {v}" for k, v in sorted(s["skipped"].items())))
    if a.dry_run:
        print("\n(dry run: nothing was saved)")


def cmd_set_resume(a):
    import resume
    try:
        info = resume.save(Path(a.pdf).expanduser().read_bytes())
    except (OSError, resume.ResumeError) as exc:
        sys.exit(f"Could not use that file: {exc}")
    print(f"Resume saved ({info['size'] // 1024} KB). Emails sent from now on attach this version; "
          f"{len(info['history'])} older version(s) kept.")


def cmd_portal(_):
    """Open the local portal (the daemon hosts it; if it isn't running, host it here)."""
    import webbrowser
    import portal
    try:
        server = portal.make_server()
    except OSError:                                  # port in use => the agent already serves it
        print("Opening the portal (hosted by the running agent)...")
        webbrowser.open(portal.url())
        return
    print(f"Portal: {portal.url()}\nPress Ctrl-C to stop.")
    webbrowser.open(portal.url())
    server.serve_forever()


def cmd_pause(a):
    import guard
    guard.pause(" ".join(a.reason) or "paused by user")
    print("Paused. Nothing will be sent until you run: python cli.py resume")


def cmd_resume(_):
    import guard
    guard.resume()
    print("Resumed.")


def cmd_suppress(a):
    with db.connect() as conn:
        db.suppress(conn, a.target, "added manually")
    print(f"Suppressed {a.target.lower()}")


def cmd_replies(_):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT r.*, l.name, l.org FROM replies r LEFT JOIN leads l ON l.id=r.lead_id "
            "WHERE r.classification IN ('needs_response','decline','stop_request') "
            "ORDER BY r.received_at DESC LIMIT 10"
        ).fetchall()
    for r in rows:
        t = time.strftime("%b %d %H:%M", time.localtime(r["received_at"]))
        print(f"\n[{t}] {r['classification']} — {r['name'] or r['sender']} ({r['org'] or ''})")
        print("  " + (r["body"] or r["snippet"])[:300].replace("\n", "\n  "))
        if r["suggested_reply"]:
            print("  --- suggested reply ---\n  " + r["suggested_reply"].replace("\n", "\n  "))


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

def cmd_run(a):
    import daemon
    daemon.run_forever(dry_run=a.dry_run)


def cmd_test_notify(_):
    import notify
    if not notify.APP_PATH.exists():
        print("Outreach.app is not built yet; run `python cli.py build-notifier` first. "
              "Falling back to a basic banner.")
    notify.priority_alert(
        "Reply from Test Person (Acme)",
        "This is how a reply alert looks. Click to open the thread.",
        thread_id=None, suggested_reply="Hi — thanks for getting back to me! (sample)",
    )
    print("Banner sent: look in the top-right corner. First time only, macOS asks "
          "'Outreach would like to send you notifications' — click Allow.")


def cmd_build_notifier(_):
    r = subprocess.run(["bash", str(config.BASE_DIR / "notifier" / "build.sh")])
    if r.returncode:
        sys.exit("Build failed (needs Xcode command line tools: xcode-select --install).")
    print("Now run: python cli.py test-notify")


PLIST_NAME = "com.ridwan.outreach.plist"
PLIST_DEST = Path.home() / "Library" / "LaunchAgents" / PLIST_NAME


def cmd_install_daemon(_):
    text = (config.BASE_DIR / PLIST_NAME).read_text()
    text = text.replace("__PROJECT_DIR__", str(config.BASE_DIR)).replace("__DATA_DIR__", str(config.DATA_DIR))
    PLIST_DEST.parent.mkdir(parents=True, exist_ok=True)
    PLIST_DEST.write_text(text)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST_DEST)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_DEST)], check=True)
    print(f"Installed. The agent now starts at login. Logs: {config.LOG_PATH}")


def cmd_uninstall_daemon(_):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST_DEST)], capture_output=True)
    PLIST_DEST.unlink(missing_ok=True)
    print("Removed.")


def main():
    p = argparse.ArgumentParser(prog="outreach", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *args, **kw):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        for a in args:
            sp.add_argument(a[0], **a[1])
        return sp

    add("init", cmd_init)
    add("set-api-key", cmd_set_api_key)
    add("set-sender", cmd_set_sender, ("address", {}))
    add("auth", cmd_auth)
    add("import-samples", cmd_import_samples, ("paths", {"nargs": "+"}))
    add("build-style", cmd_build_style)
    add("add-leads", cmd_add_leads, ("campaign", {}), ("csv", {}))
    add("preview", cmd_preview, ("campaign", {}), ("n", {"nargs": "?", "type": int, "default": 3}))
    add("status", cmd_status)
    i = add("intake", cmd_intake)
    i.add_argument("--dry-run", action="store_true", help="show what would be added; save nothing")
    i.add_argument("--limit", type=int, default=None, help="max posts to analyse (cost control)")
    i.add_argument("--recheck", action="store_true",
                   help="re-judge leads already queued under the current rules; remove any that fail")
    add("set-resume", cmd_set_resume, ("pdf", {}))
    add("portal", cmd_portal)
    add("pause", cmd_pause, ("reason", {"nargs": "*"}))
    add("resume", cmd_resume)
    add("suppress", cmd_suppress, ("target", {}))
    add("replies", cmd_replies)
    r = add("run", cmd_run)
    r.add_argument("--dry-run", action="store_true", help="print emails instead of sending")
    add("test-notify", cmd_test_notify)
    add("build-notifier", cmd_build_notifier)
    add("install-daemon", cmd_install_daemon)
    add("uninstall-daemon", cmd_uninstall_daemon)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
