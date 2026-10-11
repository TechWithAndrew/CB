import argparse
import asyncio
import json
import logging
import os
import re
import resource
import sqlite3
import sys
import time
from pathlib import Path

import discord
from pydantic import ValidationError

from . import __version__
from .bot import StudioBot
from .config import load_config
from .operations import write_health
from .security import UserError, image_url, read_token
from .storage import AsyncStore, backup_database


class RedactedFormatter(logging.Formatter):
    def __init__(self, secret: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secret = secret

    def format(self, record):
        text = super().format(record).replace(self.secret, "[REDACTED]")
        return re.sub(r"(/webhooks/\d+/)[^/?\s]+", r"\1[REDACTED]", text)


async def offline_check(config):
    # Exercise real Discord command serialization and component construction without login.
    import tempfile

    from .bot import Compose, RolePanel, TicketControls, TicketPanel, VerificationPanel

    with tempfile.TemporaryDirectory() as directory:
        store = AsyncStore(Path(directory) / "check.db")
        bot = StudioBot(config, store)
        for command in bot.tree.get_commands():
            command.to_dict(bot.tree)
        views = [VerificationPanel(bot), TicketControls(bot)]
        views.extend(RolePanel(bot, menu) for menu in config.role_menus)
        if config.ticket_categories:
            views.append(TicketPanel(bot))
        for view in views:
            if not view.is_persistent():
                raise UserError(
                    "A community panel is missing persistent component IDs."
                )
            view.to_components()
        Compose(bot, 1).to_components()
        for view in views:
            view.stop()
        await bot.close()
        await store.close()


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description="State Zero Games community bot")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default=os.environ.get("BOT_CONFIG", "config.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate config, commands, and panels offline; no Discord messages sent",
    )
    mode.add_argument(
        "--health-check",
        action="store_true",
        help="Check the private local health snapshot without reading the token or contacting Discord",
    )
    mode.add_argument(
        "--backup",
        type=Path,
        metavar="NEW_FILE",
        help="Create a private consistent SQLite backup without loading config/token or logging into Discord",
    )
    parser.add_argument(
        "--database-file",
        type=Path,
        default=Path(os.environ.get("BOT_DATA_DIR", "data")) / "statezero.db",
    )
    parser.add_argument(
        "--health-file",
        default=str(Path(os.environ.get("BOT_DATA_DIR", "data")) / "health.json"),
    )
    parser.add_argument("--max-health-age", type=int, default=60)
    args = parser.parse_args()
    if args.health_check:
        return health_check(Path(args.health_file), args.max_health_age)
    if args.backup:
        try:
            backup_database(args.database_file, args.backup)
            print(
                "Consistent database backup created; keep it private and encrypt off-host storage."
            )
            return 0
        except (UserError, OSError, sqlite3.DatabaseError) as error:
            print(
                f"Backup failed ({type(error).__name__}); check paths, permissions, source integrity, and that the destination is new.",
                file=sys.stderr,
            )
            return 1
    try:
        config = load_config(args.config)
        image_url(config.branding.welcome_image)
        if args.check:
            asyncio.run(offline_check(config))
            print(
                "Offline checks passed: config, slash command serialization, and persistent panels."
            )
            if not config.guild_id:
                print(
                    "Configure guild_id and real role/channel IDs before connecting to Discord."
                )
            return 0
        if not config.guild_id:
            raise UserError("Set guild_id in config.json before starting the bot.")
        token = read_token()
        os.umask(0o077)
        handler = logging.StreamHandler()
        handler.setFormatter(RedactedFormatter(token))
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
        logging.getLogger("discord").setLevel(logging.WARNING)
        store = AsyncStore(
            Path(os.environ.get("BOT_DATA_DIR", "data")) / "statezero.db"
        )
        try:
            store.initialized.result()  # Before starting the event loop; verifies ownership/integrity.
            write_health(
                store.path.parent / "health.json",
                {"timestamp": time.time(), "ready": False, "healthy": False},
            )
            message_content = os.environ.get(
                "BOT_MESSAGE_CONTENT", "false"
            ).lower() in ("true", "1", "yes")
            bot = StudioBot(config, store, message_content=message_content)
            bot.run(token, log_handler=None)
        finally:
            store.shutdown()
        return 0
    except ValidationError as error:
        for item in error.errors(include_input=False, include_url=False):
            print(
                f"Config error at {'.'.join(str(x) for x in item['loc'])}: {item['msg']}",
                file=sys.stderr,
            )
    except (UserError, FileNotFoundError) as error:
        print(str(error), file=sys.stderr)
    except (OSError, sqlite3.DatabaseError) as error:
        print(
            f"Host storage failed ({type(error).__name__}). Check permissions, free space, and database integrity.",
            file=sys.stderr,
        )
    except discord.LoginFailure:
        print(
            "Discord rejected the bot token. Rotate it in the Developer Portal and update your hosting secret.",
            file=sys.stderr,
        )
    except discord.PrivilegedIntentsRequired:
        print(
            "Enable Server Members Intent in the Discord Developer Portal.",
            file=sys.stderr,
        )
    except discord.HTTPException as error:
        print(
            f"Discord startup failed (HTTP {error.status}). Check access, guild_id, and bot installation.",
            file=sys.stderr,
        )
    return 1


def health_check(path: Path, max_age: int):
    try:
        if max_age <= 0:
            raise ValueError
        with path.open("rb") as file:
            data = file.read(65537)
        if len(data) > 65536:
            raise ValueError
        payload = json.loads(data)
        timestamp = payload["timestamp"]
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            raise ValueError
        age = time.time() - timestamp
        if (
            not -5 <= age <= max_age
            or payload.get("ready") is not True
            or payload.get("healthy") is not True
        ):
            print(
                "Unhealthy: snapshot is stale, disconnected, or has outstanding operational/configuration issues.",
                file=sys.stderr,
            )
            return 1
        print("Healthy: recent gateway, storage, configuration, and recovery snapshot.")
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("Unhealthy: health snapshot is unavailable or invalid.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
