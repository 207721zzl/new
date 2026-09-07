import asyncio
import os
from celery import Celery
from packages.platform.config import settings
from packages.platform.tasks import load_domain, publish_pending

domain = os.environ.get("WORKER_DOMAIN", "chat")
celery = Celery(f"evidence_{domain}", broker=settings().broker_url.get_secret_value())
celery.conf.update(task_serializer="json", accept_content=["json"], task_ignore_result=True,
    task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 900}, task_default_queue=domain)


@celery.task(name="evidence.execute")
def execute(task_id):
    module = load_domain(domain)
    async def run():
        try:
            await module.runner().execute(task_id)
        finally:
            from packages.platform.client import close_clients
            await close_clients()
            await module.database.close()
    asyncio.run(run())


async def dispatch():
    module = load_domain(domain)
    async def send(task_id):
        await asyncio.to_thread(celery.send_task, "evidence.execute", args=[task_id], queue=domain)
    while True:
        try:
            await publish_pending(module.DurableTask, module.session_factory, send)
        except Exception:
            from app.logging_config import get_logger
            get_logger("dispatcher").exception("dispatch failed; durable rows retained")
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(dispatch())
