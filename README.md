# Ghostwriter

**An AI assistant for your personal Telegram account.** It reads the chats you choose, summarises
them, answers questions about them and drafts replies that sound like you — and nothing is ever sent
without your approval.

It can be used in two ways, separately or together:

- **Control bot** — a private Telegram bot that watches one contact for you: when they write, it
  prepares a draft in your style and asks you *Send / Edit / Skip / Regenerate*. It also does
  summaries and questions on request. Needs an Anthropic API key.
- **Connector for the Claude apps** — an MCP server that lets Claude (claude.ai, the mobile app,
  Claude Desktop, Claude Code) read your chats and send messages as you, while you talk to Claude as
  usual: *"what did Sam want from me this week? — draft a reply in my style — send it"*.
  Runs on your Claude subscription, no API key needed.

Your Telegram session, messages and notes stay on your own server.

## Features

- **Summaries** of any chat or group, for a period, from a specific person, or on a topic.
- **Questions about a conversation** — "what did we agree about the trip?".
- **Replies in your voice** — style learned from your real messages plus notes you write about
  yourself and about each person.
- **Human in the loop** — drafts are sent only after you approve them; the Claude app asks before
  every send.
- **Sensitive topics are not drafted** — money, meetings and dates, heavy or conflict topics and
  "are you a bot?" are escalated to you instead (configurable).
- **Natural timing** — random delay, "typing…" indicator, quiet hours.
- **Optional email channel** (IMAP/SMTP) for the default contact, threaded with the Telegram history.

## How it works

```
                 ┌──────────────── your server ────────────────────────────────┐
Telegram  ◀────▶ │  userbot (your account)  ──▶  message store (SQLite)        │
                 │        ▲                          │                         │
                 │        │ send                     ▼                         │
                 │  control bot  ◀── drafts ── Claude API (needs a key)        │ ◀──▶  you, in Telegram
                 │        ▲                                                    │
                 │        └── MCP server (OAuth) ◀─────────────────────────────┼──── Claude apps
                 └─────────────────────────────────────────────────────────────┘     (your subscription)
```

- The **userbot** (Telethon) is logged into *your* Telegram account. It stores messages of the
  chats you select and sends approved replies as you.
- The **control bot** (aiogram) is a separate bot that only answers you. It shows drafts with
  buttons and accepts commands in plain language.
- The **MCP server** exposes tools to the Claude apps; access is protected with an OAuth login
  (your password).

| Module | Responsibility |
|---|---|
| `ghostwriter/channels/telegram.py` | reads chats, imports history, searches, sends with "typing…" |
| `ghostwriter/channels/email.py` | IMAP polling (inbox + sent), SMTP replies with threading headers |
| `ghostwriter/storage/` | async SQLAlchemy: messages, drafts, key-value state (SQLite or Postgres) |
| `ghostwriter/llm/` | Claude calls: sensitive-topic check, drafting, summaries, command routing |
| `ghostwriter/control/` | the control bot: cards, buttons, chat selection |
| `ghostwriter/core.py` | the flow: message → check → draft → approval → timed send |
| `ghostwriter/mcp_server.py`, `oauth.py` | the connector for the Claude apps and its login |
| `ghostwriter/scheduling.py` | quiet hours and send delays |

## Quick start (local)

