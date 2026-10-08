"""#887 — digest-aware provider-token matching for the S3 privacy tests.

The S3 privacy tests assert that short provider session ids (`70001`, ...)
never reach a served surface. Those surfaces legitimately carry per-run opaque
content — `cbk1_` block keys whose 40-hex digest varies with `tmp_path`, and the
test's own temporary paths in the anon-map plan — and a five-digit token occurs
inside such content by chance (merge-gate run 20260928T073227Z-79356-20070).

This is the ONE matcher both S3 test modules share
(docs/superpowers/specs/2026-10-01-887-digest-aware-leak-assertions.md):

* `normalize` strips exactly the recognized per-run content — the hex-family
  keys and the bounded per-run literals — replacing each span with a NUL
  separator, so a token can neither hide inside it nor be manufactured across
  it;
* `carries_token` searches the normalized text;
* `unrecognized_digest_runs` is the I4 tripwire. K (every known opaque-family
  prefix the product mints, anywhere, any case), G2 (hex runs >= 16) and G4
  (decimal runs >= 10) are guaranteed; G1 and G3 are best-effort. The
  operator-approved residual (2026-10-01) is a base64url value with no known K
  prefix that G1/G3 miss — a future family, or the product's existing
  unprefixed values (outline-transfer token, LAN authentication token, search
  cursor) — at about 1.5e-7 per 43-character value per run. The tripwire
  also flags some legitimate shapes no checked surface carries today (K:
  `go1.21`, `foo1.bar`; G1: `x86_64-linux`, `utf8_encoding`). Such a hit is
  deterministic — it fails on every run, never intermittently — so it surfaces
  as a fail-closed diagnostic to triage, not as a flake;
* the `assert_*` helpers raise the exact messages the S3 tests match.
"""
from __future__ import annotations

import os
import re

from _lib_conversation_anon import _dash_encode  # bin/ joins sys.path in conftest

_SEP = "\x00"

_HEX_KEY_RE = re.compile(
    r"(?<![0-9A-Za-z_])(?:cbk1|civ1|cliv1)_[0-9a-f]{40}(?![0-9A-Za-z_])")

# Guaranteed (spec §3, I4).
_K_RE = re.compile(
    r"(?i:(?:cbk|civ|cliv)[0-9]+_|(?:ofc|[ov])[0-9]+\.)[A-Za-z0-9_-]+")
_G2_RE = re.compile(r"[0-9a-fA-F]{16,}")
_G4_RE = re.compile(r"[0-9]{10,}")
# Best-effort, no completeness claim.
_G1_RE = re.compile(r"(?<![A-Za-z0-9_])[a-z]{1,6}[0-9]{1,2}[._][A-Za-z0-9_-]{8,}")
_G3_RUN_RE = re.compile(r"[A-Za-z0-9_-]{16,}")


def per_run_literals(home) -> tuple[str, ...]:
    """The test's own per-run text: `home` and its realpath, each as given, in
    the product's dash encoding (`/` -> `-` only), and as the basename the
    served anon plan publishes as its username token. Longest first."""
    out = set()
    for path in (os.fspath(home), os.path.realpath(home)):
        for form in (path, _dash_encode(path), os.path.basename(path.rstrip("/"))):
            if form:
                out.add(form)
    return tuple(sorted(out, key=lambda s: (-len(s), s)))


def normalize(text: str, per_run=()) -> str:
    """`text` with every recognized key and bounded per-run literal replaced by
    one NUL separator."""
    text = _HEX_KEY_RE.sub(_SEP, text)
    for literal in per_run:
        text = re.sub(
            r"(?<![A-Za-z0-9_.])" + re.escape(literal) + r"(?![A-Za-z0-9_.])",
            _SEP, text)
    return text


def carries_token(text: str, token: str, per_run=()) -> bool:
    return token in normalize(text, per_run)


def unrecognized_digest_runs(text: str, per_run=()) -> list[str]:
    norm = normalize(text, per_run)
    runs = []
    for rx in (_K_RE, _G2_RE, _G4_RE, _G1_RE):
        runs.extend(m.group(0) for m in rx.finditer(norm))
    runs.extend(
        run for run in _G3_RUN_RE.findall(norm)
        if re.search(r"[0-9]", run) and re.search(r"[A-Z]", run)
        and re.search(r"[a-z]", run))
    return runs


def _as_list(texts) -> list[str]:
    return [texts] if isinstance(texts, str) else list(texts)


def assert_tokens_absent(texts, tokens, where, per_run=()) -> None:
    for text in _as_list(texts):
        runs = unrecognized_digest_runs(text, per_run)
        if runs:
            raise AssertionError(f"unrecognized digest-shaped run at {where}: {runs!r}")
        for token in tokens:
            if carries_token(text, token, per_run):
                raise AssertionError(f"provider token {token!r} leaked at {where}")


def assert_token_carried(texts, tokens, where, per_run=(), each=False) -> None:
    texts = _as_list(texts)
    hits = [any(carries_token(text, token, per_run) for token in tokens)
            for text in texts]
    if not texts or not (all(hits) if each else any(hits)):
        raise AssertionError(f"no provider token outside opaque keys at {where}")


def assert_export_attribution(export_text, raw_lines, tokens, per_run=()) -> None:
    """Boundary 3: every export line carrying a token is verbatim provider
    bytes, at least one does, and no derived line carries a digest-shaped run."""
    lines = export_text.splitlines()
    hits = [line for line in lines
            if any(carries_token(line, token, per_run) for token in tokens)]
    if not hits:
        raise AssertionError("no provider token outside opaque keys at export")
    for line in hits:
        if line.strip() not in raw_lines:
            raise AssertionError(
                f"export line carrying a provider token is not provider bytes: {line!r}")
    for line in lines:
        if line.strip() and line.strip() not in raw_lines:
            runs = unrecognized_digest_runs(line, per_run)
            if runs:
                raise AssertionError(f"unrecognized digest-shaped run at export: {runs!r}")
