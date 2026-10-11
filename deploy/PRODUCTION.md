# State Zero Games deployment and operations

Release 0.2.0 adds durable ticket recovery, a bounded database worker, admission limits, private health, consistent backup tools, and hash-verified dependencies. These changes strengthen the application; they do not configure your Pelican node, certify Discord permissions, or demonstrate live capacity. Use this runbook alongside PELICAN.md and TESTING.md. Supported production runtime: Linux, Python 3.12+, one bot process, one persistent local SQLite database.

## Upgrade a Pelican server safely

1. Start with a separate test bot/server/token. Keep production stopped until staging passes.
2. Back up the existing database consistently and save the matching config. For an older bot without the backup command, stop it and copy the entire data directory. Restrict/encrypt backups, especially any containing token files.
3. Stop the bot. Upload the new `main.py`, the **entire** `statezero/` folder, `requirements.txt`, and `requirements.lock`. The new operations.py and recovery.py are required. Upload tests/ and tools/ if you want to execute local checks there. Preserve your existing config.json, secrets/, and data/.
4. Use the egg's maintained Python 3.12+ image and install dependencies. The lock requires hashes and binary wheels, refusing an unreviewed source-build fallback. requirements.txt now forwards to the hash-verified full lock, so the generic egg's normal requirements installer works. Disable additional unreviewed package installation, automatic upgrades, and automatic pulls from an unreviewed Git branch.
5. Keep the script name `main.py` and the working directory `/home/container`. Use `python -u /home/container/main.py`, or the same virtual-environment interpreter used for installation. The bot needs no incoming application port.
6. Populate announcement_channel_ids. **An empty list now disables announcements** unless you explicitly set allow_any_announcement_channel true. Keep that override false for public deployments. Remove any obsolete dm config field. New settings below have defaults and do not require replacing your existing config.
7. Restrict the audit channel: explicitly deny @everyone View Channel and allow only the bot, configured bot-staff roles, or roles with Manage Server/Administrator. Ordinary member grants are refused for audit publication. A missing or unsafe configured channel makes the health check fail. Do not grant the bot Administrator.
8. Confirm token-file permissions 600 and secret-folder permissions 700. An external provider-managed read-only secret mount is preferable. The secrets/discord_token fallback needs no added egg variable but remains accessible to sufficiently privileged panel/node users and may be included in backups. Never use the Git Token field for Discord. Manual uploads of this public repository need no Git token; leave that field blank when unused.
9. Validate offline, start once, inspect /bot-status, then complete the live TESTING.md gates. Keep only one process with this token; file ownership prevents duplication on the same database, not other databases/nodes using that token.

The database migration preserves older ticket/lock records and adds request state and guild binding. Take a pre-upgrade backup. Do not run an older bot against the migrated database; a rollback requires stopping the new bot and restoring the matching older backup/config. SQLite state cannot recreate Discord channels created/deleted since a backup, so inspect channel differences after any restore.

## Runtime budgets

These settings belong in config.json. They are conservative policies, not measured Discord throughput or member-capacity promises.

| Setting | Default | Purpose |
| --- | --- | --- |
| limits.max_concurrent_actions | 32 | Member actions in progress; overload is rejected before API work. |
| limits.interactions_per_minute | 180 | Shared member-action window. |
| limits.interactions_per_user_per_minute | 20 | Per-member fairness; rejected repeat users do not consume the global window. |
| Reserved staff admission | 8 concurrent / 60 per minute | Separate capacity for staff controls; actions still fetch fresh role membership and permission bits. Grant-time community-role definitions are also fetched again. |
| limits.max_concurrent_transcripts | 2 | Bounds history fetching and memory. |
| limits.max_open_tickets | 100 | Includes pending requests and interrupted closes. |
| limits.max_tickets_per_hour | 60 | Includes unresolved/failed requests to discourage retry abuse. |
| limits.ticket_reopen_seconds | 600 | Durable per-member request cooldown. |
| limits.verification_challenges_per_minute | 30 | Image generation/verification budget. |
| limits.welcome_messages_per_minute | 20 | Limits Discord welcome traffic. |
| operations.action_timeout_seconds | 60 | Stops stalled action workflows; inspect effects before retrying. |
| operations.health_interval_seconds | 15 | Private aggregate snapshot interval. |
| operations.recovery_interval_seconds | 300 | Reconciliation interval, also run on ready/reconnect. |

Database work uses one executor with at most 64 pending calls. Cancelled submissions may finish durable writes on that worker; this preserves recovery state instead of losing an intent at a timeout. Health/audit metric windows are bounded. Discord audit notifications are batched through a 100-event queue; every successfully saved SQLite audit event persists even if a notification is dropped. Queue drops and unexpected background-task failures are visible in health counters. Role grants, role changes, and announcement sends record a durable audit intent before their Discord write, so an unavailable audit store stops those actions. Full disk/storage errors still require operator action.

Member count is not a workload measure. Agree peak joins, verification attempts, role updates, ticket requests, and announcements before tuning. Test busy playtests/releases on the actual host. The current SQLite/gateway architecture is one process; more replicas require a different shared persistence/coordination design. Discord still limits channel counts and API traffic. Archived tickets must be cleaned up under your retention policy or category capacity will eventually block new tickets.

## Health collection and alerts

