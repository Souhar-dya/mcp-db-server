#!/usr/bin/env python3
"""Docker smoke test for MCP Database Server.

This test validates:
1) Docker daemon availability
2) Container image build
3) Container startup and health check status
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
IMAGE_TAG = "mcp-db-server:smoke-test"
CONTAINER_NAME = "mcp-db-server-smoke"


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def require_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode != 0:
        print(f"FAIL: {action}")
        if result.stdout:
            print("stdout:\n" + result.stdout)
        if result.stderr:
            print("stderr:\n" + result.stderr)
        raise SystemExit(1)


def cleanup() -> None:
    run(["docker", "rm", "-f", CONTAINER_NAME], timeout=60)


def main() -> int:
    print("Checking Docker CLI...")
    docker_version = run(["docker", "--version"], timeout=30)
    require_success(docker_version, "docker --version")
    print(docker_version.stdout.strip())

    print("Checking Docker daemon...")
    docker_info = run(["docker", "info", "--format", "{{json .ServerVersion}}"], timeout=30)
    if docker_info.returncode != 0:
        print("FAIL: Docker daemon is not available.")
        if docker_info.stderr:
            print(docker_info.stderr.strip())
        print("Hint: Start Docker Desktop (or Docker Engine) and re-run this test.")
        return 1
    try:
        server_version = json.loads(docker_info.stdout.strip())
    except json.JSONDecodeError:
        server_version = docker_info.stdout.strip()
    print(f"Docker daemon version: {server_version}")

    print("Cleaning previous smoke container (if any)...")
    cleanup()

    print("Building Docker image...")
    build = run(["docker", "build", "-t", IMAGE_TAG, "."], timeout=1800)
    require_success(build, "docker build")

    print("Starting container...")
    run_container = run(
        [
            "docker",
            "run",
            "-d",
            "-i",
            "--name",
            CONTAINER_NAME,
            "-e",
            "DATABASE_URL=sqlite+aiosqlite:////data/smoke.db",
            IMAGE_TAG,
        ],
        timeout=120,
    )
    require_success(run_container, "docker run")

    print("Waiting for health status...")
    max_wait_seconds = 90
    started = time.time()
    health_status = "unknown"

    while time.time() - started < max_wait_seconds:
        inspect = run(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}",
                CONTAINER_NAME,
            ],
            timeout=30,
        )
        if inspect.returncode == 0:
            health_status = inspect.stdout.strip()
            if health_status == "healthy":
                break
            if health_status in {"unhealthy", "exited", "dead"}:
                break
        time.sleep(3)

    if health_status != "healthy":
        print(f"FAIL: container health status is '{health_status}'")
        logs = run(["docker", "logs", CONTAINER_NAME], timeout=60)
        if logs.stdout:
            print("Container logs:\n" + logs.stdout)
        if logs.stderr:
            print("Container log errors:\n" + logs.stderr)
        cleanup()
        return 1

    print("PASS: Docker build and container health checks are working.")
    cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())