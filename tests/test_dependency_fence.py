import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = {"langgraph", "langchain", "langchain_core", "langchain_openai", "langfuse", "braintrust", "litellm"}


def test_no_forbidden_imports():
    bad = []
    for py in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".")[0]]
            else:
                continue
            bad += [(str(py), r) for r in roots if r in FORBIDDEN]
    assert not bad, bad


def test_no_forbidden_dependencies():
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    reqs = list(proj["dependencies"])
    for extra in proj.get("optional-dependencies", {}).values():
        reqs += extra
    names = {re.split(r"[\s\[<>=!~;@]", r, maxsplit=1)[0].lower().replace("-", "_") for r in reqs}
    assert names, "no dependencies parsed"
    assert not names & FORBIDDEN, names & FORBIDDEN
