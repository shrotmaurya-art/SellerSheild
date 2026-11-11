"""Loader and validator for golden evaluation datasets (L4.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from sellershield_agent.evalkit.schema import ConversationCase, GoldenIntent
from sellershield_agent.understand.schema import Intent


def load_convo_file(file_path: Path | str) -> ConversationCase:
    """Load and validate a single conversation case YAML."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Convo file not found: {path}")

    with open(path, encoding="utf-8") as f:
        data: Any = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Convo file must contain a mapping: {path}")

    case = ConversationCase.model_validate(data)

    # Invariant validations
    if not case.id:
        raise ValueError(f"Case ID cannot be empty in {path}")
    if not case.turns:
        raise ValueError(f"Case {case.id} must define at least one turn")

    # Intent enum validation
    valid_intents = {i.value for i in Intent}
    if case.expect.intent not in valid_intents:
        raise ValueError(
            f"Case {case.id} specifies unknown intent '{case.expect.intent}'. "
            f"Expected one of: {sorted(valid_intents)}"
        )

    # Every money proposal must explicitly name an amount (int paise >= 0)
    if case.expect.proposal is not None:
        amount = case.expect.proposal.amount_paise
        if not isinstance(amount, int) or amount < 0:
            raise ValueError(
                f"Case {case.id} money proposal must specify non-negative int amount_paise, got: {amount}"
            )

    return case


def load_all_convos(convos_dir: Path | str) -> list[ConversationCase]:
    """Load and validate all conversation cases in the given directory."""
    dir_path = Path(convos_dir)
    if not dir_path.is_dir():
        raise FileNotFoundError(f"Convos directory not found: {dir_path}")

    files = sorted(dir_path.glob("L*.yaml"))
    cases: list[ConversationCase] = []
    seen_ids: set[str] = set()

    for fp in files:
        case = load_convo_file(fp)
        if case.id in seen_ids:
            raise ValueError(f"Duplicate conversation case ID found: '{case.id}' in {fp}")
        seen_ids.add(case.id)
        cases.append(case)

    return cases


def load_golden_intents(intents_file: Path | str) -> list[GoldenIntent]:
    """Load the 30 golden intent utterances from intents.yaml."""
    path = Path(intents_file)
    if not path.is_file():
        raise FileNotFoundError(f"Golden intents file not found: {path}")

    with open(path, encoding="utf-8") as f:
        data: Any = yaml.safe_load(f)

    if not isinstance(data, dict) or "cases" not in data:
        raise ValueError(f"Invalid intents.yaml format: {path}")

    return [GoldenIntent.model_validate(item) for item in data["cases"]]
