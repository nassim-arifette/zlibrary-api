"""Plain metadata text shared by terminal-facing adapters and views."""
import re
import unicodedata


_STRINGS = re.compile(r"(?:\x1b[\]PX^_]|[\x90\x98\x9d\x9e\x9f]).*?(?:\x07|\x1b\\|\x9c|$)", re.S)
_ESCAPES = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-~]")


def terminal_text(value: str, *, multiline: bool = False) -> str:
    """Remove terminal sequences and Unicode controls, preserving literal markup.

    Callers enforce their own input type and length limits. This normalization
    neither interprets markup nor opens links embedded in the original text.
    """
    value = _ESCAPES.sub("", _STRINGS.sub("", value))
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    value = "".join(char for char in value if not unicodedata.category(char).startswith("C") or char == "\n")
    return value.strip() if multiline else " ".join(value.split())
