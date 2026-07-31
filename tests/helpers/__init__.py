"""Importable helper package for the pytest suite.

`tests/` is on `sys.path` (pytest's prepend import mode inserts each test
file's directory), so modules here import as `helpers.<module>`. The explicit
`__init__.py` keeps that a real package rather than relying on namespace-package
resolution.
"""
