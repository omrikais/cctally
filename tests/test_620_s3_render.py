"""#620 S3 — what the terminal and the wire publish for the S3 classes (C12).

Three claims, each of which a golden alone cannot make:

  * every class rule and the turn predicate are published as structured wire
    fields AND rendered on the terminal, so a verdict can be reproduced rather
    than trusted;
  * each S3 row's evidence fields are VISIBLE beneath it, with a withheld
    member printing its cause rather than nothing — which would read as zero;
  * the `gapCodes` vocabulary is published, so a consumer can tell a code from
    a newer server apart from one it should have handled.

The asymmetry sentence of spec 5.4 is unreachable from the CLI, because the CLI
reads its own stores and is always authorized. It is exercised here over a
hand-built report, which is the only way to reach it on this surface.

The constant is NOT imported by the modal, and an earlier version of this
docstring said it existed "precisely so the modal states the same words", which
was false: a TypeScript module cannot read a Python constant, and the modal
carries its own sentence for `transcripts_not_visible`. What the two surfaces
share is the CLAIM, not the whole sentence, because the two sentences sit in
different positions — a footer under a withheld row on the terminal, a per-class
message in the modal — and the leading clause is written for its position.
`test_the_shared_transcript_asymmetry_clause_is_stated_in_the_same_words`
below is the gate that makes the shared half literally identical across all
three places that state it, which is what the docstring should have claimed.
"""
from __future__ import annotations

import datetime as dt
import importlib
import pathlib
import re
import sys

import pytest

import _lib_diagnosis as kernel
from conftest import load_script

UTC = dt.timezone.utc
WINDOW_START = dt.datetime(2026, 8, 10, tzinfo=UTC)
WINDOW_END = dt.datetime(2026, 8, 17, tzinfo=UTC)


def _diagnosis():
    load_script()
    return importlib.import_module("_cctally_diagnosis")


def _coverage(**over):
    values = {
        "requested_start": "2026-08-10T00:00:00Z",
        "requested_end": "2026-08-17T00:00:00Z",
        "support_units": 40,
        "usd_coverage": 1.0,
    }
    values.update(over)
    return kernel.PopulationCoverage(**values)


def _denominator(usd=100.0):
    population = _coverage()
    return kernel.Denominator(
        identity="totalExplainedRetainedCost", source="claude",
        account_key=None, window_start="2026-08-10T00:00:00Z",
        window_end="2026-08-17T00:00:00Z", population_digest="digest",
        usd=kernel.available(usd, population),
    )


def _s3_class(kind, *, evidence, subject_key, observed=40.0,
              qualifications=()):
    spec = kernel.spec_for(kind)
    population = _coverage()
    subject = kernel.SubjectFacts(
        subject_key=subject_key, subject_label="Qualifying set",
        observed_usd=observed, priced_entry_count=40,
        next_step="cctally explain --source claude "
                  "--start-at 2026-08-10T00:00:00Z "
                  "--end-at 2026-08-17T00:00:00Z --json",
        qualifications=tuple(qualifications),
    )
    return kernel.classify_class(
        spec, [subject], _denominator(), population=population,
        predicate_evaluated=True, evidence=evidence,
    )


def _report(classes):
    result = kernel.build_provider_result(
        source="claude", account_key=None, effective_speed=None,
        denominator=_denominator(), classes=classes,
        coverage=_coverage(),
    )
    return kernel.build_report(
        "2026-08-17T00:00:00Z",
        kernel.DiagnosisWindow("2026-08-10T00:00:00Z", "2026-08-17T00:00:00Z",
                               "Etc/UTC", ""),
        [result],
    )


# --- C12: the rules are on both surfaces --------------------------------

def test_every_registry_rule_is_rendered_on_the_terminal():
    diagnosis = _diagnosis()
    text = diagnosis.render_terminal(_report([]))
    published = [spec for spec in kernel.CONTRIBUTOR_REGISTRY
                 if getattr(spec, "rule", None) is not None]
    assert len(published) == 3, [s.kind for s in published]
    for spec in published:
        assert spec.rule.sentence in text, spec.kind
        assert spec.label in text, spec.kind
    assert kernel.DIAGNOSIS_TURN_DEFINITION_SENTENCE in text


def test_the_four_accounting_classes_publish_no_rule():
    """The registry wire shape of the S2 classes is byte-frozen, so a rule on
    one of them would move every S2 golden for no contract reason."""
    for kind in ("model_mix", "project_concentration",
                 "session_concentration", "five_hour_bursts"):
        assert kernel.spec_for(kind).rule is None, kind


