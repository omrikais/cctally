"""#965 static entry-point check (spec 5.7) over every non-pytest boundary.

It reads text and never executes an entry point, so a mutated copy is rejected
before any cctally process could run. Shell entry points: (1) the switch is
assigned before the first cctally or transcript-builder launch, by the entry
point's own `export`, or, for a harness, by sourcing the preamble or the fixture
library (both checked themselves; the preamble, bin/_lib-harness-env.sh, must
export it itself); (2) no later line clears or overrides it; (3) every launch
through a cleared environment (`env -i`, `env -`, `env --ignore-environment`,
`exec -c`, a clear written after other `env` options) restores `SWITCH=1` where
it reaches the launched program -- a shell prefix assignment or an `env` operand
after the last clear and before the program, never an assignment before `env`,
an earlier `export` or a program argument -- or follows a `# retention-control:
production-behavior -- <reason>` line. An `env -S` string, or an `env` option
the check does not know, may hide a clear and is never taken as restoring.
Builders (AST): the assignment `os.environ[SWITCH] = "1"` precedes the first
import of cctally (spec 5.7). A load point is a call of the builder's cctally
loader, any import of `cctally` / `_cctally_*` / `_lib_*` (a function-level
import counts) or a `sys.path` extension; none may sit at module level, every
one sits textually after the first assignment, and a function holding the
assignment, a loader call or a `sys.path` extension assigns before every load
point it holds; nothing clears the switch. A parent that sourced the preamble
never satisfies a builder, bin/cctally-test-all or serve.sh: each assigns the
switch itself.
"""
from __future__ import annotations

import ast
import dataclasses
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
SWITCH = "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF"
#: The standalone transcript builders of spec 4.3: T7b's population.
TRANSCRIPT_BUILDERS = ("build-bench-fixtures.py", "build-codex-reader-fixtures.py")
AGGREGATOR = BIN / "cctally-test-all"
SERVE = REPO_ROOT / "dashboard" / "web" / "e2e" / "serve.sh"
FIXTURE_LIB = BIN / "_lib-fixture-harness.sh"
PREAMBLE = BIN / "_lib-harness-env.sh"

_PROGRAM = (r"(?:bin/cctally(?![\w.-])|build-codex-reader-fixtures\.py"
            r"|build-bench-fixtures\.py)")
_PROGRAM_RE = re.compile(_PROGRAM)
_PATH_VAR_RE = re.compile(
    r"^\s*(?:export\s+|local\s+|readonly\s+)?([A-Za-z_]\w*)=\S*?" + _PROGRAM)
_ASSIGN_ONLY_RE = re.compile(r"^\s*(?:local\s+|readonly\s+|export\s+)?[A-Za-z_]\w*=\S*\s*$")
_SOURCE_RE = re.compile(r"^\s*(?:\.|source)\s+.*_lib-(?:harness-env|fixture-harness)\.sh")
_OWN_EXPORT_RE = re.compile(r"^\s*export\s+" + SWITCH + r'=(?:1|"1")\s*$')
_AGGREGATOR_LAUNCH_RE = re.compile(r"-m\s+pytest|\bxargs\s+-P")
_CLEARED_RE = re.compile(
    r"(?:^|[\s;&|(`])(?:/usr/bin/)?env\s+(?:-[A-Za-z]*i\b|-(?=\s)|--ignore-environment\b)")
_CLEAR_RES = (
    re.compile(r"\bunset\b[^;&|]*\b" + SWITCH + r"\b"),
    re.compile(r"\benv\b[^;&|]*(?:-u\s*|--unset=)" + SWITCH + r"\b"),
    re.compile(r"(?<![\w$])" + SWITCH + r'=(?!(?:1|"1")(?!\w))'),
)
_DECLARATION_RE = re.compile(r"^\s*#\s*retention-control:\s*production-behavior\s+\S")
_HEREDOC_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_SIBLING_RE = re.compile(r"^(?:cctally|_cctally_\w+|_lib_\w+)$")
_INGEST_CALLS = frozenset(
    ("sync_claude_conversations", "sync_codex_conversations", "cmd_cache_sync"))
_INGEST_ARGS = frozenset(("cache-sync", "--prune-conversations"))


