# State Zero Games community bot

A configurable Discord bot for the State Zero Games studio community, built with Python 3.12+, discord.py, and SQLite. Private conversations use tickets; the bot has no DM commands. This repository contains the implementation and deployment templates; it is **not connected to a live Discord server yet**. You must supply your own bot token securely and configure actual server, role, and channel IDs.

## What you can do

| Feature | Behavior |
| --- | --- |
| Announcements | Choose a channel, fill out a paragraph form, add an image attachment or HTTPS image URL, preview, then confirm. Select multiple approved ping roles in the preview, or clear them for no ping. |
| Welcome and verification | Branded welcome embed with the member's avatar, optional banner, customizable text, and Verify button. A private image challenge grants a safe community role on success. |
| Self-service roles | Persistent dropdown panels with one or multiple roles. Selecting a new set replaces only roles belonging to that menu. Clear all selections to remove its roles. |
| Tickets | Persistent dropdown with Player Support, Bug Report, and Other by default. One open private ticket per member, configurable global quotas, with configured support roles, close confirmation, archiving, and transcript export. |
| Safety | Staff authorization checked at execution, explicit ping allowlist, safe role assignment, cooldowns, limited verification attempts, account-age threshold, persistent manual/automatic verification pause, private audit events. |

## First-time setup

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and add a bot. Enable **Server Members Intent**. Enable **Message Content Intent** only if you want complete ticket transcripts; the other features do not need it. See the transcript section below.
2. Generate an installation URL with scopes `bot` and `applications.commands`. Install into your server with **View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Roles, Manage Channels**. Do not grant Administrator. If you need to ping roles that are not mentionable, also grant **Mention Everyone** in announcement channels; this bot's send payload still explicitly permits only chosen allowlisted roles.
3. Create a non-staff **Verified** role and a **Support Team** role. Move the bot's role above Verified and every self-service role. Configure staff/support roles separately: verification and self-service roles cannot grant moderation or administration privileges.
4. Enable Developer Mode in Discord (Settings → Advanced), then use **Copy ID** on your server, roles, channels, and ticket category. IDs are public identifiers, not secrets.
5. Install locally:

   ```bash
   cd /path/to/CB
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.lock
   cp config.example.json config.json
   ```

6. Edit `config.json`:
   - Set `guild_id`, `verification.role_id`, `welcome_channel_id`, and a private `audit_channel_id`.
   - Set `staff_role_ids` to the staff allowed to compose messages/post panels. Members with Discord **Manage Server** can always use staff commands. Support Team members can manage their assigned tickets without receiving general bot staff access.
   - Populate `announcement_channel_ids` to restrict announcement destinations. An empty list allows staff to choose any text channel in this server.
   - Populate `ping_role_ids` with every role the bot may ping. An empty list disables all role pings. `@everyone` and `@here` are never allowed.
   - In **each** ticket category, set `parent_id` to a Discord category ID and `support_role_ids` to the support roles for that category. Multiple ticket types may use the same Discord category, or different ones.
   - Customize branding, welcome text/image, challenge rules, ticket text, traffic limits, and role menus. The example IDs below are placeholders to replace, not usable IDs.
7. Validate configuration and the implementation:

   ```bash
   .venv/bin/python -m statezero --check
   .venv/bin/python -m unittest discover -s tests -v
   ```

8. Store the bot token in your hosting provider's secret manager as `DISCORD_TOKEN`, or mount a private file and set `DISCORD_TOKEN_FILE` to its path. The bot does not load `.env` files. Never paste the token into Discord, chat, JSON configuration, source code, or a command-line argument. Run:

   ```bash
   .venv/bin/python -m statezero
   ```

   The bot synchronizes server-specific slash commands at startup. `/bot-status` reports missing roles/channels and verification safety status. Configure the host to restart the process after failures and after reboot. The Docker option below includes a restart policy.
9. Post panels using `/verification-panel`, `/role-panel`, and `/ticket-panel`. Test in a private test server or staff-only channel before posting publicly. Run **one bot instance per SQLite database**; do not run local and Docker instances simultaneously.

