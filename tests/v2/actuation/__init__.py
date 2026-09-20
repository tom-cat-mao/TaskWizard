"""Actuation-domain tests: the tools that touch the device.

The tool surface (``v2/tools/``) is exercised against fake devices and fake
sessions — taps, typing, swipes, app launches, and the run-bound deliverable —
so the tests pin the receipt text, the fail-closed error paths and the marks
binding rather than any real ADB behaviour.

The shared doubles live in ``tests/v2/doubles/``.
"""
