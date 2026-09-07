"""MySQL 独立就绪检查。"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings, get_settings
from app.logging_config import get_logger


logger = get_logger("db.health")


async def check_mysql_connection(settings: Settings | None = None) -> bool:
    """使用只读账号执行 ``SELECT 1``，并立即释放临时连接。"""
    resolved = settings or get_settings()
    engine = create_async_engine(
        resolved.database_read_url_value,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": resolved.database_connect_timeout_seconds,
        },
    )
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(text("SELECT 1"))
            return value == 1
    except Exception:
        logger.exception("readiness check failed dependency=mysql")
        return False
    finally:
        await engine.dispose()