## Protect the server before opening it

The bot can gate verification and its own features, but Discord channel permissions control actual community access:

- For `@everyone`, allow only rules, welcome, and verification channels. Deny View Channel on the community categories. Do not allow unverified members to create threads or send messages broadly.
- Give Verified access to the community categories. Remove any other default roles that bypass this gate. Complete Discord Rules Screening before verification.
- Put the ticket panel in a channel that verified members can see. Keep the ticket parent category private; the bot creates explicit overwrites for `@everyone`, the ticket creator, the bot, and configured support roles. Staff with Administrator still have access, as required by Discord's permission model.
- Keep the audit channel staff-only. Turn on Discord AutoMod, mention-spam filtering, raid protection, and appropriate server verification settings. Require 2FA for moderation actions.

The image challenge is a basic abuse barrier, **not proof of humanity**. Determined bots, OCR, and human-assisted raids can bypass it. Account-age restrictions and the join-spike lock provide additional friction. No bot can guarantee that spammers cannot join; this implementation does not automatically kick or ban new members. A spike pauses verification until staff uses `/verification-lock locked:false`. Restarting the process preserves the lock. Join counting and message/challenge rate windows reset on restart. Welcome messages stop while verification is paused and are separately limited to 20 per minute by default.

Moderators can manually grant the Verified role to legitimate members who need an accessible alternative. A Verified role should contain ordinary community permissions only. The bot rejects roles with privileges outside its safe community-permission allowlist.

## Commands

| Command | Who | Purpose |
| --- | --- | --- |
| `/announce channel [ping_role] [image]` | Staff | Opens the message form. The preview allows selecting multiple configured ping roles or clearing all pings. |
| `/verify` | Member | Begin an image verification challenge. Also available through the persistent button. |
| `/verification-panel channel` | Staff | Post a reusable verification panel. |
| `/verification-lock locked` | Staff | Pause/resume verification during an incident. |
| `/role-panel channel menu` | Staff | Post the role menu identified by a configuration key. |
| `/ticket-panel channel` | Staff | Post the configured ticket category dropdown. |
| `/ticket-close` | Creator/support/staff | Confirm and archive the current ticket. |
| `/ticket-transcript` | Creator/support/staff | Privately export the newest 1,000 messages. |
| `/bot-status` | Staff | Show configuration problems and safety status. |

Forms support real line breaks, blank lines, and Discord Markdown such as `**bold**`, `*italic*`, headings, lists, and links. Titles are limited to 256 characters and bodies to 4,000 characters, matching Discord form/embed limits. For longer announcements, split the content into multiple posts. Image attachments must be under 8 MB. Attachments use Discord's hosted URL, so links can expire; a durable HTTPS URL is preferable for long-lived announcements. No arbitrary image URL is fetched by the bot itself.

Staff see an ephemeral preview and must select **Confirm send**. Preview buttons cannot be reused. If Discord returns an uncertain network error, inspect the destination before composing another message. Discord may have accepted a send even if the response was lost.


## Role menu example

Add a menu to `role_menus` in `config.json`:

```json
{
  "key": "community",
  "title": "Choose your State Zero roles",
  "description": "Choose what you're interested in. Select your complete preferred set each time; clear the selection to remove these roles.",
  "max_selected": 2,
  "options": [
    {"role_id": 111111111111111111, "label": "Game Updates", "description": "News about our games", "emoji": "🎮"},
    {"role_id": 222222222222222222, "label": "Playtests", "description": "Join future testing sessions", "emoji": "🧪"}
  ]
}
```

Use `max_selected: 1` for a single-choice menu, or up to the number of options for multiple choices. Discord permits 25 options per dropdown. Restart after editing configuration. Existing panels resolve by their stable menu/category key; if options or panel descriptions change, delete and repost those panels so displayed choices match configuration. Keep menu keys stable across restarts. Never put Verified, staff, or support roles into a self-service menu.

