from __future__ import annotations

from abc import ABC, abstractmethod

from libremapper.config import SourceConfig
from libremapper.models import Topology


class TopologyProvider(ABC):
    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    def load(self, source: SourceConfig) -> Topology:
        """Load a topology selected for one map."""
