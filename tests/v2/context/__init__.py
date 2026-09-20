"""Context-domain tests: compaction, pruning, prompts and token accounting.

Everything that decides *what the model sees next* lives here — the two-threshold
auto-compact, the boundary-aware fold, image/marks pruning, the prompt contract,
wire serialisation of multimodal tool results, and the budget/first-diff meters.
All fakes: no real gateway, device or MLX.
"""
