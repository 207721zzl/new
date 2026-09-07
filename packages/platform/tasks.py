"""Transactional dispatch and fenced leases; business models stay in each domain."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import uuid4

from sqlalchemy import String, Integer, DateTime, Text, select, or_
from sqlalchemy.orm import Mapped, mapped_column
from packages.platform.config import settings


def now():
    return datetime.now(UTC).replace(tzinfo=None)


class TaskColumns:
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


@dataclass(frozen=True)
class Lease:
    model: object
    task_id: str
    owner: str
    generation: int


current_lease = ContextVar("current_lease", default=None)


class LeaseLost(Exception):
    pass


async def guard(session):
    lease = current_lease.get()
    if lease is None:
        return
    task = await session.scalar(select(lease.model).where(
        lease.model.task_id == lease.task_id).with_for_update())
    if (task is None or task.status != "running" or task.owner != lease.owner
            or task.generation != lease.generation or task.lease_until <= now()):
        raise LeaseLost("execution lease expired or superseded")


def enqueue(session, model, kind, resource_id, task_id=None):
    task = model(task_id=task_id or str(uuid4()), kind=kind, resource_id=resource_id,
                 status="pending", attempts=0, generation=0, available_at=now())
    session.add(task)
    return task


class TaskRunner:
    def __init__(self, model, session_factory, handler, on_failure):
        self.model, self.sessions = model, session_factory
        self.handler, self.on_failure = handler, on_failure

    async def claim(self, task_id):
        async with self.sessions() as session:
            task = await session.scalar(select(self.model).where(
                self.model.task_id == task_id).with_for_update())
            if task is None or task.status in {"completed", "failed"} or task.available_at > now():
                return None
            if task.status == "running" and task.lease_until and task.lease_until > now():
                return None
            task.owner = str(uuid4())
            task.generation += 1
            task.attempts += 1
            task.status = "running"
            task.lease_until = now() + timedelta(seconds=settings().task_lease_seconds)
            await session.commit()
            return task

    async def heartbeat(self, stop):
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings().task_lease_seconds / 3)
            except TimeoutError:
                async with self.sessions() as session:
                    await guard(session)
                    lease = current_lease.get()
                    task = await session.get(self.model, lease.task_id)
                    task.lease_until = now() + timedelta(seconds=settings().task_lease_seconds)
                    await session.commit()

    async def execute(self, task_id):
        task = await self.claim(task_id)
        if task is None:
            return
        token = current_lease.set(Lease(self.model, task.task_id, task.owner, task.generation))
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self.heartbeat(stop))
        try:
            if task.attempts > settings().task_max_attempts:
                raise RuntimeError("task attempt budget exhausted")
            await self.handler(task)
            async with self.sessions() as session:
                await guard(session)
                row = await session.get(self.model, task_id)
                row.status = "completed"
                row.lease_until = None
                await session.commit()
        except LeaseLost:
            return
        except Exception:
            try:
                if task.attempts >= settings().task_max_attempts:
                    await self.on_failure(task)
                async with self.sessions() as session:
                    await guard(session)
                    row = await session.get(self.model, task_id)
                    row.status = "failed" if task.attempts >= settings().task_max_attempts else "pending"
                    row.error = "执行失败，已达到重试上限。" if row.status == "failed" else "执行失败，等待重试。"
                    row.available_at = now() + timedelta(seconds=min(60, 2 ** task.attempts))
                    row.published_at = None
                    row.lease_until = None
                    await session.commit()
            except LeaseLost:
                pass
        finally:
            stop.set()
            await asyncio.gather(heartbeat, return_exceptions=True)
            current_lease.reset(token)


async def publish_pending(model, sessions, send):
    """Retains rows until terminal, so a lost broker can be repopulated."""
    async with sessions() as session:
        rows = list(await session.scalars(select(model).where(
            model.status.in_(["pending", "running"]), model.available_at <= now(),
            or_(model.lease_until.is_(None), model.lease_until < now()),
            or_(model.published_at.is_(None), model.published_at < now()-timedelta(seconds=30)),
        ).limit(50).with_for_update(skip_locked=True)))
        for row in rows:
            await send(row.task_id)
            row.published_at = now()
        await session.commit()


def load_domain(domain):
    if domain not in {"chat", "knowledge"}:
        raise ValueError("worker domain must be chat or knowledge")
    return import_module(f"services.{domain}.worker")
