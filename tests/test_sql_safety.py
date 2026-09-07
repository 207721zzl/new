"""SQLGlot MySQL AST 白名单与危险 SQL 阻断测试。"""

import pytest

from app.analysis.safety import SQLSafetyValidator
from app.analysis.schema import ALLOWED_TABLE_COLUMNS
from app.db.base import Base
from app.errors import UnsafeSQLError


def test_normal_select_is_allowlisted_and_gets_limit_1000():
    sql = """
    SELECT COALESCE(SUM(p.paid_amount), 0) AS gmv
    FROM payments AS p
    JOIN orders AS o ON o.id = p.order_id
    WHERE p.payment_status = 'success'
      AND o.region = '华东'
    """

    result = SQLSafetyValidator().validate(sql)

    assert result.tables == ("orders", "payments")
    assert result.limit == 1000
    assert result.sql.endswith("LIMIT 1000")


def test_with_select_and_smaller_limit_are_preserved():
    sql = """
    WITH paid AS (
      SELECT p.order_id, p.paid_amount
      FROM payments AS p
      WHERE p.payment_status = 'success'
    )
    SELECT COALESCE(SUM(paid.paid_amount), 0) AS gmv
    FROM paid
    LIMIT 10
    """

    result = SQLSafetyValidator().validate(sql)

    assert result.tables == ("payments",)
    assert result.limit == 10
    assert result.sql.endswith("LIMIT 10")


def test_limit_above_cap_is_reduced():
    result = SQLSafetyValidator(max_rows=1000).validate(
        "SELECT o.id FROM orders AS o LIMIT 5000"
    )

    assert result.limit == 1000
    assert result.sql.endswith("LIMIT 1000")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders (id) VALUES (1)",
        "UPDATE orders SET order_status = 'paid'",
        "DELETE FROM orders",
        "DROP TABLE orders",
        "SELECT o.id FROM orders AS o; SELECT p.id FROM payments AS p",
        "SELECT t.table_name FROM information_schema.tables AS t",
        "SELECT s.id FROM secret_orders AS s",
        "SELECT o.password FROM orders AS o",
        "SELECT * FROM orders AS o",
        "SELECT o.* FROM orders AS o",
        "SELECT SLEEP(10) FROM orders AS o",
        "SELECT LOAD_FILE('/etc/passwd') FROM orders AS o",
        "SELECT @@version FROM orders AS o",
        "SELECT o.id FROM orders AS o FOR UPDATE",
        "SELECT 1",
        "SELECT o.id FROM insight_agent.orders AS o",
    ],
)
def test_dangerous_or_non_allowlisted_sql_is_rejected(sql):
    with pytest.raises(UnsafeSQLError):
        SQLSafetyValidator().validate(sql)


def test_malformed_sql_is_rejected():
    with pytest.raises(UnsafeSQLError, match="parsed"):
        SQLSafetyValidator().validate("SELECT FROM WHERE")


def test_schema_whitelist_is_kept_in_sync_with_orm_models():
    for table_name, columns in ALLOWED_TABLE_COLUMNS.items():
        assert table_name in Base.metadata.tables
        assert columns == set(Base.metadata.tables[table_name].columns.keys())
