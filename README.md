# Outreach Agent

An always-on agent for job-search outreach. It writes professional emails **in your own voice**,
sends them automatically from a dedicated Gmail account, attaches your resume, finds new leads on
its own, and pops a macOS banner when someone replies.

One engine, many **campaigns** (recruiters, founders, professors, hiring teams, …), each a small
YAML file in `campaigns/`. Adding a new use case needs no code changes.

> **There is no approval step.** Once the launch agent is installed, emails go out on their own.
> Safety is enforced in code (see below), not by review. Read the previews first.

## How it works

```
leads (CSV, or found automatically on Hacker News "Who is hiring?")
   → guard.py   caps, hours, cooldowns, dedupe, suppression, required fields
   → voice.py   Claude writes the email from YOUR facts + style samples
   → guard.py   static checks + a second Claude fact-check pass
   → gmail      sends it (resume attached on a campaign's first email)
   → replies    polled every minute, classified, banner for ones that need you
```

- **No fine-tuning.** Voice comes from few-shot prompting: a curated set of your real sent emails
  plus a generated style guide. Only a handful of examples are ever sent to the API per request.
- **Facts only from you.** `profile.yaml` is the only source of facts about you; the lead's row is
  the only source about them. The fact-check pass blocks anything else.
- **State** lives in SQLite in `~/.outreach-agent/`, outside the project. Secrets (Gmail token,
  Anthropic API key) live in the macOS Keychain.

## Requirements
macOS, Python 3.12+, an Anthropic API key, a Google account to send from, Xcode command line tools
(only to build the notification helper).

## Setup (one time)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python cli.py init                      # creates ~/.outreach-agent and profile.yaml
.venv/bin/python cli.py set-api-key               # Anthropic API key -> macOS Keychain
.venv/bin/python cli.py set-sender you@gmail.com  # address emails are sent from (kept out of the code)
.venv/bin/python cli.py build-notifier            # builds the small Outreach.app banner helper
```

1. **Fill in `profile.yaml`** (copied from `profile.example.yaml`; git-ignored). The agent refuses
   to run while any `TODO` is left.
2. **Gmail access** (~10 minutes in a browser):
   - console.cloud.google.com → new project → enable the **Gmail API**.
   - *Google Auth Platform* → External; add your sender address as a **test user**; add the
     scopes `gmail.send` and `gmail.readonly`.
   - *Credentials* → Create → **OAuth client ID → Desktop app** → download the JSON → save it as
     `~/.outreach-agent/credentials.json`.
   - `cli.py auth`, sign in as the sender, click through the "unverified app" notice (it is your own app).
   - **Login expiry:** while the app's publishing status is *Testing*, Google expires the login every
     7 days. Publish the app to *In production* (it can stay unverified for personal use) to avoid that.
3. **Teach it your voice.** Export your sent mail (Gmail Takeout → *Sent* label → `.mbox`, or Outlook
   `.eml`/Graph JSON), then:
   ```bash
   .venv/bin/python cli.py import-samples ~/Downloads/Sent.mbox
   # open voice/samples.jsonl and delete anything you do not want used
   .venv/bin/python cli.py build-style      # writes voice/style_guide.md; edit it freely
   ```
4. **Upload your resume** with `cli.py set-resume resume.pdf` or in the portal (below).
5. **Allow banners:** System Settings → Notifications → *Outreach* → Allow (style *Alerts* keeps
   them on screen). If you use a Focus mode, allow *Outreach* through it.

## Daily use

```bash
.venv/bin/python cli.py add-leads recruiters leads/my_recruiters.csv   # columns: see campaigns/*.yaml
.venv/bin/python cli.py preview recruiters 3     # writes 3 sample emails, sends NOTHING
.venv/bin/python cli.py run --dry-run            # full loop, prints instead of sending
.venv/bin/python cli.py install-daemon           # starts at login, restarts on crash
.venv/bin/python cli.py status                   # counts, caps, blocked leads, resume
.venv/bin/python cli.py portal                   # local web page (resume upload, queue, replies)
.venv/bin/python cli.py pause                    # kill switch (resume to undo)
.venv/bin/python cli.py replies                  # recent replies + suggested responses
.venv/bin/python cli.py suppress someone@x.com   # never contact (or @domain.com)
```

### Resume
The only file the agent can ever attach. A campaign opts in with `attach_resume: true`; only the
**first** email carries it. Upload new versions any time (`set-resume` or the portal): later emails
use the new one, old versions are kept. If a campaign needs a resume and none is uploaded, it
does not send.

### Automatic lead intake (Hacker News "Who is hiring?")
A background job reads the newest thread through the public HN API, has Claude extract each post,
then **re-checks everything in code** before queueing: the contact email must appear word for word
in the post, role/no-reply mailboxes and non-US country domains are dropped, posts that explicitly
place the company outside the US are skipped, and a person is greeted by name only if the name
matches the address. New leads wait a random 1-2 hours (`INTAKE_GRACE_HOURS_MIN/MAX`) before their first email, so
you can delete one in the portal. `cli.py intake --dry-run` previews without saving.

### Portal
`cli.py portal` opens a page on `127.0.0.1` only: upload a resume, see status, the queue (with a
Remove button), and recent replies. It needs a private token (the command opens it for you),
checks the Host header, and requires a custom header for uploads.

## Safety rules enforced in code (`guard.py`, `config.py`)
- Weekdays 9:00–17:00 local only; random 8–25 minute gaps between sends.
- Daily cap ramps up over four weeks; weekly cap; per-campaign daily caps.
- Leads missing a campaign's required fields are blocked, never guessed.
- Static checks (placeholders, guessed titles like "Mr./Ms.", relative time like "last summer",
  inferred class standing, doubled sign-offs, wrong sign-off, claims of an attachment that is not
  attached) plus a Claude fact-check pass.
- No duplicates, 30-day per-organization cooldown, 90-day re-contact cooldown, suppression list.
- A reply that says stop/unsubscribe suppresses that person automatically. Emails contain no
  opt-out text.
- Bounces suppress the address; 3 bounces in the last 10 sends, or any Gmail error, auto-pauses
  everything and alerts you.
- Every sent email is stored in full (audit log).

## Adding a campaign
Copy a file in `campaigns/`, rename it, and edit `audience`, `goal`, `required_fields`,
`instructions`, `daily_cap`, `attach_resume`, `followups`. No code changes.

## Known limits
- Runs only while your Mac is on and logged in; replies are noticed when it is back.
- Gmail only (Microsoft Graph sending is not built).
- Founder DMs on LinkedIn/X are not automated. Scraping LinkedIn/Indeed is intentionally not
  supported (it violates their terms).
- This sends cold email to real people. Keep volume modest, send only things that are true, and
  follow the laws and mail-provider rules that apply to you. The guardrails reduce risk; they do
  not remove it.

## Tests
```bash
.venv/bin/python tests/test_offline.py     # offline: Gmail, Claude and popups are mocked
```

## Keeping private data out of git
Your profile, voice samples, style guide, contacts and resumes are git-ignored, and secrets live in
the Keychain. As a second line of defence, a pre-commit hook blocks any commit containing an email
address that isn't a known placeholder (so a real lead can't end up in a test or a doc by accident).
Enable it once per clone:

```bash
git config core.hooksPath .githooks
```

Use fictional addresses (`jane@example.com`) in tests and docs. The same check runs as a test.
