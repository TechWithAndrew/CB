import asyncio
import functools
import json
import math
import os
import tempfile
import time
from collections import Counter, OrderedDict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from .security import RateWindow, UserError


def percentile(samples, fraction=0.95):
    values = sorted(samples)
    return (
        round(values[max(0, math.ceil(len(values) * fraction) - 1)], 4)
        if values
        else None
    )


async def together(*awaitables):
    """Concurrent state reads always cancel/drain siblings on failure or timeout."""
    tasks = [asyncio.create_task(awaitable) for awaitable in awaitables]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Operations:
    """Bound work before API calls. Only aggregate metrics are retained."""

    def __init__(self, limits, timeout):
        self.limits, self.timeout = limits, timeout
        self.active = self.staff_active = 0
        self.transcripts = 0
        self.rate = RateWindow(limits.interactions_per_minute, 60)
        self.staff_rate = RateWindow(60, 60)
        self.users = OrderedDict()
        self.counts = Counter()
        self.durations = deque(maxlen=500)

    @asynccontextmanager
    async def action(self, interaction, *, staff=False):
        active = self.staff_active if staff else self.active
        cap = 8 if staff else self.limits.max_concurrent_actions
        if active >= cap:
            self.counts["rejected"] += 1
            raise UserError("The bot is busy. Please try again shortly.")
        try:
            user_id = interaction.user.id
            rate = self.users.pop(user_id, None) or RateWindow(
                self.limits.interactions_per_user_per_minute, 60
            )
            self.users[user_id] = rate
            while len(self.users) > 10000:
                self.users.popitem(last=False)
            rate.take()
            (self.staff_rate if staff else self.rate).take()
        except UserError:
            self.counts["rejected"] += 1
            raise
        if staff:
            self.staff_active += 1
        else:
            self.active += 1
        started = time.monotonic()
        self.counts["accepted"] += 1
        try:
            async with asyncio.timeout(self.timeout):
                yield
            self.counts["completed"] += 1
        except TimeoutError:
            self.counts["timeouts"] += 1
            raise UserError(
                "This action timed out. Check its result before trying again; ticket recovery may still be pending."
            ) from None
        except Exception:
            self.counts["failed"] += 1
            raise
        finally:
            self.durations.append(time.monotonic() - started)
            if staff:
                self.staff_active -= 1
            else:
                self.active -= 1

    def snapshot(self):
        return {
            "actions": dict(self.counts),
            "active_actions": self.active + self.staff_active,
            "sample_count": len(self.durations),
            "action_seconds_p95": percentile(self.durations),
            "active_transcripts": self.transcripts,
        }


def guarded(bot=None):
    """Wrap public callbacks, including direct test calls, without internal SDK hooks."""

    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            owner = bot or args[0].bot
            interaction = args[0] if bot else args[1]
            if not interaction.guild or interaction.guild.id != owner.config.guild_id:
                raise UserError(
                    "This action is only available in the configured server."
                )
            # Cached permissions reserve capacity only. The action still fetches fresh authorization.
            user = interaction.user
            permissions = getattr(user, "guild_permissions", None)
            staff = bool(permissions and permissions.manage_guild) or any(
                role.id in owner.config.staff_role_ids
                for role in getattr(user, "roles", [])
            )
            async with owner.operations.action(interaction, staff=staff):
                return await function(*args, **kwargs)

        return wrapped

    return decorate


def write_health(path: Path, payload):
    """Atomic private file; no token, member identifiers, config contents, or messages."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".health-", delete=False
        ) as file:
            temporary = file.name
            json.dump(payload, file, allow_nan=False)
            file.flush()
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
