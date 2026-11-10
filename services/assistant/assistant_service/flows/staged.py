"""A flow written as named stages: `on_<stage>` handlers, run until one asks something.

A handler either asks (returns an Ask, and stays on a stage that will read the answer) or
moves on (sets `data["stage"]` and returns None). Only the first handler of a run sees the
user's answer; the stages it moves through after that start fresh. `"done"` ends the flow.
"""

from __future__ import annotations

from typing import Any

from assistant_service.flows.base import Ask, Ctx, Flow

#: A bug that loops between stages without asking must not spin a turn forever.
MAX_STAGES_PER_RUN = 20


class StagedFlow(Flow):
    first_stage = "start"

    async def step(self, ctx: Ctx, data: dict[str, Any], answer: dict | None) -> Ask | None:
        data.setdefault("stage", self.first_stage)
        for _ in range(MAX_STAGES_PER_RUN):
            stage = data["stage"]
            if stage == "done":
                return None
            asked = await getattr(self, f"on_{stage}")(ctx, data, answer)
            answer = None
            if asked is not None:
                return asked
        raise RuntimeError(f"flow {self.id} did not settle (stage {data['stage']!r})")
