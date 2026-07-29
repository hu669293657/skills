#!/usr/bin/env python3
"""Map physical NPU card count to parallel world size (dies / devices)."""

from __future__ import annotations

import re
from typing import Any

# Atlas 800I A3: each physical card exposes 2 dies (ASCEND_RT_VISIBLE_DEVICES pairs).
# Atlas 800I A2: card count already equals device count used by DP/TP.
_DIES_PER_CARD: dict[str, int] = {
    "A3": 2,
    "A2": 1,
}


def normalize_device_family(device_type: str | None) -> str | None:
    """Extract A2/A3 family token from values like 'A3' or 'Atlas 800I A3'."""
    if not device_type:
        return None
    text = str(device_type).strip().upper()
    m = re.search(r"\bA[23]\b", text)
    if m:
        return m.group(0)
    return None


def dies_per_card(device_type: str | None) -> int:
    family = normalize_device_family(device_type)
    if family is None:
        return 1
    return _DIES_PER_CARD.get(family, 1)


def resolve_world_size(
    num_cards: int,
    device_type: str | None = None,
) -> dict[str, Any]:
    """Convert physical card count (总卡数 / NPU卡数) to DP/TP world size.

    For A3: world_size = num_cards × 2 (dual-die).
    For A2 / unknown: world_size = num_cards.
    """
    if num_cards <= 0:
        raise ValueError(f"num_cards must be positive, got {num_cards}")
    dpc = dies_per_card(device_type)
    world = int(num_cards) * dpc
    return {
        "num_cards": int(num_cards),
        "device_type": device_type,
        "device_family": normalize_device_family(device_type),
        "dies_per_card": dpc,
        "world_size": world,
    }
