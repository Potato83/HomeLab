"""Conservative temporary moderation: existing policy belongs to administrators."""
from datetime import datetime

from aiogram.types import ChatPermissions


async def is_unrestricted_member(bot, chat_id: int, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status == "member"


async def mute_if_unrestricted(bot, chat_id: int, user_id: int, until_date: datetime) -> bool:
    # Check again at the write boundary, including when replaying an old action.
    # Telegram has one deadline for all permissions: merging a temporary text
    # mute into a permanent partial restriction would change that policy too.
    if not await is_unrestricted_member(bot, chat_id, user_id):
        return False
    await bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=ChatPermissions(can_send_messages=False),
        use_independent_chat_permissions=True,
        until_date=until_date,
    )
    return True
