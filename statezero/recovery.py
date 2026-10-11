import asyncio
import json
import logging
import re
import time

import discord

from .security import UserError

log = logging.getLogger("statezero")
MARKER = re.compile(
    r"State Zero ticket \| ([a-f0-9]{32}) \| (\d+) \| ([a-z0-9_-]{1,32})"
)


def ticket_topic(request):
    return f"State Zero ticket | {request['request_id']} | {request['user_id']} | {request['category']}"


def private_ticket(channel, guild, owner, support_roles, *, parent_id=None):
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
        raise UserError("Ticket channel type or server does not match.")
    if parent_id is not None and channel.category_id != parent_id:
        raise UserError("Ticket parent category does not match its saved request.")
    overwrites = channel.overwrites
    everyone = next(
        (value for target, value in overwrites.items() if target.id == guild.id), None
    )
    if everyone is None or everyone.view_channel is not False:
        raise UserError("Ticket does not explicitly deny @everyone access.")
    allowed = {owner, guild.me.id, *support_roles}
    if any(
        value.view_channel is True and target.id not in allowed
        for target, value in overwrites.items()
    ):
        raise UserError(
            "Ticket grants access outside its saved support/creator/bot allowlist."
        )


class TicketRecovery:
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    async def revoke_owner(self, channel, user_id):
        target = channel.guild.get_member(user_id) or await self.bot.fetch_user(user_id)
        await channel.set_permissions(
            target,
            view_channel=False,
            send_messages=False,
            reason="Ticket closed (durable recovery)",
        )

    async def run(self, guild):
        if self.lock.locked():
            return
        async with self.lock, self.bot.ticket_creation_lock:
            self.bot.tickets_ready = False
            problems = []
            channels = await guild.fetch_channels()
            by_id = {channel.id: channel for channel in channels}
            candidates = {}
            for channel in channels:
                if isinstance(channel, discord.TextChannel):
                    match = MARKER.fullmatch(channel.topic or "")
                    if match:
                        candidates.setdefault(match[1], []).append(channel)
            requests = {
                request["request_id"]: request
                for request in await self.bot.store.requests()
            }
            unsafe = False
            for request_id in candidates:
                if request_id not in requests:
                    request = await self.bot.store.request(request_id)
                    if request and request["status"] == "released":
                        requests[request_id] = request
                    elif (
                        request
                        and request["status"] == "completed"
                        and any(
                            channel.id != request["channel_id"]
                            for channel in candidates[request_id]
                        )
                    ):
                        unsafe = True
                        problems.append(
                            f"Request {request_id}: extra channel uses a completed marker; staff review required"
                        )
            for request in requests.values():
                matches = candidates.get(request["request_id"], [])
                if len(matches) != 1:
                    unsafe |= len(matches) > 1
                    problems.append(
                        f"Request {request['request_id']}: {'no channel visible; staff review required' if not matches else 'multiple channels; staff review required'}"
                    )
                    continue
                channel = matches[0]
                try:
                    if channel.topic != ticket_topic(request):
                        raise UserError("Ticket marker does not match saved ownership.")
                    private_ticket(
                        channel,
                        guild,
                        request["user_id"],
                        json.loads(request["support_roles"]),
                        parent_id=request["parent_id"],
                    )
                    # Late channels for explicitly released requests are archived, never duplicated or deleted.
                    closed = request["status"] == "released"
                    if closed:
                        await self.revoke_owner(channel, request["user_id"])
                    await self.bot.store.complete_request(
                        request["request_id"], channel.id, closed=closed
                    )
                    await self.bot.audit(
                        0, "Ticket recovered", channel=channel.id, archived=closed
                    )
                except (UserError, discord.HTTPException) as error:
                    unsafe = True
                    problems.append(
                        f"Request {request['request_id']}: {type(error).__name__}; staff review required"
                    )
            for ticket in await self.bot.store.tickets():
                channel = by_id.get(ticket["channel_id"])
                if channel is None:
                    await self.bot.store.close_ticket(ticket["channel_id"])
                    continue
                category = next(
                    (
                        item
                        for item in self.bot.config.ticket_categories
                        if item.key == ticket["category"]
                    ),
                    None,
                )
                try:
                    if category is None:
                        raise UserError("Ticket support configuration is missing.")
                    private_ticket(
                        channel, guild, ticket["user_id"], category.support_role_ids
                    )
                    if ticket["status"] == "closing":
                        await self.revoke_owner(channel, ticket["user_id"])
                        await self.bot.store.close_ticket(channel.id)
                        await self.bot.audit(
                            0, "Interrupted ticket close recovered", channel=channel.id
                        )
                except (UserError, discord.HTTPException) as error:
                    unsafe = True
                    problems.append(
                        f"Channel {ticket['channel_id']}: {type(error).__name__}; staff review required"
                    )
            self.bot.recovery_problems = problems
            self.bot.recovery_last_success = time.time()
            self.bot.tickets_ready = not unsafe
            if problems:
                log.warning(
                    "Ticket recovery requires staff review (%s outstanding items)",
                    len(problems),
                )

    async def release(self, guild, request_id):
        if self.bot.ticket_creation_lock.locked():
            raise UserError("Ticket recovery is busy. Try again shortly.")
        async with self.bot.ticket_creation_lock:
            request = await self.bot.store.request(request_id)
            if not request or request["status"] != "pending":
                raise UserError("This request is missing or already resolved.")
            if time.time() - request["created"] < 180:
                raise UserError(
                    "Wait at least three minutes before releasing an uncertain request."
                )
            channels = await guild.fetch_channels()
            if any(
                isinstance(channel, discord.TextChannel)
                and (match := MARKER.fullmatch(channel.topic or ""))
                and match[1] == request_id
                for channel in channels
            ):
                raise UserError(
                    "A matching channel exists. Run recovery and inspect it; release is blocked."
                )
            await self.bot.store.release_request(request_id)
