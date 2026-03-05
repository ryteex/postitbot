"""
Permission helpers used by the Post-it commands.

All checks operate on discord.py Member objects — no database lookups needed.
"""

from __future__ import annotations

import discord


def can_manage_events(member: discord.Member) -> bool:
    """
    Return True if the member is allowed to create or edit scheduled events.

    Requires either:
    - Server-wide Manage Messages  (moderators / content managers), OR
    - Administrator
    """
    perms = member.guild_permissions
    return perms.manage_messages or perms.administrator


def can_manage_guild_settings(member: discord.Member) -> bool:
    """
    Return True if the member is allowed to change server-level settings
    (e.g. timezone).

    Requires Manage Server or Administrator.
    """
    perms = member.guild_permissions
    return perms.manage_guild or perms.administrator


def can_modify_event(
    member: discord.Member,
    creator_id: int,
) -> bool:
    """
    Return True if `member` is allowed to edit or delete a specific event.

    Allowed when:
    - The member created the event, OR
    - The member has Manage Server / Administrator permission
    """
    if member.id == creator_id:
        return True
    perms = member.guild_permissions
    return perms.manage_guild or perms.administrator
