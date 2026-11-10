"""The guided flows, and which roles may start which (spec 9.1).

`FLOWS` is the registry: the starters a role sees, the flows the model may hand off to, and
the meta-test that checks every flow is reachable all read it. Roles are enforced twice --
here, so a patient is never offered a staff flow, and by Part A, which refuses anything the
token may not do whatever this list says.
"""

from __future__ import annotations

from assistant_service.flows.base import Flow
from assistant_service.flows.operations import CapacityFlow, MyDayFlow, NoShowsFlow, SnapshotFlow
from assistant_service.flows.record import (
    BriefFlow,
    LastVisitFlow,
    PrepareFlow,
    PrescriptionsFlow,
    ReportsFlow,
    ResultsToReviewFlow,
)
from assistant_service.flows.scheduling import (
    BookFlow,
    CancelFlow,
    MyAppointmentsFlow,
    RescheduleFlow,
    WaitlistFlow,
)

FLOWS: dict[str, Flow] = {
    f.id: f
    for f in (
        BookFlow(),
        RescheduleFlow(),
        CancelFlow(),
        WaitlistFlow(),
        MyAppointmentsFlow(),
        PrepareFlow(),
        PrescriptionsFlow(),
        LastVisitFlow(),
        ReportsFlow(),
        MyDayFlow(),
        BriefFlow(),
        ResultsToReviewFlow(),
        CapacityFlow(),
        NoShowsFlow(),
        SnapshotFlow(),
    )
}


def flows_for(role: str) -> list[Flow]:
    return [f for f in FLOWS.values() if role in f.roles]
