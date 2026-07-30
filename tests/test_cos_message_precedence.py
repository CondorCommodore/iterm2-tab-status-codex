import json
from pathlib import Path

import pytest

from scripts.cos_message_precedence import (
    DISPLAY_LABEL,
    PRECEDENCE,
    PRECEDENCE_RANK,
    SCHEMA,
    effective_precedence,
    validate_label,
)

ROOT = Path(__file__).resolve().parents[1]


def test_b_is_the_canonical_ordered_contract():
    fixture = json.loads(
        (ROOT / "tests/fixtures/message_precedence_labels_v1.json").read_text()
    )
    assert fixture["schema"] == SCHEMA
    assert tuple(fixture["labels"]) == PRECEDENCE
    assert [DISPLAY_LABEL[label] for label in PRECEDENCE] == fixture[
        "display_labels"
    ]
    assert [PRECEDENCE_RANK[label] for label in PRECEDENCE] == [0, 1, 2, 3]
    assert fixture["decision"]["selected_candidate"] == "B"


@pytest.mark.parametrize("legacy", ["Normal", "Elevated", "Urgent", "Critical"])
def test_new_contract_rejects_historical_v1_names(legacy):
    with pytest.raises(ValueError):
        validate_label(legacy)


def test_rule_floor_cannot_be_downgraded():
    assert effective_precedence("routine", "flash") == "flash"
    assert effective_precedence("flash", "routine") == "flash"
    assert effective_precedence("priority", "immediate") == "immediate"
