from uuid import UUID, uuid4
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy import UniqueConstraint, ForeignKey, Index
from sqlmodel import SQLModel, Field, Column, ARRAY, Text, JSON, Integer
from pgvector.sqlalchemy import Vector


EMBEDDING_DIM = 1536


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
    content_type: str = Field(default="html")  # "html" | "pdf"
    authority_score: float = Field(default=0.5)
    original_filename: Optional[str] = Field(default=None)
    content_hash: str = Field(index=True)
    content: Optional[str] = Field(default=None, sa_column=Column(Text))  # full cleaned text
    fetched_at: datetime = Field(default_factory=utc_now_naive)


class Chunk(SQLModel, table=True):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("doc_id", "chunk_id", name="uq_chunks_doc_seq"),
        Index("chunks_doc_id_idx", "doc_id"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    doc_id: UUID = Field(
        sa_column=Column(ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False, index=True)
    )
    chunk_id: int = Field(sa_column=Column(Integer, nullable=False))  # 0-based seq within doc
    text: str = Field(sa_column=Column(Text, nullable=False))
    context: Optional[str] = Field(default=None, sa_column=Column(Text))  # filled by Stage 2 contextual retrieval
    embedding: Any = Field(sa_column=Column(Vector(EMBEDDING_DIM), nullable=False))
    token_count: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now_naive)


class ConversationSession(SQLModel, table=True):
    __tablename__ = "conversation_sessions"
    session_id: str = Field(primary_key=True, index=True)
    created_at: datetime = Field(default_factory=utc_now_naive)
    last_activity_at: datetime = Field(default_factory=utc_now_naive)
    session_state: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class ConversationMessage(SQLModel, table=True):
    __tablename__ = "conversation_messages"
    message_id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: str = Field(foreign_key="conversation_sessions.session_id", index=True)
    role: str = Field(index=True)
    text: str
    source_id: Optional[UUID] = Field(default=None, foreign_key="sources.source_id", index=True)
    created_at: datetime = Field(default_factory=utc_now_naive)


class User(SQLModel, table=True):
    __tablename__ = "user"
    user_id: UUID = Field(default_factory=uuid4, primary_key=True)
    clerk_user_id: str = Field(unique=True, index=True)
    email: Optional[str] = Field(default=None, index=True)
    api_key: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=utc_now_naive)
    updated_at: datetime = Field(default_factory=utc_now_naive)
    last_sign_in_at: Optional[datetime] = Field(default=None)


class UserSource(SQLModel, table=True):
    __tablename__ = "user_sources"
    __table_args__ = (UniqueConstraint("user_id", "source_id", name="uq_user_sources_user_source"),)
    link_id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.user_id", index=True)
    source_id: UUID = Field(foreign_key="sources.source_id", index=True)
    created_at: datetime = Field(default_factory=utc_now_naive)


class ProgramFact(SQLModel, table=True):
    __tablename__ = "program_facts"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "canonical_url",
            "program_name",
            "fact_key",
            "fact_value",
            name="uq_program_facts_source_url_program_key_value",
        ),
    )
    fact_id: UUID = Field(default_factory=uuid4, primary_key=True)
    source_id: UUID = Field(foreign_key="sources.source_id", index=True)
    canonical_url: str = Field(index=True)
    program_name: str = Field(index=True)
    fact_key: str = Field(index=True)  # e.g. program_name, duration, director, secretary_academic, year_N_subject, profile_*_page
    fact_value: str
    evidence_text: Optional[str] = None
    confidence: float = 0.7
    fetched_at: datetime = Field(default_factory=utc_now_naive, index=True)
