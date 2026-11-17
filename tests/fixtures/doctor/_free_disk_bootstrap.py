"""Run `bin/cctally` with retention's free-disk reading pinned (#870, #947).

Usage: python3 _free_disk_bootstrap.py <cctally-script> <free-bytes> [args...]

Shared by `bin/cctally-doctor-test` and `tests/test_cmd_doctor.py`, which
both execute this file by its committed path; it is never imported. Doctor's
only free-space reader is `_cctally_retention._disk_usage`, so pinning it
makes the default policy's free-disk floor independent of the host's real
free space.

Two phases:

- Setup (argument validation, path setup, the patch, loading and executing
  the binary's module body). Every failure, including a `SystemExit` raised
  by the module body, prints `free-disk bootstrap: <reason>` to stderr and
  exits 97, a status `cctally doctor` never uses (it exits 0 or 2), so a
  harness can tell a broken pin from a doctor verdict.
- Run: `main()` is called exactly once outside the setup handler, and its
  outcome (return value, exception or exit) is the process's status, as
  under the binary's shebang.
"""
import importlib.machinery
import importlib.util
import os
import re
import sys
import types

SETUP_FAILURE_STATUS = 97


def _setup():
    args = sys.argv[1:]
    if len(args) < 2:
        raise ValueError(
            "usage: _free_disk_bootstrap.py <cctally-script> <free-bytes> "
            "[args...]")
    script, free_bytes, rest = args[0], args[1], args[2:]
    if re.fullmatch(r"[0-9]+", free_bytes) is None:
        raise ValueError(
            f"free-bytes must be a non-negative decimal integer: {free_bytes!r}")
    free = int(free_bytes)

    # What a direct `python3 <script>` execution would set: the script's own
    # directory first on the import path, and the script as argv[0].
    sys.path[0] = os.path.dirname(os.path.realpath(script))
    sys.argv = [script, *rest]

    import _cctally_retention

    if not callable(getattr(_cctally_retention, "_disk_usage", None)):
        raise AttributeError(
            "_cctally_retention._disk_usage is missing or not callable")

    def _pinned_disk_usage(path):
        return types.SimpleNamespace(total=free, used=0, free=free)

    _cctally_retention._disk_usage = _pinned_disk_usage

    loader = importlib.machinery.SourceFileLoader("cctally", script)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    if not callable(getattr(module, "main", None)):
        raise AttributeError(f"{script} defines no callable main()")
    return module


def _run():
    try:
        module = _setup()
    except BaseException as exc:  # noqa: BLE001 - every setup failure is 97
        reason = f"{type(exc).__name__}: {exc}"
        print(f"free-disk bootstrap: {reason}", file=sys.stderr)
        raise SystemExit(SETUP_FAILURE_STATUS)
    raise SystemExit(module.main())


# Guarded so a multiprocessing spawn child, which re-runs the main module as
# `__mp_main__`, does not repeat the setup with the parent's argv.
if __name__ == "__main__":
    _run()
