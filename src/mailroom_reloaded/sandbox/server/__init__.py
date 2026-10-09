"""Offline sandbox server: ingress simulator, Correspondent stand-in, pipeline runner.

Nothing in this package opens a non-loopback socket (see :mod:`.guard`). The
Correspondent here is a deterministic *stand-in* behind
:class:`~mailroom_reloaded.sandbox.server.correspondent.CorrespondentAgent`.
"""
