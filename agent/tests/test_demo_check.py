"""Tests for demo_check: the demo beats as one command (Task L4.3).

Verifies:
1. All beats pass in fixture mode (--llm off).
2. A deliberately broken expectation fails with a clear message.
3. Two runs end with identical state counts (returns, approvals, refunds, cases, notices).
4. --mode http without the demo environment variables is refused.
5. No token or secret key appears anywhere in the output.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
DEMO_CHECK_SCRIPT = SCRIPTS_DIR / "demo_check.py"

# Regex for common JWT structure (header.payload.signature)
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+", re.IGNORECASE)


def _run_demo_check(
    args: list[str],
    extra_env: dict[str, str] | None = None,
    clean_env: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Helper to execute scripts/demo_check.py via subprocess."""
    base_env = os.environ.copy() if not clean_env else {}
    # Ensure PYTHONPATH includes agent/src and repo root
    agent_dir = REPO_ROOT / "agent"
    base_env["PYTHONPATH"] = f"{agent_dir / 'src'}{os.pathsep}{agent_dir}{os.pathsep}{REPO_ROOT}"
    base_env["STORE_MODE"] = "fixtures"
    base_env["DEV_MODE"] = "true"
    if extra_env:
        base_env.update(extra_env)

    cmd = [sys.executable, str(DEMO_CHECK_SCRIPT)] + args
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT / "agent"),
        env=base_env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_demo_check_all_beats_pass() -> None:
    """1. All beats pass in fixture mode with --llm off."""
    assert DEMO_CHECK_SCRIPT.is_file(), f"Expected {DEMO_CHECK_SCRIPT} to exist"
    res = _run_demo_check(["--mode", "fixtures", "--llm", "off", "--runs", "1"])
    combined = res.stdout + "\n" + res.stderr
    assert res.returncode == 0, f"demo_check failed with exit code {res.returncode}:\n{combined}"

    # Verify pass lines for beats
    assert "B1 PASS" in res.stdout, f"Expected B1 PASS in output:\n{res.stdout}"
    assert "B2 PASS" in res.stdout, f"Expected B2 PASS in output:\n{res.stdout}"
    assert "B3 PASS" in res.stdout, f"Expected B3 PASS in output:\n{res.stdout}"
    assert "B4 PASS" in res.stdout, f"Expected B4 PASS in output:\n{res.stdout}"
    assert "req_id=" in res.stdout, "Expected request id in beat output lines"
    assert "time=" in res.stdout, "Expected timing in beat output lines"


def test_demo_check_broken_expectation_fails_with_clear_message() -> None:
    """2. A deliberately broken expectation fails with a clear message and non-zero exit code."""
    assert DEMO_CHECK_SCRIPT.is_file(), f"Expected {DEMO_CHECK_SCRIPT} to exist"
    # Break expectation by forcing policy v1 (where B2 must fail on policy v3 expectation)
    res = _run_demo_check(
        ["--mode", "fixtures", "--llm", "off", "--runs", "1"],
        extra_env={"POLICY_ACTIVE_VERSION": "v1"},
    )
    combined = res.stdout + "\n" + res.stderr
    assert res.returncode != 0, f"Expected non-zero exit code, got 0:\n{combined}"
    assert "B2 FAIL" in combined, f"Expected B2 FAIL in output:\n{combined}"
    msg_found = "policy v3" in combined.lower() or "expected policy" in combined.lower()
    assert msg_found, f"Expected clear failure message in output:\n{combined}"


def test_demo_check_two_runs_identical_state() -> None:
    """3. Two runs end with identical state counts (returns, approvals, refunds, cases, notices)."""
    assert DEMO_CHECK_SCRIPT.is_file(), f"Expected {DEMO_CHECK_SCRIPT} to exist"
    res = _run_demo_check(["--mode", "fixtures", "--llm", "off", "--runs", "2"])
    combined = res.stdout + "\n" + res.stderr
    assert res.returncode == 0, f"demo_check failed with exit code {res.returncode}:\n{combined}"

    # Both runs should complete all beats
    assert res.stdout.count("B1 PASS") >= 2, f"Expected at least 2 B1 PASS:\n{res.stdout}"
    assert res.stdout.count("B2 PASS") >= 2, f"Expected at least 2 B2 PASS:\n{res.stdout}"
    assert res.stdout.count("B3 PASS") >= 2, f"Expected at least 2 B3 PASS:\n{res.stdout}"
    assert res.stdout.count("B4 PASS") >= 2, f"Expected at least 2 B4 PASS:\n{res.stdout}"
    counts_matched = (
        "identical state counts" in res.stdout.lower() or "state counts match" in res.stdout.lower()
    )
    assert counts_matched, f"Expected state counts to match:\n{res.stdout}"


def test_demo_check_http_without_env_refused() -> None:
    """4. --mode http without the demo environment variables is refused."""
    assert DEMO_CHECK_SCRIPT.is_file(), f"Expected {DEMO_CHECK_SCRIPT} to exist"
    # Run with empty environment missing STORE_BASE_URL, STORE_DEMO_PASSWORD, etc.
    res = _run_demo_check(
        ["--mode", "http"],
        clean_env=True,
        extra_env={
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", "C:\\Windows"),
        },
    )
    combined = res.stdout + "\n" + res.stderr
    assert res.returncode != 0, f"Expected refusal (exit != 0), got 0:\n{combined}"
    refusal_found = (
        "refused" in combined.lower()
        or "missing" in combined.lower()
        or "environment" in combined.lower()
    )
    assert refusal_found, f"Expected refusal message in output:\n{combined}"


def test_demo_check_no_token_in_output() -> None:
    """5. Never print tokens, Bearer headers, or secret keys in output."""
    assert DEMO_CHECK_SCRIPT.is_file(), f"Expected {DEMO_CHECK_SCRIPT} to exist"
    res = _run_demo_check(["--mode", "fixtures", "--llm", "off", "--runs", "1"])
    combined = res.stdout + "\n" + res.stderr

    # Check for raw JWT tokens
    jwt_matches = JWT_PATTERN.findall(combined)
    assert not jwt_matches, f"Found raw JWT in output: {jwt_matches}"

    # Check for Bearer tokens
    bearer_matches = BEARER_PATTERN.findall(combined)
    assert not bearer_matches, f"Found Bearer token in output: {bearer_matches}"

    # Check for common secret keywords printed with values
    assert "whsec_" not in combined, "Webhook secret leaked in output"
    assert "Demo@12345" not in combined, "Demo password leaked in output"
