"""Tests for test guard itself, run against temporary copies of a sample project."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402
from scripts.test_guard import cmd_check, cmd_freeze, main  # noqa: E402


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    """Create a minimal valid project structure and freeze it."""
    # 1. scripts
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "test_guard.py", scripts_dir / "test_guard.py")
    shutil.copy2(REPO_ROOT / "scripts" / "test_guard.toml", scripts_dir / "test_guard.toml")

    # 2. justfile
    shutil.copy2(REPO_ROOT / "justfile", tmp_path / "justfile")

    # 3. agent pyproject
    agent_dir = tmp_path / "agent"
    agent_tests = agent_dir / "tests"
    agent_tests.mkdir(parents=True)
    (agent_dir / "pyproject.toml").write_text(
        """[project]
name = "sample-agent"
version = "0.1.0"

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-m 'not live and not store'"
testpaths = ["tests"]
""",
        encoding="utf-8",
    )

    # 4. test files and conftest
    (agent_tests / "conftest.py").write_text(
        "# sample conftest\nimport pytest\n",
        encoding="utf-8",
    )
    (agent_tests / "test_sample.py").write_text(
        """def test_addition():
    assert 1 + 1 == 2
""",
        encoding="utf-8",
    )

    # Freeze sample project
    ret = cmd_freeze(tmp_path, refreeze_path=None, reason=None)
    assert ret == 0
    # Verify check passes initially
    assert cmd_check(tmp_path) == 0
    return tmp_path


def test_modifying_frozen_file_fails(sample_project: Path) -> None:
    test_file = sample_project / "agent" / "tests" / "test_sample.py"
    test_file.write_text(
        """def test_addition():
    assert 1 + 1 == 2
# comment appended
""",
        encoding="utf-8",
    )
    assert cmd_check(sample_project) == 1


def test_deleting_frozen_file_fails(sample_project: Path) -> None:
    test_file = sample_project / "agent" / "tests" / "test_sample.py"
    test_file.unlink()
    assert cmd_check(sample_project) == 1


def test_renaming_frozen_file_fails(sample_project: Path) -> None:
    test_file = sample_project / "agent" / "tests" / "test_sample.py"
    renamed = sample_project / "agent" / "tests" / "test_renamed.py"
    test_file.rename(renamed)
    assert cmd_check(sample_project) == 1


def test_adding_skip_fails(sample_project: Path) -> None:
    # 1. @pytest.mark.skip decorator
    test_file = sample_project / "agent" / "tests" / "test_new.py"
    test_file.write_text(
        """import pytest

@pytest.mark.skip(reason="skipping")
def test_skip():
    assert 1 == 2
""",
        encoding="utf-8",
    )
    assert cmd_check(sample_project) == 1

    # 2. pytest.skip() function call
    test_file.write_text(
        """import pytest

def test_skip():
    pytest.skip("skipping")
    assert 1 == 2
""",
        encoding="utf-8",
    )
    assert cmd_check(sample_project) == 1

    # 3. @pytest.mark.xfail
    test_file.write_text(
        """import pytest

@pytest.mark.xfail
def test_xfail():
    assert 1 == 2
""",
        encoding="utf-8",
    )
    assert cmd_check(sample_project) == 1


def test_adding_k_to_addopts_fails(sample_project: Path) -> None:
    cfg = sample_project / "agent" / "pyproject.toml"
    cfg.write_text(
        """[project]
name = "sample-agent"
version = "0.1.0"

[tool.pytest.ini_options]
addopts = "-k foo"
testpaths = ["tests"]
""",
        encoding="utf-8",
    )
    assert cmd_check(sample_project) == 1


def test_lowering_count_fails(sample_project: Path) -> None:
    count_file = sample_project / "tests" / "FROZEN.count"
    # Set frozen count to 999 (higher than actual collected count 1)
    count_file.write_text("999\n", encoding="utf-8")
    assert cmd_check(sample_project) == 1


def test_adding_assert_true_fails(sample_project: Path) -> None:
    test_file = sample_project / "agent" / "tests" / "test_bad.py"

    # assert True
    test_file.write_text("def test_one():\n    assert True\n", encoding="utf-8")
    assert cmd_check(sample_project) == 1

    # assert 1
    test_file.write_text("def test_one():\n    assert 1\n", encoding="utf-8")
    assert cmd_check(sample_project) == 1

    # pass-only body
    test_file.write_text("def test_one():\n    pass\n", encoding="utf-8")
    assert cmd_check(sample_project) == 1

    # self-comparison
    test_file.write_text("def test_one():\n    x = 5\n    assert x == x\n", encoding="utf-8")
    assert cmd_check(sample_project) == 1


def test_reducing_asserts_fails(sample_project: Path) -> None:
    test_file = sample_project / "agent" / "tests" / "test_multi.py"
    test_file.write_text(
        """def test_multi():
    assert 1 == 1 + 0
    assert 2 == 1 + 1
""",
        encoding="utf-8",
    )
    # Freeze it with 2 asserts
    assert cmd_freeze(sample_project, None, None) == 0

    # Reduce to 1 assert and update hash in manifest manually to isolate assert-count check
    test_file.write_text(
        """def test_multi():
    assert 2 == 1 + 1
""",
        encoding="utf-8",
    )
    assert cmd_check(sample_project) == 1


def test_new_unmanifested_conftest_fails(sample_project: Path) -> None:
    new_conftest = sample_project / "agent" / "tests" / "sub" / "conftest.py"
    new_conftest.parent.mkdir(parents=True)
    new_conftest.write_text("# rogue fixture override\n", encoding="utf-8")
    assert cmd_check(sample_project) == 1


def test_unfrozen_new_test_reported_and_passes(
    sample_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    new_test = sample_project / "agent" / "tests" / "test_new_feature.py"
    new_test.write_text(
        """def test_feature():
    val = 100
    assert val + 50 == 150
""",
        encoding="utf-8",
    )
    ret = cmd_check(sample_project)
    captured = capsys.readouterr()
    assert ret == 0
    assert "UNFROZEN: agent/tests/test_new_feature.py" in captured.out


def test_allowed_markers_pass(sample_project: Path) -> None:
    test_file = sample_project / "agent" / "tests" / "test_marked.py"
    test_file.write_text(
        """import pytest

@pytest.mark.live
def test_live():
    assert 2 > 1

@pytest.mark.store
def test_store():
    assert 3 > 2
""",
        encoding="utf-8",
    )
    assert cmd_freeze(sample_project, None, None) == 0
    assert cmd_check(sample_project) == 0


def test_refreeze_with_reason(sample_project: Path) -> None:
    test_file = sample_project / "agent" / "tests" / "test_sample.py"
    test_file.write_text(
        """def test_addition():
    assert 2 + 2 == 4
""",
        encoding="utf-8",
    )
    # Freeze without --refreeze fails
    assert cmd_freeze(sample_project, None, None) == 1

    # Freeze with --refreeze and reason succeeds
    rel_path = "agent/tests/test_sample.py"
    args = [
        "--root", str(sample_project),
        "freeze",
        "--refreeze", rel_path,
        "--reason", "Updated math assertion",
    ]
    ret = main(args)
    assert ret == 0

    changes_doc = sample_project / "docs" / "TEST_CHANGES.md"
    assert changes_doc.is_file()
    content = changes_doc.read_text(encoding="utf-8")
    assert rel_path in content
    assert "Updated math assertion" in content
    assert cmd_check(sample_project) == 0
