"""models.json single-level loader with explicit-file priority (S4/P2).

Search order (lowest to highest priority; later files win):

1. project level ``.taskwizard.models.json`` (cwd)
2. ``PHONE_AGENT_MODELS_FILE`` (V2Config.models_file) — an explicit extra file

(The former user-level ``~/.config/taskwizard/models.json`` layer was removed:
one project, one registry — no hidden global state.)

Merge semantics (pi-style): providers merge by id, models upsert by id, and a
provider entry that only carries baseUrl/headers/compat keeps the existing
model catalog intact.  Top-level ``roles`` entries (per-role session-level
call configuration, :class:`~phone_agent.v2.providers.types.RoleSpec`) upsert
wholesale per role name — the later file's entry for a role replaces the
earlier one.  Values (apiKey, baseUrl, header values) support ``$ENV`` /
``${ENV}`` interpolation via :mod:`providers.values`.

Role-section schema is fail-closed (:class:`ModelsFileError`): unknown role
names and illegal thinking levels are rejected at parse time.  As with every
parse error, :func:`build_provider_registry` converts the failure into
``None`` (log + fall back to the legacy single-gateway behavior) — the
provider layer never crashes a run.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from phone_agent.v2.providers.roles import ROLES
from phone_agent.v2.providers.types import (
    RoleSpec,
    THINKING_LEVELS,
    ProviderCompat,
    ProviderSpec,
    ModelSpec,
)
from phone_agent.v2.providers.values import resolve_headers, resolve_str

logger = logging.getLogger(__name__)

PROJECT_MODELS_NAME = ".taskwizard.models.json"

_CAMEL_TO_SNAKE = {
    "baseUrl": "base_url",
    "apiKey": "api_key",
    "contextWindow": "context_window",
    "maxTokens": "max_tokens",
    "inputModalities": "input_modalities",
    "samplingParams": "sampling_params",
    "thinkingLevelMap": "thinking_level_map",
    "modelOverrides": "model_overrides",
    # compat sub-keys
    "supportsUsageInStreaming": "supports_usage_in_streaming",
    "maxTokensField": "max_tokens_field",
    "thinkingFormat": "thinking_format",
    "supportsParallelToolCalls": "supports_parallel_tool_calls",
    "extraBody": "extra_body",
}


class ModelsFileError(Exception):
    """A models.json file is malformed or references undefined env vars."""


def candidate_paths(config: Any = None) -> list[Path]:
    """Return the ordered models.json candidates (low -> high priority).

    Single-level (P2): the project file plus, when configured, the explicit
    ``PHONE_AGENT_MODELS_FILE`` — no user-level layer.
    """

    paths = [Path.cwd() / PROJECT_MODELS_NAME]
    env_file = getattr(config, "models_file", None)
    if env_file:
        paths.append(Path(env_file))
    return paths


def _snake(name: str) -> str:
    return _CAMEL_TO_SNAKE.get(name, name)


def _parse_compat(data: Any) -> ProviderCompat:
    if not isinstance(data, dict):
        raise ModelsFileError(f"compat must be an object, got {type(data).__name__}")
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        field = _snake(str(key))
        if field == "extra_body":
            if not isinstance(value, dict):
                raise ModelsFileError("compat.extra_body must be an object")
            kwargs[field] = dict(value)
        elif field in {
            "supports_usage_in_streaming",
            "max_tokens_field",
            "thinking_format",
            "supports_parallel_tool_calls",
        }:
            kwargs[field] = value
        # Unknown compat keys are ignored (forward compatibility).
    return ProviderCompat(**kwargs)


def _parse_thinking_map(value: Any) -> Any:
    if value is None:
        return None  # explicit null -> unsupported
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return dict(value)
    raise ModelsFileError(
        f"thinkingLevelMap must be a string, object, or null, got {type(value).__name__}"
    )


def _parse_model(data: Any, *, env: dict[str, str]) -> tuple[str, ModelSpec]:
    if not isinstance(data, dict):
        raise ModelsFileError(f"model entry must be an object, got {type(data).__name__}")
    model_id = resolve_str(data.get("id"), env=env)
    if not model_id:
        raise ModelsFileError("model entry requires an 'id'")
    kwargs: dict[str, Any] = {"id": model_id}
    name = data.get("name")
    if name is not None:
        kwargs["name"] = str(name)
    for json_key, field in (
        ("contextWindow", "context_window"),
        ("maxTokens", "max_tokens"),
    ):
        if data.get(json_key) is not None:
            kwargs[field] = int(data[json_key])
    if data.get("reasoning") is not None:
        kwargs["reasoning"] = bool(data["reasoning"])
    modalities = data.get("inputModalities")
    if modalities is not None:
        if not isinstance(modalities, list):
            raise ModelsFileError("inputModalities must be a list")
        kwargs["input_modalities"] = tuple(str(item) for item in modalities)
    sampling = data.get("samplingParams")
    if sampling is not None:
        if not isinstance(sampling, dict):
            raise ModelsFileError("samplingParams must be an object")
        kwargs["sampling_params"] = dict(sampling)
    if "thinkingLevelMap" in data:
        kwargs["thinking_level_map"] = _parse_thinking_map(data["thinkingLevelMap"])
    headers = data.get("headers")
    if headers is not None:
        if not isinstance(headers, dict):
            raise ModelsFileError("model headers must be an object")
        kwargs["headers"] = resolve_headers(headers, env=env)
    compat = data.get("compat")
    if compat is not None:
        kwargs["compat"] = _parse_compat(compat)
    return model_id, ModelSpec(**kwargs)


def _apply_model_override(existing: ModelSpec, patch: Any) -> ModelSpec:
    """Apply a partial modelOverrides entry onto an existing ModelSpec."""

    if not isinstance(patch, dict):
        raise ModelsFileError("modelOverrides entries must be objects")
    fields: dict[str, Any] = {
        "name": existing.name,
        "context_window": existing.context_window,
        "max_tokens": existing.max_tokens,
        "reasoning": existing.reasoning,
        "input_modalities": existing.input_modalities,
        "sampling_params": dict(existing.sampling_params),
        "thinking_level_map": existing.thinking_level_map,
        "headers": dict(existing.headers),
        "compat": existing.compat,
    }
    for key, value in patch.items():
        field = _snake(str(key))
        if field == "thinking_level_map":
            fields[field] = _parse_thinking_map(value)
        elif field == "input_modalities":
            fields[field] = tuple(str(item) for item in value or ())
        elif field == "sampling_params":
            fields[field] = {**fields[field], **(value or {})}
        elif field == "headers":
            fields[field] = {**fields[field], **(value or {})}
        elif field == "compat":
            fields[field] = _parse_compat(value) if value is not None else None
        elif field in fields:
            fields[field] = value
    return ModelSpec(id=existing.id, **fields)


def _parse_provider(provider_id: str, data: Any, *, env: dict[str, str]) -> ProviderSpec:
    if not isinstance(data, dict):
        raise ModelsFileError(
            f"provider {provider_id!r} must be an object, got {type(data).__name__}"
        )
    api = data.get("api")
    if api is not None:
        api = str(api)  # None (absent) = override-only entry; registry inherits
    base_url = resolve_str(data.get("baseUrl"), env=env)
    api_key = resolve_str(data.get("apiKey"), env=env)
    headers_data = data.get("headers")
    if headers_data is not None and not isinstance(headers_data, dict):
        raise ModelsFileError(f"provider {provider_id!r} headers must be an object")
    compat_data = data.get("compat")
    models: dict[str, ModelSpec] = {}
    for entry in data.get("models") or []:
        model_id, spec = _parse_model(entry, env=env)
        models[model_id] = spec
    overrides = data.get("modelOverrides") or {}
    if not isinstance(overrides, dict):
        raise ModelsFileError(f"provider {provider_id!r} modelOverrides must be an object")
    for model_id, patch in overrides.items():
        existing = models.get(str(model_id)) or ModelSpec(id=str(model_id))
        models[str(model_id)] = _apply_model_override(existing, patch)
    return ProviderSpec(
        id=provider_id,
        api=api,
        base_url=base_url,
        api_key=api_key,
        headers=resolve_headers(headers_data, env=env) if headers_data else {},
        compat=_parse_compat(compat_data) if compat_data is not None else None,
        models=models,
    )


def _parse_role_entry(role: str, data: Any) -> RoleSpec:
    """Parse one ``roles.<name>`` entry; fail closed on schema violations."""

    if not isinstance(data, dict):
        raise ModelsFileError(
            f"roles.{role} must be an object, got {type(data).__name__}"
        )
    model = data.get("model")
    if model is not None:
        if not isinstance(model, str):
            raise ModelsFileError(f"roles.{role}.model must be a string")
        model = model.strip() or None  # empty string counts as not written
    sampling = data.get("samplingParams")
    if sampling is None:
        sampling = {}
    elif not isinstance(sampling, dict):
        raise ModelsFileError(f"roles.{role}.samplingParams must be an object")
    thinking = data.get("thinking")
    if thinking is not None and (
        not isinstance(thinking, str) or thinking not in THINKING_LEVELS
    ):
        raise ModelsFileError(
            f"roles.{role}.thinking must be one of {THINKING_LEVELS}, got {thinking!r}"
        )
    return RoleSpec(model=model, sampling_params=dict(sampling), thinking=thinking)


def _parse_roles(data: dict[str, Any]) -> dict[str, RoleSpec]:
    """Parse the top-level ``roles`` section (empty dict when absent)."""

    roles_data = data.get("roles")
    if roles_data is None:
        return {}
    if not isinstance(roles_data, dict):
        raise ModelsFileError("'roles' must be an object")
    parsed: dict[str, RoleSpec] = {}
    for name, entry in roles_data.items():
        role = str(name)
        if role not in ROLES:
            raise ModelsFileError(
                f"unknown role {role!r} in 'roles' (expected one of {ROLES})"
            )
        parsed[role] = _parse_role_entry(role, entry)
    return parsed


def parse_models_document(
    data: Any, *, env: dict[str, str] | None = None
) -> tuple[dict[str, ProviderSpec], dict[str, RoleSpec]]:
    """Parse a raw models.json document into ``(providers, roles)``.

    ``providers`` may be absent (a roles-only file is valid); the ``roles``
    section is always validated, even through the providers-only wrapper
    :func:`parse_models_json`.
    """

    if not isinstance(data, dict):
        raise ModelsFileError("models.json root must be an object")
    parsed: dict[str, ProviderSpec] = {}
    providers_data = data.get("providers")
    if providers_data is not None:
        if not isinstance(providers_data, dict):
            raise ModelsFileError("'providers' must be an object")
        env_map = env  # None = resolve against os.environ (values.py default)
        for provider_id, entry in providers_data.items():
            try:
                parsed[str(provider_id)] = _parse_provider(str(provider_id), entry, env=env_map)
            except ModelsFileError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalize to ModelsFileError
                raise ModelsFileError(f"provider {provider_id!r}: {exc}") from exc
    return parsed, _parse_roles(data)


def parse_models_json(data: Any, *, env: dict[str, str] | None = None) -> dict[str, ProviderSpec]:
    """Parse a raw models.json document into ``{provider_id: ProviderSpec}``.

    The ``roles`` section (if any) is validated as a side effect; use
    :func:`parse_models_document` to read it.
    """

    return parse_models_document(data, env=env)[0]


def load_raw_document(path: Path) -> tuple[dict[str, ProviderSpec], dict[str, RoleSpec]]:
    """Load and parse one models.json file; failures raise ModelsFileError."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ModelsFileError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelsFileError(f"malformed JSON in {path}: {exc}") from exc
    return parse_models_document(data)