def _code_lines(text: str) -> list[str]:
    """Per physical line: comments removed, heredoc bodies and single-quoted
    bytes blanked, double-quoted text kept (paths live there)."""
    out, state, pending, heredoc = [], None, [], None
    for raw in text.splitlines():
        if heredoc is not None:
            strip_tabs, delimiter = heredoc
            if (raw.lstrip("\t") if strip_tabs else raw) == delimiter:
                heredoc = pending.pop(0) if pending else None
            out.append("")
            continue
        buf, i = [], 0
        while i < len(raw):
            ch = raw[i]
            if state == "'":
                buf.append("'" if ch == "'" else " ")
                state = None if ch == "'" else state
                i += 1
                continue
            if ch == "\\" and i + 1 < len(raw):
                buf.append(raw[i:i + 2])
                i += 2
                continue
            if state == '"':
                state = None if ch == '"' else state
                buf.append(ch)
                i += 1
                continue
            if ch == "#" and (i == 0 or raw[i - 1] in " \t;&|("):
                break
            if ch in "'\"":
                state = ch
            buf.append(ch)
            i += 1
        line = "".join(buf)
        if state is None:
            pending.extend((m.group(1) == "-", m.group(3))
                           for m in _HEREDOC_RE.finditer(line))
            if pending:
                heredoc = pending.pop(0)
        out.append(line)
    return out


def _logical(lines: list[str]) -> list[tuple[int, str]]:
    """Join backslash continuations; returns (first physical index, text)."""
    joined, start, acc = [], None, ""
    for index, line in enumerate(lines):
        start = index if start is None else start
        if line.rstrip().endswith("\\"):
            acc += line.rstrip()[:-1] + " "
            continue
        joined.append((start, acc + line))
        start, acc = None, ""
    if acc:
        joined.append((start, acc))
    return joined


@dataclasses.dataclass(frozen=True)
class ClearedLaunch:
    lineno: int
    restores: bool
    declared: bool


#: Reserved words that may open a command.
_RESERVED = frozenset(("!", "{", "}", "if", "then", "elif", "else", "do", "while", "until"))
#: `env`'s long options (GNU; an unambiguous prefix also counts, as getopt_long
#: allows), the ones among them that take an argument when no `=` is written,
#: and the short options that take one (GNU `+C:iS:u:v0`, BSD `0iL:P:S:U:u:v`).
_ENV_LONG = ("--ignore-environment", "--unset", "--split-string", "--chdir", "--null",
             "--debug", "--block-signal", "--default-signal", "--ignore-signal",
             "--list-signal-handling", "--help", "--version")
_ENV_LONG_ARG = ("--unset", "--split-string", "--chdir")
_ENV_SHORT_ARG = "CLPSUu"
_ENV_SHORT = "iv0" + _ENV_SHORT_ARG
_PREFIXES = frozenset(("exec", "command", "nohup", "time", "builtin"))
_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)


def _commands(line: str) -> list[list[str]]:
    """The simple commands of one logical code line, as unquoted words.

    A small POSIX-shell word splitter: quotes are removed (an unclosed one runs
    to the end of the line, as a multi-line string does), a backslash escapes
    the next byte, a control operator or a `(`, `)` or backtick ends a command,
    and a redirection is dropped with its fd and target. Text `_code_lines`
    blanked stays blank."""
    commands, words, word, quoted, drop, i = [], [], None, None, False, 0

    def end_word():
        nonlocal word, drop
        if word is not None:
            if drop:
                drop = False
            else:
                words.append(word)
        word = None

    while i < len(line):
        ch = line[i]
        if quoted:
            if ch == quoted:
                quoted = None
            elif ch == "\\" and quoted == '"' and i + 1 < len(line):
                i += 1
                word += line[i]
            else:
                word += ch
        elif ch in "'\"":
            quoted, word = ch, word or ""
        elif ch == "\\" and i + 1 < len(line):
            i += 1
            word = (word or "") + line[i]
        elif ch.isspace():
            end_word()
        elif ch in "<>" or (ch == "&" and line[i + 1:i + 2] == ">"):
            if word is not None and word.isdigit():
                word = None  # the redirected fd
            end_word()
            while i + 1 < len(line) and line[i + 1] in "<>&|-":
                i += 1
            drop = True  # the target, the next word
        elif ch in ";&|()`":
            end_word()
            drop = False
            commands.append(words)
            words = []
        else:
            word = (word or "") + ch
        i += 1
    end_word()
    commands.append(words)
    return [command for command in commands if command]


