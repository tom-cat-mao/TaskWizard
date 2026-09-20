"""Safety-domain tests: the risk gate, the warning flow, and trace hygiene.

These files pin the P0 contracts that must not drift: ``wary`` never executes a
risky call without ``confirm_irreversible``, the reviewer/verifier layers degrade
in a fixed order, and every model/tool event lands in the trace already redacted.
No real device, gateway or MLX is involved.
"""