def test_the_wire_publishes_every_rule_and_the_turn_definition():
    diagnosis = _diagnosis()
    wire = diagnosis.diagnosis_to_wire(_report([]))
    constants = wire["constants"]
    assert constants["turnDefinition"] == \
        kernel.DIAGNOSIS_TURN_DEFINITION_SENTENCE
    by_kind = {entry["contributorClass"]: entry
               for entry in constants["registry"]}
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        rule = by_kind[kind]["rule"]
        assert rule["sentence"] == kernel.spec_for(kind).rule.sentence
        assert rule["parameters"] == dict(kernel.spec_for(kind).rule.parameters)
    for kind in ("model_mix", "project_concentration",
                 "session_concentration", "five_hour_bursts"):
        assert "rule" not in by_kind[kind], kind


def test_the_published_rule_parameters_reproduce_the_published_constants():
    """A rule a consumer cannot reproduce is prose. The two turn-based rules
    name the same thresholds the constants block publishes, so a client can
    apply them rather than parse English."""
    diagnosis = _diagnosis()
    constants = diagnosis.diagnosis_to_wire(_report([]))["constants"]
    by_kind = {entry["contributorClass"]: entry
               for entry in constants["registry"]}
    short = by_kind["short_high_context"]["rule"]["parameters"]
    assert short["maxHumanTurns"] == constants["shortConversationMaxHumanTurns"]
    assert short["minWindowFraction"] == \
        constants["largeContextMinWindowFraction"]
    fanout = by_kind["subagent_fanout"]["rule"]["parameters"]
    assert fanout["minBuckets"] == constants["minSubagentBuckets"]


# --- the gap-code vocabulary is published -------------------------------

def test_the_gap_code_vocabulary_is_published_on_the_wire():
    diagnosis = _diagnosis()
    constants = diagnosis.diagnosis_to_wire(_report([]))["constants"]
    assert constants["gapCodes"] == sorted(kernel.GAP_CODES)
    # A literal count, so a code added to the kernel without a decision about
    # the wire fails here rather than shipping. #834 S2 (#800) added the fifth,
    # `no_retained_transcript`.
    assert len(constants["gapCodes"]) == 5


def test_no_published_gap_code_collides_with_a_withheld_cause():
    """The two vocabularies are read in the same places, and a member of both
    would be a code whose meaning depends on where it appeared."""
    causes = {cause.value for cause in kernel.WithheldCause}
    assert not (kernel.GAP_CODES & causes), kernel.GAP_CODES & causes


# --- the evidence is visible on the row ---------------------------------

def test_each_s3_row_renders_its_evidence_fields_beneath_it():
    diagnosis = _diagnosis()
    population = _coverage()
    classes = [
        _s3_class("cache_churn", subject_key=kernel.SUBJECT_CACHE_CHURN,
                  evidence={
                      "flaggedTurnCount": kernel.available(3, population),
                      "affectedConversationCount": kernel.available(
                          1, population),
                      "estWastedUsd": kernel.available(1.5, population),
                  }),
        _s3_class("subagent_fanout",
                  subject_key=kernel.SUBJECT_SUBAGENT_FANOUT, observed=30.0,
                  evidence={
                      "identifiedSubagentCount": kernel.available(
                          2, population,
                          (kernel.QUALIFICATION_IDENTIFIABLE_SUBSET,)),
                      "largestSubagentShare": kernel.available(0.75,
                                                               population),
                      "unallocatedUsd": kernel.available(2.25, population),
                  }),
    ]
    text = diagnosis.render_terminal(_report(classes))
    # The HUMAN label, not the published camelCase key: the terminal states
    # the same vocabulary the modal does, and the wire name stays the sort
    # key so the line reads in JSON order.
    assert "evidence: conversations affected 1" in text
    assert "estimated wasted cost $1.50" in text
    assert "turns that rebuilt their cache 3" in text
    assert "largest subagent as a share of this class 75.0%" in text
    assert "unallocated cost $2.25" in text
    assert (f"identified subagents 2 "
            f"[{kernel.QUALIFICATION_IDENTIFIABLE_SUBSET}]") in text


def test_a_withheld_evidence_member_prints_its_cause_and_never_a_zero():
    """A row may report its cost while one member figure could not be
    established. Printing nothing for that member reads as zero, which is the
    same defect the withheld denominator exists to prevent."""
    diagnosis = _diagnosis()
    population = _coverage()
    classes = [_s3_class(
        "short_high_context", subject_key=kernel.SUBJECT_SHORT_HIGH_CONTEXT,
        evidence={
            "conversationCount": kernel.available(2, population),
            "medianHumanTurns": kernel.withheld("insufficient_population",
                                                population),
            "maxContextWindowFraction": kernel.available(0.91, population),
        },
    )]
    text = diagnosis.render_terminal(_report(classes))
    assert "median human turns withheld (insufficient_population)" in text
    assert "largest request as a share of its context window 91.0%" in text
    assert "median human turns 0" not in text


