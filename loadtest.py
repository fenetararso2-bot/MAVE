"""Small load test for a MAVE API (standard library only).

    python loadtest.py --base-url http://127.0.0.1:8000 --concurrency 20 --duration 30
    python loadtest.py --base-url https://staging.example.org --i-own-this-server --requests 2000 --max-p95-ms 800

It sends a weighted mix of public GET requests (search, suggest, trending, health), then prints throughput,
error counts and latency percentiles. The exit code is 1 when a threshold (--max-p95-ms / --max-error-rate) is missed,
so it can run in CI against a staging deployment.

Two things to know before reading the numbers:
* MAVE rate-limits per client IP (MAVE_RATE_LIMIT, default 90/min). A load test comes from one IP, so on staging set
  MAVE_RATE_LIMIT to something very high; otherwise most responses are 429. 429s are reported separately as
  "throttled" and do NOT count as errors, but they do mean you are measuring the limiter, not the search.
* Only run this against a server you own. Hosts outside loopback / private networks need --i-own-this-server.
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import itertools
import json
import math
import random
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

QUERIES = [
    "afaan oromoo", "barnoota", "oromiyaa", "kotlin", "python tutorial", "news today", "fayyaa", "ethiopia history",
    "weather", "barattoota", "ijaarsa", "technology", "gadaa", "football results",
]
# (weight, path template). {q} is replaced by a URL-encoded random query.
MIX = [
    (60, "/api/v1/search?q={q}&type=web&page=1"),
    (10, "/api/v1/search?q={q}&type=web&page=2"),
    (15, "/api/v1/suggest?q={q}"),
    (10, "/api/v1/trending"),
    (5, "/health"),
]


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile of an already sorted list (p in 0..100). Empty list -> 0."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(p / 100.0 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def is_private_host(host: str) -> bool:
    """True for loopback / private-network targets (safe to load-test without an explicit flag)."""
    if host in ("localhost", ""):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    return bool(addrs) and all(a.is_loopback or a.is_private for a in addrs)


def pick_path(rng: random.Random) -> str:
    """One request path drawn from the weighted MIX with a random query."""
    tpl = rng.choices([t for _, t in MIX], weights=[w for w, _ in MIX], k=1)[0]
    return tpl.format(q=quote(rng.choice(QUERIES)))


class Result:
    __slots__ = ("seconds", "status", "error")

    def __init__(self, seconds: float, status: int, error: str | None = None):
        self.seconds, self.status, self.error = seconds, status, error


class Worker(threading.local):
    conn: http.client.HTTPConnection | None = None


def _fetch(worker: Worker, scheme: str, host: str, port: int | None, path: str, timeout: float) -> Result:
    started = time.perf_counter()
    for attempt in (1, 2):  # one retry on a dropped keep-alive connection
        try:
            if worker.conn is None:
                cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
                worker.conn = cls(host, port, timeout=timeout)
            worker.conn.request("GET", path, headers={"User-Agent": "mave-loadtest/1", "Accept": "application/json"})
            resp = worker.conn.getresponse()
            resp.read()
            return Result(time.perf_counter() - started, resp.status)
        except (http.client.HTTPException, OSError) as exc:
            try:
                worker.conn.close()  # type: ignore[union-attr]
            except Exception:
                pass
            worker.conn = None
            if attempt == 2:
                return Result(time.perf_counter() - started, 0, type(exc).__name__)
    return Result(time.perf_counter() - started, 0, "unknown")  # unreachable, keeps type checkers calm


def run_load(base_url: str, concurrency: int = 10, duration: float | None = None, requests: int | None = None,
             timeout: float = 10.0, seed: int | None = None) -> dict:
    """Run the load and return a summary dict. Stops after ``requests`` requests or ``duration`` seconds."""
    if not duration and not requests:
        raise ValueError("give duration or requests")
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("base URL must look like http://host:port")
    host, port, scheme = parts.hostname, parts.port, parts.scheme
    base_path = parts.path.rstrip("/")
    rng = random.Random(seed)
    results: list[Result] = []
    lock = threading.Lock()
    counter = itertools.count()
    deadline = time.perf_counter() + duration if duration else None
    local = Worker()

    def work() -> None:
        try:
            while True:
                i = next(counter)
                if requests is not None and i >= requests:
                    return
                if deadline is not None and time.perf_counter() >= deadline:
                    return
                with lock:
                    path = base_path + pick_path(rng)
                r = _fetch(local, scheme, host, port, path, timeout)
                with lock:
                    results.append(r)
        finally:  # this worker thread is done: release its keep-alive connection
            if local.conn is not None:
                local.conn.close()
                local.conn = None

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for f in [pool.submit(work) for _ in range(max(1, concurrency))]:
            f.result()
    wall = max(time.perf_counter() - started, 1e-9)
    return summarise(results, wall)


def summarise(results: list[Result], wall: float) -> dict:
    ok = [r for r in results if 200 <= r.status < 400]
    throttled = [r for r in results if r.status == 429]
    errors = [r for r in results if r.status == 0 or r.status >= 500 or (400 <= r.status < 500 and r.status != 429)]
    lat = sorted(r.seconds * 1000 for r in ok)  # latency of successful requests only
    by_status: dict[str, int] = {}
    for r in results:
        k = str(r.status) if r.status else "connection-error"
        by_status[k] = by_status.get(k, 0) + 1
    total = len(results)
    return {
        "total": total,
        "ok": len(ok),
        "throttled_429": len(throttled),
        "errors": len(errors),
        "error_rate": (len(errors) / total) if total else 0.0,
        "rps": total / wall,
        "seconds": wall,
        "latency_ms": {"p50": percentile(lat, 50), "p90": percentile(lat, 90), "p95": percentile(lat, 95),
                       "p99": percentile(lat, 99), "max": lat[-1] if lat else 0.0},
        "by_status": dict(sorted(by_status.items())),
    }


def verdict(summary: dict, max_p95_ms: float | None, max_error_rate: float | None) -> list[str]:
    """Reasons the run failed (empty list = pass)."""
    problems = []
    if summary["total"] == 0:
        problems.append("no requests were sent")
    elif summary["ok"] == 0:
        problems.append("no request succeeded")
    if max_p95_ms is not None and summary["ok"] and summary["latency_ms"]["p95"] > max_p95_ms:
        problems.append(f"p95 {summary['latency_ms']['p95']:.0f} ms > limit {max_p95_ms:.0f} ms")
    if max_error_rate is not None and summary["error_rate"] > max_error_rate:
        problems.append(f"error rate {summary['error_rate']:.2%} > limit {max_error_rate:.2%}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load test a MAVE API.")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--duration", type=float, help="seconds to run (default 20 when --requests is not given)")
    ap.add_argument("--requests", type=int, help="total requests instead of a duration")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--max-p95-ms", type=float, help="fail (exit 1) when the p95 latency of successful requests is higher")
    ap.add_argument("--max-error-rate", type=float, help="fail when errors/total is higher, e.g. 0.01 = 1%%")
    ap.add_argument("--i-own-this-server", action="store_true", help="required for hosts outside loopback/private networks")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)

    if args.concurrency < 1 or args.concurrency > 500:
        ap.error("--concurrency must be between 1 and 500")
    if args.requests is not None and args.requests < 1:
        ap.error("--requests must be at least 1")
    duration = args.duration if args.duration is not None else (None if args.requests else 20.0)
    parts = urlsplit(args.base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        ap.error("--base-url must look like http://host:port")
    if not args.i_own_this_server and not is_private_host(parts.hostname):
        print(f"refusing to load-test {parts.hostname}: it is not a loopback/private address. "
              "Only test servers you own, then add --i-own-this-server.", file=sys.stderr)
        return 2

    summary = run_load(args.base_url, args.concurrency, duration, args.requests, args.timeout)
    problems = verdict(summary, args.max_p95_ms, args.max_error_rate)
    if args.json:
        print(json.dumps({**summary, "problems": problems}, indent=2))
    else:
        lat = summary["latency_ms"]
        print(f"requests {summary['total']} in {summary['seconds']:.1f}s = {summary['rps']:.1f} req/s")
        print(f"ok {summary['ok']}  throttled(429) {summary['throttled_429']}  errors {summary['errors']} ({summary['error_rate']:.2%})")
        print(f"latency ms (ok only): p50 {lat['p50']:.0f}  p90 {lat['p90']:.0f}  p95 {lat['p95']:.0f}  p99 {lat['p99']:.0f}  max {lat['max']:.0f}")
        print("status codes:", summary["by_status"])
        if summary["throttled_429"] > summary["total"] * 0.2:
            print("note: many 429s - raise MAVE_RATE_LIMIT on the server under test, or you are measuring the rate limiter.")
        for p in problems:
            print("FAIL:", p)
        if not problems:
            print("PASS")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
