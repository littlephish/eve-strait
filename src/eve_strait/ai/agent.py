"""The single entry point for the assistant, whichever engine is behind it.

The UI calls ``Agent.ask()`` and never imports a provider module. Swapping
Claude for ChatGPT changes one config value, not any call site.

The loop is written by hand rather than using either SDK's tool-runner helper,
because the same loop has to drive both vendors and the runners are
vendor-specific. It is small: send, run whatever tools came back, send the
results, repeat until the model stops asking for tools.
"""
from __future__ import annotations

import json

from .. import config
from . import providers, tools


class Agent:
    """Holds one conversation. Not thread-safe; call it from one worker."""

    def __init__(self, app):
        self.app = app
        self.messages: list = []
        self.log: list[str] = []          # tool calls made, for the UI

    # -- configuration ------------------------------------------------------
    @staticmethod
    def configured() -> bool:
        """True when a provider has a key AND the panel is switched on.

        The two are independent settings on purpose: turning the panel off
        must not require deleting a key someone may want to switch back on
        later, and it must not silently start working again the next time a
        key happens to be present.
        """
        return (config.get_ai_chat_enabled()
                and bool(config.get_ai_key(config.get_ai_provider())))

    @staticmethod
    def provider_info() -> dict:
        return providers.PROVIDERS[config.get_ai_provider()]

    def reset(self):
        self.messages = []
        self.log = []

    # -- the loop -----------------------------------------------------------
    def ask(self, question: str, progress=None) -> str:
        """Send one user message, run tools until done, return the reply."""
        name = config.get_ai_provider()
        info = providers.PROVIDERS.get(name)
        if info is None:
            raise providers.ProviderError(f"Unknown AI provider {name!r}.")
        key = config.get_ai_key(name)
        if not key:
            raise providers.ProviderError(
                f"No API key set for {info['label']}.")
        model = config.get_ai_model(name) or info["models"][0]

        self.messages.append({"role": "user", "content": question})
        self.log = []

        for _ in range(providers.MAX_ROUNDS):
            if progress:
                progress("Thinking...")
            turn = info["turn"](key, model, tools.SYSTEM_PROMPT,
                                self.messages, tools.TOOLS)
            self.messages = turn["messages"]
            if not turn["tool_calls"]:
                return turn["text"] or "(no reply)"

            results = []
            for call in turn["tool_calls"]:
                if progress:
                    progress(f"Running {call['name']}...")
                results.append(self._run(call))
            self.messages = info["results"](self.messages, results)

        return ("I stopped after too many tool calls without reaching an "
                "answer. Try asking something narrower.")

    def _run(self, call: dict) -> dict:
        """Execute one tool call. Errors come back as results, not raises.

        A raised exception would end the conversation; a tool_result marked as
        an error lets the model read the message and correct itself, which is
        usually what happens with a mistyped system name.
        """
        tool = tools.BY_NAME.get(call["name"])
        if tool is None:
            return {"id": call["id"], "content": f"No such tool {call['name']!r}.",
                    "is_error": True}
        args = call.get("args") or {}
        self.log.append(f"{tool.name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
        try:
            out = self.app.run_ai_tool(tool, args)
        except Exception as exc:                       # surfaced to the model
            return {"id": call["id"], "content": f"{type(exc).__name__}: {exc}",
                    "is_error": True}
        if not isinstance(out, str):
            out = json.dumps(out)
        return {"id": call["id"], "content": out}
