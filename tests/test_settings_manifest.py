"""Every allowed configuration key has exactly one on-screen disposition.

#513 S2 §2.4. The audit's finding was not that some keys were hard to reach —
it was that the surface could not say what it does with them. A key was either
editable, or absent with no statement anywhere. This test makes the partition a
checked property instead of a claim.

It imports BOTH runtime sources directly rather than restating them, which is
what makes it non-vacuous: ``ALLOWED_CONFIG_KEYS`` is the CLI's own allowlist
and ``SETTINGS_LEAF_DISPOSITIONS`` is the dashboard endpoint's own contract, so
adding a key to either without giving it a rendered row fails here. It follows
the pattern ``tests/test_config_documentation.py`` already establishes.

The manifest itself is TypeScript, so it is parsed out of the source. Parsing
is deliberately strict: an entry that does not match the expected shape is a
failure, never a silently skipped row, because a lenient parser would turn "the
manifest lost half its entries" into "the test found nothing to check".
"""
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

MANIFEST_TS = REPO / "dashboard/web/src/components/settings/manifest.ts"

VALID_DISPOSITIONS = {"editable", "readOnly", "disclosed", "cliOnly"}
VALID_SECTIONS = {
    "display", "sessions", "alerts", "viewer", "access", "restore", "cli",
}


def _entry_bodies(source: str) -> list[str]:
    """Split the manifest array into one text block per entry."""
    start = source.index("export const SETTINGS_MANIFEST")
    end = source.index("\n];", start)
    body = source[start:end]
    # Entries open at a fixed indentation, which keeps nested object literals
    # inside a command string from being mistaken for entry boundaries.
    return [block for block in re.split(r"\n  \{\n", body)[1:]]


_STRING_PART = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")


def _field(block: str, name: str) -> "str | None":
    """The value of one manifest field, including values wrapped onto their
    own line or split across several concatenated string literals."""
    match = re.search(
        rf"^    {name}:\s*(.*?)(?=^    [A-Za-z]+:|\Z)", block, re.M | re.S,
    )
    if match is None:
        return None
    raw = match.group(1).strip().rstrip(",").strip()
    if raw.startswith(("'", '"')):
        parts = _STRING_PART.findall(raw)
        assert parts, f"{name} looked like a string but did not parse: {raw!r}"
        return "".join(
            (single or double).replace("\\'", "'").replace('\\"', '"')
            for single, double in parts
        )
    return raw


def load_settings_manifest() -> list[dict]:
    source = MANIFEST_TS.read_text()
    entries = []
    for block in _entry_bodies(source):
        key = _field(block, "key")
        if key is None:
            continue
        entry = {
            "key": key,
            "label": _field(block, "label"),
            "section": _field(block, "section"),
            "disposition": _field(block, "disposition"),
            "command": _field(block, "command"),
            "reason": _field(block, "reason"),
            "defaultText": _field(block, "defaultText"),
            "acceptedThenDiscarded": "acceptedThenDiscarded: true" in block,
        }
        entries.append(entry)
    return entries


@pytest.fixture(scope="module")
def manifest():
    entries = load_settings_manifest()
    # Non-vacuity: a parser that silently found nothing would make every
    # assertion below pass.
    assert len(entries) >= 30, f"parsed only {len(entries)} manifest entries"
    return entries


def test_every_allowed_key_has_exactly_one_rendered_disposition(manifest):
    from _cctally_config import ALLOWED_CONFIG_KEYS

    assert len(ALLOWED_CONFIG_KEYS) == 37
    for key in ALLOWED_CONFIG_KEYS:
        entries = [e for e in manifest if e["key"] == key]
        assert len(entries) == 1, f"{key} has {len(entries)} dispositions"
    extra = {e["key"] for e in manifest} - set(ALLOWED_CONFIG_KEYS)
    assert extra == set(), f"manifest names keys the CLI does not allow: {extra}"


def test_the_one_endpoint_writable_non_config_leaf_is_accounted_for_separately(manifest):
    from _cctally_config import ALLOWED_CONFIG_KEYS
    from _lib_dashboard_settings_contract import SETTINGS_LEAF_DISPOSITIONS

    extra = set(SETTINGS_LEAF_DISPOSITIONS) - set(ALLOWED_CONFIG_KEYS)
    assert extra == {"cache_report.anomaly_threshold_pp"}
    # It is edited in the cache-report popover, so it must NOT appear as a row
    # in this overlay's manifest.
    assert "cache_report.anomaly_threshold_pp" not in {e["key"] for e in manifest}
    assert "MAP_ONLY_LEAF = 'cache_report.anomaly_threshold_pp'" in MANIFEST_TS.read_text()


