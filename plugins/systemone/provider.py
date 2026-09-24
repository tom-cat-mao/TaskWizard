"""System One as a LangChain chat model + provider-plane specs.

The adapter is deliberately honest about what System One is: a typed,
single-shot decision endpoint, not a chat completion API.  It is wrapped as a
``BaseChatModel`` only so that the harness's ordinary role/model routing
(``provider:model`` references, ``build_model_from_resolved``) can reach it —
the wire format underneath is the official protocol in :mod:`client`.

Chat-adapter convention (documented here, in ``pages/plugins.md`` and in the
plugin README, because it is a *convention*, not a protocol feature)::

    state  = the text of the FINAL user message
    choice = if the message carries a trailing ``Options: A | B | C`` marker
             line, one ``choice`` question is asked with that ordered option set
             as its criteria map, and the chosen option's text is the reply
    else   = one ``noul`` question ("Does the state warrant a yes?") whose
             reply is ``"yes"`` or ``"no"`` (the answer probability thresholded
             at 0.5 by the client)

Everything else about the message list — system prompt, history, images — is
NOT sent: System One takes one unstructured state value.  The marker line is the
only machine-readable channel the caller has, which is why an answer that is not
one of the offered options fails visibly instead of being passed through.

Usage and accounting: a reply always carries ``usage``
(``input_tokens``/``output_tokens``), which is translated into LangChain
``usage_metadata`` on the returned ``AIMessage``, so the harness's existing
per-role token accounting (trace ``model_call``, the budget middleware) works for
this transport exactly as it does for the built-in ones.

Build-time failure visibility (P0 #8): an explicit ``provider:model`` reference
must never degrade into another endpoint.  The builder therefore refuses to
construct a model when the referenced backend cannot serve it — no
``TYPESAFE_API_KEY`` in the environment for ``jev``, or no TCP listener at the
configured ``kev`` base URL — and raises instead of returning a client that
fails on the first call.  Sampling parameters, thinking levels and streaming
have no protocol equivalent and are ignored on this transport (documented, not
rejected, so a global ``PHONE_AGENT_TEMPERATURE`` cannot break an unrelated
deployment).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from phone_agent.v2.providers import ModelSpec, ProviderSpec

#: The api family this plugin contributes (``register_api_builder``).
API_FAMILY = "systemone"

#: Trailing marker line that turns a message into a ``choice`` question.
OPTIONS_MARKER_RE = re.compile(
    r"^[ \t>*-]*options[ \t]*:[ \t]*(?P<options>.+)$", re.IGNORECASE
)

NOUL_QUESTION_INSTRUCTIONS = "Does the state warrant a yes?"
CHOICE_QUESTION_INSTRUCTIONS = (
    "Which of the listed options is the best answer for the state? "
    "Answer with exactly one of the criteria keys."
)
_TRUE_TOKENS = frozenset({"yes", "true", "1", "y"})
_FALSE_TOKENS = frozenset({"no", "false", "0", "n"})

#: Injected seam: ``(backend, model_id, base_url) -> client``.  The client is
#: duck-typed (``.ask(state, questions) -> reply`` with ``.answers`` / ``.usage``);
#: plugin.py supplies the real one so this module stays protocol-agnostic and
#: offline-testable.
ClientFactory = Callable[[str, str, str], Any]


class SystemOneAdapterError(RuntimeError):
    """The transport answered, but not in a shape this convention can use."""


def parse_options_marker(text: Any) -> list[str] | None:
    """Return the options of the last ``Options: a | b | c`` line, or ``None``.

    The marker is the caller's machine-readable channel: the modelled message
    must therefore *offer* at least two non-empty options, otherwise the line is
    treated as ordinary prose (and the call becomes a ``noul`` question).
    """

    found: list[str] | None = None
    for line in str(text or "").splitlines():
        match = OPTIONS_MARKER_RE.match(line)
        if match is None:
            continue
        options = [part.strip() for part in match.group("options").split("|")]
        options = [option for option in options if option]
        if len(options) >= 2:
            found = options
    return found


def message_text(content: Any) -> str:
    """Best-effort text of one message content (str or multimodal block list)."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def final_user_text(messages: Sequence[Any]) -> str:
    """The state string: the text of the final human/user message."""

    for message in reversed(list(messages or ())):
        role = str(
            getattr(message, "type", None) or getattr(message, "role", "") or ""
        ).lower()
        if role in {"human", "user"}:
            return message_text(getattr(message, "content", message))
    return ""