def _assign(word: str, value):
    """The switch's state after ``word``, a NAME=VALUE: True only for exactly 1."""
    m = _ASSIGNMENT_RE.match(word)
    return value if m.group(1) != SWITCH else m.group(2) == "1"


def _walk_env(command: list[str], index: int, value):
    """One `env` invocation from its first argument. Returns (index of the
    program, cleared, the switch's state, opaque)."""
    cleared = opaque = False
    while index < len(command):
        arg = command[index]
        if arg == "--":
            index += 1
            break
        if arg == "-":  # the obsolete spelling of -i
            cleared, value = True, None
        elif arg.startswith("--"):
            name, has_value, operand = arg.partition("=")
            matches = [o for o in _ENV_LONG if o == name] or [
                o for o in _ENV_LONG if o.startswith(name) and len(name) > 2]
            name = matches[0] if len(matches) == 1 else None
            if name in _ENV_LONG_ARG and not has_value:
                index += 1
                operand = command[index] if index < len(command) else ""
            if name == "--ignore-environment":
                cleared, value = True, None
            elif name == "--unset" and operand == SWITCH:
                value = None
            elif name is None or name == "--split-string":
                cleared = opaque = True  # a clear the check cannot read
        elif arg.startswith("-") and len(arg) > 1:
            for pos, flag in enumerate(arg[1:]):
                if flag not in _ENV_SHORT:
                    cleared = opaque = True
                    break
                if flag == "i":
                    cleared, value = True, None
                elif flag in _ENV_SHORT_ARG:
                    operand = arg[pos + 2:]
                    if not operand:
                        index += 1
                        operand = command[index] if index < len(command) else ""
                    if flag == "S":
                        cleared = opaque = True
                    elif flag == "u" and operand == SWITCH:
                        value = None
                    break
        elif _ASSIGNMENT_RE.match(arg):
            value = _assign(arg, value)
        else:
            break
        index += 1
    return index, cleared, value, opaque


def _clears_and_restores(command: list[str]) -> tuple[bool, bool]:
    """Walk one simple command the way the shell and `env` apply it.

    Returns (cleared, restored): whether a step empties the environment (`env
    -i`, `env -`, `env --ignore-environment`, `exec -c`) and whether the switch
    is exactly 1 in the environment the program finally receives. An
    assignment counts only where it reaches that program: a shell prefix
    assignment or an `env` operand AFTER the last clear and before the program
    (965-DIFF-001). An `env -S` string or an option the walk does not know may
    hide a clear it cannot read: it counts as one that is never restored."""
    cleared = opaque = False
    value, index = None, 0
    while index < len(command) and command[index] in _RESERVED:
        index += 1
    while index < len(command):
        word = command[index]
        index += 1
        if _ASSIGNMENT_RE.match(word):
            value = _assign(word, value)
            continue
        head = word.rsplit("/", 1)[-1]
        if head == "env":
            index, env_cleared, value, env_opaque = _walk_env(command, index, value)
            cleared, opaque = cleared or env_cleared, opaque or env_opaque
            continue
        if head in _PREFIXES:
            while index < len(command) and len(command[index]) > 1 \
                    and command[index][0] == "-":
                option = command[index]
                index += 1
                if option == "--":
                    break
                for pos, flag in enumerate(option[1:]):
                    if head == "exec" and flag == "c":
                        cleared, value = True, None
                    if head == "exec" and flag == "a":  # -a NAME
                        index += pos == len(option) - 2
                        break
            continue
        break  # the program
    return cleared, cleared and not opaque and value is True


def cleared_env_launches(text: str) -> list[ClearedLaunch]:
    """Every logical line holding a launch through a cleared environment. It
    restores the switch only when every clearing command on it does, by its
    own walk; a line the walk cannot read (`_CLEARED_RE` sees a clear the
    walk does not, inside a string or a substitution) never restores."""
    raw = text.splitlines()
    found = []
    for start, line in _logical(_code_lines(text)):
        walked = [_clears_and_restores(command) for command in _commands(line)]
        clearing = [restored for cleared, restored in walked if cleared]
        if clearing or _CLEARED_RE.search(line):
            declared = start > 0 and bool(_DECLARATION_RE.match(raw[start - 1]))
            found.append(ClearedLaunch(start + 1, bool(clearing) and all(clearing),
                                       declared))
    return found


