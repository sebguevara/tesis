from app.core.chat_format import add_conversational_lead


def test_add_conversational_lead_does_not_prepend_fixed_connector_on_followups() -> None:
    answer = add_conversational_lead(
        "El vicedecano es Prof. Dr. Juan Perez.",
        "quien es el vicedecano?",
        is_first_turn=False,
    )
    assert answer == "El vicedecano es Prof. Dr. Juan Perez."


def test_add_conversational_lead_keeps_first_turn_greeting() -> None:
    answer = add_conversational_lead(
        "Carreras detectadas: 3.",
        "que carreras hay?",
        is_first_turn=True,
    )
    assert answer.startswith("Hola, te ayudo con eso.")
