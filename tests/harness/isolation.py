"""Per-run resource naming.

This is how a shared compose stack behaves like a private one. Every test run —
and every xdist worker within it — gets its own database, topic prefix, consumer
group, vhost, and task queue, so runs never observe each other's state.

This module is the reason the harness does not need ephemeral containers.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import asdict, dataclass

RUN_ID_ENV = "SMARTHEALTH_TEST_RUN_ID"
_UNSAFE = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _UNSAFE.sub("_", value.lower()).strip("_") or "x"


def _worker_index(worker_id: str) -> int:
    """`master` -> 0, `gw0` -> 1, `gw1` -> 2 … so no two workers share a Redis database."""
    if worker_id == "master":
        return 0
    digits = "".join(character for character in worker_id if character.isdigit())
    return int(digits) + 1 if digits else 0


@dataclass(frozen=True)
class RunIsolation:
    run_id: str
    worker_id: str
    resource_prefix: str
    postgres_db: str
    mongo_db: str
    redis_db: int
    redis_prefix: str
    kafka_topic_prefix: str
    kafka_group: str
    rabbit_vhost: str
    temporal_task_queue_prefix: str

    def as_env(self) -> dict[str, str]:
        """Environment overrides that point an app process at this run's slice."""
        return {
            "SMARTHEALTH_RESOURCE_PREFIX": self.resource_prefix,
            "SMARTHEALTH_POSTGRES_DB": self.postgres_db,
            "SMARTHEALTH_MONGO_DB": self.mongo_db,
            "SMARTHEALTH_REDIS_DB": str(self.redis_db),
            "SMARTHEALTH_REDIS_PREFIX": self.redis_prefix,
            "SMARTHEALTH_KAFKA_GROUP": self.kafka_group,
            "SMARTHEALTH_RABBIT_VHOST": self.rabbit_vhost,
            RUN_ID_ENV: self.run_id,
        }

    def to_dict(self) -> dict:
        return asdict(self)


def make_isolation(run_id: str | None = None, worker_id: str = "master") -> RunIsolation:
    """Build the naming slice for one run/worker. Deterministic given the same inputs."""
    run_id = run_id or os.environ.get(RUN_ID_ENV) or uuid.uuid4().hex[:8]
    run = _slug(run_id)
    worker = _slug(worker_id)
    prefix = f"t_{run}_{worker}_"
    return RunIsolation(
        run_id=run_id,
        worker_id=worker_id,
        resource_prefix=prefix,
        postgres_db=f"{prefix}db",
        mongo_db=f"{prefix}db",
        redis_db=_worker_index(worker_id),
        redis_prefix=prefix,
        kafka_topic_prefix=prefix,
        kafka_group=f"{prefix}group",
        rabbit_vhost=f"/{prefix.rstrip('_')}",
        temporal_task_queue_prefix=prefix,
    )
