"""models.json two-level loader with pi-style merge semantics (S4).

Search order (lowest to highest priority; later files win):

1. user level   ``~/.config/taskwizard/models.json``
2. project level ``.taskwizard.models.json`` (cwd)
3. ``PHONE_AGENT_MODELS_FILE`` (V2Config.models_file) — an explicit extra file

Merge semantics (pi-style): providers merge by id, models upsert by id, and a
provider entry that only carries baseUrl/headers/compat keeps the existing
model catalog intact.  Values (apiKey, baseUrl, header values) support
``$ENV`` / ``${ENV}`` interpolation via :mod:`providers.values`.

Every parse error is a :class:`ModelsFileError`; :func:`build_provider_registry`
converts any failure into ``None`` (log + fall back to the legacy
single-gateway behavior) — the provider layer never crashes a run.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from phone_agent.v2.providers.types import (
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


def user_models_path() -> Path:
    """User-level registry file (computed lazily so tests can monkeypatch HOME)."""

    return Path.home() / ".config" / "taskwizard" / "models.json"


def candidate_paths(config: Any = None) -> list[Path]:
    """Return the ordered models.json candidates (low -> high priority)."""

    paths = [user_models_path(), Path.cwd() / PROJECT_MODELS_NAME]
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


def parse_models_json(data: Any, *, env: dict[str, str] | None = None) -> dict[str, ProviderSpec]:
    """Parse a raw models.json document into ``{provider_id: ProviderSpec}``."""

    if not isinstance(data, dict):
        raise ModelsFileError("models.json root must be an object")
    providers_data = data.get("providers")
    if providers_data is None:
        return {}
    if not isinstance(providers_data, dict):
        raise ModelsFileError("'providers' must be an object")
    env_map = env  # None = resolve against os.environ (values.py default)
    parsed: dict[str, ProviderSpec] = {}
    for provider_id, entry in providers_data.items():
        try:
            parsed[str(provider_id)] = _parse_provider(str(provider_id), entry, env=env_map)
        except ModelsFileError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize to ModelsFileError
            raise ModelsFileError(f"provider {provider_id!r}: {exc}") from exc
    return parsed


def load_raw_file(path: Path) -> dict[str, ProviderSpec]:
    """Load and parse one models.json file; failures raise ModelsFileError."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ModelsFileError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelsFileError(f"malformed JSON in {path}: {exc}") from exc
    return parse_models_json(data)


def build_provider_registry(config: Any = None) -> Any:
    """Assemble the ProviderRegistry: built-in gateway + models.json files.

    The gateway provider is synthesized from V2Config (id ``"gateway"``,
    openai-completions, headers from ``build_default_headers`` so UA/CF Access
    survive) and models.json entries are applied on top via upsert semantics.

    Fail-open contract: any error (missing env var, malformed JSON, unsupported
    api, filesystem failure) logs a warning and returns ``None`` so callers
    degrade to the legacy single-gateway behavior instead of crashing a run.
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
        for path in candidate_paths(config):
            if not path.exists():
                continue
            for provider_id, spec in load_raw_file(path).items():
                registry.override(spec)
        return registry
    except Exception as exc:  # noqa: BLE001 - provider layer must never crash a run
        logger.warning("provider registry unavailable, using legacy gateway path: %s", exc)
        return None