def test_a_row_states_each_qualification_once_across_all_its_figures():
    """A row prints its qualifications once, not once per figure that carries
    them.

    `_build_row` already attaches the subject's qualifications to the observed
    cost ALONE, because attaching the same tuple to the share as well shipped
    `block_precedes_window` twice per row. The evidence members reopened the
    same hole from the other side: a Codex fan-out row printed
    `identifiable_subset_unknown_completeness` in its own parenthetical and
    again under `identified subagents`, with the class rule directly beneath
    both stating the same fact in English (#620 S3, browser round 1).

    It drops DUPLICATES, never qualifications: a mark the row did not state
    still prints on the figure that carries it.
    """
    diagnosis = _diagnosis()
    population = _coverage()
    subset = kernel.QUALIFICATION_IDENTIFIABLE_SUBSET
    other = kernel.QUALIFICATION_SESSION_LEVEL_CAPACITY
    classes = [_s3_class(
        "subagent_fanout", subject_key=kernel.SUBJECT_SUBAGENT_FANOUT,
        qualifications=(subset,),
        evidence={
            "identifiedSubagentCount": kernel.available(2, population,
                                                        (subset,)),
            "largestSubagentShare": kernel.available(0.5, population,
                                                     (other,)),
            "unallocatedUsd": kernel.available(0.0, population),
        },
    )]
    text = diagnosis.render_terminal(_report(classes))
    assert text.count(subset) == 1, text
    # The surviving one is the ROW's, which qualifies the observed cost the
    # row is ranked by.
    assert f"(confidence high, {subset})" in text
    assert f"identified subagents 2 [{subset}]" not in text
    assert f"largest subagent as a share of this class 50.0% [{other}]" in text


def test_the_wire_publishes_a_withheld_evidence_member_beside_available_ones():
    """One member withheld while its class reports a cost, on the WIRE.

    This is the state the dashboard renders, and the state no fixture reaches:
    every production path that withholds a member also yields a zero
    qualifying cost, so the class classifies as `no_contributor`, publishes no
    rows, and the evidence never leaves the adapter. The report is therefore
    built here, which is the only surface on which the pairing is reachable.

    What it pins is that the member stays withheld with its OWN cause and a
    null value beside available siblings — never a zero, and never absent,
    because the client renders a missing member as nothing at all.
    """
    diagnosis = _diagnosis()
    population = _coverage()
    classes = [_s3_class(
        "short_high_context", subject_key=kernel.SUBJECT_SHORT_HIGH_CONTEXT,
        evidence={
            "conversationCount": kernel.available(6, population),
            "medianHumanTurns": kernel.withheld("insufficient_population",
                                                population),
            "maxContextWindowFraction": kernel.available(0.91, population),
        },
    )]
    wire = diagnosis.diagnosis_to_wire(_report(classes))
    rows = [row for row in wire["results"][0]["contributors"]
            if row["contributorClass"] == "short_high_context"]
    assert len(rows) == 1, wire["results"][0]["contributors"]
    evidence = rows[0]["evidence"]
    assert evidence["medianHumanTurns"] == {
        "state": "withheld", "value": None, "code": "insufficient_population",
        "population": evidence["conversationCount"]["population"],
        "qualifications": [],
    }
    assert evidence["conversationCount"]["state"] == "available"
    assert evidence["conversationCount"]["value"] == 6
    assert evidence["maxContextWindowFraction"]["value"] == 0.91
    # The row still reports its own cost: the class was measured, only this
    # one figure was not.
    assert rows[0]["observedUsd"]["state"] == "available"


def test_an_s2_row_gains_no_evidence_line():
    """Every S2 class's evidence mapping is empty, so no S2 row grows a line
    and no S2 golden moves for this change."""
    diagnosis = _diagnosis()
    spec = kernel.spec_for("model_mix")
    subject = kernel.SubjectFacts(subject_key="claude-opus-4-20250514",
                                  subject_label="claude-opus-4-20250514",
                                  observed_usd=80.0, priced_entry_count=40)
    result = kernel.classify_class(spec, [subject], _denominator(),
                                   population=_coverage())
    text = diagnosis.render_terminal(_report([result]))
    assert "evidence:" not in text


# --- spec 5.4: the asymmetry is stated where it happens -----------------

