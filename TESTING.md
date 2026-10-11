# State Zero Games: testing and public rollout

The current bot has passing automated tests and basic security controls. It is not yet verified for a large public Discord server. Use this checklist to collect evidence on the exact code version, server permissions, and host you intend to deploy. There is no test that proves a system is perfectly secure forever.

Server member count alone does not determine capacity: peak joins, command clicks, concurrent ticket requests, and the hosting resources matter more. One bot instance is supported. Do not start multiple copies sharing SQLite to increase capacity.

## Your initial deployment: under 5,000 members on a VPS

One bot process with SQLite is a reasonable starting design for this target, subject to the traffic tests below. A VPS with roughly 2 vCPU, 2 GB RAM, and SSD storage is a practical starting budget, not a measured minimum or a capacity guarantee. Monitor the actual deployment before buying more resources or changing the architecture.

- Use a maintained Linux release and promptly apply security updates. Keep the bot under a dedicated unprivileged user or the restricted container in deploy/compose.yaml.
- Use SSH keys, MFA for the hosting-provider and GitHub accounts, and a host firewall. Confirm key-based access works before disabling SSH password login. Restrict SSH access where practical. The bot needs no inbound application ports; do not expose a Docker daemon, SQLite file, or metrics endpoint publicly.
- Prefer a provider secret manager if available. Otherwise keep the token in a private runtime-owned file outside the repository, with mode `0600`, on protected/encrypted storage. Never put the usable token in a container image or Compose YAML. Protect who can access the host and Docker socket; they can otherwise access runtime secrets.
- Arrange automatic process restart, external uptime/error alerts, CPU/RAM/disk monitoring, consistent encrypted backups, and an operator who receives and can act on alerts. The bot itself does not set these up on an external VPS.
- Use consenting staff/testers in a private server first. A 72-hour small-group pilot is a starting observation period; include a planned busy period or playtest rather than counting idle uptime as a capacity test.
- Pay particular attention to the default raid threshold of 10 joins in 60 seconds: a legitimate launch can trigger it. Test the pause behavior and staffing response, then set the threshold based on observed legitimate traffic. Do not turn off protection or increase every limit just to avoid testing failures.

## Prepare a private test environment

- [ ] Create a separate test Discord server and a separate test bot/token. Do not use the production token for development.
- [ ] Have consenting human testers exercise these roles: unverified member, verified member, ticket support, bot staff, and ordinary member outside another person's ticket. Testers can change roles between cases; use a second member account for privacy checks. Do not test only as the server owner or Administrator, who can bypass channel restrictions.
- [ ] Use the intended hosting provider and a persistent private data directory. Enter the token through its encrypted secret store or a private mounted file, never chat, source code, screenshots, or command arguments.
- [ ] Configure real server/role/channel IDs using README.md. Keep staff and administrator roles above the bot's role. Grant only the necessary bot permissions, not Administrator.
- [ ] Record the Git commit, config version (without secrets), host CPU/RAM/storage, tester roles, test date, outcome, and any relevant message/channel IDs.
- [ ] Run the automated checks first:

  ```bash
  python -m pip check
  python -m statezero --config config.example.json --check
  python -m unittest discover -s tests -v
  ```

  Use `.venv/bin/python` for local virtual-environment installations. All tests must execute and pass; also inspect GitHub Actions for the exact commit. A successful offline check does not verify live permissions.

## Mandatory live feature and access tests

