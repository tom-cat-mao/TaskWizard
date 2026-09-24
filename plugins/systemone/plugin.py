"""First-party ``systemone`` plugin: the declaration plane's full dogfood.

System One is a decision model: typed questions in, calibrated typed answers
out, no free text and no images.  This plugin wires it into four seams of the
harness, using the plugin-plane declarations exactly as documented — nothing
here reaches into a private agent factory:

===============  ==================================================================
(a) provider      api family ``systemone`` + providers ``jev`` (cloud) and
                  ``kev`` (local): ``PHONE_AGENT_MODEL=kev:kev-4b@qwen3`` works
                  anywhere a ``provider:model`` reference is resolved
(b) reviewer      ``tool/execute`` prepend listener: ``shadow`` (default) traces
                  a verdict, ``on`` may add a veto when the built-in safety
                  classifier would have allowed the call
(c) stability     ``wait_for_stable`` tool: polls ``session.observe()`` and
                  returns when the committed frame's ``screen_hash`` repeats
                  (declared ``readonly``)
(d) governor      ``model/pre_request`` advisory on a repeated step signature;
                  shadow only — it never touches the prompt (see ``governor.py``)
===============  ==================================================================

Declarations used (P0 #18, every one of them fail-visible):
``register_setting`` for the twelve configuration keys below,
``register_usage_role("systemone", unit="tokens")`` — the API always reports
``usage``, so the account is per token (P0 #13's unit),
``register_redaction`` for the cloud API key, ``register_tool(..., risk="readonly")``
for the stability tool, ``register_service``/``provides`` not at all, and
``register_api_builder``/``register_provider`` for the transport and catalogs.
The plugin declares ``deps=("providers",)`` so the agent mounts it in the
*provider bootstrap* pass, which is what makes its provider usable as the actor
model; `after=("providers",)` states the ordering in its own right.

Unconfigured is a state, not a failure: with no ``PHONE_AGENT_SYSTEMONE_BACKEND``
the model-backed seams stay unmounted (one stderr notice) while the
``wait_for_stable`` tool and the deterministic governor still mount.  An
**explicit** reference is held to P0 #8: ``PHONE_AGENT_SYSTEMONE_REVIEW=on``
without a backend, an unknown backend, a missing cloud key, an unreachable local
server, an out-of-band timeout or an unsupported governor mode all raise at
assembly/build time instead of silently degrading to something else.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from phone_agent.v2.capabilities import CapabilitySpec
from phone_agent.v2.events import MODEL_PRE_REQUEST, TOOL_EXECUTE
from phone_agent.v2.providers import register_api_builder, register_provider
from phone_agent.v2.providers.builders import get_api_builder, unregister_api_builder

#: The harness API this plugin was written against (provisional, PLUGIN_API_VERSION).
REQUIRES_API = ">=1"

_DIR = Path(__file__).resolve().parent
#: Stable ``sys.modules`` names for the sibling modules.  Stable (not derived
#: from ``__name__``) so a second load of this file — ``plugin list`` loads every
#: entry twice — reuses the same module objects, and so the harness's
#: path loader (which execs this file without registering it) still yields one
#: module identity per sibling.
_SIBLING_NAMES = {
    "client": "_taskwizard_systemone_client",
    "provider": "_taskwizard_systemone_provider",
    "reviewer": "_taskwizard_systemone_reviewer",
    "stability": "_taskwizard_systemone_stability",
    "governor": "_taskwizard_systemone_governor",
}

# Declared configuration keys (key -> (env_var, default, description)).  Keys are
# resolved through the harness chain (CLI override > shell env/.env > manifest
# [plugin.config] > these defaults) and mirrored onto V2Config.plugin_settings.
SETTINGS: tuple[tuple[str, str, Any, str], ...] = (
    (
        "systemone_backend",
        "PHONE_AGENT_SYSTEMONE_BACKEND",
        "",
        "System One 后端：jev（云端，需 TYPESAFE_API_KEY）或 kev（本机 server）；"
        "留空则只挂 wait_for_stable 与确定性调速器",
    ),
    (
        "systemone_base_url",
        "PHONE_AGENT_SYSTEMONE_BASE_URL",
        "",
        "覆盖所选后端的 base_url（kev 默认 http://127.0.0.1:8787）",
    ),
    (
        "systemone_model",
        "PHONE_AGENT_SYSTEMONE_MODEL",
        "",
        "复核与调速器提问所用的模型；留空取后端目录的首个模型",
    ),
    (
        "systemone_review",
        "PHONE_AGENT_SYSTEMONE_REVIEW",
        "shadow",
        "安全复核档位：off / shadow（只写 trace）/ on（可追加否决）",
    ),
    (
        "systemone_confidence",
        "PHONE_AGENT_SYSTEMONE_CONFIDENCE",
        0.9,
        "on 档追加否决所需的校准概率门槛 p（0-1）",
    ),
    (
        "systemone_timeout",
        "PHONE_AGENT_SYSTEMONE_TIMEOUT",
        10.0,
        "协议请求超时（秒，协议规定 5-15）",
    ),
    (
        "systemone_retries",
        "PHONE_AGENT_SYSTEMONE_RETRIES",
        2,
        "429/5xx/网络错误的重试上限（4xx 一律不重试）",
    ),
    (
        "systemone_stable_wait",
        "PHONE_AGENT_SYSTEMONE_STABLE_WAIT",
        6.0,
        "wait_for_stable 的默认等待预算（秒，0.5-60）",
    ),
    (
        "systemone_stable_interval",
        "PHONE_AGENT_SYSTEMONE_STABLE_INTERVAL",
        0.5,
        "wait_for_stable 的轮询间隔（秒，最小 0.05）",
    ),
    (
        "systemone_stable_polls",
        "PHONE_AGENT_SYSTEMONE_STABLE_POLLS",
        2,
        "wait_for_stable 判定稳定所需的连续相同 screen_hash 次数（≥2）",
    ),
    (
        "systemone_governor",
        "PHONE_AGENT_SYSTEMONE_GOVERNOR",
        "shadow",
        "卡死调速器档位：off / shadow（本轮只支持 shadow，不注入提示词）",
    ),
    (
        "systemone_governor_steps",
        "PHONE_AGENT_SYSTEMONE_GOVERNOR_STEPS",
        3,
        "判定重复所需的最近相同 intent 签名步数（≥2）",
    ),
)


def load_sibling(name: str) -> ModuleType:
    """Load one sibling module of this plugin by file path.

    The harness loads a path plugin with ``spec_from_file_location`` and does
    **not** register it in ``sys.modules``, so ``from . import x`` is not
    available.  Siblings are therefore loaded here under stable flat names and
    registered in ``sys.modules`` before execution: the registration is what
    gives a module a stable identity across repeated loads (and what lets a
    dataclass defined in one sibling be trusted by another).
    """

    full_name = _SIBLING_NAMES.get(name) or f"_taskwizard_systemone_{name}"
    module = sys.modules.get(full_name)
    if module is not None:
        return module
    path = _DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"systemone plugin cannot load sibling {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(full_name, None)
        raise
    return module


def _trace_recorder(session: Any):
    """One ``record(event, **payload)`` callable shared by the listeners.

    The recorder resolves ``session.resolution_trace_recorder`` **at call
    time**: the harness attaches the trace writer after the provider bootstrap
    pass, i.e. after this plugin's ``apply`` ran.
    """

    def record(event: str, **payload: Any) -> None:
        recorder = getattr(session, "resolution_trace_recorder", None)
        if callable(recorder):
            recorder(event, **payload)

    return record


def _validate_choice(value: Any, legal: tuple[str, ...], *, label: str) -> str:
    clean = str(value or "").strip().lower()
    if clean not in legal:
        raise ValueError(
            f"{label} must be one of {', '.join(legal)}, got {value!r}"
        )
    return clean


def _build_client(
    client_module: ModuleType,
    *,
    backend: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: float,
    max_retries: int,
) -> Any:
    return client_module.SystemOneClient(
        backend=backend,
        model=model,
        base_url=base_url,
        api_key=api_key or None,
        timeout=timeout,
        max_retries=max_retries,
    )


def _probe_for(client_module: ModuleType, timeout: float):
    """The transport's liveness probe, bound to the declared timeout."""

    def probe(base_url: str) -> None:
        client_module.probe_endpoint(base_url, timeout=timeout)

    return probe


