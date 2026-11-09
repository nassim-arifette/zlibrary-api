"""Safe account projections: server credentials never enter public models."""

from dataclasses import asdict, dataclass
import re

from .errors import FormatChangedError


@dataclass(frozen=True)
class DownloadQuota:
    limit: int | None
    used: int | None
    remaining: int | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AccountProfile:
    quota: DownloadQuota

    def to_dict(self) -> dict:
        return {"quota": self.quota.to_dict()}


def parse_profile(payload: dict) -> AccountProfile:
    user = payload.get("user")
    if not isinstance(user, dict):
        raise FormatChangedError("The API response has no account profile.")

    def count(key):
        value = user.get(key)
        if value is None or value == "":
            return None
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
            value = int(value)
        if type(value) is not int or not 0 <= value <= 10**12:
            raise FormatChangedError("The API returned an invalid download quota.")
        return value

    limit, used = count("downloads_limit"), count("downloads_today")
    remaining = max(0, limit - used) if limit is not None and used is not None else None
    return AccountProfile(DownloadQuota(limit, used, remaining))
