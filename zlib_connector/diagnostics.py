"""Anonymous domain diagnostics. Every probe owns a fresh, isolated client."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from itertools import islice
import math
from time import monotonic
from urllib.parse import urlsplit

from .eapi import Client
from .errors import AuthenticationError, ChallengeRequired, ConnectorError, FormatChangedError, NetworkError, RateLimitError
from .transport import validate_proxy, validate_url


# User-supplied candidates, recorded on 2026-11-09. Presence is not proof of health.
KNOWN_DOMAINS = (
    "https://z-lib.sk/", "https://z-library.im/", "https://z-library.sk/", "https://z-lib.fm/",
    "https://1lib.sk/", "https://z-lib.gd/", "https://z-lib.gl/", "https://z-lib.by/",
    "https://z-library.website/", "https://zlib.bz/", "https://z-library.qa/", "https://z-library.ec/",
)

_MESSAGES = {
    "dns": "DNS resolution failed.",
    "blocked_address": "The destination resolves to a private or reserved address.",
    "certificate": "HTTPS certificate verification failed.",
    "tls": "TLS negotiation failed.",
    "proxy": "The proxy could not be reached or refused the tunnel.",
    "timeout": "The probe timed out.",
    "redirect": "The request redirects to another origin or an unsupported flow.",
    "http": "The server returned an unsuccessful HTTP status.",
    "network": "The network request failed.",
    "challenge": "The server blocks API access or requires browser verification.",
    "authentication_required": "The server refused anonymous access.",
    "rate_limited": "The server rate limit was reached.",
    "protocol_changed": "The search response or destination does not match the expected API.",
    "api_refused": "The server refused the search operation.",
}


@dataclass(frozen=True)
class DomainDiagnostic:
    domain: str
    search_ok: bool
    checked_at: str
    elapsed_ms: int
    final_domain: str | None = None
    result_count: int | None = None
    error_code: str | None = None
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DomainReport:
    results: tuple[DomainDiagnostic, ...]
    proxy: str | None = None  # The proxy every probe went through, if any

    @property
    def recommended_domain(self) -> str | None:
        working = [result for result in self.results if result.search_ok]
        return min(working, key=lambda result: result.elapsed_ms).domain if working else None

    def to_dict(self) -> dict:
        return {"results": [result.to_dict() for result in self.results],
                "working_domains": [result.domain for result in self.results if result.search_ok],
                "recommended_domain": self.recommended_domain, "proxy": self.proxy}


def _origin(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or not value.isprintable():
        raise ValueError("Use an HTTPS site origin for each domain.")
    validate_url(value)
    parsed = urlsplit(value)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Use site origins without paths, query parameters or fragments.")
    return "https://" + parsed.netloc.lower().removesuffix(":443") + "/"


def _probe(domain: str, timeout: float, proxy: str | None = None) -> DomainDiagnostic:
    checked_at = datetime.now(timezone.utc).isoformat()
    started = monotonic()
    try:
        with Client(domain, timeout=timeout, proxy=proxy) as client:
            books = client.search("Pride and Prejudice", limit=1)
            return DomainDiagnostic(domain, True, checked_at, round((monotonic() - started) * 1000),
                                    final_domain=client.domain, result_count=len(books))
    except ConnectorError as error:
        if isinstance(error, NetworkError):
            code = error.reason if error.reason in _MESSAGES else "network"
        elif isinstance(error, ChallengeRequired):
            code = "challenge"
        elif isinstance(error, AuthenticationError):
            code = "authentication_required"
        elif isinstance(error, RateLimitError):
            code = "rate_limited"
        elif isinstance(error, FormatChangedError):
            code = "protocol_changed"
        else:
            code = "api_refused"
        return DomainDiagnostic(domain, False, checked_at, round((monotonic() - started) * 1000),
                                error_code=code, message=_MESSAGES[code])


def diagnose_domains(domains=None, *, timeout: float = 10, workers: int = 4, on_result=None,
                     proxy: str | None = None) -> DomainReport:
    """Probe up to 50 HTTPS origins without credentials, preserving input order.

    Results describe this network and time only. The recommendation never changes
    configuration or authenticates to another domain. Timeouts apply per request.
    ``on_result(done, total)`` is called after each probe, from the calling thread.
    ``proxy`` (``http://HOST:PORT``) tunnels every probe; the report names it.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("Timeout must be a finite number between 0 and 300 seconds.")
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("Worker count must be an integer between 1 and 8.")
    values = KNOWN_DOMAINS if domains is None else domains
    if isinstance(values, str):
        raise ValueError("Pass domains as a sequence, not a single string.")
    try:
        values = tuple(islice(values, 51))
    except TypeError:
        raise ValueError("Pass domains as a sequence of HTTPS origins.") from None
    if not 1 <= len(values) <= 50:
        raise ValueError("Choose between 1 and 50 domain candidates.")
    origins = tuple(dict.fromkeys(_origin(value) for value in values))
    if proxy is not None:
        proxy = validate_proxy(proxy)
    with ThreadPoolExecutor(max_workers=min(workers, len(origins))) as executor:
        results = []
        for result in executor.map(lambda domain: _probe(domain, timeout, proxy), origins):
            results.append(result)
            if on_result is not None:
                on_result(len(results), len(origins))
    return DomainReport(tuple(results), proxy)