| Test | Actions | Required outcome |
| --- | --- | --- |
| Community access | Join without Verified; inspect rules, welcome, verification, and community categories. | Only intended onboarding channels are visible. Community categories remain hidden. |
| Verification success | Accept the server rules, obtain the image challenge, and submit its correct code. | Only the configured Verified role is granted. Community access then works. No staff/support role is granted. |
| Verification failures | Try a wrong answer, exhaust attempts, wait for expiry, and resubmit a previously successful answer. Test below the configured account-age threshold using a legitimate tester when feasible. | Failed, expired, reused, or ineligible challenges do not grant roles. A fresh eligible challenge can still succeed. |
| Accessible verification | Have staff follow the documented manual verification procedure for a legitimate member needing an alternative. | The member can get help without exposing broad self-service privileges. |
| Verification pause | Use `/verification-lock locked:true`, try verification, restart, try again, then resume as staff. | New grants stay blocked across restart. Only staff can resume. Already-verified access is unaffected. |
| Automatic join-spike pause | In staging only, temporarily set the join-spike threshold low enough for two consenting testers to trigger within its window. Restore the intended threshold afterward. | The bot automatically pauses verification, records the event, and stops welcome messages. A restart does not clear the lock. |
| Role menus | Select one role, multiple roles, change the selection, and clear it. Try an unsafe staff role in a test configuration. | Only menu roles change; unrelated roles survive. Clearing removes menu roles. Unsafe grants are rejected. Unverified members cannot use these features with verification gating enabled. |
| Announcement formatting | Fill in paragraphs, blank lines, Markdown, an image attachment or URL, and a destination. Preview, cancel, then submit a new preview. | Preview/cancel publishes nothing. Confirm sends exactly the intended content and image to the intended channel. |
| Announcement notifications | Send without pings, with one approved role, and with multiple approved roles. Put `@everyone` text in the body. | Only explicitly selected approved roles receive a notification. Non-ping posts notify nobody. The body cannot create an everyone/user ping. |
| Staff authorization | As an ordinary member, try staff commands. As temporary bot staff without Manage Server, open a preview, remove all bot-staff/Manage Server access, then confirm. | Ordinary members cannot publish panels/announcements or change the lock. Revoked staff cannot send the old preview. |
| Preview ownership/reuse | Attempt repeated confirmation of the same preview; where possible verify another tester cannot operate it. | There is at most one send from a successful preview, and another staff member cannot use it. Inspect the destination before retrying after an uncertain network error. |
| Ticket privacy | Open each configured ticket category. Inspect access as its creator, configured support, and a different ordinary member. | Creator and intended support can use the ticket. The other member cannot see or read it. Administrator access is expected. |
| Ticket creation limits | Request a second ticket, make simultaneous requests from consenting testers, and lower staging quotas to exercise open/hourly limits and reopen cooldowns. | No duplicate open ticket per creator and no quota overrun. Busy/quota responses are clear; ordinary commands continue working. Restore intended production limits afterward. |
| Ticket close/transcript | Export a transcript, close through confirmation, inspect access, and attempt another ticket before/after the cooldown. Test a transcript with the optional content intent enabled. | Authorized users alone receive transcripts. Closing removes the ordinary creator's access and retains support access. Content-intent status is accurate; configured cooldowns work. |
| Existing panels after restart | Keep role/verification/ticket panels and an open ticket, restart the process, then operate them. | Persistent controls still respond and ticket ownership/verification lock state persist. |
| Removed features | Inspect registered commands after startup sync. | `/dm` and `/dm-preferences` are absent. |

Any unauthorized role grant, announcement, transcript access, or ticket visibility is a launch blocker. Fix it and repeat the affected tests. Do not accept a security failure as a harmless configuration warning.

## Host, recovery, and incident tests

- [ ] Restart the host, not just the bot process. The service starts once, uses the intended configuration/secret, and reads the same persistent database.
- [ ] Remove the bot's required permissions temporarily in staging. Failures are clear, no private channel becomes public, and service recovers after permissions are restored.
- [ ] Interrupt a ticket operation in staging and inspect both Discord channels and SQLite state. Test interruption during channel creation and closing, not just an idle restart.
- [ ] Restore a consistent database backup on a stopped staging bot and verify ticket ownership and lock state. Use SQLite's backup API, or a documented consistent stopped-service backup procedure; copying a live WAL-mode database file alone may lose recent records. Database backups do not recreate deleted Discord channels.
- [ ] Rotate the **test bot** token in the Developer Portal, update the hosting secret, restart, and confirm reconnection. Establish who can do this quickly during an incident. Never print old/new tokens to test it.
- [ ] Simulate a short network disruption on the test host and verify reconnection, useful logs, responsive commands afterward, and no duplicate sends from manual retries.
- [ ] Check that logs, backups, Git history, container image/context, and shared screenshots contain no real token. Protect backups and downloaded transcripts with host access controls.
- [ ] Check that private audit messages are visible only to intended staff. Set retention procedures for ticket channels, audit-channel messages, database records, and downloaded transcripts.
- [ ] Confirm host uptime/error monitoring alerts an authorized operator and that the operator can pause verification and investigate. Alerts are a hosting/operations requirement; they are not configured by this repository.

