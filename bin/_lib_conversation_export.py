"""Pure, I/O-free Markdown serializer for a conversation session (issue #217 S5,
F1/F5). No DB / filesystem / locks — fed the assembled ``items`` (+
``subagent_meta``) from ``_assemble_session`` in ``_lib_conversation_query.py``,
so the renderer stays golden-testable (the same purity contract as
``_lib_share.py``). Golden-tested in ``tests/test_conversation_export.py``.

Public surface::

    export_session_markdown(items, scope, *, subagent_meta=None,
                            title=None, session_id=None) -> str

``scope`` is one of ``all`` / ``chat`` / ``prompts`` / ``recipe`` (the four
export scopes; the handler validates the value, so an unknown scope here falls
back to the ``all`` renderer). ``subagent_meta`` supplies subagent labels
(Codex P1-2); the serializer groups subagent turns by ``subagent_key`` in
document order — a deliberate simplification of the reader's full nesting tree.

Item / block shape (matches ``_assemble_session``): each item is a dict with
``kind`` (``human``/``assistant``/``tool_result``/``meta``), ``anchor`` (with
``uuid``), ``ts``, ``text``, ``blocks`` (list), ``subagent_key`` (``None`` for
the main session), ``is_sidechain``, plus ``meta_kind`` on meta items and
``model`` on assistant turns. Tool blocks are ``kind == "tool_call"`` with
``name`` / ``input`` (bounded dict) / ``input_truncated`` / ``result``
(``{text, truncated, is_error, ...}`` or ``None``) / optional ``edit_stat`` /
optional ``stderr`` (the structured Bash stderr suffix of ``result.text``).
"""

# Deliberately the narrow {Edit, MultiEdit, Write} set — NOT the wider
# `_FILE_TOUCH_TOOLS` (which includes NotebookEdit). Edit-family tool calls
# render as a fenced ```diff block.
_EDIT_TOOLS = {"edit", "multiedit", "write"}
_TRUNCATED = " … [truncated]"


def export_session_markdown(items, scope, *, subagent_meta=None, title=None,
                            session_id=None):
    """Render a whole assembled session to Markdown for one export scope."""
    items = list(items or [])
    subagent_meta = subagent_meta or {}
    if scope == "recipe":
        return _render_recipe(items, title)
    if scope == "prompts":
        return _render_prompts(items, title)
    if scope == "chat":
        return _render_chat(items, title)
    return _render_all(items, subagent_meta, title)   # "all" (default)


# ---- shared predicates / helpers ------------------------------------------

def _is_main(it):
    """True for a main-session turn (not a subagent thread, not a sidechain)."""
    return it.get("subagent_key") is None and not it.get("is_sidechain")


def _item_text(it):
    return (it.get("text") or "").strip()


def _human_prompts(items):
    """Main-session human turns with non-empty prose, in document order."""
    return [it for it in items
            if it.get("kind") == "human" and _is_main(it) and _item_text(it)]


def extract_prompt_entries(items):
    """Ordered main-thread human prompts as ``[{"uuid", "text"}]``.

    Single source of truth for the structured prompt spine (#217 S7) — reuses
    the same main-thread predicate (``_human_prompts``) and the same prose
    extraction (``_item_text``) as the recipe/prompts export, so the
    ``/prompts`` route and the export can never drift. The uuid is the turn's
    ``anchor`` uuid — the same identity ``get_conversation_outline`` emits — so
    the spine aligns 1:1 with the outline's human turns.
    """
    return [{"uuid": it["anchor"]["uuid"], "text": _item_text(it)}
            for it in _human_prompts(items)]


def _title_header(title):
    """An H1 title block (with a trailing blank line) or empty string."""
    t = (title or "").strip()
    return f"# {t}\n\n" if t else ""


def _role_header(it):
    kind = it.get("kind")
    ts = it.get("ts") or ""
    if kind == "human":
        label = "## 👤 Human"
    elif kind == "assistant":
        label = "## 🤖 Assistant"
    elif kind == "meta":
        label = f"## ⚙️ {_meta_label(it)}"
    else:                                   # tool_result orphan / other
        label = "## 🔧 Tool result"
    return f"{label}{f'  ·  {ts}' if ts else ''}"


