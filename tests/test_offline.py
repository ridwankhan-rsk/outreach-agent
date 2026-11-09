"""
Offline tests: no Gmail, no Claude, no popups. Everything external is mocked, and the
database lives in a throw-away temp folder.

Run:  .venv/bin/python -m unittest discover -s tests -v
"""
import json
import mailbox
import os
import sys
import tempfile
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

# Point the app at a temp data dir BEFORE importing anything that reads config.
_TMP = tempfile.mkdtemp()
os.environ["OUTREACH_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config            # noqa: E402
import db                # noqa: E402
import engine            # noqa: E402
import guard             # noqa: E402
import import_samples    # noqa: E402
import replies           # noqa: E402

PROFILE = {"name": "Test User", "first_name": "Test", "email_signature": "Best,\nTest"}
GOOD = {"subject": "Hello", "body": " ".join(["word"] * 90) + "\n\nBest,\nTest", "facts_used": []}


def fresh_db():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    config.PAUSE_FILE.unlink(missing_ok=True)


def add(conn, email, campaign="recruiters", **extra):
    row = {"email": email, "name": "Pat", "org": "Acme", **extra}
    ok, why = db.add_lead(conn, campaign, row)
    return ok, why


class ImporterTests(unittest.TestCase):
    def test_cleans_and_masks(self):
        raw = ("Hi Sam,\nThanks for the intro. I'd love to chat about the role; my number is "
               "404-555-1212 and my email is me@example.com. I am free next week to talk through "
               "the team and what you are working on right now.\n\nBest,\nSam\n"
               "On Mon, Jan 1, 2026 at 9:00 AM Sam <sam@x.com> wrote:\n> old quoted text")
        body = import_samples.mask_private(
            import_samples.trim_signature_block(import_samples.strip_quotes_and_signature(raw)))
        self.assertNotIn("old quoted", body)
        self.assertNotIn("404-555", body)
        self.assertNotIn("me@example.com", body)
        self.assertIn("[email]", body)

    def test_mbox_eml_and_graph(self):
        d = Path(tempfile.mkdtemp())
        text = ("Hi there,\n" + "I wanted to follow up on our conversation about the research role. " * 4
                + "\n\nBest,\nR")
        # mbox
        box = mailbox.mbox(str(d / "sent.mbox"))
        m = EmailMessage(); m["Subject"] = "Research role"; m["To"] = "a@b.com"; m.set_content(text)
        box.add(m); box.flush()
        # eml (different text so it isn't deduped)
        e = EmailMessage(); e["Subject"] = "Another"; e.set_content(text.replace("research", "lab"))
        (d / "one.eml").write_bytes(e.as_bytes())
        # graph json
        (d / "g.json").write_text(json.dumps({"value": [{
            "subject": "Hello", "body": {"contentType": "html",
            "content": "<p>" + text.replace("conversation", "call") + "</p>"}}]}))
        # Redirect output to a temp file so a test run can never touch your real samples.
        fake_samples = Path(tempfile.mkdtemp()) / "samples.jsonl"
        with mock.patch.object(config, "SAMPLES_PATH", fake_samples):
            added = import_samples.run([str(d)])
        self.assertEqual(added, 3)
        rows = [json.loads(l) for l in fake_samples.read_text().splitlines()]
        self.assertTrue(all(set(r) == {"id", "register", "subject", "body"} for r in rows))


class StructuredOutputTests(unittest.TestCase):
    """_call_tool must retry when the model returns prose or a half-filled tool call."""

    @staticmethod
    def _resp(*blocks):
        return mock.Mock(content=list(blocks))

    @staticmethod
    def _tool_block(data):
        return mock.Mock(type="tool_use", input=data)

    def test_retries_on_missing_fields_and_prose(self):
        import voice
        tool = voice.CHECK_TOOL                       # requires ok + problems
        client = mock.Mock()
        client.messages.create.side_effect = [
            self._resp(mock.Mock(type="text")),                       # prose, no tool call
            self._resp(self._tool_block({"problems": []})),           # missing "ok"
            self._resp(self._tool_block({"ok": True, "problems": []})),
        ]
        with mock.patch.object(voice, "_client", return_value=client):
            out = voice._call_tool("m", [], "hi", tool)
        self.assertEqual(out, {"ok": True, "problems": []})
        self.assertEqual(client.messages.create.call_count, 3)

    def test_gives_up_after_three_bad_answers(self):
        import voice
        client = mock.Mock()
        client.messages.create.return_value = self._resp(self._tool_block({}))
        with mock.patch.object(voice, "_client", return_value=client):
            with self.assertRaises(RuntimeError):
                voice._call_tool("m", [], "hi", voice.CHECK_TOOL)

    def test_missing_verdict_never_counts_as_pass(self):
        camp = {"length_words": [70, 130]}
        with mock.patch("voice.fact_check", return_value={"problems": []}):    # no "ok" key
            ok, why = guard.draft_checks(camp, {}, GOOD)
        self.assertFalse(ok)


class DbTests(unittest.TestCase):
    def setUp(self):
        fresh_db()

    def test_dedupe_and_suppression(self):
        with db.connect() as c:
            self.assertTrue(add(c, "A@x.com")[0])
            self.assertEqual(add(c, "a@x.com"), (False, "already queued"))   # case-insensitive
            db.suppress(c, "@blocked.com", "test")
            self.assertEqual(add(c, "z@blocked.com"), (False, "on suppression list"))

    def test_recontact_cooldown(self):
        with db.connect() as c:
            add(c, "a@x.com")
            c.execute("UPDATE leads SET status='sent'")
            c.execute("INSERT INTO sends(lead_id,kind,sent_at) VALUES(1,'initial',?)", (db.now(),))
            self.assertFalse(add(c, "a@x.com")[0])                            # just contacted
            c.execute("UPDATE sends SET sent_at=?", (db.now() - 100 * 86400,))
            self.assertTrue(add(c, "a@x.com")[0])                             # >90 days ago


class GuardTests(unittest.TestCase):
    def setUp(self):
        fresh_db()
        self.camp = {"name": "recruiters", "required_fields": ["name", "org"], "daily_cap": 5}

    def lead(self, c, i=1):
        return c.execute("SELECT * FROM leads WHERE id=?", (i,)).fetchone()

    def test_missing_fields_blocks(self):
        with db.connect() as c:
            db.add_lead(c, "recruiters", {"email": "a@x.com", "name": "Pat"})   # no org
            with mock.patch.object(guard, "in_send_window", return_value=True):
                ok, why = guard.pre_send_checks(c, self.camp, self.lead(c))
        self.assertFalse(ok)
        self.assertIn("missing required fields", why)

    def test_window_and_daily_cap(self):
        with db.connect() as c:
            add(c, "a@x.com")
            lead = self.lead(c)
            with mock.patch.object(guard, "in_send_window", return_value=False):
                self.assertEqual(guard.pre_send_checks(c, self.camp, lead)[1], "later: outside send hours")
            with mock.patch.object(guard, "in_send_window", return_value=True):
                self.assertTrue(guard.pre_send_checks(c, self.camp, lead)[0])
                for _ in range(config.RAMP_SCHEDULE[0][1]):                     # fill day-1 cap
                    c.execute("INSERT INTO sends(lead_id,kind,sent_at) VALUES(1,'initial',?)", (db.now(),))
                self.assertEqual(guard.pre_send_checks(c, self.camp, lead)[1], "later: daily cap reached")

    def test_ramp_grows(self):
        caps = dict(config.RAMP_SCHEDULE)           # {start day: cap}, so the test follows config
        with db.connect() as c:
            self.assertEqual(guard.daily_limit(c), caps[0])
            db.set_state(c, "first_send_at", str(db.now() - 15 * 86400))
            self.assertEqual(guard.daily_limit(c), caps[14])        # day 15 is in the 14+ tier
            db.set_state(c, "first_send_at", str(db.now() - 40 * 86400))
            self.assertEqual(guard.daily_limit(c), max(caps.values()))

    def test_org_cooldown(self):
        with db.connect() as c:
            add(c, "a@x.com", org="Acme Inc"); add(c, "b@x.com", org="ACME, inc.")
            c.execute("INSERT INTO sends(lead_id,kind,sent_at) VALUES(1,'initial',?)", (db.now(),))
            with mock.patch.object(guard, "in_send_window", return_value=True):
                ok, why = guard.pre_send_checks(c, self.camp, self.lead(c, 2))
        self.assertFalse(ok)
        self.assertIn("another person at this org", why)

    def test_draft_checks(self):
        camp = {"length_words": [70, 130]}
        with mock.patch("voice.fact_check", return_value={"ok": True, "problems": []}):
            self.assertTrue(guard.draft_checks(camp, {}, GOOD)[0])
            bad = dict(GOOD, body=GOOD["body"] + " [Company]")
            self.assertEqual(guard.draft_checks(camp, {}, bad), (False, "contains a placeholder"))
            optout = dict(GOOD, body=GOOD["body"] + " Reply to unsubscribe.")
            self.assertFalse(guard.draft_checks(camp, {}, optout)[0])
        with mock.patch("voice.fact_check", return_value={"ok": False, "problems": ["invented paper"]}):
            ok, why = guard.draft_checks(camp, {}, GOOD)
            self.assertFalse(ok)
            self.assertIn("invented paper", why)


class InferredClaimTests(unittest.TestCase):
    """Static checks for the mistakes found in live drafts (run with a passing fact-check)."""
    camp = {"length_words": [10, 300]}
    pad = " ".join(["detail"] * 20)

    def check(self, text, lead=None):
        body = f"Dear Maya,\n\n{text} {self.pad}.\n\nBest,\nSam Lee"
        with mock.patch("voice.fact_check", return_value={"ok": True, "problems": []}):
            return guard.draft_checks(self.camp, lead or {}, {"subject": "Hi", "body": body})

    def test_blocks_each_known_mistake(self):
        self.assertFalse(self.check("I am a computer engineering sophomore.")[0])
        self.assertFalse(self.check("Last summer I interned at Initech.")[0])
        self.assertFalse(self.check("I recently built a pipeline.")[0])
        ok, why = self.check("Hello Ms. Patel, I am writing.")
        self.assertFalse(ok); self.assertIn("Mr./Ms.", why)

    def test_double_signoff_blocked(self):
        body = ("Dear Maya,\n\n" + " ".join(["word"] * 40) +
                ".\n\nThank you for your consideration,\n\nBest,\nSam")
        with mock.patch("voice.fact_check", return_value={"ok": True, "problems": []}):
            ok, why = guard.draft_checks(self.camp, {}, {"subject": "Hi", "body": body})
        self.assertFalse(ok); self.assertIn("double sign-off", why)

    def test_words_from_lead_data_are_allowed(self):
        lead = {"role": "Senior Recruiter", "note": "recently launched a new program"}
        self.assertTrue(self.check("I saw you are a Senior Recruiter who recently launched it.", lead)[0])
        self.assertTrue(self.check("Dear Professor Lee, hello.", {"title": "Professor"})[0])

    def test_clean_email_passes(self):
        self.assertTrue(self.check("I am a State University student in the class of 2028; in summer 2025 I interned.")[0])


class EngineTests(unittest.TestCase):
    def setUp(self):
        fresh_db()
        self.sent = []

        def fake_send(**kw):
            self.sent.append(kw)
            return {"id": f"m{len(self.sent)}", "threadId": f"t{len(self.sent)}"}

        camps = {"recruiters": {"name": "recruiters", "required_fields": ["name", "org"],
                                "daily_cap": 5, "followups": 1, "followup_after_days": 10,
                                "length_words": [70, 130], "goal": "g", "audience": "a"}}
        self.patches = [
            mock.patch.object(guard, "in_send_window", return_value=True),
            mock.patch.object(guard, "load_campaigns", return_value=camps),
            mock.patch("voice.load_profile", return_value=PROFILE),
            mock.patch("voice.generate_email", return_value=GOOD),
            mock.patch("voice.generate_followup", return_value=dict(GOOD, body="Just following up. " * 4)),
            mock.patch("voice.fact_check", return_value={"ok": True, "problems": []}),
            mock.patch("gmail_client.send_email", side_effect=fake_send),
            mock.patch("gmail_client.rfc_message_id", return_value="<id@x>"),
            mock.patch("notify.priority_alert"),
            mock.patch("notify.quiet_notice"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(mock.patch.stopall)

    def test_sends_one_then_waits(self):
        with db.connect() as c:
            add(c, "a@x.com", org="One"); add(c, "b@x.com", org="Two")
        self.assertEqual(engine.send_cycle(), "sent to a@x.com")
        self.assertEqual(engine.send_cycle(), "waiting for next send slot")   # random gap enforced
        self.assertEqual(len(self.sent), 1)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM leads WHERE id=1").fetchone()[0], "sent")

    def test_pause_stops_everything(self):
        with db.connect() as c:
            add(c, "a@x.com")
        guard.pause("test")
        self.assertTrue(engine.send_cycle().startswith("paused"))
        self.assertEqual(self.sent, [])

    def test_missing_field_is_blocked_not_sent(self):
        with db.connect() as c:
            db.add_lead(c, "recruiters", {"email": "a@x.com", "name": "Pat"})   # no org
        engine.send_cycle()
        self.assertEqual(self.sent, [])
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM leads").fetchone()[0], "blocked")

    def test_retry_fixes_a_rejected_draft(self):
        """First draft fails a check; the retry gets the reason as feedback and passes."""
        with db.connect() as c:
            add(c, "a@x.com")
        bad = dict(GOOD, body=GOOD["body"] + " I am a sophomore.")
        calls = []

        def fake_generate(campaign, data, feedback=""):
            calls.append(feedback)
            return bad if not feedback else GOOD

        with mock.patch("voice.generate_email", side_effect=fake_generate):
            self.assertEqual(engine.send_cycle(), "sent to a@x.com")
        self.assertEqual(len(calls), 2)
        self.assertIn("class standing", calls[1])             # the reason was passed back

    def test_signoff_must_match_profile_exactly(self):
        """A blank line after 'Best,' (seen live) is rejected, and the retry fixes it."""
        with db.connect() as c:
            add(c, "a@x.com")
        drifted = dict(GOOD, body=GOOD["body"].replace("Best,\nTest", "Best,\n\nTest"))
        calls = []

        def fake_generate(campaign, data, feedback=""):
            calls.append(feedback)
            return drifted if not feedback else GOOD

        with mock.patch("voice.generate_email", side_effect=fake_generate):
            self.assertEqual(engine.send_cycle(), "sent to a@x.com")
        self.assertEqual(len(calls), 2)
        self.assertIn("sign-off exactly", calls[1])
        self.assertTrue(self.sent[0]["body"].endswith("Best,\nTest"))

    def test_failed_factcheck_blocks(self):
        with db.connect() as c:
            add(c, "a@x.com")
        with mock.patch("voice.fact_check", return_value={"ok": False, "problems": ["made up"]}):
            engine.send_cycle()
        self.assertEqual(self.sent, [])

    def test_followup_in_same_thread_after_delay(self):
        with db.connect() as c:
            add(c, "a@x.com")
        engine.send_cycle()
        with db.connect() as c:
            db.set_state(c, "next_send_at", "0")
            c.execute("UPDATE sends SET sent_at=?", (db.now() - 11 * 86400,))   # 11 days ago
        self.assertEqual(engine.send_cycle(), "sent follow-up")
        self.assertEqual(self.sent[-1]["thread_id"], "t1")                      # same conversation
        self.assertEqual(self.sent[-1]["in_reply_to"], "<id@x>")
        with db.connect() as c:
            db.set_state(c, "next_send_at", "0")
        engine.send_cycle()
        self.assertEqual(len(self.sent), 2)                                     # never a second follow-up


class ReplyTests(unittest.TestCase):
    def setUp(self):
        fresh_db()
        with db.connect() as c:
            add(c, "pat@acme.com")
            c.execute("UPDATE leads SET status='sent'")
            c.execute("INSERT INTO sends(lead_id,kind,thread_id,message_id,body,sent_at) "
                      "VALUES(1,'initial','T1','M1','hello',?)", (db.now(),))

    def run_poll(self, msg, category=None):
        full = {"id": "g1", "threadId": "T1", "sender": "Pat <pat@acme.com>", "subject": "Re: Hello",
                "auto": False, "snippet": "s", "body": "text", "internal_ts": db.now()}
        full.update(msg)
        with mock.patch("gmail_client.recent_inbound", return_value=[{"id": full["id"], "threadId": full["threadId"]}]), \
             mock.patch("gmail_client.fetch_message", return_value=full), \
             mock.patch("voice.classify_reply", return_value={"category": category, "reason": "r"}), \
             mock.patch("voice.suggest_reply", return_value="Thanks!"), \
             mock.patch("notify.priority_alert") as alert, \
             mock.patch("notify.quiet_notice") as quiet:
            replies.poll_once()
            return alert, quiet

    def status(self):
        with db.connect() as c:
            return c.execute("SELECT status FROM leads WHERE id=1").fetchone()[0]

    def test_needs_response_fires_priority_alert(self):
        alert, _ = self.run_poll({}, "needs_response")
        alert.assert_called_once()
        self.assertEqual(alert.call_args.kwargs["suggested_reply"], "Thanks!")
        self.assertEqual(self.status(), "replied")

    def test_stop_request_suppresses_without_alert(self):
        alert, _ = self.run_poll({"body": "please stop emailing me"}, "stop_request")
        alert.assert_not_called()
        with db.connect() as c:
            self.assertTrue(db.is_suppressed(c, "pat@acme.com"))
        self.assertEqual(self.status(), "suppressed")

    def test_decline_cancels_followup_quietly(self):
        alert, quiet = self.run_poll({}, "decline")
        alert.assert_not_called(); quiet.assert_called_once()
        self.assertEqual(self.status(), "declined")

    def test_auto_reply_ignored(self):
        alert, quiet = self.run_poll({"auto": True}, "needs_response")
        alert.assert_not_called(); quiet.assert_not_called()
        self.assertEqual(self.status(), "sent")

    def test_bounce_suppresses(self):
        alert, _ = self.run_poll({"id": "b1", "threadId": "OTHER", "sender": "MAILER-DAEMON@google.com",
                                  "body": "Delivery to pat@acme.com failed"})
        self.assertEqual(self.status(), "bounced")
        with db.connect() as c:
            self.assertTrue(db.is_suppressed(c, "pat@acme.com"))

    def test_message_processed_only_once(self):
        self.run_poll({}, "needs_response")
        alert, _ = self.run_poll({}, "needs_response")
        alert.assert_not_called()

    def test_banner_says_to_attach_resume_when_suggestion_mentions_it(self):
        with mock.patch("replies._suggest", return_value="I have attached my resume."):
            alert, _ = self.run_poll({}, "needs_response")
        self.assertIn("Attach your resume", alert.call_args[0][1])


# --------------------------------------------------------------------------- #
# Resume attachment + portal
# --------------------------------------------------------------------------- #

import resume  # noqa: E402
import portal  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PDF2 = PDF + b"% second version\n%%EOF\n"


def clear_resume():
    import shutil
    shutil.rmtree(config.DATA_DIR / "resume", ignore_errors=True)


class QuoteStrippingTests(unittest.TestCase):
    def test_header_wrapped_over_two_lines(self):      # seen in a real Gmail reply
        import gmail_client
        t = ("thanks, can you send over a resume?\n\nOn Mon, Nov 9, 2026 at 12:28 PM Sam Lee "
             "<sender@example.com>\nwrote:\n\n> Dear Sam,\n> I am writing")
        self.assertEqual(gmail_client._strip_quoted(t), "thanks, can you send over a resume?")

    def test_single_line_header_and_plain_text_unchanged(self):
        import gmail_client
        self.assertEqual(gmail_client._strip_quoted("Yes!\n\nOn Tue, May 5, 2026, Sam <s@x.com> wrote:\n> hi"), "Yes!")
        self.assertEqual(gmail_client._strip_quoted("On Monday I can meet.\nSee you then."),
                         "On Monday I can meet.\nSee you then.")


class ResumeStoreTests(unittest.TestCase):
    def setUp(self):
        clear_resume()

    def test_rejects_bad_files(self):
        for bad, frag in [(b"", "empty"), (b"hello world", "not a PDF"),
                          (b"%PDF-1.4 but no end marker", "cut off"),
                          (PDF + b"x" * config.RESUME_MAX_BYTES, "limit")]:
            with self.assertRaises(resume.ResumeError) as cm:
                resume.save(bad)
            self.assertIn(frag, str(cm.exception))
        self.assertIsNone(resume.current_path())              # nothing half-saved

    def test_new_upload_replaces_current_and_keeps_old(self):
        resume.save(PDF)
        info = resume.save(PDF2)
        self.assertEqual(resume.current_path().read_bytes(), PDF2)
        self.assertEqual(len(info["history"]), 1)
        self.assertEqual((resume.history_dir() / info["history"][0]["name"]).read_bytes(), PDF)

    def test_same_file_twice_makes_no_history(self):
        resume.save(PDF)
        self.assertEqual(resume.save(PDF)["history"], [])

    def test_attaches_only_first_email_of_opted_in_campaign(self):
        self.assertTrue(resume.attaches({"attach_resume": True}, "initial"))
        self.assertFalse(resume.attaches({"attach_resume": True}, "followup"))
        self.assertFalse(resume.attaches({}, "initial"))

    def test_attachment_name(self):
        self.assertEqual(resume.attachment_name("Sam Lee"), "Sam_Lee_Resume.pdf")


class ResumeAttachGuardTests(unittest.TestCase):
    camp = {"name": "recruiters", "attach_resume": True, "length_words": [70, 130]}

    def setUp(self):
        clear_resume()

    def test_draft_may_not_claim_an_attachment_that_will_not_exist(self):
        body = GOOD["body"] + " I have attached my resume."
        with mock.patch("voice.fact_check", return_value={"ok": True, "problems": []}):
            ok, why = guard.draft_checks({"length_words": [70, 130]}, {}, dict(GOOD, body=body))
            self.assertFalse(ok); self.assertIn("nothing will be attached", why)
            self.assertTrue(guard.draft_checks(self.camp, {}, dict(GOOD, body=body))[0])
            # follow-ups never attach, so they may not claim it either
            fu = dict(GOOD, body="Just following up, my resume is attached. " * 3)
            self.assertFalse(guard.draft_checks(self.camp, {}, fu, kind="followup")[0])

    def test_campaign_waits_while_no_resume_is_uploaded(self):
        fresh_db()
        with db.connect() as c:
            add(c, "a@x.com")
            lead = c.execute("SELECT * FROM leads").fetchone()
            ok, why = guard.pre_send_checks(c, self.camp, lead)
        self.assertFalse(ok); self.assertTrue(why.startswith("later: no resume"))


class ResumeEngineTests(unittest.TestCase):
    def setUp(self):
        fresh_db(); clear_resume()
        self.sent = []

        def fake_send(**kw):
            self.sent.append(kw)
            return {"id": "m1", "threadId": "t1"}

        self.camps = {"recruiters": {"name": "recruiters", "required_fields": ["name", "org"],
                                     "daily_cap": 5, "followups": 1, "followup_after_days": 10,
                                     "length_words": [70, 130], "goal": "g", "audience": "a",
                                     "attach_resume": True,
                                     "attachments": ["/etc/passwd"]}}        # must be ignored
        for p in [mock.patch.object(guard, "in_send_window", return_value=True),
                  mock.patch.object(guard, "load_campaigns", return_value=self.camps),
                  mock.patch("voice.load_profile", return_value=PROFILE),
                  mock.patch("voice.generate_email", return_value=GOOD),
                  mock.patch("voice.fact_check", return_value={"ok": True, "problems": []}),
                  mock.patch("gmail_client.send_email", side_effect=fake_send),
                  mock.patch("gmail_client.rfc_message_id", return_value="<id@x>"),
                  mock.patch("notify.priority_alert"), mock.patch("notify.quiet_notice")]:
            p.start()
        self.addCleanup(mock.patch.stopall)
        with db.connect() as c:
            add(c, "a@x.com")

    def test_does_not_send_without_a_resume(self):
        self.assertTrue(engine.send_cycle().startswith("later: no resume"))
        self.assertEqual(self.sent, [])

    def test_attaches_only_the_resume(self):
        resume.save(PDF)
        self.assertEqual(engine.send_cycle(), "sent to a@x.com")
        att = self.sent[0]["attachments"]
        self.assertEqual(len(att), 1)
        self.assertEqual(att[0][1], "Test_User_Resume.pdf")
        self.assertEqual(Path(att[0][0]), resume.current_path())
        self.assertNotIn("/etc/passwd", str(att))            # arbitrary YAML paths are ignored

    def test_followup_carries_no_attachment(self):
        resume.save(PDF)
        engine.send_cycle()
        with db.connect() as c:
            db.set_state(c, "next_send_at", "0")
            c.execute("UPDATE sends SET sent_at=?", (db.now() - 11 * 86400,))
        with mock.patch("voice.generate_followup", return_value=dict(GOOD, body="Just following up. " * 4)):
            self.assertEqual(engine.send_cycle(), "sent follow-up")
        self.assertIsNone(self.sent[-1]["attachments"])

    def test_resume_deleted_between_draft_and_send_blocks_the_send(self):
        resume.save(PDF)
        real = resume.current_path
        calls = {"n": 0}

        def vanishing():                  # present for the pre-send check, gone at send time
            calls["n"] += 1
            return real() if calls["n"] == 1 else None

        with mock.patch("resume.current_path", side_effect=vanishing):
            self.assertEqual(engine.send_cycle(), "send failed")
        self.assertEqual(self.sent, [])


class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = portal.make_server(port=0)
        cls.port = cls.srv.server_address[1]
        import threading
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.token = portal.get_token()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        fresh_db(); clear_resume()

    def req(self, method, path, body=None, headers=None, host=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        return r.status, r.read()

    def test_needs_token(self):
        self.assertEqual(self.req("GET", "/api/state")[0], 403)
        self.assertEqual(self.req("GET", "/api/state", headers={"X-Token": "wrong"})[0], 403)
        self.assertEqual(self.req("GET", f"/api/state?t={self.token}")[0], 200)

    def test_rejects_foreign_host_header(self):                 # DNS-rebinding defence
        self.assertEqual(self.req("GET", f"/?t={self.token}", host="evil.example.com")[0], 403)

    def test_upload_needs_pdf_content_type_and_valid_pdf(self):
        h = {"X-Token": self.token}
        self.assertEqual(self.req("POST", "/upload", PDF, {**h, "Content-Type": "text/plain"})[0], 415)
        status, body = self.req("POST", "/upload", b"not a pdf", {**h, "Content-Type": "application/pdf"})
        self.assertEqual(status, 400); self.assertIn("not a PDF", body.decode())
        self.assertIsNone(resume.current_path())

    def test_upload_and_download_roundtrip(self):
        h = {"X-Token": self.token, "Content-Type": "application/pdf"}
        self.assertEqual(self.req("POST", "/upload", PDF, h)[0], 200)
        self.assertEqual(self.req("POST", "/upload", PDF2, h)[0], 200)
        self.assertEqual(self.req("GET", f"/resume.pdf?t={self.token}"), (200, PDF2))
        state = json.loads(self.req("GET", f"/api/state?t={self.token}")[1])
        self.assertEqual(len(state["resume"]["history"]), 1)
        name = state["resume"]["history"][0]["name"]
        self.assertEqual(self.req("GET", f"/history/{name}?t={self.token}"), (200, PDF))
        self.assertEqual(self.req("GET", f"/history/..%2Fcurrent.pdf?t={self.token}")[0], 404)

    def test_remove_queued_lead(self):
        with db.connect() as c:
            add(c, "a@x.com")
        h = {"X-Token": self.token, "Content-Type": "application/json"}
        self.assertEqual(self.req("POST", "/remove", json.dumps({"id": 1}), {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.req("POST", "/remove", json.dumps({"id": 1}), h)[0], 200)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM leads").fetchone()[0], "removed")
            self.assertEqual(db.add_lead(c, "recruiters", {"email": "a@x.com", "name": "P", "org": "A"}),
                             (False, "already removed"))               # cannot sneak back in
        # a lead that was already emailed cannot be "removed" (nothing to take back)
        with db.connect() as c:
            add(c, "b@x.com", org="B"); c.execute("UPDATE leads SET status='sent' WHERE email='b@x.com'")
        self.req("POST", "/remove", json.dumps({"id": 2}), h)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM leads WHERE id=2").fetchone()[0], "sent")

    def test_oversize_upload_refused(self):
        h = {"X-Token": self.token, "Content-Type": "application/pdf"}
        # claim a huge body without sending it: the server must refuse before reading anything
        h["Content-Length"] = str(config.RESUME_MAX_BYTES + 1)
        self.assertEqual(self.req("POST", "/upload", None, h)[0], 413)


# --------------------------------------------------------------------------- #
# Automatic lead intake (Hacker News)
# --------------------------------------------------------------------------- #

import intake  # noqa: E402

POST = ("Acme Health (YC W24) | Backend Engineer | Remote\n"
        "We build AI tools that summarize patient intake forms for clinics. "
        "Email jane.doe@acmehealth.com or apply at https://acmehealth.com/jobs")


def found(**kw):
    base = {"is_startup": True, "has_technical_roles": True, "company": "Acme Health",
            "contact_email": "jane.doe@acmehealth.com", "contact_name": "",
            "note": "Builds AI tools that summarize patient intake forms for clinics.",
            "mentions_interns": False, "explicitly_non_us": False}
    return {**base, **kw}


class IntakeJudgeTests(unittest.TestCase):
    def test_good_post_becomes_a_team_lead(self):
        row, why = intake.judge(POST, found())
        self.assertEqual(why, "")
        self.assertEqual(row["email"], "jane.doe@acmehealth.com")
        self.assertEqual(intake.campaign_for(row), "hiring_team")
        self.assertNotIn("jane.doe@", row["post_excerpt"])            # address never shown to the drafter
        self.assertIn("(email omitted)", row["post_excerpt"])

    def test_email_must_appear_verbatim_in_the_post(self):
        for bad in ["jane@acmehealth.com", "jobs@acmehealth.com", ""]:
            self.assertIsNone(intake.judge(POST, found(contact_email=bad))[0])

    def test_rejects_roleboxes_non_startups_and_non_technical(self):
        post = POST.replace("jane.doe@", "noreply@")
        self.assertEqual(intake.judge(post, found(contact_email="noreply@acmehealth.com"))[1],
                         "no-reply/role mailbox")
        self.assertEqual(intake.judge(POST, found(is_startup=False))[1], "not a startup")
        self.assertEqual(intake.judge(POST, found(has_technical_roles=False))[1], "no technical roles")
        self.assertEqual(intake.judge(POST, found(note="x"))[1], "no usable note")
        self.assertEqual(intake.judge(POST, found(explicitly_non_us=True))[1], "explicitly non-US")
        # a post that says nothing about location is fine: only an EXPLICIT non-US location excludes
        self.assertIsNotNone(intake.judge(POST, found(explicitly_non_us=False))[0])
        agency = POST.replace("jane.doe@", "agency@")
        self.assertEqual(intake.judge(agency, found(contact_email="agency@acmehealth.com"))[1],
                         "no-reply/role mailbox")

    def test_name_only_kept_if_stated_in_post(self):
        post = POST + "\nContact: Jane Doe"
        row, _ = intake.judge(post, found(contact_name="Jane Doe"))
        self.assertEqual(row["name"], "Jane Doe")
        self.assertEqual(intake.campaign_for(row), "founders")
        row, _ = intake.judge(POST, found(contact_name="Sam Smith"))     # invented: dropped, not fatal
        self.assertEqual(row["name"], "")
        self.assertEqual(intake.campaign_for(row), "hiring_team")

    def test_non_us_email_domains_rejected_even_if_claude_says_us(self):
        for dom in ["firm.ch", "firm.co.uk", "firm.de", "firm.ca", "firm.in"]:
            post = f"Firm | Engineer | NYC. jobs@{dom}"
            self.assertEqual(intake.judge(post, found(company="Firm", contact_email=f"jobs@{dom}"))[1],
                             "non-US email domain", dom)
        for dom in ["firm.ai", "firm.io", "firm.com", "firm.dev", "firm.co"]:     # common US-startup domains
            post = f"Firm | Engineer | NYC. jobs@{dom}"
            self.assertIsNotNone(intake.judge(post, found(company="Firm", contact_email=f"jobs@{dom}"))[0], dom)

    def test_name_must_match_the_address(self):
        """'Dylan' named in the post but the address is careers@ -> team greeting, not 'Dear Dylan'."""
        post = "BuildQ | Engineer | Dylan, founder. Apply: careers@buildq.ai"
        row, _ = intake.judge(post, found(company="BuildQ", contact_email="careers@buildq.ai",
                                          contact_name="Dylan"))
        self.assertEqual(row["name"], "")
        post = "BuildQ | Engineer | Contact Dylan Reyes dylan@buildq.ai"
        row, _ = intake.judge(post, found(company="BuildQ", contact_email="dylan@buildq.ai",
                                          contact_name="Dylan Reyes"))
        self.assertEqual(row["name"], "Dylan Reyes")

    def test_html_to_text(self):
        t = intake.to_text('A | B<p>mail <a href="https://x.com/jobs" rel="nofollow">https://x.com/j...</a> &amp; me')
        self.assertEqual(t, "A | B\nmail https://x.com/jobs & me")


class IntakeRunTests(unittest.TestCase):
    def setUp(self):
        fresh_db()
        now = int(time.time())

        def post(i, company, email=None):
            body = f"{company} | Engineer | Remote\nWe build things. Email {email or f'jobs@{company.lower()}.com'}"
            return {"id": str(i), "ts": now - i * 60, "text": body}

        self.posts = [post(1, "Alpha"), post(2, "Beta"), post(3, "Gamma")]
        self.extract_calls = 0

        def fake_extract(text):
            self.extract_calls += 1
            company = text.split(" |")[0]
            return found(company=company, contact_email=intake.EMAIL_RE.findall(text)[0],
                         note=f"{company} builds things for customers.")

        for p in [mock.patch("intake.latest_thread", return_value={"id": "9", "title": "Ask HN: Who is hiring? (X)"}),
                  mock.patch("intake.fetch_posts", side_effect=lambda _: list(self.posts)),
                  mock.patch("intake.extract", side_effect=fake_extract)]:
            p.start()
        self.addCleanup(mock.patch.stopall)

    def test_queues_leads_with_grace_window_and_source(self):
        s = intake.run_once()
        self.assertEqual(s["added"], 3)
        with db.connect() as c:
            rows = c.execute("SELECT * FROM leads").fetchall()
            self.assertEqual({r["campaign"] for r in rows}, {"hiring_team"})
            self.assertTrue(all(r["source"] == "hn-hiring" for r in rows))
            self.assertTrue(all(r["eligible_at"] >= db.now() + config.INTAKE_GRACE_HOURS * 3600 - 5 for r in rows))
            self.assertEqual(json.loads(rows[0]["data_json"])["found_via"], "Hacker News 'Who is hiring?' thread")

    def test_each_post_judged_only_once(self):
        intake.run_once()
        calls = self.extract_calls
        self.assertEqual(intake.run_once()["added"], 0)
        self.assertEqual(self.extract_calls, calls)               # no repeat Claude calls

    def test_dry_run_saves_nothing(self):
        s = intake.run_once(dry_run=True)
        self.assertEqual(s["added"], 3)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM intake_seen").fetchone()[0], 0)

    def test_daily_and_queue_caps(self):
        with mock.patch.object(config, "INTAKE_MAX_PER_DAY", 2):
            self.assertEqual(intake.run_once()["added"], 2)
            self.assertEqual(intake.run_once()["added"], 0)        # today's allowance used up
        fresh_db()
        with mock.patch.object(config, "INTAKE_QUEUE_MAX", 1):
            self.assertEqual(intake.run_once()["added"], 1)

    def test_same_company_twice_and_unusable_posts_skipped(self):
        self.posts.append({"id": "4", "ts": int(time.time()), "text": "Alpha | Designer | alt@alpha.com"})
        self.posts.append({"id": "5", "ts": int(time.time()), "text": "Delta | no email here at all"})
        self.posts.append({"id": "6", "ts": 1000, "text": "Old | jobs@old.com"})
        s = intake.run_once()
        self.assertEqual(s["added"], 3)
        self.assertEqual(s["skipped"]["company already queued/contacted"], 1)
        self.assertEqual(s["skipped"]["no email in post"], 1)
        self.assertEqual(s["skipped"]["too old"], 1)

    def test_suppressed_address_is_not_added(self):
        with db.connect() as c:
            db.suppress(c, "jobs@alpha.com", "asked to stop")
        s = intake.run_once()
        self.assertEqual(s["added"], 2)
        self.assertEqual(s["skipped"]["on suppression list"], 1)

    def test_extraction_error_is_retried_next_run(self):
        with mock.patch("intake.extract", side_effect=RuntimeError("api down")):
            self.assertEqual(intake.run_once()["added"], 0)
        self.assertEqual(intake.run_once()["added"], 3)           # not marked seen, so tried again


class RecheckQueueTests(unittest.TestCase):
    """After the rules get stricter, already-queued leads are re-judged and the failures removed."""
    def setUp(self):
        fresh_db()
        with db.connect() as c:
            for i, org in enumerate(["Alpha", "Beta"], 1):
                db.add_lead(c, "hiring_team", {"email": f"jobs@{org.lower()}.com", "org": org,
                            "note": "does things for people", "source_url": f"https://news.ycombinator.com/item?id={i}"},
                            source="hn-hiring", delay_seconds=3600)
            db.add_lead(c, "recruiters", {"email": "manual@x.com", "name": "M", "org": "Manual"})   # not auto-found
            db.add_lead(c, "hiring_team", {"email": "jobs@sent.com", "org": "Sent", "note": "x",
                        "source_url": "https://news.ycombinator.com/item?id=9"}, source="hn-hiring")
            c.execute("UPDATE leads SET status='sent' WHERE org='Sent'")

    def test_removes_leads_that_fail_and_keeps_the_rest(self):
        texts = {"1": "Alpha | NYC. jobs@alpha.com", "2": "Beta | Zurich. jobs@beta.com"}

        def fake_extract(text):
            company = text.split(" |")[0]
            return found(company=company, contact_email=intake.EMAIL_RE.findall(text)[0],
                         explicitly_non_us=company == "Beta")

        with mock.patch("intake.fetch_post_text", side_effect=lambda pid: texts[pid]), \
             mock.patch("intake.extract", side_effect=fake_extract):
            result = {r["org"]: r for r in intake.recheck_queue()}
        self.assertTrue(result["Alpha"]["kept"]); self.assertFalse(result["Beta"]["kept"])
        self.assertEqual(result["Beta"]["reason"], "explicitly non-US")
        with db.connect() as c:
            status = {r["org"]: r["status"] for r in c.execute("SELECT org, status FROM leads")}
        self.assertEqual(status, {"Alpha": "new", "Beta": "removed", "Manual": "new", "Sent": "sent"})

    def test_loosened_rule_restores_recheck_removals_but_not_portal_removals(self):
        with db.connect() as c:
            c.execute("UPDATE leads SET status='removed', status_reason='recheck: not clearly US-based' "
                      "WHERE org='Alpha'")
            c.execute("UPDATE leads SET status='removed', status_reason='removed in portal' WHERE org='Beta'")
        texts = {"1": "Alpha | jobs@alpha.com", "2": "Beta | jobs@beta.com"}
        with mock.patch("intake.fetch_post_text", side_effect=lambda pid: texts[pid]), \
             mock.patch("intake.extract", side_effect=lambda t: found(
                 company=t.split(" |")[0], contact_email=intake.EMAIL_RE.findall(t)[0])):
            result = {r["org"]: r for r in intake.recheck_queue()}
        self.assertTrue(result["Alpha"]["restored"]); self.assertNotIn("Beta", result)   # Beta never even looked at
        with db.connect() as c:
            status = {r["org"]: r["status"] for r in c.execute("SELECT org, status FROM leads")}
        self.assertEqual((status["Alpha"], status["Beta"]), ("new", "removed"))

    def test_network_failure_keeps_the_lead(self):
        with mock.patch("intake.fetch_post_text", side_effect=OSError("offline")):
            result = intake.recheck_queue()
        self.assertTrue(all(r["kept"] for r in result))
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM leads WHERE status='removed'").fetchone()[0], 0)


class GraceWindowTests(unittest.TestCase):
    def setUp(self):
        fresh_db(); clear_resume()
        self.sent = []
        camps = {"hiring_team": {"name": "hiring_team", "required_fields": ["org", "note"], "daily_cap": 5,
                                 "followups": 0, "length_words": [70, 130], "goal": "g", "audience": "a"}}
        for p in [mock.patch.object(guard, "in_send_window", return_value=True),
                  mock.patch.object(guard, "load_campaigns", return_value=camps),
                  mock.patch("voice.load_profile", return_value=PROFILE),
                  mock.patch("voice.generate_email", return_value=GOOD),
                  mock.patch("voice.fact_check", return_value={"ok": True, "problems": []}),
                  mock.patch("gmail_client.send_email",
                             side_effect=lambda **kw: self.sent.append(kw) or {"id": "m", "threadId": "t"}),
                  mock.patch("notify.priority_alert"), mock.patch("notify.quiet_notice")]:
            p.start()
        self.addCleanup(mock.patch.stopall)

    def test_lead_is_not_sent_until_grace_window_ends(self):
        with db.connect() as c:
            db.add_lead(c, "hiring_team", {"email": "jobs@a.com", "org": "A", "note": "does things"},
                        source="hn-hiring", delay_seconds=3600)
        self.assertEqual(engine.send_cycle(), "nothing eligible to send")
        self.assertEqual(self.sent, [])
        with db.connect() as c:
            c.execute("UPDATE leads SET eligible_at=?", (db.now() - 1,))
        self.assertEqual(engine.send_cycle(), "sent to jobs@a.com")

    def test_old_database_gets_new_columns(self):
        import sqlite3
        config.DB_PATH.unlink(missing_ok=True)
        old = sqlite3.connect(config.DB_PATH)
        old.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, campaign TEXT NOT NULL, "
                    "email TEXT NOT NULL, name TEXT, org TEXT, org_key TEXT, data_json TEXT NOT NULL, "
                    "status TEXT NOT NULL DEFAULT 'new', status_reason TEXT, added_at INTEGER NOT NULL)")
        old.execute("INSERT INTO leads(campaign,email,data_json,added_at) VALUES('r','a@x.com','{}',1)")
        old.commit(); old.close()
        with db.connect() as c:
            row = c.execute("SELECT source, eligible_at FROM leads").fetchone()
        self.assertEqual((row["source"], row["eligible_at"]), ("manual", 0))     # old leads stay sendable


if __name__ == "__main__":
    unittest.main()
