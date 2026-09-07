from functools import cached_property
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from packages.platform.config import settings


class Database:
    """Each instance receives only one domain's credentials. No fallback to the legacy DB."""
    def __init__(self, domain):
        self.domain = domain

    @cached_property
    def engine(self):
        secret = getattr(settings(), f"{self.domain}_database_url")
        if secret is None:
            raise ValueError(f"{self.domain.upper()}_DATABASE_URL is required")
        return create_async_engine(secret.get_secret_value(), pool_pre_ping=True)

    @cached_property
    def session_factory(self):
        return async_sessionmaker(self.engine, expire_on_commit=False)

    async def session(self):
        async with self.session_factory() as session:
            yield session

    async def close(self):
        if "engine" in self.__dict__:
            await self.engine.dispose()
