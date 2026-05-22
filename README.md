# slackdown

Convert a Slack thread URL into clean, structured Markdown.

It renders the common case (text, mentions, links, basic mrkdwn, `rich_text`
blocks, reactions) deterministically, and falls back to Claude on a
per-message basis only when it hits something it doesn't natively handle
(file uploads, legacy attachments, unusual subtypes, etc).

The rendered Markdown is written to `./thread.md` and also echoed to stdout.

## 1. Prerequisites

- Python 3.10+
- A Slack workspace where you can install (or have someone install) an app
- An Anthropic API key (optional — only needed if you want the LLM fallback)

## 2. Create a Slack app and get a user token

We use a **user token** (not a bot token) so the script can read anything
*you* can already see in Slack — public channels, private channels, DMs, and
group DMs — without having to invite a bot to each one.

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick your workspace, then paste this YAML manifest:

   ```yaml
   display_information:
     name: slackdown
     description: Export Slack threads to Markdown
   oauth_config:
     scopes:
       user:
         - channels:history
         - groups:history
         - im:history
         - mpim:history
         - users:read
   settings:
     org_deploy_enabled: false
     socket_mode_enabled: false
     token_rotation_enabled: false
   ```

3. Click **Next** → **Create**, then on the app page click **Install to Workspace** and approve.
4. On **OAuth & Permissions**, copy the **User OAuth Token** (starts with `xoxp-`). This is your `SLACK_TOKEN`.

## 3. (Optional) Get an Anthropic API key

Only needed if you want the LLM fallback for messages with file uploads, legacy
attachments, or other rare elements. If you don't set this, run with
`--no-fallback` and those messages will be rendered as `_[unsupported content: ...]_`.

Get a key from <https://console.anthropic.com/> → **API Keys** → **Create Key**.

## 4. Clone and install

```bash
git clone <this-repo-url> slackdown
cd slackdown

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

## 5. Configure environment variables

Create a `.env` file in the project root (it's already gitignored):

```bash
SLACK_TOKEN=xoxp-your-user-token-here
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

`python-dotenv` loads this automatically when you run `main.py`. You can also
export them in your shell instead if you prefer.

## 6. Run it

Grab a Slack thread URL by right-clicking any message in the thread →
**Copy link**. The URL looks like:

```
https://your-workspace.slack.com/archives/C0123456789/p1700000000123456
```

Then:

```bash
python main.py "https://your-workspace.slack.com/archives/C0123456789/p1700000000123456"
```

The rendered Markdown is written to `./thread.md` and echoed to stdout.
Progress and any LLM-fallback notices go to stderr.

### Flags

- `--no-fallback` — skip the LLM fallback entirely. Messages with unsupported
  content get an `_[unsupported content: ...]_` marker instead. Useful if you
  don't have (or don't want to spend) an Anthropic key.
- `--model <name>` — override the Claude model used for fallback. Defaults to
  `claude-opus-4-7`.

### Examples

```bash
# Default: deterministic rendering + Claude fallback for unsupported messages
python main.py "<thread-url>"

# No LLM, ever — purely deterministic
python main.py "<thread-url>" --no-fallback

# Use a different Claude model for fallback
python main.py "<thread-url>" --model claude-sonnet-4-5
```

## Troubleshooting

- **`error: SLACK_TOKEN not set`** — your `.env` isn't being loaded, or the
  variable name is wrong. Make sure you're running from the project root.
- **`Slack API error: missing_scope`** — re-open **OAuth & Permissions**, add
  the missing **user** scope, then click **Reinstall to Workspace** at the top
  of the page. Double-check the scope is under **User Token Scopes**, not **Bot
  Token Scopes**.
- **`Slack API error: channel_not_found`** — the token's user isn't a member of
  that channel/DM. Join it in Slack and try again.
- **`Slack API error: not_authed` / `invalid_auth`** — token is missing or
  wrong. Make sure you copied the **User OAuth Token** (`xoxp-…`), not the Bot
  token.
- **`Could not parse Slack URL`** — make sure you copied the message link, not
  the channel link. It must contain `/archives/<channel>/p<timestamp>`.
- **`error: ANTHROPIC_API_KEY not set (or pass --no-fallback)`** — either set
  the key in `.env` or run with `--no-fallback`.
