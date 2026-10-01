"""Read-only local web dashboard (SPEC §12.3): ``python -m bot dashboard``."""

from __future__ import annotations

from bot.dashboard.app import CDN_LIGHTWEIGHT_CHARTS, EXIT_REASON_KO, create_app, run_dashboard

__all__ = ["CDN_LIGHTWEIGHT_CHARTS", "EXIT_REASON_KO", "create_app", "run_dashboard"]
