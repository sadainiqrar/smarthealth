"""The chat model, from one factory keyed on `llm_mode` (harness requirement 2, spec 9.2).

- `auto` (the default): `live` when an API key is set, else `fixture` -- so setting
  the key is the whole switch.
- `off`: no model; every typed message is handled offline (offline.py). No key.
- `fixture`: recorded responses (`eval/fixtures/llm.jsonl`), matched by a hash of the
  request. A request never recorded raises `LLMUnavailable`, and the turn falls back to
  offline. No key.
- `record`: the live provider, and every response is appended to the fixture file. Key.
- `live`: the live provider. Key.

The provider and model are settings (`ASSISTANT_LLM_PROVIDER` = anthropic | openai | groq,
`ASSISTANT_LLM_MODEL`, `ASSISTANT_LLM_API_KEY`); the provider package is imported only when
that provider is used, so no LLM library is needed until a key exists (spec O-9).

The request hash covers the messages and the tool schemas, not the model's settings: a
recording stays valid until a prompt, a tool or the conversation changes.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, messages_from_dict, messages_to_dict
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field

DEFAULT_FIXTURES = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "llm.jsonl"


class LLMUnavailable(Exception):
    """No model can answer this request (off, fixture miss, provider down)."""


def request_key(messages: list[BaseMessage], tools: list[dict]) -> str:
    canonical = [
        {
            "type": m.type,
            "content": m.content,
            "tool_calls": [
                {"name": c["name"], "args": c["args"]} for c in getattr(m, "tool_calls", [])
            ],
            "tool_call_id": getattr(m, "tool_call_id", None),
        }
        for m in messages
    ]
    blob = json.dumps({"messages": canonical, "tools": tools}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def load_fixtures(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[record["key"]] = record["response"]
    return records


class _ToolAware(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    tools: list[dict] = Field(default_factory=list)

    def bind_tools(self, tools: list[Any], **_: Any) -> _ToolAware:  # type: ignore[override]
        return self.model_copy(update={"tools": [convert_to_openai_tool(t) for t in tools]})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("the assistant calls models asynchronously")


class FixtureChatModel(_ToolAware):
    """Replays recorded responses; never reaches a network."""

    records: dict[str, dict] = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "fixture"

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        key = request_key(messages, self.tools)
        if key not in self.records:
            raise LLMUnavailable(f"no recorded response for request {key[:12]}")
        [message] = messages_from_dict([self.records[key]])
        return ChatResult(generations=[ChatGeneration(message=message)])


class RecordingChatModel(_ToolAware):
    """The live model, writing every response to the fixture file."""

    inner: Any
    path: Path
    _lock: Any = threading.Lock()

    @property
    def _llm_type(self) -> str:
        return "recording"

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        bound = self.inner.bind_tools(self.tools) if self.tools else self.inner
        response = await bound.ainvoke(messages)
        message = AIMessage(content=response.content, tool_calls=response.tool_calls)
        record = {
            "key": request_key(messages, self.tools),
            "response": messages_to_dict([message])[0],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        return ChatResult(generations=[ChatGeneration(message=message)])


#: The model used when ASSISTANT_LLM_MODEL is empty: switching provider is one setting.
#: Each supports tool calling, which the agent needs (search_knowledge, start_flow).
DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-5-mini",
}


def _live(provider: str, model: str, api_key: str) -> BaseChatModel:
    model = model or DEFAULT_MODELS.get(provider, "")
    if not api_key:
        raise LLMUnavailable("ASSISTANT_LLM_API_KEY is not set")
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model, api_key=api_key, max_tokens=1024, timeout=30, max_retries=2
        )
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key=api_key, timeout=30, max_retries=2)
    if provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(model=model, api_key=api_key, timeout=30, max_retries=2)
    raise ValueError(f"unknown LLM provider {provider!r}")


def chat_model(
    mode: str,
    *,
    provider: str = "",
    model: str = "",
    api_key: str = "",
    fixtures: Path = DEFAULT_FIXTURES,
) -> BaseChatModel | None:
    """The model for this process, or None when typed messages are handled offline."""
    if mode == "auto":
        mode = "live" if api_key else "fixture"
    if mode == "off":
        return None
    if mode == "fixture":
        return FixtureChatModel(records=load_fixtures(fixtures))
    live = _live(provider, model, api_key)
    if mode == "record":
        return RecordingChatModel(inner=live, path=fixtures)
    if mode == "live":
        return live
    raise ValueError(f"unknown llm_mode {mode!r}")
