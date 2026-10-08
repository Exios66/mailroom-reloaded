"""Pipeline package: flow state, guards, report, archivist and ``MailroomFlow``."""

from __future__ import annotations

from mailroom_reloaded.pipeline.archivist import ArchiveResult, archive_document
from mailroom_reloaded.pipeline.flow import MailroomFlow, run_document
from mailroom_reloaded.pipeline.guards import NodeFailed, guarded
from mailroom_reloaded.pipeline.report import compile_report
from mailroom_reloaded.pipeline.state import NODE_ORDER, MailroomState

__all__ = [
    "NODE_ORDER",
    "ArchiveResult",
    "MailroomFlow",
    "MailroomState",
    "NodeFailed",
    "archive_document",
    "compile_report",
    "guarded",
    "run_document",
]