Welcome placeholders: `{mention}`, `{user}`, `{member_count}`, `{server}`. Welcome mentions are displayed in the embed without sending a notification. The welcome includes the new member's avatar and can show a custom banner using `branding.welcome_image`. Colors are decimal RGB values, for example `8150268` for `#7C5CFC`.

## Tickets and transcripts

Closing removes the creator's channel access and marks the ticket closed in SQLite; support staff retain access to the archived channel. It frees the creator to open a new ticket after the configured reopen cooldown (10 minutes by default). The bot does not delete archived channels automatically. Staff should apply a documented retention policy and delete old ticket channels in Discord. If a creator has Administrator, Discord can bypass the access restriction. Export a transcript **before** closing if the creator needs a copy.

Transcripts are text files containing the newest 1,000 messages, basic embed title/body text, and attachment URLs. They do not download attachment contents. URLs may expire. Transcripts are generated in memory and delivered ephemerally; the bot does not retain transcript files.

For complete user-message transcripts, enable Message Content Intent in the Developer Portal **and** set the hosting environment variable `BOT_MESSAGE_CONTENT=true`. This is an optional privileged intent. If it is disabled, Discord may omit message contents, embeds, or attachments. Transcripts explicitly state the content-intent status. Avoid exporting sensitive information unnecessarily; the author downloading a transcript is responsible for keeping it private.

## Token and data security

- A Discord bot token is a bearer credential. Use an encrypted hosting secret manager for storage, TLS for Discord traffic, and restrictive runtime permissions. The application must still read the usable token in memory to connect to Discord. Encrypting it in source code with a key stored beside it does not protect it.
- Prefer a mounted secret file outside the repository, owned by the runtime user with mode `0600`. `DISCORD_TOKEN_FILE` takes precedence over `DISCORD_TOKEN`. The bot rejects group/world-readable token files. In containers, ensure the source file is readable by UID 10001 while keeping mode `0600`.
- Token values are excluded from config and audit events, `.env` and runtime data are Git-ignored, and log output redacts the active token. Do not enable HTTP header tracing or share process environments. If a token is exposed, reset it in the Developer Portal immediately and replace the secret.
- SQLite stores member IDs, verification lock status, ticket ownership/status, and action metadata. It does not store tokens, CAPTCHA answers, or message bodies. CAPTCHA answers exist only as keyed hashes with a short expiry; challenges reset on restart. Audit rows older than 90 days are pruned when a new audit event is recorded. Discord audit-channel messages are not automatically pruned. Ticket records persist until your administrator removes them.
- Protect `data/` and backups using host access controls and encrypted storage. Keep a backup of `data/statezero.db` using SQLite's backup API while stopped or through a consistent backup process. Persistent panels need both the configuration and database after restoring the host.
- Use a maintained host, run as an unprivileged user, update dependency pins intentionally, and review changes before deployment. No claim of absolute security is made.

## Docker deployment

Docker is optional; local Python is the tested development workflow. Container deployment templates must be validated on your Docker host.

1. Fill in `config.json`. Create a private token file **outside this repository** through your host's secret manager or secure editor. For Compose's local file secret, make its owner UID/GID `10001:10001` and permissions `0600`. Docker Compose's local secret bind mounts may preserve the source ownership, so do not rely on a YAML `uid` setting to change it.
2. Prepare persistent storage:

   ```bash
   mkdir -p data
   sudo chown 10001:10001 data
   sudo chmod 700 data
   chmod 644 config.json
   ```

3. Set `DISCORD_SECRET_FILE` to the **path** of that token file (this variable contains no token), then start:

   ```bash
   docker compose -f deploy/compose.yaml up -d --build
   docker compose -f deploy/compose.yaml logs --tail=50 bot
   ```

The template runs without Linux capabilities or root privileges, with a read-only application filesystem and persistent writable data directory. It exposes no incoming ports. The host must allow outbound HTTPS to `discord.com` and secure WebSocket connections to Discord's gateway (normally `gateway.discord.gg` and gateway endpoints returned by Discord), plus Discord attachment CDN access as needed. A proxy-only secret placeholder that works for HTTPS headers is not necessarily usable for Discord's WebSocket IDENTIFY; use a host that supports a real runtime bot secret and outbound WebSockets.

