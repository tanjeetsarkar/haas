"""Unstable preview namespaces (`haas.preview.*`, §3).

Preview APIs may change in a minor release. Importing any module in this
package emits a one-time `PreviewWarning`. Promotion to stable requires the
server criteria in §19 (require_action, OpenAPI snapshot, integration tests).
"""

from __future__ import annotations

import warnings


class PreviewWarning(UserWarning):
    """Emitted once per process when a preview namespace is first used."""


_warned = False


def warn_preview() -> None:
    """Emit the one-time preview warning (idempotent per process)."""
    global _warned
    if not _warned:
        warnings.warn(
            "haas.preview namespaces are unstable and may change in a minor "
            "release (spec §3).",
            PreviewWarning,
            stacklevel=3,
        )
        _warned = True
