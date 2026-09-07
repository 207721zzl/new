"""管理账号与只读账号使用的异步 SQLAlchemy 会话。"""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def _create_engine(url: str) -> AsyncEngine:
    """按统一连接池和超时策略创建 MySQL 异步引擎。"""
    settings = get_settings()
    return create_async_engine(
        url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=settings.database_pool_size,
        connect_args={"connect_timeout": settings.database_connect_timeout_seconds},
    )


@lru_cache(maxsize=1)
def get_admin_engine() -> AsyncEngine:
    """返回迁移、灌数等受控写操作使用的管理引擎。"""
    return _create_engine(get_settings().database_url_value)


@lru_cache(maxsize=1)
def get_read_engine() -> AsyncEngine:
    """返回在线父块恢复使用的数据库只读引擎。"""
    return _create_engine(get_settings().database_read_url_value)


@lru_cache(maxsize=1)
def get_admin_session_factory() -> async_sessionmaker[AsyncSession]:
    """返回管理会话工厂。"""
    return async_sessionmaker(get_admin_engine(), expire_on_commit=False)


@lru_cache(maxsize=1)
def get_read_session_factory() -> async_sessionmaker[AsyncSession]:
    """返回只读会话工厂。"""
    return async_sessionmaker(get_read_engine(), expire_on_commit=False)


async def get_read_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：为一次请求提供并自动关闭只读会话。"""
    async with get_read_session_factory()() as session:
        yield session


async def get_admin_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：为认证等受控写操作提供短生命周期会话。"""
    async with get_admin_session_factory()() as session:
        yield session


async def dispose_database_engines() -> None:
    """在应用关闭时释放已创建的数据库连接池。"""
    if get_admin_engine.cache_info().currsize:
        await get_admin_engine().dispose()
    if get_read_engine.cache_info().currsize:
        await get_read_engine().dispose()
    get_admin_session_factory.cache_clear()
    get_read_session_factory.cache_clear()
    get_admin_engine.cache_clear()
    get_read_engine.cache_clear()
