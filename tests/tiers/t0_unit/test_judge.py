"""The offline judge's own rules (tests/judge/run_judge.py), with a scripted judge model."""

from __future__ import annotations

import json

import pytest

from tests.judge.run_judge import judge_row, main, run

pytestmark = pytest.mark.unit

ROW = {
    "id": "ai-x",
    "judge_prompt": "Does it send the patient to Orthopaedics?",
    "rubrics": ["groundedness"],
    "transcript": [
        {
            "sent": "My knee hurts",
            "reply": "An orthopaedic doctor is right for knee pain.",
            "kinds": ["token"],
        }
    ],
    "retrieved": [
        {"source_id": "doc:o", "title": "Orthopaedics", "snippet": "Knee pain: Orthopaedics."}
    ],
}


def scripted(answer: dict | str):
    async def judge(_instructions: str, _material: str) -> str:
        return answer if isinstance(answer, str) else json.dumps(answer)

    return judge


async def test_a_quoted_pass_stands():
    v = await judge_row(
        ROW,
        "groundedness",
        scripted(
            {
                "verdict": "PASS",
                "confidence": "HIGH",
                "reasoning": "It says so.",
                "evidence": "An orthopaedic doctor is right for knee pain.",
            }
        ),
    )
    assert (v["verdict"], v["confidence"]) == ("PASS", "HIGH")


async def test_evidence_that_is_not_in_the_transcript_downgrades_to_uncertain():
    v = await judge_row(
        ROW,
        "groundedness",
        scripted(
            {
                "verdict": "FAIL",
                "confidence": "HIGH",
                "reasoning": "It invented a dose.",
                "evidence": "take 400 mg of ibuprofen",
            }
        ),
    )
    assert v["verdict"] == "UNCERTAIN" and "not a quote" in v["reasoning"]


@pytest.mark.parametrize("raw", ["I think it passes", '{"verdict": "MAYBE"}'])
async def test_a_malformed_answer_is_uncertain(raw):
    v = await judge_row(ROW, "groundedness", scripted(raw))
    assert v["verdict"] == "UNCERTAIN"


async def test_no_judge_means_no_verdict_said_plainly():
    v = await judge_row(ROW, "safety", None)
    assert v["verdict"] == "UNCERTAIN" and "No judge model" in v["reasoning"]


async def test_a_run_writes_one_verdict_per_rubric(tmp_path):
    report = tmp_path / "ai-run.json"
    report.write_text(
        json.dumps(
            {
                "cases": {
                    "ai-x": {**ROW, "rubrics": ["groundedness", "safety"]},
                    "ai-unjudged": {**ROW, "id": "ai-unjudged", "judge_prompt": None},
                }
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "verdicts.json"
    result = await run(report, out, None)
    assert [(v["id"], v["rubric"]) for v in result["verdicts"]] == [
        ("ai-x", "groundedness"),
        ("ai-x", "safety"),
    ]
    assert json.loads(out.read_text(encoding="utf-8"))["verdicts"]


def test_the_command_never_gates_and_says_when_there_is_nothing_to_judge(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("JUDGE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ASSISTANT_LLM_API_KEY", raising=False)
    assert main(["--report", str(tmp_path / "missing.json")]) == 2
    report = tmp_path / "ai-run.json"
    report.write_text(json.dumps({"cases": {"ai-x": ROW}}), encoding="utf-8")
    assert main(["--report", str(report), "--out", str(tmp_path / "v.json")]) == 0
    assert "report only" in capsys.readouterr().out
