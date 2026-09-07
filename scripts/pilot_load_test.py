"""使用普通员工账号执行不暴露密码的试点并发问答验收。"""

import argparse
import asyncio
import getpass
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

from app.config import PROJECT_ROOT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an authenticated pilot RAG load test.")
    parser.add_argument("--username", required=True, help="Active employee username.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=240)
    parser.add_argument(
        "--question",
        default="请概括知识库中的核心流程，并给出引用。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/pilot_load_test.json"),
    )
    return parser.parse_args()


def percentile(values: list[float], value: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(value * len(ordered)))
    return round(ordered[rank - 1], 2)


async def run_case(
    client: httpx.AsyncClient,
    csrf_token: str,
    question: str,
    timeout_seconds: float,
    semaphore: asyncio.Semaphore,
    case_number: int,
) -> dict[str, Any]:
    async with semaphore:
        started = perf_counter()
        try:
            created = await client.post(
                "/api/v1/runs",
                headers={"X-CSRF-Token": csrf_token},
                json={"question": question, "top_k": 5},
            )
            created.raise_for_status()
            result_url = created.json()["result_url"]
            deadline = perf_counter() + timeout_seconds
            while perf_counter() < deadline:
                response = await client.get(result_url)
                response.raise_for_status()
                payload = response.json()
                if payload["status"] == "completed":
                    return {
                        "case": case_number,
                        "passed": True,
                        "latency_ms": round((perf_counter() - started) * 1000, 2),
                        "citation_count": len(payload.get("citations") or []),
                        "error": None,
                    }
                if payload["status"] == "failed":
                    raise RuntimeError(payload.get("error", {}).get("code", "run_failed"))
                await asyncio.sleep(0.5)
            raise TimeoutError("run polling timed out")
        except Exception as exc:
            return {
                "case": case_number,
                "passed": False,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
                "citation_count": 0,
                "error": type(exc).__name__,
            }


async def execute(args: argparse.Namespace, password: str) -> dict[str, Any]:
    if args.requests < 1 or args.concurrency < 1:
        raise ValueError("requests and concurrency must be positive")
    if args.concurrency > args.requests:
        raise ValueError("concurrency must not exceed requests")
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=max(30.0, args.timeout_seconds),
        follow_redirects=False,
    ) as client:
        login = await client.post(
            "/api/v1/auth/login",
            headers={"Origin": args.base_url.rstrip("/")},
            json={"username": args.username, "password": password},
        )
        login.raise_for_status()
        user = login.json()["user"]
        if user["role"] != "employee" or user["must_change_password"]:
            raise RuntimeError("load test requires a password-ready employee account")
        config = (await client.get("/api/v1/auth/config")).json()
        csrf_token = client.cookies.get(config["csrf_cookie_name"])
        if not csrf_token:
            raise RuntimeError("login did not issue a CSRF cookie")
        semaphore = asyncio.Semaphore(args.concurrency)
        cases = await asyncio.gather(
            *(
                run_case(
                    client,
                    csrf_token,
                    args.question,
                    args.timeout_seconds,
                    semaphore,
                    index,
                )
                for index in range(1, args.requests + 1)
            )
        )
        try:
            await client.post(
                "/api/v1/auth/logout",
                headers={"X-CSRF-Token": csrf_token},
            )
        except httpx.HTTPError:
            pass

    latencies = [case["latency_ms"] for case in cases if case["passed"]]
    passed_count = sum(bool(case["passed"]) for case in cases)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "username": args.username,
        "request_count": args.requests,
        "concurrency": args.concurrency,
        "passed_count": passed_count,
        "success_rate": round(passed_count / args.requests, 4),
        "p50_latency_ms": percentile(latencies, 0.5),
        "p95_latency_ms": percentile(latencies, 0.95),
        "cases": cases,
    }


def main() -> int:
    args = parse_args()
    password = getpass.getpass("Employee password: ")
    report = asyncio.run(execute(args, password))
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "success_rate": report["success_rate"],
                "p50_latency_ms": report["p50_latency_ms"],
                "p95_latency_ms": report["p95_latency_ms"],
                "report": str(output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed_count"] == report["request_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
