def owner_allowed(owner: int, user_id: int | None, chat_id: int | None, chat_type: str | None) -> bool:
    return owner > 0 and user_id == owner and chat_id == owner and chat_type == 'private'
