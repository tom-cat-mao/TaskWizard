"""Locate-domain tests: description→mark resolution and the visual-locate chain.

Everything here is offline: the mark dumps are synthetic and the grounding
provider is a fake, so the tests pin the *policy* the chain promises — exact /
substring / normalized tiers, fail-closed ambiguity, the fresh-frame geometry a
locate miss must not commit, and the honest failure surface when the provider
itself is unavailable.

The shared doubles live in ``tests/v2/doubles/``.
"""
