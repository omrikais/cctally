"""Tests for v2 kernel additions: KERNEL_VERSION, _data_digest, _render_fragment, compose."""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Reuse an already-loaded `_lib_share` if `tests/test_lib_share.py` (or any
# other peer) registered one — otherwise pytest's shared sys.modules table
# would end up holding TWO distinct module objects under the same key, and
# `_lib_share.PercentCell` identity would diverge across files. Importing
# `bin/cctally` (which the v1 test does) caches its own `_lib_share` ref at
# import time, so the LAST loader wins for isinstance checks against module
# attributes that the cctally module also references.
_HERE = Path(__file__).resolve().parent
if "_lib_share" in sys.modules:
    _LS = sys.modules["_lib_share"]
else:
    _SPEC_PATH = _HERE.parent / "bin" / "_lib_share.py"
    _SPEC = importlib.util.spec_from_file_location("_lib_share", _SPEC_PATH)
    _LS = importlib.util.module_from_spec(_SPEC)
    sys.modules["_lib_share"] = _LS
    _SPEC.loader.exec_module(_LS)


def test_kernel_version_is_int_geq_1():
    assert isinstance(_LS.KERNEL_VERSION, int)
    assert _LS.KERNEL_VERSION >= 1


def test_data_digest_is_deterministic():
    payload = {"a": 1, "b": [2, 3], "c": "weekly"}
    d1 = _LS._data_digest(payload)
    d2 = _LS._data_digest(payload)
    assert d1 == d2
    assert d1.startswith("sha256:")
    assert len(d1) == len("sha256:") + 64  # hex sha256


def test_data_digest_key_order_independent():
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert _LS._data_digest(a) == _LS._data_digest(b)


def test_data_digest_changes_on_value_change():
    base = {"a": 1, "b": 2}
    mutated = {"a": 1, "b": 3}
    assert _LS._data_digest(base) != _LS._data_digest(mutated)


def _trivial_snapshot():
    return _LS.ShareSnapshot(
        cmd="weekly",
        title="Test",
        subtitle=None,
        period=_LS.PeriodSpec(
            start=datetime(2026, 5, 4, tzinfo=timezone.utc),
            end=datetime(2026, 5, 10, tzinfo=timezone.utc),
            display_tz="Etc/UTC",
            label="This week",
        ),
        columns=(),
        rows=(),
        chart=None,
        totals=(),
        notes=(),
        generated_at=datetime(2026, 5, 11, 9, 30, tzinfo=timezone.utc),
        version="1.5.0",
        template_id="weekly-recap",
    )


def test_render_fragment_html_has_no_document_chrome():
    snap = _trivial_snapshot()
    frag = _LS._render_fragment(snap, format="html",
                                 palette=_LS.PALETTE_LIGHT, branding=True)
    assert "<!DOCTYPE" not in frag
    assert "<html" not in frag
    assert "<body" not in frag


def test_render_fragment_svg_returns_inner_xml_and_dims():
    snap = _trivial_snapshot()
    inner, w, h = _LS._render_fragment(snap, format="svg",
                                        palette=_LS.PALETTE_LIGHT, branding=True)
    assert "<svg" not in inner          # NO outer <svg> wrapper
    assert isinstance(w, (int, float)) and w > 0
    assert isinstance(h, (int, float)) and h > 0


def test_render_dispatch_still_produces_v1_compatible_html():
    """v1 contract: render(format=html) returns a full document."""
    snap = _trivial_snapshot()
    out = _LS.render(snap, format="html", theme="light", branding=True, reveal_projects=True)
    assert out.startswith("<!DOCTYPE")
    assert out.rstrip().endswith("</html>")


def test_render_dispatch_still_produces_v1_compatible_svg():
    snap = _trivial_snapshot()
    out = _LS.render(snap, format="svg", theme="light", branding=True, reveal_projects=True)
    assert out.lstrip().startswith("<svg")
    assert out.rstrip().endswith("</svg>")


def test_md_frontmatter_byte_stable_for_identical_input():
    snap = _trivial_snapshot()
    out_a = _LS.render(snap, format="md", theme="light", branding=True, reveal_projects=True)
    out_b = _LS.render(snap, format="md", theme="light", branding=True, reveal_projects=True)
    assert out_a == out_b
    assert out_a.startswith("---\n")
    # Ordered keys per spec §11.5
    keys_in_order = ["title:", "generated_at:", "period:", "panel:",
                     "template_id:", "anonymized:", "cctally_version:"]
    prev_idx = -1
    for key in keys_in_order:
        idx = out_a.find(key)
        assert idx > 0, f"frontmatter missing key {key!r}"
        assert idx > prev_idx, f"frontmatter key {key!r} out of lexical order"
        prev_idx = idx


def test_md_frontmatter_includes_template_id_when_present():
    snap = replace(_trivial_snapshot(), template_id="weekly-visual")
    out = _LS.render(snap, format="md", theme="light", branding=True, reveal_projects=True)
    assert "template_id: weekly-visual\n" in out


# DELIBERATE REPLACEMENT (#503 S2 D2). The predecessor of this test
# asserted that `--no-branding` strips the WHOLE frontmatter, and
# justified it as consistency with the HTML/SVG footer-link strip. That
# justification was false: HTML loses exactly one `<footer>` and keeps its
# timestamp, and SVG does the same. Markdown alone lost the document's
# title, period, panel and privacy mode — the reader could no longer tell
# what the file covered. D2 splits the two: the advertisement goes, the
# provenance stays.
def test_no_branding_strips_the_advertisement_and_keeps_the_provenance():
    snap = _trivial_snapshot()
    md = _LS.render(snap, format="md", theme="light", branding=False,
                    reveal_projects=False)
    assert md.startswith("---\n")
    for kept in ("title:", "generated_at:", "period:", "panel:",
                 "template_id:", "anonymized:"):
        assert kept in md, kept
    assert "cctally_version:" not in md
    assert "Generated by [cctally]" not in md


def test_frontmatter_key_order_is_unchanged_and_version_is_last():
    md = _LS.render(_trivial_snapshot(), format="md", theme="light",
                    branding=True, reveal_projects=False)
    block = md.split("---\n")[1]
    keys = [line.split(":", 1)[0] for line in block.splitlines() if ":" in line]
    assert keys == ["title", "generated_at", "period", "panel",
                    "template_id", "anonymized", "cctally_version"]


def test_composed_markdown_uses_the_same_frontmatter_builder():
    sections = (_make_section(title="A"), _make_section(title="B"))
    out = _LS.compose(sections, opts=_LS.ComposeOptions(
        title="Combined", theme="light", format="md",
        no_branding=False, reveal_projects=True))
    block = out.split("---\n")[1]
    keys = [line.split(":", 1)[0] for line in block.splitlines() if ":" in line]
    assert keys == ["title", "generated_at", "period", "panel",
                    "anonymized", "cctally_version"]
    assert "panel: composed\n" in out
    assert "template_id:" not in block


def test_composed_markdown_no_branding_keeps_its_frontmatter():
    sections = (_make_section(title="A"), _make_section(title="B"))
    out = _LS.compose(sections, opts=_LS.ComposeOptions(
        title="Combined", theme="light", format="md",
        no_branding=True, reveal_projects=True))
    assert out.startswith("---\n")
    assert "panel: composed\n" in out
    assert "cctally_version:" not in out


def test_md_frontmatter_utc_uses_z_suffix(tmp_path):
    """Issue #37 — UTC `generated_at:` / `period:` must end with `Z`, not
    `+00:00`. Routes through `_format_generated_at_iso` to match spec §11.5
    and the SVG/HTML chrome (which already emits `Z`)."""
    snap = _trivial_snapshot()
    out = _LS.render(snap, format="md", theme="light", branding=True, reveal_projects=True)
    assert "generated_at: 2026-05-11T09:30:00Z\n" in out
    assert "period: 2026-05-04T00:00:00Z..2026-05-10T00:00:00Z\n" in out
    assert "+00:00" not in out


def _project_snapshot():
    """A snapshot with two ProjectCell rows so anonymization produces
    project-N labels.

    `render()`'s preparation pass rewrites typed project display fields, so a
    snapshot with zero project cells renders identically in both privacy modes
    apart from the frontmatter line. Testing the rendered labels therefore
    requires real project labels. (The `anonymized:` frontmatter field itself
    now reports the MODE — the label-prefix predicate `_snapshot_is_anonymized`
    that used to guess it was deleted in #503 S1.)
    """
    return _LS.ShareSnapshot(
        cmd="project",
        title="Project",
        subtitle=None,
        period=_LS.PeriodSpec(
            start=datetime(2026, 5, 4, tzinfo=timezone.utc),
            end=datetime(2026, 5, 10, tzinfo=timezone.utc),
            display_tz="Etc/UTC",
            label="This week",
        ),
        columns=(
            _LS.ColumnSpec(key="project", label="Project"),
            _LS.ColumnSpec(key="cost", label="$ Cost", align="right"),
        ),
        rows=(
            _LS.Row(cells={
                "project": _LS.ProjectCell("cctally-dev"),
                "cost": _LS.MoneyCell(0.50),
            }),
            _LS.Row(cells={
                "project": _LS.ProjectCell("other-repo"),
                "cost": _LS.MoneyCell(0.25),
            }),
        ),
        chart=None,
        totals=(),
        notes=(),
        generated_at=datetime(2026, 5, 11, 9, 30, tzinfo=timezone.utc),
        version="1.5.0",
    )


def test_md_frontmatter_anonymized_field_reflects_scrub():
    snap = _project_snapshot()
    out_reveal = _LS.render(snap, format="md", theme="light", branding=True, reveal_projects=True)
    out_anon = _LS.render(snap, format="md", theme="light", branding=True,
                          reveal_projects=False)
    assert "anonymized: false" in out_reveal
    assert "anonymized: true" in out_anon


# ---- M3.1 — compose() per-format stitching ---------------------------------


def _make_section(cmd: str = "weekly", title: str = "S"):
    """Build a minimal ComposedSection for stitch tests."""
    snap = _LS.ShareSnapshot(
        cmd=cmd, title=title, subtitle=None,
        period=_LS.PeriodSpec(
            start=datetime(2026, 5, 4, tzinfo=timezone.utc),
            end=datetime(2026, 5, 10, tzinfo=timezone.utc),
            display_tz="Etc/UTC", label="This week",
        ),
        columns=(), rows=(), chart=None, totals=(), notes=(),
        generated_at=datetime(2026, 5, 11, 9, 30, tzinfo=timezone.utc),
        version="1.5.0",
    )
    return _LS.ComposedSection(snap=snap, drift_detected=False)


def test_compose_html_single_wrapper_one_body_per_section():
    sections = (_make_section(title="A"), _make_section(title="B"))
    opts = _LS.ComposeOptions(
        title="Combined", theme="light", format="html",
        no_branding=False, reveal_projects=True,
    )
    out = _LS.compose(sections, opts=opts)
    # Exactly one document wrapper, two section blocks
    assert out.count("<!DOCTYPE") == 1
    assert out.count("<html") == 1
    assert out.count("</html>") == 1
    assert out.count('<section class="share-section"') == 2
    assert "Combined" in out


def test_compose_md_one_frontmatter_two_section_headers():
    sections = (_make_section(title="A"), _make_section(title="B"))
    opts = _LS.ComposeOptions(
        title="Combined", theme="light", format="md",
        no_branding=False, reveal_projects=True,
    )
    out = _LS.compose(sections, opts=opts)
    # Exactly one frontmatter block (---...---) at the top
    assert out.startswith("---\n"), "frontmatter must be first"
    assert out.count("\n---\n") == 1, (
        "expected exactly one closing --- delimiter; per-section "
        "frontmatter is forbidden by spec §4.3"
    )
    assert "## A" in out
    assert "## B" in out


def test_compose_svg_outer_viewBox_covers_total_height():
    sec_a = _make_section(title="A")
    sec_b = _make_section(title="B")
    opts = _LS.ComposeOptions(
        title="Combined", theme="light", format="svg",
        no_branding=False, reveal_projects=True,
    )
    out = _LS.compose((sec_a, sec_b), opts=opts)
    assert out.startswith("<svg")
    # Stacked vertically — two <g transform="translate(0,Y)"> wrappers
    assert out.count('<g transform="translate(0') == 2


# DELIBERATE REPLACEMENT (#503 S2 D2), the composed twin of
# `test_no_branding_strips_the_advertisement_and_keeps_the_provenance`
# above. The composed path had the same wrong rule as the single-section
# one and had to be replaced with it, not left behind.
def test_compose_no_branding_strips_only_the_version_from_the_frontmatter():
    sections = (_make_section(title="A"),)
    opts = _LS.ComposeOptions(
        title="Combined", theme="light", format="md",
        no_branding=True, reveal_projects=True,
    )
    out = _LS.compose(sections, opts=opts)
    assert out.startswith("---\n")
    for kept in ("title: Combined", "generated_at:", "period:",
                 "panel: composed", "anonymized:"):
        assert kept in out, kept
    assert "cctally_version:" not in out


def test_compose_md_escapes_composite_title_and_section_headings():
    """Codex P2 on PR #35 — `_stitch_md` previously emitted `opts.title`
    and `sec.snap.title` raw into H1/H2. Per single-section parity
    (`_render_md_body` at bin/_lib_share.py:915), composite headings
    must also route through `_md_escape` so MD/HTML specials in a
    user-entered title don't survive into the export unescaped."""
    sec = _LS.ComposedSection(
        snap=_LS.ShareSnapshot(
            cmd="weekly",
            title="Section<script>alert('x')</script>",
            subtitle=None,
            period=_LS.PeriodSpec(
                start=datetime(2026, 5, 4, tzinfo=timezone.utc),
                end=datetime(2026, 5, 10, tzinfo=timezone.utc),
                display_tz="Etc/UTC", label="This week",
            ),
            columns=(), rows=(), chart=None, totals=(), notes=(),
            generated_at=datetime(2026, 5, 11, 9, 30, tzinfo=timezone.utc),
            version="1.5.0",
        ),
        drift_detected=False,
    )
    opts = _LS.ComposeOptions(
        title='Composite "report" & <em>more</em>',
        theme="light", format="md",
        no_branding=False, reveal_projects=True,
    )
    out = _LS.compose((sec,), opts=opts)
    # The H1 line is the composite title — HTML chars must be escaped.
    # `_md_escape` covers &/</> but not quotes; the regression is HTML
    # specials surviving in markdown body, which is the privacy/safety
    # hole. (Frontmatter is a separate concern: it carries the title as
    # a YAML scalar, which is opaque to MD/HTML renderers.)
    assert '# Composite "report" &amp; &lt;em&gt;more&lt;/em&gt;' in out, (
        "composite title H1 must be _md_escape'd"
    )
    # The H2 section heading carries the user-entered section title —
    # must also be escaped so embedded HTML doesn't survive.
    assert "## Section&lt;script&gt;alert('x')&lt;/script&gt;" in out, (
        "section heading H2 must be _md_escape'd"
    )


def test_compose_md_frontmatter_utc_uses_z_suffix():
    """Issue #37 — composite-MD frontmatter must also emit `Z` for UTC
    datetimes, matching the single-section path."""
    sections = (_make_section(title="A"), _make_section(title="B"))
    opts = _LS.ComposeOptions(
        title="Combined", theme="light", format="md",
        no_branding=False, reveal_projects=True,
    )
    out = _LS.compose(sections, opts=opts)
    assert "generated_at: 2026-05-11T09:30:00Z\n" in out
    assert "period: 2026-05-04T00:00:00Z..2026-05-10T00:00:00Z\n" in out
    assert "+00:00" not in out
    # Body (post-frontmatter) must not carry raw HTML.
    body_only = out.split("\n---\n\n", 1)[-1]
    assert "<em>more</em>" not in body_only, (
        "raw <em> must not survive into MD body H1"
    )
    assert "<script>" not in body_only, (
        "raw <script> must not survive into MD body H2"
    )


def test_compose_per_section_drift_flag_does_not_change_body():
    """drift_detected is a metadata flag; it must not alter the rendered body."""
    a = _LS.ComposedSection(snap=_make_section().snap, drift_detected=False)
    b = _LS.ComposedSection(snap=_make_section().snap, drift_detected=True)
    opts = _LS.ComposeOptions(title="C", theme="light", format="html",
                              no_branding=False, reveal_projects=True)
    out_a = _LS.compose((a,), opts=opts)
    out_b = _LS.compose((b,), opts=opts)
    assert out_a == out_b, "drift_detected must not change rendered output"


# --- M4.2: print stylesheet injection (spec §11.2) ---

def test_html_output_carries_print_stylesheet():
    """HTML render() must inject `_print_stylesheet()` into <head> so
    Print → PDF on a dark-theme export prints as black-on-white instead
    of a solid-black page."""
    snap = _trivial_snapshot()
    out = _LS.render(snap, format="html", theme="light", branding=True, reveal_projects=True)
    assert "@media print" in out
    assert "color-scheme: light" in out
    assert "page-break-inside: avoid" in out


def test_print_stylesheet_unaffected_by_no_branding():
    """The print stylesheet is functional CSS, not branding — keep it
    under --no-branding (which only strips footer-link / frontmatter
    branding, not document-level CSS rules)."""
    snap = _trivial_snapshot()
    out = _LS.render(snap, format="html", theme="light", branding=False, reveal_projects=True)
    assert "@media print" in out


def test_compose_html_carries_print_stylesheet():
    """`_stitch_html` must inject the same print stylesheet so multi-
    section composed reports also print cleanly."""
    sections = (_make_section(title="A"), _make_section(title="B"))
    opts = _LS.ComposeOptions(
        title="Combined", theme="dark", format="html",
        no_branding=False, reveal_projects=True,
    )
    out = _LS.compose(sections, opts=opts)
    assert "@media print" in out
    assert "color-scheme: light" in out


# ---- #503 S1 F4 — one alias namespace across a composed document ----------


def _project_section(pairs, *, title="S", cmd="weekly"):
    """A section whose rows are (project label, cost) pairs."""
    snap = _LS.ShareSnapshot(
        cmd=cmd, title=title, subtitle=None,
        period=_LS.PeriodSpec(
            start=datetime(2026, 5, 4, tzinfo=timezone.utc),
            end=datetime(2026, 5, 10, tzinfo=timezone.utc),
            display_tz="Etc/UTC", label="This week",
        ),
        columns=(
            _LS.ColumnSpec(key="project", label="Project", align="left"),
            _LS.ColumnSpec(key="cost", label="$ Cost", align="right"),
        ),
        rows=tuple(
            _LS.Row(cells={
                "project": _LS.ProjectCell(label=label),
                "cost": _LS.MoneyCell(cost),
            })
            for label, cost in pairs
        ),
        chart=None, totals=(), notes=(),
        generated_at=datetime(2026, 5, 11, 9, 30, tzinfo=timezone.utc),
        version="1.5.0",
    )
    return _LS.ComposedSection(snap=snap, drift_detected=False)


def _sections_sharing_a_project():
    """Two sections, four rows, three distinct projects — `shared` appears in
    both and must resolve to ONE alias across the composed document."""
    return (
        _project_section([("shared", 5.12), ("weekly-only", 9.40)], title="Weekly"),
        _project_section([("shared", 5.12), ("daily-only", 1.10)], title="Daily"),
    )


def _alias_for_cost(section_body: str, cost: float) -> str:
    needle = f"${cost:,.2f}"
    for line in section_body.splitlines():
        if needle in line and line.startswith("|"):
            return line.split("|")[1].strip()
    raise AssertionError(f"no row carrying {needle} in:\n{section_body}")


def _split_sections(body: str):
    head, _, rest = body.partition("## Weekly")
    weekly, _, daily = rest.partition("## Daily")
    return weekly, daily


def test_composed_document_uses_one_alias_namespace():
    """project-1 must denote the same project in every section."""
    body = _LS.compose(_sections_sharing_a_project(),
                       opts=_LS.ComposeOptions(
                           title="Combined", theme="light", format="md",
                           no_branding=False, reveal_projects=False))
    weekly, daily = _split_sections(body)
    assert _alias_for_cost(weekly, 5.12) == _alias_for_cost(daily, 5.12)


@pytest.mark.parametrize("format", ("md", "html", "svg"))
@pytest.mark.parametrize("no_branding", (False, True))
def test_anonymized_composite_discloses_its_alias_scope(format, no_branding):
    body = _LS.compose(
        _sections_sharing_a_project(),
        opts=_LS.ComposeOptions(
            title="Combined", theme="light", format=format,
            no_branding=no_branding, reveal_projects=False,
        ),
    )
    assert "Project aliases are shared across sections." in body


@pytest.mark.parametrize("format", ("md", "html", "svg"))
def test_revealed_composite_does_not_claim_to_use_aliases(format):
    body = _LS.compose(
        _sections_sharing_a_project(),
        opts=_LS.ComposeOptions(
            title="Combined", theme="light", format=format,
            no_branding=False, reveal_projects=True,
        ),
    )
    assert "Project aliases are shared across sections." not in body


def test_merged_mapping_sums_costs_across_sections():
    mapping = _LS._merged_anon_mapping(_sections_sharing_a_project())
    assert len(mapping) == 3  # not 4 — the shared project appears once


def test_merged_mapping_ranks_on_the_summed_cost_not_a_single_section():
    """Summing rather than overwriting is what makes the global rank right:
    `shared` totals 10.24 across the two sections and outranks `weekly-only`,
    which wins in its own section alone."""
    mapping = _LS._merged_anon_mapping(_sections_sharing_a_project())
    assert mapping[("legacy", "shared")] == "project-1"
    assert mapping[("legacy", "weekly-only")] == "project-2"


def test_compose_keeps_provider_qualified_identities_distinct():
    """The same directory under Claude and under Codex stays two aliases."""
    a = _project_section([("app", 5.0)], title="Weekly")
    b = _project_section([("app", 4.0)], title="Daily")
    b = _LS.ComposedSection(
        snap=replace(b.snap, rows=(
            _LS.Row(cells={
                "project": _LS.ProjectCell(label="app", identity="codex:app"),
                "cost": _LS.MoneyCell(4.0),
            }),
        )),
        drift_detected=False,
    )
    mapping = _LS._merged_anon_mapping((a, b))
    assert len(mapping) == 2
    assert len(set(mapping.values())) == 2


def test_preparation_rewrites_project_labels_and_leaves_the_period_alone():
    """`_prepare` is the anonymization step, and this states its blast
    radius: it rewrites project labels and touches the period not at all.

    It used to be named for the drift digest, but it cannot observe that
    property. Under version 2 the digest reads `rows` — which this test
    itself shows preparation rewrites — so "preparation does not move the
    digest" is now a property of ORDER in the dashboard handlers, not of
    this kernel function: the digest hashes the snapshots the BUILDERS
    produced, before `render()` / `compose()` prepare them (#503 S3 §4).
    That property is pinned where it lives, by the `reveal_projects` leg of
    `tests/test_dashboard_source_share.py::
    test_share_digest_ignores_every_render_only_knob_across_the_registry`.
    """
    section = _sections_sharing_a_project()[0]
    prepared = _LS._prepare(section.snap, reveal_projects=False)
    assert prepared.rows[0].cells["project"].label != \
        section.snap.rows[0].cells["project"].label
    assert prepared.period == section.snap.period


# ---- #503 S1 F5 — the anonymization claim reports the mode ----------------


def _snapshot_without_projects():
    return _make_section(title="No projects").snap


def _chart_only_snapshot():
    """The `sessions-visual` shape: empty rows and columns, plus a chart
    carrying the project. `_snapshot_is_anonymized` early-returned False on
    this and stamped `anonymized: false` onto a demonstrably scrubbed
    snapshot."""
    return replace(
        _make_section(title="Sessions visual").snap,
        columns=(), rows=(),
        chart=_LS.HorizontalBarChart(
            points=(_LS.ChartPoint(
                x_label="1", x_value=1.0, y_value=1.0,
                project_label="alpha", x_label_kind="project",
                x_label_prefix="1"),),
            x_label="$", cap=None,
        ),
    )


def test_anonymized_reports_mode_for_a_project_free_snapshot():
    out = _LS.render(_snapshot_without_projects(), format="md",
                     theme="light", branding=True, reveal_projects=False)
    assert "anonymized: true" in out


def test_anonymized_reports_mode_in_reveal_for_a_project_free_snapshot():
    out = _LS.render(_snapshot_without_projects(), format="md",
                     theme="light", branding=True, reveal_projects=True)
    assert "anonymized: false" in out


def test_chart_only_snapshot_reports_anonymized_true():
    out = _LS.render(_chart_only_snapshot(), format="md",
                     theme="light", branding=True, reveal_projects=False)
    assert "anonymized: true" in out


def test_a_project_literally_named_project_1_is_not_claimed_anonymized():
    """The old label-shape heuristic reported a real project named
    `project-1` as anonymized."""
    section = _project_section([("project-1", 5.0)])
    out = _LS.render(section.snap, format="md", theme="light", branding=True,
                     reveal_projects=True)
    assert "anonymized: false" in out


def test_composed_frontmatter_reports_the_composite_mode():
    out = _LS.compose(_sections_sharing_a_project(),
                      opts=_LS.ComposeOptions(
                          title="Combined", theme="light", format="md",
                          no_branding=False, reveal_projects=False))
    assert "anonymized: true" in out


def test_snapshot_is_anonymized_is_gone():
    assert not hasattr(_LS, "_snapshot_is_anonymized")


# =====================================================================
# #503 S2 Task 5 — heading levels and provider-qualified composed
# headings (F12, D6).
#
# `_stitch_md` emitted its own `## <title>` and then a fragment that
# opened with `# <title>`, so every composed section printed its title
# twice at two different ranks. The HTML stitcher had the inverted form of
# the same defect: a composite `<h1>` wrapping one `<h1>` per section.
# =====================================================================

def _s2_source_section(*, title="Token Reuse Report", source="claude",
                       source_label=None, availability="ok",
                       availability_reason=None):
    snap = _LS.ShareSnapshot(
        cmd="cache-report", title=title, subtitle=None,
        period=_LS.PeriodSpec(
            start=datetime(2026, 5, 4, tzinfo=timezone.utc),
            end=datetime(2026, 5, 10, tzinfo=timezone.utc),
            display_tz="Etc/UTC", label="This week"),
        columns=(), rows=(), chart=None, totals=(), notes=(),
        generated_at=datetime(2026, 5, 11, 9, 30, tzinfo=timezone.utc),
        version="1.5.0", source=source, source_label=source_label,
        availability=availability, availability_reason=availability_reason)
    return _LS.ComposedSection(snap=snap, drift_detected=False)


def _s2_compose(sections, *, fmt="md", theme="light", no_branding=False,
                title="Composite title"):
    return _LS.compose(sections, opts=_LS.ComposeOptions(
        title=title, theme=theme, format=fmt, no_branding=no_branding,
        reveal_projects=True))


def _s2_compose_two_sections(**kwargs):
    return _s2_compose((_make_section(title="A"), _make_section(title="B")),
                       **kwargs)


def _s2_compose_all_source(**kwargs):
    return _s2_compose((
        _s2_source_section(source="claude", source_label="Claude"),
        _s2_source_section(source="codex", source_label="Codex"),
    ), **kwargs)


def test_composed_markdown_has_one_h1_and_one_h2_per_section():
    out = _s2_compose_two_sections(fmt="md")
    assert [l for l in out.splitlines() if l.startswith("# ")] == [
        "# Composite title"]
    assert len([l for l in out.splitlines() if l.startswith("## ")]) == 2


def test_composed_markdown_states_each_section_title_once():
    """`_stitch_md` used to emit its own `## <title>` ahead of a fragment
    that opened with `# <title>`, so each section's title appeared twice
    at two ranks. Asserted on the HEADING LINES rather than on a count of
    the bare letter, which any unrelated title change would move."""
    out = _s2_compose((_make_section(title="Alpha"),
                       _make_section(title="Beta")), fmt="md")
    headings = [line for line in out.splitlines() if line.startswith("#")]
    assert headings == ["# Composite title", "## Alpha", "## Beta"]


def test_composed_html_does_not_nest_h1_inside_h1():
    out = _s2_compose_two_sections(fmt="html")
    assert out.count("<h1") == 1
    assert out.count("<h2") == 2


def test_standalone_fragment_headings_are_unchanged():
    md = _LS.render(_trivial_snapshot(), format="md", theme="light",
                    branding=True, reveal_projects=False)
    assert md.count("\n# ") == 1
    html = _LS.render(_trivial_snapshot(), format="html", theme="light",
                      branding=True, reveal_projects=False)
    assert html.count("<h1") == 1
    assert "<h2" not in html


def test_all_source_composition_qualifies_section_headings_by_provider():
    out = _s2_compose_all_source(fmt="md")
    assert "## Token Reuse Report — Claude" in out
    assert "## Token Reuse Report — Codex" in out
    assert "## Token Reuse Report\n" not in out
    # The provider line is now IN the heading, so the separate one goes.
    assert "**Claude**" not in out
    assert "**Codex**" not in out


def test_all_source_html_composition_qualifies_and_drops_the_provider_line():
    out = _s2_compose_all_source(fmt="html")
    assert "Token Reuse Report — Claude</h2>" in out
    assert "Token Reuse Report — Codex</h2>" in out
    assert ">Claude</div>" not in out
    assert ">Codex</div>" not in out


def test_provider_qualification_preserves_the_availability_text():
    """The provider line goes; `No data` and `Unavailable: …` must not."""
    out = _s2_compose((
        _s2_source_section(source="claude", source_label="Claude",
                           availability="empty"),
        _s2_source_section(source="codex", source_label="Codex",
                           availability="unavailable",
                           availability_reason="Source analytics are unavailable."),
    ), fmt="md")
    assert "No data" in out
    assert "Unavailable: Source analytics are unavailable." in out


def test_single_provider_composition_headings_are_not_qualified():
    out = _s2_compose_two_sections(fmt="md")
    assert "— Claude" not in out
    out_codex = _s2_compose((
        _s2_source_section(source="codex", source_label="Codex", title="One"),
        _s2_source_section(source="codex", source_label="Codex", title="Two"),
    ), fmt="md")
    assert "— Codex" not in out_codex
    assert "**Codex**" in out_codex


def test_composed_svg_is_not_provider_qualified():
    """D6 is Markdown and HTML only: in SVG the provider and the
    availability text share ONE text node, so suppressing the provider
    would also lose `No data`."""
    out = _s2_compose_all_source(fmt="svg")
    assert "— Claude" not in out
    assert ">Claude<" in out


# =====================================================================
# #503 S2 Task 6 — composed chrome (F13).
#
# The composite HTML `<h1>` was the ONLY text-bearing element in the whole
# document with no specified colour, so on a dark theme it resolved to the
# UA default and rendered black on #0b0f17. Composed SVG carried no
# composite title, no footer and no background at all, so `no_branding`
# was provably a no-op on it — branded and unbranded output were
# byte-identical.
# =====================================================================

def test_composed_html_title_carries_an_explicit_colour():
    out = _s2_compose_two_sections(fmt="html", theme="dark")
    header = out.split("<header>")[1].split("</header>")[0]
    assert "color:" in header, header


def test_composed_html_title_uses_the_dark_palette_foreground():
    dark = _s2_compose_two_sections(fmt="html", theme="dark")
    light = _s2_compose_two_sections(fmt="html", theme="light")
    assert f'color:{_LS.PALETTE_DARK["fg"]}' in dark.split("</header>")[0]
    assert f'color:{_LS.PALETTE_LIGHT["fg"]}' in light.split("</header>")[0]


def test_composed_svg_renders_the_composite_title_and_footer():
    out = _s2_compose_two_sections(fmt="svg", theme="dark")
    assert "Composite title" in out
    # #503 S2 review F7: the composed footer now carries the SAME
    # attribution as the standalone one, not the bare `cctally · composed`.
    assert "Generated by cctally · github.com/omrikais/cctally" in out


def test_composed_svg_honours_no_branding():
    """Today branded and unbranded composed SVG are byte-identical."""
    branded = _s2_compose_two_sections(fmt="svg", theme="dark",
                                       no_branding=False)
    bare = _s2_compose_two_sections(fmt="svg", theme="dark", no_branding=True)
    assert branded != bare
    assert "cctally · composed" not in bare
    # The title is chrome the flag must NOT remove — it is what names the
    # document, not what advertises the tool.
    assert "Composite title" in bare


def test_composed_svg_paints_its_section_gaps():
    out = _s2_compose_two_sections(fmt="svg", theme="dark")
    assert out.count(f'fill="{_LS.PALETTE_DARK["bg"]}"') >= 3


def test_composed_svg_height_covers_its_title_footer_and_sections():
    import re as _re
    out = _s2_compose_two_sections(fmt="svg", theme="dark")
    height = float(_re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', out).group(1))
    inner_max = max(float(m) for m in
                    _re.findall(r'<text [^>]*\by="([\d.]+)"', out))
    # `y` inside a translated section group is section-local, so the check
    # that matters is the canvas rect: it must span the whole document.
    canvas_h = max(float(m) for m in _re.findall(
        r'<rect [^>]*\bheight="([\d.]+)"[^>]*/>', out))
    assert canvas_h == height
    assert inner_max <= height
    bare = _s2_compose_two_sections(fmt="svg", theme="dark", no_branding=True)
    bare_h = float(_re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', bare).group(1))
    assert bare_h < height


# =====================================================================
# #503 S2 Task 7 — the print stylesheet (F14).
#
# The shipped rule set `color: #000 !important` on `body` alone. `!important`
# resolves the cascade among declarations FOR ONE ELEMENT; it does not
# propagate through inheritance. Every descendant carries an inline
# `color`, so each has a specified value and never inherits — the dark
# palette reached print at a 1.24:1 contrast ratio. The embedded chart SVG
# was worse: it paints a full-canvas #0b0f17 rectangle and sets `fill` /
# `stroke` presentation attributes that no `body`-scoped rule can reach.
# =====================================================================

import json as _s2_json
import re as _s2_re

_S2_TPL_PATH = _HERE.parent / "bin" / "_lib_share_templates.py"
if "_lib_share_templates" in sys.modules:
    _S2_T = sys.modules["_lib_share_templates"]
else:
    _S2_TSPEC = importlib.util.spec_from_file_location(
        "_lib_share_templates", _S2_TPL_PATH)
    _S2_T = importlib.util.module_from_spec(_S2_TSPEC)
    sys.modules["_lib_share_templates"] = _S2_T
    _S2_TSPEC.loader.exec_module(_S2_T)

_S2_TOP_N = {"current-week": 3, "trend": 3, "weekly": 5, "daily": 5,
             "monthly": 5, "blocks": 3, "forecast": 5, "sessions": 15,
             "projects": 5}

# Same mirror-private gate as `tests/test_lib_share.py` — see the comment
# there. This file is public and the public suite runs it, so the one case
# that reads the private share-v2 fixture tree is gated on its presence.
# The reference is built from path segments rather than a single literal,
# which the Scope A2 scanner does not currently resolve; gating it anyway
# is the class fix, not the instance the scanner happened to catch.
import pytest as _s2_pytest
_s2_needs_fixtures = _s2_pytest.mark.skipif(
    not (_HERE / "fixtures" / "share-v2").is_dir(),
    reason="share-v2 fixture tree is mirror-private and absent on a public clone",
)

# The dark roles whose emitted value is a SURFACE or a STRUCTURE line, so
# printing it unchanged puts dark paint on paper. Data colours (the series
# palette, the two reference-line colours) are deliberately excluded: they
# are legible on white and carry meaning.
_S2_DARK_SURFACE_ROLES = ("bg", "table_header_bg", "table_row_alt", "grid",
                          "axis")


def _s2_template_dark_html(template_id: str) -> str:
    tpl = _S2_T.get_template(template_id)
    payload = _s2_json.loads(
        (_HERE / "fixtures" / "share-v2" / tpl.panel / "panel_data.json")
        .read_text(encoding="utf-8"))
    for key in ("period_start", "period_end"):
        if isinstance(payload.get(key), str):
            payload[key] = datetime.fromisoformat(
                payload[key].replace("Z", "+00:00"))
    options = {"format": "html", "theme": "dark", "reveal_projects": False,
               "no_branding": False, "top_n": _S2_TOP_N[tpl.panel],
               "show_chart": True, "show_table": True, "period": None,
               "project_allowlist": None, "display_tz": "Etc/UTC"}
    for key, value in tpl.default_options.items():
        if key not in ("reveal_projects", "theme", "no_branding"):
            options[key] = value
    return _LS.render(tpl.builder(panel_data=payload, options=options),
                      format="html", theme="dark", branding=True,
                      reveal_projects=False)


def _s2_emitted_dark_paint() -> "dict[str, set[str]]":
    """`{'fill': {...}, 'stroke': {...}}` over EVERY dark template render.

    Measured from real output rather than read off the palette, so a role
    the renderers stopped emitting does not inflate the requirement and a
    role they started emitting is not missed.
    """
    found = {"fill": set(), "stroke": set()}
    for tpl in _S2_T.SHARE_TEMPLATES:
        out = _s2_template_dark_html(tpl.id)
        found["fill"] |= set(_s2_re.findall(r'fill="(#[0-9a-fA-F]{3,8})"', out))
        found["stroke"] |= set(
            _s2_re.findall(r'stroke="(#[0-9a-fA-F]{3,8})"', out))
    return found


def test_print_rules_override_the_inline_colour_on_every_descendant():
    css = _LS._print_stylesheet()
    assert "body * { color: #000 !important" in css.replace("  ", " ")


@_s2_needs_fixtures
def test_print_rules_cover_every_emitted_dark_surface_role():
    css = _LS._print_stylesheet()
    emitted = _s2_emitted_dark_paint()
    surfaces = {_LS.PALETTE_DARK[role] for role in _S2_DARK_SURFACE_ROLES}
    covered = (emitted["fill"] | emitted["stroke"]) & surfaces
    assert covered, "no dark surface role was emitted — the check is vacuous"
    for value in sorted(covered):
        assert value in css, (value, sorted(covered))


def test_print_rules_disambiguate_the_two_roles_sharing_one_dark_value():
    """`#1f2937` is BOTH `grid` and `table_row_alt`, whose light
    counterparts differ, so a value-to-value map cannot be written."""
    assert _LS.PALETTE_DARK["grid"] == _LS.PALETTE_DARK["table_row_alt"]
    assert _LS.PALETTE_LIGHT["grid"] != _LS.PALETTE_LIGHT["table_row_alt"]
    css = _LS._print_stylesheet()
    assert f'rect[fill="{_LS.PALETTE_DARK["table_row_alt"]}"]' in css
    assert f'line[stroke="{_LS.PALETTE_DARK["grid"]}"]' in css
    assert _LS.PALETTE_LIGHT["grid"] in css
    assert _LS.PALETTE_LIGHT["table_row_alt"] in css


def test_print_rules_recolour_svg_text_which_uses_fill_not_color():
    css = _LS._print_stylesheet()
    assert "svg text" in css
    assert "fill: #1a1a1a !important" in css


def test_print_rules_avoid_breaking_inside_semantic_blocks_both_ways():
    css = _LS._print_stylesheet()
    assert "break-inside: avoid" in css
    assert "page-break-inside: avoid" in css


def test_print_stylesheet_is_injected_on_both_paths():
    standalone = _LS.render(_trivial_snapshot(), format="html", theme="dark",
                            branding=True, reveal_projects=False)
    assert "@media print" in standalone
    assert "@media print" in _s2_compose_two_sections(fmt="html", theme="dark")


def test_print_stylesheet_is_a_single_style_element():
    """One replaced line per affected HTML golden, auditable mechanically."""
    css = _LS._print_stylesheet()
    assert css.count("<style>") == 1
    assert css.count("</style>") == 1
    assert "\n" not in css


# =====================================================================
# #503 S2 review — F5 / M3 / F8, on the composed document.
# =====================================================================

def _s2r_svg_roots(text: str) -> "list[str]":
    import re as _re
    return _re.findall(r"<svg\b[^>]*>", text)


def test_a_composed_svg_declares_one_font_family():
    """F5: only the SVG table cells carried `font-family`, so every other
    text element fell back to the viewer default (Times in Chromium)."""
    roots = _s2r_svg_roots(_s2_compose_two_sections(fmt="svg"))
    assert roots
    assert 'font-family="sans-serif"' in roots[0], roots[0][:160]


def test_a_composed_svg_caps_its_own_width():
    """M3: the outer composed SVG must scale down inside a narrow page
    the same way a standalone one does."""
    roots = _s2r_svg_roots(_s2_compose_two_sections(fmt="svg"))
    assert "max-width:100%" in roots[0], roots[0][:160]


def _s2r_charted_section(title="C"):
    section = _make_section(title=title)
    return _LS.ComposedSection(
        snap=replace(section.snap, chart=_LS.BarChart(
            points=(_LS.ChartPoint(x_label="2026-05-04", x_value=0.0,
                                   y_value=1.0),),
            y_label="$ / week")),
        drift_detected=section.drift_detected)


def test_a_composed_html_chart_keeps_its_size_inside_a_scroll_box():
    """The composed path gets the same mechanism as the standalone one.

    It used to get the same broken pairing instead: a scroll container
    around an element already capped to that container, which could
    never scroll and shrank the chart instead (#503 S2 second review N1).
    """
    out = _s2_compose((_s2r_charted_section("C"), _s2r_charted_section("D")),
                      fmt="html")
    for root in _s2r_svg_roots(out):
        assert "max-width" not in root, root[:160]
    assert out.count("overflow:auto") >= 2, out.count("overflow:auto")


# =====================================================================
# #503 S2 review — F1 / F2: the dark DATA colours never reached print.
#
# `_print_stylesheet` mapped the dark text, canvas, grid and table roles
# to their light counterparts and asserted in its own docstring that the
# data colours were "legible on white". Measured against white paper they
# are not: dark `ref_warn` #fbbf24 is 1.67:1 (its light counterpart
# #d97706 is 3.19:1), dark `ref_alarm` #f87171 is 2.77:1 (light #dc2626
# is 4.83:1), and dark `series_primary` #60a5fa is 2.54:1 (light #2563eb
# is 5.17:1). So a printed dark forecast showed its 90% ceiling line
# essentially invisible while the light artifact of the same report
# printed legibly — which is F14's own acceptance.
#
# F2 is the pairing: the blanket `svg text { fill: … }` flattened the
# reference LABELS to black while their `<line>` elements kept #fbbf24
# and #f87171, so the colour that encodes severity was broken in one
# direction only.
# =====================================================================

def _s2r_relative_luminance(hex_colour: str) -> float:
    channels = []
    for offset in (1, 3, 5):
        value = int(hex_colour[offset:offset + 2], 16) / 255.0
        channels.append(value / 12.92 if value <= 0.04045
                        else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _s2r_contrast_on_white(hex_colour: str) -> float:
    return 1.05 / (_s2r_relative_luminance(hex_colour) + 0.05)


_S2R_DARK_DATA_ROLES = ("ref_warn", "ref_alarm", "series_primary",
                        "series_secondary")
# 3:1 is the WCAG threshold for a non-text graphical object, which is
# what a reference line and a chart series are.
_S2R_PRINT_MIN_CONTRAST = 3.0


def test_the_dark_data_colours_are_not_legible_on_white_unmapped():
    """The premise, asserted so the fix below cannot become vacuous if
    the dark palette is ever re-picked."""
    offenders = [
        (role, _LS.PALETTE_DARK[role],
         round(_s2r_contrast_on_white(_LS.PALETTE_DARK[role]), 2))
        for role in _S2R_DARK_DATA_ROLES
        if _s2r_contrast_on_white(_LS.PALETTE_DARK[role])
        >= _S2R_PRINT_MIN_CONTRAST
    ]
    assert not offenders, (
        "these dark data colours now pass on white unmapped, so the print "
        f"mapping no longer needs them: {offenders}")


def test_every_light_counterpart_the_print_map_uses_is_legible_on_white():
    for role in _S2R_DARK_DATA_ROLES:
        ratio = _s2r_contrast_on_white(_LS.PALETTE_LIGHT[role])
        assert ratio >= _S2R_PRINT_MIN_CONTRAST, (role, round(ratio, 2))
    for index, value in enumerate(_LS.PALETTE_LIGHT["series_palette"]):
        ratio = _s2r_contrast_on_white(value)
        assert ratio >= _S2R_PRINT_MIN_CONTRAST, (index, value, round(ratio, 2))


def test_print_rules_map_every_dark_data_colour_to_its_light_counterpart():
    css = _LS._print_stylesheet()
    for role in _S2R_DARK_DATA_ROLES:
        dark = _LS.PALETTE_DARK[role]
        light = _LS.PALETTE_LIGHT[role]
        assert dark in css, (role, dark)
        assert light in css, (role, light)
    for dark, light in zip(_LS.PALETTE_DARK["series_palette"],
                           _LS.PALETTE_LIGHT["series_palette"]):
        assert dark in css, dark
        assert light in css, light


def test_a_reference_label_and_its_line_print_the_same_colour():
    """F2: today the label goes black and the line stays amber."""
    css = _LS._print_stylesheet()
    for role in ("ref_warn", "ref_alarm"):
        dark = _LS.PALETTE_DARK[role]
        light = _LS.PALETTE_LIGHT[role]
        assert f'text[fill="{dark}"] {{ fill: {light} !important; }}' in css, role
        assert f'line[stroke="{dark}"] {{ stroke: {light} !important; }}' in css, role


def test_the_light_artifact_prints_the_same_pairing_as_the_dark_one():
    """The map was keyed only on the dark values while the blanket
    `svg text` rule is theme-blind.

    So a LIGHT forecast printed a black `90%` above an amber reference
    line, where the dark forecast of the same report printed both amber
    — the same broken pairing F2 named, in the theme F2 did not look at
    (#503 S2 second review N7).
    """
    css = _LS._print_stylesheet()
    for role in ("ref_warn", "ref_alarm", "muted", "footer_link"):
        light = _LS.PALETTE_LIGHT[role]
        assert f'text[fill="{light}"] {{ fill: {light} !important; }}' in css, role


def test_a_data_colour_used_as_a_fill_prints_light_whatever_element_bears_it():
    """The map named `rect` for fills and `line`/`polyline`/`path` for
    strokes, so a filled `path` or `polyline` would have printed dark
    (#503 S2 second review N8). Latent today; closed rather than
    recorded, because the asymmetry is the bug."""
    css = _LS._print_stylesheet()
    dark = _LS.PALETTE_DARK["series_primary"]
    for element in ("rect", "path", "polyline"):
        assert f'svg {element}[fill="{dark}"]' in css, element


def test_the_two_palettes_index_the_same_number_of_series_colours():
    """`_print_stylesheet` and its own test both `zip` these two arrays,
    so a one-sided edit is silently truncated by both and the extra
    colour reaches print unmapped (#503 S2 second review N8)."""
    assert len(_LS.PALETTE_DARK["series_palette"]) == len(
        _LS.PALETTE_LIGHT["series_palette"])


@_s2_needs_fixtures
def test_print_rules_cover_every_emitted_dark_data_paint():
    """Measured from real dark renders, not read off the palette."""
    css = _LS._print_stylesheet()
    emitted = _s2_emitted_dark_paint()
    data_values = (
        {_LS.PALETTE_DARK[role] for role in _S2R_DARK_DATA_ROLES}
        | set(_LS.PALETTE_DARK["series_palette"])
    )
    covered = (emitted["fill"] | emitted["stroke"]) & data_values
    assert covered, "no dark data paint was emitted — the check is vacuous"
    for value in sorted(covered):
        assert value in css, (value, sorted(covered))


# =====================================================================
# #503 S2 review — F7 / F8: the composed document's own chrome.
# =====================================================================

def test_a_composed_document_carries_the_same_attribution_as_a_standalone():
    """F7: the composed footer read the bare `cctally · composed` while
    the standalone one names the project and the version, so the composed
    artifact dropped provenance the standalone keeps."""
    standalone = _LS.render(_trivial_snapshot(), format="html", theme="light",
                            branding=True, reveal_projects=False)
    composed = _s2_compose_two_sections(fmt="html")
    assert "github.com/omrikais/cctally" in standalone
    assert "github.com/omrikais/cctally" in composed
    assert "Generated by cctally" in composed
    assert "cctally · composed" not in composed


def test_a_composed_svg_carries_the_same_attribution_as_a_standalone():
    composed = _s2_compose_two_sections(fmt="svg")
    assert "Generated by cctally · github.com/omrikais/cctally" in composed


@_s2_pytest.mark.parametrize("fmt", ["html", "svg"])
def test_no_branding_removes_the_composed_advertisement_entirely(fmt):
    """D2's split is identical to the standalone one: for HTML and SVG
    the footer IS the advertisement, and the provenance a reader needs
    lives in each section's facts strip."""
    bare = _s2_compose_two_sections(fmt=fmt, no_branding=True)
    assert "Generated by cctally" not in bare
    assert "github.com/omrikais/cctally" not in bare
    assert "2026-05-04 → 2026-05-10 (Etc/UTC)" in bare


def _s2r_svg_viewbox_width(svg: str) -> float:
    import re as _re
    return float(_re.search(r'viewBox="0 0 ([\d.]+) ', svg).group(1))


def test_a_long_composite_svg_title_does_not_overflow_the_viewbox():
    """F8: `_stitch_svg` placed `opts.title` at 18pt against a width
    taken only from the section maximum, so a long title ran off."""
    title = ("Quarterly review of every project across both providers "
             "for the finance team")
    out = _s2_compose((_make_section(title="A"), _make_section(title="B")),
                      fmt="svg", title=title)
    view_w = _s2r_svg_viewbox_width(out)
    width = _LS._svg_text_width(title, 18.0)
    assert 20.0 + width <= view_w + 0.01, (width, view_w)


def test_no_composed_svg_text_extends_beyond_the_viewbox():
    """The standalone bounds sweep covered templates only."""
    for title in ("Composite title",
                  "Quarterly review of every project across both providers"):
        out = _s2_compose((_make_section(title="A"), _make_section(title="B")),
                          fmt="svg", title=title)
        view_w = _s2r_svg_viewbox_width(out)
        for raw_attrs, text in _s2_re.findall(
                r"<text ([^>]*)>([^<]*)</text>", out):
            attrs = dict(_s2_re.findall(r'([\w-]+)="([^"]*)"', raw_attrs))
            x = float(attrs["x"])
            size = float(attrs["font-size"])
            anchor = attrs.get("text-anchor", "start")
            width = _LS._svg_text_width(text, size)
            left = (x - width if anchor == "end"
                    else x - width / 2 if anchor == "middle" else x)
            assert left >= -0.01, (title, text, left)
            assert left + width <= view_w + 0.01, (title, text, left + width,
                                                   view_w)
