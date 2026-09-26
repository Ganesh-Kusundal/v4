"""Position tracking — alias to PositionAccountant.

PositionManager is consolidated directly into PositionAccountant so all
position mutations, locks, and reconciliations live in one authoritative module.
"""

from tradex_execution.position_accountant import PositionAccountant

#: Authoritative alias
PositionManager = PositionAccountant

__all__ = ["PositionAccountant", "PositionManager"]

