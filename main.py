#!/usr/bin/env python3
"""
slack_thread_to_markdown.py (hybrid version)

Convert a Slack thread URL into structured Markdown.

Uses deterministic templating for the common case (text, mentions, links,
basic mrkdwn, rich_text blocks, reactions). Falls back to Claude on a
per-message basis when it encounters something unknown (file uploads,
legacy attachments, non-rich_text blocks, unusual subtypes, etc).

Setup:
    pip install slack-sdk anthropic
    export SLACK_BOT_TOKEN=xoxb-...
    export ANTHROPIC_API_KEY=sk-ant-...   # only needed if fallback fires

Usage:
    python slack_thread_to_markdown.py "<slack_thread_url>"
    python slack_thread_to_markdown.py <url> -o out.md
    python slack_thread_to_markdown.py <url> --no-fallback  # skip LLM, emit [unsupported] markers
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()


SLACK_URL_RE = re.compile(r"slack\.com/archives/(?P<channel>[A-Z0-9]+)/p(?P<ts>\d+)")
MENTION_RE = re.compile(r"<@(U[A-Z0-9]+)(?:\|[^>]+)?>")
CHANNEL_REF_RE = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]+))?>")
LINK_LABELED_RE = re.compile(r"<((?:https?|mailto):[^|>]+)\|([^>]+)>")
LINK_BARE_RE = re.compile(r"<((?:https?|mailto):[^>]+)>")

# Subtypes that don't change rendering (regular messages from bots, broadcast replies).
SAFE_SUBTYPES = {"bot_message", "thread_broadcast"}


class UnsupportedElement(Exception):
    """Raised when deterministic rendering hits something it doesn't handle."""


# ---------- URL & Slack API ----------


def parse_slack_url(url: str) -> tuple[str, str]:
    m = SLACK_URL_RE.search(url)
    if not m:
        raise ValueError(f"Could not parse Slack URL: {url!r}")
    raw = m.group("ts")
    if len(raw) <= 6:
        raise ValueError(f"Unexpected timestamp format: {raw!r}")
    return m.group("channel"), f"{raw[:-6]}.{raw[-6:]}"


def resolve_users(client: WebClient, user_ids: set[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for uid in user_ids:
        if not uid:
            continue
        try:
            r = client.users_info(user=uid)
            p = r["user"]["profile"]
            names[uid] = p.get("display_name") or p.get("real_name") or uid
        except SlackApiError:
            names[uid] = uid
    return names


def fetch_thread(client: WebClient, channel: str, ts: str) -> dict:
    try:
        resp = client.conversations_replies(channel=channel, ts=ts, limit=1000)
    except SlackApiError as e:
        raise RuntimeError(f"Slack API error: {e.response.get('error')}") from e
    messages = resp.get("messages", [])
    if not messages:
        raise RuntimeError("Thread is empty or not accessible")
    user_ids = {m.get("user") for m in messages if m.get("user")}
    return {
        "channel": channel,
        "messages": messages,
        "users": resolve_users(client, user_ids),
    }


# ---------- Deterministic rendering ----------


def normalize_inline(text: str, users: dict[str, str]) -> str:
    """Slack mrkdwn → Markdown for the parts that map cleanly."""
    text = MENTION_RE.sub(lambda m: f"@{users.get(m.group(1), m.group(1))}", text)
    text = CHANNEL_REF_RE.sub(lambda m: f"#{m.group(2) or m.group(1)}", text)
    text = LINK_LABELED_RE.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", text)
    text = LINK_BARE_RE.sub(lambda m: m.group(1), text)
    # *bold* → **bold** (word-boundary guard to avoid eating *not_bold* in code/paths)
    text = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"**\1**", text)
    # ~strike~ → ~~strike~~
    text = re.sub(r"(?<!\w)~([^~\n]+)~(?!\w)", r"~~\1~~", text)
    return text


def render_rich_text_element(el: dict, users: dict) -> str:
    """Render an inline element inside a rich_text section."""
    t = el.get("type")
    if t == "text":
        s = el.get("text", "")
        style = el.get("style", {})
        if style.get("code"):
            s = f"`{s}`"
        if style.get("bold"):
            s = f"**{s}**"
        if style.get("italic"):
            s = f"_{s}_"
        if style.get("strike"):
            s = f"~~{s}~~"
        return s
    if t == "link":
        url = el.get("url", "")
        return f"[{el.get('text') or url}]({url})"
    if t == "user":
        uid = el.get("user_id", "")
        return f"@{users.get(uid, uid)}"
    if t == "channel":
        return f"#{el.get('channel_id', '')}"
    if t == "emoji":
        return f":{el.get('name', '')}:"
    if t == "usergroup":
        return f"@{el.get('usergroup_id', '')}"
    if t == "broadcast":
        return f"@{el.get('range', 'channel')}"
    raise UnsupportedElement(f"rich_text element type: {t}")


def render_rich_text_section(section: dict, users: dict) -> str:
    return "".join(
        render_rich_text_element(e, users) for e in section.get("elements", [])
    )


def render_block(block: dict, users: dict) -> str:
    t = block.get("type")
    if t != "rich_text":
        raise UnsupportedElement(f"block type: {t}")
    parts: list[str] = []
    for el in block.get("elements", []):
        sub = el.get("type")
        if sub == "rich_text_section":
            parts.append(render_rich_text_section(el, users))
        elif sub == "rich_text_preformatted":
            content = "".join(
                e.get("text", "")
                for e in el.get("elements", [])
                if e.get("type") == "text"
            )
            parts.append(f"```\n{content}\n```")
        elif sub == "rich_text_quote":
            content = render_rich_text_section(el, users)
            parts.append("\n".join(f"> {line}" for line in content.split("\n")))
        elif sub == "rich_text_list":
            if el.get("indent", 0) > 0:
                raise UnsupportedElement("nested rich_text_list")
            style = el.get("style", "bullet")
            for i, item in enumerate(el.get("elements", []), 1):
                prefix = f"{i}." if style == "ordered" else "-"
                parts.append(f"{prefix} {render_rich_text_section(item, users)}")
        else:
            raise UnsupportedElement(f"rich_text sub-type: {sub}")
    return "\n\n".join(parts)


def _is_link_unfurl(att: dict, text: str) -> bool:
    url = att.get("from_url") or att.get("original_url")
    return bool(url) and url in text


def render_message_deterministic(msg: dict, users: dict) -> str:
    """Render one message. Raises UnsupportedElement on anything unhandled."""
    text = msg.get("text", "")
    non_unfurl_attachments = [
        a for a in (msg.get("attachments") or []) if not _is_link_unfurl(a, text)
    ]
    if non_unfurl_attachments:
        raise UnsupportedElement("legacy attachments")
    if msg.get("files"):
        raise UnsupportedElement("file uploads")
    subtype = msg.get("subtype")
    if subtype and subtype not in SAFE_SUBTYPES:
        raise UnsupportedElement(f"subtype: {subtype}")

    if msg.get("blocks"):
        return "\n\n".join(render_block(b, users) for b in msg["blocks"])
    return normalize_inline(msg.get("text", ""), users)


# ---------- LLM fallback ----------

FALLBACK_PROMPT = """Convert this Slack message JSON into clean Markdown.

Output the message body only — no author header, no timestamp, no commentary,
no surrounding fences. Preserve code blocks, links, and quoted text. Summarize
any file uploads or attachments inline. Resolve user IDs using this map:

{users}

Message:
{message}
"""


def render_message_via_llm(msg: dict, users: dict, model: str) -> str:
    import anthropic  # lazy import so --no-fallback works without it

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": FALLBACK_PROMPT.format(
                    users=json.dumps(users),
                    message=json.dumps(msg, indent=2),
                ),
            }
        ],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


