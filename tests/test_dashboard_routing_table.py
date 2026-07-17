"""#279 S5 F5: route-table order regression (spec §7).

The route tables are module-level constants on ``_cctally_dashboard``.
That module cannot be imported standalone (its ``BLOCK_DURATION`` shim
reads ``sys.modules['cctally']`` at load time), so we go through the
conftest ``load_script()`` harness that populates ``sys.modules`` — then
read the dashboard module object.
"""
import sys

from conftest import load_script


def _dash():
    load_script()
    return sys.modules["_cctally_dashboard"]


def _names(table):
    return [entry[2] for entry in table]


def test_conversation_routes_precede_catch_all():
    dash = _dash()
    names = _names(dash._GET_ROUTES)
    catch = names.index("_handle_get_conversation_detail")
    for earlier in (
        "_handle_get_conversations",
        "_handle_get_conversation_search",
        "_handle_get_conversation_payload",
        "_handle_get_conversation_media",
        "_handle_get_conversation_outline",
        "_handle_get_conversation_find",
        "_handle_get_conversation_events",
        "_handle_get_conversation_export",
        "_handle_get_conversation_anon_map",
        "_handle_get_conversation_prompts",
    ):
        assert names.index(earlier) < catch, earlier


def test_source_detail_route_is_registered_before_legacy_catch_alls():
    dash = _dash()
    route = next(entry for entry in dash._GET_ROUTES if entry[2] == "_handle_get_source_detail")
    assert route[:2] == ("prefix", "/api/source/")
    assert route[4] is True


def test_delete_share_asymmetry_preserved():
    dash = _dash()
    kinds = {e[2]: (e[0], e[1]) for e in dash._DELETE_ROUTES}
    assert kinds["_handle_share_presets_delete"][0] == "prefix"
    assert kinds["_handle_share_presets_delete"][1] == "/api/share/presets/"
    assert kinds["_handle_share_history_delete"][0] == "exact"


def test_post_routes_have_no_settings_405_guard_entry():
    # gate P1-3: POST /api/settings must dispatch to its handler
    dash = _dash()
    assert any(e[1] == "/api/settings" and e[2] == "_handle_post_settings"
               for e in dash._POST_ROUTES)


def test_conversation_perf_wraps_present():
    # gate P2-1: the per-route perf wraps survive as table entries (12 after
    # #281 S4 adds /anon-map with a scope wrap).
    dash = _dash()
    wrapped = [e for e in dash._GET_ROUTES if e[3] is not None]
    assert len(wrapped) == 12
    assert {e[3][0] for e in wrapped} == {"scope", "phase"}
