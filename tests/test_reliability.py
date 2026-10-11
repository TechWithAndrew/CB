import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from test_bot import Entity, Role, interaction, make_guild

from statezero.__main__ import health_check
from statezero.bot import (
    AnswerModal,
    ReleaseRequest,
    StudioBot,
    TicketSelect,
    explain,
    export_transcript,
    ticket_member,
)
from statezero.config import Settings
from statezero.operations import Operations, together, write_health
from statezero.recovery import ticket_topic
from statezero.security import UserError, read_token
from statezero.storage import AsyncStore, Store, backup_database


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.db"
        self.store = AsyncStore(self.path)
        self.config = Settings.model_validate(
            {
                "guild_id": 123,
                "verification": {"role_id": 500},
                "ticket_categories": [
                    {
                        "key": "support",
                        "label": "Support",
                        "parent_id": 700,
                        "support_role_ids": [600],
                    }
                ],
            }
        )
        self.bot = StudioBot(self.config, self.store)
        self.bot.tickets_ready = True
        self.bot.audit = AsyncMock()
        self.guild, verified, self.support = make_guild()
        self.member = Entity(
            id=42,
            bot=False,
            guild=self.guild,
            roles=[verified],
            guild_permissions=discord.Permissions.none(),
            mention="<@42>",
        )
        self.guild.fetch_member.return_value = self.member
        self.guild.get_member = lambda user_id: self.member if user_id == 42 else None
        self.bot.fetch_user = AsyncMock(return_value=self.member)
        self.request = await self.store.reserve_ticket(
            "a" * 32, 42, "support", 700, [600], self.config.limits
        )
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id, self.channel.guild, self.channel.category_id = (
            888,
            self.guild,
            700,
        )
        self.channel.topic = ticket_topic(self.request)
        self.channel.overwrites = {
            self.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            self.member: discord.PermissionOverwrite(view_channel=True),
            self.support: discord.PermissionOverwrite(view_channel=True),
        }
        self.channel.set_permissions, self.channel.send = AsyncMock(), AsyncMock()
        self.guild.fetch_channels = AsyncMock(return_value=[self.channel])

    async def asyncTearDown(self):
        await self.bot.close()
        await self.store.close()
        self.directory.cleanup()

    async def test_recovery_after_process_crash_adopts_once(self):
        await self.store.close()
        self.store = AsyncStore(self.path)
        self.bot.store = self.store
        await self.bot.recovery.run(self.guild)
        await self.bot.recovery.run(self.guild)
        self.assertEqual((await self.store.open_ticket(42))["channel_id"], 888)
        self.assertEqual((await self.store.request("a" * 32))["status"], "completed")
        self.assertEqual(await self.store.count_open_tickets(), 1)
        self.guild.create_text_channel.assert_not_awaited()
        self.channel.delete.assert_not_called()

    async def test_missing_uncertain_channel_preserves_reservation(self):
        self.guild.fetch_channels.return_value = []
        await self.bot.recovery.run(self.guild)
        self.assertEqual(await self.store.count_open_tickets(), 1)
        with self.assertRaisesRegex(UserError, "awaiting recovery"):
            await self.store.reserve_ticket(
                "b" * 32, 42, "support", 700, [600], self.config.limits
            )
        self.assertTrue(self.bot.recovery_problems)
        self.assertTrue(
            self.bot.tickets_ready
        )  # Other members may still use spare capacity.

    async def test_forged_owner_marker_is_never_adopted(self):
        self.channel.topic = ticket_topic({**self.request, "user_id": 99999})
        await self.bot.recovery.run(self.guild)
        self.assertIsNone(await self.store.open_ticket(42))
        self.assertFalse(self.bot.tickets_ready)
        self.assertEqual((await self.store.request("a" * 32))["status"], "pending")

    async def test_duplicate_markers_require_review_and_no_deletion(self):
        duplicate = MagicMock(spec=discord.TextChannel)
        duplicate.id, duplicate.topic = 889, self.channel.topic
        self.guild.fetch_channels.return_value.append(duplicate)
        await self.bot.recovery.run(self.guild)
        self.assertIsNone(await self.store.open_ticket(42))
        self.assertFalse(self.bot.tickets_ready)
        self.channel.delete.assert_not_called()
        duplicate.delete.assert_not_called()

    async def test_broad_permission_or_wrong_parent_blocks_adoption(self):
        self.channel.overwrites[Role(601)] = discord.PermissionOverwrite(
            view_channel=True
        )
        await self.bot.recovery.run(self.guild)
        self.assertIsNone(await self.store.open_ticket(42))
        self.assertFalse(self.bot.tickets_ready)
        del self.channel.overwrites[
            next(role for role in self.channel.overwrites if role.id == 601)
        ]
        self.channel.category_id = 701
        await self.bot.recovery.run(self.guild)
        self.assertIsNone(await self.store.open_ticket(42))

    async def test_interrupted_close_retries_permissions_before_completion(self):
        await self.store.complete_request("a" * 32, 888)
        await self.store.begin_close(888)
        await self.store.close()
        self.store = AsyncStore(self.path)
        self.bot.store = self.store
        self.channel.set_permissions.side_effect = discord.Forbidden(
            NS(status=403, reason="Forbidden"), "denied"
        )
        await self.bot.recovery.run(self.guild)
        self.assertEqual((await self.store.ticket(888))["status"], "closing")
        self.channel.set_permissions.side_effect = None
        await self.bot.recovery.run(self.guild)
        self.assertEqual((await self.store.ticket(888))["status"], "closed")
        self.assertIsNone(await self.store.open_ticket(42))
        self.channel.set_permissions.assert_awaited_with(
            self.member,
            view_channel=False,
            send_messages=False,
            reason="Ticket closed (durable recovery)",
        )

    async def test_staff_release_waits_and_refuses_visible_channels(self):
        with self.assertRaisesRegex(UserError, "three minutes"):
            await self.bot.recovery.release(self.guild, "a" * 32)
        with patch(
            "statezero.recovery.time.time", return_value=self.request["created"] + 181
        ):
            with self.assertRaisesRegex(UserError, "matching channel"):
                await self.bot.recovery.release(self.guild, "a" * 32)
            self.guild.fetch_channels.return_value = []
            await self.bot.recovery.release(self.guild, "a" * 32)
        self.assertEqual((await self.store.request("a" * 32))["status"], "released")
        self.assertEqual(await self.store.count_open_tickets(), 0)

    async def test_late_channel_after_release_is_archived_not_deleted(self):
        await self.store.release_request("a" * 32)
        await self.bot.recovery.run(self.guild)
        self.assertEqual((await self.store.ticket(888))["status"], "closed")
        self.channel.set_permissions.assert_awaited_once()
        self.channel.delete.assert_not_called()

    async def test_missing_tracked_channel_frees_open_slot(self):
        await self.store.complete_request("a" * 32, 888)
        self.guild.fetch_channels.return_value = []
        await self.bot.recovery.run(self.guild)
        self.assertEqual((await self.store.ticket(888))["status"], "closed")

    async def test_unknown_marker_never_creates_a_record(self):
        await self.store.release_request("a" * 32)
        self.channel.topic = ticket_topic({**self.request, "request_id": "b" * 32})
        await self.bot.recovery.run(self.guild)
        self.assertIsNone(await self.store.ticket(888))

    async def test_creator_cannot_export_archived_ticket(self):
        await self.store.complete_request("a" * 32, 888, closed=True)
        with self.assertRaisesRegex(UserError, "archived"):
            await ticket_member(self.bot, interaction(self.guild, channel=self.channel))

    async def test_transcript_checks_revoked_access_before_delivery(self):
        await self.store.complete_request("a" * 32, 888)

        async def history(**kwargs):
            yield NS(
                content="private content",
                embeds=[],
                attachments=[],
                created_at=discord.utils.utcnow(),
                author=NS(id=42),
            )
            await self.store.close_ticket(888)

        self.channel.history = history
        self.guild.filesize_limit = 8_000_000
        it = interaction(self.guild, channel=self.channel)
        with self.assertRaisesRegex(UserError, "archived"):
            await export_transcript(self.bot, it)
        it.followup.send.assert_not_awaited()
        self.assertEqual(self.bot.operations.transcripts, 0)

    async def test_transcript_size_is_checked_while_streaming(self):
        await self.store.complete_request("a" * 32, 888)

        async def history(**kwargs):
            yield NS(
                content="x" * 1000,
                embeds=[],
                attachments=[],
                created_at=discord.utils.utcnow(),
                author=NS(id=42),
            )
            self.fail("Export must stop at the size bound before reading more history")

        self.channel.history = history
        self.guild.filesize_limit = 500
        with self.assertRaisesRegex(UserError, "exceeds"):
            await export_transcript(
                self.bot, interaction(self.guild, channel=self.channel)
            )
        self.assertEqual(self.bot.operations.transcripts, 0)

    async def test_startup_gate_rejects_before_permission_fetch(self):
        self.bot.tickets_ready = False
        select = TicketSelect(self.bot)
        select._values = ["support"]
        with self.assertRaisesRegex(UserError, "safety checks"):
            await select.callback(interaction(self.guild))
        self.guild.fetch_member.assert_not_awaited()

    async def prepare_creation(self):
        self.member.id = 43

        async def create_channel(name, **kwargs):
            self.channel.topic = kwargs["topic"]
            self.channel.overwrites = kwargs["overwrites"]
            return self.channel

        self.guild.create_text_channel.side_effect = create_channel
        select = TicketSelect(self.bot)
        select._values = ["support"]
        return select

    async def test_channel_created_but_database_completion_fails_is_recovered(self):
        select = await self.prepare_creation()
        with patch.object(
            self.store,
            "complete_request",
            AsyncMock(side_effect=sqlite3.OperationalError("simulated disk failure")),
        ):
            with self.assertRaisesRegex(UserError, "reconcile"):
                await select.callback(interaction(self.guild, 43))
        self.assertIsNone(await self.store.open_ticket(43))
        self.channel.delete.assert_not_called()
        await self.bot.recovery.run(self.guild)
        self.assertEqual((await self.store.open_ticket(43))["channel_id"], 888)
        self.guild.create_text_channel.assert_awaited_once()

    async def test_uncertain_http_error_keeps_request_and_blocks_duplicate(self):
        select = await self.prepare_creation()
        self.guild.create_text_channel.side_effect = discord.HTTPException(
            NS(status=504, reason="Timeout"), "uncertain"
        )
        with self.assertRaises(discord.HTTPException):
            await select.callback(interaction(self.guild, 43))
        requests = await self.store.requests()
        pending = next(item for item in requests if item["user_id"] == 43)
        self.assertEqual(pending["status"], "pending")
        with self.assertRaisesRegex(UserError, "awaiting recovery"):
            await self.store.reserve_ticket(
                "c" * 32, 43, "support", 700, [600], self.config.limits
            )

    async def test_definite_permission_error_releases_failed_request(self):
        select = await self.prepare_creation()
        self.guild.create_text_channel.side_effect = discord.Forbidden(
            NS(status=403, reason="Forbidden"), "denied"
        )
        with self.assertRaises(discord.Forbidden):
            await select.callback(interaction(self.guild, 43))
        self.assertFalse(
            any(item["user_id"] == 43 for item in await self.store.requests())
        )
        self.assertGreater(await self.store.last_ticket_created(43), 0)

    async def test_opening_message_failure_preserves_registered_private_ticket(self):
        select = await self.prepare_creation()
        self.channel.send.side_effect = discord.Forbidden(
            NS(status=403, reason="Forbidden"), "denied"
        )
        with self.assertRaises(discord.Forbidden):
            await select.callback(interaction(self.guild, 43))
        self.assertEqual((await self.store.open_ticket(43))["channel_id"], 888)
        self.channel.delete.assert_not_called()

    async def test_support_revoked_during_transcript_cannot_receive_content(self):
        await self.store.complete_request("a" * 32, 888)
        supporter = Entity(
            id=43,
            bot=False,
            guild=self.guild,
            roles=[self.support],
            guild_permissions=discord.Permissions.none(),
        )
        self.guild.fetch_member.return_value = supporter

        async def history(**kwargs):
            yield NS(
                content="private",
                embeds=[],
                attachments=[],
                created_at=discord.utils.utcnow(),
                author=NS(id=42),
            )
            supporter.roles = []

        self.channel.history = history
        self.guild.filesize_limit = 8_000_000
        it = interaction(self.guild, 43, self.channel)
        with self.assertRaises(UserError):
            await export_transcript(self.bot, it)
        it.followup.send.assert_not_awaited()

    async def test_release_confirmation_rechecks_revoked_staff(self):
        view = ReleaseRequest(self.bot, 42, "a" * 32)
        with self.assertRaises(UserError):
            await view.release.callback(interaction(self.guild))
        self.guild.fetch_channels.assert_not_awaited()
        self.assertEqual((await self.store.request("a" * 32))["status"], "pending")

    async def test_extra_channel_with_completed_marker_requires_review(self):
        await self.store.complete_request("a" * 32, 888)
        duplicate = MagicMock(spec=discord.TextChannel)
        duplicate.id, duplicate.topic = 889, self.channel.topic
        self.guild.fetch_channels.return_value.append(duplicate)
        await self.bot.recovery.run(self.guild)
        self.assertFalse(self.bot.tickets_ready)
        duplicate.delete.assert_not_called()

    async def test_audit_never_publishes_to_a_public_channel(self):
        self.bot.get_channel = lambda channel_id: self.channel
        self.channel.overwrites[self.guild.default_role] = discord.PermissionOverwrite(
            view_channel=True
        )
        await StudioBot.audit(self.bot, 42, "Member verified", role=500)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.bot.audit_queue.qsize(), 0)

    async def test_audit_queue_is_bounded_and_durable_path_does_not_wait_for_discord(
        self,
    ):
        self.bot.config.staff_role_ids = [600]
        del self.channel.overwrites[self.member]
        self.bot.get_channel = lambda channel_id: self.channel
        for _ in range(105):
            await StudioBot.audit(self.bot, 42, "Member verified", role=500)
        self.assertEqual(self.bot.audit_queue.qsize(), 100)
        self.assertEqual(self.bot.operations.counts["audit_notifications_dropped"], 5)
        self.channel.send.assert_not_awaited()

    async def test_database_server_binding_rejects_wrong_guild_before_sync(self):
        await self.store.set_setting("guild_id", "999")
        self.bot.tree.sync = AsyncMock()
        with self.assertRaisesRegex(UserError, "another Discord server"):
            await self.bot.setup_hook()
        self.bot.tree.sync.assert_not_awaited()

    async def test_raid_lock_is_fail_closed_when_persistence_fails(self):
        self.bot.verification_locked = False
        with patch.object(
            self.store,
            "set_setting",
            AsyncMock(
                side_effect=sqlite3.OperationalError("simulated storage failure")
            ),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await self.bot.set_verification_lock(True)
        self.member.pending = False
        with self.assertRaisesRegex(UserError, "paused"):
            await self.bot.verification_eligible(self.member)

    async def test_bot_leaves_an_unconfigured_server(self):
        other = NS(id=9999, leave=AsyncMock())
        await self.bot.on_guild_join(other)
        other.leave.assert_awaited_once()

    async def test_empty_announcement_allowlist_is_disabled_by_default(self):
        self.channel.permissions_for.return_value = discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True
        )
        with self.assertRaisesRegex(UserError, "announcement_channel_ids"):
            self.bot.destination(self.channel, None)
        self.config.allow_any_announcement_channel = True
        self.bot.destination(self.channel, None)

    async def test_cached_manage_server_cannot_override_current_role_permissions(self):
        self.member.guild_permissions.manage_guild = True
        self.member.roles = [self.guild.get_role(650)]
        current = Role(650, discord.Permissions.none(), position=20)
        self.guild.fetch_roles.return_value = [self.guild.default_role, current]
        with self.assertRaises(UserError):
            await self.bot.member(interaction(self.guild), staff=True)

    async def test_fresh_manage_server_permissions_authorize_without_cached_flag(self):
        self.member.guild_permissions.manage_guild = False
        self.member.roles = [self.guild.get_role(650)]
        self.assertIs(
            await self.bot.member(interaction(self.guild), staff=True), self.member
        )

    async def test_current_member_timeout_blocks_privileged_actions(self):
        self.member.roles = [self.guild.get_role(650)]
        self.member.is_timed_out = lambda: True
        with self.assertRaisesRegex(UserError, "timeout"):
            await self.bot.member(interaction(self.guild), staff=True)

    async def test_overload_error_notices_do_not_create_an_unbounded_http_queue(self):
        held = asyncio.Event()
        notices = []

        async def send_notice(*args, **kwargs):
            notices.append(1)
            await held.wait()

        it = interaction(self.guild)
        it.client = self.bot
        it.response.send_message = send_notice
        tasks = [
            asyncio.create_task(explain(it, UserError("busy"))) for _ in range(200)
        ]
        await asyncio.sleep(0)
        self.assertEqual(len(notices), 8)
        self.assertEqual(self.bot.operations.counts["error_responses_suppressed"], 192)
        held.set()
        await asyncio.gather(*tasks)
        self.assertEqual(self.bot.operations.error_responses, 0)

    async def test_elevated_verification_role_is_rejected_even_if_cache_is_safe(self):
        self.member.roles = []
        self.member.pending = False
        self.member.created_at = discord.utils.utcnow() - __import__(
            "datetime"
        ).timedelta(days=10)
        self.member.add_roles = AsyncMock()
        self.bot.verification_locked = False
        code = self.bot.challenges.create(42)
        modal = AnswerModal(self.bot, 42)
        modal.answer._value = code
        unsafe = Role(500, discord.Permissions(manage_guild=True))
        self.guild.fetch_roles.return_value = [self.guild.default_role, unsafe]
        with self.assertRaises(UserError):
            await modal.on_submit(interaction(self.guild))
        self.member.add_roles.assert_not_awaited()

    async def test_current_server_owner_is_verified_against_fresh_guild_metadata(self):
        self.guild.owner_id = 42
        self.bot.fetch_guild = AsyncMock(return_value=NS(owner_id=43))
        with self.assertRaises(UserError):
            await self.bot.member(interaction(self.guild), staff=True)
        self.bot.fetch_guild.return_value = NS(owner_id=42)
        self.assertIs(
            await self.bot.member(interaction(self.guild), staff=True), self.member
        )

    async def test_storage_failure_blocks_role_grant_before_discord_write(self):
        self.member.roles = []
        self.member.pending = False
        self.member.created_at = discord.utils.utcnow() - __import__(
            "datetime"
        ).timedelta(days=10)
        self.member.add_roles = AsyncMock()
        self.bot.verification_locked = False
        modal = AnswerModal(self.bot, 42)
        modal.answer._value = self.bot.challenges.create(42)
        with patch.object(
            self.store,
            "audit",
            AsyncMock(side_effect=sqlite3.OperationalError("simulated disk full")),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await modal.on_submit(interaction(self.guild))
        self.member.add_roles.assert_not_awaited()

    async def test_background_task_failure_is_observed_and_pauses_ticket_readiness(
        self,
    ):
        async def fail():
            raise RuntimeError("simulated background failure")

        task = asyncio.create_task(fail(), name="test-background")
        with self.assertRaises(RuntimeError):
            await task
        self.bot.background_finished(task)
        self.assertEqual(self.bot.background_failures, 1)
        self.assertFalse(self.bot.tickets_ready)


class AsyncStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_database_does_not_block_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AsyncStore(Path(directory) / "db", capacity=1)
            try:
                await store.health()
                blocker = store.executor.submit(time.sleep, 0.1)
                call = asyncio.create_task(store.health())
                await asyncio.sleep(0.01)
                self.assertFalse(call.done())
                with self.assertRaisesRegex(UserError, "busy"):
                    await store.health()
                await call
                blocker.result()
            finally:
                await store.close()

    async def test_cancelled_write_finishes_and_keeps_queue_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            store = AsyncStore(path, capacity=1)
            try:
                await store.health()
                store.executor.submit(time.sleep, 0.1)
                write = asyncio.create_task(
                    store.set_setting("verification_locked", "1")
                )
                await asyncio.sleep(0.01)
                write.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await write
                with self.assertRaisesRegex(UserError, "busy"):
                    await store.health()
            finally:
                await store.close()
            reopened = AsyncStore(path)
            try:
                self.assertEqual(await reopened.setting("verification_locked"), "1")
            finally:
                await reopened.close()


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_parallel_read_cancels_and_drains_its_sibling(self):
        cancelled = asyncio.Event()

        async def failed():
            await asyncio.sleep(0)
            raise UserError("state read failed")

        async def stalled():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(UserError):
            await together(failed(), stalled())
        self.assertTrue(cancelled.is_set())

    async def test_single_user_cannot_consume_global_budget_after_own_limit(self):
        operations = Operations(Settings().limits, 60)
        for _ in range(20):
            async with operations.action(NS(user=NS(id=42))):
                pass
        for _ in range(200):
            with self.assertRaises(UserError):
                async with operations.action(NS(user=NS(id=42))):
                    self.fail("Rejected member must not run")
        self.assertEqual(len(operations.rate.events), 20)
        async with operations.action(NS(user=NS(id=43))):
            pass

    async def test_overload_rejects_without_unbounded_work_and_reserves_staff_slots(
        self,
    ):
        operations = Operations(Settings().limits, 60)
        held = asyncio.Event()
        started = []

        async def run(user_id):
            try:
                async with operations.action(NS(user=NS(id=user_id))):
                    started.append(user_id)
                    await held.wait()
                return True
            except UserError:
                return False

        tasks = [asyncio.create_task(run(user_id)) for user_id in range(1000)]
        await asyncio.sleep(0)
        self.assertEqual(len(started), 32)
        async with operations.action(NS(user=NS(id=2000)), staff=True):
            self.assertEqual(operations.active, 32)
            self.assertEqual(operations.staff_active, 1)
        held.set()
        results = await asyncio.gather(*tasks)
        self.assertEqual(sum(results), 32)
        self.assertEqual(operations.counts["rejected"], 968)
        self.assertEqual(operations.active, 0)

    async def test_timeout_releases_capacity(self):
        operations = Operations(Settings().limits, 0.01)
        with self.assertRaisesRegex(UserError, "timed out"):
            async with operations.action(NS(user=NS(id=42))):
                await asyncio.Event().wait()
        self.assertEqual(operations.active, 0)
        self.assertEqual(operations.counts["timeouts"], 1)

    async def test_per_user_rate_bound(self):
        operations = Operations(Settings().limits, 60)
        for _ in range(20):
            async with operations.action(NS(user=NS(id=42))):
                pass
        with self.assertRaises(UserError):
            async with operations.action(NS(user=NS(id=42))):
                self.fail("Rate-limited work must not start")


class DeploymentTests(unittest.TestCase):
    def test_invalid_environment_token_fails_without_echoing_it(self):
        secret = "test\x01secret"
        with patch.dict(os.environ, {"DISCORD_TOKEN": secret}, clear=True):
            with self.assertRaises(UserError) as result:
                read_token()
        self.assertNotIn(secret, str(result.exception))

    def test_consistent_live_backup_includes_wal_and_restores_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "live.db", Path(directory) / "backup.db"
            live = Store(source)
            try:
                live.set_setting("verification_locked", "1")
                live.add_ticket(888, 42, "support")
                self.assertTrue(Path(str(source) + "-wal").exists())
                backup_database(source, target)
                with self.assertRaises(FileExistsError):
                    backup_database(source, target)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                restored = Store(target)
                try:
                    self.assertEqual(restored.setting("verification_locked"), "1")
                    self.assertEqual(restored.open_ticket(42)["channel_id"], 888)
                finally:
                    restored.close()
            finally:
                live.close()

    def test_file_lock_rejects_second_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            store = Store(path)
            try:
                code = "from statezero.storage import Store; import sys; Store(sys.argv[1])"
                result = subprocess.run(
                    [sys.executable, "-c", code, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already owns", result.stderr)
            finally:
                store.close()

    def test_forced_process_exit_preserves_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            code = "from statezero.storage import Store; from statezero.config import Settings; import sys,os; store=Store(sys.argv[1]); store.reserve_ticket('a'*32,42,'support',700,[600],Settings().limits); os._exit(0)"
            result = subprocess.run([sys.executable, "-c", code, str(path)], timeout=10)
            self.assertEqual(result.returncode, 0)
            store = Store(path)
            try:
                self.assertEqual(store.request("a" * 32)["user_id"], 42)
                self.assertEqual(store.count_open_tickets(), 1)
            finally:
                store.close()

    def test_old_database_migrates_without_losing_tickets_or_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            database = sqlite3.connect(path)
            database.executescript(
                "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL); INSERT INTO settings VALUES('verification_locked','1'); CREATE TABLE tickets(channel_id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,category TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',created REAL NOT NULL); INSERT INTO tickets VALUES(888,42,'support','open',1000);"
            )
            database.close()
            store = Store(path)
            try:
                self.assertEqual(store.setting("verification_locked"), "1")
                self.assertEqual(store.open_ticket(42)["channel_id"], 888)
                self.assertEqual(
                    store.db.execute("PRAGMA user_version").fetchone()[0], 2
                )
            finally:
                store.close()

    def test_token_rejects_symlink_fifo_and_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "secret"
            secret.write_text("test-secret")
            secret.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(secret)
            with patch.dict(os.environ, {"DISCORD_TOKEN_FILE": str(link)}, clear=True):
                with self.assertRaises(UserError):
                    read_token()
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo, mode=0o600)
            with patch.dict(os.environ, {"DISCORD_TOKEN_FILE": str(fifo)}, clear=True):
                with self.assertRaises(UserError):
                    read_token()
            secret.write_text("x" * 4097)
            with patch.dict(
                os.environ, {"DISCORD_TOKEN_FILE": str(secret)}, clear=True
            ):
                with self.assertRaises(UserError):
                    read_token()

    def test_health_is_private_and_rejects_stale_or_unhealthy_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            write_health(
                path, {"timestamp": time.time(), "ready": True, "healthy": True}
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.assertEqual(health_check(path, 60), 0)
                write_health(
                    path,
                    {"timestamp": time.time() - 61, "ready": True, "healthy": True},
                )
                self.assertEqual(health_check(path, 60), 1)
                write_health(
                    path, {"timestamp": time.time(), "ready": True, "healthy": False}
                )
                self.assertEqual(health_check(path, 60), 1)
                path.write_text(
                    json.dumps({"timestamp": "invalid", "ready": True, "healthy": True})
                )
                self.assertEqual(health_check(path, 60), 1)
