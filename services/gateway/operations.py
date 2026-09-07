import asyncio
from datetime import UTC, datetime
from uuid import uuid4, UUID
from fastapi import APIRouter, Depends, Request, HTTPException
from app.config import get_settings
from app.pilot.schemas import PilotCleanupRequest
from packages.platform.auth import require_admin, require_admin_csrf
from packages.platform.client import ServiceClient

router=APIRouter(prefix="/api/v1/admin/pilot")


@router.get("/overview")
async def overview(request: Request, context=Depends(require_admin)):
    domains=["identity","chat","knowledge"]
    responses=await asyncio.gather(*(ServiceClient(d).post("/internal/v1/stats",{}) for d in domains),return_exceptions=True)
    result={"generated_at":datetime.now(UTC).isoformat(),"window_hours":get_settings().pilot_metrics_window_hours,
            "warnings":[],"model_state":"unknown","disk_free_bytes":None}
    for domain,response in zip(domains,responses):
        if isinstance(response,Exception):
            result[domain+"_unavailable"]=True
            result["warnings"].append(f"{domain} 服务统计暂不可用。")
        else:
            result.update(response)
    # Keep unavailable fields explicit; the dashboard must not display a fabricated zero.
    try:
        from packages.platform.config import settings
        health=await request.app.state.http.get(settings().inference_url+"/api/v1/health/ready")
        result["model_state"]="ready" if health.is_success else "not_ready"
    except Exception:
        result["model_state"]="unavailable"
    try:
        from services.gateway.metrics import snapshot
        result["runtime"]=await snapshot(request.app.state.redis)
    except Exception:
        result["runtime"]=None
    return result


async def maintain(request, context, execute):
    operation_id=request.headers.get("Idempotency-Key") or str(uuid4())
    try:
        UUID(operation_id)
    except ValueError:
        raise HTTPException(422,"Idempotency-Key 必须为 UUID。")
    config=get_settings()
    result={"generated_at":datetime.now(UTC).isoformat(),"operation_id":operation_id,
        "conversation_retention_days":config.pilot_conversation_retention_days,
        "session_retention_days":config.pilot_session_retention_days,"audit_retention_days":config.pilot_audit_retention_days}
    payload={"execute":execute,"operation_id":operation_id,"actor_user_id":context.user.user_id}
    domains=["identity","chat"]
    responses=await asyncio.gather(*(ServiceClient(d).post("/internal/v1/maintenance",payload) for d in domains),return_exceptions=True)
    result["services"]={}
    for domain,response in zip(domains,responses):
        if isinstance(response,Exception):
            result["services"][domain]="failed"
        else:
            result.update(response)
            result["services"][domain]="completed"
    result["status"]="completed" if all(v=="completed" for v in result["services"].values()) else "partial_failed"
    return result


@router.get("/maintenance")
async def preview(request: Request, context=Depends(require_admin)):
    return await maintain(request,context,False)


@router.post("/maintenance/cleanup")
async def cleanup(payload: PilotCleanupRequest, request: Request, context=Depends(require_admin_csrf)):
    return await maintain(request,context,True)
