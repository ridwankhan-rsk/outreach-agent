#!/usr/bin/env python3
"""
check_emails.py — keeps real people's email addresses out of the public repo.

Every email-looking string in a tracked file must be on the allowlist below (placeholders and
made-up test addresses). A real contact, e.g. one copied from a lead, fails the check, so adding
one has to be a deliberate edit to this file rather than an accident.

    python3 scripts/check_emails.py            # check every tracked file
    python3 scripts/check_emails.py --staged   # check what is about to be committed (used by the hook)

Enable the pre-commit hook once per clone:   git config core.hooksPath .githooks
Only the Python standard library is used.
"""
from __future__ import annotations

import re
import subprocess
import sys

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")

# Domains that are fake, reserved for examples, or made up for tests (none belongs to a real lead).
ALLOWED_DOMAINS = {
    "example.com", "example.org", "example.net", "x.com", "a.com", "b.com", "acme.com",
    "alpha.com", "beta.com", "sent.com", "old.com", "blocked.com", "company.com",
    "acmehealth.com", "examplecompany.com", "examplestartup.com", "university.edu",
    "google.com",                      # MAILER-DAEMON@google.com in the bounce-handling test
}
ALLOWED_SUFFIXES = (".example", ".test", ".invalid")           # reserved by RFC 2606 / 6761
ALLOWED_ADDRESSES = {"you@gmail.com", "your.sender@gmail.com"}  # placeholders in the docs


def is_allowed(address: str, domain: str) -> bool:
    domain = domain.lower()
    return (address.lower() in ALLOWED_ADDRESSES or domain in ALLOWED_DOMAINS
            or domain.endswith(ALLOWED_SUFFIXES))


def violations(text: str) -> list[str]:
    return sorted({m.group(0) for m in EMAIL.finditer(text) if not is_allowed(m.group(0), m.group(1))})


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def main(staged: bool) -> int:
    paths = (_git("diff", "--cached", "--name-only", "--diff-filter=ACM") if staged
             else _git("ls-files")).splitlines()
    bad: dict[str, list[str]] = {}
    for path in paths:
        try:
            text = _git("show", f":{path}") if staged else open(path, encoding="utf-8").read()
        except (subprocess.CalledProcessError, UnicodeDecodeError, OSError):
            continue                                              # binary or missing: skip
        found = violations(text)
        if found:
            bad[path] = found
    if bad:
        print("Blocked: these files contain email addresses that are not on the allowlist")
        print("(use a fictional address like jane@example.com, never a real contact):\n")
        for path, found in bad.items():
            print(f"  {path}: {', '.join(found)}")
        print("\nIf an address really is a harmless placeholder, add its domain to "
              "ALLOWED_DOMAINS in scripts/check_emails.py.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(staged="--staged" in sys.argv))
