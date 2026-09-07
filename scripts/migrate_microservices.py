"""Copy a quiesced legacy database into empty isolated domains; source is never modified.

Run inside the migration container with SOURCE_DATABASE_URL and target domain URLs.
Without --execute this only reports source counts. Source uploads are reconstructed
from persisted document content and structural parent blocks, then indexed by the new worker.
"""
import argparse
import asyncio
from datetime import date, datetime
from importlib import import_module
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4
import hashlib

from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import create_async_engine
from app.auth.constants import LEGACY_USER_ID

TABLES={"identity":["users","auth_sessions","admin_audit_logs"],
        "chat":["conversations","agent_runs","messages","user_feedback"],
        "knowledge":["indexing_jobs"]}


async def inspect_source(connection):
    counts={}
    for table in [*TABLES['identity'],*TABLES['chat'],'documents','indexing_jobs']:
        counts[table]=int((await connection.execute(text(f"SELECT COUNT(*) FROM `{table}`"))).scalar_one())
    active=int((await connection.execute(text("SELECT COUNT(*) FROM agent_runs WHERE status IN ('queued','running')"))).scalar_one())
    active+=int((await connection.execute(text("SELECT COUNT(*) FROM indexing_jobs WHERE status IN ('pending','running')"))).scalar_one())
    return {"counts":counts,"active_tasks":active}


async def copy_domain(connection,domain):
    module=import_module(f"services.{domain}.models")
    database=import_module(f"services.{domain}.db").database
    # All writes for one domain are transactional. Resume only when contents match exactly.
    async with database.engine.begin() as destination:
        for name in TABLES[domain]:
            table=module.Base.metadata.tables[name]
            rows=(await connection.execute(text(f"SELECT * FROM `{name}`"))).mappings().all()
            def normalize(row):
                values={c.name:row[c.name] for c in table.columns if c.name in row}
                for column in table.columns:
                    if column.name not in values:
                        continue
                    if column.type.__class__.__name__=='JSON' and isinstance(values[column.name],str):
                        values[column.name]=json.loads(values[column.name])
                    if column.type.__class__.__name__=='Boolean' and values[column.name] is not None:
                        values[column.name]=bool(values[column.name])
                    if column.type.__class__.__name__=='DateTime' and isinstance(values[column.name],str):
                        values[column.name]=datetime.fromisoformat(values[column.name])
                return values
            def fingerprint(values):
                return hashlib.sha256(json.dumps(sorted([normalize(v) for v in values],key=lambda v:str([v[c.name] for c in table.primary_key])),
                    sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()
            existing=(await destination.execute(select(table))).mappings().all()
            if existing and fingerprint(existing)==fingerprint(rows):
                continue
            bootstrap_only=name=='users' and len(existing)==1 and existing[0]['user_id']==LEGACY_USER_ID
            if existing and not bootstrap_only:
                raise RuntimeError(f"Target {domain}.{name} contains different data; refusing to overwrite")
            for row in rows:
                if name=='users' and row['user_id']==LEGACY_USER_ID:
                    await destination.execute(table.update().where(table.c.user_id==LEGACY_USER_ID).values(**normalize(row)))
                    continue
                await destination.execute(table.insert().values(**normalize(row)))
            expected=len(rows)
            actual=await destination.scalar(select(func.count()).select_from(table))
            if actual!=expected:
                raise RuntimeError(f"Row count mismatch: {domain}.{name}")
            copied=(await destination.execute(select(table))).mappings().all()
            if fingerprint(copied)!=fingerprint(rows):
                raise RuntimeError(f"Content fingerprint mismatch: {domain}.{name}")


def json_value(value):
    if isinstance(value,(date,datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


async def import_documents(connection):
    from services.knowledge.db import session_factory
    from services.knowledge.models import DocumentHead
    from services.knowledge.store import IndexingJobStore
    async with session_factory() as session:
        if await session.scalar(select(func.count()).select_from(DocumentHead)):
            raise RuntimeError("Knowledge destination is not empty")
    rows=(await connection.execute(text("SELECT * FROM documents WHERE index_status='completed'"))).mappings().all()
    batch_ids=[]
    for row in rows:
        parents=(await connection.execute(text("SELECT * FROM document_parent_chunks WHERE document_id=:id AND index_status='completed' ORDER BY parent_index"),{'id':row['document_id']})).mappings().all()
        document={key:row.get(key) for key in ['document_id','title','content','doc_type','metric_name','source','version','original_filename','mime_type','content_sha256','language']}
        document['updated_at']=row['source_updated_at'].date().isoformat()
        metadata=row.get('metadata') or {}
        document['metadata']=json.loads(metadata) if isinstance(metadata,str) else metadata
        document['blocks']=[{'text':p['content'],'block_type':p['block_type'],
            'section_path':json.loads(p['section_path']) if isinstance(p['section_path'],str) else p['section_path'],
            'page_start':p['page_start'],'page_end':p['page_end']} for p in parents]
        # Preserve structural context without requiring the old host's upload paths.
        with TemporaryDirectory(prefix='evidence-migration-') as temporary:
            path=Path(temporary)/'migrated.json'
            path.write_text(json.dumps([document],ensure_ascii=False,default=json_value),encoding='utf-8')
            batch=SimpleNamespace(batch_id=str(uuid4()),files=[SimpleNamespace(source_path=path,original_filename='migrated.json',
                file_size_bytes=path.stat().st_size,content_sha256=hashlib.sha256(path.read_bytes()).hexdigest())])
            await IndexingJobStore().create_batch(batch,created_by_user_id=LEGACY_USER_ID,recreate=False)
            batch_ids.append(batch.batch_id)
    return batch_ids


async def migrate(execute=False, source_quiesced=False):
    source=os.environ.get('SOURCE_DATABASE_URL')
    if not source:
        raise RuntimeError('SOURCE_DATABASE_URL must be supplied through the environment')
    engine=create_async_engine(source,pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            # MySQL consistent read snapshot; source must also be quiesced before cutover.
            await connection.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
            await connection.execute(text('START TRANSACTION READ ONLY, WITH CONSISTENT SNAPSHOT'))
            report=await inspect_source(connection)
            print(json.dumps(report,ensure_ascii=False))
            if not execute:
                return
            if not source_quiesced:
                raise RuntimeError('Execution requires --source-quiesced after stopping source API writes')
            if report['active_tasks']:
                raise RuntimeError('Source still has active tasks; drain it before migration')
            for domain in TABLES:
                await copy_domain(connection,domain)
            batches=await import_documents(connection)
            report['knowledge_batches']=batches
            report['status']='copied_pending_reindex'
            destination=Path('reports/microservices-migration.json')
            destination.parent.mkdir(exist_ok=True)
            destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print('Domain rows copied and verified. Wait for reindex completion before routing traffic.')
    finally:
        await engine.dispose()
        for domain in ['identity','chat','knowledge']:
            await import_module(f'services.{domain}.db').database.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--source-quiesced',action='store_true')
    args=parser.parse_args()
    asyncio.run(migrate(args.execute,args.source_quiesced))
