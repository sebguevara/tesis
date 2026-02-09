class SessionMemory:
    def __init__(self, max_turns: int = 24):
        self._max_turns = max_turns

    async def ensure_session(self, session_id: str) -> str:
        from sqlalchemy import select

        from app.embedding.models import ConversationSession, utc_now_naive
        from app.storage.db_client import async_session

        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id es obligatorio")

        async with async_session() as session:
            existing = await session.execute(
                select(ConversationSession).where(ConversationSession.session_id == sid)
            )
            row = existing.scalar_one_or_none()
            if row is None:
                session.add(ConversationSession(session_id=sid))
            else:
                row.last_activity_at = utc_now_naive()
            await session.commit()
        return sid

    async def append_user(self, session_id: str, text: str, source_id=None) -> None:
        await self._append_message(session_id, "user", text, source_id=source_id)

    async def append_assistant(self, session_id: str, text: str, source_id=None) -> None:
        await self._append_message(session_id, "assistant", text, source_id=source_id)

    async def _append_message(self, session_id: str, role: str, text: str, source_id=None) -> None:
        from sqlalchemy import select

        from app.embedding.models import (
            ConversationMessage,
            ConversationSession,
            utc_now_naive,
        )
        from app.storage.db_client import async_session

        sid = (session_id or "").strip()
        if not sid:
            return
        message_text = (text or "").strip()
        if not message_text:
            return

        async with async_session() as session:
            existing = await session.execute(
                select(ConversationSession).where(ConversationSession.session_id == sid)
            )
            sess = existing.scalar_one_or_none()
            if sess is None:
                sess = ConversationSession(session_id=sid)
                session.add(sess)
                await session.flush()
            sess.last_activity_at = utc_now_naive()
            session.add(
                ConversationMessage(
                    session_id=sid,
                    role=role,
                    text=message_text,
                    source_id=source_id,
                )
            )
            await session.commit()

    async def recent_history(self, session_id: str, source_id=None, max_items: int = 8) -> list[str]:
        from sqlalchemy import select

        from app.embedding.models import ConversationMessage
        from app.storage.db_client import async_session

        sid = (session_id or "").strip()
        if not sid:
            return []

        async with async_session() as session:
            stmt = select(ConversationMessage).where(
                ConversationMessage.session_id == sid
            )
            if source_id is not None:
                stmt = stmt.where(ConversationMessage.source_id == source_id)
            stmt = stmt.order_by(ConversationMessage.created_at.desc()).limit(max(1, max_items))
            rows = (await session.execute(stmt)).scalars().all()
        rows = list(reversed(rows))
        return [f"{msg.role.upper()}: {msg.text}" for msg in rows]


session_memory = SessionMemory()
