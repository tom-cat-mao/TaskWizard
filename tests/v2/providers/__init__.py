"""Provider-domain tests: model config, capability mounting and plugins.

``models.json`` parsing and precedence, per-role sampling/thinking, transport
builders, the capability registry (mount/release/reconcile) and the plugin seams
all live here, next to the config-precedence tests they depend on. Every probe is
in-process: fake transports and registries, never a live endpoint.
"""