# ---------- Assembly ----------


@dataclass
class RenderedMessage:
    author: str
    time: str
    body: str
    reactions: str
    fell_back: bool


def render_thread(thread: dict, *, allow_fallback: bool, model: str) -> tuple[str, int]:
    users = thread["users"]
    rendered: list[RenderedMessage] = []
    fallback_count = 0

    for msg in thread["messages"]:
        author = users.get(msg.get("user")) or msg.get("username") or "bot"
        dt = datetime.fromtimestamp(float(msg["ts"]), tz=timezone.utc)
        time_str = dt.strftime("%Y-%m-%d %H:%M UTC")
        reactions = ""
        if msg.get("reactions"):
            reactions = ", ".join(
                f":{r['name']}: ({r['count']})" for r in msg["reactions"]
            )

        fell_back = False
        try:
            body = render_message_deterministic(msg, users)
        except UnsupportedElement as e:
            if allow_fallback:
                print(
                    f"  → LLM fallback for ts={msg['ts']} (reason: {e})",
                    file=sys.stderr,
                )
                body = render_message_via_llm(msg, users, model)
                fell_back = True
                fallback_count += 1
            else:
                body = f"_[unsupported content: {e}]_"

        rendered.append(RenderedMessage(author, time_str, body, reactions, fell_back))

    parent, *replies = rendered
    lines = [
        "# Slack thread",
        "",
        f"**Channel:** `{thread['channel']}`  ",
        f"**Started:** {parent.time}  ",
        f"**Replies:** {len(replies)}",
        "",
        "## Original message",
        "",
        f"**{parent.author}** — {parent.time}",
    ]
    if parent.reactions:
        lines.append(f"_Reactions: {parent.reactions}_")
    quoted = "\n".join(f"> {line}" for line in parent.body.splitlines())
    lines.extend(["", quoted, ""])

    if replies:
        lines.extend(["## Replies", ""])
        for i, r in enumerate(replies, 1):
            marker = " _(LLM-formatted)_" if r.fell_back else ""
            lines.append(f"### {i}. {r.author} — {r.time}{marker}")
            if r.reactions:
                lines.append(f"_Reactions: {r.reactions}_")
            lines.extend(["", r.body, ""])

    return "\n".join(lines), fallback_count


# ---------- CLI ----------


def main() -> int:
    p = argparse.ArgumentParser(description="Slack thread → structured Markdown")
    p.add_argument("url")
    p.add_argument("-o", "--output")
    p.add_argument(
        "--no-fallback",
        action="store_true",
        help="Disable LLM fallback; emit [unsupported] markers instead",
    )
    p.add_argument("--model", default="claude-opus-4-7")
    args = p.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("error: SLACK_BOT_TOKEN not set", file=sys.stderr)
        return 1
    if not args.no_fallback and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "error: ANTHROPIC_API_KEY not set (or pass --no-fallback)",
            file=sys.stderr,
        )
        return 1

    try:
        channel, ts = parse_slack_url(args.url)
        thread = fetch_thread(WebClient(token=token), channel, ts)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    md, fallback_count = render_thread(
        thread, allow_fallback=not args.no_fallback, model=args.model
    )
    print(
        f"rendered {len(thread['messages'])} messages "
        f"({fallback_count} via LLM fallback)",
        file=sys.stderr,
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(md)

    return 0


if __name__ == "__main__":
    sys.exit(main())
