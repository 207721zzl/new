"""基于 SQLGlot MySQL AST 的只读 SQL 安全校验。"""

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.analysis.models import ValidatedSQL
from app.analysis.schema import ALLOWED_TABLE_COLUMNS, SYSTEM_DATABASES
from app.errors import UnsafeSQLError


FORBIDDEN_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Command,
    exp.Merge,
    exp.Copy,
    exp.TruncateTable,
    exp.LoadData,
    exp.Into,
    exp.Lock,
    exp.Transaction,
    exp.Grant,
    exp.Revoke,
    exp.Use,
    exp.Set,
    exp.Execute,
    exp.Pragma,
    exp.Union,
    exp.Intersect,
    exp.Except,
    exp.Parameter,
    exp.SessionParameter,
)

FORBIDDEN_FUNCTIONS = frozenset(
    {"BENCHMARK", "GET_LOCK", "LOAD_FILE", "RELEASE_LOCK", "SLEEP"}
)


class SQLSafetyValidator:
    """只允许白名单 Schema 上的单条 SELECT/WITH，并统一限制行数。"""

    def __init__(self, max_rows: int = 1000) -> None:
        if max_rows <= 0:
            raise ValueError("max_rows must be positive")
        self.max_rows = max_rows

    def validate(self, sql: str) -> ValidatedSQL:
        """解析、校验并返回规范化且带安全 LIMIT 的 MySQL SQL。"""
        if not sql or not sql.strip():
            raise UnsafeSQLError("SQL is empty")

        try:
            statements = [
                statement
                for statement in sqlglot.parse(sql, read="mysql")
                if statement is not None
            ]
        except ParseError as exc:
            raise UnsafeSQLError("SQL cannot be parsed as MySQL") from exc

        if len(statements) != 1:
            raise UnsafeSQLError("exactly one SQL statement is required")

        statement = statements[0]
        if not isinstance(statement, exp.Select):
            raise UnsafeSQLError("only SELECT or WITH ... SELECT is allowed")

        forbidden = next(
            (node for node in statement.walk() if isinstance(node, FORBIDDEN_NODE_TYPES)),
            None,
        )
        if forbidden is not None:
            raise UnsafeSQLError(
                f"forbidden SQL construct: {type(forbidden).__name__}"
            )

        for function in statement.find_all(exp.Anonymous):
            if function.name.upper() in FORBIDDEN_FUNCTIONS:
                raise UnsafeSQLError(f"forbidden SQL function: {function.name}")
        if statement.find(exp.Star) is not None:
            raise UnsafeSQLError("wildcard columns are forbidden")

        tables = self._validate_tables(statement)
        self._validate_columns(statement)
        self._apply_limit(statement)

        return ValidatedSQL(
            sql=statement.sql(dialect="mysql"),
            tables=tuple(sorted(tables)),
            limit=self._limit_value(statement),
        )

    @staticmethod
    def _validate_tables(statement: exp.Select) -> set[str]:
        tables: set[str] = set()
        for scope in traverse_scope(statement):
            for source in scope.sources.values():
                if isinstance(source, Scope):
                    continue
                if not isinstance(source, exp.Table):
                    raise UnsafeSQLError("unsupported query source")

                database = source.db.lower() if source.db else ""
                catalog = source.catalog.lower() if source.catalog else ""
                if database in SYSTEM_DATABASES or catalog in SYSTEM_DATABASES:
                    raise UnsafeSQLError("system databases are forbidden")
                if database or catalog:
                    raise UnsafeSQLError("database-qualified tables are forbidden")

                table_name = source.name.lower()
                if table_name not in ALLOWED_TABLE_COLUMNS:
                    raise UnsafeSQLError(f"table is not allowlisted: {table_name}")
                tables.add(table_name)

        if not tables:
            raise UnsafeSQLError("query must read at least one allowlisted table")
        return tables

    def _validate_columns(self, statement: exp.Select) -> None:
        for scope in traverse_scope(statement):
            for column in scope.columns:
                self._validate_column(scope, column)

    def _validate_column(self, scope: Scope, column: exp.Column) -> None:
        column_name = column.name.lower()
        qualifier = column.table.lower()

        if qualifier:
            source = scope.sources.get(qualifier)
            if source is None:
                raise UnsafeSQLError(f"unknown table alias: {qualifier}")
            if column_name not in self._source_columns(source):
                raise UnsafeSQLError(
                    f"column is not allowlisted: {qualifier}.{column_name}"
                )
            return

        matches = sum(
            column_name in self._source_columns(source)
            for source in scope.sources.values()
        )
        if matches == 0:
            raise UnsafeSQLError(f"column is not allowlisted: {column_name}")
        if matches > 1:
            raise UnsafeSQLError(f"ambiguous unqualified column: {column_name}")

    @staticmethod
    def _source_columns(source: exp.Table | Scope) -> frozenset[str]:
        if isinstance(source, Scope):
            return frozenset(name.lower() for name in source.expression.named_selects)
        return ALLOWED_TABLE_COLUMNS.get(source.name.lower(), frozenset())

    def _apply_limit(self, statement: exp.Select) -> None:
        limit = statement.args.get("limit")
        if limit is None:
            statement.limit(self.max_rows, copy=False)
            return
        value = self._literal_integer(limit.expression)
        if value <= 0:
            raise UnsafeSQLError("LIMIT must be a positive integer")
        if value > self.max_rows:
            statement.limit(self.max_rows, copy=False)

    @staticmethod
    def _literal_integer(expression: exp.Expression) -> int:
        if not isinstance(expression, exp.Literal) or not expression.is_int:
            raise UnsafeSQLError("LIMIT must be a literal integer")
        return int(expression.this)

    def _limit_value(self, statement: exp.Select) -> int:
        limit = statement.args["limit"]
        return self._literal_integer(limit.expression)


def validate_sql(sql: str, max_rows: int = 1000) -> ValidatedSQL:
    """无状态校验入口，便于工具与测试直接使用。"""
    return SQLSafetyValidator(max_rows=max_rows).validate(sql)
