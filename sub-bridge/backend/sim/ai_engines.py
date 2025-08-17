from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import httpx
from openai import AsyncOpenAI

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
        self._last_call_meta: Dict[str, Any] | None = None

    async def _chat(self, system_prompt: str, user_prompt: str) -> str:
        started_ms = None
        try:
            started_ms = httpx.Timeout(0).start if hasattr(httpx, "Timeout") else None  # placeholder; we compute below
        except Exception:
            started_ms = None
        t0 = None
        try:
            import time as _time
            t0 = _time.perf_counter()
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
                dur_ms = int(((_time.perf_counter() - t0) * 1000.0)) if t0 is not None else None
                resp.raise_for_status()
                data = resp.json()
                content = (
                    (data.get("message", {}) or {}).get("content")
                    or (data.get("messages", [{}])[-1].get("content") if data.get("messages") else None)
                )
                if not content:
                    raise ValueError("Empty response content from Ollama")
                # Save call metadata for UI/debug
                self._last_call_meta = {
                    "provider": "ollama",
                    "url": f"{self.host}/api/chat",
                    "status": resp.status_code,
                    "duration_ms": dur_ms,
                    "model": self.model,
                    "response_bytes": len(resp.content or b""),
                }
                return content
        except Exception as e:
            # Attach failure meta for visibility
            import time as _time
            dur_ms = int(((_time.perf_counter() - t0) * 1000.0)) if t0 is not None else None
            self._last_call_meta = {
                "provider": "ollama",
                "url": f"{self.host}/api/chat",
                "status": None,
                "duration_ms": dur_ms,
                "model": self.model,
                "error": str(e),
            }
            raise

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
        obj = _extract_json(content)
        if obj is None:
            raise ValueError("Failed to extract FleetIntent JSON from Ollama output")
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
            raise ValueError("Failed to extract ToolCall JSON from Ollama output")
        return obj


class OpenAIAgentsEngine(BaseEngine):
    """Engine using OpenAI Chat Completions API.

    - No dependency on third-party "agents" SDK.
    - Captures metadata (request id, token usage, duration) for debugging.
    - Enforces information boundaries by only passing sanitized summaries.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self._client: AsyncOpenAI | None = None
        self._last_call_meta: Dict[str, Any] | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(api_key=CONFIG.openai_api_key, base_url=CONFIG.openai_base_url)
        return self._client

    async def _chat(self, system_prompt: str, user_prompt: str) -> str:
        import time as _time
        t0 = _time.perf_counter()
        client = self._get_client()
        try:
            resp = await client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
            )
            dur_ms = int(((_time.perf_counter() - t0) * 1000.0))
            choice = (resp.choices or [None])[0]
            content = choice.message.content if choice and choice.message else None
            if not content:
                raise ValueError("Empty content from OpenAI chat completion")
            # Capture metadata
            usage = getattr(resp, "usage", None)
            self._last_call_meta = {
                "provider": "openai",
                "model": self.model,
                "duration_ms": dur_ms,
                "id": getattr(resp, "id", None),
                "usage": {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                    "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                    "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
                },
            }
            return content
        except Exception as e:
            dur_ms = int(((_time.perf_counter() - t0) * 1000.0))
            self._last_call_meta = {
                "provider": "openai",
                "model": self.model,
                "duration_ms": dur_ms,
                "error": str(e),
            }
            raise

    async def propose_fleet_intent(self, fleet_summary: Dict[str, Any]) -> Dict[str, Any]:
        # Honor explicit prompt hint if provided by orchestrator for exact reproducibility
        hint = fleet_summary.get("_prompt_hint") if isinstance(fleet_summary, dict) else None
        if isinstance(hint, dict) and hint.get("system_prompt") and hint.get("user_prompt"):
            system_prompt = str(hint.get("system_prompt"))
            user_prompt = str(hint.get("user_prompt"))
            content = await self._chat(system_prompt, user_prompt)
            obj = _extract_json(content)
            if obj is None:
                raise ValueError("Failed to parse FleetIntent JSON from OpenAI response")
            return obj
        system = (
            "You are the RED Fleet Commander. Define mid-level FleetIntent that encodes strategy and objectives; do not micromanage tactics. "
            "Use only the provided summaries; never assume ground-truth enemy positions. Coordinates: X east (m), Y north (m). Output ONLY one JSON object (no markdown):\n"
            "{\n"
            '  "objectives": {"<ship_id>": {"destination": [x, y], "speed_kn": 12, "goal": "one sentence"}},\n'
            '  "emcon": {"active_ping_allowed": false, "radio_discipline": "restricted"},\n'
            '  "summary": "One short sentence describing the fleet plan",\n'
            '  "notes": [{"ship_id": "<id>" | null, "text": "<advisory>"}]\n'
            "}"
        )
        fs = dict(fleet_summary)
        fs.pop("_prompt_hint", None)
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
        obj = _extract_json(content)
        if obj is None:
            raise ValueError("Failed to parse FleetIntent JSON from OpenAI response")
        return obj

    async def propose_ship_tool(self, ship: Ship, ship_summary: Dict[str, Any]) -> Dict[str, Any]:
        # Honor explicit prompt hint if provided by orchestrator for exact reproducibility
        hint = ship_summary.get("_prompt_hint") if isinstance(ship_summary, dict) else None
        if isinstance(hint, dict) and hint.get("system_prompt") and hint.get("user_prompt"):
            system_prompt = str(hint.get("system_prompt"))
            user_prompt = str(hint.get("user_prompt"))
            content = await self._chat(system_prompt, user_prompt)
            obj = _extract_json(content)
            if obj is None:
                raise ValueError("Failed to parse ToolCall JSON from OpenAI response")
            return obj
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
            raise ValueError("Failed to parse ToolCall JSON from OpenAI response")
        return obj


