"""
voice.py — everything that talks to Claude.

Four jobs:
  1. build_style_guide()   read your sample emails, write voice/style_guide.md
  2. generate_email()      write an initial email for one lead, in your voice
  3. generate_followup()   write the single short follow-up
  4. classify_reply()      sort an incoming reply (needs response / stop / ...)
  5. suggest_reply()       draft a reply for you to send after a reply arrives
  6. fact_check()          second pass: reject any claim not backed by provided facts

HOW "TRAINING ON YOUR VOICE" WORKS
There is no model training. Each request includes (a) a style guide and (b) a few real
emails you wrote, and Claude imitates them. That is cheap, instantly editable (just edit
voice/style_guide.md), and works very well for short emails.

STRUCTURED OUTPUT
We force Claude to answer through a "tool" with a fixed JSON schema. That guarantees we
always get a clean {subject, body} back and never have to parse free-form text.
"""
from __future__ import annotations

import json
import os
import random
from datetime import date

import anthropic
import keyring
import yaml

import config
import resume

# --------------------------------------------------------------------------- #
# Client + small helpers
# --------------------------------------------------------------------------- #

def _client() -> anthropic.Anthropic:
    """API key comes from $ANTHROPIC_API_KEY, else from the macOS Keychain."""
    key = os.environ.get("ANTHROPIC_API_KEY") or keyring.get_password(
        config.KEYRING_SERVICE, "anthropic_api_key"
    )
    if not key:
        raise RuntimeError(
            "No Anthropic API key. Run: python cli.py set-api-key   (stores it in Keychain)"
        )
    return anthropic.Anthropic(api_key=key)


def _call_tool(model: str, system_blocks: list[dict], user: str, tool: dict, max_tokens=4000) -> dict:
    """
    Call Claude and get a structured answer via `tool`; return the tool's JSON input.

    Some models reject tool_choice type "tool" (forcing a specific tool), so we use "auto"
    and instead tell the model, in plain words, which tool it must call. If it still
    answers in prose, we retry once with a firmer reminder.
    """
    instruction = (
        f"\n\nRespond ONLY by calling the `{tool['name']}` tool. Do not write any other text."
    )
    required = tool["input_schema"].get("required", [])
    extra = {}
    if model == config.CHECK_MODEL:
        # The small checker model "thinks" by default, which wastes tokens (and cost) on a
        # yes/no verdict. Turn it off. (The drafting model rejects this setting, so it is
        # left on there and simply given a generous max_tokens instead.)
        extra["thinking"] = {"type": "disabled"}
    for attempt in range(3):
        resp = _client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            tools=[tool],
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": user + instruction}],
            **extra,
        )
        if resp.stop_reason == "max_tokens":
            # Cut off mid-answer (thinking ate the budget): the tool call can't be trusted.
            instruction += "\nBe brief: keep any reasoning very short, then call the tool."
            continue
        for block in resp.content:
            if block.type == "tool_use":
                # With "auto" the schema isn't enforced, so confirm every required field is
                # there. A half-filled answer (e.g. a verdict with no "ok") must never be
                # mistaken for a real result.
                if all(k in block.input for k in required):
                    return block.input
                instruction += (
                    f"\nYour last call was missing required fields: "
                    f"{[k for k in required if k not in block.input]}. Include ALL of: {required}."
                )
                break
        else:
            instruction += f"\nYou MUST call the `{tool['name']}` tool; plain text is not accepted."
    raise RuntimeError(f"Claude returned no complete structured output after 3 attempts ({tool['name']})")


def load_profile() -> dict:
    """Facts about YOU. Refuses to continue if you haven't filled it in."""
    if not config.PROFILE_PATH.exists():
        raise RuntimeError("profile.yaml missing — copy profile.example.yaml and fill it in.")
    profile = yaml.safe_load(config.PROFILE_PATH.read_text())

    # Look for "TODO" only in the VALUES (comments are ignored by the YAML parser),
    # so explanatory comments in the file can't trigger a false alarm.
    def has_todo(node) -> bool:
        if isinstance(node, str):
            return "TODO" in node
        if isinstance(node, dict):
            return any(has_todo(v) for v in node.values())
        if isinstance(node, list):
            return any(has_todo(v) for v in node)
        return False

    if has_todo(profile):
        raise RuntimeError("profile.yaml still contains TODO placeholders — fill them in first.")
    return profile


