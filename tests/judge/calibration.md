# Judge calibration log

The offline judge (`python -m tests.judge.run_judge`) **reports and never gates** (harness
spec section 11). A verdict becomes a gating signal only when this log shows it is right: every
verdict a person checked, and whether they agreed.

Add a row whenever you review a verdict in `tests/reports/judge-verdicts.json`. *Overturned*
means the person disagreed; a low overturn rate over many rows, per rubric, is the evidence
that would justify letting that rubric fail a build.

| Date | Case | Rubric | Judge said | Reviewed by | Agreed? | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-11 | ai-001-symptoms-routed-to-a-specialty | groundedness | FAIL (HIGH): cited `doc:urgent-care#chest-pain` for knee-pain advice | Claude (coding agent) — awaiting a human's review | Yes | A real defect: the agent cited every retrieved passage, and the chest-pain guide was retrieved for sharing the word "pain". Fixed (`agent.sources_used`: cite only passages the answer draws on); re-judged PASS. |
| 2026-11-11 | all five `ai-` cases | all | 9 PASS after the fix | Claude (coding agent) — awaiting a human's review | Yes | Each reasoning quoted the transcript; spot-checked against `ai-run.json`. |

Status: **0 human-reviewed rows.** No rubric is eligible to gate.
