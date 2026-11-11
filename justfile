set windows-shell := ['powershell.exe', '-NoLogo', '-NoProfile', '-Command']

default:
    just --list

<<<<<<< HEAD
=======
agent-test:
    cd agent; uv run pytest -q

>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
agent-lint:
    cd agent; uv run ruff check .

agent-types:
    cd agent; uv run mypy src

<<<<<<< HEAD
agent-test:
    cd agent; uv run pytest -q

test-guard:
    cd agent; uv run python ../scripts/test_guard.py check

freeze-tests:
    cd agent; uv run python ../scripts/test_guard.py freeze

status-guard:
    cd agent; uv run python ../scripts/test_guard.py status

agent-check: agent-lint agent-types agent-test test-guard
=======
agent-check: agent-test agent-lint agent-types
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be

secrets:
    gitleaks detect --source . --no-banner

tree:
    Get-ChildItem -Recurse -Depth 2 -Name

store:
    Write-Output "owned by W1"

web:
    Write-Output "owned by W2"
<<<<<<< HEAD
=======

>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
