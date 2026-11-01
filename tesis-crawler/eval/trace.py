"""
Corre el grafo RAG en proceso y muestra cada paso (query reescrita, URLs del
contexto, respuesta generada, veredicto del verificador). Para depurar un caso.

    python eval/trace.py <source_id> "pregunta"
"""
import asyncio
import re
import sys

from app.core.rag_service import RAGService


async def main() -> None:
    source_id, question = sys.argv[1], sys.argv[2]
    svc = RAGService()
    state = {"query": question, "history": [], "source_id": source_id}
    state.update(await svc.rewrite(state))
    state.update(await svc.retrieve(state))
    print("CONTEXT URLS:")
    for c in state["context"][:12]:
        m = re.search(r"URL: (\S+)", c)
        print("  ", m.group(1) if m else "?", len(c))
    state.update(await svc.generate(state))
    print("\nGENERATED:", state["response"])
    out = await svc.verify(state)
    print("\nVERIFY:", out)


if __name__ == "__main__":
    asyncio.run(main())
