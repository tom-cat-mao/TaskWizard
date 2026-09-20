"""Runner-domain tests: the run record, diagnostics and the CLI entry.

A finished run leaves a directory behind — ``run.json``, the event stream, the
diagnostic evidence file — and the skill/diagnosis tooling reads it back. These
files pin that record (including the persisted verifier verdict), the diagnostic
observation annotations, and the ``main_v2`` maintenance entry points. No real
device or network.
"""
