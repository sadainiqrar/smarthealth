"""Red flags: messages that get urgent-care guidance at once, and never a booking.

Deterministic on purpose (spec 9.1): whether to tell someone to call the emergency number is
not a decision left to a model's mood. The phrases follow the clinic's own urgent-care guide
(knowledge corpus `urgent-care.md`); a hit answers from that guide and offers no booking.
Over-triggering is the safe failure -- "chest pain after a workout" still gets the advice.
"""

from __future__ import annotations

import re

RED_FLAGS: dict[str, tuple[str, ...]] = {
    "chest-pain": (r"chest (pain|pressure|tightness)", r"pain .* (left )?arm .* jaw",
                   r"heart attack"),
    "stroke": (r"face (is )?droop", r"slurred speech", r"speech (is )?slurred", r"\bstroke\b",
               r"sudden(ly)? (severe )?headache", r"can'?t (move|lift) (my|one) arm",
               r"sudden (loss of )?(vision|confusion)"),
    "breathing": (r"can'?t breathe", r"cannot breathe", r"difficulty breathing",
                  r"struggling to breathe", r"chok(ing|es)"),
    "bleeding": (r"bleeding (that )?(won'?t|will not|doesn'?t) stop", r"severe bleeding",
                 r"lot of blood"),
    "collapse": (r"collapsed", r"unconscious", r"won'?t wake", r"seizure", r"fitting"),
    "self-harm": (r"kill myself", r"suicid", r"self[- ]harm", r"hurt myself",
                  r"end (my|it all)", r"don'?t want to (live|be alive)"),
}

_COMPILED = {name: [re.compile(p, re.IGNORECASE) for p in patterns]
             for name, patterns in RED_FLAGS.items()}

GUIDANCE = {
    "self-harm": ("If you are thinking about harming yourself, please call the emergency number "
                  "or a crisis line **now**. You will be listened to."),
}
DEFAULT_GUIDANCE = ("What you describe can be an emergency. **Call your local emergency number "
                    "now**, or go to the nearest emergency department. Please don't wait for an "
                    "appointment.")


def red_flag(text: str) -> str | None:
    for name, patterns in _COMPILED.items():
        if any(p.search(text) for p in patterns):
            return name
    return None


def guidance(flag: str) -> str:
    return GUIDANCE.get(flag, DEFAULT_GUIDANCE)
