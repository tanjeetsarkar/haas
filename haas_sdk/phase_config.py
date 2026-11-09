"""Typed opt-in configuration for bounded run acquisition and finalization.

This value describes a server capability; constructing it does not enable that
capability on a server that has not activated it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class PhaseConfig:
    """Strict v1 phase configuration; unknown fields cannot be supplied."""

    version: Literal[1] = 1
    mode: Literal["bounded_acquisition_finalization"] = (
        "bounded_acquisition_finalization"
    )
    reserve_basis_points: Literal[2000] = 2000

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("phase_config.version must be 1")
        if self.mode != "bounded_acquisition_finalization":
            raise ValueError(
                "phase_config.mode must be 'bounded_acquisition_finalization'"
            )
        if type(self.reserve_basis_points) is not int or self.reserve_basis_points != 2000:
            raise ValueError("phase_config.reserve_basis_points must be 2000")

    def to_dict(self) -> dict[str, int | str]:
        """Return the exact wire representation without defaults or extras."""
        return {
            "version": self.version,
            "mode": self.mode,
            "reserve_basis_points": self.reserve_basis_points,
        }
