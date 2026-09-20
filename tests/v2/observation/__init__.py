"""Observation-domain tests: the atomic observe window, marks, screenshots.

Everything here drives the **real** ``PhoneSession`` against a scripted
:class:`~tests.v2.doubles.device.FakeDeviceFactory` (``observing=True``) — no ADB,
no MLX, no network. That is the point of the domain: the session's frame
semantics (epoch, stability, retry, digest) are what these files pin, so they
share one device double instead of carrying four evolved copies.

The shared doubles live in ``tests/v2/doubles/``; a file here only keeps what is
specific to the surface it pins (a windowed dump fixture, a bespoke per-call
device script).
"""
