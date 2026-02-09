from sqlalchemy.ext.asyncio import AsyncSession
from app.embedding.models import Chunk


class EmbeddingsStore:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add_chunks(self, chunks: list[Chunk]):
        self.session.add_all(chunks)
        await self.session.commit()
