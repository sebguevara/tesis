from fastapi import APIRouter, HTTPException
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
            session_id, source_id=req.source_id, max_items=8
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
