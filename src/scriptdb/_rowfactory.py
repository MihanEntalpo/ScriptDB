from __future__ import annotations

import inspect
import sqlite3
from typing import Any, Dict, Sequence, Tuple, Type, Union, cast

RowDict = Dict[str, Any]
RowType = Union[sqlite3.Row, RowDict]
RowFactorySetting = Union[Type[sqlite3.Row], Type[dict]]


def normalize_row_factory(row_factory: RowFactorySetting) -> Tuple[RowFactorySetting, bool]:
    if row_factory is dict:
        return dict, True
    if row_factory is sqlite3.Row:
        return sqlite3.Row, False
    raise TypeError("row_factory must be either dict or sqlite3.Row")


def dict_row_factory(cursor: "sqlite3.Cursor", row: Sequence[Any]) -> RowDict:
    """sqlite3 row factory that returns plain dictionaries."""

    return {description[0]: row[idx] for idx, description in enumerate(cursor.description or [])}


def first_column_value(row: RowType, rows_as_dict: bool) -> Any:
    if rows_as_dict:
        values = cast(RowDict, row).values()
        return next(iter(values), None)
    row_obj = cast(sqlite3.Row, row)
    return row_obj[0]


def supports_row_factory(cls: Type[Any]) -> bool:
    """Return True if *cls*'s __init__ accepts a row_factory argument."""

    try:
        signature = inspect.signature(cls.__init__)
    except (TypeError, ValueError):  # pragma: no cover - builtins or exotic callables
        return True

    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
    return "row_factory" in signature.parameters

