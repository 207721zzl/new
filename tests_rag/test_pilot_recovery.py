import asyncio
from datetime import UTC, datetime

from app.db.models import AgentRun, Conversation, IndexingJob
from app.pilot.recovery import reconcile_interrupted_tasks


class _Rows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class FakeSession:
    def __init__(self, run, conversation, job) -> None:
        self.run = run
        self.conversation = conversation
        self.job = job
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return _Rows([(self.run, self.conversation)])

    async def scalar(self, _statement):
        return None

    async def scalars(self, _statement):
        return [self.job]

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


def test_reconcile_interrupted_tasks_turns_orphans_into_audited_failures() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    conversation = Conversation(
        conversation_id="conversation-1",
        user_id="employee-1",
        title="test",
        created_at=now,
        updated_at=now,
    )
    run = AgentRun(
        run_id="run-1",
        conversation_id=conversation.conversation_id,
        turn_index=1,
        question="test",
        status="running",
        current_node="retrieve_knowledge",
        events=[{"sequence": 2}],
        created_at=now,
        updated_at=now,
    )
    job = IndexingJob(
        job_id="job-1",
        created_by_user_id="admin-1",
        source_path="source.pdf",
        status="pending",
        recreate=False,
        document_count=0,
        chunk_count=0,
        created_at=now,
        updated_at=now,
    )
    session = FakeSession(run, conversation, job)

    result = asyncio.run(reconcile_interrupted_tasks(lambda: session))

    assert result == {"agent_runs": 1, "indexing_jobs": 1}
    assert run.status == "failed"
    assert run.error_code == "run_interrupted_by_restart"
    assert run.events[-1]["sequence"] == 3
    assert job.status == "failed"
    assert session.committed is True
    assert len(session.added) == 2  # assistant failure message + system audit
