"""Shell completion: glue scripts and the hidden ``__complete`` command.

Completion is offline and side-effect free: it builds the command tree from ``cli_parser.parser()`` and a few
constants, and never opens a connection or reads the settings, credentials, session or token.
"""
import argparse
from pathlib import Path
import shlex
import sys

SHELLS = ("bash", "zsh", "fish", "powershell")
FILES = ":files"  # Tells the glue script to fall back to the shell's own file completion.
_PROGRESS = ("auto", "always", "never")

_GLUE = {
    "bash": r"""# zlib-cli completion for bash: eval "$(zlib-cli completion bash)" in ~/.bashrc
_zlib_cli_complete() {
    local IFS=$'\n' cur="${COMP_WORDS[COMP_CWORD]}"
    local -a candidates
    candidates=($(zlib-cli __complete bash "--line=${COMP_LINE:0:COMP_POINT}" 2>/dev/null))
    if [[ "${candidates[0]}" == ":files" ]]; then
        compopt -o filenames 2>/dev/null
        COMPREPLY=($(compgen -f -- "$cur"))
    else
        COMPREPLY=("${candidates[@]}")
    fi
}
complete -F _zlib_cli_complete zlib-cli
""",
    "zsh": r"""# zlib-cli completion for zsh: eval "$(zlib-cli completion zsh)" in ~/.zshrc, after compinit
_zlib_cli() {
    local -a candidates
    candidates=("${(@f)$(zlib-cli __complete zsh -- "${(@)words[2,CURRENT]}" 2>/dev/null)}")
    candidates=(${candidates:#})
    if [[ "${candidates[1]}" == ":files" ]]; then
        _files
    else
        compadd -- "${candidates[@]}"
    fi
}
compdef _zlib_cli zlib-cli
""",
    "fish": r"""# zlib-cli completion for fish: zlib-cli completion fish > ~/.config/fish/completions/zlib-cli.fish
function __zlib_cli_complete
    set -l words (commandline -opc)
    set -e words[1]
    set -l current (commandline -ct)
    set -l candidates (zlib-cli __complete fish -- $words "$current" 2>/dev/null)
    if test "$candidates[1]" = ":files"
        __fish_complete_path "$current"
    else
        printf '%s\n' $candidates
    end
end
complete -c zlib-cli -f -a '(__zlib_cli_complete)'
""",
    "powershell": r"""# zlib-cli completion for PowerShell: zlib-cli completion powershell | Out-String | Invoke-Expression in $PROFILE
Register-ArgumentCompleter -Native -CommandName 'zlib-cli', 'zlib-cli.exe' -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $done = @($commandAst.CommandElements | Select-Object -Skip 1 |
        Where-Object { $_.Extent.EndOffset -lt $cursorPosition } | ForEach-Object { $_.Extent.Text })
    $candidates = @(& zlib-cli __complete powershell "--current=$wordToComplete" -- @done 2>$null)
    if ($candidates.Count -gt 0 -and $candidates[0] -eq ':files') { return }
    $candidates | ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
}
""",
}


def glue(shell: str) -> str:
    return _GLUE[shell]


def _children(command) -> dict:
    for action in command._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _path_like(action) -> bool:
    return action.type is Path or (action.metavar or "").upper() in {"FILE", "DIR"}


def _values(action) -> list:
    from .diagnostics import KNOWN_DOMAINS
    if action.choices:
        return [str(choice) for choice in action.choices]
    if action.dest in ("domain", "domains"):
        return list(KNOWN_DOMAINS)
    return [FILES] if _path_like(action) else []


def _setting_values(key) -> list:
    from .client import SUPPORTED_FORMATS
    from .diagnostics import KNOWN_DOMAINS
    return {"domain": list(KNOWN_DOMAINS), "formats": list(SUPPORTED_FORMATS), "progress": list(_PROGRESS),
            "download_dir": [FILES], "credentials_file": [FILES]}.get(key, [])


def _positional(command, index):
    """The positional action that takes the word at ``index`` among the given positionals."""
    used = 0
    for action in command._actions:
        if action.option_strings or isinstance(action, argparse._SubParsersAction):
            continue
        if action.nargs in ("*", "+") or index == used:
            return action
        used += 1
    return None


def candidates(words) -> list:
    """Completion candidates for the words after ``zlib-cli``; the last word is the one being typed."""
    from .cli_examples import HIDDEN
    from .cli_parser import parser
    from .settings import KEYS
    *done, current = list(words) or [""]
    # bash splits --option=value at "=", so "--format = ep" is "--format ep".
    done = [word for index, word in enumerate(done) if not (word == "=" and index and done[index - 1].startswith("--"))]
    if current == "=" and done and done[-1].startswith("--"):
        current = ""
    command = root = parser()
    path, given, pending = [], [], None
    for word in done:
        if pending is not None:
            pending = None
        elif word.startswith("-") and word != "-":
            action = command._option_string_actions.get(word.split("=", 1)[0])
            if action is not None and action.nargs != 0 and "=" not in word:
                pending = action
        elif not given and word in _children(command):
            command = _children(command)[word]
            path.append(word)
        else:
            given.append(word)
    if pending is not None:
        found = _values(pending)
    elif current.startswith("--") and "=" in current:
        name, current = current.split("=", 1)
        action = command._option_string_actions.get(name)
        values = [value for value in _values(action) if value != FILES] if action is not None and action.nargs != 0 else []
        return [f"{name}={value}" for value in values if value.startswith(current)]
    elif current.startswith("-") and current != "-":
        found = [option for action in command._actions if action.help != argparse.SUPPRESS
                 for option in action.option_strings]
    elif _children(command) and not given:
        found = [name for name in _children(command) if not (command is root and name in HIDDEN)]
    elif path[:1] == ["config"] and path[1:] in (["get"], ["set"], ["unset"]):
        found = list(KEYS) if not given else _setting_values(given[0]) if path[1] == "set" else []
    else:
        action = _positional(command, len(given))
        found = _values(action) if action is not None else []
    if found == [FILES]:
        return found
    return [value for value in found if value.startswith(current) and value != FILES]


def split_line(line: str) -> list:
    """The words of a command line up to the cursor; the last one is being typed (empty after a space)."""
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    words = []
    try:
        for word in lexer:
            words.append(word)
    except ValueError:  # An open quote: the partial word is the one being typed.
        words.append(lexer.token)
        return words
    if not line or line[-1].isspace():
        words.append("")
    return words


def run_complete(argv) -> int:
    """``__complete SHELL [--current=WORD] -- WORDS...`` or ``__complete bash --line=LINE``.

    Prints one candidate per line on stdout. bash splits words at ``:`` and ``=`` (COMP_WORDBREAKS), so it
    passes the line itself and gets candidates without the part of the word before the last break.
    """
    if not argv or argv[0] not in SHELLS:
        return 2
    rest = list(argv[1:])
    current = line = None
    if rest and rest[0].startswith("--line="):
        line = rest.pop(0).split("=", 1)[1]
    elif rest and rest[0].startswith("--current="):
        current = rest.pop(0).split("=", 1)[1]
    if rest and rest[0] == "--":
        rest.pop(0)
    try:
        words = split_line(line)[1:] if line is not None else rest + ([current] if current is not None else [])
        found = candidates(words)
        if line is not None and found != [FILES]:
            typed = words[-1] if words else ""
            cut = max(typed.rfind(":"), typed.rfind("=")) + 1
            found = [value[cut:] for value in found]
    except Exception:  # A completion error must never print a traceback into the user's command line.
        return 1
    for value in found:
        sys.stdout.write(value + "\n")
    sys.stdout.flush()
    return 0
