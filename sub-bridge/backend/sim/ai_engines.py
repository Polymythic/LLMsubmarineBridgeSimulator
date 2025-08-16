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
    
    # Try to find JSON after common prefixes
    prefixes = ["Here's the FleetIntent:", "FleetIntent:", "JSON:", "Response:", "Here's the plan:"]
    for prefix in prefixes:
        if prefix in text:
            start = text.find(prefix) + len(prefix)
            # Find first { after prefix
            brace_start = text.find("{", start)
            if brace_start != -1:
                # Find matching closing brace
                depth = 0
                for i in range(brace_start, len(text)):
                    ch = text[i]
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            candidate = text[brace_start:i+1]
                            try:
                                return json.loads(candidate)
                            except Exception:
                                break
                break
    
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
    
    # Last resort: try to clean up common Ollama formatting issues
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned[3:-3].strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:].strip()
    
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    
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
        # Honor explicit prompt hint if provided by orchestrator for exact reproducibility
        hint = fleet_summary.get("_prompt_hint") if isinstance(fleet_summary, dict) else None
        if isinstance(hint, dict) and hint.get("system_prompt") and hint.get("user_prompt"):
            system = str(hint.get("system_prompt"))
            user = str(hint.get("user_prompt"))
            content = await self._chat(system, user)
        else:
            system = (
                "You are the RED Fleet Commander. Define mid-level FleetIntent that encodes strategy and objectives; do not micromanage tactics. "
                "Use only the provided summaries; never assume ground-truth enemy positions. "
                "Coordinates: X east (m), Y north (m). Output ONLY one JSON object (no markdown):\n"
                "{\n"
                '  "objectives": {"<ship_id>": {"destination": [x, y], "speed_kn": 12, "goal": "one sentence"}},\n'
                '  "emcon": {"active_ping_allowed": false, "radio_discipline": "restricted"},\n'
                '  "summary": "One short sentence describing the fleet plan",\n'
                '  "notes": [{"ship_id": "<id>" | null, "text": "<advisory>"}]\n'
                "}"
            )
            fs = dict(fleet_summary)
            fs.pop("_prompt_hint", None)
            # Ensure mission_summary is included if present
            mission = fs.get("mission") or {}
            if mission and mission.get("mission_summary") is None and fs.get("objective"):
                mission["mission_summary"] = fs.get("objective")
                fs["mission"] = mission
            user = (
                "FLEET_SUMMARY_JSON:\n" + json.dumps(fs, separators=(",", ":")) +
                "\n\nFORMAT REQUIREMENTS:\n"
                "- Include EVERY RED ship id under 'objectives' with a 'destination' [x,y] in meters.\n"
                "- 'speed_kn' and 'goal' are optional per ship.\n"
                "- Output ONLY the JSON object with allowed keys shown above. No extra prose.\n"
                "- Do not infer unknown enemy truth beyond the provided beliefs."
            )
            content = await self._chat(system, user)
        print(f"Ollama Fleet Commander response: {content[:200]}...")  # Debug: show first 200 chars
        obj = _extract_json(content)
        if obj is None:
            print(f"Failed to extract JSON from: {content}")  # Debug: show full response on failure
            # Graceful no-op: return empty intent so orchestrator can continue
            return {"objectives": [], "groups": {}, "target_priority": [], "engagement_rules": {}, "emcon": {}}
        print(f"Extracted FleetIntent: {obj}")  # Debug: show parsed result
        return obj

    async def propose_ship_tool(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        # Honor explicit prompt hint if provided by orchestrator for exact reproducibility
        hint = ship_summary.get("_prompt_hint") if isinstance(ship_summary, dict) else None
        if isinstance(hint, dict) and hint.get("system_prompt") and hint.get("user_prompt"):
            system = str(hint.get("system_prompt"))
            user = str(hint.get("user_prompt"))
            content = await self._chat(system, user)
        else:
            system = (
                "You command a single RED ship. Make tactical decisions using only your Ship Summary and the FleetIntent. "
                "Follow FleetIntent when possible; if immediate safety or opportunity requires otherwise, prefix the summary with 'deviate:'. "
                "Coordinates: X east (m), Y north (m). Bearings: 0°=North, 90°=East. "
                "Output EXACTLY one JSON object with keys {tool, arguments, summary}. No markdown or extra keys. Allowed tools: "
                "set_nav(heading: float 0-359.9, speed: float >=0, depth: float >=0); "
                "fire_torpedo(tube: int, bearing: float 0-359.9, run_depth: float, enable_range: float); "
                "deploy_countermeasure(type: 'noisemaker'|'decoy'). Use only tools supported by your capabilities."
            )
            ss = dict(ship_summary)
            ss.pop("_prompt_hint", None)
            user = (
                "SHIP_SUMMARY_JSON:\n" + json.dumps(ss, separators=(",", ":")) +
                "\n\nFORMAT & BEHAVIOR:\n"
                "- Prefer the FleetIntent; if deviating, prefix summary with 'deviate:'.\n"
                "- Use only allowed tools supported by capabilities. Choose plausible parameters (e.g., bearings from contacts).\n"
                "- If no change is needed, return set_nav holding current values with a brief summary.\n"
                "- Output ONLY one JSON with keys {tool, arguments, summary}."
            )
            content = await self._chat(system, user)
        obj = _extract_json(content)
        if obj is None:
            # Fallback handled by orchestrator validation; return an impossible tool to trigger fallback
            return {"tool": "unknown", "arguments": {}}
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
        # Honor explicit prompt hint if provided by orchestrator for exact reproducibility
        hint = fleet_summary.get("_prompt_hint") if isinstance(fleet_summary, dict) else None
        if isinstance(hint, dict) and hint.get("system_prompt") and hint.get("user_prompt"):
            system_prompt = str(hint.get("system_prompt"))
            user_prompt = str(hint.get("user_prompt"))
            content = user_prompt  # default fallback
            # Optional tracing using OpenAI Agents SDK's trace() if available
            try:
                from agents import trace  # type: ignore
                from agents import Runner  # type: ignore
                agent = self.Agent(name="FleetCommander", instructions=system_prompt, model=self.model)
                with trace(name="fleet.propose_intent", metadata={"summary_size": len(user_prompt)}):  # type: ignore
                    result = await Runner.run(agent, user_prompt)  # type: ignore[attr-defined]
                    content = str(result.final_output)
            except Exception:
                # As a last resort, try to parse the provided user prompt directly (should still be JSON per our hints)
                content = user_prompt
            obj = _extract_json(content)
            if obj is None:
                raise ValueError("Failed to parse FleetIntent JSON from OpenAI Agents output")
            return obj
        # Optional tracing using OpenAI Agents SDK's trace() if available
        try:
            from agents import trace  # type: ignore
        except Exception:  # fallback no-op
            class trace:  # type: ignore
                def __init__(self, *_args, **_kwargs):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *_args):
                    return False
        from agents import Runner  # type: ignore

        agent = self.Agent(
            name="FleetCommander",
            instructions=(
                "You are the RED Fleet Commander. Define mid-level FleetIntent that encodes strategy and objectives; do not micromanage tactics. "
                "Use only the provided summaries; never assume ground-truth enemy positions. Coordinates: X east (m), Y north (m). "
                "Output ONLY one JSON object (no markdown) with fields: objectives (per ship: destination [x,y], optional speed_kn, goal), emcon (optional), summary, notes (optional)."
            ),
            model=self.model,
        )
        fs = dict(fleet_summary)
        for k in ("_prompt_hint",):
            fs.pop(k, None)
        prompt = (
            "FLEET_SUMMARY_JSON:\n" + json.dumps(fs, separators=(",", ":")) +
            "\n\nFORMAT REQUIREMENTS:\n"
            "- Include EVERY RED ship id under 'objectives' with a 'destination' [x,y] in meters.\n"
            "- 'speed_kn' and 'goal' are optional per ship.\n"
            "- Output ONLY the JSON object with allowed keys shown above. No extra prose.\n"
            "- Do not infer unknown enemy truth beyond the provided beliefs.\n"
        )
        with trace(name="fleet.propose_intent", metadata={"summary_size": len(str(fleet_summary))}):  # type: ignore
            result = await Runner.run(agent, prompt)  # type: ignore[attr-defined]
            content = str(result.final_output)
        obj = _extract_json(content)
        if obj is None:
            raise ValueError("Failed to parse FleetIntent JSON from OpenAI Agents output")
        return obj

    async def propose_ship_tool(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        # Honor explicit prompt hint if provided by orchestrator for exact reproducibility
        hint = ship_summary.get("_prompt_hint") if isinstance(ship_summary, dict) else None
        if isinstance(hint, dict) and hint.get("system_prompt") and hint.get("user_prompt"):
            system_prompt = str(hint.get("system_prompt"))
            user_prompt = str(hint.get("user_prompt"))
            content = user_prompt
            try:
                from agents import trace  # type: ignore
                from agents import Runner  # type: ignore
                agent = self.Agent(name=f"ShipCommander-{ship.id}", instructions=system_prompt, model=self.model)
                with trace(name=f"ship.propose_tool.{ship.id}", metadata={"summary_size": len(user_prompt)}):  # type: ignore
                    result = await Runner.run(agent, user_prompt)  # type: ignore[attr-defined]
                    content = str(result.final_output)
            except Exception:
                content = user_prompt
            obj = _extract_json(content)
            if obj is None:
                raise ValueError("Failed to parse tool call JSON from OpenAI Agents output")
            return obj
        # Optional tracing using OpenAI Agents SDK's trace() if available
        try:
            from agents import trace  # type: ignore
        except Exception:  # fallback no-op
            class trace:  # type: ignore
                def __init__(self, *_args, **_kwargs):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *_args):
                    return False
        from agents import Runner  # type: ignore

        agent = self.Agent(
            name=f"ShipCommander-{ship.id}",
            instructions=(
                "You command a single RED ship. Make tactical decisions using only your Ship Summary and the FleetIntent. "
                "Prefer following FleetIntent; if deviating, prefix summary with 'deviate:'. Bearings: 0°=North, 90°=East. Output ONLY one JSON object with keys {tool, arguments, summary}. No markdown or extra keys."
            ),
            model=self.model,
        )
        prompt = (
            "SHIP_SUMMARY_JSON:\n" + json.dumps(ship_summary, separators=(",", ":")) +
            "\n\nFORMAT & BEHAVIOR:\n"
            "- Prefer the FleetIntent; if deviating, prefix summary with 'deviate:'.\n"
            "- Use only allowed tools supported by capabilities; choose plausible parameters. If no change needed, return set_nav with current values and a brief summary.\n"
        )
        with trace(name=f"ship.propose_tool.{ship.id}", metadata={"summary_size": len(str(ship_summary))}):  # type: ignore
            result = await Runner.run(agent, prompt)  # type: ignore[attr-defined]
            content = str(result.final_output)
        obj = _extract_json(content)
        if obj is None:
            raise ValueError("Failed to parse tool call JSON from OpenAI Agents output")
        return obj


