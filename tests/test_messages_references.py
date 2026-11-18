"""Guard test: every ``messages.<name>`` reference in the code base must exist."""
import ast
from pathlib import Path

from utils import messages

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "tests", "__pycache__"}


def _referenced_names() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in ROOT.rglob("*.py"):
        if SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "messages":
                found.setdefault(node.attr, []).append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def test_every_referenced_message_function_exists():
    missing = {name: where for name, where in _referenced_names().items() if not hasattr(messages, name)}
    assert not missing, f"messages.* functions referenced but not defined: {missing}"


def test_history_empty_message_is_a_formatted_string():
    text = messages.history_empty()
    assert isinstance(text, str) and text.startswith("**")
