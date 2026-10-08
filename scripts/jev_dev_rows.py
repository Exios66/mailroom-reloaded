"""Deterministic synthetic Jev calibration rows (split=train) for the dev stack."""

import json
import sys

# (confidence, n_rows, n_correct): well separated so the fit lands between the
# mock Jev's 0.55 (escalate), 0.80 (verify) and 0.95 (proceed) confidences.
BUCKETS = [
    (0.95, 20, 20),
    (0.92, 10, 10),
    (0.80, 12, 9),
    (0.60, 8, 2),
    (0.50, 10, 1),
]


def rows():
    for conf, n, correct in BUCKETS:
        for i in range(n):
            yield {"split": "train", "confidence": conf, "correct": 1 if i < correct else 0}


if __name__ == "__main__":
    for row in rows():
        sys.stdout.write(json.dumps(row) + "\n")
