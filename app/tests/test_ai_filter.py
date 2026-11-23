"""Tests for ai_filter.py — no real Gemini calls. The fake client
returns the SDK's own answer models so screen_one's serialization is
exercised exactly as with a live response.
Run with: python -m pytest app/tests/test_ai_filter.py
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.genai._gaos.types.decisions.choiceanswer import ChoiceAnswer
from google.genai._gaos.types.decisions.noulanswer import NoulAnswer
from google.genai._gaos.types.decisions.scoreanswer import ScoreAnswer

from app import ai_filter
from app import dedup

JOB = {
    "url": "https://example.com/jobs/1",
    "company": "Acme",
    "title": "Senior Frontend Engineer",
    "location": "Remote",
    "posted_at": None,
    "description": "<p>React and TypeScript, fully remote across Europe.</p>",
}


def _choice(choice: str, confidence: float = 0.9) -> dict:
    return {"choice": choice, "confidence": confidence, "probabilities": {choice: confidence}, "type": "choice"}


def _noul(probability: float) -> dict:
    return {"noul": probability, "confidence": 0.9, "type": "noul"}


def _answers(**overrides) -> dict:
    answers = {
        "work_mode": _choice("remote"),
        "in_relocation_city": _noul(0.05),
        "hireable": _noul(0.9),
        "role_type": _choice("frontend"),
        "seniority": _choice("senior"),
        "stack_fit": {"score": 3.5, "confidence": 0.9, "probabilities": {}, "legend": {}, "type": "score"},
        "other_language_required": _noul(0.05),
    }
    return {**answers, **overrides}


def test_good_remote_frontend_job_passes():
    assert ai_filter.rejection_reason(_answers()) is None


@pytest.mark.parametrize("overrides, expected", [
    ({"work_mode": _choice("hybrid")}, "not remote (hybrid)"),
    ({"work_mode": _choice("onsite")}, "not remote (onsite)"),
    ({"hireable": _noul(0.1)}, "not hireable from Spain/UK/EU, nor in Bangkok, Thailand"),
    ({"work_mode": _choice("onsite"), "in_relocation_city": _noul(0.4)}, "not remote (onsite)"),
    ({"role_type": _choice("backend")}, "role type (backend, senior)"),
    ({"role_type": _choice("mobile")}, "role type (mobile, senior)"),
    ({"role_type": _choice("design")}, "role type (design, senior)"),
    ({"seniority": _choice("manager")}, "seniority (manager)"),
    ({"stack_fit": {"score": 0.8, "confidence": 0.9}}, "stack fit (0.8/4)"),
    ({"other_language_required": _noul(0.9)}, "requires a language other than English/Spanish"),
])
def test_rejections(overrides, expected):
    assert ai_filter.rejection_reason(_answers(**overrides)) == expected


@pytest.mark.parametrize("role_type", ["mobile", "design"])
def test_junior_mobile_and_design_pass(role_type):
    answers = _answers(role_type=_choice(role_type), seniority=_choice("junior"))
    assert ai_filter.rejection_reason(answers) is None


def test_design_role_skips_stack_fit():
    answers = _answers(role_type=_choice("design"), seniority=_choice("junior"),
                       stack_fit={"score": 0.0, "confidence": 0.9})
    assert ai_filter.rejection_reason(answers) is None


@pytest.mark.parametrize("work_mode", ["hybrid", "onsite"])
def test_non_remote_in_bangkok_passes(work_mode):
    answers = _answers(work_mode=_choice(work_mode), in_relocation_city=_noul(0.95))
    assert ai_filter.rejection_reason(answers) is None


def test_bangkok_role_still_needs_the_right_role_type():
    answers = _answers(work_mode=_choice("onsite"), in_relocation_city=_noul(0.95),
                       role_type=_choice("backend"))
    assert ai_filter.rejection_reason(answers) == "role type (backend, senior)"


def test_questions_only_allow_relocating_to_bangkok():
    questions = ai_filter.build_questions(["React"])
    assert "Bangkok, Thailand" in questions["hireable"]["instructions"]
    assert "other cities in the same country" in questions["hireable"]["instructions"]
    assert "independent contractor" in questions["hireable"]["instructions"]
    assert "Bangkok, Thailand" in questions["in_relocation_city"]["instructions"]
    assert "Thailand with no specific city named" in questions["in_relocation_city"]["instructions"]
    assert "Thailand with no specific city named" in questions["hireable"]["instructions"]


def test_unsure_work_mode_keeps_the_job():
    assert ai_filter.rejection_reason(_answers(work_mode=_choice("hybrid", confidence=0.4))) is None


def test_stack_comes_only_from_core_stack():
    profile = {"identity": {"name": "Private"}, "core_stack": {"a": ["React"], "b": ["Go", "Node.js"]}}
    assert ai_filter.load_stack(profile) == ["React", "Go", "Node.js"]


def test_input_strips_html_and_marks_untrusted():
    text = ai_filter.build_input(JOB)
    assert "<p>" not in text and "React and TypeScript" in text
    assert "untrusted" in text


def test_screen_one_serializes_sdk_answers():
    response = SimpleNamespace(answers={
        "work_mode": ChoiceAnswer(choice="remote", confidence=0.9, probabilities={"remote": 0.9}),
        "hireable": NoulAnswer(noul=0.8, confidence=0.9),
        "stack_fit": ScoreAnswer(score=3.0, confidence=0.8, probabilities={}, legend={}),
    })
    client = MagicMock()
    client.decisions.create.return_value = response
    questions = {key: {} for key in response.answers}

    answers = ai_filter.screen_one(client, questions, JOB)

    assert answers["work_mode"]["choice"] == "remote"
    assert answers["hireable"]["noul"] == 0.8
    assert answers["stack_fit"]["score"] == 3.0
    assert client.decisions.create.call_args.kwargs["model"] == ai_filter.MODEL


def test_screen_one_raises_on_missing_answer():
    client = MagicMock()
    client.decisions.create.return_value = SimpleNamespace(answers={})
    with pytest.raises(RuntimeError, match="work_mode"):
        ai_filter.screen_one(client, {"work_mode": {}}, JOB)


def test_questions_are_valid_for_the_sdk():
    from google.genai._gaos.types.decisions.createdecisionrequest import CreateDecisionRequest

    questions = ai_filter.build_questions(["React", "TypeScript"])
    CreateDecisionRequest(model=ai_filter.MODEL, input="x", questions=questions)


def test_rejected_jobs_skip_claude_evaluation(tmp_path):
    other_job = {**JOB, "url": "https://example.com/jobs/2", "title": "Backend Engineer"}
    unscreened_job = {**JOB, "url": "https://example.com/jobs/3", "title": "Frontend Developer"}
    with dedup.connect(str(tmp_path / "test.sqlite3")) as conn:
        for job in (JOB, other_job, unscreened_job):
            dedup.save_details(conn, job, passed_filters=True)
        assert len(dedup.get_ai_filter_queue(conn)) == 3

        dedup.save_ai_filter(conn, JOB["url"], None, "{}", ai_filter.MODEL)
        dedup.save_ai_filter(conn, other_job["url"], "role type (backend, senior)", "{}", ai_filter.MODEL)

        assert [j["url"] for j in dedup.get_ai_filter_queue(conn)] == [unscreened_job["url"]]
        assert len(dedup.get_ai_filter_queue(conn, include_screened=True)) == 3
        evaluation_queue = {j["url"] for j in dedup.get_unevaluated_candidates(conn)}
        assert evaluation_queue == {JOB["url"], unscreened_job["url"]}
