import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from bravo import audit
from bravo.agents.llm import LLM, get_llm
from bravo.agents.prompts import SAFETY_RULES
from bravo.config import get_settings
from bravo.enums import ActorType


class ToolError(Exception):
    """Expected, model-recoverable failure (validation, compliance). Message is shown to the model."""


@dataclass
class AgentContext:
    session: Session
    actor: str  # e.g. "agent:seller_conversation"
    triggered_by: str = "system"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[AgentContext, dict], dict]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass
class AgentRun:
    final_text: str
    steps: int
    tool_calls: list[dict]
    stopped_early: bool = False


def _short(value: Any, limit: int = 1500) -> Any:
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else {"truncated": text[:limit]}


class Agent:
    name = "agent"
    role_prompt = ""

    def __init__(self, llm: LLM | None = None):
        self._llm = llm

    @property
    def llm(self) -> LLM:
        return self._llm or get_llm()

    def tools(self, ctx: AgentContext) -> list[Tool]:
        return []

    def system_prompt(self) -> str:
        owner = get_settings().owner_name
        return (f"{SAFETY_RULES}\nThe business owner and your boss is {owner}. When you report to or address the owner, call them "
                f"{owner}, and be direct and concise. Never use the owner's name or details in messages to sellers or buyers.\n{self.role_prompt}")

    def run(self, session: Session, user_message: str, *, triggered_by: str = "system", data: dict | None = None) -> AgentRun:
        ctx = AgentContext(session=session, actor=f"agent:{self.name}", triggered_by=triggered_by, data=data or {})
        tools = {t.name: t for t in self.tools(ctx)}
        specs = [t.spec() for t in tools.values()]
        messages: list[dict] = [{"role": "user", "content": user_message}]
        calls: list[dict] = []
        audit.log_event(session, actor=ctx.actor, actor_type=ActorType.AGENT, event="agent.run_started",
                        data={"triggered_by": triggered_by, "input": user_message[:500]})
        max_steps = get_settings().agent_max_steps
        text = ""
        for step in range(1, max_steps + 1):
            resp = self.llm.create(system=self.system_prompt(), messages=messages, tools=specs)
            text = "\n".join(b["text"] for b in resp.content if b["type"] == "text").strip()
            uses = [b for b in resp.content if b["type"] == "tool_use"]
            if text:
                audit.log_event(session, actor=ctx.actor, actor_type=ActorType.AGENT, event="agent.decision",
                                data={"step": step, "text": text[:1000]})
            if not uses:
                audit.log_event(session, actor=ctx.actor, actor_type=ActorType.AGENT, event="agent.run_finished",
                                data={"steps": step, "tool_calls": len(calls)})
                return AgentRun(text, step, calls)
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for use in uses:
                results.append(self._call_tool(ctx, tools, use, calls))
            messages.append({"role": "user", "content": results})
        audit.log_event(session, actor=ctx.actor, actor_type=ActorType.AGENT, event="agent.run_stopped_early",
                        data={"max_steps": max_steps, "tool_calls": len(calls)})
        return AgentRun(text, max_steps, calls, stopped_early=True)

    def _call_tool(self, ctx: AgentContext, tools: dict[str, Tool], use: dict, calls: list[dict]) -> dict:
        name, args = use["name"], use.get("input") or {}
        audit.log_event(ctx.session, actor=ctx.actor, actor_type=ActorType.AGENT, event="agent.tool_call",
                        data={"tool": name, "input": _short(args)})
        tool = tools.get(name)
        record = {"tool": name, "input": args}
        try:
            if tool is None:
                raise ToolError(f"unknown tool '{name}'")
            with ctx.session.begin_nested():  # a failing tool leaves no partial writes behind
                result = tool.handler(ctx, args)
            record.update(ok=True, result=result)
            audit.log_event(ctx.session, actor=ctx.actor, actor_type=ActorType.AGENT, event="agent.tool_result",
                            data={"tool": name, "ok": True, "result": _short(result)})
            content, is_error = json.dumps(result, default=str), False
        except ToolError as exc:
            record.update(ok=False, error=str(exc))
            audit.log_event(ctx.session, actor=ctx.actor, actor_type=ActorType.AGENT, event="agent.tool_result",
                            data={"tool": name, "ok": False, "error": str(exc)})
            content, is_error = f"Error: {exc}", True
        except Exception as exc:  # unexpected: record it, tell the model something generic
            record.update(ok=False, error=f"{type(exc).__name__}")
            audit.log_event(ctx.session, actor=ctx.actor, actor_type=ActorType.AGENT, event="agent.tool_error",
                            data={"tool": name, "error": f"{type(exc).__name__}: {exc}"})
            content, is_error = "Error: tool failed unexpectedly; do not retry blindly.", True
        calls.append(record)
        return {"type": "tool_result", "tool_use_id": use["id"], "content": content, "is_error": is_error}