def _meta_label(it):
    mk = (it.get("meta_kind") or "note")
    return mk.replace("_", " ").title()


def _blockquote(text):
    """Render ``text`` as a Markdown blockquote (one ``> `` per line)."""
    lines = (text or "").splitlines() or [""]
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines)


# ---- tool-call rendering ---------------------------------------------------

def _render_tool_call(b):
    """Render one ``tool_call`` block to a Markdown chunk."""
    name = b.get("name") or "Tool"
    nm = name.lower()
    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    if nm in _EDIT_TOOLS:
        return _render_edit_call(name, inp, b)
    if nm == "bash":
        return _render_bash_call(inp, b)
    return _render_generic_call(name, b)


def _edit_diff_lines(name, inp):
    """Reconstruct +/- diff lines for an Edit/MultiEdit/Write call from the
    bounded input (best-effort — bounded leaves may be clipped)."""
    nm = (name or "").lower()
    lines = []
    if nm == "write":
        content = inp.get("content")
        if isinstance(content, str):
            for ln in content.splitlines():
                lines.append("+" + ln)
        return lines
    if nm == "multiedit":
        edits = inp.get("edits")
        if isinstance(edits, list):
            for e in edits:
                e = e if isinstance(e, dict) else {}
                lines.extend(_one_edit_lines(e.get("old_string"),
                                             e.get("new_string")))
        return lines
    # edit
    return _one_edit_lines(inp.get("old_string"), inp.get("new_string"))


def _one_edit_lines(old, new):
    out = []
    if isinstance(old, str):
        for ln in old.splitlines():
            out.append("-" + ln)
    if isinstance(new, str):
        for ln in new.splitlines():
            out.append("+" + ln)
    return out


def _render_edit_call(name, inp, b):
    path = inp.get("file_path")
    path = path if isinstance(path, str) and path else "(file)"
    diff = "\n".join(_edit_diff_lines(name, inp))
    trunc = _TRUNCATED if b.get("input_truncated") else ""
    head = f"**{name}** `{path}`{trunc}"
    return f"{head}\n\n```diff\n{diff}\n```"


def _render_bash_call(inp, b):
    command = inp.get("command")
    command = command if isinstance(command, str) else ""
    res = b.get("result") if isinstance(b.get("result"), dict) else None
    out_lines = [f"$ {command}"]
    if res is not None:
        text = res.get("text") or ""
        stderr = b.get("stderr")
        stdout = text
        # result.text == stdout + stderr (the empirical Bash storage shape); when
        # the structured stderr suffix is present, split it out for clarity.
        if isinstance(stderr, str) and stderr and text.endswith(stderr):
            stdout = text[: len(text) - len(stderr)]
        if stdout:
            out_lines.append(stdout.rstrip("\n"))
        if isinstance(stderr, str) and stderr:
            out_lines.append("# stderr")
            out_lines.append(stderr.rstrip("\n"))
        if res.get("truncated"):
            out_lines.append(_TRUNCATED.strip())
    body = "\n".join(out_lines)
    return f"```bash\n{body}\n```"


def _render_generic_call(name, b):
    parts = [f"**Tool: {name}**"]
    summary = b.get("input_summary")
    if isinstance(summary, str) and summary.strip() and summary.strip() != "{}":
        parts.append(f"`{summary.strip()}`")
    res = b.get("result") if isinstance(b.get("result"), dict) else None
    if res is not None:
        text = (res.get("text") or "").rstrip("\n")
        trunc = _TRUNCATED if res.get("truncated") else ""
        if text:
            parts.append(f"```\n{text}\n```{trunc}" if not trunc
                         else f"```\n{text}\n```\n{trunc.strip()}")
        elif trunc:
            parts.append(trunc.strip())
    return "\n\n".join(parts)


# ---- per-turn rendering (the `all` matrix) ---------------------------------