Run this **inside the same container**, through the provider's monitoring/terminal workflow:

```bash
python /home/container/main.py --health-check --health-file /home/container/data/health.json
```

Exit 0 means the latest snapshot is no more than 60 seconds old, the gateway is ready, and configuration/recovery checks report healthy. Exit 1 means unavailable/invalid/stale/disconnected or an operational issue needs review. It does not log into Discord, load config/token, acquire the database lock, or start another bot. A health file alone is not an external uptime guarantee and the check can lag failure by its freshness window.

The private 600-mode JSON file contains release version, readiness, verification-pause flag, sampled action-duration p95 (last 500 accepted actions including failed ones), sampled loop-lag p95 (last 120 monitoring ticks), database-call p95 (last 500 completions including queue wait), queue depth, and aggregate action/storage/recovery/configuration counts. It contains no member IDs, message bodies, config contents, or token. These are process-local samples, not a cumulative SLA, exact acknowledgement timings, or measurements of every latency spike. Counters reset on process restart. Action timing ends when a workflow completes or fails; it does not include a human waiting between modal/button interactions.

Ask the provider to run the check periodically and deliver an alert after repeated failures. Also alert on CPU/RAM/disk pressure, disconnects, verification pauses, unexpected action failures/timeouts, audit queue drops, sustained loop/database latency, and repeated safety-limit rejections. Keep the file private rather than exposing an unauthenticated HTTP endpoint. Use host/panel monitoring for CPU/RAM/disk. The repository does not send external alerts or configure provider accounts.

The optional standalone Docker image has a healthcheck. Docker's restart policy does not automatically restart every merely unhealthy container; arrange monitoring/operator action. Pelican does not necessarily execute an image healthcheck, so verify your provider's behavior.

## Consistent backups and restore

The backup command uses SQLite's backup API, including committed WAL transactions. Use a **new filename** each time:

```bash
python /home/container/main.py --backup /home/container/backups/statezero-2026-10-11.db --database-file /home/container/data/statezero.db
```

The snapshot is mode 600, verified with SQLite quick_check, and existing destinations are refused. It works with the bot running and does not load the token or connect to Discord. It does not encrypt, upload, schedule, or back up config/secrets/Discord messages. Schedule through your provider, cap backup storage, keep the matching config/release details, and encrypt/restrict off-node copies. Never assume a live copy of only the .db file includes the WAL.

To restore in staging: stop the bot, preserve the old data directory, restore a verified snapshot as data/statezero.db with runtime ownership/mode 600, and ensure old WAL/SHM sidecars from a different database are not reused. Restore the matching config and separately provision the correct secret. Start once and inspect recovery. Confirm ticket ownership, lock state, and existing panels; test node reboot too. Do not overwrite a running database or blindly delete Discord channels during reconciliation.

## Interrupted ticket operations

Creation commits a random request ID, owner, support-role snapshot, parent, category, and timestamp before POSTing the channel. The topic contains the exact request marker. API timeouts or a failed final write retain that intent. Recovery takes a fresh channel list and adopts exactly one matching private channel. It never creates another channel on behalf of an uncertain request and never adopts an arbitrary channel solely because its name looks like a ticket.

Closing records `closing` before revoking creator access, then marks it closed only after Discord reports success. Recovery repeats an interrupted revoke safely before completing the record. Unavailable permissions or suspicious channel overwrites leave state unresolved and notify through status/health rather than silently claiming success.

When recovery needs attention:

1. Run `/ticket-recovery` as bot staff/Manage Server. Inspect the pending IDs and Discord channels. Keep topics intact; do not alter IDs to force adoption.
2. For duplicate markers, wrong parents, or broadened permissions, repair the underlying channel/config through authorized staff review, then run recovery again. New tickets pause when unsafe tracked channels or ambiguous candidates are detected. Recovery never blindly deletes them. Legacy untracked channels without saved requests need manual investigation.
3. If no matching channel exists after careful inspection, run `/ticket-recovery request_id:<ID>` and confirm release. The bot fetches fresh staff access, requires an age of at least three minutes, and fetches channels again before release. A visible matching marker blocks release. The request/cooldown history remains durable.
4. A channel appearing later for a released ID is archived and recorded closed during recovery; its creator's explicit access is revoked. It is not deleted or made into a second open ticket. Unknown markers are ignored, and extras using completed markers require review.

Discord channel creation and SQLite are separate systems, so there is no claim of atomic exactly-once API execution. Durable intent plus conservative reconciliation handles the observable states without guessing after an ambiguous response. Successful adoption cannot restore an opening message that was never sent; the slash close/transcript commands still work in the tracked ticket.

## Public-release gate

Run the exact release's automated checks and advisory scan, then demonstrate the access/privacy tests, realistic traffic, gateway endurance/reconnect, restart/node reboot, consistent restore, token rotation, and interrupted-ticket workflows on the actual Pelican host. Have another qualified developer review the release. Keep a responding operator, protected accounts/secrets/backups, AutoMod/Rules Screening, appropriate Discord role hierarchy, and a documented data/ticket retention policy.

Start with a small pilot and include a busy period before expanding. Do not interpret passing tests, an idle 72 hours, or the local simulation as certification for any number of members. No bot can guarantee perfect security, stop all spam, or hide usable runtime secrets from a malicious host administrator. Application hardening and host operations must both be maintained.
