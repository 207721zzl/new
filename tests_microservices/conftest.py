import importlib
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from packages.platform.config import settings

TOKEN = "test-internal-token-with-at-least-32-characters"


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("MODEL_PRELOAD_ENABLED", "false")
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest_asyncio.fixture
async def databases(tmp_path):
    engines = []
    modules = {}
    for domain in ["identity", "chat", "knowledge"]:
        module = importlib.import_module(f"services.{domain}.models")
        db = importlib.import_module(f"services.{domain}.db").database
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / domain}.db")

        @event.listens_for(engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        async with engine.begin() as connection:
            await connection.run_sync(module.Base.metadata.create_all)
        db.__dict__["engine"] = engine
        db.__dict__["session_factory"] = async_sessionmaker(
            engine, expire_on_commit=False
        )
        engines.append((db, engine))
        modules[domain] = module
    knowledge = modules["knowledge"]
    from services.knowledge.db import session_factory

    async with session_factory() as session:
        session.add(
            knowledge.KnowledgeState(state_id=1, collection="test_index", epoch=0)
        )
        await session.commit()
    yield modules
    for db, engine in engines:
        await engine.dispose()
        db.__dict__.pop("engine", None)
        db.__dict__.pop("session_factory", None)
