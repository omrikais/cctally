import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(p):
    return (ROOT / p).read_text(encoding="utf-8")


def test_runtime_gate_is_311():
    assert "__min_python_version__ = (3, 11)" in _read("bin/cctally")
    assert "__min_python_version__ = (3, 11)" in _read("bin/cctally-release")


def test_user_facing_declarations_agree_on_311():
    assert "Python 3.11+" in _read("README.md")
    assert "Python 3.11+" in _read("docs/installation.md")
    assert "3.11+" in _read("bin/cctally-npm-shim.js")
    # The floor declarations must not still advertise 3.13.
    assert "Python 3.13+" not in _read("README.md")
    assert "Python 3.13+" not in _read("docs/installation.md")


def test_brew_template_still_pins_313():
    # Deliberately unchanged: brew bundles its own interpreter.
    assert 'python@3.13' in _read("homebrew/cctally.rb.template")
