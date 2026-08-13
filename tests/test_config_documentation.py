"""`docs/commands/config.md` documents the real configuration contract (#513 S1).

The page's "Allowed keys" table is tied to two runtime sources: the key list
comes from ``ALLOWED_CONFIG_KEYS`` and the `Dashboard writable` column comes
from ``SETTINGS_LEAF_DISPOSITIONS``. The check imports both rather than
restating them, so it can never certify a stale copy of either.

The page is parsed with ``markdown-it-py``'s table rule, not scanned for
pipes. Pipe-shaped prose is common in this repo's docs and a grep-shaped
check would read a sentence as a table row -- the canaries at the bottom pin
that. ``markdown-it-py`` is pinned for authoritative tests at
``tests/requirements-dev.txt``; the standalone doc-lint workflow installs no
dependencies, which is why this is a pytest rather than a rule in
``bin/cctally-doc-lint-test``.

What this check does NOT certify: that a `Values` cell describes the value
domain correctly. It requires the cell to be non-empty and, where the default
is mechanically obtainable from the runtime, requires the `Default` cell to
match it. The prose itself is a human review obligation.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script  # noqa: E402

# Imported, never skipped. `markdown-it-py` is pinned at
# `tests/requirements-dev.txt` and every gate installs that closure, so an
# ImportError here means the gate is broken -- and this check is the one
# mechanical tie between the documented key table and the runtime registry.
# Skipping it would report a green run that certified nothing.
import markdown_it  # noqa: E402

CONFIG_MD = _ROOT / "docs" / "commands" / "config.md"
HEADING = "Allowed keys"
HEADER_ROW = ["Key", "Values", "Default", "Dashboard writable", "Notes"]


# --- Markdown parsing ----------------------------------------------------

def _parser():
    return markdown_it.MarkdownIt("commonmark").enable("table")


def _parse_blocks(text):
    """Return the document as a flat list of heading / table / paragraph blocks."""
    tokens = _parser().parse(text)
    blocks = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open":
            blocks.append({
                "kind": "heading",
                "level": int(tok.tag[1:]),
                "text": tokens[i + 1].content.strip(),
            })
            i += 3
            continue
        if tok.type == "paragraph_open":
            blocks.append({"kind": "paragraph", "text": tokens[i + 1].content})
            i += 3
            continue
        if tok.type == "table_open":
            header, rows, current, section = [], [], None, None
            j = i + 1
            while tokens[j].type != "table_close":
                inner = tokens[j]
                if inner.type == "thead_open":
                    section = "head"
                elif inner.type == "tbody_open":
                    section = "body"
                elif inner.type == "tr_open":
                    current = []
                elif inner.type in ("th_open", "td_open"):
                    nxt = tokens[j + 1]
                    current.append(
                        nxt.content.strip() if nxt.type == "inline" else ""
                    )
                elif inner.type == "tr_close":
                    if section == "head":
                        header = current
                    else:
                        rows.append(current)
                j += 1
            blocks.append({"kind": "table", "header": header, "rows": rows})
            i = j + 1
            continue
        i += 1
    return blocks


@pytest.fixture(scope="module")
def allowed_keys_table():
    blocks = _parse_blocks(CONFIG_MD.read_text())
    headings = [
        i for i, b in enumerate(blocks)
        if b["kind"] == "heading" and b["level"] == 2 and b["text"] == HEADING
    ]
    assert len(headings) == 1, (
        f"expected exactly one level-2 '{HEADING}' heading, found {len(headings)}"
    )
    # Scoped to the section rather than to the block immediately after the
    # heading: the section opens with a paragraph explaining the three-state
    # column, and a reader meeting `Ignored` for the first time inside a
    # table cell is worse off. Requiring EXACTLY ONE table in the section is
    # the property that matters -- it is what stops a second table from
    # being added here and quietly becoming the one nobody checks.
    start = headings[0] + 1
    end = start
    while end < len(blocks) and not (
        blocks[end]["kind"] == "heading" and blocks[end]["level"] <= 2
    ):
        end += 1
    tables = [b for b in blocks[start:end] if b["kind"] == "table"]
    assert len(tables) == 1, (
        f"expected exactly one table in the '{HEADING}' section, "
        f"found {len(tables)}"
    )
    return tables[0]


# --- Runtime sources ------------------------------------------------------

@pytest.fixture(scope="module")
def runtime():
    ns = load_script()
    spec = importlib.util.spec_from_file_location(
        "_lib_dashboard_settings_contract",
        _BIN / "_lib_dashboard_settings_contract.py",
    )
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    return {"ns": ns, "contract": contract}


def _render_default(value):
    """Render a runtime default the way the Default column writes it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@pytest.fixture(scope="module")