def test_the_partition_matches_the_specified_counts(manifest):
    counts: dict[str, int] = {}
    for entry in manifest:
        counts[entry["disposition"]] = counts.get(entry["disposition"], 0) + 1
    assert counts == {
        "editable": 13,
        "readOnly": 1,
        "disclosed": 3,
        "cliOnly": 20,
    }


def test_no_class_3_key_became_editable(manifest):
    """A key the endpoint cannot write must not render an editor.

    ``budget.period`` and the three CLI-only Codex leaves are ACCEPTED by the
    endpoint and dropped, so they are not writable either.
    """
    from _lib_dashboard_settings_contract import (
        SETTINGS_LEAF_DISPOSITIONS,
        WRITABLE,
    )

    for entry in manifest:
        if entry["disposition"] != "editable":
            continue
        assert SETTINGS_LEAF_DISPOSITIONS.get(entry["key"]) == WRITABLE, (
            f"{entry['key']} renders an editor but the endpoint cannot write it"
        )


def test_the_four_accepted_then_discarded_leaves_say_so(manifest):
    from _lib_dashboard_settings_contract import (
        KNOWN_IGNORED,
        SETTINGS_LEAF_DISPOSITIONS,
    )

    expected = {
        path
        for path, disposition in SETTINGS_LEAF_DISPOSITIONS.items()
        if disposition == KNOWN_IGNORED
    }
    assert len(expected) == 4
    marked = {e["key"] for e in manifest if e["acceptedThenDiscarded"]}
    assert marked == expected


def test_every_entry_states_a_section_a_default_and_a_command(manifest):
    for entry in manifest:
        assert entry["section"] in VALID_SECTIONS, entry
        assert entry["disposition"] in VALID_DISPOSITIONS, entry
        assert entry["defaultText"], entry
        assert entry["command"], entry
        if entry["disposition"] != "editable":
            assert entry["reason"], f"{entry['key']} gives no reason"


# The pinned command text, per key. Accepting any non-empty string would pass
# vacuously against a row that said "use the CLI" and nothing more, which is
# exactly the uselessness this row type exists to replace. Where a
# purpose-built wrapper exists, the wrapper is what the row must name.
PINNED_COMMANDS = {
    "telemetry.enabled": "cctally telemetry off",
    "budget.codex": "cctally budget set 200 --vendor codex",
    "budget.codex.amount_usd": "cctally budget set 200 --vendor codex",
    "budget.weekly_usd": "cctally budget set 200",
    "alerts.command_template":
        'cctally config set alerts.command_template \'["notify-send","{title}","{body}"]\'',
    "alerts.quota":
        'cctally config set alerts.quota \'{"enabled": true, "actual_thresholds": [90, 95], '
        '"projected_thresholds": [], "rules": []}\'',
    "budget.projects":
        'cctally config set budget.projects \'{"/Users/you/repos/cctally-dev": 50}\'',
    "budget.accounts": 'cctally config set budget.accounts \'{"work": 200}\'',
    "budget.codex.accounts": 'cctally config set budget.codex.accounts \'{"work": 150}\'',
    "budget.alerts_enabled": "cctally config set budget.alerts_enabled true",
    "update.check.enabled": "cctally config set update.check.enabled false",
    "update.check.ttl_hours": "cctally config set update.check.ttl_hours 24",
    "budget.alert_thresholds": "cctally config set budget.alert_thresholds 90,100",
    "dashboard.bind": "cctally config set dashboard.bind lan",
    "dashboard.expose_transcripts": "cctally config set dashboard.expose_transcripts true",
    "conversation.retention_days": "cctally config set conversation.retention_days 90",
    "codex.hook.ingest_budget_seconds":
        "cctally config set codex.hook.ingest_budget_seconds 5",
    "statusline.cost_source": "cctally config set statusline.cost_source cctally",
    "statusline.usage_only": "cctally config set statusline.usage_only true",
    "statusline.cctally_extensions":
        "cctally config set statusline.cctally_extensions false",
    "statusline.visual_burn_rate":
        "cctally config set statusline.visual_burn_rate emoji",
    "storage.artifact_retention":
        'cctally config set storage.artifact_retention \'{"max_age_days": 30, '
        '"max_count_per_family": 20, "max_total_mib": 4096, "min_free_mib": 10240, '
        '"max_shape_examples": 8}\'',
    "budget.period": "cctally config set budget.period calendar-month",
    "budget.codex.period": "cctally config set budget.codex.period calendar-month",
    "budget.codex.alert_thresholds":
        "cctally config set budget.codex.alert_thresholds 90,100",
}


def test_every_non_editable_row_pins_its_expected_command(manifest):
    by_key = {e["key"]: e for e in manifest}
    non_editable = {e["key"] for e in manifest if e["disposition"] != "editable"}
    # Every non-editable row is pinned. `budget.weekly_usd` is pinned too even
    # though it is editable, because its wrapper is what the CLI hint names.
    assert non_editable <= set(PINNED_COMMANDS), (
        f"unpinned non-editable rows: {sorted(non_editable - set(PINNED_COMMANDS))}"
    )
    for key, expected in PINNED_COMMANDS.items():
        assert by_key[key]["command"] == expected, key


