# Security and deployment status

This bot is intended for one State Zero Games server and one running process. It is ready for private staging evaluation. It has not had an independent security audit, a live Discord permissions test, a gateway endurance test, or a large-server load test. Automated tests and an advisory scan do not certify production security or capacity.

## Controls implemented

- No individual or bulk DM commands or delivery paths. Announcements require a staff member's confirmation.
- Current member role membership and Discord role permission bits are fetched for authorization; server-owner fallback is checked against fresh guild metadata; confirmation buttons recheck staff access. Only the originating staff member can submit their preview.
- Announcement notifications use explicit role allowlists and Discord allowed-mentions fields. User mentions, @everyone, and @here notifications are disabled.
- Verification and self-service roles cannot grant staff privileges or configured support access. Discord-managed roles, @everyone, and roles above the bot are rejected. Grant-time role definitions are fetched again, so an elevated role cannot pass solely because its cached definition was safe.
- Verification answers are stored as expiring keyed hashes, with attempt limits, cooldowns, and a global challenge budget. The basic image challenge can be solved by OCR; it is not proof of humanity.
- Join spikes persistently pause verification and suppress welcome messages. Join history is bounded and welcome/challenge rate windows are bounded.
- Ticket channels deny @everyone and explicitly allow only their creator, configured support roles, and the bot. Discord administrators can still access them.
- Ticket creation is serialized, durable reservations precede API calls, one active ticket per owner is enforced in SQLite, and persistent quotas include uncertain reservations. Recovery validates request markers and private permissions, retries interrupted closing, and detects missing channels. Ambiguous/missing requests require guarded staff review; channels are never blindly deleted.
- Database work runs on a dedicated bounded worker. An OS file lock rejects another process using the same database, and guild binding prevents accidental reuse with another server. Actions, transcripts, rate windows, audit notification queues, and recent metric samples have fixed bounds. Staff controls have reserved admission capacity with fresh authorization.
- No full member-list caching/chunking at startup. SQLite queries supporting quotas and audit retention have indexes.
- Tokens are read from a runtime secret or private file, not project configuration. Token-file reads reject symlinks, non-regular files, unsafe permissions, and oversized/non-ASCII inputs. Logs redact the active token and temporary Discord webhook credentials in URLs; audit events exclude message bodies and secrets. Discord audit publication rechecks current channel/role access and refuses broad grants. Role/announcement writes first persist a private audit intent; storage failures stop the write.
- Runtime databases, secrets, local config, dependency environments, and logs are excluded from Git. The optional Docker template runs as a non-root user with dropped capabilities and a read-only application filesystem. The Pelican egg/provider must separately enforce its runtime/host isolation; uploading this code does not apply the Compose security settings.
- Runtime dependencies are fully pinned with SHA-256 artifact hashes; the lock requires binary wheels and refuses source-build fallback. requirements.txt forwards to that lock for generic eggs. CI verifies installation, static checks, offline behavior, recovery/abuse tests, advisory scanning, and local overload simulation.
- The CLI applies a private creation umask and disables process core dumps before loading the token, reducing accidental credential persistence. This does not prevent a privileged host administrator from reading runtime memory.
- Private health snapshots expose aggregate readiness, sampled durations/loop lag, bounded queue depth, and recovery/configuration status. The CLI rejects stale/unhealthy snapshots. Consistent SQLite backups include committed WAL data and reject overwriting existing backup files. Neither CLI operation loads the token.

## Required deployment controls

Store the bot token in an encrypted hosting secret manager or securely mounted private file. The live process necessarily uses the usable token in memory. Keep staff and administrator roles above the bot's role; grant the bot only the Discord permissions it needs. Enable AutoMod, Rules Screening, and the community channel gates described in README.md. Protect database backups and define ticket/audit retention.

Start in a private test server and verify channel isolation, role grants, pings, old-preview permission revocation, transcripts, and controls after restarts. Measure latency, CPU, memory, database behavior, and Discord API rate limits before expanding. Monitor uptime and failures and practice token rotation. Closed ticket channels still count against Discord's server/category channel limits and must be cleaned up by staff.

Multiple replicas are not supported: process-local locks and rate windows do not coordinate across instances or separate database copies. SQLite calls run off the gateway loop but still use a single writer; high-traffic capacity must be measured. Scaling requires shared persistence and distributed coordination rather than simply starting more copies.

## Validation performed

Local validation covers automated tests, forced-process-exit persistence, cross-process ownership, legacy database migration, recovery/permission failures, consistent live backup/restore, overload rejection, offline command/panel serialization, dependency consistency, and Ruff checks. A clean environment installation verifies all runtime artifact hashes. A pip-audit scan found no known reported vulnerabilities in the pinned runtime dependencies. These results are advisory coverage and simulation, not a guarantee against unknown/application vulnerabilities or a live-host capacity certification. See deploy/VALIDATION.md for measured local evidence; GitHub Actions status is separate from local results.

## Reporting and rotation

Report suspected vulnerabilities privately to the repository owner using an available private contact method. Do not post tokens, private transcripts, or exploitable server configuration in public issues. If a token is exposed, reset it in the Discord Developer Portal immediately, replace the hosting secret, restart the bot, and investigate the Discord audit log.
