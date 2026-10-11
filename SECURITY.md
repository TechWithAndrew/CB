# Security and deployment status

This bot is intended for one State Zero Games server and one running process. It is ready for private staging evaluation. It has not had an independent security audit, a live Discord permissions test, a gateway endurance test, or a large-server load test. Automated tests and an advisory scan do not certify production security or capacity.

## Controls implemented

- No individual or bulk DM commands or delivery paths. Announcements require a staff member's confirmation.
- Current member permissions are fetched for authorization; confirmation buttons recheck staff access. Only the originating staff member can submit their preview.
- Announcement notifications use explicit role allowlists and Discord allowed-mentions fields. User mentions, @everyone, and @here notifications are disabled.
- Verification and self-service roles cannot grant staff privileges or configured support access. Discord-managed roles, @everyone, and roles above the bot are rejected.
- Verification answers are stored as expiring keyed hashes, with attempt limits, cooldowns, and a global challenge budget. The basic image challenge can be solved by OCR; it is not proof of humanity.
- Join spikes persistently pause verification and suppress welcome messages. Join history is bounded and welcome/challenge rate windows are bounded.
- Ticket channels deny @everyone and explicitly allow only their creator, configured support roles, and the bot. Discord administrators can still access them.
- Ticket creation is serialized, one open ticket per owner is enforced in SQLite, and persistent global quotas and reopen cooldowns limit channel creation.
- No full member-list caching/chunking at startup. SQLite queries supporting quotas and audit retention have indexes.
- Tokens are read from a runtime secret or private mounted file, not project configuration. Logs redact the active token; audit events exclude message bodies and secrets.
- Runtime databases, secrets, local config, dependency environments, and logs are excluded from Git. The container runs as a non-root user with dropped capabilities and a read-only application filesystem.

## Required deployment controls

Store the bot token in an encrypted hosting secret manager or securely mounted private file. The live process necessarily uses the usable token in memory. Keep staff and administrator roles above the bot's role; grant the bot only the Discord permissions it needs. Enable AutoMod, Rules Screening, and the community channel gates described in README.md. Protect database backups and define ticket/audit retention.

Start in a private test server and verify channel isolation, role grants, pings, old-preview permission revocation, transcripts, and controls after restarts. Measure latency, CPU, memory, database behavior, and Discord API rate limits before expanding. Monitor uptime and failures and practice token rotation. Closed ticket channels still count against Discord's server/category channel limits and must be cleaned up by staff.

Multiple replicas are not supported: process-local locks and rate windows do not coordinate across instances. SQLite calls are synchronous; high-traffic capacity must be measured. Scaling requires shared persistence and distributed coordination rather than simply starting more copies.

## Validation performed

The current version passes 31 local automated tests, offline command/panel serialization, dependency consistency, and Ruff static checks. A pip-audit scan of all pinned runtime dependencies found no known reported vulnerabilities. This finding is advisory database coverage at scan time, not a guarantee against unknown or application-level vulnerabilities. GitHub Actions repeats the tests and dependency advisory check; its execution status is separate from the locally verified results.

## Reporting and rotation

Report suspected vulnerabilities privately to the repository owner using an available private contact method. Do not post tokens, private transcripts, or exploitable server configuration in public issues. If a token is exposed, reset it in the Discord Developer Portal immediately, replace the hosting secret, restart the bot, and investigate the Discord audit log.
