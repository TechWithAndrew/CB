# Local release validation: 0.2.0

Validated on October 11, 2026. These are local automated checks and simulations, not a live Discord/Pelican certification or an independent audit. The repository has not been given a production token or access to the user's Pelican host.

## Checks

- 83 automated tests pass in a fresh Python 3.12 environment installed from the hash-verified runtime lock.
- Offline configuration, real slash-command serialization, and persistent component construction pass. No Discord login or messages are involved.
- pip check reports no broken runtime requirements. Ruff syntax/import/async checks and formatting pass.
- A pip-audit scan of all 16 pinned runtime packages reports no known vulnerabilities. Advisory coverage does not establish absence of unknown/application/OS issues.
- Hash-verified dependency installation works through requirements.txt's forwarding to requirements.lock. The lock requires hashes and binary wheels; source-build fallback is refused.
- Forced process exit retains the committed ticket request; cross-process database ownership is rejected; legacy ticket/lock state migrates; a consistent live backup includes WAL state and restores successfully.
- Recovery tests cover missing/duplicate/forged markers, unexpected parents/permissions, database completion failure after creation, uncertain API failure, failed permission updates during closing, staff release checks, late channels, and idempotent adoption without deletion.
- Authorization tests cover stale cached permission bits, fresh role definitions, current owner metadata, revoked staff/support access, non-owner previews, archived transcripts, storage failure before a role grant, and unsafe audit destinations. Token tests cover file permissions, symlinks/FIFOs, oversized/control-character values, and credential redaction including temporary webhook URLs.
- Overload tests cap active work, reserve staff capacity, bound per-user/global windows, cancel/drain sibling reads, retain submitted durable writes on cancellation, observe background failures, and stop transcripts at their size/concurrency bounds.

## Restricted container check

An offline smoke image is derived from deploy/Dockerfile using the same code/runtime pins, a downloaded **hash-verified wheelhouse**, and extra test/example files. The production template's network download is replaced with an offline installer for this check. Run as UID 10001 with networking disabled, a read-only root filesystem, all Linux capabilities dropped, no-new-privileges, a private tmpfs, a 128-PID cap, one CPU, and 256 MB RAM. Passing this test demonstrates the automated suite's behavior under these restrictions; it does not establish production resource requirements or validate the provider's exact egg/image/settings. No runtime secret is embedded in the smoke image.

The actual Pelican deployment and normal production-image download/build must still be validated by its operator. Pelican's generic egg does not inherit the optional Compose template's filesystem/capability settings automatically.

## Local overload simulation

The harness exercises the admission controller, two real SQLite operations per admitted action, and a 10 ms sleep representing an API response. It does not exercise the full feature workflows, network rate limits, gateway traffic, or Discord itself. One shared-runner observation:

| Measurement | Baseline | Overload |
| --- | --- | --- |
| Requested actions | 120 | 5,000 |
| Burst concurrency | 16 | 64 |
| Accepted/completed | 120 / 120 | 180 / 180 |
| Rejected by configured policies | 0 | 4,820 |
| Peak active member actions | 16 | 32 |
| Active work after completion | 0 | 0 |
| Action duration p95 | 0.0362 s | 0.0404 s |
| Database-call p95 | 0.0223 s | 0.0066 s |
| Sampled loop-lag p95 | 0.0027 s | 0.0071 s |

The 180 admissions in the overload case match the default per-minute window; 5,000 attempts were **not** 5,000 successful Discord actions or members. Timings vary with runner scheduling/caching and are not SLA guarantees. Tracemalloc covers harness allocations after imports, not total resident bot memory; use actual host CPU/RAM measurements for sizing.

Reproduce safely, without a token:

```bash
python tools/load_test.py --actions 120 --concurrency 16 --output baseline.json
python tools/load_test.py --actions 5000 --concurrency 64 --output overload.json
```

The harness performs no Discord calls or member actions. CI repeats the default overload check; its run status must be inspected independently for the release commit.

## Still required for public release

Validate the live permission/privacy cases, real expected traffic, gateway reconnect/endurance, node reboot, alerts delivered to an operator, protected host/accounts/secrets/backups, token rotation, and staging restore/recovery. Obtain a second qualified developer's review. Start with a small pilot including a busy period. See TESTING.md and PRODUCTION.md. There is no demonstrated unlimited member/traffic capacity, distributed deployment support, or guarantee of perfect security.