def mechanical_defaults(runtime):
    """Defaults obtainable from the runtime, keyed by config key.

    Every value here is read out of the code, never typed in. A key absent
    from this map has a `Default` cell the check only requires to be
    non-empty -- see the module docstring on what this does not certify.
    """
    ns = runtime["ns"]

    def sym(name):
        """Resolve a runtime symbol wherever it lives.

        The re-export surface is not uniform -- some kernel names never
        reach the `cctally` namespace -- and a hard-coded module would make
        this fixture fragile in a way that has nothing to do with the
        documentation it checks.
        """
        if name in ns:
            return ns[name]
        for mod in ("_cctally_core", "_cctally_config", "_cctally_update"):
            module = sys.modules.get(mod)
            if module is None:
                module = importlib.import_module(mod)
            if hasattr(module, name):
                return getattr(module, name)
        raise AssertionError(f"cannot resolve runtime symbol {name!r}")

    import _lib_artifact_retention

    alerts = sym("_get_alerts_config")({})
    budget = sym("_get_budget_config")({})
    codex = sym("_validate_codex_budget_block")({"amount_usd": 1})
    retention = _lib_artifact_retention.default_policy_block()
    out = {
        "alerts.enabled": alerts["enabled"],
        "alerts.projected_enabled": alerts["projected_enabled"],
        "alerts.notifier": alerts["notifier"],
        "alerts.command_template": alerts["command_template"],
        "budget.weekly_usd": budget["weekly_usd"],
        "budget.alerts_enabled": budget["alerts_enabled"],
        "budget.alert_thresholds": budget["alert_thresholds"],
        "budget.projected_enabled": budget["projected_enabled"],
        "budget.period": budget["period"],
        "budget.projects": budget["projects"],
        "budget.project_alerts_enabled": budget["project_alerts_enabled"],
        "budget.accounts": budget["accounts"],
        "budget.codex": budget["codex"],
        "budget.codex.period": codex["period"],
        "budget.codex.alerts_enabled": codex["alerts_enabled"],
        "budget.codex.alert_thresholds": codex["alert_thresholds"],
        "budget.codex.projected_enabled": codex["projected_enabled"],
        "update.check.enabled": sym("_config_known_value")(
            {}, "update.check.enabled"
        ),
        "update.check.ttl_hours": sym("_config_known_value")(
            {}, "update.check.ttl_hours"
        ),
        "update.channel": sym("resolve_update_channel")({}),
        "conversation.retention_days": sym("_DEFAULT_CONVERSATION_RETENTION_DAYS"),
        "codex.hook.ingest_budget_seconds": sym(
            "CODEX_HOOK_INGEST_BUDGET_DEFAULT_SECONDS"
        ),
        "storage.artifact_retention": retention,
    }
    return {k: _render_default(v) for k, v in out.items()}


#: Keys whose Notes cell must cross-link to the family page that owns them,
#: rather than the contract page restating that page's rules.
FAMILY_LINKS = {
    "alerts.quota": "codex-quota.md",
    "statusline.visual_burn_rate": "statusline.md",
    "statusline.cost_source": "statusline.md",
    "statusline.cctally_extensions": "statusline.md",
    "statusline.usage_only": "statusline.md",
    "budget.weekly_usd": "budget.md",
    "budget.alerts_enabled": "budget.md",
    "budget.alert_thresholds": "budget.md",
    "budget.projected_enabled": "budget.md",
    "budget.period": "budget.md",
    "budget.projects": "budget.md",
    "budget.project_alerts_enabled": "budget.md",
    "budget.accounts": "account.md",
    "budget.codex.accounts": "account.md",
}


# --- The checks -----------------------------------------------------------

def test_header_row_is_exact(allowed_keys_table):
    assert allowed_keys_table["header"] == HEADER_ROW


def test_every_row_has_five_cells(allowed_keys_table):
    bad = [r for r in allowed_keys_table["rows"] if len(r) != len(HEADER_ROW)]
    assert bad == []


def _keys(table):
    return [row[0].strip("`") for row in table["rows"]]


def test_documented_keys_match_the_registry_in_order(allowed_keys_table, runtime):
    """Order is asserted because the tuple order is the page's organizing
    principle, and a silent append in the wrong place is exactly the drift
    this check exists to catch."""
    documented = _keys(allowed_keys_table)
    expected = list(runtime["ns"]["ALLOWED_CONFIG_KEYS"])
    missing = [k for k in expected if k not in documented]
    extra = [k for k in documented if k not in expected]
    assert missing == [], f"undocumented config keys: {missing}"
    assert extra == [], f"documented keys that are not in the registry: {extra}"
    assert len(documented) == len(set(documented)), "duplicate rows"
    assert documented == expected, "row order does not follow ALLOWED_CONFIG_KEYS"


