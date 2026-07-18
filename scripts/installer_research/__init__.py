"""Deterministic inventory and comparison primitives for installer research."""

from .comparator import accept_baseline, evaluate_candidate
from .inventory import InventoryError, build_inventory

__all__ = [
    "InventoryError",
    "accept_baseline",
    "build_inventory",
    "evaluate_candidate",
]
