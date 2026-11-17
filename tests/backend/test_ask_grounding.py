"""Ask answers from real records: citations resolve, nothing is invented. Fakes only."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.endpoints import context as context_endpoints
from backend.context.ask import AskService
from test_ask_retrieval import FAKE_TOKEN, make_state


class SecureModel:
    """Fake secure transport (has ``check``): records prompts, cites the first record."""

    fixture_mode = True
    base_url = "http://omniroute.invalid"

    def __init__(self, cite_extra: str | None = None, ref_form: bool = False) -> None:
        self.prompts: list[str] = []
        self.cite_extra = cite_extra
        self.ref_form = ref_form

    def check(self, model: str) -> None:
        return None

    def _reply(self, prompt: str) -> str:
        ids = re.findall(r"^REF (\S+):", prompt, re.MULTILINE)
        mark = (lambda i: f"[REF {i}]") if self.ref_form else (lambda i: f"[{i}]")
        text = f"It is on your calendar {mark(ids[0])}." if ids else "I have no records for that."
        return text + (f" Also {mark(self.cite_extra)}." if self.cite_extra else "")

    def generate(self, model, prompt, parameters=None, timeout=None, *, kind=None, **kw):
        self.prompts.append(prompt)
        return {"content": self._reply(prompt), "input_tokens": 1, "output_tokens": 1}

    def generate_stream(self, model, prompt, headers=None, max_tokens=None, timeout=None, **kw):
        self.prompts.append(prompt)
        yield self._reply(prompt)


class PlainModel:
    """Fake non-secure transport: no ``check``."""

    fixture_mode = True
    base_url = "http://omniroute.invalid"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, model, prompt, parameters=None, timeout=None, **kw):
        self.prompts.append(prompt)
        return {"content": "ok", "input_tokens": 1, "output_tokens": 1}


def client_for(model: Any) -> TestClient:
    app = FastAPI()
    app.include_router(context_endpoints.router)
    for key, value in vars(make_state(datetime.now(UTC))).items():
        setattr(app.state, key, value)
    app.state.ask_service = AskService(transport=model, remote_provider_allowed=True)
    return TestClient(app)


def ask(client: TestClient, question: str) -> dict[str, Any]:
    response = client.post("/api/context/ask", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()


def test_citation_resolves_to_the_real_record() -> None:
    model = SecureModel()
    body = ask(client_for(model), "When is my piano lesson?")
    assert body["status"] == "answered" and body["grounded"] is True
    cite = body["citations"][0]
    assert cite["record_id"].startswith("cal-") and cite["title"] == "Piano lesson with Marta"
    assert cite["domain"] == "calendar" and cite["link"] == "/calendar"
    assert body["records"][0]["id"] == cite["record_id"]
    assert body["provenance"]["context_records"] == [cite["record_id"]]
    assert body["policy"]["denied"] is False


def test_prompt_carries_real_content_never_placeholder_ids() -> None:
    model = SecureModel()
    body = ask(client_for(model), "Tell me about evidence-0000")
    assert "evidence-0000" not in json.dumps(body["provenance"])
    assert all("evidence-0000" not in p.split("Question:")[0] for p in model.prompts)
    prompt = SecureModel()
    ask(client_for(prompt), "When is my piano lesson?")
    assert "Piano lesson with Marta" in prompt.prompts[0]
    assert FAKE_TOKEN not in prompt.prompts[0]


def test_ref_marker_form_resolves_to_the_real_record() -> None:
    body = ask(client_for(SecureModel(ref_form=True)), "When is my piano lesson?")
    assert body["grounded"] is True and body["ungrounded_claims"] == []
    cite = body["citations"][0]
    assert cite["record_id"].startswith("cal-") and cite["title"] == "Piano lesson with Marta"
    assert cite["domain"] == "calendar" and cite["when"]
    assert body["records"][0]["id"] == cite["record_id"]


def test_unknown_ref_id_is_ungrounded_and_dropped() -> None:
    body = ask(client_for(SecureModel(cite_extra="cal-ffffffffff", ref_form=True)), "When is my piano lesson?")
    assert body["ungrounded_claims"] == ["cal-ffffffffff"] and body["grounded"] is False
    assert all(c["record_id"] != "cal-ffffffffff" for c in body["citations"]) and body["citations"]


def test_citation_parser_accepts_both_forms_and_lists() -> None:
    from backend.context.ask import _citation_markers
    text = "a [REF mail-6b1d4abb1a] b [cal-0123456789] c [REF: x-1, REF y-2] d [see note] e [REF  z_3]"
    assert _citation_markers(text) == ["mail-6b1d4abb1a", "cal-0123456789", "x-1", "y-2", "z_3"]


def test_prompt_asks_for_the_form_the_parser_reads() -> None:
    model = SecureModel()
    ask(client_for(model), "When is my piano lesson?")
    from backend.context.ask import _citation_markers
    instruction = [ln for ln in model.prompts[0].splitlines() if "[REF" in ln and "exactly like" in ln]
    assert instruction and _citation_markers(instruction[0])[0] == "mail-1234abcd"


def test_failed_domain_is_named_only_when_one_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    clean = ask(client_for(SecureModel()), "When is my piano lesson?")
    assert "could not search" not in clean["answer"]
    from backend.context.retrieval import sources, service

    async def boom(state, intent, limit):
        raise RuntimeError("down")
    monkeypatch.setattr(service, "ADAPTERS", {**sources.ADAPTERS, "bills": boom})
    hurt = ask(client_for(SecureModel()), "When is my piano lesson?")
    assert hurt["answer"].endswith("I could not search bills just now.") and hurt["citations"]


def test_a_cited_id_that_was_not_retrieved_is_ungrounded() -> None:
    body = ask(client_for(SecureModel(cite_extra="cal-ffffffffff")), "When is my piano lesson?")
    assert body["grounded"] is False and body["ungrounded_claims"] == ["cal-ffffffffff"]
    assert [c["record_id"] for c in body["citations"]] != ["cal-ffffffffff"]


def test_a_lookup_for_something_that_does_not_exist_is_answered_in_code() -> None:
    model = SecureModel()
    body = ask(client_for(model), "What did Zebulon say about the glacier?")
    assert body["answer"].startswith("I found nothing about")
    assert "calendar" in body["answer"] and body["grounded"] is False and body["citations"] == []
    assert model.prompts == []


def test_evidence_0000_question_says_plainly_it_does_not_exist() -> None:
    model = SecureModel()
    body = ask(client_for(model), "can you tell me about evidence-0000?")
    assert "I found nothing about" in body["answer"] and model.prompts == []


def test_non_secure_transport_never_receives_a_record_snippet() -> None:
    model = PlainModel()
    body = ask(client_for(model), "When is my piano lesson?")
    assert body["status"] == "answered" and body["records"] == []
    assert all("Piano lesson" not in p and "Marta" not in p for p in model.prompts)


def test_service_refuses_personal_records_on_a_non_secure_transport() -> None:
    import asyncio

    from backend.context.ask import AskRequest
    from backend.context.retrieval import retrieve

    state = make_state()
    retrieval = asyncio.run(retrieve(state, "piano lesson", datetime.now(UTC)))
    model = PlainModel()
    outcome = AskService(transport=model, remote_provider_allowed=True).ask(
        AskRequest(question="piano lesson"), retrieval=retrieval
    )
    assert model.prompts == [] or all("Piano" not in p for p in model.prompts)
    assert outcome.body["status"] in ("answered", "policy_denied")


def test_stream_done_body_carries_records() -> None:
    client = client_for(SecureModel())
    with client.stream("POST", "/api/context/ask/stream", json={"question": "When is my piano lesson?"}) as r:
        text = "".join(r.iter_text())
    done = [json.loads(b.split("data: ", 1)[1]) for b in text.split("\n\n") if b.startswith("event: done")]
    assert done and done[0]["records"][0]["title"] == "Piano lesson with Marta"
    assert done[0]["citations"][0]["record_id"].startswith("cal-")


def test_client_supplied_context_still_works() -> None:
    client = client_for(SecureModel())
    response = client.post("/api/context/ask", json={
        "question": "summarize",
        "context": {"fixture_refs": ["INC-7"], "summaries": {"INC-7": "disk full"}},
    })
    assert response.status_code == 200
    assert response.json()["provenance"]["context_records"] == ["INC-7"]
async def _fake_calendar_context(*args: Any, **kwargs: Any) -> object:
    return object()
def _with_calendar_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_endpoints, "build_calendar_context", _fake_calendar_context)
def _stub_delete_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.context import ask_delete
    monkeypatch.setattr(
        ask_delete, "propose_calendar_delete",
        lambda *a, **k: {"status": "delete_proposal", "answer": "Review the list.",
                         "delete_proposal": {"id": "dp-1", "rows": []}},
    )
def test_delete_gate_false_positive_still_gets_cited_records(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_calendar_context(monkeypatch)
    model = SecureModel()
    body = ask(client_for(model), "Which meetings did Marta cancel?")
    assert body["grounded"] is True and "delete_proposal" not in body
    assert body["citations"][0]["title"] == "Piano lesson with Marta"
    assert any("Piano lesson with Marta" in p for p in model.prompts)
def test_delete_gate_false_positive_stream_gets_cited_records(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_calendar_context(monkeypatch)
    client = client_for(SecureModel())
    with client.stream("POST", "/api/context/ask/stream", json={"question": "Which meetings did Marta cancel?"}) as r:
        text = "".join(r.iter_text())
    done = [json.loads(b.split("data: ", 1)[1]) for b in text.split("\n\n") if b.startswith("event: done")]
    assert done and done[0]["citations"][0]["title"] == "Piano lesson with Marta"
def test_delete_request_with_zero_retrieved_records_still_proposes(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_calendar_context(monkeypatch)
    _stub_delete_proposal(monkeypatch)
    body = ask(client_for(SecureModel()), "remove all my appointments with Zebulon")
    assert body["status"] == "delete_proposal" and body["delete_proposal"]["id"] == "dp-1"
    assert not body["answer"].startswith("I found nothing about")
def test_stream_delete_request_with_zero_retrieved_records_still_proposes(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_calendar_context(monkeypatch)
    _stub_delete_proposal(monkeypatch)
    client = client_for(SecureModel())
    with client.stream("POST", "/api/context/ask/stream", json={"question": "remove all my appointments with Zebulon"}) as r:
        text = "".join(r.iter_text())
    assert "event: delete_proposal" in text and "I found nothing about" not in text