**Known recovery limitation:** Discord channel creation and the SQLite write are separate operations. A process crash after Discord creates a channel but before SQLite records it can leave an orphan channel. The bot does not currently automatically reconcile these orphans. Test and document safe staff cleanup; for a large public launch, add and validate stronger crash recovery before relying on unattended ticket operations. Similar interrupted operations must be checked for stale state. Never delete channels blindly during recovery.

## Capacity testing and monitoring

Agree the target workload before running tests: expected peak joins/minute, verification attempts/minute, role updates/minute, ticket requests/minute, and announcement frequency. The configured defaults intentionally pause or reject some bursts; they are safety limits, not claims of measured capacity.

1. Exercise realistic bursts locally with mocked Discord calls or a dedicated test harness. Include invalid requests and simultaneous actions, and test at least twice the planned peak workload as an initial headroom goal.
2. Confirm actual integration behavior with a modest number of consenting testers in the private server. Do not automate normal user accounts, create self-bots, or flood Discord's API. A local simulation does not establish Discord's real API throughput.
3. Measure interaction acknowledgement time, ordinary command completion time, CPU, RAM, event-loop delays, database errors/lock waits, disconnects, and Discord rate-limit incidents. The current bot does not expose a metrics dashboard or measured percentile latency; add instrumentation or use appropriate hosting/tools before claiming load-test results.
4. Document the tested operating envelope and keep the configured limits inside it. Do not increase caps just to make a stress test pass.

Suggested initial acceptance targets, to adapt to the intended workload and host:

| Measurement | Initial target |
| --- | --- |
| Interaction acknowledgement | Within Discord's three-second initial-response deadline for the tested requests. A deferred response is an acknowledgement, not completion. |
| Ordinary successful command completion | A provisional 95th percentile under five seconds during normal peak traffic. Report ticket/image/API-dependent actions separately. |
| Safety limits reached | A clear busy/limit response or documented welcome suppression; no duplicate channel, unintended role, privacy leak, crash, or growing ticket-creation backlog. |
| CPU/RAM at expected normal peak | Stable usage with practical headroom; approximately 30% spare capacity is a starting planning target, not a guarantee. |
| After a burst | Processing recovers, memory does not keep growing under steady activity, and existing controls still work. |
| Persistence | No lost/cross-owned open-ticket record or lost verification lock in restart/backup tests. Interrupted-operation exceptions must have tested recovery. |

Fix unexplained failures. SQLite operations are synchronous and multi-process coordination is absent, so measured results may require architectural changes. Adding RAM or switching programming languages is not proof that a bottleneck or vulnerability is resolved.

## Public-release decision

- [ ] All mandatory access/privacy tests pass with actual ordinary/support/staff accounts.
- [ ] All automated checks and the dependency advisory scan pass on the exact release commit.
- [ ] A second qualified developer reviews authorization, role assignment, ticket permissions, secret handling, and recovery behavior. No unresolved critical/high-severity issue remains.
- [ ] Live restart, reconnect, consistent backup/restore, token rotation, and interrupted-ticket recovery have been demonstrated.
- [ ] Recorded traffic measurements meet agreed targets with headroom; the host has monitoring, alert routing, and an operator who can respond.
- [ ] AutoMod, Rules Screening, server channel gates, staff 2FA, and minimum bot permissions are configured.
- [ ] A small pilot group uses the deployment successfully. A clean 72-hour pilot is a useful first observation period, but time alone does not prove readiness. Keep watching during busier playtests/releases.
- [ ] The studio owner and reviewer record the release commit, tested host/workload, known limitations, retention/recovery procedures, and decision to expand.

Launch gradually and repeat relevant checks after code, permissions, config, dependency, or hosting changes. Readiness applies to the tested configuration and workload, not an unlimited number of members or all future threats.
