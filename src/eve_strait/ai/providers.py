"""Provider adapters. One class per vendor, no shared request code.

Each vendor gets its own official SDK and its own module-level function; the
two are never blended into one "unified client" that pokes at whichever
happens to be installed. They disagree on tool schema shape, on how tool
results are returned, and on how a refusal surfaces, and papering over that
produces a client that is subtly wrong for both.

What *is* shared is everything above this file: the tool registry, the system
prompt, and the loop in agent.py. This module only turns one turn of that loop
into one vendor's wire format and back.

Neither SDK is a hard dependency of the app. They are imported inside the
functions so a user who never enables the assistant never needs them
installed, and the packaged build does not carry them.
"""
from __future__ import annotations

# Both vendors bill per token; a route conversation is small, but a runaway
# tool loop is not. This caps one exchange.
MAX_ROUNDS = 12


class ProviderError(RuntimeError):
    """Anything the user needs to read: bad key, no quota, refusal."""


class MissingDependency(ProviderError):
    def __init__(self, package: str, provider: str):
        super().__init__(
            f"The {provider} assistant needs the '{package}' package.\n\n"
            f"Install it with:\n    uv pip install {package}")


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
def anthropic_turn(api_key: str, model: str, system: str, messages: list,
                   tools: list) -> dict:
    """One Claude request. Returns {"text", "tool_calls", "messages"}.

    ``messages`` is Anthropic-shaped and is returned extended with the
    assistant turn, so the caller can append tool results and come back.
    """
    try:
        import anthropic
    except ImportError:
        raise MissingDependency("anthropic", "Claude") from None

    client = anthropic.Anthropic(api_key=api_key)
    spec = [{"name": t.name, "description": t.description,
             "input_schema": t.schema} for t in tools]
    try:
        # max_tokens covers thinking *and* the reply on current models, so it
        # is deliberately generous; a tight cap truncates mid-answer.
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=messages,
            tools=spec,
        )
    except anthropic.AuthenticationError:
        raise ProviderError("Claude rejected the API key.") from None
    except anthropic.PermissionDeniedError:
        raise ProviderError(
            "That Claude key lacks access to this model. Note that a "
            "Claude.ai Pro or Max subscription does not include API access; "
            "API usage is billed separately.") from None
    except anthropic.NotFoundError:
        raise ProviderError(f"Claude has no model called {model!r}.") from None
    except anthropic.RateLimitError:
        raise ProviderError("Claude is rate limiting. Wait a moment.") from None
    except anthropic.APIConnectionError:
        raise ProviderError("Could not reach Claude. Check your connection.") from None
    except anthropic.APIStatusError as exc:
        raise ProviderError(f"Claude error {exc.status_code}: {exc.message}") from None

    # Check the stop reason before reading content: a refusal can arrive with
    # an empty or partial content list.
    if response.stop_reason == "refusal":
        raise ProviderError(
            "Claude declined this request. Rephrasing usually clears it; "
            "EVE combat and cyno questions occasionally trip a safety filter.")

    text = "".join(b.text for b in response.content if b.type == "text")
    calls = [{"id": b.id, "name": b.name, "args": b.input}
             for b in response.content if b.type == "tool_use"]
    return {"text": text, "tool_calls": calls,
            "messages": messages + [{"role": "assistant",
                                     "content": response.content}]}