def load_samples(register: str, k: int = 6) -> list[dict]:
    """Pick up to k example emails: matching register first, then any."""
    if not config.SAMPLES_PATH.exists():
        return []
    rows = [json.loads(l) for l in config.SAMPLES_PATH.read_text().splitlines() if l.strip()]
    # Casual emails (to friends, quick notes) are never used as examples for professional
    # outreach, unless a campaign explicitly asks for a casual register.
    if register != "casual":
        rows = [r for r in rows if r.get("register") != "casual"]
    same = [r for r in rows if r.get("register") == register]
    other = [r for r in rows if r.get("register") != register]
    random.shuffle(same)
    random.shuffle(other)
    return (same + other)[:k]


def _voice_system(campaign: dict, profile: dict) -> list[dict]:
    """
    The (long) system prompt: your style guide, example emails, and your profile.
    It's marked cacheable, so repeated calls in a session are much cheaper.
    """
    style = config.STYLE_GUIDE_PATH.read_text() if config.STYLE_GUIDE_PATH.exists() else "(none yet)"
    examples = "\n\n".join(
        f"<example register='{s['register']}'>\nSubject: {s['subject']}\n\n{s['body']}\n</example>"
        for s in load_samples(campaign.get("register", "professional"))
    )
    text = f"""You write emails on behalf of {profile['name']}, imitating their personal writing voice.

<style_guide>
{style}
</style_guide>

<voice_examples>
These are real emails written by {profile['first_name']}. Match their TONE: formality,
rhythm, greetings and sign-offs. Do NOT reuse their topics, facts or sentences; the
content of your email comes only from the facts supplied in this request. The result must
read as a polished, professional email that still sounds like {profile['first_name']}.
{examples}
</voice_examples>

<sender_facts>
{yaml.safe_dump(profile, sort_keys=False)}
</sender_facts>

TODAY'S DATE: {date.today().strftime('%B %d, %Y')}

HARD RULES
- Use ONLY facts from <sender_facts> and the lead facts supplied in the request.
  Never invent projects, publications, numbers, mutual connections, or details about
  the recipient or their work. If a fact isn't given, leave it out.
- Do NOT infer anything that isn't written down. In particular:
  * Never state class standing (freshman/sophomore/junior/senior). Say "class of 2028"
    or "graduating May 2028" instead.
  * Never use relative time ("last summer", "recently", "this year", "currently" for a
    past job). Use the explicit dates from the facts (e.g. "in summer 2025"). Use
    "since August 2026" for the lab role, which is ongoing.
  * Never use Mr./Ms./Mrs./Mx. or guess anyone's gender. Address recruiters and founders by
    first name ("Dear Maya,"). Only use a title like "Professor" or "Dr." when the lead
    data supplies it.
- The sentence before the sign-off must end with a period. Do not add a closing phrase such
  as "Thank you for your time," above the sign-off; the sign-off itself is the closing.
- No placeholders like [Name] or {{{{company}}}}. The email must be ready to send.
- No unsubscribe/opt-out line. No emojis. No exclamation-mark spam.
- Sound like a real person writing one-to-one, not a template.
- End the body with the sender's sign-off exactly as in email_signature.
"""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


EMAIL_TOOL = {
    "name": "write_email",
    "description": "Return the finished email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body incl. greeting and sign-off"},
            "facts_used": {
                "type": "array", "items": {"type": "string"},
                "description": "Each specific fact from the provided data that the email relies on",
            },
        },
        "required": ["subject", "body", "facts_used"],
    },
}


# --------------------------------------------------------------------------- #
# 1. Style guide
# --------------------------------------------------------------------------- #

