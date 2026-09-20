"""TaskDoc-domain tests: the board, its flow line, and task parsing.

The TaskDoc contract (seeded ``goal_base``, open-route items, render shape), the
transcript-derived flow line, the per-step context request, and the
description→mark resolver all live here. All fakes — no device, gateway or MLX.

The shared doubles live in ``tests/v2/doubles/``.
"""
