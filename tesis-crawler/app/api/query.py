from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from uuid import UUID
from app.core.rag_service import RAGService
from app.core.session_memory import session_memory
from app.storage.db_client import async_session

router = APIRouter(tags=["Query"])
rag = RAGService()


class QueryRequest(BaseModel):
    question: str
    session_id: str
    source_id: UUID


@router.get("/query/history")
async def get_query_history(
    source_id: UUID = Query(...),
    session_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    source_key = str(source_id)
    session_key = (session_id or "").strip()
    try:
        async with async_session() as session:
            if not session_key:
                session_row = (
                    await session.execute(
                        text(
                            """
                            SELECT cm.session_id
                            FROM conversation_messages cm
                            WHERE cm.source_id = CAST(:source_id AS uuid) AND cm.role = 'user'
                            GROUP BY cm.session_id
                            ORDER BY MAX(cm.created_at) DESC
                            LIMIT 1
                            """
                        ),
                        {"source_id": source_key},
                    )
                ).mappings().first()
                if not session_row:
                    return {"session_id": None, "messages": []}
                session_key = str(session_row["session_id"])

            rows = (
                await session.execute(
                    text(
                        """
                        SELECT role, text, created_at
                        FROM conversation_messages
                        WHERE source_id = CAST(:source_id AS uuid)
                          AND session_id = :session_id
                        ORDER BY created_at ASC
                        LIMIT :limit
                        """
                    ),
                    {
                        "source_id": source_key,
                        "session_id": session_key,
                        "limit": limit,
                    },
                )
            ).mappings().all()

        return {
            "session_id": session_key,
            "messages": [
                {
                    "role": str(row.get("role") or ""),
                    "content": str(row.get("text") or ""),
                    "created_at": row.get("created_at"),
                }
                for row in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/query")
async def ask_question(req: QueryRequest):
    graph = rag.build_graph()
    try:
        async with async_session() as session:
            source_exists = (
                await session.execute(
                    text(
                        "SELECT 1 FROM sources WHERE source_id = CAST(:source_id AS uuid) LIMIT 1"
                    ),
                    {"source_id": str(req.source_id)},
                )
            ).first()
        if not source_exists:
            raise HTTPException(status_code=404, detail="source_id no existe")

        session_id = await session_memory.ensure_session(req.session_id)
        question = (req.question or "").strip()
        await session_memory.append_user(session_id, question, source_id=req.source_id)
        history = await session_memory.recent_history(
            session_id, source_id=req.source_id, max_items=20
        )

        result = await graph.ainvoke(
            {
                "query": question,
                "context": [],
                "response": "",
                "history": history,
                "source_id": str(req.source_id),
            }
        )
        answer = result.get("response", "")
        await session_memory.append_assistant(session_id, answer, source_id=req.source_id)
        return {"session_id": session_id, "source_id": str(req.source_id), "answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
