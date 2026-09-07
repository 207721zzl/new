import time
from datetime import UTC, datetime
from packages.contracts.metrics import percentile


async def record(redis, status, duration):
    pipe=redis.pipeline(transaction=True)
    pipe.hsetnx("metrics:requests","started",str(time.time()))
    pipe.hincrby("metrics:requests","total",1)
    if status>=500:
        pipe.hincrby("metrics:requests","5xx",1)
    elif status>=400:
        pipe.hincrby("metrics:requests","4xx",1)
    pipe.lpush("metrics:latency",str(duration))
    pipe.ltrim("metrics:latency",0,999)
    await pipe.execute()


async def snapshot(redis):
    data=await redis.hgetall("metrics:requests")
    samples=[float(v) for v in await redis.lrange("metrics:latency",0,999)]
    started=float(data.get("started",time.time()))
    return {"started_at":datetime.fromtimestamp(started,UTC).isoformat(),"uptime_seconds":max(0,time.time()-started),
        "requests_total":int(data.get("total",0)),"responses_4xx":int(data.get("4xx",0)),"responses_5xx":int(data.get("5xx",0)),
        "latency_sample_count":len(samples),"average_latency_ms":sum(samples)/len(samples) if samples else None,
        "p95_latency_ms":percentile(samples)}
