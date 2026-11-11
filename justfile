set windows-shell := ['powershell.exe', '-NoLogo', '-NoProfile', '-Command']

default:
    just --list

agent-lint:
    cd agent; uv run ruff check .

agent-types:
    cd agent; uv run mypy src

agent-test:
    cd agent; uv run pytest -q

test-guard:
    cd agent; uv run python ../scripts/test_guard.py check

freeze-tests:
    cd agent; uv run python ../scripts/test_guard.py freeze

status-guard:
    cd agent; uv run python ../scripts/test_guard.py status

agent-check: agent-lint agent-types agent-test test-guard

secrets:
    gitleaks detect --source . --no-banner

tree:
    Get-ChildItem -Recurse -Depth 2 -Name

store:
    Write-Output "owned by W1"

web:
    Write-Output "owned by W2"


demo-check:
    cd agent; uv run python ../scripts/demo_check.py --mode fixtures --llm off

eval:
    cd agent; uv run python ../scripts/eval_run.py --mode scripted --publish
    cd agent; uv run python ../scripts/eval_run.py --mode replay

eval-live:
    powershell -NoLogo -NoProfile -Command "Write-Output 'QUOTA REMINDER: Running live evaluation consumes LLM API tokens/quota.'"
    cd agent; uv run python ../scripts/eval_run.py --mode live --publish
