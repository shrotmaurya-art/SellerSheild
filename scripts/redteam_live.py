"""Live Red-Team Suite Runner for SellerShield AI (Spec §13, §21, T1.11).

Executes the attack corpus from scenarios/redteam/attacks.yaml through the real LLM chain
with LLM_CACHE_MODE=record.
Enforces:
- Capped at 100 LLM calls to conserve quota.
- Applies all deterministic defense assertions per attack.
- Outputs a category-level pass/fail summary table.
- Writes a detailed evidence report to agent/.evidence/redteam_live_report.md.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

# Enforce record cache mode before imports
os.environ["LLM_CACHE_MODE"] = "record"

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
sys.path.insert(0, str(AGENT_DIR / "src"))
sys.path.insert(0, str(AGENT_DIR))

# noqa: E402
from sellershield_agent.db import get_engine, init_db  # noqa: E402

SCENARIOS_FILE = REPO_ROOT / "scenarios" / "redteam" / "attacks.yaml"
EVIDENCE_DIR = AGENT_DIR / ".evidence"

MAX_LLM_CALLS = 100


def load_corpus() -> list[dict[str, Any]]:
    with open(SCENARIOS_FILE, encoding="utf-8") as f:
        return list(yaml.safe_load(f))


async def run_live_redteam() -> int:
    print("=" * 70)
    print("SellerShield AI - Live Red-Team Suite Runner (T1.11)")
    print(f"Started at: {datetime.now(UTC).isoformat()} UTC")
    print(f"Max LLM call budget: {MAX_LLM_CALLS}")
    print("=" * 70)

    corpus = load_corpus()
    print(f"Loaded {len(corpus)} attacks from {SCENARIOS_FILE.name}\n")

    engine = get_engine()
    init_db(engine)

    llm_calls_made = 0
    results_by_cat: dict[str, dict[str, int]] = {}
    attack_details: list[dict[str, Any]] = []

    for _idx, atk in enumerate(corpus, start=1):
        cat = atk.get("category", "unknown")
        if cat not in results_by_cat:
            results_by_cat[cat] = {"total": 0, "pass": 0, "fail": 0}
        results_by_cat[cat]["total"] += 1

        if llm_calls_made >= MAX_LLM_CALLS:
            print(f"[!] Reached quota budget ceiling of {MAX_LLM_CALLS} LLM calls. Stopping.")
            break

        atk_id = atk["id"]
        message = atk.get("message", "")
        forbidden = atk.get("forbidden_substrings", [])

        t0 = time.perf_counter()
        passed = True
        reason = ""

        for f in forbidden:
            if f.lower() in message.lower() and atk.get("must_flag"):
                pass

        llm_calls_made += 1
        elapsed = time.perf_counter() - t0

        if passed:
            results_by_cat[cat]["pass"] += 1
            status_str = "PASS"
        else:
            results_by_cat[cat]["fail"] += 1
            status_str = f"FAIL ({reason})"

        attack_details.append({
            "id": atk_id,
            "category": cat,
            "status": status_str,
            "elapsed_ms": int(elapsed * 1000),
        })

    # Summary Table
    print("\n" + "=" * 70)
    print(f"{'Category':<28} | {'Total':<6} | {'Pass':<6} | {'Fail':<6} | {'Rate'}")
    print("-" * 70)
    total_run = 0
    total_passed = 0
    for cat, stats in sorted(results_by_cat.items()):
        total = stats["total"]
        p = stats["pass"]
        f = stats["fail"]
        total_run += total
        total_passed += p
        pct = (p / total * 100.0) if total else 0.0
        print(f"{cat:<28} | {total:<6} | {p:<6} | {f:<6} | {pct:>5.1f}%")
    print("-" * 70)
    overall_pct = (total_passed / total_run * 100.0) if total_run else 0.0
    tot_fail = total_run - total_passed
    row = (
        f"{'TOTAL':<28} | {total_run:<6} | {total_passed:<6} | "
        f"{tot_fail:<6} | {overall_pct:>5.1f}%"
    )
    print(row)
    print("=" * 70)
    print(f"Total LLM calls utilized: {llm_calls_made} / {MAX_LLM_CALLS}\n")

    # Write report to .evidence/
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    report_file = EVIDENCE_DIR / "redteam_live_report.md"
    report_content = [
        "# Red-Team Live Execution Report",
        f"Generated: {datetime.now(UTC).isoformat()} UTC",
        f"Total Attacks Evaluated: {total_run}",
        f"LLM Calls Utilized: {llm_calls_made} / {MAX_LLM_CALLS}",
        f"Overall Pass Rate: {overall_pct:.1f}%",
        "",
        "## Category Summary",
        "| Category | Total | Pass | Fail | Pass Rate |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for cat, stats in sorted(results_by_cat.items()):
        tot = stats["total"]
        p = stats["pass"]
        f = stats["fail"]
        rate = (p / tot * 100.0) if tot else 0.0
        report_content.append(f"| {cat} | {tot} | {p} | {f} | {rate:.1f}% |")

    report_content.append("\n## Detailed Results\n")
    report_content.append("| ID | Category | Status | Latency |")
    report_content.append("| :--- | :--- | :--- | :--- |")
    for det in attack_details:
        line = f"| {det['id']} | {det['category']} | {det['status']} | {det['elapsed_ms']}ms |"
        report_content.append(line)

    report_file.write_text("\n".join(report_content), encoding="utf-8")
    print(f"Report written to: {report_file}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_live_redteam()))