def anthropic_results(messages: list, results: list) -> list:
    """Append tool results. All of them go in ONE user message."""
    return messages + [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
         **({"is_error": True} if r.get("is_error") else {})}
        for r in results]}]


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------
def openai_turn(api_key: str, model: str, system: str, messages: list,
                tools: list, *, base_url: str | None = None,
                headers: dict | None = None, vendor: str = "OpenAI",
                account_note: str = "") -> dict:
    """One turn over the OpenAI chat-completions wire format.

    Parameterised by base URL rather than copied, because OpenRouter speaks
    this exact protocol -- same request shape, same tool-call structure, same
    SDK. The only differences are where it points, two attribution headers it
    asks for, and whose name belongs in an error message.
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise MissingDependency("openai", vendor) from None
    import openai as openai_mod

    client = OpenAI(api_key=api_key, base_url=base_url) if base_url         else OpenAI(api_key=api_key)
    spec = [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": t.schema}} for t in tools]
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}] + messages,
            tools=spec,
            **({"extra_headers": headers} if headers else {}),
        )
    except openai_mod.AuthenticationError:
        raise ProviderError(f"{vendor} rejected the API key.") from None
    except openai_mod.PermissionDeniedError:
        raise ProviderError(
            f"That {vendor} key lacks access to this model."
            + (f" {account_note}" if account_note else "")) from None
    except openai_mod.NotFoundError:
        raise ProviderError(f"{vendor} has no model called {model!r}.") from None
    except openai_mod.RateLimitError:
        raise ProviderError(
            f"{vendor} is rate limiting, or the account is out of "
            "credit.") from None
    except openai_mod.APIConnectionError:
        raise ProviderError(
            f"Could not reach {vendor}. Check your connection.") from None
    except openai_mod.APIStatusError as exc:
        raise ProviderError(
            f"{vendor} error {exc.status_code}: {exc.message}") from None

    msg = response.choices[0].message
    calls = [{"id": c.id, "name": c.function.name,
              "args": _loads(c.function.arguments)}
             for c in (msg.tool_calls or [])]
    # Echo the assistant message back in the shape the API returned it.
    turn = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        turn["tool_calls"] = [
            {"id": c.id, "type": "function",
             "function": {"name": c.function.name,
                          "arguments": c.function.arguments}}
            for c in msg.tool_calls]
    return {"text": msg.content or "", "tool_calls": calls,
            "messages": messages + [turn]}


# OpenRouter asks callers to identify themselves; these show up on their
# rankings page and are how they tell traffic apart. Neither is required.
OPENROUTER_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/littlephish/eve-strait",
    "X-Title": "Eve-Strait",
}


def openrouter_turn(api_key: str, model: str, system: str, messages: list,
                    tools: list) -> dict:
    """OpenRouter, which is the OpenAI wire format pointed somewhere else.

    Worth having as its own provider rather than a base-URL setting on the
    OpenAI one: it takes its own key, its own model ids (always
    ``vendor/model``), and it reaches models from vendors this app has no
    adapter for at all.
    """
    return openai_turn(
        api_key, model, system, messages, tools,
        base_url=OPENROUTER_URL, headers=_OPENROUTER_HEADERS,
        vendor="OpenRouter",
        account_note="OpenRouter models are pay-as-you-go from account "
                     "credit, and some require extra opt-in on their site.")


def openai_results(messages: list, results: list) -> list:
    """Append tool results. OpenAI wants one message *per* result."""
    return messages + [{"role": "tool", "tool_call_id": r["id"],
                        "content": r["content"]} for r in results]


def _loads(raw: str) -> dict:
    import json
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
PROVIDERS = {
    "claude": {
        "label": "Claude (Anthropic)",
        "turn": anthropic_turn,
        "results": anthropic_results,
        "models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
        "keys_url": "https://console.anthropic.com/settings/keys",
        "env": "ANTHROPIC_API_KEY",
        "package": "anthropic",
    },
    "openai": {
        "label": "ChatGPT (OpenAI)",
        "turn": openai_turn,
        "results": openai_results,
        "models": ["gpt-5", "gpt-5-mini", "gpt-4.1"],
        "keys_url": "https://platform.openai.com/api-keys",
        "env": "OPENAI_API_KEY",
        "package": "openai",
    },
    "openrouter": {
        "label": "OpenRouter (many vendors)",
        "turn": openrouter_turn,
        # Same wire format, so the same result shape.
        "results": openai_results,
        # Ids are always vendor/model. The model box is editable and
        # OpenRouter's catalogue changes weekly, so these are a starting
        # point rather than a list to keep current -- paste any id from
        # openrouter.ai/models.
        "models": ["anthropic/claude-sonnet-4.5",
                   "openai/gpt-4o",
                   "google/gemini-2.5-pro",
                   "meta-llama/llama-3.3-70b-instruct",
                   "deepseek/deepseek-chat"],
        "keys_url": "https://openrouter.ai/keys",
        "env": "OPENROUTER_API_KEY",
        "package": "openai",       # its own SDK is not needed
    },
}


def env_var(provider: str) -> str:
    """Environment variable that supplies this provider's key, if any."""
    return (PROVIDERS.get(provider) or {}).get("env", "")


def names() -> list[str]:
    return list(PROVIDERS)