# --- where a settings error lands -------------------------------------------
#
# `dashboard/web/src/components/settings/issues.ts` claims GROUP_OWNERS holds
# "every non-leaf path `_handle_post_settings` can name". That claim was false
# when it was written: `cache_report`, `update.check.enabled` and
# `update.check.ttl_hours` were all emitted by the handler and absent from the
# map, so an error naming one of them fell to form level with no statement
# anywhere that it was meant to. This makes the claim a checked property.

DASHBOARD_PY = REPO / "bin/_cctally_dashboard.py"
ISSUES_TS = REPO / "dashboard/web/src/components/settings/issues.ts"
REGISTRY_TS = REPO / "dashboard/web/src/components/settings/registry.ts"

#: The handler's own line span. Scoped rather than whole-file, because
#: `"field":` appears in other handlers with other vocabularies.
_HANDLER_START = "    def _handle_post_settings(self) -> None:"

#: `"field": "<literal>"`, including the `exc.field or "<literal>"` form. An
#: f-string value (`f"dashboard.{_leaf}"`) deliberately does NOT match: it names
#: no fixed path, and the leaf it interpolates is a registry path already.
_FIELD_LITERAL = re.compile(r'"field":\s*(?:[^,}\n]*?\bor\s+)?"([^"{}]+)"')


def _handler_source() -> str:
    lines = DASHBOARD_PY.read_text().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.rstrip("\n") == _HANDLER_START)
    end = next(
        i
        for i, line in enumerate(lines)
        if i > start and line.startswith("    def ")
    )
    return "".join(lines[start:end])


def _emitted_field_paths() -> set:
    return set(_FIELD_LITERAL.findall(_handler_source()))


def _registry_paths() -> set:
    return set(re.findall(r"^\s+path: '([^']+)',", REGISTRY_TS.read_text(), re.M))


def _group_owner_keys() -> set:
    source = ISSUES_TS.read_text()
    start = source.index("export const GROUP_OWNERS")
    body = source[start: source.index("\n};", start)]
    return set(re.findall(r"^\s+'?([A-Za-z_][\w.]*)'?:\s*(?:'[a-z]+'|null),$", body, re.M))


def test_every_field_pointer_the_endpoint_emits_has_a_declared_target():
    emitted = _emitted_field_paths()
    # Non-vacuity in three places: a scrape that found nothing, a registry that
    # parsed to nothing, and a map that parsed to nothing would each make the
    # assertion below pass while checking no path at all. Set well BELOW the
    # current counts on purpose — a floor tight enough to trip on one missing
    # entry would report "the parser broke" for what is really "a path has no
    # declared target", and the caller would fix the wrong thing.
    assert len(emitted) >= 10, f"scraped only {sorted(emitted)}"
    leaves = _registry_paths()
    assert len(leaves) >= 10, f"parsed only {sorted(leaves)} registry paths"
    owners = _group_owner_keys()
    assert len(owners) >= 5, f"parsed only {sorted(owners)} group owners"

    # `$` is the endpoint's own marker for "the body as a whole"; it names no
    # element and `resolveIssueTarget` answers form-level for it by design.
    undeclared = {path for path in emitted if path != "$"} - leaves - owners
    assert undeclared == set(), (
        "POST /api/settings can answer with these field pointers and the "
        f"client declares no target for them: {sorted(undeclared)}"
    )
    # And the scrape really did see both kinds, so a regex that had silently
    # stopped matching one of them could not pass this.
    assert emitted & leaves, "no leaf pointer was scraped"
    assert emitted & owners, "no ancestor pointer was scraped"


def test_the_map_states_a_deliberate_form_level_owner_rather_than_omitting_it():
    """`cache_report` is endpoint-writable and rendered nowhere in the overlay.

    Declared `null` rather than left out, so the exhaustiveness check above can
    tell "deliberately form-level" from "nobody looked at this path".
    """
    source = ISSUES_TS.read_text()
    assert "cache_report" in _group_owner_keys()
    assert re.search(r"^\s+cache_report: null,$", source, re.M), source
    assert "Object.hasOwn(GROUP_OWNERS, field)" in source


def test_a_json_valued_key_shows_a_real_object_example(manifest):
    by_key = {e["key"]: e for e in manifest}
    for key in ("alerts.quota", "budget.projects", "budget.accounts"):
        command = by_key[key]["command"]
        start = command.index("'")
        payload = command[start + 1: command.rindex("'")]
        parsed = json.loads(payload)
        assert isinstance(parsed, dict) and parsed, f"{key} example is not a real object"
