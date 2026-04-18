# Docker Smoke Tests

This folder contains a smoke test to verify that the Docker image builds and starts correctly.

## What it checks

1. Docker CLI is installed
2. Docker daemon is running
3. Image builds from the current repository
4. Container starts and becomes healthy

## Run

```bash
python tests/docker/smoke_test.py
```

## Expected output

- `PASS: Docker build and container health checks are working.`

If Docker Desktop is not running, the test exits with a clear message and a hint.