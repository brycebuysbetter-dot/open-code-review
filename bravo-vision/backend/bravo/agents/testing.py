"""Scripted LLM for tests and offline demos."""
from bravo.agents.llm import LLMResponse


class ScriptedLLM:
    """Replays prepared responses. Each item is a list of blocks or a callable(messages)->blocks."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls: list[dict] = []

    def create(self, *, system, messages, tools, max_tokens=2048):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        turn = self.turns.pop(0)
        blocks = turn(messages) if callable(turn) else turn
        has_tool = any(b["type"] == "tool_use" for b in blocks)
        return LLMResponse("tool_use" if has_tool else "end_turn", blocks)


def tool_use(name: str, input: dict, id: str = "tu_1") -> dict:
    return {"type": "tool_use", "id": id, "name": name, "input": input}


def text(t: str) -> dict:
    return {"type": "text", "text": t}
