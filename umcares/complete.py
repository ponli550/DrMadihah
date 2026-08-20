"""Completion candidates for the umcares CLI.

`umcares complete <tokens...>` prints the candidates for the next token.
The shell completion function (completions/_umcares) calls this and feeds the
list to fzf, scoped to the `umcares` command only.

The last token is the one being completed (possibly empty). A leading marker
line selects how the shell should treat the rest:

    __RECIPES__   recipe files under recipes/ (and .umcares/)
    __FILES__     any local file
    <none>        literal candidates
"""
import argparse

_SUBS_TO_SKIP = {"complete"}

_GLOBAL_OPTIONS = ["-h", "--help", "-v", "--transport", "--version"]

_RECIPE_VALUE = {
    ("render", "file"),
    ("verify", "file"),
    ("script", "file"),
    ("preview", "voice"),
}

_FILE_VALUE = {
    ("ingest", "csv"),
    ("ingest", "out"),
    ("recipe", "out"),
    ("script", "md"),
    ("script", "srt"),
    ("script", "out"),
    ("push", "local"),
    ("premiere", "out"),
    ("premiere", "preset"),
    ("media", "out"),
    ("post", "master"),
    ("post", "music"),
    ("post", "srt"),
    ("post", "src"),
    ("post", "sections"),
    ("post", "out"),
}


def _find_action(parser: argparse.ArgumentParser, name: str):
    n = name.lstrip("-")
    for a in parser._actions:
        if any(o.lstrip("-") == n for o in a.option_strings):
            return a
    return None


def _takes_value(a) -> bool:
    return not isinstance(a, (argparse._StoreConstAction,
                              argparse._StoreTrueAction,
                              argparse._StoreFalseAction,
                              argparse._HelpAction,
                              argparse._VersionAction)) and a.nargs != 0


def _option_strings(sp: argparse.ArgumentParser) -> list:
    out = set(_GLOBAL_OPTIONS)
    for a in sp._actions:
        out.update(a.option_strings)
    return sorted(out)


def _positional_count(tail: list, sp: argparse.ArgumentParser) -> int:
    count, i = 0, 0
    while i < len(tail):
        t = tail[i]
        if t.startswith("-"):
            if "=" in t:
                i += 1
                continue
            a = _find_action(sp, t)
            if a is not None and _takes_value(a):
                i += 2
            else:
                i += 1
            continue
        count += 1
        i += 1
    return count


def _value_candidates(sp: argparse.ArgumentParser, name: str) -> list:
    a = _find_action(sp, name)
    if a is None:
        return []
    if a.choices:
        return sorted(a.choices)
    key = (sp.prog.rsplit(" ", 1)[-1], a.dest)
    if key in _RECIPE_VALUE:
        return ["__RECIPES__"]
    if key in _FILE_VALUE:
        return ["__FILES__"]
    return []


def _prefixed(cands: list, prefix: str) -> list:
    return [c for c in cands if c.startswith(prefix)] if prefix else cands


def _flag_candidates(sp: argparse.ArgumentParser, current: str,
                     main: argparse.ArgumentParser) -> list:
    """Flags, plus `flag=choice` forms when the typed flag has choices."""
    out = _option_strings(sp)
    a = _find_action(sp, current) or _find_action(main, current)
    if a is not None and a.choices:
        out += [f"{current}={c}" for c in sorted(a.choices)]
    return _prefixed(out, current)


def complete_candidates(tokens: list) -> list:
    """Candidates for the next token after `tokens` (last = being completed)."""
    import umcares.cli as cli
    main = cli.build_parser()
    sub = main._subparsers._group_actions[0]
    subs = {name: p for name, p in sub.choices.items()
            if name not in _SUBS_TO_SKIP}

    if not tokens:
        return sorted(subs)

    if tokens and tokens[0] == "--":
        tokens = tokens[1:]      # zsh separator; the rest are raw arguments
    current = tokens[-1]
    done = tokens[:-1]

    first = next((t for t in done if t in subs), None)
    if first is None:
        if current.startswith("-"):
            return _flag_candidates(main, current, main)
        if done and done[-1] == "--transport":
            a = _find_action(main, "--transport")
            return sorted(a.choices) if a and a.choices else []
        return _prefixed(sorted(subs), current)

    sp = subs[first]
    tail = done[1:]

    if tail and tail[-1].startswith("-") and "=" not in tail[-1]:
        a = _find_action(sp, tail[-1]) or _find_action(main, tail[-1])
        if a is not None and _takes_value(a):
            return _value_candidates(sp, tail[-1])

    if "=" in current:
        name = current.split("=")[0]
        vals = _value_candidates(sp, name)
        if vals == ["__RECIPES__"] or vals == ["__FILES__"]:
            return vals
        return _prefixed([f"{name}={c}" for c in vals], current) or [name + "="]

    if current.startswith("-"):
        return _flag_candidates(sp, current, main)

    if tail and tail[-1].startswith("-"):
        a = _find_action(sp, tail[-1]) or _find_action(main, tail[-1])
        if a is not None and _takes_value(a):
            return _value_candidates(sp, tail[-1])

    pos = [a for a in sp._actions if not a.option_strings]
    idx = _positional_count(tail, sp)
    out = _option_strings(sp)
    if idx < len(pos):
        a = pos[idx]
        if a.choices:
            out += sorted(a.choices)
        elif a.dest in ("local", "csv"):
            return ["__FILES__"]
    return _prefixed(out, current)


def print_candidates(tokens: list) -> None:
    try:
        for c in complete_candidates(tokens):
            print(c)
    except Exception:
        pass