def shell_entry_points() -> list[tuple[pathlib.Path, str]]:
    points = [(p, "harness") for p in sorted(BIN.glob("cctally-*-test")) if p.is_file()]
    points += [(PREAMBLE, "preamble"), (FIXTURE_LIB, "harness"),
               (AGGREGATOR, "aggregator"), (SERVE, "serve")]
    return points


#: Entry points that must assign the switch with their own `export`, whether or
#: not they launch anything: sourcing never satisfies them.
_OWN_EXPORT_KINDS = ("preamble", "aggregator", "serve")


def shell_violations(text: str, *, name: str, kind: str) -> list[str]:
    """``kind``: "harness", "preamble" (bin/_lib-harness-env.sh, which a harness
    relies on when it sources it), "aggregator" (bin/cctally-test-all) or
    "serve"."""
    logical = _logical(_code_lines(text))
    path_vars = {m.group(1) for _, line in logical if (m := _PATH_VAR_RE.match(line))}
    var_re = (re.compile(r"\$\{?(?:" + "|".join(map(re.escape, sorted(path_vars)))
                         + r")\}?(?!\w)") if path_vars else None)
    assigned = launch = None
    problems = []
    for start, line in logical:
        if assigned is None and (_OWN_EXPORT_RE.match(line)
                                 or (kind == "harness" and _SOURCE_RE.match(line))):
            assigned = start
        if launch is None and not _ASSIGN_ONLY_RE.match(line) and (
                _PROGRAM_RE.search(line) or (var_re and var_re.search(line))
                or (kind == "aggregator" and _AGGREGATOR_LAUNCH_RE.search(line))):
            launch = start
        if assigned is not None and start > assigned and any(
                pattern.search(line) for pattern in _CLEAR_RES):
            problems.append(f"{name}:{start + 1}: clears or overrides {SWITCH} after assigning it")
    if kind in _OWN_EXPORT_KINDS and assigned is None:
        problems.append(f"{name}: never exports {SWITCH}=1 itself")
    elif launch is not None and (assigned is None or assigned > launch):
        problems.append(f"{name}:{launch + 1}: launches cctally or a transcript "
                        f"builder before assigning {SWITCH}=1")
    for cleared in cleared_env_launches(text):
        if not (cleared.restores or cleared.declared):
            problems.append(f"{name}:{cleared.lineno}: clears the environment without "
                            f"restoring {SWITCH}=1 or declaring a production-behavior control")
    return problems


def _is_switch_target(node) -> bool:
    return (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute) and node.value.attr == "environ"
            and isinstance(node.value.value, ast.Name) and node.value.value.id == "os"
            and isinstance(node.slice, ast.Constant) and node.slice.value == SWITCH)


def _assigns_one(node) -> bool:
    return (isinstance(node, ast.Assign) and any(map(_is_switch_target, node.targets))
            and isinstance(node.value, ast.Constant) and node.value.value == "1")


def _clears(node) -> bool:
    if isinstance(node, ast.Assign) and any(map(_is_switch_target, node.targets)):
        return not _assigns_one(node)
    if isinstance(node, ast.Delete):
        return any(map(_is_switch_target, node.targets))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        func = node.func
        names_switch = (bool(node.args) and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == SWITCH)
        on_environ = isinstance(func.value, ast.Attribute) and func.value.attr == "environ"
        on_os = isinstance(func.value, ast.Name) and func.value.id == "os"
        return names_switch and ((on_environ and func.attr in ("pop", "setdefault"))
                                 or (on_os and func.attr in ("unsetenv", "putenv")))
    return False


def _own_nodes(fn):
    """``fn``'s nodes, never descending into nested function bodies."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node)
                     if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                               ast.Lambda)))


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _loader_names(tree) -> set[str]:
    names = set()
    for fn in _functions(tree):
        for node in _own_nodes(fn):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", getattr(node.func, "id", None)) == "SourceFileLoader"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "cctally"):
                names.add(fn.name)
    return names


def _sibling_import(node) -> bool:
    """An absolute import of ``cctally`` or a sibling ``_cctally_*`` / ``_lib_*``."""
    if isinstance(node, ast.Import):
        modules = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.level == 0:
        modules = [node.module or ""]
    else:
        return False
    return any(_SIBLING_RE.match(module.split(".")[0]) for module in modules)


def _extends_sys_path(node) -> bool:
    """``sys.path.insert/append/extend(...)``: what lets a sibling import resolve."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("insert", "append", "extend")
            and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "path"
            and isinstance(node.func.value.value, ast.Name) and node.func.value.value.id == "sys")


