"""Automated scan script for Phase 8 final review."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

WORKSPACE = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "logs", "__pycache__"}


def get_files_to_check() -> list[Path]:
    files: list[Path] = []
    for p in WORKSPACE.rglob("*"):
        if p.is_file():
            if any(part in EXCLUDE_DIRS for part in p.parts):
                continue
            if p.suffix in (".py", ".md", ".json", ".bat") and p.name != "verify_phase8.py":
                files.append(p)
    return files


def scan_non_ascii(files: list[Path]) -> list[str]:
    hits: list[str] = []
    for file in files:
        # golden_spotify_matches.json tests unicode matching per Phase 3 prompt
        if file.name == "golden_spotify_matches.json":
            continue
        try:
            content = file.read_text(encoding="utf-8")
        except Exception:
            continue
        for line_no, line in enumerate(content.splitlines(), start=1):
            for ch in line:
                if ord(ch) > 127:
                    # The bullet character U+2022 is allowed in bot messages and source
                    if ord(ch) == 0x2022:
                        continue
                    hits.append(f"{file.relative_to(WORKSPACE)}:{line_no} Non-ASCII char {ch!r} (ord {ord(ch)})")
                    break
    return hits


def scan_separator(files: list[Path]) -> list[str]:
    hits: list[str] = []
    for file in files:
        if file.suffix != ".py":
            continue
        content = file.read_text(encoding="utf-8")
        for line_no, line in enumerate(content.splitlines(), start=1):
            if "Separator" in line:
                hits.append(f"{file.relative_to(WORKSPACE)}:{line_no}: Found 'Separator' in code")
    return hits


def scan_button_styles(files: list[Path]) -> list[str]:
    hits: list[str] = []
    # Match any ButtonStyle that is NOT secondary
    pattern = re.compile(r"ButtonStyle\.(primary|success|danger|link|blurple|grey|gray|green|red)", re.IGNORECASE)
    for file in files:
        if file.suffix != ".py":
            continue
        content = file.read_text(encoding="utf-8")
        for line_no, line in enumerate(content.splitlines(), start=1):
            m = pattern.search(line)
            if m:
                # Stop button on nowplaying card is allowed to be ButtonStyle.danger
                if m.group(0).lower() == "buttonstyle.danger":
                    if file.name == "components_v2.py" and "stop" in line.lower():
                        continue
                    if file.name.startswith("test_") and ("danger" in line.lower() or "stop" in line.lower()):
                        continue
                hits.append(f"{file.relative_to(WORKSPACE)}:{line_no}: Non-secondary ButtonStyle: {m.group(0)}")
    return hits


def scan_forbidden_intents(files: list[Path]) -> list[str]:
    hits: list[str] = []
    forbidden = ["message_content", "members", "presences", "presence"]
    for file in files:
        if file.suffix != ".py":
            continue
        content = file.read_text(encoding="utf-8")
        for line_no, line in enumerate(content.splitlines(), start=1):
            for f in forbidden:
                # check for intents.xxx = True or Intents(xxx=True)
                if re.search(rf"\b{f}\s*=\s*True\b", line):
                    hits.append(f"{file.relative_to(WORKSPACE)}:{line_no}: Forbidden intent enabled: {f}")
    return hits


def scan_todos_and_empty_excepts(files: list[Path]) -> list[str]:
    hits: list[str] = []
    for file in files:
        if file.suffix != ".py":
            continue
        content = file.read_text(encoding="utf-8")
        for line_no, line in enumerate(content.splitlines(), start=1):
            if re.search(r"\b(TODO|FIXME|placeholder)\b", line, re.IGNORECASE):
                hits.append(f"{file.relative_to(WORKSPACE)}:{line_no}: TODO/placeholder found")

        # Check AST for empty except
        try:
            tree = ast.parse(content, filename=str(file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if not node.body:
                    hits.append(f"{file.relative_to(WORKSPACE)}:{node.lineno}: Empty except block")
                elif len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    hits.append(f"{file.relative_to(WORKSPACE)}:{node.lineno}: Except block with only pass")
    return hits


def main() -> int:
    files = get_files_to_check()
    all_hits: dict[str, list[str]] = {
        "Non-ASCII / Emoji": scan_non_ascii(files),
        "Separator in code": scan_separator(files),
        "Non-secondary ButtonStyle": scan_button_styles(files),
        "Forbidden Intents": scan_forbidden_intents(files),
        "TODO / Empty except": scan_todos_and_empty_excepts(files),
    }

    total_failures = 0
    for category, hits in all_hits.items():
        if hits:
            print(f"FAILED: {category} ({len(hits)} issues):")
            for h in hits:
                print(f"  - {h}")
            total_failures += len(hits)
        else:
            print(f"PASSED: {category}")

    if total_failures == 0:
        print("\nALL PHASE 8 AUDIT SCANS PASSED PERFECTLY!")
        return 0
    else:
        print(f"\nTOTAL FAILURES: {total_failures}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
