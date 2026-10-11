from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Snowflake = Annotated[int, Field(gt=0)]
Key = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,32}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Branding(Model):
    name: str = Field(default="State Zero Games", min_length=1, max_length=80)
    color: int = Field(default=0x7C5CFC, ge=0, le=0xFFFFFF)
    footer: str = Field(
        default="State Zero Games • Build. Play. Connect.", max_length=200
    )
    welcome_title: str = Field(default="Welcome to State Zero Games", max_length=256)
    welcome_message: str = Field(
        default="Welcome {mention}! Complete verification to unlock the community. You are member #{member_count}.",
        max_length=3000,
    )
    welcome_image: str | None = None
    verification_title: str = Field(default="Unlock the community", max_length=256)
    verification_message: str = Field(
        default="Read the rules, then select Verify to complete a short image challenge. Need help? Contact a moderator.",
        max_length=4000,
    )
    ticket_title: str = Field(default="State Zero Support", max_length=256)
    ticket_message: str = Field(
        default="Choose a category below to open a private ticket with our team.",
        max_length=4000,
    )


class Verification(Model):
    role_id: Snowflake | None = None
    min_account_age_hours: int = Field(default=24, ge=0, le=8760)
    challenge_seconds: int = Field(default=180, ge=30, le=600)
    max_attempts: int = Field(default=3, ge=1, le=5)
    cooldown_seconds: int = Field(default=60, ge=10, le=3600)
    # The safety switch remains active until a staff member clears it.
    raid_join_count: int = Field(default=10, ge=2, le=1000)
    raid_window_seconds: int = Field(default=60, ge=10, le=3600)


class RoleOption(Model):
    role_id: Snowflake
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=100)
    emoji: str | None = None


class RoleMenu(Model):
    key: Key
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(
        default="Choose your roles. Clear your selection to remove them.",
        max_length=4000,
    )
    max_selected: int = Field(default=5, ge=1, le=25)
    options: list[RoleOption] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def unique_roles(self):
        ids = [o.role_id for o in self.options]
        if len(ids) != len(set(ids)) or self.max_selected > len(ids):
            raise ValueError(
                "Role options must be unique and max_selected cannot exceed their count"
            )
        return self


class TicketCategory(Model):
    key: Key
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=100)
    parent_id: Snowflake | None = None
    support_role_ids: list[Snowflake] = Field(default_factory=list, max_length=20)
    opening_message: str = Field(
        default="Thanks for contacting State Zero Games. Describe your issue and our team will help you here.",
        max_length=4000,
    )


class Limits(Model):
    max_open_tickets: int = Field(default=100, ge=1, le=400)
    max_tickets_per_hour: int = Field(default=60, ge=1, le=1000)
    ticket_reopen_seconds: int = Field(default=600, ge=60, le=86400)
    welcome_messages_per_minute: int = Field(default=20, ge=1, le=300)
    verification_challenges_per_minute: int = Field(default=30, ge=1, le=300)


class Settings(Model):
    guild_id: Snowflake | None = None
    staff_role_ids: list[Snowflake] = Field(default_factory=list)
    welcome_channel_id: Snowflake | None = None
    audit_channel_id: Snowflake | None = None
    announcement_channel_ids: list[Snowflake] = Field(default_factory=list)
    ping_role_ids: list[Snowflake] = Field(default_factory=list, max_length=25)
    require_verified_for_features: bool = True
    branding: Branding = Field(default_factory=Branding)
    verification: Verification = Field(default_factory=Verification)
    role_menus: list[RoleMenu] = Field(default_factory=list, max_length=20)
    ticket_categories: list[TicketCategory] = Field(default_factory=list, max_length=25)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def distinct_keys_and_roles(self):
        lists = [self.staff_role_ids, self.announcement_channel_ids, self.ping_role_ids]
        lists.extend(category.support_role_ids for category in self.ticket_categories)
        if any(len(ids) != len(set(ids)) for ids in lists):
            raise ValueError("Role and channel allowlists cannot contain duplicate IDs")
        for collection in (self.role_menus, self.ticket_categories):
            keys = [x.key for x in collection]
            if len(keys) != len(set(keys)):
                raise ValueError("Panel/category keys must be unique")
        protected = set(self.staff_role_ids)
        protected.update(r for c in self.ticket_categories for r in c.support_role_ids)
        if self.verification.role_id:
            protected.add(self.verification.role_id)
        if any(o.role_id in protected for m in self.role_menus for o in m.options):
            raise ValueError(
                "Self-service menus cannot grant verification, staff, or ticket support roles"
            )
        if self.verification.role_id in self.staff_role_ids or any(
            self.verification.role_id in c.support_role_ids
            for c in self.ticket_categories
        ):
            raise ValueError(
                "The verification role cannot also be a staff or support role"
            )
        return self


def load_config(path: str | Path) -> Settings:
    return Settings.model_validate_json(Path(path).read_text(encoding="utf-8"))
