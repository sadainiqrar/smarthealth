"""A named registry.

Kafka consumers, Temporal workflows, and Celery tasks each register here so the
harness's meta-tests can enumerate them and assert properties — every consumer is
idempotent, every workflow is replay-safe — without a hand-maintained list that
drifts. Harness requirement 3.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    """Maps a unique name to a registered item.

    Duplicate names raise. Two handlers silently claiming one name is exactly how an
    event ends up processed twice, so the collision must be loud and immediate.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, T] = {}

    def register(self, name: str, item: T) -> T:
        if name in self._items:
            raise ValueError(f"{self.kind} '{name}' is already registered")
        self._items[name] = item
        return item

    def get(self, name: str) -> T:
        if name not in self._items:
            raise KeyError(
                f"no {self.kind} named '{name}'; registered: {', '.join(self.names()) or 'none'}"
            )
        return self._items[name]

    def names(self) -> list[str]:
        return sorted(self._items)

    def items(self) -> list[tuple[str, T]]:
        return [(name, self._items[name]) for name in self.names()]

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[T]:
        return (self._items[name] for name in self.names())
