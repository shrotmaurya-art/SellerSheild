"""Test guard - enforces test integrity and prevents test weakening.

Commands:
    check   - Verifies manifest hashes, test counts, config flags, and AST.
    freeze  - Adds new frozen files and records test count.
    status  - Displays current integrity status and lists unfrozen test files.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORBIDDEN_ADDOPTS = ["-k", "--deselect", "--ignore", "-x", "--maxfail", "--lf", "--co"]
CONFIG_FILES = ["agent/pyproject.toml", "pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"]


def resolve_root(root: Path) -> Path:
    if (root / "scripts" / "test_guard.toml").is_file():
        return root
    if (root.parent / "scripts" / "test_guard.toml").is_file():
        return root.parent
    return root


def load_config(root: Path) -> dict[str, Any]:
    root = resolve_root(root)
    config_path = root / "scripts" / "test_guard.toml"
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
        guard_cfg: dict[str, Any] = data.get("guard", {})
        return guard_cfg


def compute_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def count_asserts(path: Path) -> int:
    if path.suffix != ".py":
        return 0
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assert))
    except Exception:
        return 0


def get_matching_files(root: Path, globs: list[str]) -> list[Path]:
    matched: set[Path] = set()
    for glob_pattern in globs:
        for p in root.glob(glob_pattern):
            if p.is_file():
                parts = p.relative_to(root).parts
                if not any(part.startswith(".") or part == "__pycache__" for part in parts):
                    matched.add(p)
    return sorted(matched, key=lambda p: p.relative_to(root).as_posix())


def run_pytest_collect(root: Path) -> int:
    agent_dir = root / "agent"
    cwd = agent_dir if agent_dir.is_dir() else root
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    except Exception as exc:
        raise RuntimeError(f"Failed to execute pytest collect: {exc}") from exc

    output = proc.stdout + "\n" + proc.stderr
    match = re.search(r"(\d+)\s+tests?\s+collected", output)
    if match:
        return int(match.group(1))
    if "no tests collected" in output:
        return 0
    lines = [line.strip() for line in output.splitlines() if "::" in line]
    if lines:
        return len(lines)
    raise RuntimeError(f"Could not parse test count from pytest output:\n{output}")


def check_ast_invariants(path: Path, allowed_markers: list[str]) -> list[str]:
    violations: list[str] = []
    if path.suffix != ".py":
        return violations
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: SyntaxError while parsing test file: {e.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            if isinstance(node.test, ast.Constant):
                val = node.test.value
                if val is True or val == 1:
                    violations.append(f"{path}:{node.lineno}: assert {val!r} is forbidden")
            elif isinstance(node.test, ast.Compare):
                left_src = ast.unparse(node.test.left).strip()
                for comp in node.test.comparators:
                    if ast.unparse(comp).strip() == left_src:
                        msg = f"{path}:{node.lineno}: self-comparison assertion '{left_src}'"
                        violations.append(f"{msg} forbidden")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                stmts = [
                    s for s in node.body
                    if not (
                        isinstance(s, ast.Expr)
                        and isinstance(s.value, ast.Constant)
                        and isinstance(s.value.value, str)
                    )
                ]
                if not stmts or all(isinstance(s, ast.Pass) for s in stmts):
                    violations.append(
                        f"{path}:{node.lineno}: test '{node.name}' has pass/empty body"
                    )
            for deco in node.decorator_list:
                d_str = ast.unparse(deco)
                if any(k in d_str for k in ["skip", "skipif", "xfail"]):
                    violations.append(
                        f"{path}:{node.lineno}: forbidden test skip/xfail decorator '{d_str}'"
                    )
                elif "pytest.mark." in d_str:
                    m = re.search(r"pytest\.mark\.([A-Za-z0-9_]+)", d_str)
                    if m and m.group(1) not in allowed_markers:
                        violations.append(f"{path}:{node.lineno}: unapproved marker '{m.group(1)}'")
        elif isinstance(node, ast.Call):
            call_src = ast.unparse(node.func)
            forbidden_calls = [
                "pytest.skip",
                "pytest.xfail",
                "pytest.importorskip",
                "unittest.skip",
                "self.skipTest",
            ]
            if any(k in call_src for k in forbidden_calls):
                violations.append(f"{path}:{node.lineno}: forbidden call '{call_src}'")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id == "pytestmark" and any(
                        k in ast.unparse(node.value) for k in ["skip", "xfail"]
                    ):
                        violations.append(
                            f"{path}:{node.lineno}: forbidden skip/xfail in pytestmark"
                        )
                    elif (
                        target.id == "__test__"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value is False
                    ):
                        violations.append(f"{path}:{node.lineno}: forbidden __test__ = False")
    return violations


def check_config_addopts(root: Path, allowed_addopts_marker: str) -> list[str]:
    violations: list[str] = []
    for rel_path in CONFIG_FILES:
        cfg = root / rel_path
        if not cfg.is_file():
            continue
        for line in cfg.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean.startswith("#") or "addopts" not in clean:
                continue
            for flag in FORBIDDEN_ADDOPTS:
                if re.search(rf"(?:^|\s|\"|'){re.escape(flag)}(?:$|\s|=|\"|')", clean):
                    violations.append(
                        f"{rel_path}: forbidden option '{flag}' found in addopts: '{clean}'"
                    )
            if "-m " in clean:
                extracted = re.search(r"-m\s+(['\"][^'\"]+['\"])", clean)
                if extracted and f"-m {extracted.group(1)}" != allowed_addopts_marker:
                    violations.append(
                        f"{rel_path}: unapproved marker expression in addopts: '{clean}'"
                    )
    return violations


def check_justfile(root: Path) -> list[str]:
    justfile = root / "justfile"
    if not justfile.is_file():
        return ["justfile not found"]
    lines = justfile.read_text(encoding="utf-8").splitlines()
    recipe: str | None = None
    commands: dict[str, list[str]] = {}
    for line in lines:
        if m := re.match(r"^([a-zA-Z0-9_\-]+)\s*:", line):
            recipe = m.group(1)
            commands[recipe] = [line.strip()]
        elif recipe and line.startswith("    "):
            commands[recipe].append(line.strip())

    violations: list[str] = []
    agent_check = "\n".join(commands.get("agent-check", []))
    all_cmds = commands.get("agent-check", []) + commands.get("agent-test", [])
    for cmd in all_cmds:
        if "pytest" in cmd and cmd.replace("cd agent;", "").strip() != "uv run pytest -q":
            violations.append(
                f"justfile pytest invocation must be plain 'uv run pytest -q', got: '{cmd}'"
            )
    if "test-guard" not in agent_check:
        violations.append("justfile recipe 'agent-check' must include 'test-guard'")
    return violations


def load_manifest(manifest_path: Path) -> dict[str, tuple[str, int, int]]:
    manifest: dict[str, tuple[str, int, int]] = {}
    if not manifest_path.is_file():
        return manifest
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            parts = line.split()
            if len(parts) >= 4:
                manifest[parts[3]] = (parts[0], int(parts[1]), int(parts[2]))
    return manifest


def cmd_freeze(root: Path, refreeze_path: str | None, reason: str | None) -> int:
    config = load_config(root)
    manifest_path = root / config.get("manifest_file", "tests/FROZEN.sha256")
    count_path = root / config.get("count_file", "tests/FROZEN.count")
    changes_path = root / config.get("changes_file", "docs/TEST_CHANGES.md")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = dict(load_manifest(manifest_path))

    norm_refreeze = Path(refreeze_path).as_posix() if refreeze_path else None
    for f in get_matching_files(root, config.get("frozen_globs", [])):
        rel = f.relative_to(root).as_posix()
        sha, size, asserts = compute_sha256(f), f.stat().st_size, count_asserts(f)
        if rel in manifest:
            old_sha, old_sz, old_as = manifest[rel]
            if old_sha != sha or old_sz != size or old_as != asserts:
                if norm_refreeze == rel:
                    if not reason or not reason.strip():
                        print(
                            f"ERROR: --refreeze for {rel} requires non-empty --reason.",
                            file=sys.stderr,
                        )
                        return 1
                    changes_path.parent.mkdir(parents=True, exist_ok=True)
                    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
                    with open(changes_path, "a", encoding="utf-8") as chg:
                        chg.write(f"- {ts} | {rel} | Refrozen: {reason.strip()}\n")
                    print(f"WARNING: Refreezing {rel}: {reason.strip()}")
                    manifest[rel] = (sha, size, asserts)
                else:
                    print(
                        f"ERROR: Refusing to modify frozen '{rel}' without --refreeze/reason.",
                        file=sys.stderr,
                    )
                    return 1
        else:
            manifest[rel] = (sha, size, asserts)

    collected = run_pytest_collect(root)
    if count_path.is_file():
        old_count = int(count_path.read_text(encoding="utf-8").strip())
        if collected < old_count:
            print(
                f"ERROR: Collected count ({collected}) dropped below existing FROZEN.count.",
                file=sys.stderr,
            )
            return 1
    count_path.write_text(f"{collected}\n", encoding="utf-8")
    lines = [
        f"{sha}  {size:<8}  {asserts:<4}  {path}"
        for path, (sha, size, asserts) in sorted(manifest.items())
    ]
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Froze {len(manifest)} files; recorded test count {collected}.")
    return 0


def cmd_check(root: Path) -> int:
    config = load_config(root)
    manifest_path = root / config.get("manifest_file", "tests/FROZEN.sha256")
    count_path = root / config.get("count_file", "tests/FROZEN.count")
    if not manifest_path.is_file():
        print(f"ERROR: Manifest file not found: {manifest_path}", file=sys.stderr)
        return 1

    manifest = load_manifest(manifest_path)
    errors: list[str] = []
    for rel, (f_sha, f_sz, f_as) in manifest.items():
        fp = root / rel
        if not fp.is_file():
            errors.append(f"Missing or renamed frozen file: {rel}")
            continue
        if compute_sha256(fp) != f_sha or fp.stat().st_size != f_sz:
            errors.append(f"Frozen file modified: {rel} (hash/size mismatch)")
        if count_asserts(fp) < f_as:
            errors.append(f"Frozen test file lost assertions: {rel}")

    for p in get_matching_files(root, config.get("frozen_globs", [])):
        rel = p.relative_to(root).as_posix()
        is_conftest = p.name == "conftest.py"
        is_fixture = "fixtures/" in rel or "fixture" in p.name
        if (is_conftest or is_fixture) and rel not in manifest:
            errors.append(f"New unmanifested conftest or fixture file: {rel}")

    if count_path.is_file():
        frozen_cnt = int(count_path.read_text(encoding="utf-8").strip())
        cur_cnt = run_pytest_collect(root)
        if cur_cnt < frozen_cnt:
            errors.append(f"Collected test count dropped: {cur_cnt} < {frozen_cnt}")

    tf_list = list(root.glob("agent/tests/**/*.py")) + list(root.glob("tests/**/*.py"))
    test_files = sorted(set(tf_list))
    unfrozen: list[str] = []
    for tf in test_files:
        rel = tf.relative_to(root).as_posix()
        if rel not in manifest and not tf.name.startswith("__init__"):
            unfrozen.append(rel)
        errors.extend(check_ast_invariants(tf, config.get("allowed_markers", ["live", "store"])))

    marker = config.get("allowed_addopts_marker", "-m 'not live and not store'")
    errors.extend(check_config_addopts(root, marker))
    errors.extend(check_justfile(root))

    if errors:
        print("FAIL: test-guard detected integrity violations:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    for u in unfrozen:
        print(f"UNFROZEN: {u}")
    print("PASS: test-guard verified test suite integrity.")
    return 0


def cmd_status(root: Path) -> int:
    config = load_config(root)
    manifest_path = root / config.get("manifest_file", "tests/FROZEN.sha256")
    count_path = root / config.get("count_file", "tests/FROZEN.count")
    if not manifest_path.is_file():
        print("Status: UNINITIALIZED (manifest does not exist)")
        return 1
    manifest = load_manifest(manifest_path)
    count = count_path.read_text(encoding="utf-8").strip() if count_path.is_file() else "none"
    print(f"Test Guard Status:\n  Frozen files: {len(manifest)}\n  FROZEN.count: {count}")
    tf_list = list(root.glob("agent/tests/**/*.py")) + list(root.glob("tests/**/*.py"))
    test_files = sorted(set(tf_list))
    unfrozen = [
        p.relative_to(root).as_posix()
        for p in test_files
        if p.relative_to(root).as_posix() not in manifest and not p.name.startswith("__")
    ]
    print(f"  Unfrozen tests ({len(unfrozen)}):" if unfrozen else "  Unfrozen tests: none")
    for u in unfrozen:
        print(f"    - {u}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SellerShield AI test guard")
    parser.add_argument("--root", default=".", help="Root repository directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Run integrity checks")
    frz = sub.add_parser("freeze", help="Freeze manifest and test counts")
    frz.add_argument("--refreeze", help="Path to existing file to refreeze")
    frz.add_argument("--reason", help="Required reason for refreezing")
    sub.add_parser("status", help="Print guard status")

    args = parser.parse_args(argv)
    root = resolve_root(Path(args.root).resolve())
    if args.command == "freeze":
        return cmd_freeze(root, args.refreeze, args.reason)
    if args.command == "check":
        return cmd_check(root)
    if args.command == "status":
        return cmd_status(root)
    return 1


if __name__ == "__main__":
    sys.exit(main())
