"""Fixtures for the in-repo first-party plugin suite (``plugins/systemone``).

The doubles live in :mod:`tests.plugins.systemone_harness`; this module only
turns them into fixtures.  Nothing here touches the network beyond a loopback
socket, a real device, or a model gateway.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.plugins.systemone_harness import (
    FakeSystemOneServer,
    ScriptedPhoneSession,
    closed_port,
    load_plugin_module,
    mount_systemone,
    sibling,
)


@pytest.fixture(scope="session")
def plugin_module() -> Any:
    """The plugin entry module, loaded the way ``phone_agent.v2.plugins`` loads it."""

    return load_plugin_module()


@pytest.fixture(scope="session")
def siblings(plugin_module: Any) -> dict[str, Any]:
    """Every sibling module, loaded through the plugin's own loader."""

    return {
        name: plugin_module.load_sibling(name)
        for name in ("client", "provider", "reviewer", "stability", "governor")
    }


@pytest.fixture(autouse=True)
def ensure_systemone_api_family(plugin_module: Any, siblings: dict[str, Any]) -> None:
    """Make ``api="systemone"`` declarable before a test builds a spec.

    ``register_api_builder`` is what appends the family to the declarative
    ``SUPPORTED_APIS`` tuple, and the plugin registers it during ``apply``.
    A test that constructs a ``ProviderSpec(api="systemone")`` outside a mount
    therefore needs the same step; the registration here is marked as the
    plugin's own, so a later ``apply`` overrides it instead of refusing it.
    The registered builder is only ever used through a real mount.
    """

    from phone_agent.v2.providers import register_api_builder
    from phone_agent.v2.providers.builders import get_api_builder

    provider_module = siblings["provider"]
    client_module = siblings["client"]
    existing = get_api_builder(provider_module.API_FAMILY)
    if existing is not None:
        return
    builder = provider_module.make_api_builder(
        lambda backend, model, url: client_module.SystemOneClient(
            backend=backend, model=model, base_url=url, api_key="k"
        ),
        probe=None,
    )
    builder._taskwizard_systemone = True  # type: ignore[attr-defined]
    register_api_builder(provider_module.API_FAMILY, builder)


@pytest.fixture(autouse=True)
def release_declared_globals() -> Any:
    """Withdraw this plugin's process-global declarations after every test.

    ``usage_role_registry`` and ``redaction_registry`` are process-level by
    design (a run and an offline reader in the same process must see the same
    roles), so a mount that a test never releases would leak into the *next*
    test — including the ``tests/v2`` suites that assert the harness role set
    exactly.  Withdrawing through the public API mirrors what a capability
    release does in production.
    """

    yield
    from phone_agent.v2.redaction import REDACTION_REGISTRY
    from phone_agent.v2.usage_roles import USAGE_ROLE_REGISTRY

    USAGE_ROLE_REGISTRY.withdraw("systemone")
    REDACTION_REGISTRY.withdraw("systemone")


@pytest.fixture
def client_module(siblings: dict[str, Any]) -> Any:
    return siblings["client"]


@pytest.fixture
def provider_module(siblings: dict[str, Any]) -> Any:
    return siblings["provider"]


@pytest.fixture
def reviewer_module(siblings: dict[str, Any]) -> Any:
    return siblings["reviewer"]


@pytest.fixture
def stability_module(siblings: dict[str, Any]) -> Any:
    return siblings["stability"]


@pytest.fixture
def governor_module(siblings: dict[str, Any]) -> Any:
    return siblings["governor"]


@pytest.fixture
def fake_server() -> Any:
    """A loopback server speaking the real System One protocol."""

    server = FakeSystemOneServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def no_listener_port() -> int:
    """A loopback port with no listener (for unreachable-endpoint tests)."""

    return closed_port()


@pytest.fixture
def session() -> ScriptedPhoneSession:
    """A scripted frame session: two identical frames, then a third one."""

    return ScriptedPhoneSession(["h1", "h1", "h1"])


@pytest.fixture
def mount() -> Callable[..., Any]:
    """Factory: assemble the plugin with the given doubles."""

    return mount_systemone


@pytest.fixture
def sibling_loader() -> Callable[[str], Any]:
    return sibling
