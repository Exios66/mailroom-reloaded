"""Read dev helper settings as dotenv data, without shell or $ interpolation.

NUL-delimited name/value pairs are consumed by Bash `read -r`, never eval.
Only the helper's settings are emitted; Compose reads the full file itself.
"""

import os
import re
import sys
from pathlib import Path

KEYS = (
    "MAILROOM_API_URL", "PHOENIX_URL", "PROMETHEUS_URL", "GRAFANA_URL",
    "SMOKE_TIMEOUT", "MAILROOM_API_TOKEN",
)
# Quoted values may span lines. Comments require whitespace for bare values.
ENTRY = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z_0-9]*)[ \t]*=[ \t]*"
    r"(?:'(?P<single>(?:\\.|[^'\\])*)'|"
    r'"(?P<double>(?:\\.|[^"\\])*)"|(?P<bare>[^\r\n]*))'
    r"[ \t]*(?:\#[^\r\n]*)?(?:\r?\n|$)",
    re.MULTILINE,
)


def read_settings(text):
    values = {}
    for match in ENTRY.finditer(text):
        if match['single'] is not None:
            value = re.sub(r"\\(['\\])", r"\1", match['single'])
        elif match['double'] is not None:
            escapes = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '"': '"'}
            value = re.sub(r'\\([nrt\\"])', lambda m, escapes=escapes: escapes[m[1]], match['double'])
        else:
            value = re.split(r"[ \t]+#", match['bare'], maxsplit=1)[0].rstrip()
        values[match['key']] = value
    return values


if __name__ == "__main__":
    values = read_settings(Path(sys.argv[1]).read_text(encoding="utf-8"))
    for key in KEYS:
        if key in values and key not in os.environ:
            sys.stdout.buffer.write(f"{key}\0{values[key]}\0".encode())
    password = os.environ.get('GRAFANA_ADMIN_PASSWORD', values.get('GRAFANA_ADMIN_PASSWORD', ''))
    label = '<configured>' if password else 'admin (default)'
    sys.stdout.buffer.write(f"GRAFANA_PASSWORD_LABEL\0{label}\0".encode())
