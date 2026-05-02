import re

from app.config import settings


def _show_sources() -> bool:
    return (settings.NODE_ENV or "").strip().lower() == "development"


def apply_source_visibility(answer: str) -> str:
    text = (answer or "").strip()
    if not text:
        return ""
    if _show_sources():
        return text

    # Hide trailing "Fuente/Fuentes:" attribution outside development.
    cleaned = re.sub(r"(?is)\n*\s*fuentes?\s*:\s*.*$", "", text).strip()
    return cleaned or text


def add_first_turn_greeting(answer: str) -> str:
    text = (answer or "").strip()
    if not text:
        return "Hola, estoy para ayudarte con lo que necesites."
    low = text.lower()
    if low.startswith("hola") or low.startswith("buenas") or low.startswith("buen dia") or low.startswith("buen día"):
        return text
    return f"Hola, te ayudo con eso. {text}"


def add_conversational_lead(answer: str, question: str, *, is_first_turn: bool = False) -> str:
    text = (answer or "").strip()
    if not text:
        return "Hola, estoy para ayudarte con lo que necesites." if is_first_turn else ""
    if is_first_turn:
        return add_first_turn_greeting(text)
    return text
