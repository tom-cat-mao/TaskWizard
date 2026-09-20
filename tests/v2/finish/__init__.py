"""Finish-domain tests: the two-step finish packet and its verifier.

``finish`` is two-step (review packet → ``confirm=true``) and an accepted finish
is terminal; the verifier never reads the actor transcript and fails open
(P0 #12). These files pin the packet contents, the verifier verdicts and the
token-boundary continuation the runner grants once.
"""
