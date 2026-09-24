"""Declared configuration keys for capability/plugin settings (P0 #8 / #18).

A capability declares the keys it owns at assembly time::

    threshold = ctx.register_setting(
        "systemone_confidence",
        env_var="PHONE_AGENT_SYSTEMONE_CONFIDENCE",
        default=0.7,
        description="shadow 复核的置信度门槛",
    )

The declared key resolves through the same precedence chain as the built-in
``V2Config`` fields, with the manifest's per-plugin table as the tier that
replaces the declared default:

    1. harness CLI override  (``CapabilityAssemblyContext`` service
       ``setting_overrides``: declared key -> value)
    2. shell environment / project ``.env`` (``load_project_env`` copies the
       latter into the environment without overriding an exported value, so
       shell env > ``.env`` holds for every declared key)
    3. manifest ``[plugin.config]`` value of the declaring plugin
    4. the declared ``default``

Why the manifest sits below the environment: ``.taskwizard.toml`` is committed
project configuration while the environment is the machine-local operator
surface, and P0 #8's chain (CLI > env > .env > default) is the chain declared
keys join.  ``[plugin.config]`` therefore overrides the *declared default* for
that plugin instance, not the operator's exported value.

Validation is fail-visible and happens before any value is returned:
``env_var`` must carry the ``PHONE_AGENT_`` prefix (``.env`` only loads that
prefix, so anything else would silently never pick a value up), the key follows
the same lowercase-identifier grammar as a capability id, the description must
be non-empty (it is the in-code documentation hook) and the default must be a
scalar (``str`` / ``bool`` / ``int`` / ``float`` / ``None``).  A second
capability declaring the same key raises.  The CLI tier is harness-supplied and
only answers keys something declares: an override for an undeclared key is never
consulted (``overrides`` exposes the map for audit).

See ``pages/configuration.md`` §配置层级与装配 for the operator-facing contract;
in-repo declared keys must be listed there (``tests/docs/test_config_keys_sync``
scans the sources for ``PHONE_AGENT_*`` literals).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import os
import re
from typing import Any

ENV_VAR_PREFIX = "PHONE_AGENT_"
"""Every declared ``env_var`` must start with this prefix (P0 #8 / ``.env``)."""

_SCALAR_TYPES = (str, bool, int, float)
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class SettingDeclaration:
    """One declared key: where it resolves from, and what it resolved to."""

    key: str
    env_var: str
    default: Any
    description: str
    owner: str
    value: Any


class SettingRegistry(Mapping[str, SettingDeclaration]):
    """Assembly-time declarations of capability/plugin configuration keys.

    Mapping of declared key -> :class:`SettingDeclaration`; only declared keys
    appear.  :meth:`declare` is the single writer (called by
    :meth:`phone_agent.v2.capabilities.CapabilityAssemblyContext.register_setting`)
    and the harness seeds the CLI override tier through the constructor.  The
    registry is per-assembly: release withdraws the declaring capability's keys.
    """

    def __init__(self, overrides: Mapping[str, Any] | None = None) -> None:
        self._overrides: dict[str, Any] = {
            str(key): value for key, value in (overrides or {}).items()
        }
        self._declared: dict[str, SettingDeclaration] = {}

    @property
    def overrides(self) -> dict[str, Any]:
        """The CLI-tier override mapping this registry resolves first."""

        return dict(self._overrides)

    @staticmethod
    def _validate_key(key: Any) -> str:
        clean = str(key).strip()
        if not _KEY_RE.fullmatch(clean):
            raise ValueError(
                f"invalid setting key: {key!r} (expected a lowercase identifier "
                "such as 'systemone_confidence')"
            )
        return clean

    @staticmethod
    def _validate_env_var(env_var: Any) -> str:
        clean = str(env_var).strip()
        if not clean.startswith(ENV_VAR_PREFIX):
            raise ValueError(
                f"invalid setting env_var: {env_var!r} (must start with "
                f"{ENV_VAR_PREFIX!r}; only that prefix is loaded from .env)"
            )
        if not re.fullmatch(r"[A-Z0-9_]+", clean):
            raise ValueError(
                f"invalid setting env_var: {env_var!r} (expected upper-case "
                "letters, digits and underscores)"
            )
        return clean

    @staticmethod
    def _validate_description(description: Any) -> str:
        clean = str(description or "").strip()
        if not clean:
            raise ValueError("a declared setting requires a non-empty description")
        return clean

    @staticmethod
    def _validate_default(default: Any) -> None:
        if default is None or isinstance(default, _SCALAR_TYPES):
            return
        raise ValueError(
            "a declared setting default must be a scalar (str/bool/int/float) "
            f"or None, got {type(default).__name__}"
        )

    @staticmethod
    def _coerce(value: Any, default: Any, *, source: str) -> Any:
        """Typed read of a raw (env / manifest / override) value.

        The declared default decides the type; a value that cannot be read as
        that type raises instead of silently degrading to the default (a typo'd
        operator value must be visible, unlike the built-in ``*_env_choice``
        switches which deliberately degrade).
        """

        if default is None:
            return value
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in _TRUE_TOKENS:
                return True
            if text in _FALSE_TOKENS:
                return False
            raise ValueError(f"{source}: expected a boolean, got {value!r}")
        if isinstance(default, int):
            if isinstance(value, bool):
                raise ValueError(f"{source}: expected an integer, got {value!r}")
            try:
                return int(str(value).strip())
            except ValueError as exc:
                raise ValueError(f"{source}: expected an integer, got {value!r}") from exc
        if isinstance(default, float):
            try:
                return float(str(value).strip())
            except ValueError as exc:
                raise ValueError(f"{source}: expected a float, got {value!r}") from exc
        if isinstance(value, (dict, list, tuple)):
            raise ValueError(
                f"{source}: expected a string, got {type(value).__name__}"
            )
        return str(value)

    def resolve(
        self,
        key: str,
        *,
        env_var: str,
        default: Any,
        plugin_config: Mapping[str, Any] | None = None,
    ) -> Any:
        """Resolve one key through the declared precedence chain (no record)."""

        if key in self._overrides:
            return self._coerce(
                self._overrides[key], default, source=f"CLI override for {key!r}"
            )
        raw = os.getenv(env_var)
        if raw is not None and str(raw).strip():
            return self._coerce(raw, default, source=env_var)
        if plugin_config and key in plugin_config:
            return self._coerce(
                plugin_config[key],
                default,
                source=f"manifest [plugin.config] {key!r}",
            )
        return default

    def declare(
        self,
        key: Any,
        *,
        env_var: Any,
        default: Any,
        description: Any,
        owner: str,
        plugin_config: Mapping[str, Any] | None = None,
    ) -> Any:
        """Validate, record and resolve one declared key; returns its value."""

        clean_key = self._validate_key(key)
        clean_env = self._validate_env_var(env_var)
        clean_description = self._validate_description(description)
        self._validate_default(default)
        existing = self._declared.get(clean_key)
        if existing is not None and existing.owner != owner:
            raise ValueError(
                f"setting {clean_key!r} is already declared by capability "
                f"{existing.owner!r}"
            )
        value = self.resolve(
            clean_key, env_var=clean_env, default=default, plugin_config=plugin_config
        )
        self._declared[clean_key] = SettingDeclaration(
            key=clean_key,
            env_var=clean_env,
            default=default,
            description=clean_description,
            owner=owner,
            value=value,
        )
        return value

    def withdraw(self, owner: str) -> tuple[str, ...]:
        """Drop every key declared by ``owner``; returns the removed keys."""

        removed = tuple(
            key for key, item in self._declared.items() if item.owner == owner
        )
        for key in removed:
            self._declared.pop(key, None)
        return removed

    def __getitem__(self, key: str) -> SettingDeclaration:
        return self._declared[str(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self._declared))

    def __len__(self) -> int:
        return len(self._declared)


__all__ = ["ENV_VAR_PREFIX", "SettingDeclaration", "SettingRegistry"]