def test_a_withheld_claude_fanout_row_states_the_lan_asymmetry():
    withheld = kernel.ClassResult(
        "subagent_fanout", kernel.VerdictState.WITHHELD.value,
        kernel.WithheldCause.TRANSCRIPTS_NOT_VISIBLE.value, (), _coverage(),
    )
    text = _diagnosis().render_terminal(_report([withheld]))
    assert kernel.DIAGNOSIS_TRANSCRIPT_ASYMMETRY_SENTENCE in text


def test_the_asymmetry_sentence_is_not_stated_for_another_cause():
    """It explains a denial, so printing it beside an absent store would tell
    the reader the dashboard was not authorized when the store was simply not
    there."""
    withheld = kernel.ClassResult(
        "subagent_fanout", kernel.VerdictState.WITHHELD.value,
        kernel.WithheldCause.SIGNAL_UNAVAILABLE.value, (), _coverage(),
    )
    text = _diagnosis().render_terminal(_report([withheld]))
    assert kernel.DIAGNOSIS_TRANSCRIPT_ASYMMETRY_SENTENCE not in text


def _repo_root():
    return pathlib.Path(__file__).resolve().parent.parent


def test_the_shared_transcript_asymmetry_clause_is_stated_in_the_same_words():
    """The claim is stated in three places, and only the CLAIM is shared.

    `bin/_lib_diagnosis.py` holds the terminal sentence, `bin/_cctally_parser.py`
    states the same fact in `explain --help`, and
    `dashboard/web/src/lib/diagnosis.ts` carries the modal's own sentence for
    `transcripts_not_visible`. A TypeScript module cannot import a Python
    constant, so nothing but this gate keeps the three from drifting into three
    different accounts of the same behaviour — which is exactly what they had
    done before R13.

    The gate is over the SECOND sentence of the constant, because that is the
    half that is position-independent. The leading clause differs on purpose:
    a footer under a withheld row and a per-class message are not the same
    sentence.
    """
    shared = kernel.DIAGNOSIS_TRANSCRIPT_ASYMMETRY_SENTENCE.split(". ", 1)[1]
    assert shared.startswith("Codex subagent attribution"), shared
    for relative in ("bin/_cctally_parser.py",
                     "dashboard/web/src/lib/diagnosis.ts"):
        source = (_repo_root() / relative).read_text()
        # The TypeScript literal is split across lines as `'a ' + 'b'` and the
        # help text is re-wrapped by `textwrap.dedent`, so the comparison joins
        # adjacent string literals and collapses whitespace rather than
        # comparing raw bytes.
        collapsed = " ".join(re.sub(r"'\s*\+\s*'", "", source).split())
        assert " ".join(shared.split()) in collapsed, relative


def test_the_terminal_and_the_modal_name_every_evidence_field_alike():
    """One vocabulary. The terminal printed the published camelCase key while
    the modal printed a human label for the same figure, so the two surfaces
    named the same evidence two different ways."""
    diagnosis = _diagnosis()
    source = (_repo_root() / "dashboard" / "web" / "src" / "lib"
              / "diagnosis.ts").read_text()
    body = source.split("export function evidenceLabel(", 1)[1]
    body = body.split("\n}", 1)[0]
    client = dict(re.findall(r"case '([A-Za-z]+)':\s*\n\s*return '([^']*)';",
                             body))
    assert client, body[:400]
    # The terminal joins evidence members with ", ", so a label carrying
    # a comma makes the line unreadable. Both files state the rule in a
    # comment and neither gated it: an equality check passes happily when
    # a comma is added to BOTH sides.
    assert all("," not in label for label in client.values()), client
    assert dict(diagnosis._EVIDENCE_LABELS) == client


def test_a_withheld_class_states_no_confidence_and_not_applicable_is_separate():
    diagnosis = _diagnosis()
    withheld = kernel.ClassResult(
        "short_high_context", kernel.VerdictState.WITHHELD.value,
        kernel.WithheldCause.SIGNAL_UNAVAILABLE.value, (), _coverage(),
    )
    inapplicable = kernel.ClassResult(
        "cache_churn", kernel.VerdictState.NOT_APPLICABLE.value, None, (),
        _coverage(),
    )
    report = _report([withheld, inapplicable])
    text = diagnosis.render_terminal(report)
    assert "Classes this provider does not support:" in text
    withheld_line = next(line for line in text.splitlines()
                         if "signal_unavailable" in line)
    assert "confidence" not in withheld_line
    assert report.results[0].withheld_class_count == 1
    assert report.results[0].applicable_class_count == 1