Requires Python 3.11+ (or just Docker, see [Deployment](#deployment)).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fill it in, see below
```

1. **Telegram API credentials** — <https://my.telegram.org> → *API development tools* →
   `TG_API_ID`, `TG_API_HASH`.
2. **Control bot** (optional) — create a bot with @BotFather → `CONTROL_BOT_TOKEN`; your numeric
   user id (e.g. from @userinfobot) → `OWNER_TG_ID`. Open your bot and press **Start**.
3. **Claude API key** (optional, needed for automatic drafts) → `ANTHROPIC_API_KEY`.
4. **Log in to your Telegram account** — enter your phone number (not a bot token), the code
   Telegram sends you and your 2FA password:
   ```bash
   python -m ghostwriter login
   ```
5. **Default contact** (optional) — `python -m ghostwriter chats` lists chat ids; put one into
   `CONTACT_TG_ID`, or choose later in the bot with `/contact`.
6. **Persona** — see [Your style](#your-style-persona):
   ```bash
   cp persona/context.example.md persona/context.md
   python -m ghostwriter backfill --limit 2000      # import history (--chat <id> for another chat)
   python -m ghostwriter export-examples           # your real messages -> persona/examples.md
   ```
7. **Run:** `python -m ghostwriter run`

## Your style (persona)

| File | What to put there |
|---|---|
| `persona/context.md` | who you are, how you write, what the assistant must never say or promise — used for every chat |
| `persona/contacts/<chat id>.md` | who this person or group is to you and how you talk; read on every draft |
| `persona/examples.md` | 20–40 typical messages of yours (keep it short: it goes into every draft) |
| `persona/escalation.toml` | sensitive categories and keywords that are escalated instead of drafted |

On top of that, your most recent hand-typed messages in the chat are used as a style reference.
All persona files except the examples are gitignored.

## Control bot

Write to it in plain language — *"summary of the work group for today"*, *"reply in my style,
politely decline"*, *"switch to the chat with Sam"* — or use commands:

| Command | |
|---|---|
| `/contact [name]` | choose the person whose messages get automatic drafts |
| `/chat [name]` | choose the current chat or group for summaries, questions and `/reply` |
| `/summary [N] [24h\|3d] [from Name] [topic]` | summary of the current chat |
| `/reply [instructions]` | draft a reply to the latest incoming message in the current chat |
| `/ask question` | ask about the current chat |
| `/pending` | drafts waiting for a decision |
| `/sync [N]` | import more history of the current chat |
| `/status` | model, channels, counters, quiet hours |
| `/cancel` | cancel typing a text for *Edit* |

Every draft card has **Send · Edit · Skip · Regenerate**. *Send* queues the message with a random
delay (`REPLY_DELAY_SECONDS`) inside active hours (`ACTIVE_HOURS`, `TIMEZONE`); it can still be
cancelled. A burst of messages gets one reply; if you reply by hand, the pending draft is dropped.
Drafts for a group show *"To: 👥 group"* — everyone in it will see the reply.

## Connector for the Claude apps (MCP)

Tools Claude gets:

| Tool | |
|---|---|
| `list_chats` | find a chat or group (searches all chats) |
| `get_messages` | messages of a chat — for a period, the last N, or from one person |
| `search_messages` | full-history search in a chat |
| `get_persona` | your context, notes about the person and style examples |
| `send_message` | send as you (only with `MCP_ALLOW_SEND=true`; the app asks you to confirm) |

**Setup**

1. Pick a login password (12+ characters, easy to type on a phone) and hash it:
   ```bash
   docker compose run --rm ghostwriter hash-password     # or: python -m ghostwriter hash-password
   ```
2. In `.env`:
   ```
   MCP_ENABLED=true
   MCP_AUTH=oauth
   MCP_PASSWORD_HASH=scrypt:...         # from step 1
   MCP_ALLOW_SEND=true                  # to allow sending from Claude
   MCP_DOMAIN=203-0-113-7.sslip.io      # your server's IP with dashes + .sslip.io, or your own domain
   ```
   `ANTHROPIC_API_KEY` and `CONTROL_BOT_TOKEN` may stay empty: without a key there are no
   automatic drafts, without a token there is no control bot.
3. Start with HTTPS ([Deployment](#deployment)) and check `curl https://<MCP_DOMAIN>/health` → `ok`.
4. On claude.ai: **Settings → Connectors → Add custom connector** → `https://<MCP_DOMAIN>/mcp`.
   A login page on your server opens; enter the password. The connector then also works in the
   mobile app. For Claude Code: `claude mcp add --transport http telegram https://<MCP_DOMAIN>/mcp`.

**How the login works:** Claude registers as an OAuth client, you enter the password once, and Claude
gets an access token (1 hour) and a refresh token (30 days, rotated on every use). Tokens are stored
only as hashes. Only clients that return to claude.ai, claude.com or localhost are accepted.
Five wrong passwords lock the login for 15 minutes.

Fallback without OAuth: `MCP_AUTH=token`, `MCP_TOKEN=$(openssl rand -hex 24)`, connector URL
`https://<MCP_DOMAIN>/<MCP_TOKEN>/mcp` — the URL is then the secret, keep it private.

## Deployment

The assistant is a long-running process (it keeps a Telegram connection open), so it needs a small
always-on server — 1 GB of RAM is enough. Serverless platforms don't fit.

```bash
git clone https://github.com/vanbogota/ch-sum.git && cd ch-sum
cp .env.example .env && nano .env
mkdir -p data secrets persona/contacts && sudo chown -R 1000:1000 data secrets persona   # container user
docker compose run --rm ghostwriter login        # once, interactive
docker compose --profile https up -d             # assistant + Caddy (automatic HTTPS)
```

- The `https` profile starts **Caddy**, which gets a Let's Encrypt certificate for `MCP_DOMAIN` and
  forwards to the assistant. Ports 80 and 443 must be reachable from the internet.
- Without the connector, `docker compose up -d` is enough.
- A systemd unit is in `deploy/ghostwriter.service` if you prefer running without Docker.
- On small VMs add swap before building images on the server, and open ports both in the cloud
  provider's firewall and in the OS firewall.

### Automatic deploys (GitHub Actions)

`.github/workflows/deploy.yml` runs on every pull request (tests) and on every push to `main`:

1. **test** — `pytest`; on failure nothing is built or deployed;
2. **build** — builds the image on GitHub's runners and pushes it to `ghcr.io/<owner>/<repo>`
   with the tags `latest` and `<commit sha>`;
3. **deploy** — SSH to the server, `deploy/deploy.sh <commit sha>`: `git pull`, pull the image,
   restart, wait for `/health`.

One-time setup:

1. *On the server* — a deploy key that can **only** run `deploy/deploy.sh`:
   ```bash
   ssh-keygen -t ed25519 -N "" -C github-deploy -f ~/.ssh/github_deploy
   echo "command=\"$HOME/ch-sum/deploy/deploy.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $(cat ~/.ssh/github_deploy.pub)" >> ~/.ssh/authorized_keys
   cat ~/.ssh/github_deploy        # copy the whole output, including the BEGIN/END lines
   rm ~/.ssh/github_deploy ~/.ssh/github_deploy.pub
   ```
2. *On the server* — the host key line for `DEPLOY_KNOWN_HOSTS` (works everywhere; `ssh-keyscan`
   from an older Windows OpenSSH fails with `unsupported KEX method`):
   ```bash
   echo "$(curl -s ifconfig.me) $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
   ```
3. *On GitHub* — **Settings → Secrets and variables → Actions**: `DEPLOY_HOST` (server IP),
   `DEPLOY_USER` (e.g. `ubuntu`), `DEPLOY_SSH_KEY` (step 1), `DEPLOY_KNOWN_HOSTS` (step 2, one line `<IP> ssh-ed25519 AAAA…`).
4. After the first build make the package public (**Packages → <repo> → Package settings → Change
   visibility**) so the server can pull it without a login, and re-run the deploy job. The image
   contains only code; `.env`, the session and the persona stay on the server.

Without `DEPLOY_HOST` the deploy job is skipped. Revoke the key by deleting its `github-deploy`
line from `~/.ssh/authorized_keys`.

### Operations cheat sheet

Run on the server in the project folder.

| Task | Command |
|---|---|
| status | `docker compose ps` |
| logs | `docker compose logs -f ghostwriter` |
| health | `curl https://<MCP_DOMAIN>/health` |
| update by hand | `deploy/deploy.sh` (prebuilt image) or `git pull && docker compose --profile https up -d --build` |
| roll back | `deploy/deploy.sh <commit sha>` |
| apply `.env` changes | `docker compose --profile https up -d --force-recreate` (`restart` does not reload `.env`) |
| connected Claude clients | `docker compose run --rm ghostwriter mcp-sessions [--revoke-all]` |
| change the login password | `hash-password` → `.env` → recreate; revoke sessions if the old one leaked |

Gotchas:

- `sqlite3.OperationalError: unable to open database file` — the container (uid 1000) can't write
  the mounted folders: `sudo chown -R 1000:1000 data secrets persona`.
- Run the assistant in **one place only**: two copies with the same bot token or Telegram session
  fight over updates.
- `query is too old and response timeout expired` in the logs — a button was pressed while the bot
  was down; harmless.

## Configuration

Everything is set in `.env`; `.env.example` lists all options with comments. The main ones:

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | model for the control bot's own Claude calls |
| `CONTACT_TG_ID` / `TG_CHAT` | — | default contact / chat (can be changed in the bot) |
| `OWNER_NAME`, `CONTACT_NAME` | `Owner`, `Contact` | names used in prompts |
| `ACTIVE_HOURS`, `TIMEZONE` | `09:00-23:00`, `Europe/Helsinki` | sending only inside this window |
| `REPLY_DELAY_SECONDS` | `60-600` | random delay before sending |
| `AUTO_MODE` | `false` | send non-escalated drafts without approval (card still shown, cancellable) |

### Token usage

Drafts are the main cost: the prompt contains the persona, the examples, your recent messages, the
history and the messages being answered. Limits:

| Variable | Default | Limits |
|---|---|---|
| `HISTORY_LIMIT` | 15 | history messages before the ones being answered |
| `STYLE_SAMPLE_SIZE` | 15 | your messages used as a style reference (each cut to 300 chars) |
| `REPLY_BATCH_LIMIT` | 6 | newest unanswered messages answered at once |
| `DRAFT_MESSAGE_CHARS` | 700 | characters kept from each long message / forwarded post |
| `ESCALATION_HISTORY` | 8 | context messages for the sensitive-topic check |

Summaries send only the messages of the requested period (the last 100 if no period is given).
Through the connector, everything Claude reads counts against your subscription limits.

### Using an LLM gateway

The control bot can call Claude through any Anthropic-compatible gateway or proxy (for cost
tracking, key management or tracing): set `ANTHROPIC_BASE_URL` to the gateway and
`ANTHROPIC_API_KEY` to the key it issued. Prefer a pass-through route that forwards requests
unchanged — the assistant relies on structured outputs. Through a gateway each request carries tags
with its purpose (`route`, `summary`, `question`, `escalation`, `draft`), so its logs can be filtered
by them. A gateway on the same host is reachable from the container as `http://host.docker.internal:<port>`.

## Security and privacy

- **The Telegram session file (`secrets/`) is full access to your account** — treat it like a
  password. `.env`, `secrets/`, `data/` and personal persona files are gitignored and never part of
  the Docker image.
- The control bot ignores everyone except `OWNER_TG_ID` and works only in a private chat.
- The connector is behind an OAuth password login; tokens expire and can be revoked.
- Message texts are not logged at INFO level.
- The model is instructed never to invent facts about you, make commitments or say it is an AI;
  when it can't answer without that, the message is escalated to you.
- Telegram allows third-party clients on your own account (that's what `api_id` is for); it does not
  allow spam or flooding. Don't message strangers and avoid huge history imports. A login from a new
  data-centre IP may trigger a security check.

## Development

```bash
pytest -q
```

The tests don't use the network: the LLM, Telegram and the bot are replaced by fakes; the MCP server
and its OAuth flow are tested over real local HTTP.

## Limitations

- Voice messages are stored as `[voice message]` without transcription.
- Edited and deleted Telegram messages are not tracked.
- Email belongs to the default contact only; messages sent by the assistant are not copied to the
  *Sent* folder (Gmail does this itself).
- One owner per installation.