def build_style_guide() -> str:
    if not config.SAMPLES_PATH.exists():
        raise RuntimeError("No samples yet. Run: python cli.py import-samples <files>")
    rows = [json.loads(l) for l in config.SAMPLES_PATH.read_text().splitlines() if l.strip()]
    # Professional outreach only: drop casual emails, then take a random spread (not just the
    # first 60 lines, which may all be from one era/topic).
    rows = [r for r in rows if r.get("register") != "casual"] or rows
    picked = random.Random(0).sample(rows, min(60, len(rows)))
    sample_text = "\n\n---\n\n".join(
        f"[{r['register']}] Subject: {r['subject']}\n{r['body']}" for r in picked
    )
    resp = _client().messages.create(
        model=config.DRAFT_MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": (
            "Below are emails written by one person. Write a concise STYLE GUIDE (markdown, under "
            "400 words) for writing PROFESSIONAL emails in this person's voice.\n"
            "Focus on HOW they write, not WHAT they wrote about: the topics, events and facts in "
            "these emails are irrelevant and must not appear in the guide. Capture: the level of "
            "formality and professional courtesy (polished and respectful, but still sounding "
            "like a real person), typical greetings and sign-offs, sentence length and rhythm, "
            "how they open and close requests, punctuation habits, and words/phrases they favour "
            "or avoid. Where the samples vary in formality, describe the polished, professional "
            "end as the target. Be concrete; quote short generic phrases. Do not include any "
            "private details (names, addresses, employers, IDs).\n\n"
            + sample_text
        )}],
    )
    # The response may begin with a "thinking" block; take the actual text block.
    guide = "".join(b.text for b in resp.content if b.type == "text").strip()
    config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    config.STYLE_GUIDE_PATH.write_text(guide + "\n")
    return guide


# --------------------------------------------------------------------------- #
# 2/3. Writing emails
# --------------------------------------------------------------------------- #

def _lead_facts(lead_data: dict) -> str:
    skip = {"email", "source_url"}                 # not useful as facts to write from
    return "\n".join(f"- {k}: {v}" for k, v in lead_data.items() if k not in skip)


def _attachment_note(campaign: dict, kind: str = "initial") -> str:
    """Tell the model exactly what is (not) attached, so it never claims otherwise."""
    if resume.attaches(campaign, kind):
        return ("ATTACHMENT: my resume (a PDF) is attached to this email. Mention it once, in a "
                "short natural sentence such as \"I have attached my resume.\" Do not mention any "
                "other attachment.")
    return ("ATTACHMENT: nothing is attached to this email. Never say or imply that anything is "
            "attached.")


def generate_email(campaign: dict, lead_data: dict, feedback: str = "") -> dict:
    """
    Return {'subject','body','facts_used'} for one lead.
    `feedback` lists problems from a previous rejected draft so the retry can fix them.
    """
    profile = load_profile()
    lo, hi = campaign.get("length_words", [70, 130])
    retry_note = (
        f"\nYOUR PREVIOUS DRAFT WAS REJECTED FOR THESE PROBLEMS. Write a new version that fixes "
        f"them:\n{feedback}\n" if feedback else ""
    )
    user = f"""{retry_note}Write the first email to this person.

CAMPAIGN GOAL: {campaign['goal']}
AUDIENCE: {campaign['audience']}
LENGTH: {lo}-{hi} words in the body.
{_attachment_note(campaign)}
CAMPAIGN INSTRUCTIONS:
{campaign.get('instructions', '')}

FACTS ABOUT THE RECIPIENT (the only things you may claim about them):
{_lead_facts(lead_data)}
"""
    return _call_tool(config.DRAFT_MODEL, _voice_system(campaign, profile), user, EMAIL_TOOL)


def generate_followup(campaign: dict, lead_data: dict, original: dict) -> dict:
    profile = load_profile()
    user = f"""Write a very short follow-up (2-4 sentences) to the email below, which got no reply.
Be polite, add no new claims, make it easy to say no. Same thread, so use subject "Re: {original['subject']}".
{_attachment_note(campaign, "followup")}

ORIGINAL EMAIL:
{original['body']}

FACTS ABOUT THE RECIPIENT:
{_lead_facts(lead_data)}
"""
    return _call_tool(config.DRAFT_MODEL, _voice_system(campaign, profile), user, EMAIL_TOOL)


# --------------------------------------------------------------------------- #
# 6. Fact check (the safety net for fully automatic sending)
# --------------------------------------------------------------------------- #

