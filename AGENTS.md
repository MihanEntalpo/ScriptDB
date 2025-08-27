# ScriptDB Agent Guide

This repository contains **ScriptDB**, a minimal asynchronous wrapper around
SQLite with built-in migration support. It is intended for small integration
and ETL scripts that require persistence without maintaining a separate
database server. The project will be published to PyPI for installation as a
regular Python package.

## Guidelines for contributors

- Keep changes focused; avoid modifying unrelated files.
- Install test dependencies with `pip install -e .[test]` before running tests.
- Run `ruff check .`, `mypy src/scriptdb`, and `pytest --cov=scriptdb --cov-report=term-missing` before committing.
- The codebase uses the `src/` layout with the package located at `src/scriptdb`.

