import asyncio
import io
import logging
import math
import secrets
import time
from collections import deque

import discord
from discord import app_commands

from . import __version__
from .captcha import render
from .config import RoleMenu, Settings
from .operations import Operations, guarded, percentile, together, write_health
from .recovery import TicketRecovery, private_ticket, ticket_topic
from .security import Challenges, Cooldowns, RateWindow, UserError, image_url, safe_role
from .storage import AsyncStore

log = logging.getLogger("statezero")


async def explain(interaction: discord.Interaction, error: Exception):
    if isinstance(error, UserError):
        message = str(error)
    elif isinstance(error, discord.Forbidden):
        message = "Discord denied this action. Check the bot's channel permissions and role hierarchy."
    elif isinstance(error, discord.HTTPException):
        message = "Discord could not complete this action. Check the result before trying again."
    else:
        log.error("Action failed (%s)", type(error).__name__)
        message = "This action failed. Ask a server administrator to check the bot configuration."
    client = getattr(interaction, "client", None)
    operations = getattr(client, "operations", None)
    if operations:
        if operations.error_responses >= 8:
            operations.counts["error_responses_suppressed"] += 1
            return
        try:
            operations.error_rate.take()
        except UserError:
            operations.counts["error_responses_suppressed"] += 1
            return
        operations.error_responses += 1
    try:
        async with asyncio.timeout(2):
            if interaction.response.is_done():
                await interaction.followup.send(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.response.send_message(
                    message,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
    except (TimeoutError, discord.HTTPException):
        log.warning("Error notice could not be delivered")
    finally:
        if operations:
            operations.error_responses -= 1


class View(discord.ui.View):
    async def on_error(self, interaction, error, item):
        await explain(interaction, error)


class Modal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        await explain(interaction, error)


class StudioBot(discord.Client):
    def __init__(self, config: Settings, store: AsyncStore, *, message_content=False):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.message_content = message_content
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
        )
        self.config, self.store = config, store
        self.operations = Operations(
            config.limits, config.operations.action_timeout_seconds
        )
        self.tickets_ready = False
        self.verification_locked = True
        self.background_tasks = []
        self.background_failures = 0
        self.recovery_problems = []
        self.recovery_last_success = None
        self.loop_lags = deque(maxlen=120)
        self.recovery = TicketRecovery(self)
        self.audit_queue = asyncio.Queue(maxsize=100)
        self.tree = app_commands.CommandTree(self)
        self.cooldowns = Cooldowns()
        self.challenges = Challenges(
            config.verification.challenge_seconds, config.verification.max_attempts
        )
        self.locks = [asyncio.Lock() for _ in range(64)]
        self.joins = deque(maxlen=config.verification.raid_join_count)
        self.ticket_creation_lock = asyncio.Lock()
        self.welcome_rate = RateWindow(config.limits.welcome_messages_per_minute, 60)
        self.verification_rate = RateWindow(
            config.limits.verification_challenges_per_minute, 60
        )
        self.register_commands()

    def lock(self, user_id: int):
        return self.locks[user_id % len(self.locks)]

    def embed(self, title: str, description: str = "", image: str | None = None):
        embed = discord.Embed(
            title=title,
            description=description,
            color=self.config.branding.color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=self.config.branding.name)
        embed.set_footer(text=self.config.branding.footer)
        if image:
            embed.set_image(url=image_url(image))
        return embed

    async def member(
        self, interaction: discord.Interaction, *, staff=False, verified=False
    ):
        if not interaction.guild or interaction.guild.id != self.config.guild_id:
            raise UserError(
                "This action is only available in the configured State Zero Games server."
            )
        try:
            async with asyncio.timeout(2):
                if staff:
                    member, roles = await together(
                        interaction.guild.fetch_member(interaction.user.id),
                        interaction.guild.fetch_roles(),
                    )
                    authorized = await self.staff_access(member, roles=roles)
                else:
                    member = await interaction.guild.fetch_member(interaction.user.id)
                    authorized = False
        except TimeoutError:
            raise UserError(
                "Permission checks are busy. Please try again shortly."
            ) from None
        if member.bot:
            raise UserError("This feature is for community members.")
        if getattr(member, "is_timed_out", lambda: False)():
            raise UserError(
                "Bot actions are unavailable while your Discord timeout is active."
            )
        if staff and not authorized:
            raise UserError(
                "This action requires Manage Server or a configured staff role."
            )
        if verified and self.config.require_verified_for_features:
            role_id = self.config.verification.role_id
            if not role_id or not any(r.id == role_id for r in member.roles):
                raise UserError("Complete verification before using this feature.")
        return member

    async def role_map(self, guild):
        try:
            async with asyncio.timeout(2):
                return {role.id: role for role in await guild.fetch_roles()}
        except TimeoutError:
            raise UserError(
                "Role safety checks are busy. Please try again shortly."
            ) from None

    async def staff_access(self, member, *, roles=None):
        if any(
            not role.is_default() and role.id in self.config.staff_role_ids
            for role in member.roles
        ):
            return True
        async with asyncio.timeout(2):
            if member.id == getattr(member.guild, "owner_id", None):
                fresh_guild = await self.fetch_guild(member.guild.id)
                if fresh_guild.owner_id == member.id:
                    return True
            fresh = (
                {role.id: role for role in roles}
                if roles is not None
                else await self.role_map(member.guild)
            )
            ids = {member.guild.id, *(role.id for role in member.roles)}
            return any(
                role.permissions.manage_guild or role.permissions.administrator
                for role_id in ids
                if (role := fresh.get(role_id))
            )

    def audit_channel_safe(self, channel, *, fresh_roles=None):
        if (
            not isinstance(channel, discord.TextChannel)
            or channel.guild.id != self.config.guild_id
        ):
            return False
        guild = channel.guild
        allowed = {guild.me.id, *self.config.staff_role_ids}
        for target, overwrite in channel.overwrites.items():
            if overwrite.view_channel is True and target.id not in allowed:
                role = (
                    fresh_roles.get(target.id)
                    if fresh_roles is not None
                    else guild.get_role(target.id)
                )
                if not role or not (
                    role.permissions.manage_guild or role.permissions.administrator
                ):
                    return False
        everyone = next(
            (
                value
                for target, value in channel.overwrites.items()
                if target.id == guild.id
            ),
            None,
        )
        return everyone is not None and everyone.view_channel is False

    async def audit(self, actor: int, action: str, **details):
        await self.store.audit(actor, action, **details)
        channel = self.get_channel(self.config.audit_channel_id or 0)
        if self.audit_channel_safe(channel):
            # IDs and outcomes only. Never log announcement bodies, CAPTCHA answers, or tokens.
            try:
                self.audit_queue.put_nowait((actor, action, details))
            except asyncio.QueueFull:
                self.operations.counts["audit_notifications_dropped"] += 1

    async def setup_hook(self):
        await self.store.health()
        previous_guild = await self.store.setting("guild_id")
        if previous_guild and previous_guild != str(self.config.guild_id):
            raise UserError(
                "This database belongs to another Discord server. Use the correct config or a separate data directory."
            )
        await self.store.set_setting("guild_id", str(self.config.guild_id))
        self.verification_locked = (
            await self.store.setting("verification_locked") == "1"
        )
        self.add_view(VerificationPanel(self))
        self.add_view(TicketControls(self))
        if self.config.ticket_categories:
            self.add_view(TicketPanel(self))
        for menu in self.config.role_menus:
            self.add_view(RolePanel(self, menu))
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.background_tasks = [
            asyncio.create_task(self.monitor(), name="statezero-health"),
            asyncio.create_task(self.recovery_loop(), name="statezero-recovery"),
            asyncio.create_task(self.audit_loop(), name="statezero-audit"),
        ]
        for task in self.background_tasks:
            task.add_done_callback(self.background_finished)

    def background_finished(self, task):
        if task.cancelled() or self.is_closed():
            return
        error = task.exception()
        self.background_failures += 1
        self.tickets_ready = False
        log.error(
            "Background task %s stopped (%s)",
            task.get_name(),
            type(error).__name__ if error else "unexpected completion",
        )

    async def on_ready(self):
        log.info(
            "Connected as bot user %s; guild %s", self.user.id, self.config.guild_id
        )
        guild = self.get_guild(self.config.guild_id)
        if not guild:
            log.error("Bot is not a member of the configured guild")
            return
        for other in self.guilds:
            if other.id != self.config.guild_id:
                await other.leave()
        problems = self.configuration_problems(guild)
        for problem in problems:
            log.warning("Configuration: %s", problem)
        try:
            await self.recovery.run(guild)
        except Exception as error:
            self.tickets_ready = False
            log.error("Startup ticket recovery failed (%s)", type(error).__name__)

    async def on_guild_join(self, guild):
        if guild.id != self.config.guild_id:
            await guild.leave()

    async def close(self):
        for task in self.background_tasks:
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.background_tasks = []
        await super().close()

    async def set_verification_lock(self, locked):
        if locked:
            self.verification_locked = True
        await self.store.set_setting("verification_locked", "1" if locked else "0")
        self.verification_locked = locked

    async def audit_loop(self):
        while not self.is_closed():
            actor, action, details = await self.audit_queue.get()
            batch = [(actor, action, details)]
            for _ in range(7):
                try:
                    batch.append(self.audit_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            channel = self.get_channel(self.config.audit_channel_id or 0)
            try:
                if self.audit_channel_safe(channel):
                    async with asyncio.timeout(2):
                        channel, roles = await together(
                            self.fetch_channel(channel.id), self.role_map(channel.guild)
                        )
                    if not self.audit_channel_safe(channel, fresh_roles=roles):
                        raise UserError("Audit channel access is unsafe.")
                    summaries = [
                        f"**{action}** | actor {actor}\n"
                        + "; ".join(
                            f"{key}: {value}" for key, value in details.items()
                        )[:350]
                        for actor, action, details in batch
                    ]
                    await channel.send(
                        embed=self.embed(
                            "Recent bot actions", "\n\n".join(summaries)[:4000]
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    self.operations.counts["audit_notifications_dropped"] += len(batch)
            except (discord.HTTPException, UserError, TimeoutError):
                self.operations.counts["audit_notifications_dropped"] += len(batch)
                log.warning("Audit notification unavailable; events retained in SQLite")
            finally:
                for _ in batch:
                    self.audit_queue.task_done()
            await asyncio.sleep(5)

    async def recovery_loop(self):
        while not self.is_closed():
            await asyncio.sleep(self.config.operations.recovery_interval_seconds)
            guild = self.get_guild(self.config.guild_id)
            if self.is_ready() and guild:
                try:
                    await self.recovery.run(guild)
                except Exception as error:
                    self.tickets_ready = False
                    log.error(
                        "Periodic ticket recovery failed (%s)", type(error).__name__
                    )

    async def monitor(self):
        interval = self.config.operations.health_interval_seconds
        expected = time.monotonic()
        while not self.is_closed():
            self.loop_lags.append(max(0, time.monotonic() - expected))
            guild = self.get_guild(self.config.guild_id)
            problems = (
                self.configuration_problems(guild)
                if guild
                else ["Configured guild unavailable"]
            )
            try:
                storage = await self.store.health()
                latency = self.latency
                payload = {
                    "version": __version__,
                    "timestamp": time.time(),
                    "ready": self.is_ready(),
                    "verification_paused": self.verification_locked,
                    "healthy": self.is_ready()
                    and self.tickets_ready
                    and self.background_failures == 0
                    and not problems
                    and not self.recovery_problems
                    and storage["pending_requests"] == 0,
                    "configuration_problem_count": len(problems),
                    "recovery_problem_count": len(self.recovery_problems),
                    "recovery_last_success": self.recovery_last_success,
                    "background_task_failures": self.background_failures,
                    "gateway_latency_seconds": round(latency, 4)
                    if math.isfinite(latency)
                    else None,
                    "event_loop_lag_seconds_p95": percentile(self.loop_lags),
                    "database_seconds_p95": percentile(self.store.latencies),
                    "database_pending": self.store.pending,
                    "audit_notifications_pending": self.audit_queue.qsize(),
                    **storage,
                    **self.operations.snapshot(),
                }
                await asyncio.to_thread(
                    write_health, self.store.path.parent / "health.json", payload
                )
                log.info(
                    "Operational health: ready=%s healthy=%s active=%s pending_tickets=%s lag_p95=%s",
                    payload["ready"],
                    payload["healthy"],
                    payload["active_actions"],
                    payload["pending_requests"],
                    payload["event_loop_lag_seconds_p95"],
                )
            except Exception as error:
                log.error("Health monitoring failed (%s)", type(error).__name__)
            expected = time.monotonic() + interval
            await asyncio.sleep(interval)

    def configuration_problems(self, guild: discord.Guild):
        problems = []
        baseline = guild.default_role.permissions
        if (
            baseline.administrator
            or baseline.manage_guild
            or baseline.manage_roles
            or baseline.manage_channels
            or baseline.ban_members
            or baseline.kick_members
            or baseline.moderate_members
        ):
            problems.append(
                "Remove administrative/moderation permissions from @everyone."
            )
        if guild.me and guild.me.guild_permissions.administrator:
            problems.append("Remove Administrator permission from the bot.")
        if self.config.audit_channel_id and not self.audit_channel_safe(
            guild.get_channel(self.config.audit_channel_id)
        ):
            problems.append(
                "Audit channel must deny @everyone and allow only bot/configured staff/Manage Server roles."
            )
        if not self.config.verification.role_id:
            problems.append("Set verification.role_id before posting verification")
        else:
            try:
                safe_role(guild, self.config.verification.role_id)
            except UserError as error:
                problems.append(str(error))
        for menu in self.config.role_menus:
            for option in menu.options:
                try:
                    safe_role(guild, option.role_id)
                except UserError as error:
                    problems.append(f"Role menu {menu.key}: {error}")
        for category in self.config.ticket_categories:
            if not isinstance(
                guild.get_channel(category.parent_id or 0), discord.CategoryChannel
            ):
                problems.append(
                    f"Ticket category {category.key}: set a valid parent_id"
                )
            if not category.support_role_ids:
                problems.append(
                    f"Ticket category {category.key}: configure support_role_ids"
                )
            for role_id in category.support_role_ids:
                role = guild.get_role(role_id)
                if not role or role.is_default():
                    problems.append(
                        f"Ticket category {category.key}: invalid support role"
                    )
        for role_id in self.config.staff_role_ids:
            role = guild.get_role(role_id)
            if not role or role.is_default():
                problems.append("staff_role_ids contains a missing role or @everyone")
        for channel_id in filter(
            None,
            [
                self.config.welcome_channel_id,
                self.config.audit_channel_id,
                *self.config.announcement_channel_ids,
            ],
        ):
            if not isinstance(guild.get_channel(channel_id), discord.TextChannel):
                problems.append(
                    f"Configured channel {channel_id} is missing or not a text channel"
                )
        return problems

    async def on_member_join(self, member):
        if member.guild.id != self.config.guild_id or member.bot:
            return
        now = time.monotonic()
        self.joins.append(now)
        window = self.config.verification.raid_window_seconds
        while self.joins and self.joins[0] < now - window:
            self.joins.popleft()
        if (
            len(self.joins) >= self.config.verification.raid_join_count
            and not self.verification_locked
        ):
            await self.set_verification_lock(True)
            await self.audit(
                0, "Verification paused: join spike", count=len(self.joins)
            )
        if self.verification_locked:
            return
        channel = member.guild.get_channel(self.config.welcome_channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            try:
                self.welcome_rate.take()
            except UserError:
                return
            brand = self.config.branding
            text = (
                brand.welcome_message.replace("{mention}", member.mention)
                .replace("{user}", discord.utils.escape_markdown(member.display_name))
                .replace("{member_count}", str(member.guild.member_count or 0))
                .replace("{server}", member.guild.name)
            )
            try:
                embed = self.embed(brand.welcome_title, text, brand.welcome_image)
                embed.set_thumbnail(url=member.display_avatar.url)
                await channel.send(
                    embed=embed,
                    view=VerificationPanel(self),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.HTTPException, UserError):
                log.warning(
                    "Welcome could not be sent; check channel permissions and branding"
                )

    async def on_guild_channel_delete(self, channel):
        if channel.guild.id == self.config.guild_id and await self.store.ticket(
            channel.id
        ):
            await self.store.close_ticket(channel.id)

    async def begin_verification(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.cooldowns.take(
            ("verify", interaction.user.id), self.config.verification.cooldown_seconds
        )
        self.verification_rate.take()
        member = await self.member(interaction)
        async with self.lock(member.id):
            await self.verification_eligible(member)
            role = safe_role(
                member.guild,
                self.config.verification.role_id or 0,
                fresh_roles=await self.role_map(member.guild),
            )
            if role in member.roles:
                raise UserError("You are already verified.")
            code = self.challenges.create(member.id)
            file = discord.File(
                await asyncio.to_thread(render, code), filename="challenge.png"
            )
            embed = self.embed(
                "Verify your membership",
                "Type the six characters in the image. Select Enter code below. The challenge expires in a few minutes. Need an accessible alternative? Contact a moderator.",
            )
            embed.set_image(url="attachment://challenge.png")
            await interaction.followup.send(
                embed=embed, file=file, view=AnswerView(self, member.id), ephemeral=True
            )

    async def verification_eligible(self, member):
        if (
            self.verification_locked
            or await self.store.setting("verification_locked") == "1"
        ):
            raise UserError(
                "Verification is temporarily paused for server safety. Please contact a moderator."
            )
        if member.pending:
            raise UserError("Accept Discord's server rules before verifying.")
        age = (discord.utils.utcnow() - member.created_at).total_seconds() / 3600
        if age < self.config.verification.min_account_age_hours:
            raise UserError(
                "Your Discord account is too new for automatic verification. Contact a moderator for help."
            )

    def destination(self, channel, role):
        if channel.guild.id != self.config.guild_id:
            raise UserError("Choose a channel in this server.")
        allowed = self.config.announcement_channel_ids
        if not allowed and not self.config.allow_any_announcement_channel:
            raise UserError(
                "Configure announcement_channel_ids before sending announcements."
            )
        if allowed and channel.id not in allowed:
            raise UserError("This channel is not in announcement_channel_ids.")
        permissions = channel.permissions_for(channel.guild.me)
        if (
            not permissions.view_channel
            or not permissions.send_messages
            or not permissions.embed_links
        ):
            raise UserError(
                "The bot needs View Channel, Send Messages, and Embed Links in this channel."
            )
        if role:
            if (
                role.guild.id != channel.guild.id
                or role.is_default()
                or role.id not in self.config.ping_role_ids
            ):
                raise UserError(
                    "Only roles explicitly listed in ping_role_ids can be pinged."
                )
            if not role.mentionable and not permissions.mention_everyone:
                raise UserError(
                    "Make this role mentionable or grant the bot Mention Everyone in this channel."
                )

    def register_commands(self):
        @self.tree.error
        async def command_error(interaction, error):
            await explain(interaction, getattr(error, "original", error))

        @self.tree.command(
            name="announce",
            description="Compose, preview, and publish an embed announcement",
        )
        @app_commands.guild_only()
        @guarded(self)
        async def announce(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
            ping_role: discord.Role | None = None,
            image: discord.Attachment | None = None,
        ):
            await self.member(interaction, staff=True)
            self.destination(channel, ping_role)
            await interaction.response.send_modal(
                Compose(
                    self,
                    interaction.user.id,
                    channel=channel,
                    role=ping_role,
                    image=image,
                )
            )

        @self.tree.command(
            name="verify", description="Complete the community verification challenge"
        )
        @app_commands.guild_only()
        @guarded(self)
        async def verify(interaction: discord.Interaction):
            await self.begin_verification(interaction)

        @self.tree.command(
            name="verification-panel",
            description="Post the persistent verification panel",
        )
        @app_commands.guild_only()
        @guarded(self)
        async def verification_panel(
            interaction: discord.Interaction, channel: discord.TextChannel
        ):
            await self.member(interaction, staff=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            safe_role(
                interaction.guild,
                self.config.verification.role_id or 0,
                fresh_roles=await self.role_map(interaction.guild),
            )
            brand = self.config.branding
            await channel.send(
                embed=self.embed(brand.verification_title, brand.verification_message),
                view=VerificationPanel(self),
            )
            await interaction.followup.send(
                "Verification panel posted.", ephemeral=True
            )

        @self.tree.command(
            name="verification-lock",
            description="Pause or resume new verification during a raid",
        )
        @app_commands.guild_only()
        @guarded(self)
        async def verification_lock(interaction: discord.Interaction, locked: bool):
            await self.member(interaction, staff=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.set_verification_lock(locked)
            self.joins.clear()
            await interaction.followup.send(
                "Verification paused." if locked else "Verification resumed.",
                ephemeral=True,
            )
            await self.audit(interaction.user.id, "Verification lock", enabled=locked)

        @self.tree.command(
            name="role-panel",
            description="Post a configured persistent role selection menu",
        )
        @app_commands.guild_only()
        @guarded(self)
        async def role_panel(
            interaction: discord.Interaction, channel: discord.TextChannel, menu: str
        ):
            await self.member(interaction, staff=True)
            config = next((m for m in self.config.role_menus if m.key == menu), None)
            if not config:
                raise UserError(
                    "Unknown menu key. Configure role_menus in config.json."
                )
            await interaction.response.defer(ephemeral=True, thinking=True)
            fresh = await self.role_map(interaction.guild)
            for option in config.options:
                safe_role(interaction.guild, option.role_id, fresh_roles=fresh)
            await channel.send(
                embed=self.embed(config.title, config.description),
                view=RolePanel(self, config),
            )
            await interaction.followup.send("Role menu posted.", ephemeral=True)

        @role_panel.autocomplete("menu")
        async def role_menu_autocomplete(interaction, current):
            return [
                app_commands.Choice(name=m.title[:100], value=m.key)
                for m in self.config.role_menus
                if current.lower() in m.key
            ][:25]

        @self.tree.command(
            name="ticket-panel",
            description="Post the configured private ticket category menu",
        )
        @app_commands.guild_only()
        @guarded(self)
        async def ticket_panel(
            interaction: discord.Interaction, channel: discord.TextChannel
        ):
            await self.member(interaction, staff=True)
            if not self.config.ticket_categories:
                raise UserError("Configure ticket_categories before posting a panel.")
            for category in self.config.ticket_categories:
                validate_ticket_category(interaction.guild, category)
            await interaction.response.defer(ephemeral=True, thinking=True)
            brand = self.config.branding
            await channel.send(
                embed=self.embed(brand.ticket_title, brand.ticket_message),
                view=TicketPanel(self),
            )
            await interaction.followup.send("Ticket panel posted.", ephemeral=True)

        @self.tree.command(
            name="ticket-close", description="Close and archive this private ticket"
        )
        @app_commands.guild_only()
        @guarded(self)
        async def ticket_close(interaction: discord.Interaction):
            await request_close(self, interaction)

        @self.tree.command(
            name="ticket-transcript",
            description="Export this ticket as a private text file",
        )
        @app_commands.guild_only()
        @guarded(self)
        async def transcript(interaction: discord.Interaction):
            await export_transcript(self, interaction)

        @self.tree.command(
            name="bot-status", description="Show configuration issues and safety status"
        )
        @app_commands.guild_only()
        @guarded(self)
        async def status(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.member(interaction, staff=True)
            problems = self.configuration_problems(interaction.guild)
            text = f"Verification paused: {await self.store.setting('verification_locked', '0') == '1'}\nRole menus: {len(self.config.role_menus)}\nTicket categories: {len(self.config.ticket_categories)}\n"
            snapshot = self.operations.snapshot()
            text += f"Tickets accepting requests: {self.tickets_ready}\nActive actions: {snapshot['active_actions']}\nRecent action p95: {snapshot['action_seconds_p95']} seconds\nRecovery items: {len(self.recovery_problems)}\n"
            text += (
                "\n".join(problems + self.recovery_problems)
                or "Configuration checks passed."
            )
            await interaction.followup.send(
                embed=self.embed("Bot status", text[:4000]), ephemeral=True
            )

        @self.tree.command(
            name="ticket-recovery",
            description="Inspect or release an uncertain ticket request after staff review",
        )
        @app_commands.guild_only()
        @guarded(self)
        async def ticket_recovery(
            interaction: discord.Interaction, request_id: str | None = None
        ):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.member(interaction, staff=True)
            if request_id is None:
                await self.recovery.run(interaction.guild)
                requests = await self.store.requests()
                text = (
                    "\n".join(
                        f"`{item['request_id']}` — owner {item['user_id']} — {item['category']}"
                        for item in requests[:20]
                    )
                    or "No pending ticket requests."
                )
                text += "\n\n" + (
                    "\n".join(self.recovery_problems[:10]) or "Recovery checks passed."
                )
                await interaction.followup.send(
                    embed=self.embed("Ticket recovery", text[:4000]), ephemeral=True
                )
                return
            request = await self.store.request(request_id)
            if not request or request["status"] != "pending":
                raise UserError("This request is missing or already resolved.")
            await interaction.followup.send(
                "Release this uncertain request only after inspecting Discord for an existing ticket. Confirmation checks for matching channels again. No channel will be deleted; any late matching channel will be archived by recovery.",
                view=ReleaseRequest(self, interaction.user.id, request_id),
                ephemeral=True,
            )


class VerificationPanel(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Verify", style=discord.ButtonStyle.success, custom_id="sz:verify"
    )
    @guarded()
    async def verify(self, interaction, button):
        await self.bot.begin_verification(interaction)


class AnswerView(View):
    def __init__(self, bot, owner):
        super().__init__(timeout=bot.config.verification.challenge_seconds)
        self.bot, self.owner = bot, owner

    @discord.ui.button(label="Enter code", style=discord.ButtonStyle.primary)
    @guarded()
    async def enter(self, interaction, button):
        if interaction.user.id != self.owner:
            raise UserError("Start your own verification challenge.")
        await interaction.response.send_modal(AnswerModal(self.bot, self.owner))


class AnswerModal(Modal, title="Community verification"):
    answer = discord.ui.TextInput(
        label="Six characters from the image", min_length=6, max_length=6
    )

    def __init__(self, bot, owner):
        super().__init__(timeout=bot.config.verification.challenge_seconds)
        self.bot, self.owner = bot, owner

    @guarded()
    async def on_submit(self, interaction):
        if interaction.user.id != self.owner:
            raise UserError("This challenge belongs to another member.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = await self.bot.member(interaction)
        async with self.bot.lock(member.id):
            await self.bot.verification_eligible(member)
            role = safe_role(
                member.guild,
                self.bot.config.verification.role_id or 0,
                fresh_roles=await self.bot.role_map(member.guild),
            )
            if not self.bot.challenges.check(member.id, self.answer.value):
                raise UserError(
                    "That code did not match. Select Enter code and try again."
                )
            await self.bot.store.audit(
                member.id, "Verification grant requested", role=role.id
            )
            await member.add_roles(role, reason="Completed community verification")
            await interaction.followup.send(
                "Verified! Welcome to State Zero Games.", ephemeral=True
            )
            await self.bot.audit(member.id, "Member verified", role=role.id)


class RoleSelect(discord.ui.Select):
    def __init__(self, bot, menu: RoleMenu):
        self.bot, self.menu = bot, menu
        super().__init__(
            custom_id=f"sz:roles:{menu.key}",
            placeholder="Choose roles (clear all to remove)",
            min_values=0,
            max_values=menu.max_selected,
            options=[
                discord.SelectOption(
                    label=o.label,
                    value=str(o.role_id),
                    description=o.description or None,
                    emoji=o.emoji,
                )
                for o in menu.options
            ],
        )

    @guarded()
    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.bot.cooldowns.take(("roles", interaction.user.id), 3)
        member = await self.bot.member(interaction, verified=True)
        selected = {int(value) for value in self.values}
        async with self.bot.lock(member.id):
            fresh = await self.bot.role_map(member.guild)
            roles = {
                o.role_id: safe_role(member.guild, o.role_id, fresh_roles=fresh)
                for o in self.menu.options
            }
            if not selected <= roles.keys() or len(selected) > self.menu.max_selected:
                raise UserError("Invalid role selection.")
            # Add/remove only panel roles. Do not replace the member's entire role list.
            adds = [
                r
                for id_, r in roles.items()
                if id_ in selected and r not in member.roles
            ]
            removes = [
                r
                for id_, r in roles.items()
                if id_ not in selected and r in member.roles
            ]
            await self.bot.store.audit(
                member.id,
                "Role change requested",
                added=[role.id for role in adds],
                removed=[role.id for role in removes],
            )
            if adds:
                await member.add_roles(
                    *adds, reason=f"Self-service menu {self.menu.key}"
                )
            if removes:
                await member.remove_roles(
                    *removes, reason=f"Self-service menu {self.menu.key}"
                )
            await interaction.followup.send(
                "Your roles were updated. Reopen the menu to change them again.",
                ephemeral=True,
            )
            await self.bot.audit(
                member.id,
                "Self-service roles",
                added=[r.id for r in adds],
                removed=[r.id for r in removes],
            )


class RolePanel(View):
    def __init__(self, bot, menu):
        super().__init__(timeout=None)
        self.add_item(RoleSelect(bot, menu))


class Compose(Modal, title="State Zero message composer"):
    heading = discord.ui.TextInput(label="Title", max_length=256)
    body = discord.ui.TextInput(
        label="Message — line breaks and Markdown supported",
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )
    image_link = discord.ui.TextInput(
        label="Image HTTPS URL (optional)", required=False, max_length=1500
    )

    def __init__(self, bot, owner, *, channel=None, role=None, image=None):
        super().__init__(timeout=600)
        self.bot, self.owner = bot, owner
        self.channel, self.role = channel, role
        self.ping_roles = [role] if channel and role else []
        self.image = image
        if image and (
            not image.content_type
            or not image.content_type.startswith("image/")
            or image.size > 8_000_000
        ):
            raise UserError("Use an image attachment smaller than 8 MB.")

    @guarded()
    async def on_submit(self, interaction):
        if interaction.user.id != self.owner:
            raise UserError("This form belongs to another staff member.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.member(interaction, staff=True)
        image = image_url(
            self.image_link.value or (self.image.url if self.image else None)
        )
        embed = self.bot.embed(self.heading.value, self.body.value, image)
        if self.channel is None:
            raise UserError("Choose an announcement channel.")
        self.bot.destination(self.channel, self.role)
        note = f"Destination: {self.channel.mention}\nPing: {self.role.mention if self.role else 'none'}"
        await interaction.followup.send(
            content=f"**Preview — nothing sent yet**\n{note}",
            embed=embed,
            view=SendPreview(self, embed),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class SendPreview(View):
    def __init__(self, composer, embed):
        super().__init__(timeout=300)
        self.composer, self.embed = composer, embed
        self.bot = composer.bot
        self.used = False
        self.submit_lock = asyncio.Lock()
        if composer.channel and composer.bot.config.ping_role_ids:
            roles = [
                composer.channel.guild.get_role(id_)
                for id_ in composer.bot.config.ping_role_ids
            ]
            roles = [role for role in roles if role and not role.is_default()]
            if roles:
                self.add_item(PingSelect(self, roles[:25]))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.composer.owner:
            await interaction.response.send_message(
                "This preview belongs to another staff member.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm send", style=discord.ButtonStyle.primary)
    @guarded()
    async def send(self, interaction, button):
        if interaction.user.id != self.composer.owner:
            raise UserError("This preview belongs to another staff member.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.submit_lock:
            if self.used:
                raise UserError(
                    "This preview was already used. Check the delivery result."
                )
            bot = self.composer.bot
            await bot.member(interaction, staff=True)
            # Fetch fresh channel/role state instead of trusting the original form.
            channel = await interaction.guild.fetch_channel(self.composer.channel.id)
            if not isinstance(channel, discord.TextChannel):
                raise UserError("The destination is no longer a text channel.")
            roles = []
            if self.composer.ping_roles:
                fresh = {r.id: r for r in await interaction.guild.fetch_roles()}
                for selected in self.composer.ping_roles:
                    role = fresh.get(selected.id)
                    if not role:
                        raise UserError("A ping role no longer exists.")
                    bot.destination(channel, role)
                    roles.append(role)
            else:
                bot.destination(channel, None)
            if len(roles) > 25:
                raise UserError("Choose at most 25 ping roles.")
            bot.cooldowns.take(("announce", interaction.user.id), 5)
            await bot.store.audit(
                interaction.user.id,
                "Announcement send requested",
                channel=channel.id,
                pings=[role.id for role in roles],
            )
            self.used = True
            self.stop()
            message = await channel.send(
                content=" ".join(role.mention for role in roles) if roles else None,
                embed=self.embed,
                allowed_mentions=discord.AllowedMentions(
                    roles=roles, users=False, everyone=False, replied_user=False
                ),
            )
            await interaction.message.edit(view=None, content="Announcement sent.")
            await interaction.followup.send(
                f"Announcement sent to {channel.mention}.", ephemeral=True
            )
            await bot.audit(
                interaction.user.id,
                "Announcement sent",
                channel=channel.id,
                message=message.id,
                pings=[role.id for role in roles],
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    @guarded()
    async def cancel(self, interaction, button):
        if interaction.user.id != self.composer.owner:
            raise UserError("This preview belongs to another staff member.")
        async with self.submit_lock:
            if self.used:
                raise UserError("This preview was already used.")
            self.used = True
            self.stop()
            await interaction.response.edit_message(
                content="Message cancelled.", embed=None, view=None
            )


class PingSelect(discord.ui.Select):
    def __init__(self, preview, roles):
        self.preview = preview
        self.bot = preview.composer.bot
        self.roles = {role.id: role for role in roles}
        selected = {role.id for role in preview.composer.ping_roles}
        super().__init__(
            placeholder="Ping roles (clear all for no ping)",
            min_values=0,
            max_values=len(roles),
            options=[
                discord.SelectOption(
                    label=role.name, value=str(role.id), default=role.id in selected
                )
                for role in roles
            ],
        )

    @guarded()
    async def callback(self, interaction):
        if interaction.user.id != self.preview.composer.owner:
            raise UserError("This preview belongs to another staff member.")
        bot = self.preview.composer.bot
        await bot.member(interaction, staff=True)
        async with self.preview.submit_lock:
            if self.preview.used:
                raise UserError("This preview was already used.")
            selected = [self.roles.get(int(value)) for value in self.values]
            if any(role is None for role in selected):
                raise UserError("Invalid ping role selection.")
            for role in selected:
                bot.destination(self.preview.composer.channel, role)
            self.preview.composer.ping_roles = selected
            for option in self.options:
                option.default = int(option.value) in {role.id for role in selected}
            pings = " ".join(role.mention for role in selected) or "none"
            await interaction.response.edit_message(
                content=f"**Preview — nothing sent yet**\nDestination: {self.preview.composer.channel.mention}\nPings: {pings}",
                view=self.preview,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def validate_ticket_category(guild, category):
    parent = guild.get_channel(category.parent_id or 0)
    if not isinstance(parent, discord.CategoryChannel):
        raise UserError("A ticket category needs a valid Discord category parent_id.")
    if not category.support_role_ids:
        raise UserError("Configure at least one support role for this ticket category.")
    roles = [guild.get_role(role_id) for role_id in category.support_role_ids]
    if any(role is None or role.is_default() for role in roles):
        raise UserError("A ticket support role is missing or is @everyone.")
    if (
        not guild.me
        or not guild.me.guild_permissions.manage_channels
        or not guild.me.guild_permissions.manage_roles
    ):
        raise UserError(
            "The bot needs Manage Channels and Manage Roles to create private tickets."
        )
    return parent, roles


class TicketSelect(discord.ui.Select):
    def __init__(self, bot):
        self.bot = bot
        super().__init__(
            custom_id="sz:tickets",
            placeholder="Choose a support category",
            options=[
                discord.SelectOption(
                    label=c.label, value=c.key, description=c.description or None
                )
                for c in bot.config.ticket_categories
            ],
        )

    @guarded()
    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.bot.tickets_ready:
            raise UserError(
                "Ticket safety checks are pending or require staff attention. Please try later."
            )
        if self.bot.ticket_creation_lock.locked():
            raise UserError(
                "Another ticket is being created. Please try again shortly."
            )
        self.bot.cooldowns.take(("ticket", interaction.user.id), 60)
        member = await self.bot.member(interaction, verified=True)
        category = next(
            (c for c in self.bot.config.ticket_categories if c.key == self.values[0]),
            None,
        )
        if not category:
            raise UserError(
                "This category is no longer configured. Ask staff to update the panel."
            )
        # Recheck after the member fetch. Do not build an unbounded creation queue.
        if self.bot.ticket_creation_lock.locked():
            raise UserError(
                "Another ticket is being created. Please try again shortly."
            )
        async with self.bot.ticket_creation_lock, self.bot.lock(member.id):
            existing = await self.bot.store.open_ticket(member.id)
            if existing:
                try:
                    channel = await interaction.guild.fetch_channel(
                        existing["channel_id"]
                    )
                except discord.NotFound:
                    await self.bot.store.close_ticket(existing["channel_id"])
                else:
                    raise UserError(
                        f"You already have an open ticket: {channel.mention}"
                    )
            parent, support = validate_ticket_category(interaction.guild, category)
            if len(interaction.guild.channels) >= 490 or len(parent.channels) >= 49:
                raise UserError(
                    "This server or ticket category is nearing Discord's channel limit. Contact staff."
                )
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(
                    view_channel=False
                ),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    create_public_threads=False,
                    create_private_threads=False,
                    mention_everyone=False,
                ),
                interaction.guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                    embed_links=True,
                    attach_files=True,
                ),
            }
            for role in support:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                )
            request = await self.bot.store.reserve_ticket(
                secrets.token_hex(16),
                member.id,
                category.key,
                parent.id,
                category.support_role_ids,
                self.bot.config.limits,
            )
            try:
                channel = await interaction.guild.create_text_channel(
                    f"{category.key}-{member.id}",
                    category=parent,
                    overwrites=overwrites,
                    reason=f"Private support ticket for {member.id}",
                    topic=ticket_topic(request),
                )
            except discord.HTTPException as error:
                if error.status in (400, 401, 403, 404):
                    await self.bot.store.release_request(request["request_id"])
                raise
            private_ticket(
                channel,
                interaction.guild,
                member.id,
                category.support_role_ids,
                parent_id=parent.id,
            )
            try:
                await self.bot.store.complete_request(request["request_id"], channel.id)
            except Exception:
                log.error(
                    "Ticket save interrupted; durable request retained for recovery"
                )
                raise UserError(
                    "Your ticket may already exist. Staff recovery will reconcile it; do not submit another request."
                ) from None
            # If the panel send fails, retain the ticket; /ticket-close still works.
            await channel.send(
                embed=self.bot.embed(
                    category.label, f"{member.mention}\n\n{category.opening_message}"
                ),
                view=TicketControls(self.bot),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await interaction.followup.send(
                f"Your private ticket is ready: {channel.mention}", ephemeral=True
            )
            await self.bot.audit(
                member.id, "Ticket opened", channel=channel.id, category=category.key
            )


class TicketPanel(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(bot))


async def ticket_member(bot, interaction):
    member = await bot.member(interaction)
    ticket = await bot.store.ticket(interaction.channel_id)
    if not ticket or not isinstance(interaction.channel, discord.TextChannel):
        raise UserError("Use this command inside a bot-created ticket.")
    category = next(
        (c for c in bot.config.ticket_categories if c.key == ticket["category"]), None
    )
    support = set(category.support_role_ids) if category else set()
    private_ticket(interaction.channel, interaction.guild, ticket["user_id"], support)
    is_support = any(r.id in support for r in member.roles)
    staff = (
        await bot.staff_access(member)
        if not is_support
        and (member.id != ticket["user_id"] or ticket["status"] != "open")
        else False
    )
    if ticket["status"] != "open" and not staff and not is_support:
        raise UserError(
            "This ticket is archived or closing; only authorized staff can export it."
        )
    if member.id != ticket["user_id"] and not staff and not is_support:
        raise UserError("Only the ticket creator or support staff can use this action.")
    return member, ticket


async def export_transcript(bot, interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if bot.operations.transcripts >= bot.config.limits.max_concurrent_transcripts:
        raise UserError("Transcript export is busy. Try again shortly.")
    bot.operations.transcripts += 1
    try:
        await ticket_member(bot, interaction)
        bot.cooldowns.take(("transcript", interaction.channel_id), 30)
        buffer = io.BytesIO()
        header = (
            "State Zero Games ticket transcript\nNewest 1,000 messages (text and attachment URLs only).\n"
            f"Message Content Intent enabled: {bot.intents.message_content}. Content may be omitted by Discord when disabled.\n\n"
        )
        buffer.write(header.encode("utf-8"))
        limit = min(interaction.guild.filesize_limit, 7_000_000)
        async for message in interaction.channel.history(limit=1000):
            content = message.content
            for embed in message.embeds:
                content += f"\n[Embed] {embed.title or ''}\n{embed.description or ''}"
            for attachment in message.attachments:
                content += f"\n[Attachment] {attachment.url}"
            chunk = f"{message.created_at.isoformat()} | {message.author.id} | {content}\n\n".encode(
                "utf-8"
            )
            if buffer.tell() + len(chunk) > limit:
                raise UserError(
                    "Transcript exceeds the upload limit. Contact staff for a smaller export."
                )
            buffer.write(chunk)
        # Recheck revoked support access and ticket closure before delivering any content.
        await ticket_member(bot, interaction)
        buffer.seek(0)
        await interaction.followup.send(
            file=discord.File(buffer, filename=f"ticket-{interaction.channel_id}.txt"),
            ephemeral=True,
        )
        await bot.audit(
            interaction.user.id, "Transcript exported", channel=interaction.channel_id
        )
    finally:
        bot.operations.transcripts -= 1


async def request_close(bot, interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    _, ticket = await ticket_member(bot, interaction)
    if ticket["status"] != "open":
        raise UserError("This ticket is already closed.")
    await interaction.followup.send(
        "Close this ticket? It will be archived, and the creator will lose access. Export a transcript first if needed. Staff can review the archived channel.",
        view=CloseTicket(bot, interaction.user.id),
        ephemeral=True,
    )


class TicketControls(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Close ticket",
        style=discord.ButtonStyle.danger,
        custom_id="sz:close-ticket",
    )
    @guarded()
    async def close_ticket(self, interaction, button):
        await request_close(self.bot, interaction)


class CloseTicket(View):
    def __init__(self, bot, owner):
        super().__init__(timeout=120)
        self.bot, self.owner = bot, owner

    @discord.ui.button(label="Confirm close", style=discord.ButtonStyle.danger)
    @guarded()
    async def close_ticket(self, interaction, button):
        if interaction.user.id != self.owner:
            raise UserError("This confirmation belongs to someone else.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, ticket = await ticket_member(self.bot, interaction)
        async with self.bot.lock(ticket["user_id"]):
            ticket = await self.bot.store.ticket(interaction.channel_id)
            if ticket["status"] != "open":
                raise UserError("This ticket is already closed.")
            await self.bot.store.begin_close(interaction.channel_id)
            target = interaction.guild.get_member(
                ticket["user_id"]
            ) or await self.bot.fetch_user(ticket["user_id"])
            await interaction.channel.set_permissions(
                target, view_channel=False, send_messages=False, reason="Ticket closed"
            )
            await self.bot.store.close_ticket(interaction.channel_id)
            self.stop()
            await interaction.followup.send(
                "Ticket closed and archived for staff review.", ephemeral=True
            )
            await self.bot.audit(
                interaction.user.id, "Ticket closed", channel=interaction.channel_id
            )
            await interaction.channel.edit(
                name=f"closed-{interaction.channel.name}"[:100],
                reason="Ticket archived",
            )


class ReleaseRequest(View):
    def __init__(self, bot, owner, request_id):
        super().__init__(timeout=120)
        self.bot, self.owner, self.request_id = bot, owner, request_id

    @discord.ui.button(label="Confirm release", style=discord.ButtonStyle.danger)
    @guarded()
    async def release(self, interaction, button):
        if interaction.user.id != self.owner:
            raise UserError("This confirmation belongs to another staff member.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.member(interaction, staff=True)
        await self.bot.recovery.release(interaction.guild, self.request_id)
        self.stop()
        await self.bot.audit(
            interaction.user.id,
            "Uncertain ticket request released",
            request=self.request_id,
        )
        await interaction.followup.send(
            "Request released. Late matching channels will be archived by recovery.",
            ephemeral=True,
        )