def _render_turn_body(it):
    """The body chunks of a single turn (prose, thinking, tool calls, meta)."""
    chunks = []
    kind = it.get("kind")
    prose = _item_text(it)

    if kind == "meta":
        # A meta turn renders its body as a labeled blockquote.
        if prose:
            chunks.append(_blockquote(prose))
        return chunks

    if prose:
        chunks.append(prose)

    for b in it.get("blocks", []):
        bk = b.get("kind")
        if bk == "thinking":
            tx = (b.get("text") or "").strip()
            if tx:
                chunks.append("> 💭 Thinking\n>\n" + _blockquote(tx))
        elif bk in ("tool_call", "tool_use"):
            chunks.append(_render_tool_call(b))
        elif bk == "tool_result":
            # orphan tool_result block (rare — a result the kernel could not fold)
            tx = (b.get("text") or "").rstrip("\n")
            trunc = _TRUNCATED.strip() if b.get("truncated") else ""
            if tx:
                chunks.append(f"**Tool result**\n\n```\n{tx}\n```"
                              + (f"\n{trunc}" if trunc else ""))
    return chunks


def _render_all(items, subagent_meta, title):
    """Full-fidelity, document-order render. Main-session turns render inline;
    consecutive subagent turns (same subagent_key) group under one heading."""
    parts = [_title_header(title)] if _title_header(title) else []
    cur_subagent = None     # the subagent_key whose heading is currently open

    for it in items:
        sk = it.get("subagent_key")
        if sk is not None or it.get("is_sidechain"):
            # Subagent turn — open a grouping heading on a new key.
            if sk != cur_subagent:
                label = _subagent_label(sk, subagent_meta)
                parts.append(f"### ⎇ Subagent: {label}")
                cur_subagent = sk
        else:
            cur_subagent = None     # back to the main thread

        body = _render_turn_body(it)
        block = [_role_header(it)]
        block.extend(body)
        parts.append("\n\n".join(p for p in block if p))

    return "\n\n".join(p for p in parts if p).rstrip() + "\n"


def _subagent_label(sk, subagent_meta):
    meta = subagent_meta.get(sk) if isinstance(subagent_meta, dict) else None
    if isinstance(meta, dict):
        kind = meta.get("kind") or meta.get("subagent_type")
        if isinstance(kind, str) and kind.strip():
            return kind.strip()
    return sk if isinstance(sk, str) and sk else "subagent"


# ---- the `chat` scope (prose-only, main-session) --------------------------

def _render_chat(items, title):
    """Human + assistant prose only — no thinking, no tools, no meta;
    main-session only. Deliberately leaner than the live 'chat' focus mode
    (which retains thinking) — an intentional divergence (spec §1)."""
    parts = [_title_header(title)] if _title_header(title) else []
    for it in items:
        if it.get("kind") not in ("human", "assistant"):
            continue
        if not _is_main(it):
            continue
        prose = _item_text(it)
        if not prose:
            continue
        parts.append(f"{_role_header(it)}\n\n{prose}")
    return "\n\n".join(p for p in parts if p).rstrip() + "\n"


# ---- the `prompts` scope --------------------------------------------------

def _render_prompts(items, title):
    """Main-session human turns only, each as ``## Prompt N`` + full text."""
    parts = [_title_header(title)] if _title_header(title) else []
    for n, it in enumerate(_human_prompts(items), start=1):
        parts.append(f"## Prompt {n}\n\n{_item_text(it)}")
    return "\n\n".join(p for p in parts if p).rstrip() + "\n"


# ---- the `recipe` scope (F5) ----------------------------------------------

def _render_recipe(items, title):
    """A ``# Replay recipe`` header + a numbered list of main-session prompts —
    the re-runnable script form."""
    prompts = _human_prompts(items)
    out = ["# Replay recipe"]
    if title and title.strip():
        out.append(f"_{title.strip()}_")
    if prompts:
        numbered = []
        for n, it in enumerate(prompts, start=1):
            # Collapse internal newlines so each prompt is one numbered step.
            text = " ".join(_item_text(it).split())
            numbered.append(f"{n}. {text}")
        out.append("\n".join(numbered))
    else:
        out.append("_(no prompts)_")
    return "\n\n".join(out).rstrip() + "\n"
