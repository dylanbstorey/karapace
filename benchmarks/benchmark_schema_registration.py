"""
Copyright (c) 2026 Aiven Ltd
See LICENSE for details

Benchmark for schema registration performance — targets compatibility checking
and schema ID lookup paths in the schema registry.
"""

import asyncio
import httpx
import time
from statistics import mean

import os

SCHEMA_REGISTRY_URL = os.environ.get("SCHEMA_REGISTRY_URL", "http://localhost:8081")
TIMEOUT = 30.0


def make_evolving_schema(version: int) -> dict:
    """Generate backward-compatible Avro schema evolutions by adding optional fields."""
    fields = [
        {"name": "name", "type": "string"},
        {"name": "age", "type": "int"},
    ]
    for i in range(version):
        fields.append({"name": f"field_{i}", "type": ["null", "string"], "default": None})
    schema = {
        "type": "record",
        "name": "User",
        "fields": fields,
    }
    import json

    return {"schema": json.dumps(schema)}


async def benchmark_registration(n_versions: int, subject: str) -> list[float]:
    """Register n_versions of a schema and return per-registration latencies in ms."""
    latencies = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        url = f"{SCHEMA_REGISTRY_URL}/subjects/{subject}/versions"
        for v in range(n_versions):
            schema = make_evolving_schema(v)
            t0 = time.perf_counter()
            r = await client.post(url, json=schema)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)
            assert r.status_code == 200, f"Version {v}: {r.status_code} {r.text}"
    return latencies


async def benchmark_duplicate_registration(n_subjects: int) -> list[float]:
    """Register the same schema under n_subjects different subjects — tests schema ID dedup lookup."""
    import json

    schema = {"schema": json.dumps({"type": "record", "name": "Shared", "fields": [{"name": "id", "type": "int"}]})}
    latencies = []
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for i in range(n_subjects):
            subject = f"bench-dedup-{i}-value"
            url = f"{SCHEMA_REGISTRY_URL}/subjects/{subject}/versions"
            t0 = time.perf_counter()
            r = await client.post(url, json=schema)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            latencies.append(elapsed_ms)
            assert r.status_code == 200, f"Subject {i}: {r.status_code} {r.text}"
    return latencies


async def cleanup(subjects: list[str]) -> None:
    """Delete test subjects, ignoring errors for non-existent subjects."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for subject in subjects:
            try:
                await client.delete(f"{SCHEMA_REGISTRY_URL}/subjects/{subject}?permanent=false")
            except Exception:
                pass
            try:
                await client.delete(f"{SCHEMA_REGISTRY_URL}/subjects/{subject}?permanent=true")
            except Exception:
                pass


def print_stats(name: str, latencies: list[float]) -> None:
    avg = mean(latencies)
    p50 = sorted(latencies)[len(latencies) // 2]
    p95 = sorted(latencies)[int(0.95 * len(latencies))]
    p99 = sorted(latencies)[int(0.99 * len(latencies))] if len(latencies) >= 100 else p95
    total = sum(latencies)
    print(f"  {name}:")
    print(f"    Total: {total:.0f}ms | Avg: {avg:.1f}ms | P50: {p50:.1f}ms | P95: {p95:.1f}ms | P99: {p99:.1f}ms")
    print(f"    Count: {len(latencies)}")


async def wait_for_ready():
    """Wait for schema registry to be ready."""
    async with httpx.AsyncClient(timeout=5) as client:
        for attempt in range(30):
            try:
                r = await client.get(f"{SCHEMA_REGISTRY_URL}/_health")
                if r.status_code == 200:
                    print(f"Schema registry ready (attempt {attempt + 1})")
                    return
            except Exception:
                pass
            await asyncio.sleep(2)
    raise RuntimeError("Schema registry not ready after 60s")


async def main():
    print("=" * 60)
    print("  Schema Registration Benchmark")
    print("=" * 60)

    await wait_for_ready()

    # Test 1: Evolving schema with compatibility checks (FULL mode)
    # Each new version triggers compatibility check against all previous versions
    n_versions = 50
    subject = "bench-compat-test-value"
    print(f"\n--- Test 1: {n_versions} evolving versions (compatibility checks) ---")
    await cleanup([subject])
    latencies = await benchmark_registration(n_versions, subject)
    print_stats("Schema evolution", latencies)
    # Show degradation curve: first 10 vs last 10
    print(f"    First 10 avg: {mean(latencies[:10]):.1f}ms")
    print(f"    Last 10 avg:  {mean(latencies[-10:]):.1f}ms")

    # Test 2: Same schema across many subjects (tests schema ID dedup)
    n_subjects = 200
    print(f"\n--- Test 2: Same schema across {n_subjects} subjects (dedup lookup) ---")
    dedup_subjects = [f"bench-dedup-{i}-value" for i in range(n_subjects)]
    await cleanup(dedup_subjects)
    latencies = await benchmark_duplicate_registration(n_subjects)
    print_stats("Schema dedup", latencies)
    print(f"    First 50 avg:  {mean(latencies[:50]):.1f}ms")
    print(f"    Last 50 avg:   {mean(latencies[-50:]):.1f}ms")

    # Test 3: Re-register existing schemas (should be fast — cache hit)
    print(f"\n--- Test 3: Re-register same {n_versions} versions (cache hit path) ---")
    latencies = await benchmark_registration(n_versions, subject)
    print_stats("Re-registration", latencies)

    # Cleanup
    await cleanup([subject] + dedup_subjects)

    print("\n" + "=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