`config.json` contains public server configuration, never secrets; its read permission lets the container's runtime user read the bind mount. Keep the token file separate and private.

## Local checks

```bash
.venv/bin/python -m pip check
.venv/bin/python -m statezero --config config.example.json --check
.venv/bin/python -m unittest discover -s tests -v
```

The tests exercise mocked Discord actions, persistent SQLite state, permission checks, global ticket quotas, image generation, challenge expiry/replay prevention, explicit mention restrictions, and concurrent ticket creation. They do not establish live Discord permission or gateway readiness. The default example deliberately leaves IDs unset rather than inventing credentials or server IDs.

Before going live, verify with a normal non-staff account: an unverified member cannot view community channels; verification grants only Verified; role changes preserve unrelated roles; a ticket is invisible to other members; closing archives it; non-ping announcements produce no notification; approved role pings notify the intended roles. Restart the bot and check existing verification, role, and ticket controls still respond.


## Capacity and production rollout

This is a single-server, single-process bot. It avoids loading the full member list at startup and fetches individual members when checking permissions. Discord API rate limiting is handled by discord.py; local limits reduce avoidable work and channel creation. SQLite is appropriate for this deployment shape, but its operations are synchronous and its performance under your traffic has not been established. For multiple bot replicas, move persistence to a shared transactional database and replace process-local locks/rate windows with distributed controls before scaling.

The `limits` configuration defaults to 100 open tickets, 60 new tickets per hour, a 600-second per-user reopen cooldown, 20 welcomes per minute, and 30 new verification challenges per minute. Ticket limits and reopen history survive restarts. Creation is serialized across members to prevent quota races. The bot also refuses new tickets when the server has at least 490 channels or the parent category has at least 49 children. Archived channels continue consuming Discord's capacity; staff must clean them up. Limits intentionally reject or suppress work instead of queueing an unlimited backlog. Adjust them cautiously after measurement.

The project is ready for **staging evaluation**, not certified for unrestricted large-server deployment. Mocked tests cannot prove live Discord permissions, gateway resilience, human verification, or capacity. Before public rollout:

1. Use a separate test bot and private Discord server. Test permission isolation with normal, unverified, support, and staff accounts; verify removed staff cannot submit an old announcement preview.
2. Restart with an open ticket, an existing role panel, and verification paused. Confirm controls and stored state behave correctly. Confirm obsolete `/dm` and `/dm-preferences` commands disappear after startup command synchronization. If you have an old local config, remove its obsolete `dm` field before upgrading.
3. Exercise a realistic join/interaction burst in a controlled test server and record CPU, memory, interaction latency, Discord rate limits, and database errors. Never load-test real members or Discord indiscriminately.
4. Use encrypted hosting secrets, staff roles above the bot role, private database backups, logging/uptime monitoring, Discord AutoMod and Rules Screening, and a practiced token-rotation procedure. Verify the mounted token remains excluded from Git and container build contexts.
5. Roll out to a small group first. Keep ticket quotas and the verification pause available; expand only after monitoring shows acceptable behavior.

Old SQLite databases may still contain an unused legacy preferences table from the previous DM implementation; new databases do not create it. It is not read by this version. Administrators can remove that table after a backup if they need to delete the old opt-in records.

## Committing and pushing changes

Environment publication and GitHub publication are separate operations. GitHub receives code only after a commit and push. For later changes, from this checkout:

```bash
git status --short
# Stage only the source/documentation files you intend to publish.
git add statezero tests README.md config.example.json requirements.txt requirements.lock pyproject.toml deploy .gitignore .dockerignore
git diff --cached --check
git diff --cached --stat
git commit -m "Describe the change"
git push origin main
```

Never stage `config.json`, token files, `.env`, `.venv`, `data/`, logs, or backups. This environment already supplies scoped HTTPS Git authentication; do not put tokens into the remote URL or commit credentials.
