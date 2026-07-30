#!/usr/bin/env python3
"""Versioned, side-effect-free contract for COS message precedence labels."""

from __future__ import annotations

from typing import Final

SCHEMA: Final = "cos.message-precedence.labels.v1"
PRECEDENCE: Final = ("routine", "priority", "immediate", "flash")
PRECEDENCE_RANK: Final = {label: rank for rank, label in enumerate(PRECEDENCE)}
DISPLAY_LABEL: Final = {label: label.title() for label in PRECEDENCE}


def validate_label(label: str) -> str:
    """Return the canonical lowercase label, rejecting aliases and old v1 names."""

    if label not in PRECEDENCE_RANK:
        raise ValueError(f"unsupported message precedence: {label!r}")
    return label


def effective_precedence(model_label: str, rule_floor: str = "routine") -> str:
    """Apply the safety floor; a model may raise but never lower that floor."""

    model_label = validate_label(model_label)
    rule_floor = validate_label(rule_floor)
    return PRECEDENCE[max(PRECEDENCE_RANK[model_label], PRECEDENCE_RANK[rule_floor])]
