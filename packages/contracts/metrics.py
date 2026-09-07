import math
from app.pilot.schemas import PilotWorkloadSummary


def percentile(values, fraction=.95):
    return round(sorted(values)[max(0, math.ceil(len(values)*fraction)-1)], 2) if values else None


def workload(rows, stale_before):
    counts = dict.fromkeys(["queued", "running", "completed", "failed"], 0)
    durations, stale = [], 0
    for status, start, end, updated in rows:
        status = "queued" if status == "pending" else status
        counts[status] = counts.get(status, 0)+1
        if status in {"queued", "running"} and updated < stale_before:
            stale += 1
        if start and end:
            durations.append(max(0, (end-start).total_seconds()*1000))
    terminal = counts["completed"] + counts["failed"]
    return PilotWorkloadSummary(total=len(rows), **counts, stale=stale,
        success_rate=counts["completed"]/terminal if terminal else None,
        average_duration_ms=sum(durations)/len(durations) if durations else None,
        p95_duration_ms=percentile(durations)).model_dump()
