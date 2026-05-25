"""Every bin/cctally-*-test harness must source the dev-autodetect
suppressor preamble (directly, or transitively via _lib-fixture-harness.sh).
A new harness added without it relocates its data dir to cctally-dev on a
dev machine and its goldens break on direct runs — this test makes that a
hard, drift-proof failure rather than something to remember."""
import pathlib

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"


def _harnesses():
    return sorted(p for p in BIN.glob("cctally-*-test") if p.is_file())


def test_every_harness_sources_the_suppressor_preamble():
    missing = []
    for h in _harnesses():
        text = h.read_text()
        sources_preamble = "_lib-harness-env.sh" in text
        sources_fixture_lib = "_lib-fixture-harness.sh" in text
        if not (sources_preamble or sources_fixture_lib):
            missing.append(h.name)
    assert not missing, (
        "these harnesses source neither _lib-harness-env.sh nor "
        f"_lib-fixture-harness.sh: {missing}"
    )


def test_preamble_exports_the_suppressor():
    preamble = BIN / "_lib-harness-env.sh"
    assert preamble.is_file()
    assert "export CCTALLY_DISABLE_DEV_AUTODETECT=1" in preamble.read_text()


def test_fixture_lib_sources_the_preamble():
    lib = (BIN / "_lib-fixture-harness.sh").read_text()
    assert "_lib-harness-env.sh" in lib
