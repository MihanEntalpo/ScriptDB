from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple, Type, Union, Final

# --- typing & mapping utilities ---

_PyType = Union[Type[int], Type[str], Type[float], Type[bytes], Type[bool]]

_SQLITE_TYPE_MAP: Final[dict[type, str]] = {
    int: "INTEGER",
    bool: "INTEGER",
    float: "REAL",
    str: "TEXT",
    bytes: "BLOB",
}

# Sentinel for automatic AUTOINCREMENT behaviour
Auto = object()


def _to_sqlite_type(py_type: _PyType) -> str:
    """Map a Python type to a SQLite storage class."""
    try:
        return _SQLITE_TYPE_MAP[py_type]  # type: ignore[index]
    except KeyError:
        raise ValueError(f"Unsupported Python type for SQLite column: {py_type!r}")


def _quote_ident(name: str) -> str:
    """Quote an identifier using double quotes with basic escaping."""
    return '"' + name.replace('"', '""') + '"'


def _default_literal(value: Any) -> str:
    """Render a Python value as a SQLite literal for DEFAULT clauses."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bytes):
        # Represent as X'ABCD...'
        return "X'" + value.hex() + "'"
    s = str(value)
    return "'" + s.replace("'", "''") + "'"


@dataclass
class _Column:
    name: str
    decl: str  # full column declaration fragment


@dataclass
class _CreateTableState:
    table: str
    if_not_exists: bool = True
    without_rowid: bool = False
    columns: List[_Column] = field(default_factory=list)
    table_constraints: List[str] = field(default_factory=list)


class _SQLBuilder:
    """Common base class for SQL builder objects."""

    __slots__ = ()

    def done(self) -> str:
        raise NotImplementedError

    def __str__(self) -> str:  # pragma: no cover - simple delegation
        return self.done()


class CreateTableBuilder(_SQLBuilder):
    """
    Builder for CREATE TABLE.
    The chain returns this concrete class to keep IDE autocompletion precise.

    Usage example:
        sql = (
            Builder.create_table("users")  # if_not_exists defaults to True
            .primary_key("id", int)
            .add_field("name", str, not_null=True)
            .add_field("is_active", bool, default=False, not_null=True)
            .unique("name")
            .done()
        )
    """

    __slots__ = ("_st",)

    def __init__(self, table: str, *, if_not_exists: bool = True, without_rowid: bool = False) -> None:
        self._st = _CreateTableState(
            table=table,
            if_not_exists=if_not_exists,
            without_rowid=without_rowid,
        )

    def primary_key(
        self,
        name: str,
        py_type: _PyType,
        *,
        auto_increment: bool | object = Auto,
        not_null: bool = True,
    ) -> "CreateTableBuilder":
        """Add a ``PRIMARY KEY`` column.

        By default, ``AUTOINCREMENT`` is enabled for ``int`` primary keys and
        disabled for other types. Override this behaviour by passing
        ``auto_increment=True`` or ``False`` explicitly. ``AUTOINCREMENT`` is
        only valid for ``INTEGER`` primary keys.

        Example:
            Builder.create_table("users").primary_key("id", int).done()
        """
        t = _to_sqlite_type(py_type)
        auto_inc = (t == "INTEGER") if auto_increment is Auto else bool(auto_increment)
        if auto_inc and t != "INTEGER":
            raise ValueError("AUTOINCREMENT is allowed only for INTEGER PRIMARY KEY")
        parts: List[str] = [t, "PRIMARY KEY"]
        if not_null and not auto_inc:
            parts.append("NOT NULL")
        if auto_inc:
            parts.append("AUTOINCREMENT")
        decl = f"{_quote_ident(name)} {' '.join(parts)}"
        self._st.columns.append(_Column(name=name, decl=decl))
        return self

    def add_field(
        self,
        name: str,
        py_type: _PyType,
        *,
        not_null: bool = False,
        unique: bool = False,
        default: Any = None,
        check: Optional[str] = None,
        references: Optional[Tuple[str, Optional[str]]] = None,
    ) -> "CreateTableBuilder":
        """Add a column to the table definition.

        Supports ``NOT NULL``, ``UNIQUE``, ``DEFAULT``, ``CHECK`` and
        ``REFERENCES`` clauses.

        Example:
            Builder.create_table("users").add_field("name", str, not_null=True).done()
        """
        t = _to_sqlite_type(py_type)
        parts: List[str] = [t]
        if not_null:
            parts.append("NOT NULL")
        if unique:
            parts.append("UNIQUE")
        if default is not None:
            parts.append("DEFAULT " + _default_literal(default))
        if check:
            parts.append(f"CHECK ({check})")
        if references:
            rt, rc = references
            if rc:
                parts.append(f"REFERENCES {_quote_ident(rt)}({_quote_ident(rc)})")
            else:
                parts.append(f"REFERENCES {_quote_ident(rt)}")
        decl = f"{_quote_ident(name)} {' '.join(parts)}"
        self._st.columns.append(_Column(name=name, decl=decl))
        return self

    def unique(self, *cols: str) -> "CreateTableBuilder":
        """Add a table-level ``UNIQUE`` constraint over columns.

        Example:
            Builder.create_table("users").unique("email").done()
        """
        if not cols:
            raise ValueError("UNIQUE requires at least one column")
        cols_sql = ", ".join(_quote_ident(c) for c in cols)
        self._st.table_constraints.append(f"UNIQUE ({cols_sql})")
        return self

    def check(self, expr: str) -> "CreateTableBuilder":
        """Add a table-level ``CHECK`` constraint.

        Example:
            Builder.create_table("numbers").check("value > 0").done()
        """
        self._st.table_constraints.append(f"CHECK ({expr})")
        return self

    def done(self) -> str:
        """Render the ``CREATE TABLE`` statement.

        Example:
            sql = Builder.create_table("users").add_field("name", str).done()
        """
        if not self._st.columns:
            raise ValueError("CREATE TABLE needs at least one column")
        cols_sql = ", ".join(c.decl for c in self._st.columns)
        constraints_sql = (
            (", " + ", ".join(self._st.table_constraints)) if self._st.table_constraints else ""
        )
        ine = " IF NOT EXISTS" if self._st.if_not_exists else ""
        tail = " WITHOUT ROWID" if self._st.without_rowid else ""
        sql = f"CREATE TABLE{ine} {_quote_ident(self._st.table)} ({cols_sql}{constraints_sql}){tail};"
        return sql


@dataclass
class _AlterTableAction:
    sql: str


class AlterTableBuilder(_SQLBuilder):
    """
    Builder for ALTER TABLE. Emits one or more statements on .done().
    """

    __slots__ = ("_table", "_actions")

    def __init__(self, table: str) -> None:
        self._table = table
        self._actions: List[_AlterTableAction] = []

    def add_column(
        self,
        name: str,
        py_type: _PyType,
        *,
        not_null: bool = False,
        unique: bool = False,
        default: Any = None,
        check: Optional[str] = None,
        references: Optional[Tuple[str, Optional[str]]] = None,
    ) -> "AlterTableBuilder":
        """Queue an ``ADD COLUMN`` action.

        Example:
            Builder.alter_table("users").add_column("age", int, default=0).done()
        """
        t = _to_sqlite_type(py_type)
        parts: List[str] = [t]
        if not_null:
            parts.append("NOT NULL")
        if unique:
            parts.append("UNIQUE")
        if default is not None:
            parts.append("DEFAULT " + _default_literal(default))
        if check:
            parts.append(f"CHECK ({check})")
        if references:
            rt, rc = references
            if rc:
                parts.append(f"REFERENCES {_quote_ident(rt)}({_quote_ident(rc)})")
            else:
                parts.append(f"REFERENCES {_quote_ident(rt)}")
        col_sql = f"{_quote_ident(name)} {' '.join(parts)}"
        self._actions.append(
            _AlterTableAction(
                sql=f"ALTER TABLE {_quote_ident(self._table)} ADD COLUMN {col_sql};"
            )
        )
        return self

    def drop_column(self, name: str) -> "AlterTableBuilder":
        """Queue a ``DROP COLUMN`` action.

        Example:
            Builder.alter_table("users").drop_column("email").done()
        """
        self._actions.append(
            _AlterTableAction(
                sql=f"ALTER TABLE {_quote_ident(self._table)} DROP COLUMN {_quote_ident(name)};"
            )
        )
        return self

    def rename_to(self, new_table_name: str) -> "AlterTableBuilder":
        """Queue a ``RENAME TO`` action to rename the table.

        Example:
            Builder.alter_table("users").rename_to("customers").done()
        """
        self._actions.append(
            _AlterTableAction(
                sql=f"ALTER TABLE {_quote_ident(self._table)} RENAME TO {_quote_ident(new_table_name)};"
            )
        )
        self._table = new_table_name
        return self

    def rename_column(self, old_name: str, new_name: str) -> "AlterTableBuilder":
        """Queue a ``RENAME COLUMN`` action.

        Example:
            Builder.alter_table("users").rename_column("name", "username").done()
        """
        self._actions.append(
            _AlterTableAction(
                sql=(
                    f"ALTER TABLE {_quote_ident(self._table)} "
                    f"RENAME COLUMN {_quote_ident(old_name)} TO {_quote_ident(new_name)};"
                )
            )
        )
        return self

    def done(self) -> str:
        """Render the queued ``ALTER TABLE`` statements joined by newlines.

        Example:
            Builder.alter_table("users").add_column("age", int).done()
        """
        if not self._actions:
            raise ValueError("ALTER TABLE: no actions queued")
        return "\n".join(a.sql for a in self._actions)




class Builder:
    """
      Entry points with explicit return types for solid autocompletion:
        - Builder.create_table(name, *, if_not_exists=True, without_rowid=False) -> CreateTableBuilder
        - Builder.alter_table(name) -> AlterTableBuilder
        - Builder.drop_table(name, *, if_exists=True) -> str
        - Builder.create_index(name, table, *, on, unique=False, if_not_exists=True) -> str
        - Builder.drop_index(name, *, if_exists=True) -> str
    """

    @staticmethod
    def create_table(name: str, *, if_not_exists: bool = True, without_rowid: bool = False) -> CreateTableBuilder:
        """Start building a ``CREATE TABLE`` statement.

        Example:
            Builder.create_table("users").done()
        """
        return CreateTableBuilder(name, if_not_exists=if_not_exists, without_rowid=without_rowid)

    @staticmethod
    def alter_table(name: str) -> AlterTableBuilder:
        """Start an ``ALTER TABLE`` builder for the given table.

        Example:
            Builder.alter_table("users").add_column("age", int).done()
        """
        return AlterTableBuilder(name)

    @staticmethod
    def drop_table(name: str, *, if_exists: bool = True) -> str:
        """Return a ``DROP TABLE`` statement."""

        ie = " IF EXISTS" if if_exists else ""
        return f"DROP TABLE{ie} {_quote_ident(name)};"

    @staticmethod
    def create_index(
        name: str,
        table: str,
        *,
        on: Union[str, List[str]],
        unique: bool = False,
        if_not_exists: bool = True,
    ) -> str:
        """Generate a ``CREATE INDEX`` statement.

        Example:
            Builder.create_index("idx_users_name", "users", on="name")
        """
        columns = [on] if isinstance(on, str) else list(on)
        if not columns:
            raise ValueError("CREATE INDEX requires at least one column")
        cols_sql = ", ".join(_quote_ident(c) for c in columns)
        ine = " IF NOT EXISTS" if if_not_exists else ""
        unique_sql = "UNIQUE " if unique else ""
        return (
            f"CREATE {unique_sql}INDEX{ine} {_quote_ident(name)} "
            f"ON {_quote_ident(table)} ({cols_sql});"
        )

    @staticmethod
    def drop_index(name: str, *, if_exists: bool = True) -> str:
        """Generate a ``DROP INDEX`` statement.

        Example:
            Builder.drop_index("idx_users_name")
        """
        ie = " IF EXISTS" if if_exists else ""
        return f"DROP INDEX{ie} {_quote_ident(name)};"
