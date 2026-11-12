"""A small provider interface with an Ollama backend (default, local) and a Claude backend (optional).

Messages use one neutral shape:
    {"role": "system" | "user" | "assistant" | "tool", "content": str,
     "tool_calls": [{"id", "name", "arguments": dict}]   (assistant only)
     "tool_call_id": str, "name": str                      (tool only)}
Tools are {"name", "description", "parameters": JSON schema}.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class Provider(Protocol):
    name: str
    model: str
    local: bool  # True when nothing leaves the user's own machine

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply: ...

    def structured(self, system: str, prompt: str, schema: dict) -> dict: ...

    def health(self) -> dict: ...


def _strip_think(text: str) -> str:
    # qwen3 can emit <think>...</think> inline when thinking isn't split out by the server.
    while "<think>" in text and "</think>" in text:
        a, b = text.index("<think>"), text.index("</think>") + len("</think>")
        text = text[:a] + text[b:]
    return text.strip()


class OllamaProvider:
    name = "ollama"
    local = True

    def __init__(self, url: str, model: str, think: bool, timeout: float):
        self.url = url.rstrip("/")
        self.model = model
        self.think = think
        self.timeout = timeout

    def _post(self, body: dict) -> dict:
        try:
            r = httpx.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise ProviderError(f"Couldn't reach Ollama at {self.url} ({e.__class__.__name__})") from None
        if r.status_code >= 400:
            raise ProviderError(f"Ollama returned {r.status_code}: {r.text[:200]}")
        return r.json()

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        out = []
        for m in messages:
            if m["role"] == "assistant":
                o = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    o["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]
                out.append(o)
            elif m["role"] == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        body = {
            "model": self.model,
            "messages": out,
            "tools": [{"type": "function", "function": t} for t in tools],
            "stream": False,
            "think": self.think,
            "options": {"temperature": 0.2},
        }
        data = self._post(body)
        msg = data.get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(id=c.get("id") or f"call_{uuid.uuid4().hex[:12]}", name=fn.get("name", ""), arguments=args))
        return Reply(content=_strip_think(msg.get("content") or ""), tool_calls=calls)

    def structured(self, system: str, prompt: str, schema: dict) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.3},
        }
        data = self._post(body)
        text = _strip_think((data.get("message") or {}).get("content") or "")
        try:
            return json.loads(text)
        except ValueError:
            raise ProviderError("The model didn't return valid JSON") from None

    def health(self) -> dict:
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
            names = [m.get("name") for m in r.json().get("models", [])]
        except Exception as e:  # noqa: BLE001 - any failure means "not reachable"
            return {"ok": False, "error": f"Couldn't reach Ollama at {self.url} ({e.__class__.__name__})"}
        if self.model not in names and f"{self.model}:latest" not in names:
            return {"ok": False, "error": f"Ollama is running but the model {self.model} isn't pulled (ollama pull {self.model})"}
        return {"ok": True}


class ClaudeProvider:
    """Anthropic API. Only used when MERIDIAN_AI_PROVIDER=claude and an API key is set on the server."""

    name = "claude"
    local = False

    def __init__(self, api_key: str, model: str, timeout: float):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model

    def _convert(self, messages: list[dict]) -> tuple[str, list[dict]]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        out: list[dict] = []
        for m in messages:
            if m["role"] == "system":
                continue
            if m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                # Consecutive tool results go back in one user turn.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m["role"] == "assistant":
                content: list[dict] = []
                if m.get("content"):
                    content.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:
                    content.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]})
                out.append({"role": "assistant", "content": content or [{"type": "text", "text": "(no reply)"}]})
            else:
                out.append({"role": "user", "content": m["content"]})
        return system, out

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        system, msgs = self._convert(messages)
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=msgs,
                tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools],
            )
        except self._anthropic.APIError as e:
            raise ProviderError(f"Claude API error: {e.__class__.__name__}") from None
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, arguments=dict(b.input or {})) for b in resp.content if b.type == "tool_use"]
        return Reply(content=text.strip(), tool_calls=calls)

    def structured(self, system: str, prompt: str, schema: dict) -> dict:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[{"name": "answer", "description": "Return the answer in this shape.", "input_schema": schema}],
                tool_choice={"type": "tool", "name": "answer"},
            )
        except self._anthropic.APIError as e:
            raise ProviderError(f"Claude API error: {e.__class__.__name__}") from None
        for b in resp.content:
            if b.type == "tool_use":
                return dict(b.input or {})
        raise ProviderError("Claude didn't return an answer")

    def health(self) -> dict:
        return {"ok": True}


class NoProvider:
    name = "none"
    model = ""
    local = True

    def chat(self, messages, tools):
        raise ProviderError("The AI assistant is switched off on this server (MERIDIAN_AI_PROVIDER=none)")

    def structured(self, system, prompt, schema):
        raise ProviderError("The AI assistant is switched off on this server (MERIDIAN_AI_PROVIDER=none)")

    def health(self):
        return {"ok": False, "error": "The AI assistant is switched off on this server"}


_override: Provider | None = None


def set_provider_for_tests(p: Provider | None) -> None:
    global _override
    _override = p


def get_provider() -> Provider:
    if _override is not None:
        return _override
    s = get_settings()
    if s.ai_provider == "claude":
        if not s.anthropic_api_key:
            return NoProvider()
        return ClaudeProvider(s.anthropic_api_key, s.claude_model, s.ai_timeout_s)
    if s.ai_provider == "ollama":
        return OllamaProvider(s.ollama_url, s.ollama_model, s.ollama_think, s.ai_timeout_s)
    return NoProvider()


class ScriptedProvider:
    """Test double: each call to chat() pops the next scripted step. A step is a Reply, or a function of the
    messages so far that returns one (so a test can read tool results and react, like a model would)."""

    name = "scripted"
    model = "scripted"

    def __init__(self, steps: list[Reply | Callable[[list[dict]], Reply]] | None = None, local: bool = True, structured_answer: dict | None = None):
        self.steps = list(steps or [])
        self.local = local
        self.seen: list[list[dict]] = []
        self.structured_answer = structured_answer
        self.structured_prompts: list[str] = []

    def chat(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        if not self.steps:
            return Reply(content="(no more scripted replies)")
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step

    def structured(self, system, prompt, schema):
        self.structured_prompts.append(prompt)
        if self.structured_answer is None:
            raise ProviderError("no scripted answer")
        return self.structured_answer

    def health(self):
        return {"ok": True}
