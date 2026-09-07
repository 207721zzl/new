import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from services.identity.models import Base, User
from services.identity.db import session_factory
from app.auth.constants import LEGACY_USER_ID
from scripts.migrate_microservices import copy_domain


@pytest.mark.asyncio
async def test_migration_verifies_contents_and_refuses_unrelated_target_changes(
    databases, tmp_path
):
    source = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/source.db")
    async with source.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            User.__table__.insert(),
            [
                dict(
                    user_id=LEGACY_USER_ID,
                    username="legacy",
                    normalized_username="legacy",
                    display_name="旧系统",
                    password_hash="!disabled",
                    role="employee",
                    status="disabled",
                    must_change_password=False,
                    failed_login_count=0,
                ),
                dict(
                    user_id="alice",
                    username="alice",
                    normalized_username="alice",
                    display_name="员工甲",
                    password_hash="already-hashed",
                    role="employee",
                    status="active",
                    must_change_password=False,
                    failed_login_count=0,
                ),
            ],
        )
    async with session_factory() as session:
        session.add(
            User(
                user_id=LEGACY_USER_ID,
                username="bootstrap",
                normalized_username="bootstrap",
                display_name="引导",
                password_hash="!disabled",
                role="employee",
                status="disabled",
                must_change_password=False,
                failed_login_count=0,
            )
        )
        await session.commit()
    async with source.connect() as connection:
        await copy_domain(connection, "identity")
        await copy_domain(connection, "identity")
        async with session_factory() as session:
            user = await session.get(User, "alice")
            assert (
                user.display_name == "员工甲" and user.password_hash == "already-hashed"
            )
            user.display_name = "changed"
            await session.commit()
        with pytest.raises(RuntimeError, match="different data"):
            await copy_domain(connection, "identity")
    await source.dispose()
