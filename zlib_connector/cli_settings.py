"""Settings for one CLI run: load once, fill the arguments, print each notice once (stderr)."""
from pathlib import Path
import sys

from .search import SearchFilters
from .settings import SettingsError, load_settings
from .terminal_text import terminal_text


# Option names of the overrides, for messages and ``config list --show-origin``.
_OPTIONS = {"domain": "--domain", "timeout": "--timeout", "credentials_file": "--credentials", "api_url": "--api-url"}


def overrides(args) -> dict:
    """The values given on the command line in this run (None when not given)."""
    values = {"domain": getattr(args, "domain", None), "timeout": getattr(args, "timeout", None),
              "credentials_file": getattr(args, "credentials", None), "api_url": getattr(args, "api_url", None)}
    return {key: value for key, value in values.items() if value is not None}


def load_for(args, *, environ=None, missing_ok=False):
    """Load the settings for these arguments (--config, --no-config and the overrides)."""
    return load_settings(overrides(args), environ=environ, config_file=getattr(args, "config", None),
                         use_config=not getattr(args, "no_config", False), missing_ok=missing_ok, options=_OPTIONS)


def ensure_settings(args, *, environ=None):
    """Load once per run and fill the shared arguments; later calls return the same Settings.

    ``args.domain`` becomes the domain of levels 1 to 3 (an explicit --domain is kept as given),
    ``args.domain_source`` names who set it. The timeout and the API URL get their resolved values.
    """
    settings = getattr(args, "settings", None)
    if settings is not None:
        return settings
    settings = load_for(args, environ=environ)
    args.settings = settings
    if not hasattr(args, "notices"):
        args.notices = set()
    if settings.origin("domain") != "cli":
        args.domain = settings.domain
    args.domain_source = settings.source_label("domain") if settings.domain is not None else None
    args.timeout = settings.timeout
    if hasattr(args, "api_url") and settings.origin("api_url") != "cli":
        args.api_url = settings.api_url
    if getattr(args, "quiet", False):
        args.progress = False
    elif getattr(args, "progress", None) is None:
        args.progress = {"always": True, "never": False}.get(settings.progress)
    return settings


def notice(args, text: str) -> None:
    """Print one stderr notice, at most once per run."""
    if getattr(args, "quiet", False):
        return
    seen = getattr(args, "notices", None)
    if seen is None:
        seen = args.notices = set()
    if text in seen:
        return
    seen.add(text)
    try:
        print(terminal_text(text), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def cli_domain(args):
    """The domain given with --domain in this run, or None."""
    settings = getattr(args, "settings", None)
    if settings is not None and settings.origin("domain") != "cli":
        return None
    return getattr(args, "domain", None)


def named_credentials(args) -> Path | None:
    """A named credentials file: --credentials, else ZLIB_CREDENTIALS, else credentials_file.

    A configured file must exist (config_invalid naming the source); --credentials keeps its message.
    """
    given = getattr(args, "credentials", None)
    if given is not None:
        return given
    settings = getattr(args, "settings", None)
    if settings is None or settings.credentials_file is None:
        return None
    path = settings.credentials_file
    if not path.is_file():
        origin = settings.origin("credentials_file")
        raise SettingsError(f"The credentials file set by {settings.source_label('credentials_file')} does not exist.",
                            key="credentials_file", source=origin,
                            hint="Run: zlib-cli config unset credentials_file" if origin == "config" else None)
    return path


def configured_folder(args):
    """The resolved download_dir for a default output folder, checked to exist; None when unset."""
    settings = getattr(args, "settings", None)
    if settings is None or settings.download_dir is None:
        return None
    folder = settings.download_dir
    if not folder.is_dir():
        origin = settings.origin("download_dir")
        raise SettingsError(f"The download folder set by {settings.source_label('download_dir')} does not exist or is"
                            " not a folder.", key="download_dir", source=origin,
                            hint="Create it, or run: zlib-cli config unset download_dir" if origin == "config"
                            else None)
    return folder


def default_output(args) -> Path:
    """--output when given, else the configured download_dir, else the current directory."""
    if getattr(args, "output", None) is not None:
        return args.output
    return configured_folder(args) or Path.cwd()


def _filter_notice(args, key, values):
    settings = args.settings
    origin = settings.origin(key)
    flag = "--format" if key == "formats" else "--language"
    shown = ", ".join(values)
    if origin == "config":
        text = (f"Using {key} from the config file: {shown}. Use {flag} to replace them, or --no-config to ignore"
                " the file.")
    else:
        text = f"Using {key} from {settings.source_label(key)}: {shown}. Use {flag} to replace them."
    notice(args, text)


def with_configured_filters(args, filters: SearchFilters, *, announce=True) -> SearchFilters:
    """Add configured formats and languages to a search that did not give its own; announce them once.

    Filters never apply to direct references and never choose an edition.
    """
    settings = getattr(args, "settings", None)
    if settings is None:
        return filters
    formats, languages = filters.formats, filters.languages
    if not formats and settings.formats and settings.origin("formats") in ("env", "config"):
        formats = settings.formats
        if announce:
            _filter_notice(args, "formats", formats)
    if not languages and settings.languages and settings.origin("languages") in ("env", "config"):
        languages = settings.languages
        if announce:
            _filter_notice(args, "languages", languages)
    if (formats, languages) == (filters.formats, filters.languages):
        return filters
    return SearchFilters(languages=languages, formats=formats, year_from=filters.year_from, year_to=filters.year_to,
                         order=filters.order)