def test_key_cells_are_code_spans(allowed_keys_table):
    for row in allowed_keys_table["rows"]:
        assert row[0].startswith("`") and row[0].endswith("`"), row[0]


def test_values_and_default_cells_are_non_empty(allowed_keys_table):
    for row in allowed_keys_table["rows"]:
        assert row[1].strip(), f"empty Values cell for {row[0]}"
        assert row[2].strip(), f"empty Default cell for {row[0]}"


def test_dashboard_writable_column_matches_the_endpoint(
    allowed_keys_table, runtime
):
    contract = runtime["contract"]
    expected_by_disposition = {
        contract.WRITABLE: "Yes",
        contract.KNOWN_IGNORED: "Ignored",
        None: "No",
    }
    mismatches = []
    for row in allowed_keys_table["rows"]:
        key = row[0].strip("`")
        want = expected_by_disposition[contract.disposition_for(key)]
        if row[3].strip() != want:
            mismatches.append((key, row[3].strip(), want))
    assert mismatches == [], mismatches


def test_dashboard_writable_column_uses_only_the_three_states(allowed_keys_table):
    states = {row[3].strip() for row in allowed_keys_table["rows"]}
    assert states <= {"Yes", "Ignored", "No"}, states


def test_defaults_match_the_runtime_where_obtainable(
    allowed_keys_table, mechanical_defaults
):
    """A non-empty Default cell is not the same as a correct one, so every
    default the code can hand over is compared against the cell."""
    mismatches = []
    covered = 0
    for row in allowed_keys_table["rows"]:
        key = row[0].strip("`")
        if key not in mechanical_defaults:
            continue
        covered += 1
        cell = row[2].strip().strip("`")
        if cell != mechanical_defaults[key]:
            mismatches.append((key, cell, mechanical_defaults[key]))
    assert mismatches == [], mismatches
    assert covered == len(mechanical_defaults), (
        "a key with a mechanically obtainable default is missing from the table"
    )


def test_family_pages_are_linked_rather_than_restated(allowed_keys_table):
    missing = []
    for row in allowed_keys_table["rows"]:
        key = row[0].strip("`")
        notes = row[4]
        if not notes.strip():
            if key in FAMILY_LINKS:
                missing.append((key, "empty Notes"))
            continue
        target = FAMILY_LINKS.get(key)
        if target is not None and target not in notes:
            missing.append((key, f"Notes does not link to {target}"))
    assert missing == [], missing


def test_every_row_has_notes(allowed_keys_table):
    empty = [row[0] for row in allowed_keys_table["rows"] if not row[4].strip()]
    assert empty == [], f"rows with an empty Notes cell: {empty}"


# --- Canaries: the parser must not accept table-shaped prose --------------

def test_pipe_shaped_prose_is_not_read_as_a_table():
    blocks = _parse_blocks(
        "## Allowed keys\n\n"
        "Write `a | b` when you mean either one, and | never | starts a row.\n"
    )
    assert [b["kind"] for b in blocks] == ["heading", "paragraph"]


def test_a_table_without_its_delimiter_row_is_not_a_table():
    blocks = _parse_blocks(
        "## Allowed keys\n\n"
        "| Key | Values |\n"
        "| `display.tz` | local |\n"
    )
    assert not any(b["kind"] == "table" for b in blocks)


def test_an_html_comment_does_not_contribute_rows():
    blocks = _parse_blocks(
        "## Allowed keys\n\n"
        "| Key | Values |\n"
        "|---|---|\n"
        "| `display.tz` | local |\n"
        "<!-- | `fake.key` | smuggled | -->\n"
    )
    tables = [b for b in blocks if b["kind"] == "table"]
    assert len(tables) == 1
    assert [r[0] for r in tables[0]["rows"]] == ["`display.tz`"]


def test_the_fixture_rejects_a_wrong_header_row():
    """The real check would pass on any five-column table, so prove the
    header assertion actually discriminates."""
    blocks = _parse_blocks(
        "## Allowed keys\n\n"
        "| Key | Values | Default | Writable | Notes |\n"
        "|---|---|---|---|---|\n"
        "| `display.tz` | local | `local` | Yes | - |\n"
    )
    table = [b for b in blocks if b["kind"] == "table"][0]
    assert table["header"] != HEADER_ROW