def load_raw_file(path: Path) -> dict[str, ProviderSpec]:
    """Providers view of :func:`load_raw_document` (roles validated, not returned)."""

    return load_raw_document(path)[0]


def build_provider_registry(config: Any = None) -> Any:
    """Assemble the ProviderRegistry: built-in gateway + models.json files.

    The gateway provider is synthesized from V2Config (id ``"gateway"``,
    openai-completions, headers from ``build_default_headers`` so UA/CF Access
    survive) and models.json entries are applied on top via upsert semantics.
    Parsed ``roles`` sections are attached to the returned registry as its
    dynamic ``roles`` attribute (``dict[str, RoleSpec]``; empty dict when no
    file declares roles; later files upsert wholesale per role name —
    :class:`ProviderRegistry` itself stays provider-generic, the attach is
    loader-side).  Read it back through
    :func:`phone_agent.v2.providers.roles.get_role_specs`, which tolerates
    registries built elsewhere (no attribute).

    Fail-open contract: any error (missing env var, malformed JSON, unknown
    role/thinking value, unsupported api, filesystem failure) logs a warning
    and returns ``None`` so callers degrade to the legacy single-gateway
    behavior instead of crashing a run.
    """

    from phone_agent.v2.providers.registry import (
        DEFAULT_PROVIDER_ID,
        ProviderRegistry,
    )

    try:
        # Imported inside the try: the model module may be a partial fake in
        # test/concurrent-worktree environments; any failure must land the
        # fail-open return below.
        from phone_agent.v2.model import build_default_headers

        model_name = str(getattr(config, "model_name", "") or "")
        registry = ProviderRegistry(default_provider=DEFAULT_PROVIDER_ID)
        gateway = ProviderSpec(
            id=DEFAULT_PROVIDER_ID,
            api="openai-completions",
            base_url=getattr(config, "base_url", None),
            api_key=getattr(config, "api_key", None),
            headers=build_default_headers(config) if config is not None else {},
            models={model_name: ModelSpec(id=model_name, name=model_name)} if model_name else {},
        )
        registry.register(gateway)
        roles: dict[str, RoleSpec] = {}
        for path in candidate_paths(config):
            if not path.exists():
                continue
            providers, file_roles = load_raw_document(path)
            for provider_id, spec in providers.items():
                registry.override(spec)
            roles.update(file_roles)
        registry.roles = roles  # loader-side attach (see docstring)
        return registry
    except Exception as exc:  # noqa: BLE001 - provider layer must never crash a run
        logger.warning("provider registry unavailable, using legacy gateway path: %s", exc)
        return None
