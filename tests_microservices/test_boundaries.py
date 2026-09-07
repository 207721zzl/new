import ast
import json
import subprocess
import sys
from pathlib import Path
import httpx
import pytest
from tests_microservices.conftest import TOKEN


@pytest.mark.asyncio
async def test_internal_services_reject_missing_service_credentials():
    from services.identity.api import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://identity") as client:
        response=await client.post("/internal/v1/authenticate",json={"csrf":False},headers={"X-User-Id":"admin","X-User-Role":"admin"})
        assert response.status_code==401
        assert (await client.get("/api/v1/health")).status_code==200


def test_domains_do_not_import_other_domain_orm_or_legacy_repositories():
    for domain in ["identity","chat","knowledge","gateway","inference"]:
        for path in Path("services",domain).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node,ast.ImportFrom):
                    module=node.module or ""
                    assert not module.startswith("app.db"), (path,module)
                    if module.startswith("services.") and module.endswith((".models",".repositories",".db")):
                        assert module.split('.')[1]==domain, (path,module)


def test_cpu_service_imports_do_not_load_gpu_libraries():
    code="import sys; import services.identity.api,services.chat.api,services.knowledge.api,services.gateway.api; assert 'torch' not in sys.modules; assert 'FlagEmbedding' not in sys.modules"
    result=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr


def test_public_contract_paths_preserved():
    from fastapi.openapi.utils import get_openapi
    from services.identity.api import app as identity
    from services.chat.api import app as chat
    from services.knowledge.api import app as knowledge
    from services.gateway.api import app as gateway
    current=set()
    for app in [identity,chat,knowledge,gateway]:
        schema=get_openapi(title=app.title,version=app.version,routes=app.routes)
        for path,operations in schema["paths"].items():
            for method in operations:
                current.add((path,method.lower()))
    legacy=json.loads(Path("packages/contracts/public-api.json").read_text(encoding="utf-8"))
    for path,operations in legacy["paths"].items():
        for method in operations:
            if method in {"get","post","put","patch","delete"}:
                assert (path,method) in current,(path,method)


@pytest.mark.asyncio
async def test_identity_login_csrf_and_session_revocation(databases):
    from services.identity.api import app
    from services.identity.db import session_factory
    from services.identity.models import User
    from app.auth.security import hash_password
    from app.config import get_settings
    async with session_factory() as session:
        session.add(User(user_id="employee",username="employee",normalized_username="employee",display_name="员工",
            password_hash=hash_password("Password12345!"),role="employee",status="active",must_change_password=False,failed_login_count=0))
        await session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://identity",headers={"X-Service-Token":TOKEN}) as client:
        response=await client.post("/api/v1/auth/login",json={"username":"employee","password":"Password12345!"})
        assert response.status_code==200,response.text
        assert len(response.headers.get_list("set-cookie"))==2
        assert (await client.post("/internal/v1/authenticate",json={"csrf":True})).status_code==403
        csrf=client.cookies.get(get_settings().auth_csrf_cookie_name)
        response=await client.post("/internal/v1/authenticate",json={"csrf":True},headers={"X-CSRF-Token":csrf})
        assert response.status_code==200,response.text
        assert response.json()["user_id"]=="employee"
        assert (await client.post("/api/v1/auth/logout",headers={"X-CSRF-Token":csrf})).status_code==204
        assert (await client.get("/api/v1/auth/me")).status_code==401
