set windows-shell := ['powershell.exe', '-NoLogo', '-NoProfile', '-Command']

default:
    just --list

agent-test:
    cd agent; uv run pytest -q

agent-lint:
    cd agent; uv run ruff check .

agent-types:
    cd agent; uv run mypy src

agent-check: agent-test agent-lint agent-types

secrets:
    gitleaks detect --source . --no-banner

tree:
    Get-ChildItem -Recurse -Depth 2 -Name

store:
    Write-Output "owned by W1"

web:
    Write-Output "owned by W2"

