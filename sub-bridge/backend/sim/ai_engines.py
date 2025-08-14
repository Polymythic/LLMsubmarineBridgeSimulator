from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import httpx

from ..config import CONFIG
from ..models import Ship
from .ai_tools import LocalAIStub


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract the first top-level JSON object from a text response.

    This is resilient to LLMs that wrap JSON with prose or code fences.
    """
    # Fast path: clean code fences
    fence = re.search(r"```(json)?\s*([\s\S]*?)```", text)
    if fence:
        candidate = fence.group(2).strip()
        try:
            return json.loads(candidate)
        except Exception:
            pass
    # General path: find first balanced { ... }
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


class BaseEngine:
    async def propose_fleet_intent(self, fleet_summary: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def propose_ship_tool(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class StubEngine(BaseEngine):
    def __init__(self) -> None:
        self._stub = LocalAIStub()

    async def propose_fleet_intent(self, fleet_summary: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "objectives": ["patrol"],
            "groups": {},
            "target_priority": ["SSN", "Destroyer", "Convoy"],
            "engagement_rules": {"weapons_free": False, "min_confidence": 0.6, "hold_fire_in_emcon": True},
            "emcon": {"active_ping_allowed": False, "radio_discipline": "restricted"},
        }

    async def propose_ship_tool(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        return self._stub.propose_orders(ship)


class OllamaAgentsEngine(BaseEngine):
    def __init__(self, model: str, host: Optional[str] = None) -> None:
        self.model = model
        self.host = host or CONFIG.ollama_host

    async def _chat(self, system_prompt: str, user_prompt: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self.host}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # Ollama responses typically include a single message
            content = (
                (data.get("message", {}) or {}).get("content")
                or (data.get("messages", [{}])[-1].get("content") if data.get("messages") else None)
            )
            if not content:
                raise ValueError("Empty response content from Ollama")
            return content

    async def propose_fleet_intent(self, fleet_summary: Dict[str, Any]) -> Dict[str, Any]:
        system = (
            "You are the Fleet Commander for a hostile flotilla. Plan strategy to achieve mission objectives "
            "while minimizing detectability and respecting ROE. You will receive a structured summary of your fleet "
            "and an uncertain belief of enemy contacts. Never assume ground-truth positions. Output only a JSON FleetIntent."
        )
        user = (
            "FLEET_SUMMARY_JSON:\n" + json.dumps(fleet_summary, separators=(",", ":")) +
            "\n\nCONSTRAINTS:\n- Do not reveal or rely on unknown enemy truth.\n- Prefer convoy protection unless ROE authorizes engagement.\n" \
            "- If escorts are low on ammo, bias toward defensive spacing.\n\nProduce FleetIntent JSON."
        )
        content = await self._chat(system, user)
        obj = _extract_json(content)
        if obj is None:
            raise ValueError("Failed to parse FleetIntent JSON from Ollama response")
        return obj

    async def propose_ship_tool(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        system = (
            "You command a single ship. Make conservative, doctrine-aligned decisions based only on your local summary "
            "and the FleetIntent. Never expose or rely on information you do not have. Output exactly one tool call in JSON."
        )
        user = (
            "SHIP_SUMMARY_JSON:\n" + json.dumps(ship_summary, separators=(",", ":")) +
            "\n\nRules:\n- Respect EMCON posture.\n- Obey ROE and captain consent requirement.\n- Use active ping only if allowed and tactically necessary.\n\nOutput a single tool call JSON."
        )
        content = await self._chat(system, user)
        obj = _extract_json(content)
        if obj is None:
            raise ValueError("Failed to parse tool call JSON from Ollama response")
        return obj


class OpenAIAgentsEngine(BaseEngine):
    """Engine using OpenAI Agents SDK primitives (Agents, Tools, Handoffs).

    This implementation keeps the same narrow interface as other engines and enforces
    information boundaries by only passing in already-sanitized summaries from the orchestrator.
    """

    def __init__(self, model: str) -> None:
        # Lazy import to avoid forcing dependency when not used
        from agents import Agent  # type: ignore
        self.Agent = Agent
        self.model = model

    async def propose_fleet_intent(self, fleet_summary: Dict[str, Any]) -> Dict[str, Any]:
        from agents import Runner  # type: ignore

        agent = self.Agent(
            name="FleetCommander",
            instructions=(
                "You are the Fleet Commander for a hostile flotilla. Plan strategy to achieve mission objectives "
                "while minimizing detectability and respecting ROE. You will receive a structured summary of your fleet "
                "and an uncertain belief of enemy contacts. Never assume ground-truth positions. Output only a JSON FleetIntent."
            ),
            model=self.model,
        )
        prompt = (
            "FLEET_SUMMARY_JSON:\n" + json.dumps(fleet_summary, separators=(",", ":")) +
            "\n\nCONSTRAINTS:\n- Do not reveal or rely on unknown enemy truth.\n- Prefer convoy protection unless ROE authorizes engagement.\n"
            "- If escorts are low on ammo, bias toward defensive spacing.\n\nProduce FleetIntent JSON."
        )
        result = await Runner.run(agent, prompt)  # type: ignore[attr-defined]
        content = str(result.final_output)
        obj = _extract_json(content)
        if obj is None:
            raise ValueError("Failed to parse FleetIntent JSON from OpenAI Agents output")
        return obj

    async def propose_ship_tool(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        from agents import Runner  # type: ignore

        agent = self.Agent(
            name=f"ShipCommander-{ship.id}",
            instructions=(
                "You command a single ship. Make conservative, doctrine-aligned decisions based only on your local summary "
                "and the FleetIntent. Never expose or rely on information you do not have. Output exactly one tool call in JSON."
            ),
            model=self.model,
        )
        prompt = (
            "SHIP_SUMMARY_JSON:\n" + json.dumps(ship_summary, separators=(",", ":")) +
            "\n\nRules:\n- Respect EMCON posture.\n- Obey ROE and captain consent requirement.\n- Use active ping only if allowed and tactically necessary.\n\nOutput a single tool call JSON."
        )
        result = await Runner.run(agent, prompt)  # type: ignore[attr-defined]
        content = str(result.final_output)
        obj = _extract_json(content)
        if obj is None:
            raise ValueError("Failed to parse tool call JSON from OpenAI Agents output")
        return obj


