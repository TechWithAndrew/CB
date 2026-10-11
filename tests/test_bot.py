import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from PIL import Image
from pydantic import ValidationError

from statezero.__main__ import RedactedFormatter, offline_check
from statezero.bot import (
    AnswerModal,
    CloseTicket,
    Compose,
    PingSelect,
    RoleSelect,
    SendPreview,
    StudioBot,
    TicketSelect,
    ticket_member,
    validate_ticket_category,
)
from statezero.captcha import render
from statezero.config import Settings
from statezero.security import (
    Challenges,
    Cooldowns,
    RateWindow,
    UserError,
    image_url,
    read_token,
    safe_role,
)
from statezero.storage import Store


class Role:
    def __init__(self, id_, permissions=None, position=1, managed=False, default=False):
        self.id, self.position, self.managed = id_, position, managed
        self.permissions = permissions or discord.Permissions.none()
        self.default = default
        self.mention, self.mentionable = f"<@&{id_}>", True
        self.name = f"Role {id_}"

    def is_default(self):
        return self.default

    def __ge__(self, other):
        return self.position >= other.position


class Entity(NS):
    __hash__ = object.__hash__


def interaction(guild=None, user_id=42, channel=None):
    return NS(
        user=NS(id=user_id),
        guild=guild,
        channel=channel,
        channel_id=channel.id if channel else None,
        response=NS(
            defer=AsyncMock(),
            send_message=AsyncMock(),
            send_modal=AsyncMock(),
            edit_message=AsyncMock(),
            is_done=MagicMock(return_value=False),
        ),
        followup=NS(send=AsyncMock()),
        message=NS(edit=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def make_guild():
    role = Role(500)
    support = Role(600)
    default = Role(123, default=True)
    me = Entity(
        top_role=Role(900, position=10),
        guild_permissions=discord.Permissions(manage_roles=True, manage_channels=True),
    )
    roles = {500: role, 600: support, 123: default}
    parent = MagicMock(spec=discord.CategoryChannel)
    parent.id = 700
    parent.channels = []
    guild = NS(
        id=123,
        channels=[],
        me=me,
        default_role=default,
        get_role=lambda id_: roles.get(id_),
        get_channel=lambda id_: parent if id_ == 700 else None,
        fetch_member=AsyncMock(),
        fetch_channel=AsyncMock(),
        fetch_roles=AsyncMock(return_value=list(roles.values())),
        create_text_channel=AsyncMock(),
        get_member=MagicMock(return_value=None),
    )
    for r in roles.values():
        r.guild = guild
    return guild, role, support


class SecurityTests(unittest.TestCase):
    def test_rate_window_limits_work_and_expires(self):
        window = RateWindow(2, 60)
        with patch("statezero.security.time.monotonic", return_value=100):
            window.take()
            window.take()
            with self.assertRaises(UserError):
                window.take()
        self.assertEqual(len(window.events), 2)
        with patch("statezero.security.time.monotonic", return_value=161):
            window.take()
        self.assertEqual(len(window.events), 1)

    def test_challenge_replay_attempts_and_expiry(self):
        challenges = Challenges(60, 2)
        code = challenges.create(1)
        self.assertNotEqual(challenges.pending[1].digest, code.encode())
        self.assertFalse(challenges.check(1, "XXXXXX"))
        self.assertTrue(challenges.check(1, code.lower()))
        with self.assertRaises(UserError):
            challenges.check(1, code)
        challenges.create(1)
        self.assertFalse(challenges.check(1, "XXXXXX"))
        with self.assertRaises(UserError):
            challenges.check(1, "XXXXXX")
        code = challenges.create(1)
        challenges.pending[1].expires = time.monotonic() - 1
        with self.assertRaises(UserError):
            challenges.check(1, code)
        self.assertNotIn(1, challenges.pending)

    def test_challenge_user_binding(self):
        challenges = Challenges(60, 2)
        code = challenges.create(1)
        with self.assertRaises(UserError):
            challenges.check(2, code)

    def test_captcha_is_real_image(self):
        with Image.open(render("ABC234")) as image:
            self.assertEqual(image.size, (340, 110))
            self.assertEqual(image.format, "PNG")

    def test_restrict_urls(self):
        for value in (
            "http://example.org/a.png",
            "file:///a",
            "https://user:pass@example.org/a",
        ):
            with self.assertRaises(UserError):
                image_url(value)
        self.assertEqual(
            image_url("https://cdn.discordapp.com/image.png"),
            "https://cdn.discordapp.com/image.png",
        )

    def test_role_permissions_and_hierarchy(self):
        guild, role, _ = make_guild()
        self.assertIs(safe_role(guild, 500), role)
        for permissions in (
            discord.Permissions(administrator=True),
            discord.Permissions(manage_nicknames=True),
            discord.Permissions(mention_everyone=True),
            discord.Permissions(ban_members=True),
        ):
            role.permissions = permissions
            with self.assertRaises(UserError):
                safe_role(guild, 500)
        role.permissions = discord.Permissions.none()
        role.position = 10
        with self.assertRaises(UserError):
            safe_role(guild, 500)
        role.position, role.managed = 1, True
        with self.assertRaises(UserError):
            safe_role(guild, 500)
        with self.assertRaises(UserError):
            safe_role(guild, 123)

    def test_token_file_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("test-secret")
            path.chmod(0o600)
            with patch.dict(os.environ, {"DISCORD_TOKEN_FILE": str(path)}):
                self.assertEqual(read_token(), "test-secret")
                path.chmod(0o644)
                with self.assertRaises(UserError):
                    read_token()

    def test_logging_redacts_secret(self):
        import logging

        formatter = RedactedFormatter("private-token")
        record = logging.LogRecord("test", 20, "", 0, "error private-token", (), None)
        self.assertNotIn("private-token", formatter.format(record))

    def test_cooldown_blocks_and_is_bounded(self):
        cooldowns = Cooldowns(capacity=2)
        cooldowns.take((1,), 60)
        with self.assertRaises(UserError):
            cooldowns.take((1,), 60)
        cooldowns.take((2,), 60)
        cooldowns.take((3,), 60)
        self.assertEqual(len(cooldowns.entries), 2)

    def test_config_prevents_staff_or_verified_self_roles(self):
        for field in ("staff_role_ids", "verification", "ticket_categories"):
            data = {
                "role_menus": [
                    {
                        "key": "test",
                        "title": "test",
                        "max_selected": 1,
                        "options": [{"role_id": 500, "label": "bad"}],
                    }
                ]
            }
            data[field] = {
                "staff_role_ids": [500],
                "verification": {"role_id": 500},
                "ticket_categories": [
                    {"key": "help", "label": "Help", "support_role_ids": [500]}
                ],
            }[field]
            with self.assertRaises(ValidationError):
                Settings.model_validate(data)

    def test_config_rejects_unknown_fields_and_duplicate_keys(self):
        with self.assertRaises(ValidationError):
            Settings.model_validate({"token": "not-allowed"})
        with self.assertRaises(ValidationError):
            Settings.model_validate(
                {"ticket_categories": [{"key": "x", "label": "x"}] * 2}
            )


class StorageTests(unittest.TestCase):
    def test_persistence_lock_and_unique_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = Store(path)
            store.set_setting("verification_locked", "1")
            store.add_ticket(900, 42, "support")
            import sqlite3

            with self.assertRaises(sqlite3.IntegrityError):
                store.add_ticket(901, 42, "bug")
            store.close()
            store = Store(path)
            self.assertEqual(store.setting("verification_locked"), "1")
            self.assertEqual(store.open_ticket(42)["channel_id"], 900)
            store.close_ticket(900)
            store.add_ticket(901, 42, "bug")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            store.close()


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "state.db")
        self.config = Settings.model_validate(
            {
                "guild_id": 123,
                "verification": {"role_id": 500},
                "ping_role_ids": [600],
                "ticket_categories": [
                    {
                        "key": "support",
                        "label": "Support",
                        "parent_id": 700,
                        "support_role_ids": [600],
                    }
                ],
                "role_menus": [
                    {
                        "key": "games",
                        "title": "Games",
                        "max_selected": 2,
                        "options": [
                            {"role_id": 501, "label": "Game A"},
                            {"role_id": 502, "label": "Game B"},
                        ],
                    }
                ],
            }
        )
        self.bot = StudioBot(self.config, self.store)
        self.guild, self.verified, self.support = make_guild()
        self.member = Entity(
            id=42,
            bot=False,
            guild=self.guild,
            roles=[self.verified],
            pending=False,
            created_at=discord.utils.utcnow()
            - __import__("datetime").timedelta(days=10),
            guild_permissions=discord.Permissions.none(),
            mention="<@42>",
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
            send=AsyncMock(),
        )
        self.guild.fetch_member.return_value = self.member
        self.bot.audit = AsyncMock()

    async def asyncTearDown(self):
        await self.bot.close()
        self.store.close()
        self.directory.cleanup()

    async def test_offline_real_command_serialization(self):
        await offline_check(self.config)
        self.assertEqual(len(self.bot.tree.get_commands()), 9)
        self.assertFalse(
            any(
                command.name.startswith("dm")
                for command in self.bot.tree.get_commands()
            )
        )
        self.assertFalse(self.bot._connection._chunk_guilds)
        self.assertEqual(
            self.bot._connection.member_cache_flags, discord.MemberCacheFlags.none()
        )

    async def test_ticket_global_quota_serialized_across_members(self):
        self.bot.config.limits.max_open_tickets = 1
        other = Entity(**{**vars(self.member), "id": 43})
        self.guild.fetch_member.side_effect = lambda id_: (
            self.member if id_ == 42 else other
        )
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.mention, channel.send = 888, "<#888>", AsyncMock()
        self.guild.create_text_channel.return_value = channel
        first, second = TicketSelect(self.bot), TicketSelect(self.bot)
        first._values = second._values = ["support"]
        results = await asyncio.gather(
            first.callback(interaction(self.guild)),
            second.callback(interaction(self.guild, 43)),
            return_exceptions=True,
        )
        self.assertEqual(sum(result is None for result in results), 1, results)
        self.assertEqual(
            sum(isinstance(result, UserError) for result in results), 1, results
        )
        self.guild.create_text_channel.assert_awaited_once()
        self.assertEqual(self.store.count_open_tickets(), 1)

    async def test_ticket_reopen_cooldown_survives_closed_ticket(self):
        self.store.add_ticket(888, 42, "support")
        self.store.close_ticket(888)
        select = TicketSelect(self.bot)
        select._values = ["support"]
        with self.assertRaisesRegex(UserError, "wait before opening"):
            await select.callback(interaction(self.guild))
        self.guild.create_text_channel.assert_not_awaited()

    async def test_hourly_ticket_quota_counts_closed_tickets(self):
        self.bot.config.limits.max_tickets_per_hour = 1
        self.store.add_ticket(888, 99, "support")
        self.store.close_ticket(888)
        select = TicketSelect(self.bot)
        select._values = ["support"]
        with self.assertRaisesRegex(UserError, "temporarily limited"):
            await select.callback(interaction(self.guild))
        self.guild.create_text_channel.assert_not_awaited()

    async def test_ticket_category_capacity_blocks_creation(self):
        self.guild.get_channel(700).channels = [None] * 49
        select = TicketSelect(self.bot)
        select._values = ["support"]
        with self.assertRaisesRegex(UserError, "channel limit"):
            await select.callback(interaction(self.guild))
        self.guild.create_text_channel.assert_not_awaited()

    async def test_repeated_verify_does_not_consume_global_budget(self):
        self.member.roles = []
        with patch("statezero.bot.render", return_value=render("ABC234")):
            await self.bot.begin_verification(interaction(self.guild))
        with self.assertRaises(UserError):
            await self.bot.begin_verification(interaction(self.guild))
        self.assertEqual(len(self.bot.verification_rate.events), 1)

    async def test_welcome_limit_and_pause_suppress_messages(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        self.guild.get_channel = lambda id_: channel if id_ == 777 else None
        self.bot.config.welcome_channel_id = 777
        self.bot.welcome_rate = RateWindow(2, 60)
        self.member.display_name = "Player"
        self.member.display_avatar = NS(url="https://example.com/avatar.png")
        self.guild.member_count, self.guild.name = 50000, "Test studio"
        for _ in range(3):
            await self.bot.on_member_join(self.member)
        self.assertEqual(channel.send.await_count, 2)
        self.store.set_setting("verification_locked", "1")
        for _ in range(1000):
            await self.bot.on_member_join(self.member)
        self.assertEqual(channel.send.await_count, 2)
        self.assertLessEqual(
            len(self.bot.joins), self.bot.config.verification.raid_join_count
        )

    async def test_guard_staff_verified_guild(self):
        it = interaction(self.guild)
        with self.assertRaises(UserError):
            await self.bot.member(it, staff=True)
        self.member.guild_permissions.manage_guild = True
        self.assertIs(await self.bot.member(it, staff=True), self.member)
        self.member.roles = []
        with self.assertRaises(UserError):
            await self.bot.member(it, verified=True)
        with self.assertRaises(UserError):
            await self.bot.member(interaction(NS(id=999)))

    async def test_verification_grants_role_once_and_rechecks_lock(self):
        self.member.roles = []
        code = self.bot.challenges.create(42)
        modal = AnswerModal(self.bot, 42)
        modal.answer._value = code
        await modal.on_submit(interaction(self.guild))
        self.member.add_roles.assert_awaited_once_with(
            self.verified, reason="Completed community verification"
        )
        with self.assertRaises(UserError):
            await modal.on_submit(interaction(self.guild))
        self.store.set_setting("verification_locked", "1")
        self.bot.challenges.create(42)
        with self.assertRaises(UserError):
            await modal.on_submit(interaction(self.guild))
        self.assertEqual(self.member.add_roles.await_count, 1)

    async def test_role_select_only_changes_menu_roles(self):
        role_a, role_b = Role(501), Role(502)
        original_lookup = self.guild.get_role
        self.guild.get_role = lambda id_: (
            {501: role_a, 502: role_b}.get(id_) or original_lookup(id_)
        )
        self.member.roles.append(role_a)
        select = RoleSelect(self.bot, self.config.role_menus[0])
        select._values = ["502"]
        await select.callback(interaction(self.guild))
        self.member.add_roles.assert_awaited_once_with(
            role_b, reason="Self-service menu games"
        )
        self.member.remove_roles.assert_awaited_once_with(
            role_a, reason="Self-service menu games"
        )
        self.assertNotIn(self.verified, self.member.remove_roles.call_args.args)

    async def test_concurrent_ticket_creation_private_and_single(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.mention, channel.send = 888, "<#888>", AsyncMock()
        self.guild.create_text_channel.return_value = channel
        self.guild.fetch_channel.return_value = channel
        select = TicketSelect(self.bot)
        select._values = ["support"]
        results = await asyncio.gather(
            select.callback(interaction(self.guild)),
            select.callback(interaction(self.guild)),
            return_exceptions=True,
        )
        self.assertTrue(
            all(result is None or isinstance(result, UserError) for result in results),
            results,
        )
        self.assertEqual(sum(isinstance(result, UserError) for result in results), 1)
        self.guild.create_text_channel.assert_awaited_once()
        overwrites = self.guild.create_text_channel.call_args.kwargs["overwrites"]
        self.assertFalse(overwrites[self.guild.default_role].view_channel)
        self.assertTrue(overwrites[self.support].view_channel)
        self.assertTrue(overwrites[self.member].view_channel)
        self.assertEqual(self.store.open_ticket(42)["channel_id"], 888)

    async def test_ticket_rejects_everyone_support(self):
        category = self.config.ticket_categories[0].model_copy(
            update={"support_role_ids": [123]}
        )
        with self.assertRaises(UserError):
            validate_ticket_category(self.guild, category)

    async def test_ticket_authorization_and_close(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.name = 888, "support-42"
        channel.set_permissions, channel.edit = AsyncMock(), AsyncMock()
        self.store.add_ticket(888, 42, "support")
        self.bot.fetch_user = AsyncMock(return_value=self.member)
        it = interaction(self.guild, channel=channel)
        self.assertEqual((await ticket_member(self.bot, it))[1]["user_id"], 42)
        outsider = NS(**{**vars(self.member), "id": 43, "roles": []})
        self.guild.fetch_member.return_value = outsider
        with self.assertRaises(UserError):
            await ticket_member(self.bot, interaction(self.guild, 43, channel))
        self.guild.fetch_member.return_value = self.member
        close = CloseTicket(self.bot, 42)
        await close.close_ticket.callback(it)
        self.assertIsNone(self.store.open_ticket(42))
        channel.set_permissions.assert_awaited_once_with(
            self.member, view_channel=False, send_messages=False, reason="Ticket closed"
        )
        with self.assertRaises(UserError):
            await close.close_ticket.callback(it)

    async def test_announcement_only_explicit_ping_no_double_send(self):
        self.member.guild_permissions.manage_guild = True
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild, channel.id, channel.mention = self.guild, 777, "<#777>"
        channel.permissions_for.return_value = discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True
        )
        channel.send = AsyncMock(return_value=NS(id=999))
        self.guild.fetch_channel.return_value = channel
        self.bot.destination(channel, self.support)
        composer = Compose(self.bot, 42, channel=channel, role=self.support)
        preview = SendPreview(
            composer, self.bot.embed("Hello", "@everyone\n\nSecond paragraph")
        )
        it = interaction(self.guild)
        await preview.send.callback(it)
        mentions = channel.send.call_args.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertEqual(mentions.roles, [self.support])
        with self.assertRaises(UserError):
            await preview.send.callback(it)
        channel.send.assert_awaited_once()

    async def test_preview_rechecks_sender_before_send(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild = self.guild
        channel.send = AsyncMock()
        composer = Compose(self.bot, 42, channel=channel)
        preview = SendPreview(composer, self.bot.embed("Hello", "Test"))
        with self.assertRaises(UserError):
            await preview.send.callback(interaction(self.guild))
        channel.send.assert_not_awaited()

    async def test_announcement_no_ping_and_unapproved_ping_rejected(self):
        self.member.guild_permissions.manage_guild = True
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild, channel.id, channel.mention = self.guild, 777, "<#777>"
        channel.permissions_for.return_value = discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True
        )
        channel.send = AsyncMock(return_value=NS(id=999))
        self.guild.fetch_channel.return_value = channel
        with self.assertRaises(UserError):
            self.bot.destination(channel, self.verified)
        preview = SendPreview(
            Compose(self.bot, 42, channel=channel),
            self.bot.embed("Hello", "@everyone"),
        )
        await preview.send.callback(interaction(self.guild))
        payload = channel.send.call_args.kwargs
        self.assertIsNone(payload["content"])
        self.assertEqual(payload["allowed_mentions"].roles, [])
        self.assertFalse(payload["allowed_mentions"].everyone)

    async def test_multiple_ping_roles_and_clear_selection(self):
        self.member.guild_permissions.manage_guild = True
        second = Role(601)
        second.guild = self.guild
        self.bot.config.ping_role_ids = [600, 601]
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild, channel.id, channel.mention = self.guild, 777, "<#777>"
        channel.permissions_for.return_value = discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True
        )
        channel.send = AsyncMock(return_value=NS(id=999))
        self.guild.fetch_channel.return_value = channel
        self.guild.fetch_roles.return_value = [self.support, second]
        composer = Compose(self.bot, 42, channel=channel)
        preview = SendPreview(composer, self.bot.embed("Hello", "Test"))
        select = PingSelect(preview, [self.support, second])
        select._values = ["600", "601"]
        await select.callback(interaction(self.guild))
        await preview.send.callback(interaction(self.guild))
        payload = channel.send.call_args.kwargs
        self.assertEqual(payload["allowed_mentions"].roles, [self.support, second])
        self.assertEqual(payload["content"], "<@&600> <@&601>")
        composer2 = Compose(self.bot, 42, channel=channel, role=self.support)
        preview2 = SendPreview(composer2, self.bot.embed("Hello", "Test"))
        select = PingSelect(preview2, [self.support, second])
        select._values = []
        await select.callback(interaction(self.guild))
        self.assertEqual(composer2.ping_roles, [])

    async def test_compose_preserves_paragraphs_and_image_in_preview(self):
        self.member.guild_permissions.manage_guild = True
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild, channel.id, channel.mention = self.guild, 777, "<#777>"
        channel.permissions_for.return_value = discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True
        )
        composer = Compose(self.bot, 42, channel=channel)
        composer.heading._value = "Studio news"
        composer.body._value = "**Update**\n\nParagraph two\n- List item"
        composer.image_link._value = "https://example.com/banner.png"
        it = interaction(self.guild)
        await composer.on_submit(it)
        embed = it.followup.send.call_args.kwargs["embed"]
        self.assertEqual(embed.description, composer.body.value)
        self.assertEqual(embed.image.url, "https://example.com/banner.png")
        channel.send.assert_not_called()

    async def test_join_spike_persists_verification_lock(self):
        self.bot.config.verification.raid_join_count = 2
        await self.bot.on_member_join(self.member)
        await self.bot.on_member_join(self.member)
        self.assertEqual(self.store.setting("verification_locked"), "1")
        with self.assertRaises(UserError):
            self.bot.verification_eligible(self.member)
        self.bot.audit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
