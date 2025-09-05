from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, List, Optional, Tuple, Type, Union

# --- typing & mapping utilities ---

_PyType = Union[
    Type[int],
    Type[str],
    Type[float],
    Type[bytes],
    Type[bool],
    Type[date],
    Type[datetime],
]

_SQLITE_TYPE_MAP: Final[dict[type, str]] = {
    int: "INTEGER",
    bool: "INTEGER",
    float: "REAL",
    str: "TEXT",
    bytes: "BLOB",
    date: "TEXT",
    datetime: "TEXT",
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

        Parameters
        ----------
        name:
            Column name.
        py_type:
            Python type of the column. Supported types: ``int``, ``str``,
            ``float``, ``bytes``, ``bool``, ``date`` and ``datetime``.
        auto_increment:
            Enable ``AUTOINCREMENT`` for ``INTEGER`` primary keys. By default it
            is enabled automatically for ``int`` columns and disabled for other
            types. Pass ``True`` or ``False`` to override.
        not_null:
            Whether to add ``NOT NULL``. Ignored if ``auto_increment`` is true
            because SQLite automatically implies ``NOT NULL``.

        Example
        -------
        ``Builder.create_table("users").primary_key("id", int).done()``
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

        Parameters
        ----------
        name:
            Column name.
        py_type:
            Python type of the column. Supported types: ``int``, ``str``,
            ``float``, ``bytes``, ``bool``, ``date`` and ``datetime``.
        not_null:
            If ``True`` adds a ``NOT NULL`` constraint.
        unique:
            If ``True`` adds a ``UNIQUE`` constraint.
        default:
            Default value for the column rendered as a SQLite literal.
        check:
            ``CHECK`` expression.
        references:
            ``(table, column)`` tuple for a ``REFERENCES`` clause. Pass ``(table,
            None)`` to reference a table without specifying a column.

        Example
        -------
        ``Builder.create_table("users").add_field("name", str, not_null=True).done()``
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
        """Add a table-level ``UNIQUE`` constraint.

        Parameters
        ----------
        cols:
            One or more column names to include in the constraint.

        Example
        -------
        ``Builder.create_table("users").unique("email").done()``
        """
        if not cols:
            raise ValueError("UNIQUE requires at least one column")
        cols_sql = ", ".join(_quote_ident(c) for c in cols)
        self._st.table_constraints.append(f"UNIQUE ({cols_sql})")
        return self

    def check(self, expr: str) -> "CreateTableBuilder":
        """Add a table-level ``CHECK`` constraint.

        Parameters
        ----------
        expr:
            Expression evaluated for each row.

        Example
        -------
        ``Builder.create_table("numbers").check("value > 0").done()``
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

        Parameters
        ----------
        name:
            Column name to add.
        py_type:
            Python type of the column. Supported types: ``int``, ``str``,
            ``float``, ``bytes``, ``bool``, ``date`` and ``datetime``.
        not_null:
            If ``True`` adds a ``NOT NULL`` constraint.
        unique:
            If ``True`` adds a ``UNIQUE`` constraint.
        default:
            Default value for the column rendered as a SQLite literal.
        check:
            ``CHECK`` expression.
        references:
            ``(table, column)`` tuple for a ``REFERENCES`` clause. Pass ``(table,
            None)`` to reference a table without specifying a column.

        Example
        -------
        ``Builder.alter_table("users").add_column("age", int, default=0).done()``
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

        Parameters
        ----------
        name:
            Column name to drop.

        Example
        -------
        ``Builder.alter_table("users").drop_column("email").done()``
        """
        self._actions.append(
            _AlterTableAction(
                sql=f"ALTER TABLE {_quote_ident(self._table)} DROP COLUMN {_quote_ident(name)};"
            )
        )
        return self

    def rename_to(self, new_table_name: str) -> "AlterTableBuilder":
        """Queue a ``RENAME TO`` action to rename the table.

        Parameters
        ----------
        new_table_name:
            New table name.

        Example
        -------
        ``Builder.alter_table("users").rename_to("customers").done()``
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

        Parameters
        ----------
        old_name:
            Existing column name.
        new_name:
            New column name.

        Example
        -------
        ``Builder.alter_table("users").rename_column("name", "username").done()``
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


class DropTableBuilder(_SQLBuilder):
    """Builder for ``DROP TABLE`` statements."""

    __slots__ = ("_table", "_if_exists")

    def __init__(self, table: str, *, if_exists: bool = True) -> None:
        self._table = table
        self._if_exists = if_exists

    def done(self) -> str:
        """Render the ``DROP TABLE`` statement."""
        ie = " IF EXISTS" if self._if_exists else ""
        return f"DROP TABLE{ie} {_quote_ident(self._table)};"


class CreateIndexBuilder(_SQLBuilder):
    """Builder for ``CREATE INDEX`` statements."""

    __slots__ = (
        "_name",
        "_table",
        "_columns",
        "_unique",
        "_if_not_exists",
    )

    def __init__(
        self,
        table: str,
        columns: List[str],
        *,
        unique: bool = False,
        if_not_exists: bool = True,
        name: Optional[str] = None,
    ) -> None:
        if not columns:
            raise ValueError("CREATE INDEX requires at least one column")
        self._table = table
        self._columns = columns
        self._unique = unique
        self._if_not_exists = if_not_exists
        self._name = name or f"{table}_{'_'.join(columns)}_idx"

    def done(self) -> str:
        """Render the ``CREATE INDEX`` statement."""
        cols_sql = ", ".join(_quote_ident(c) for c in self._columns)
        ine = " IF NOT EXISTS" if self._if_not_exists else ""
        unique_sql = "UNIQUE " if self._unique else ""
        return (
            f"CREATE {unique_sql}INDEX{ine} {_quote_ident(self._name)} "
            f"ON {_quote_ident(self._table)} ({cols_sql});"
        )


class DropIndexBuilder(_SQLBuilder):
    """Builder for ``DROP INDEX`` statements."""

    __slots__ = ("_name", "_if_exists")

    def __init__(
        self,
        name: str,
        *,
        if_exists: bool = True,
    ) -> None:
        self._name = name
        self._if_exists = if_exists

    @classmethod
    def from_table(
        cls,
        table: str,
        columns: List[str],
        *,
        if_exists: bool = True,
        name: Optional[str] = None,
    ) -> "DropIndexBuilder":
        if not columns and name is None:
            raise ValueError("DROP INDEX requires at least one column for automatic naming")
        idx_name = name or f"{table}_{'_'.join(columns)}_idx"
        return cls(idx_name, if_exists=if_exists)

    def done(self) -> str:
        """Render the ``DROP INDEX`` statement."""
        ie = " IF EXISTS" if self._if_exists else ""
        return f"DROP INDEX{ie} {_quote_ident(self._name)};"




class Builder:
    """
      Entry points with explicit return types for solid autocompletion:
        - Builder.create_table(name, *, if_not_exists=True, without_rowid=False) -> CreateTableBuilder
        - Builder.alter_table(name) -> AlterTableBuilder
        - Builder.drop_table(name, *, if_exists=True) -> DropTableBuilder
        - Builder.create_index(table, on, *, unique=False, if_not_exists=True, name=None) -> CreateIndexBuilder
        - Builder.drop_index(table, on, *, if_exists=True, name=None) -> DropIndexBuilder
    """

    @staticmethod
    def create_table(name: str, *, if_not_exists: bool = True, without_rowid: bool = False) -> CreateTableBuilder:
        """Start building a ``CREATE TABLE`` statement.

        Parameters
        ----------
        name:
            Table name.
        if_not_exists:
            Add ``IF NOT EXISTS`` to the statement if ``True``.
        without_rowid:
            Append ``WITHOUT ROWID`` to the statement if ``True``.

        Example
        -------
        ``Builder.create_table("users").done()``
        """
        return CreateTableBuilder(name, if_not_exists=if_not_exists, without_rowid=without_rowid)

    @staticmethod
    def alter_table(name: str) -> AlterTableBuilder:
        """Start an ``ALTER TABLE`` builder for the given table.

        Parameters
        ----------
        name:
            Table name to alter.

        Example
        -------
        ``Builder.alter_table("users").add_column("age", int).done()``
        """
        return AlterTableBuilder(name)

    @staticmethod
    def drop_table(name: str, *, if_exists: bool = True) -> DropTableBuilder:
        """Start a ``DROP TABLE`` builder for the given table.

        Parameters
        ----------
        name:
            Table name to drop.
        if_exists:
            Add ``IF EXISTS`` if ``True``.
        """

        return DropTableBuilder(name, if_exists=if_exists)

    @staticmethod
    def create_index(
        table: str,
        on: Union[str, List[str]],
        *,
        unique: bool = False,
        if_not_exists: bool = True,
        name: Optional[str] = None,
    ) -> CreateIndexBuilder:
        """Start building a ``CREATE INDEX`` statement.

        Parameters
        ----------
        table:
            Table on which to create the index.
        on:
            Column name or list of column names to index.
        unique:
            If ``True`` create a ``UNIQUE`` index.
        if_not_exists:
            Add ``IF NOT EXISTS`` if ``True``.
        name:
            Optional explicit index name. If omitted, the name is constructed as
            ``"{table}_{cols_joined}_idx"``.
        """
        columns = [on] if isinstance(on, str) else list(on)
        return CreateIndexBuilder(
            table,
            columns,
            unique=unique,
            if_not_exists=if_not_exists,
            name=name,
        )

    @staticmethod
    def drop_index(
        table: str,
        on: Union[str, List[str]],
        *,
        if_exists: bool = True,
        name: Optional[str] = None,
    ) -> DropIndexBuilder:
        """Start building a ``DROP INDEX`` statement.

        Parameters
        ----------
        table:
            Table the index belongs to.
        on:
            Column name or list of column names that were indexed.
        if_exists:
            Add ``IF EXISTS`` if ``True``.
        name:
            Explicit index name. If omitted, the name is constructed as in
            :py:meth:`create_index`.
        """
        columns = [on] if isinstance(on, str) else list(on)
        return DropIndexBuilder.from_table(
            table,
            columns,
            if_exists=if_exists,
            name=name,
        )
