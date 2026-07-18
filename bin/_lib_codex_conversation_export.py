"""#294 S7 — deterministic Codex conversation Markdown renderer (§3.3).

The Codex sibling of ``_lib_conversation_export`` (Claude). Pure and I/O-free: the
whole input is the neutral detail envelope ``get_codex_conversation`` produces
(items + blocks + per-turn cost + tokens + threading), so the render is a pure
function of a fixed DB + effective_speed — no ambient width, version, or clock
inputs, so a fixture golden only stales on a real data/pricing change.

Provider-truthful throughout: native Codex token labels
(``input``/``output``/``cached_input``/``reasoning_output``, never Claude cache
vocabulary), per-turn cost with the unattributed bucket rendered as an explicit
line, and children listed as ``v1.`` conversation-key references — NEVER inlined
(a Codex child is its own conversation; inlining would double-export and break
cost-once).
"""
from __future__ import annotations


def _money(value: float | None) -> str:
    """Deterministic USD formatting (no locale / ambient inputs)."""
    return f"${(value or 0.0):.4f}"


def _token_line(tokens: dict | None) -> str:
    """Provider-native token summary — Codex fields only."""
    t = tokens or {}
    return (f"input {int(t.get('input', 0) or 0)} · "
            f"output {int(t.get('output', 0) or 0)} · "
            f"cached_input {int(t.get('cached_input', 0) or 0)} · "
            f"reasoning_output {int(t.get('reasoning_output', 0) or 0)}")


def _blockquote(text: str) -> str:
    """Render ``text`` as a Markdown blockquote (one ``> `` per line)."""
    lines = (text or "").splitlines() or [""]
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines)


def _fence(text: str) -> str:
    return f"```\n{text}\n```" if text else "_(empty)_"


_ITEM_HEADER = {
    "user": "## 👤 User",
    "assistant": "## 🤖 Assistant",
    "event": "## 🗓 Event",
}


def _render_block(b: dict) -> str:
    bk = b.get("kind")
    text = (b.get("text") or "").strip()
    if bk == "reasoning":
        return "> 💭 Reasoning\n>\n" + _blockquote(text) if text else ""
    if bk == "tool_call":
        detail = b.get("detail") if isinstance(b.get("detail"), dict) else {}
        name = detail.get("name")
        head = f"**🔧 Tool call: {name}**" if name else "**🔧 Tool call**"
        chunks = [head]
        if text:
            chunks.append(_fence(text))
        output = b.get("output")
        if isinstance(output, dict):
            otext = (output.get("text") or "").strip()
            chunks.append("**Output**")
            chunks.append(_fence(otext))
        return "\n\n".join(c for c in chunks if c)
    if bk == "tool_output":
        return "**🔧 Tool output**\n\n" + _fence(text)
    if bk == "event":
        return f"> 🗓 {text}" if text else ""
    # user / assistant prose block
    return text


def _render_item(it: dict) -> str:
    kind = it.get("kind")
    header = _ITEM_HEADER.get(kind, f"## {kind}")
    ts = it.get("timestamp_utc") or ""
    if ts:
        header += f"  ·  {ts}"
    model = it.get("model")
    if kind == "assistant" and model:
        header += f"  ·  {model}"
    chunks = [header]
    for b in it.get("blocks", []):
        rendered = _render_block(b)
        if rendered:
            chunks.append(rendered)
    cost = it.get("cost_usd")
    if cost is not None:
        chunks.append(f"_Cost: {_money(cost)} · {_token_line(it.get('tokens'))}_")
    return "\n\n".join(c for c in chunks if c)


def render_codex_conversation_markdown(detail: dict) -> str:
    """Render a whole Codex conversation (an ``ok`` detail envelope) to Markdown.

    Deterministic for a fixed ``detail``. Children appear only as ``v1.`` reference
    lines — never inlined. The pending / not_found envelopes are handled upstream
    (this renderer only ever sees ``ok``)."""
    title = (detail.get("title") or "").strip()
    parts = [f"# {title}" if title else "# Codex conversation"]
    parent = detail.get("parent")
    if isinstance(parent, dict) and parent.get("conversation_key"):
        parts.append(
            f"_Parent: {(parent.get('title') or '').strip()} "
            f"(`{parent['conversation_key']}`)_")
    for it in detail.get("items", []):
        parts.append(_render_item(it))
    unattr = detail.get("unattributed_cost_usd")
    if unattr:
        parts.append(f"_Unattributed cost: {_money(unattr)}_")
    parts.append(
        f"_Total cost: {_money(detail.get('total_cost_usd'))} · "
        f"{_token_line(detail.get('tokens'))}_")
    children = detail.get("children") or []
    if children:
        lines = ["## Child conversations"]
        for c in children:
            lines.append(
                f"- {(c.get('title') or '').strip()} (`{c.get('conversation_key')}`)")
        parts.append("\n".join(lines))
    return "\n\n".join(p for p in parts if p).rstrip() + "\n"