def _module_level_nodes(tree):
    """Every node outside a function body. A class body is included: it runs
    when the module is imported, exactly like module-level code."""
    stack = [n for n in tree.body
             if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(child for child in ast.iter_child_nodes(node)
                     if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                               ast.Lambda)))


def builder_violations(source: str, name: str) -> list[str]:
    """Spec 5.7: the assignment precedes the first import of cctally.

    Load points are a call of the builder's cctally loader, any absolute
    import of ``cctally`` / ``_cctally_*`` / ``_lib_*`` (a function-level
    import counts), and any ``sys.path`` extension (what makes a sibling
    import resolvable). Rules: (1) no load point at module level; (2) a
    function that calls the loader, extends ``sys.path`` or assigns the switch
    assigns it before EVERY load point it contains; (3) every load point, at
    any nesting level, sits textually after the file's first assignment.

    Rule 3 replaces an assumption that a function whose only load points are
    sibling imports cannot run before rule 2's function: `python3
    bin/build-*.py` already has ``bin/`` as ``sys.path[0]``, so such an import
    resolves wherever it sits. The rule is textual, which is the spec's own
    measure ("precedes"); it does not follow calls, so a load point written
    after the assignment inside a function that a caller runs before the
    assigning function is not seen. Following calls would need value analysis:
    `build_fixture` in the bench builder imports `_lib_source_identity` before
    `_pin_env` assigns, but only when its caller passes ``identity_root``,
    which the builder's own `_main` never does."""
    tree = ast.parse(source, filename=name)
    problems = []
    loaders = _loader_names(tree)
    if not loaders:
        problems.append(f"{name}: no cctally loader (SourceFileLoader('cctally', ...))")

    def _is_loader_call(node):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in loaders)

    first_assignment = min((n.lineno for n in ast.walk(tree) if _assigns_one(n)),
                           default=None)
    loads = 0
    for node in _module_level_nodes(tree):
        if _sibling_import(node):
            problems.append(f"{name}:{node.lineno}: imports a cctally module at module level")
        elif _is_loader_call(node):
            loads += 1
            problems.append(f"{name}:{node.lineno}: loads cctally at module level")
        elif _extends_sys_path(node):
            problems.append(f"{name}:{node.lineno}: extends sys.path at module level, so a "
                            f"sibling import could resolve before {SWITCH}=1 is assigned")
    for fn in _functions(tree):
        own = list(_own_nodes(fn))
        assigned = min((n.lineno for n in own if _assigns_one(n)), default=None)
        gates = [n for n in own if _is_loader_call(n) or _extends_sys_path(n)]
        loads += sum(1 for n in own if _is_loader_call(n))
        if not gates and assigned is None:
            # A post-load helper, or no load point at all: rule 3 alone.
            problems += [f"{name}:{node.lineno}: {fn.name}() imports a cctally module "
                         f"before the first assignment of {SWITCH}=1"
                         for node in own if _sibling_import(node)
                         and (first_assignment is None or node.lineno < first_assignment)]
            continue
        for node in own:
            if not (_is_loader_call(node) or _extends_sys_path(node) or _sibling_import(node)):
                continue
            if assigned is None or assigned > node.lineno:
                what = ("loads cctally" if _is_loader_call(node)
                        else "extends sys.path" if _extends_sys_path(node)
                        else "imports a cctally module")
                problems.append(f"{name}:{node.lineno}: {fn.name}() {what} "
                                f"before assigning {SWITCH}=1")
    if loaders and not loads:
        problems.append(f"{name}: its cctally loader is never called")
    problems += [f"{name}:{n.lineno}: clears or overrides {SWITCH}"
                 for n in ast.walk(tree) if _clears(n)]
    return problems


def covered_builders(bin_dir: pathlib.Path = BIN) -> tuple[str, ...]:
    """Every builder that drives a cctally conversation ingest, from its own AST."""
    found = []
    for path in sorted(bin_dir.glob("build-*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        for node in ast.walk(tree):
            called = (getattr(node.func, "attr", getattr(node.func, "id", None))
                      if isinstance(node, ast.Call) else None)
            listed = isinstance(node, (ast.List, ast.Tuple)) and any(
                isinstance(e, ast.Constant) and e.value in _INGEST_ARGS for e in node.elts)
            if called in _INGEST_CALLS or listed:
                found.append(path.name)
                break
    return tuple(found)
