"""Harness-domain tests: the thin agent loop and the contracts around it.

One model call per step, an event-bus chain of policy listeners, a fixed
execution contract for tool arguments and receipts, and the per-run bookkeeping
(usage ledger, terminal branches). These files assemble a real
``ThinPhoneAgent`` against fakes or pin the contract tables directly; no real
device, gateway, MLX or network is involved.
"""
