# ScriptDB Agent Guide

This repository contains **ScriptDB**, a minimal asynchronous wrapper around
SQLite with built-in migration support. It is intended for small integration
and ETL scripts that require persistence without maintaining a separate
database server. The project will be published to PyPI for installation as a
regular Python package.

## Guidelines for contributors

- Read `SPEC.md` before starting every task so implementation work is grounded in the current architecture and behavior.
- Keep changes focused; avoid modifying unrelated files.
- Install test dependencies with `pip install -e .[test]` before running tests.
- Run `ruff check .`, `mypy src/scriptdb`, and `pytest --cov=scriptdb --cov-report=term-missing` before committing.
- The codebase uses the `src/` layout with the package located at `src/scriptdb`.
- Every time you edit the README, verify that code block formatting is correct and fences are balanced.
- If you change the project architecture, core behavior, public API shape, or any material design constraint, update
  `SPEC.md` in the same task.