CHECK_TOOL = {
    "name": "verdict",
    "description": "Report whether the email is safe to send.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "problems": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["ok", "problems"],
    },
}


def fact_check(draft: dict, campaign: dict, lead_data: dict, kind: str = "initial") -> dict:
    """Ask a second, cheaper model to audit the draft. Returns {'ok': bool, 'problems': [...]}."""
    profile = load_profile()
    system = [{"type": "text", "text": (
        "You are a strict fact-checker for outgoing emails. You will be given the ONLY facts "
        "the email may rely on. Flag (ok=false) if the email: states any fact about the sender or "
        "recipient not present in the facts; invents papers, projects, numbers, events or "
        "connections; contains placeholders (brackets, braces, 'TODO'); includes an "
        "unsubscribe line; is rude, deceptive, or makes promises on the sender's behalf. "
        "ALSO flag any detail that is merely INFERRED rather than written in the facts, "
        "even if it sounds plausible: class standing (freshman/sophomore/junior/senior), "
        "relative time phrases ('last summer', 'recently', 'this year') that don't match the "
        "dates in the facts and today's date, gendered titles (Mr./Ms./Mrs.) or guessed "
        "gender, and opinions presented as the recipient's own views. Also flag a doubled "
        "closing (a phrase like 'Thank you for your time,' directly above 'Best,'). "
        "You are also told what is attached: flag any claim of an attachment that "
        "contradicts that, or any attachment other than the resume. "
        "Generic polite phrasing is fine. List each problem briefly."
    )}]
    user = f"""TODAY'S DATE: {date.today().strftime('%B %d, %Y')}

SENDER FACTS:
{yaml.safe_dump(profile, sort_keys=False)}

RECIPIENT FACTS:
{_lead_facts(lead_data)}

{_attachment_note(campaign, kind)}

EMAIL:
Subject: {draft['subject']}

{draft['body']}
"""
    return _call_tool(config.CHECK_MODEL, system, user, CHECK_TOOL, max_tokens=2000)


# --------------------------------------------------------------------------- #
# 4/5. Replies
# --------------------------------------------------------------------------- #

CLASSIFY_TOOL = {
    "name": "classify",
    "description": "Classify an incoming reply to a cold email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["needs_response", "decline", "stop_request", "auto_reply"],
                "description": (
                    "needs_response: a human replied with interest, a question, a referral, or "
                    "scheduling; decline: polite no / not hiring / not taking students "
                    "(no request to stop emailing); stop_request: asks not to be contacted "
                    "again, says unsubscribe/remove me/stop, or is hostile; "
                    "auto_reply: out-of-office or automated acknowledgement"
                ),
            },
            "reason": {"type": "string"},
        },
        "required": ["category", "reason"],
    },
}


def classify_reply(reply_text: str) -> dict:
    system = [{"type": "text", "text": (
        "You classify replies to cold outreach emails (job search / research positions). "
        "When in doubt between stop_request and decline, choose stop_request only if the "
        "person clearly wants no further contact."
    )}]
    return _call_tool(config.CHECK_MODEL, system, f"REPLY:\n{reply_text[:3000]}", CLASSIFY_TOOL, 1500)


REPLY_TOOL = {
    "name": "write_reply",
    "description": "Draft a reply for the sender to review and send themselves.",
    "input_schema": {
        "type": "object",
        "properties": {"body": {"type": "string"}},
        "required": ["body"],
    },
}


def suggest_reply(campaign: dict, lead_data: dict, original_body: str, reply_text: str) -> str:
    profile = load_profile()
    user = f"""{lead_data.get('name', 'The recipient')} replied to my email. Draft my response.
Keep it short, answer what they asked, use only facts you were given, and do not invent
availability or commitments (use a clear placeholder-free phrasing such as "let me check
and get back to you" when unsure).
You cannot attach files, but I will attach my resume myself before sending. If they asked for my
resume or CV, say once "I have attached my resume." Never mention any other attachment.

MY ORIGINAL EMAIL:
{original_body}

THEIR REPLY:
{reply_text[:3000]}
"""
    out = _call_tool(config.DRAFT_MODEL, _voice_system(campaign, profile), user, REPLY_TOOL)
    return out["body"]
