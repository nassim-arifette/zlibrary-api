"""Explicit local credential configuration; never log or repr its contents."""
from dataclasses import dataclass
from pathlib import Path
import tomllib

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "credentials.local.toml"

@dataclass(frozen=True, repr=False)
class Credentials:
    domain: str = "https://z-lib.by/"
    email: str = ""
    password: str = ""

def load_credentials(path: Path | None = None) -> Credentials:
    source = path if path is not None else DEFAULT_FILE
    if not source.exists():
        if path is not None:
            raise ValueError("The specified credentials file does not exist.")
        return Credentials()
    try:
        values = tomllib.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("Cannot read the local credentials file. Check its TOML syntax; its contents are not shown.") from None
    allowed = {"domain", "email", "password"}
    if set(values) - allowed or any(not isinstance(value, str) for value in values.values()):
        raise ValueError("Credentials must contain only string fields: domain, email, password.")
    return Credentials(domain=values.get("domain", "https://z-lib.by/").strip(), email=values.get("email", "").strip(), password=values.get("password", ""))
