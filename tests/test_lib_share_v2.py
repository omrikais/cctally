"""Tests for v2 kernel additions: KERNEL_VERSION, _data_digest, _render_fragment, compose."""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

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
    out = _LS.render(snap, format="html", theme="light", branding=True)
    assert out.startswith("<!DOCTYPE")
    assert out.rstrip().endswith("</html>")


def test_render_dispatch_still_produces_v1_compatible_svg():
    snap = _trivial_snapshot()
    out = _LS.render(snap, format="svg", theme="light", branding=True)
    assert out.lstrip().startswith("<svg")
    assert out.rstrip().endswith("</svg>")


def test_md_frontmatter_byte_stable_for_identical_input():
    snap = _trivial_snapshot()
    out_a = _LS.render(snap, format="md", theme="light", branding=True)
    out_b = _LS.render(snap, format="md", theme="light", branding=True)
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
    out = _LS.render(snap, format="md", theme="light", branding=True)
    assert "template_id: weekly-visual\n" in out


def test_md_frontmatter_stripped_when_no_branding():
    snap = _trivial_snapshot()
    with_brand = _LS.render(snap, format="md", theme="light", branding=True)
    without = _LS.render(snap, format="md", theme="light", branding=False)
    assert with_brand.startswith("---\n")
    assert not without.startswith("---\n"), (
        "frontmatter should be stripped by --no-branding "
        "(spec §11.5 — same surface as the HTML/SVG footer link)"
    )


def _project_snapshot():
    """A snapshot with two ProjectCell rows so `_scrub` produces project-N labels.

    `_snapshot_is_anonymized` is a label-prefix predicate over the
    ProjectCell rows in `snap.rows` — a snapshot with zero project cells
    is reported as not anonymized (nothing to anonymize), so testing the
    anonymized field requires real project labels.
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
    scrubbed = _LS._scrub(snap, reveal_projects=False)
    out_reveal = _LS.render(snap, format="md", theme="light", branding=True)
    out_anon = _LS.render(scrubbed, format="md", theme="light", branding=True)
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


def test_compose_no_branding_strips_md_frontmatter():
    sections = (_make_section(title="A"),)
    opts = _LS.ComposeOptions(
        title="Combined", theme="light", format="md",
        no_branding=True, reveal_projects=True,
    )
    out = _LS.compose(sections, opts=opts)
    assert not out.startswith("---\n"), (
        "no_branding must strip composite frontmatter (spec §11.5)"
    )


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
    out = _LS.render(snap, format="html", theme="light", branding=True)
    assert "@media print" in out
    assert "color-scheme: light" in out
    assert "page-break-inside: avoid" in out


def test_print_stylesheet_unaffected_by_no_branding():
    """The print stylesheet is functional CSS, not branding — keep it
    under --no-branding (which only strips footer-link / frontmatter
    branding, not document-level CSS rules)."""
    snap = _trivial_snapshot()
    out = _LS.render(snap, format="html", theme="light", branding=False)
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
