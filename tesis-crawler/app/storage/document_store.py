from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.embedding.models import Document


class DocumentStore:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_document_by_url(self, url: str):
        statement = select(Document).where(Document.canonical_url == url)
        results = await self.session.exec(statement)
        return results.first()

    async def add_document(self, doc: Document):
        self.session.add(doc)
        await self.session.flush()
        return doc
