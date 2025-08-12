from __future__ import annotations
from typing import Dict, List
from ..models import Ship


class World:
    def __init__(self) -> None:
        self.ships: Dict[str, Ship] = {}
        self.torpedoes: List[dict] = []  # simple dicts for MVP

    def add_ship(self, ship: Ship) -> None:
        self.ships[ship.id] = ship

    def get_ship(self, ship_id: str) -> Ship:
        return self.ships[ship_id]

    def all_ships(self) -> List[Ship]:
        return list(self.ships.values())
