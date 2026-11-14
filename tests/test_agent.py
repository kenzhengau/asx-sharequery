from datetime import date

from asx_sharequery.agent import build_instructions


def test_agent_instructions_use_conversation_context_for_followups() -> None:
    instructions = build_instructions(date(2026, 11, 15))

    assert "Treat the conversation as one ongoing chat" in instructions
    assert "Resolve clear references" in instructions
    assert "without repeating the" in instructions
    assert "Today is 2026-11-15" in instructions