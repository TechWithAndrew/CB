import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import discord
from pydantic import ValidationError

from .bot import StudioBot
from .config import load_config
from .security import UserError, image_url, read_token
from .storage import Store


class RedactedFormatter(logging.Formatter):
    def __init__(self, secret: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secret = secret

    def format(self, record):
        return super().format(record).replace(self.secret, "[REDACTED]")


async def offline_check(config):
    # Exercise real Discord command serialization and component construction without login.
    import tempfile

    from .bot import Compose, RolePanel, TicketControls, TicketPanel, VerificationPanel

    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "check.db")
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
        store.close()


def main():
    parser = argparse.ArgumentParser(description="State Zero Games community bot")
    parser.add_argument("--config", default=os.environ.get("BOT_CONFIG", "config.json"))
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate config, commands, and panels offline; no Discord messages sent",
    )
    args = parser.parse_args()
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
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        logging.getLogger("discord").setLevel(logging.WARNING)
        store = Store(Path(os.environ.get("BOT_DATA_DIR", "data")) / "statezero.db")
        try:
            message_content = os.environ.get(
                "BOT_MESSAGE_CONTENT", "false"
            ).lower() in ("true", "1", "yes")
            bot = StudioBot(config, store, message_content=message_content)
            bot.run(token, log_handler=None)
        finally:
            store.close()
        return 0
    except ValidationError as error:
        for item in error.errors(include_input=False, include_url=False):
            print(
                f"Config error at {'.'.join(str(x) for x in item['loc'])}: {item['msg']}",
                file=sys.stderr,
            )
    except (UserError, FileNotFoundError) as error:
        print(str(error), file=sys.stderr)
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


if __name__ == "__main__":
    raise SystemExit(main())