def _as_yes_no(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    token = str(value).strip().lower()
    if token in _TRUE_TOKENS:
        return "yes"
    if token in _FALSE_TOKENS:
        return "no"
    raise SystemOneAdapterError(
        f"systemone noul answer is not a yes/no value: {value!r}"
    )


def _usage_from(usage: Mapping[str, Any]) -> dict[str, int] | None:
    """Translate a protocol ``usage`` block into LangChain ``usage_metadata``.

    Only integer values with a known meaning are translated; an unknown or
    absent block yields ``None`` so no invented number reaches the ledger.
    """

    if not usage:
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    values = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
    }
    cleaned: dict[str, int] = {}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        cleaned[key] = value
    if not cleaned:
        return None
    cleaned.setdefault("input_tokens", 0)
    cleaned.setdefault("output_tokens", 0)
    cleaned.setdefault(
        "total_tokens", cleaned["input_tokens"] + cleaned["output_tokens"]
    )
    return cleaned


class SystemOneChatModel(BaseChatModel):
    """``BaseChatModel`` facade over the System One decision protocol.

    One ``invoke`` is exactly one protocol call.  The convention above decides
    whether it is a ``choice`` or a ``noul`` question; nothing about the
    transcript beyond the final user message is transmitted.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    backend: str = ""
    model_id: str = ""
    base_url: str = ""
    request_timeout: float = 10.0
    max_retries: int = 0
    # The transport client (duck-typed, injected at build time).  Excluded from
    # serialization and repr: it holds the API key.
    client: Any = Field(default=None, exclude=True, repr=False)

    @property
    def _llm_type(self) -> str:
        return "systemone"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model_id,
            "base_url": self.base_url,
        }

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> Any:
        """Refuse tool binding: a typed-answer model cannot emit tool calls.

        Raising keeps the failure where it belongs — at assembly, for the
        deployment that referenced System One as the *actor* — instead of
        producing a run that silently never acts.
        """

        raise NotImplementedError(
            "System One is a typed-answer decision model: it cannot emit tool "
            "calls, so it cannot serve as the actor model. Reference it from a "
            "text-only role instead (e.g. PHONE_AGENT_SAFETY_REVIEWER_MODEL="
            "kev:kev-4b@qwen3) — see plugins/systemone/README.md."
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.client is None:
            raise SystemOneAdapterError(
                "systemone chat model has no client (built without one)"
            )
        state = final_user_text(messages)
        if not state.strip():
            raise SystemOneAdapterError(
                "systemone needs a final user message to use as the state"
            )
        options = parse_options_marker(state)
        if options is not None:
            question: dict[str, Any] = {
                "type": "choice",
                # A ``choice`` question's criteria is an option -> description
                # map; the adapter has no descriptions to offer, so every value
                # is null while the key order carries the option order.
                "criteria": {option: None for option in options},
                "instructions": CHOICE_QUESTION_INSTRUCTIONS,
            }
        else:
            question = {
                "type": "noul",
                "instructions": NOUL_QUESTION_INSTRUCTIONS,
            }
        reply = self.client.ask(state, {"reply": question})
        answer = reply.answer("reply")
        text = str(answer.value) if options is not None else _as_yes_no(answer.value)
        if options is not None and text not in options:
            raise SystemOneAdapterError(
                f"systemone chose {text!r}, which is not one of the offered options "
                f"{options!r}; the choice question was not answerable as written"
            )
        message = AIMessage(
            content=text,
            usage_metadata=_usage_from(getattr(reply, "usage", {}) or {}),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _declared(config: Any, key: str, fallback: Any) -> Any:
    """Read one of this plugin's declared settings off the run config.

    Declared keys land on ``V2Config.plugin_settings`` (read-only mirror of what
    ``ctx.register_setting`` resolved).  The closure's value is the fallback so
    the builder also works with a config that never saw the plugin (offline
    tests, direct builder calls).
    """

    settings = getattr(config, "plugin_settings", None)
    if isinstance(settings, Mapping) and key in settings:
        return settings[key]
    return fallback


def make_api_builder(
    make_client: ClientFactory,
    *,
    probe: Callable[[str], None] | None = None,
    timeout: float = 10.0,
    max_retries: int = 2,
) -> Callable[..., SystemOneChatModel]:
    """Build the transport builder registered under :data:`API_FAMILY`.

    ``make_client`` constructs the protocol client for one backend/model; it is
    called once per build, so a missing credential or an unreachable endpoint
    fails at **build time** (the reference is explicit — P0 #8).  ``probe`` is
    an optional ``base_url -> None`` liveness check the plugin supplies for the
    local backend; ``None`` skips it (used by offline tests that inject a fake
    client).  ``timeout`` / ``max_retries`` are the resolved declared settings
    and are re-read per build from ``config.plugin_settings`` when present.
    """

    def build(
        provider: ProviderSpec,
        model: ModelSpec,
        config: Any,
        *,
        sampling: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        level: str = "",
        compat: Any = None,
        streaming: bool = False,
    ) -> SystemOneChatModel:
        backend = str(provider.id or "").strip().lower()
        base_url = str(provider.base_url or "").strip()
        if not base_url:
            raise ValueError(
                f"systemone provider {provider.id!r} requires a base_url "
                "(the plugin sets the documented default; an override must not be empty)"
            )
        if backend == "jev" and not provider.api_key:
            raise ValueError(
                "systemone backend 'jev' requires an API key: set TYPESAFE_API_KEY "
                "in the environment (the plugin never falls back to another endpoint)"
            )
        if probe is not None:
            probe(base_url)
        effective_timeout = float(_declared(config, "systemone_timeout", timeout))
        effective_retries = int(_declared(config, "systemone_retries", max_retries))
        client = make_client(backend, str(model.id), base_url)
        return SystemOneChatModel(
            backend=backend,
            model_id=str(model.id),
            base_url=base_url,
            request_timeout=effective_timeout,
            max_retries=max(0, effective_retries),
            client=client,
        )

    return build


def build_provider_specs(
    *,
    catalogs: Mapping[str, Sequence[str]],
    base_urls: Mapping[str, str],
    jev_api_key: str | None = None,
) -> tuple[ProviderSpec, ...]:
    """The two declared providers (``jev`` cloud, ``kev`` local).

    ``catalogs`` documents the model lists so a ``provider:model`` reference can
    pin a version; ``api_key`` stays ``None`` for the local backend (it needs no
    auth).  The key is never stored anywhere else in the plugin.
    """

    specs: list[ProviderSpec] = []
    for backend in ("jev", "kev"):
        models = {
            model_id: ModelSpec(
                id=model_id,
                name=f"System One {backend} {model_id}",
                input_modalities=("text",),
            )
            for model_id in catalogs.get(backend, ())
        }
        if not models:
            raise ValueError(f"systemone backend {backend!r} declares no models")
        specs.append(
            ProviderSpec(
                id=backend,
                api=API_FAMILY,
                base_url=str(base_urls.get(backend, "")).strip() or None,
                api_key=jev_api_key if backend == "jev" else None,
                models=models,
            )
        )
    return tuple(specs)


__all__ = [
    "API_FAMILY",
    "CHOICE_QUESTION_INSTRUCTIONS",
    "NOUL_QUESTION_INSTRUCTIONS",
    "OPTIONS_MARKER_RE",
    "SystemOneAdapterError",
    "SystemOneChatModel",
    "build_provider_specs",
    "final_user_text",
    "make_api_builder",
    "message_text",
    "parse_options_marker",
]