def apply(ctx: Any) -> None:
    """Mount the four seams; every reference is explicit and fail-visible."""

    client = load_sibling("client")
    provider_module = load_sibling("provider")
    reviewer_module = load_sibling("reviewer")
    stability_module = load_sibling("stability")
    governor_module = load_sibling("governor")

    # -- declarations ------------------------------------------------------
    settings = {
        key: ctx.register_setting(key, env_var=env_var, default=default, description=desc)
        for key, env_var, default, desc in SETTINGS
    }
    ctx.register_usage_role("systemone", unit="tokens")
    api_key = str(os.getenv(client.CLOUD_KEY_ENV) or "").strip()
    if len(api_key) >= 4:  # shorter is not a secret, and would redact unrelated text
        ctx.register_redaction(api_key)

    backend = str(settings["systemone_backend"] or "").strip().lower()
    if backend and backend not in client.BACKENDS:
        raise ValueError(
            f"PHONE_AGENT_SYSTEMONE_BACKEND must be one of "
            f"{', '.join(client.BACKENDS)} (or empty), got {backend!r}"
        )
    review_mode = _validate_choice(
        settings["systemone_review"], reviewer_module.REVIEW_MODES, label="systemone_review"
    )
    governor_mode = _validate_choice(
        settings["systemone_governor"],
        governor_module.GOVERNOR_MODES,
        label="systemone_governor",
    )
    timeout = float(settings["systemone_timeout"])
    low, high = client.TIMEOUT_BAND
    if not low <= timeout <= high:
        raise ValueError(
            f"PHONE_AGENT_SYSTEMONE_TIMEOUT must be within the protocol band "
            f"{low:g}-{high:g}s, got {timeout:g}"
        )
    max_retries = int(settings["systemone_retries"])
    if max_retries < 0:
        raise ValueError("PHONE_AGENT_SYSTEMONE_RETRIES must not be negative")
    confidence = float(settings["systemone_confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(
            f"PHONE_AGENT_SYSTEMONE_CONFIDENCE must be a probability, got {confidence}"
        )
    stable_wait = float(settings["systemone_stable_wait"])
    if not stability_module.MAX_WAIT_BAND[0] <= stable_wait <= stability_module.MAX_WAIT_BAND[1]:
        raise ValueError(
            "PHONE_AGENT_SYSTEMONE_STABLE_WAIT must be within "
            f"{stability_module.MAX_WAIT_BAND[0]:g}-{stability_module.MAX_WAIT_BAND[1]:g}s, "
            f"got {stable_wait:g}"
        )
    stable_interval = float(settings["systemone_stable_interval"])
    if stable_interval < stability_module.MIN_INTERVAL_S:
        raise ValueError(
            "PHONE_AGENT_SYSTEMONE_STABLE_INTERVAL must be at least "
            f"{stability_module.MIN_INTERVAL_S:g}s, got {stable_interval:g}"
        )
    stable_polls = int(settings["systemone_stable_polls"])
    if stable_polls < 2:
        raise ValueError(
            f"PHONE_AGENT_SYSTEMONE_STABLE_POLLS must be at least 2, got {stable_polls}"
        )
    governor_steps = int(settings["systemone_governor_steps"])
    if governor_steps < 2:
        raise ValueError(
            "PHONE_AGENT_SYSTEMONE_GOVERNOR_STEPS must be at least 2, "
            f"got {governor_steps}"
        )

    session = ctx.service("session")
    config = ctx.service("config")
    record = _trace_recorder(session)

    if ctx.service("event_bus") is None:
        # Assembly-only context (``main_v2.py``'s CLI maintenance path publishes
        # no event bus and no device session): there is no model call and no tool
        # execution on this plane, so the run-time seams stay unmounted — exactly
        # how the built-in capabilities degrade there.  The declarations above
        # are still made, because declarations are what this plane is for.
        return

    # -- (c) wait_for_stable: model-free, mounts whenever the plugin is on ---
    ctx.register_tool(
        stability_module.make_wait_for_stable_tool(
            session,
            max_wait_s=stable_wait,
            interval_s=stable_interval,
            consecutive=stable_polls,
        ),
        risk="readonly",
    )

    # -- backend-dependent seams -------------------------------------------
    base_url = str(settings["systemone_base_url"] or "").strip() or (
        client.DEFAULT_BASE_URLS[backend] if backend else ""
    )
    model_id = str(settings["systemone_model"] or "").strip() or (
        client.DEFAULT_MODELS[backend][0] if backend else ""
    )

    if not backend:
        if review_mode == "on":
            raise ValueError(
                "PHONE_AGENT_SYSTEMONE_REVIEW=on needs a backend: set "
                "PHONE_AGENT_SYSTEMONE_BACKEND=jev|kev (an explicit review "
                "request is never silently downgraded)"
            )
        print(
            "[systemone] no PHONE_AGENT_SYSTEMONE_BACKEND configured: the safety "
            "reviewer is not mounted (wait_for_stable and the deterministic "
            "governor are). Set it to kev (local) or jev (cloud) to enable review.",
            file=sys.stderr,
            flush=True,
        )
    else:
        if backend == client.BACKEND_JEV and not api_key:
            raise ValueError(
                f"systemone backend 'jev' requires {client.CLOUD_KEY_ENV} in the "
                "environment (the plugin never falls back to another endpoint)"
            )
        client_probe = _probe_for(client, timeout)
        client_probe(base_url)

        # api family first: ProviderSpec validates its ``api`` against the
        # declared family, and registration is what adds ``systemone`` to it.
        existing = get_api_builder(provider_module.API_FAMILY)
        if existing is not None and not getattr(existing, "_taskwizard_systemone", False):
            raise ValueError(
                f"api family {provider_module.API_FAMILY!r} is already registered by "
                "another component; refusing to replace it"
            )
        builder = provider_module.make_api_builder(
            lambda backend_id, model, url: _build_client(
                client,
                backend=backend_id,
                model=model,
                base_url=url,
                api_key=api_key if backend_id == client.BACKEND_JEV else "",
                timeout=timeout,
                max_retries=max_retries,
            ),
            probe=client_probe,
            timeout=timeout,
            max_retries=max_retries,
        )
        builder._taskwizard_systemone = True  # type: ignore[attr-defined]
        register_api_builder(
            provider_module.API_FAMILY, builder, override=existing is not None
        )
        ctx.on_dispose(lambda: unregister_api_builder(provider_module.API_FAMILY))
        for spec in provider_module.build_provider_specs(
            catalogs=client.DEFAULT_MODELS,
            # The selected backend's *effective* URL wins over the documented
            # default (``PHONE_AGENT_SYSTEMONE_BASE_URL`` override).
            base_urls={**dict(client.DEFAULT_BASE_URLS), backend: base_url},
            jev_api_key=api_key or None,
        ):
            register_provider(ctx, spec)

        if review_mode != "off":
            reviewer_client = _build_client(
                client,
                backend=backend,
                model=model_id,
                base_url=base_url,
                api_key=api_key if backend == client.BACKEND_JEV else "",
                timeout=timeout,
                max_retries=max_retries,
            )
            ctx.on(
                TOOL_EXECUTE,
                reviewer_module.SystemOneSafetyReviewer(
                    session,
                    config,
                    client=reviewer_client,
                    record=record,
                    tokens_of=client.usage_tokens,
                    mode=review_mode,
                    confidence=confidence,
                    risks=ctx.service("tool_risk_registry"),
                ),
                prepend=True,
            )
            if review_mode == "on":
                # Additive veto: a loud line makes the one behaviour that can
                # block a call visible at assembly, next to the stderr notice
                # the undeclared-risk report already prints.
                print(
                    "[systemone] safety review is in 'on' mode: calls the built-in "
                    f"gate allows are vetoed when p >= {confidence:g}",
                    file=sys.stderr,
                    flush=True,
                )

    if governor_mode == "shadow":
        governor_client = (
            _build_client(
                client,
                backend=backend,
                model=model_id,
                base_url=base_url,
                api_key=api_key if backend == client.BACKEND_JEV else "",
                timeout=timeout,
                max_retries=max_retries,
            )
            if backend
            else None
        )
        ctx.on(
            MODEL_PRE_REQUEST,
            governor_module.StuckGovernor(
                session,
                record=record,
                tokens_of=client.usage_tokens,
                client=governor_client,
                steps=governor_steps,
                mode=governor_mode,
            ),
        )


def release(ctx: Any) -> None:
    """Release is the standard owner-scoped teardown.

    Every listener, the tool and the declared settings/roles/literals are
    withdrawn by the assembly layer; the one process-global resource this plugin
    owns (the transport builder) is disposed explicitly through
    ``ctx.on_dispose`` in :func:`apply`.
    """

    _ = ctx


CAPABILITY = CapabilitySpec(
    cap_id="systemone",
    title="System One decision model (provider / reviewer / stability / governor)",
    mode="on",
    deps=("providers",),
    after=("providers",),
    apply=apply,
    release=release,
)

__all__ = ["CAPABILITY", "REQUIRES_API", "SETTINGS", "apply", "load_sibling", "release"]
