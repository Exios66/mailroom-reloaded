"""Intake adapters: alternate ways documents enter the mailroom inbox.

The filesystem watcher (:mod:`mailroom_reloaded.watcher`) is the canonical
entry point. :mod:`mailroom_reloaded.intake.gmail` adds a Gmail attachment
intake that lands supported attachments in ``inbox/`` so the existing pipeline
can drain them unchanged.
"""
