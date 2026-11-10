"""What a caller can start with: guided flows for their role, plus open questions.

A starter is either a **flow** (click-driven, works with no model) or a **prompt** (sent as
a chat message: knowledge questions the clinic's documents answer). Chosen on the server
from the token's role; the client never says what role it is.
"""

from __future__ import annotations

from dataclasses import dataclass

from assistant_service.flows import flows_for


@dataclass(frozen=True)
class Starter:
    id: str
    title: str
    description: str
    icon: str
    mode: str  # "flow" | "prompt"
    prompt: str = ""


#: Questions the corpus answers, by audience. Each is a real use case from spec 9.1.
PROMPTS: dict[str, list[Starter]] = {
    "patient": [
        Starter(
            "q-specialist",
            "Which doctor should I see?",
            "Describe what's wrong",
            "search",
            "prompt",
            "I've had knee pain for two weeks. Which kind of doctor should I see?",
        ),
        Starter(
            "q-late-cancel",
            "Is there a fee if I cancel late?",
            "Clinic policies",
            "book-open",
            "prompt",
            "Is there a fee if I cancel late?",
        ),
        Starter(
            "q-urgent",
            "When is it an emergency?",
            "Urgent care guidance",
            "siren",
            "prompt",
            "When should I go to urgent care instead of booking?",
        ),
    ],
    "front_desk": [
        Starter(
            "q-walkin",
            "Walk-in procedure",
            "The front-desk protocol",
            "book-open",
            "prompt",
            "What is our procedure for a walk-in patient?",
        ),
        Starter(
            "q-policy",
            "Late cancellation policy",
            "For phone queries",
            "book-open",
            "prompt",
            "What is the late cancellation policy?",
        ),
    ],
    "provider": [
        Starter(
            "q-referral",
            "Physiotherapy referrals",
            "Clinic protocol",
            "book-open",
            "prompt",
            "What's our referral process for physiotherapy?",
        ),
    ],
    "admin": [
        Starter(
            "q-policy-admin",
            "Cancellation policy",
            "What patients are told",
            "book-open",
            "prompt",
            "What is the late cancellation policy?",
        ),
    ],
}


def starters_for(role: str) -> list[tuple[str, list[Starter]]]:
    groups: dict[str, list[Starter]] = {}
    for flow in flows_for(role):
        groups.setdefault(flow.group, []).append(
            Starter(flow.id, flow.title, flow.description, flow.icon, "flow")
        )
    questions = PROMPTS.get(role, [])
    if questions:
        groups.setdefault("Ask a question", []).extend(questions)
    return list(groups.items())
