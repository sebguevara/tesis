import os
import uuid

import pytest
import httpx


BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SOURCE_ID = os.getenv("E2E_SOURCE_ID")
TIMEOUT = float(os.getenv("E2E_TIMEOUT", "30"))


pytestmark = pytest.mark.skipif(
    not SOURCE_ID,
    reason="Set E2E_SOURCE_ID to run live conversation E2E tests.",
)


def ask(session_id: str, question: str) -> str:
    payload = {
        "question": question,
        "session_id": session_id,
        "source_id": SOURCE_ID,
    }
    response = httpx.post(f"{BASE_URL}/api/query", json=payload, timeout=TIMEOUT)
    response.raise_for_status()
    body = response.json()
    assert "answer" in body and isinstance(body["answer"], str)
    return body["answer"].strip().lower()


def test_followup_keeps_program_context_enfermeria() -> None:
    session_id = str(uuid.uuid4())

    answer_1 = ask(session_id, "que carreras se dictan aca?")
    assert answer_1

    answer_2 = ask(session_id, "segun el plan de estudio de enfermeria, cuales son las materias de primer anio?")
    assert "enfermer" in answer_2
    assert "materias" in answer_2 or "asignaturas" in answer_2

    answer_3 = ask(session_id, "y de segundo anio?")
    assert "enfermer" in answer_3 or "materias de anio 2" in answer_3 or "materias de a\u00f1o 2" in answer_3
    assert "medicina" not in answer_3


def test_authority_followup_stays_on_same_program() -> None:
    session_id = str(uuid.uuid4())

    answer_1 = ask(session_id, "quien es el director de carrera de licenciatura en enfermeria?")
    assert "enfermer" in answer_1
    assert "fuente:" in answer_1

    answer_2 = ask(session_id, "y cual es su duracion?")
    assert "duraci" in answer_2
    assert "enfermer" in answer_2 or "la carrera" in answer_2
    assert "medicina" not in answer_2


def test_explicit_program_switch_overrides_old_context() -> None:
    session_id = str(uuid.uuid4())

    answer_1 = ask(session_id, "materias de primer anio de enfermeria")
    assert "enfermer" in answer_1

    answer_2 = ask(session_id, "ahora de medicina, quien es el director?")
    assert "medicina" in answer_2
    assert "enfermer" not in answer_2


def test_ambiguous_program_prompts_clarification_when_confidence_low() -> None:
    session_id = str(uuid.uuid4())

    answer_1 = ask(session_id, "quien es el director de carrera?")
    assert any(
        token in answer_1
        for token in (
            "carrera exacta",
            "para confirmar",
            "decime primero la carrera",
            "a que carrera",
            "a qué carrera",
        )
    )
