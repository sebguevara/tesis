from uuid import UUID, uuid4
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlmodel import SQLModel, Field, Column, ARRAY, Text, JSON
from pgvector.sqlalchemy import Vector


def utc_now_naive() -> datetime:
    # Store UTC as naive datetime to match TIMESTAMP WITHOUT TIME ZONE columns.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Source(SQLModel, table=True):
    __tablename__ = "sources"
    source_id: UUID = Field(default_factory=uuid4, primary_key=True)
    domain: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=utc_now_naive)


class Document(SQLModel, table=True):
    __tablename__ = "documents"
    doc_id: UUID = Field(default_factory=uuid4, primary_key=True)
    source_id: UUID = Field(foreign_key="sources.source_id")
    url: str
    canonical_url: str = Field(unique=True, index=True)
    title: Optional[str]
    page_type: str
    content_hash: str = Field(index=True)
    fetched_at: datetime = Field(default_factory=utc_now_naive)


class Chunk(SQLModel, table=True):
    __tablename__ = "chunks"
    chunk_id: UUID = Field(default_factory=uuid4, primary_key=True)
    doc_id: UUID = Field(foreign_key="documents.doc_id")
    text: str
    heading_path: List[str] = Field(sa_column=Column(ARRAY(Text)))
    embedding: List[float] = Field(sa_column=Column(Vector(1536)))
    meta: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class ConversationSession(SQLModel, table=True):
    __tablename__ = "conversation_sessions"
    session_id: str = Field(primary_key=True, index=True)
    created_at: datetime = Field(default_factory=utc_now_naive)
    last_activity_at: datetime = Field(default_factory=utc_now_naive)


class ConversationMessage(SQLModel, table=True):
    __tablename__ = "conversation_messages"
    message_id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: str = Field(foreign_key="conversation_sessions.session_id", index=True)
    role: str = Field(index=True)
    text: str
    source_id: Optional[UUID] = Field(default=None, foreign_key="sources.source_id", index=True)
    created_at: datetime = Field(default_factory=utc_now_naive)


class WidgetCredential(SQLModel, table=True):
    __tablename__ = "widget_credentials"
    credential_id: UUID = Field(default_factory=uuid4, primary_key=True)
    source_public_id: str = Field(index=True)
    source_id: UUID = Field(foreign_key="sources.source_id", index=True)
    api_key_hash: str = Field(index=True)
    api_key_prefix: str = Field(default="pfc_sk")
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utc_now_naive)
    last_used_at: Optional[datetime] = Field(default=None)
