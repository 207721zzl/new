import asyncio
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from services.knowledge.models import Base
from packages.platform.config import settings

def run(connection):
    context.configure(connection=connection,target_metadata=Base.metadata,compare_type=True)
    with context.begin_transaction():
        context.run_migrations()

async def online():
    value=settings().knowledge_database_url
    if value is None:
        raise ValueError("KNOWLEDGE_DATABASE_URL is required")
    engine=create_async_engine(value.get_secret_value())
    async with engine.connect() as connection:
        await connection.run_sync(run)
    await engine.dispose()

if context.is_offline_mode():
    context.configure(url="mysql+asyncmy://localhost/knowledge",target_metadata=Base.metadata,literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
