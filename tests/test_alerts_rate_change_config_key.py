"""``alerts.rate_change_enabled`` on the CLI, and the hole that hid its absence.

#661 S2 section 6.2 gates the metering-rate-change PUSH on this key. It was
added to ``ALLOWED_CONFIG_KEYS`` and to the read validator, and no ``config
set``/``get``/``unset`` branch was ever written, so ``cctally config set
alerts.rate_change_enabled true`` fell through ``_cmd_config_set``'s closing
``return 2`` and exited 2 printing nothing at all, while ``config get`` printed
an empty value at exit 0 even with the key present in ``config.json``.

Two subjects, deliberately in one module. The first is the key's own
round-trip, written the way ``dashboard.cache_failure_markers`` is written. The
second is the STRUCTURAL guard: no allowlisted key may reach that closing
``return 2``, because a silent exit 2 tells a user nothing and tells the estate
nothing either.

Driven through ``load_script() + redirect_paths()`` so every read and write
lands in a temp data dir rather than the developer's real store.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402

_KEY = "alerts.rate_change_enabled"


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _get(ns):
    return ns["_config_known_value"](ns["load_config"](), _KEY)


def _set(ns, value, *, emit_json=False):
    return ns["_cmd_config_set"](
        argparse.Namespace(key=_KEY, value=value, emit_json=emit_json)
    )


def test_key_is_in_allowed_config_keys(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    assert _KEY in ns["ALLOWED_CONFIG_KEYS"]


def test_default_is_false_when_unset(tmp_path, monkeypatch):
    """Opt-in, like its sibling ``alerts.projected_enabled``: recording is
    unconditional and only the notification is gated."""
    ns = _load(tmp_path, monkeypatch)
    assert _get(ns) is False


def test_set_normalizes_truthy_and_falsy_strings(tmp_path, monkeypatch):
    for v in ("true", "True", "1", "yes", "on"):
        ns = _load(tmp_path / v, monkeypatch)
        assert _set(ns, v) == 0
        assert _get(ns) is True
    for v in ("false", "False", "0", "no", "off"):
        ns = _load(tmp_path / f"f_{v}", monkeypatch)
        assert _set(ns, v) == 0
        assert _get(ns) is False


def test_set_rejects_a_non_boolean_and_names_this_key(tmp_path, monkeypatch,
                                                      capsys):
    """The shared normalizer spells ``alerts.enabled`` into its own error, so
    the branch must re-message with the key the user actually typed."""
    ns = _load(tmp_path, monkeypatch)
    assert _set(ns, "maybe") == 2
    err = capsys.readouterr().err
    assert _KEY in err
    assert "alerts.enabled" not in err


def test_set_preserves_sibling_alerts_keys(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    ns["save_config"]({"alerts": {"enabled": True, "weekly_thresholds": [50]}})
    assert _set(ns, "true") == 0
    stored = ns["load_config"]()["alerts"]
    assert stored["rate_change_enabled"] is True
    assert stored["enabled"] is True
    assert stored["weekly_thresholds"] == [50]


def test_get_reads_a_stored_value_rather_than_printing_nothing(tmp_path,
                                                               monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    ns["save_config"]({"alerts": {"rate_change_enabled": True}})
    assert _get(ns) is True
    ns["save_config"]({"alerts": {"rate_change_enabled": False}})
    assert _get(ns) is False


def test_get_surfaces_the_default_over_a_corrupt_alerts_block(tmp_path,
                                                              monkeypatch):
    """Mirrors ``alerts.projected_enabled``: a hand-edited junk block reads as
    the default rather than erroring out of a plain ``config get``."""
    ns = _load(tmp_path, monkeypatch)
    ns["save_config"]({"alerts": {"rate_change_enabled": True,
                                  "notifier": "not-a-backend"}})
    assert _get(ns) is False


def test_unset_drops_only_this_leaf(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    ns["save_config"]({"alerts": {"enabled": True, "rate_change_enabled": True}})
    rc = ns["_cmd_config_unset"](argparse.Namespace(key=_KEY))
    assert rc == 0
    stored = ns["load_config"]()["alerts"]
    assert "rate_change_enabled" not in stored
    assert stored["enabled"] is True


def test_unset_is_idempotent_on_a_missing_key(tmp_path, monkeypatch):
    ns = _load(tmp_path, monkeypatch)
    rc = ns["_cmd_config_unset"](argparse.Namespace(key=_KEY))
    assert rc == 0


# --- The structural guard ------------------------------------------------
#
# Scoped to keys whose set branch is pure config I/O. The ``budget.*`` family
# is excluded by name because its set branches run the forward-only budget
# reconcile through the journal ingest cycle, so exercising them here would
# open stores rather than test the dispatch this asserts.
_RECONCILING_PREFIX = "budget."


def _non_reconciling_keys():
    ns = load_script()
    return [k for k in ns["ALLOWED_CONFIG_KEYS"]
            if not k.startswith(_RECONCILING_PREFIX)]


@pytest.mark.parametrize("key", _non_reconciling_keys())
def test_no_allowlisted_key_falls_through_set_in_silence(key, tmp_path,
                                                         monkeypatch, capsys):
    """``_cmd_config_set`` ends in ``return 2  # unreachable given the gate
    above``. It was reachable, and reaching it prints nothing.

    The value below is nonsense for every key, so a handled branch either
    accepts it (rc 0) or refuses it with a message. An UNHANDLED key is the
    only way to get a non-zero exit and an empty stderr, which is exactly what
    ``alerts.rate_change_enabled`` did.

    SCOPE, because this guard proves less than its name suggests. The
    parametrization excludes the ``budget.*`` family by name, for the reason
    given at ``_RECONCILING_PREFIX``, so it covers 23 of the 39 allowlisted
    keys rather than all of them. A ``budget.*`` key added later with no set
    branch reproduces the exact #661 S2 defect with nothing here catching it.
    That limit is recorded rather than closed: exercising those branches here
    would run the forward-only budget reconcile through the journal ingest
    cycle, which is a different test's subject.
    """
    ns = _load(tmp_path / key.replace(".", "_"), monkeypatch)
    rc = ns["_cmd_config_set"](
        argparse.Namespace(key=key, value="__not_a_valid_value__",
                           emit_json=False)
    )
    err = capsys.readouterr().err
    assert rc == 0 or err.strip(), (
        f"{key}: exited {rc} with empty stderr — the closing `return 2` was "
        f"reached, so this key has no set branch"
    )
