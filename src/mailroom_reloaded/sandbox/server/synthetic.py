"""Deterministic placeholder documents for dataset-draw attachments.

The content pack names attachments as ``{class, stratum, group, ref}`` draws from a
dataset that is not part of an offline pack. The sandbox materialises each draw as a tiny
valid PDF whose text is a pure function of the draw (no clock, no randomness), so the
same draw always yields the same bytes, the same sha256 and the same ``doc_id``. They are
clearly marked synthetic and carry a matter reference derived from the draw's ``group``
(``cr_0577`` -> ``CR-2026-0577``) so reference matching between related documents works.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

__all__ = ["materialise_draw", "matter_ref_for", "synthetic_pdf"]


def matter_ref_for(spec: dict) -> str:
    """Derive a synthetic matter reference from a dataset draw.

    Groups such as ``cr_0577`` become ``CR-2026-0577``. Otherwise derive a
    four-digit suffix from class, stratum, and ref; uniqueness is not guaranteed.
    """
    group = str(spec.get("group") or "")
    m = re.fullmatch(r"([a-z]{1,4})_(\d{2,6})", group)
    if m:
        return f"{m.group(1).upper()}-2026-{m.group(2)}"
    key = f"{spec.get('class')}/{spec.get('stratum')}/{spec.get('ref') or ''}"
    return "SYN-2026-" + str(
        int(hashlib.sha256(key.encode()).hexdigest()[:6], 16) % 10000
    ).zfill(4)


def _esc(s: str) -> str:
    """Escape backslashes and parentheses for a PDF literal string."""
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def synthetic_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF (Helvetica text), byte-for-byte deterministic."""
    ops = ["BT", "/F1 11 Tf", "14 TL", "72 740 Td"]
    ops += [f"({_esc(ln)}) Tj T*" for ln in lines]
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def materialise_draw(spec: dict, directory: Path) -> tuple[Path, str]:
    """Write the placeholder for one dataset draw; returns (path, delivered filename)."""
    ref = spec.get("ref")
    cls, stratum = spec.get("class"), spec.get("stratum")
    name = spec.get("as") or f"{ref or f'{cls}_{stratum}'}.pdf"
    lines = [
        "SYNTHETIC PLACEHOLDER DOCUMENT (sandbox)",
        f"Document class: {cls}",
        f"Subclass: {stratum}",
        f"Matter reference: {matter_ref_for(spec)}",
        f"Draw label: {ref or '-'}",
        "Generated deterministically by the offline sandbox. Not real data.",
    ]
    data = synthetic_pdf(lines)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(data).hexdigest()[:16] + "_" + Path(name).name)
    path.write_bytes(data)
    return path, name
