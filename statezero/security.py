import hmac
import os
import secrets
import stat
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import discord


class UserError(Exception):
    """An expected error safe to show to a Discord user."""


def read_token() -> str:
    path = os.environ.get("DISCORD_TOKEN_FILE")
    environment_token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not path and not environment_token:
        default_file = Path("secrets") / "discord_token"
        if default_file.exists():
            path = str(default_file)
    if path:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise UserError(
                "Token file is unavailable, unreadable, or a symbolic link."
            ) from None
        with os.fdopen(fd, "rb") as file:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise UserError("Token secret must be a regular file.")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise UserError("Token file must be private (chmod 600).")
            if metadata.st_size > 4096:
                raise UserError("Token file is too large.")
            try:
                token = file.read(4097).decode("ascii").strip()
            except UnicodeDecodeError:
                raise UserError(
                    "Token file must contain only the Discord token."
                ) from None
    else:
        token = environment_token
    if not token:
        raise UserError(
            "Set DISCORD_TOKEN through your hosting secret store, set DISCORD_TOKEN_FILE to a private secret file, or create secrets/discord_token with permissions 600."
        )
    if (
        len(token) > 4096
        or not token.isascii()
        or not token.isprintable()
        or any(character.isspace() for character in token)
    ):
        raise UserError(
            "Token must contain only a single credential without internal whitespace."
        )
    return token


def image_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or len(url) > 1500
    ):
        raise UserError("Images must use a valid HTTPS URL without credentials.")
    # We pass URLs to Discord; the bot never fetches an arbitrary user URL.
    return url


def safe_role(guild: discord.Guild, role_id: int, *, fresh_roles=None) -> discord.Role:
    role = (
        fresh_roles.get(role_id) if fresh_roles is not None else guild.get_role(role_id)
    )
    if role is None or guild.me is None:
        raise UserError("A configured role or the bot's member record is missing.")
    permitted = discord.Permissions(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        embed_links=True,
        attach_files=True,
        add_reactions=True,
        external_emojis=True,
        external_stickers=True,
        connect=True,
        speak=True,
        stream=True,
        use_voice_activation=True,
        change_nickname=True,
        use_application_commands=True,
        create_public_threads=True,
        create_private_threads=True,
        send_messages_in_threads=True,
        use_embedded_activities=True,
        send_polls=True,
    )
    if role.is_default() or role.managed or role >= guild.me.top_role:
        raise UserError("This role is managed, @everyone, or above the bot's role.")
    if role.permissions.value & ~permitted.value:
        raise UserError(
            "Verification and self-service roles cannot have staff permissions."
        )
    if not guild.me.guild_permissions.manage_roles:
        raise UserError("The bot needs Manage Roles to assign this role.")
    return role


class Cooldowns:
    def __init__(self, capacity=10000):
        self.entries = OrderedDict()
        self.capacity = capacity

    def take(self, key: tuple, seconds: float):
        now = time.monotonic()
        if self.entries.get(key, 0) > now:
            raise UserError("Please wait before trying this action again.")
        self.entries[key] = now + seconds
        self.entries.move_to_end(key)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)


class RateWindow:
    """Bounded process-local sliding window; no task queues or unbounded join storage."""

    def __init__(self, limit: int, seconds: float):
        self.limit, self.seconds = limit, seconds
        self.events = deque()

    def take(self):
        now = time.monotonic()
        while self.events and self.events[0] <= now - self.seconds:
            self.events.popleft()
        if len(self.events) >= self.limit:
            raise UserError(
                "This feature is temporarily busy. Please try again in a minute."
            )
        self.events.append(now)


@dataclass
class Challenge:
    digest: bytes
    expires: float
    attempts: int = 0


class Challenges:
    def __init__(self, lifetime: int, attempts: int):
        self.lifetime, self.max_attempts = lifetime, attempts
        self.key = secrets.token_bytes(32)
        self.pending: dict[int, Challenge] = {}

    def digest(self, user_id: int, answer: str):
        return hmac.digest(
            self.key, f"{user_id}:{answer.strip().upper()}".encode(), "sha256"
        )

    def create(self, user_id: int) -> str:
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if v.expires > now}
        if len(self.pending) >= 10000:
            raise UserError("Verification is busy. Please try again shortly.")
        code = "".join(
            secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)
        )
        self.pending[user_id] = Challenge(
            self.digest(user_id, code), now + self.lifetime
        )
        return code

    def check(self, user_id: int, answer: str) -> bool:
        challenge = self.pending.get(user_id)
        if not challenge or challenge.expires <= time.monotonic():
            self.pending.pop(user_id, None)
            raise UserError("This challenge expired. Start verification again.")
        challenge.attempts += 1
        success = hmac.compare_digest(challenge.digest, self.digest(user_id, answer))
        if success or challenge.attempts >= self.max_attempts:
            del self.pending[user_id]
        if not success and challenge.attempts >= self.max_attempts:
            raise UserError(
                "Too many attempts. Start a new challenge after the cooldown."
            )
        return success
