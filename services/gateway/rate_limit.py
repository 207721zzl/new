import hashlib
from uuid import uuid4
from app.config import get_settings

SCRIPT = """
local time = redis.call('TIME')
local now = time[1]*1000 + math.floor(time[2]/1000)
local window = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now-window)
local count = redis.call('ZCARD', KEYS[1])
if count >= tonumber(ARGV[2]) then
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return math.max(1, math.ceil((tonumber(first[2])+window-now)/1000))
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window)
return 0
"""


async def check(redis, request):
    config = get_settings()
    if not config.pilot_rate_limit_enabled or request.method != "POST":
        return 0
    path = request.url.path
    policy = {
        "/api/v1/auth/login": ("login", config.pilot_login_requests_per_5_minutes, 300),
        "/api/v1/auth/register": ("registration", config.pilot_registration_requests_per_hour, 3600),
        "/api/v1/knowledge/uploads": ("upload", config.pilot_upload_requests_per_hour, 3600),
    }.get(path)
    if path in {"/api/v1/runs", "/api/v1/answers", "/api/v1/knowledge/answers", "/api/v1/retrieval/search", "/api/v1/feedback"}:
        policy = ("query", config.pilot_query_requests_per_minute, 60)
    if policy is None:
        return 0
    scope, limit, window = policy
    client = request.client.host if request.client else "unknown"
    if scope in {"query", "upload"}:
        client += ":" + request.cookies.get(config.auth_session_cookie_name, "anonymous")
    key = "rate:"+scope+":"+hashlib.sha256(client.encode()).hexdigest()
    return int(await redis.eval(SCRIPT, 1, key, window*1000, limit, str(uuid4())))
