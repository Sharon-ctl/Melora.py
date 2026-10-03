import inspect
import re
from typing import get_type_hints

import utils.messages as msg_mod

PATTERN_B = re.compile(r"^\*\*[A-Za-z0-9][^\n*]*\*\*\s+•\s+`[^\n`]+`$")
PATTERN_A = re.compile(r"^[A-Za-z0-9][^\n]*(?:\s+•\s+`[^\n`]+`)?$")
PATTERN_C = re.compile(r"^\*\*[A-Za-z0-9\s/]+:\*\*\s+(?:`[^\n`]+`|<[@#][!&]?\d+>)$")

PROGRESS_FUNCTIONS = {
    "collection_tracks_added",
    "favorites_queued",
    "playlist_queued",
}


def _dummy_arg_for(param: inspect.Parameter, hint: type):
    if param.name == "is_mention":
        return False
    if hint is int:
        return 5
    if hint is float:
        return 10.0
    if hint is bool:
        return True
    if hint is str:
        return "Test Track"
    if hint is list or getattr(hint, "__origin__", None) is list:
        return ["item1", "item2"]
    if param.default is not inspect.Parameter.empty:
        return param.default
    return "Dummy"


def test_messages_catalog_format_and_encoding():
    functions = inspect.getmembers(msg_mod, inspect.isfunction)
    assert len(functions) >= 50, f"Expected comprehensive catalog, found {len(functions)}"

    for name, func in functions:
        if name.startswith("_") or name == "escape_subject" or name == "clean":
            continue

        sig = inspect.signature(func)
        hints = get_type_hints(func)
        args = []
        for param_name, param in sig.parameters.items():
            hint = hints.get(param_name, str)
            args.append(_dummy_arg_for(param, hint))

        result = func(*args)
        assert isinstance(result, str), f"{name} did not return str"
        assert len(result) > 0, f"{name} returned empty string"

        # Rule: No exclamation marks
        assert "!" not in result, f"{name} contains exclamation mark: {result!r}"

        # Rule: Only ASCII plus bullet (U+2022)
        for char in result:
            code = ord(char)
            assert code < 128 or code == 0x2022, f"{name} contains forbidden non-ASCII code {code}: {result!r}"

        # Rule: Mentions are never put in backticks
        assert not re.search(r"`<[@#][!&]?\d+>`", result), f"{name} puts mention in backticks: {result!r}"

        # Rule: Pattern C for card label/value rows
        if name == "server_setting_row":
            assert PATTERN_C.match(result), f"{name} does not match Pattern C: {result!r}"
            continue

        # Rule: One line (at most two lines for progress messages)
        lines = result.splitlines()
        if name in PROGRESS_FUNCTIONS:
            assert len(lines) <= 2, f"{name} exceeds 2 lines: {result!r}"
        else:
            assert len(lines) == 1, f"{name} exceeds 1 line: {result!r}"

        # Rule: Every line must match Pattern A or Pattern B
        for line in lines:
            matches_a = bool(PATTERN_A.match(line))
            matches_b = bool(PATTERN_B.match(line))
            assert matches_a or matches_b, f"{name} line {line!r} does not match Pattern A or B"
