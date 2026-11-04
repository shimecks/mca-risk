"""Notes extraction: regex baseline, LLM structured-output schema, cache behaviour (client mocked: these
tests never touch the network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from mcarisk.notes import extract as nx


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Merchant requested reconciliation, pmt reduced from $1,072.92 to $596.66 eff 07/18",
            {"new_payment": 596.66, "previous_payment": 1072.92},
        ),
        ("approved recon - new pmt 248/day per bank stmts", {"new_payment": 248.0, "payment_period": "day"}),
        (
            "approved recon - new pmt 1,050/wk per bank stmts",
            {"new_payment": 1050.0, "payment_period": "week"},
        ),
        ("Owner called, sales down. Lowered to $400.24. revisit in 30d", {"new_payment": 400.24}),
        ("Agreed to settle for 35.7k in installments", {"settlement_amount": 35700.0}),
        ("settlement approved by mgmt $15,900", {"settlement_amount": 15900.0}),
        ("Settled - see agreement in drive", {}),
        ("ACH revoked (R08 x3). Default notice sent", {}),
        ("uncollectible per policy", {}),
        (None, {}),
    ],
)
def test_regex_extractor(text, expected) -> None:
    got = nx.extract_regex(text).model_dump()
    want = nx.NoteTerms(**expected).model_dump()
    assert got == want


def test_schema_validation() -> None:
    nx.NoteTerms(new_payment=1.5, payment_period="day")
    for bad in ({"new_payment": -1}, {"payment_period": "month"}, {"settlement_installments": 0}, {"foo": 1}):
        with pytest.raises(ValidationError):
            nx.NoteTerms(**bad)
    with pytest.raises(ValidationError):
        nx._Batch.model_validate_json(
            '{"items": [{"id": 0, "new_payment": "abc", "payment_period": null, '
            '"previous_payment": null, "settlement_amount": null, '
            '"settlement_installments": null}]}'
        )
    s = nx._json_schema()
    assert s["properties"]["items"]["items"]["additionalProperties"] is False


class FakeClient:
    """Minimal stand-in for ``openai.OpenAI``: answers with the regex extraction of each note."""

    def __init__(self, mode: str = "ok") -> None:
        self.calls = 0
        self.mode = mode
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        assert kw["response_format"]["type"] == "json_schema"
        if self.mode == "auth":
            raise RuntimeError("401 unauthorized")
        items = json.loads(kw["messages"][1]["content"])
        out = [{"id": it["id"], **nx.extract_regex(it["note"]).model_dump()} for it in items]
        if self.mode == "garbage":
            content = "not json"
        elif self.mode == "drop":
            content = json.dumps({"items": out[:-1]})
        else:
            content = "```json\n" + json.dumps({"items": out}) + "\n```"
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, model_extra={"cost": 0.001})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage
        )


NOTES = [
    (1, "approved recon - new pmt 248/day per bank stmts"),
    (2, "Agreed to settle for 35.7k in installments"),
    (3, None),
    (4, "approved recon - new pmt 248/day per bank stmts"),
]


def test_llm_then_cache(tmp_path) -> None:
    cache = nx.NotesCache(tmp_path / "c.jsonl")
    cfg = nx.LLMConfig(model="test/model", api_key="k")
    fake = FakeClient()
    res = nx.extract_all(NOTES, mode="llm", cache=cache, cfg=cfg, client_factory=lambda c: fake)
    assert fake.calls == 1 and res.chosen == "llm" and res.usage.input_tokens == 100
    assert res.usage.cost_usd == pytest.approx(0.001) and res.fallback_notes == 1  # the None note
    used = [r for r in res.rows if r["used"]]
    assert len(used) == 4 and {r["extractor"] for r in used} == {"llm", "regex"}
    # file holds one line per distinct note, no credentials
    lines = (tmp_path / "c.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all('"k"' not in ln for ln in lines)
    # a new process with the same cache: no calls, auto mode picks the LLM
    fake2 = FakeClient()
    res2 = nx.extract_all(
        NOTES,
        mode="auto",
        cache=nx.NotesCache(tmp_path / "c.jsonl"),
        cfg=nx.LLMConfig(model="test/model", api_key=None),
        client_factory=lambda c: fake2,
    )
    assert fake2.calls == 0 and res2.chosen == "llm" and res2.usage.cache_hits == 2
    # a different model is a cache miss -> auto falls back to regex, still no call
    res3 = nx.extract_all(
        NOTES,
        mode="auto",
        cache=nx.NotesCache(tmp_path / "c.jsonl"),
        cfg=nx.LLMConfig(model="other/model", api_key="k"),
        client_factory=lambda c: fake2,
    )
    assert fake2.calls == 0 and res3.chosen == "regex"


def test_cache_key_depends_on_model_prompt_and_text() -> None:
    k = nx.cache_key("m", "t")
    assert k != nx.cache_key("m2", "t") and k != nx.cache_key("m", "t2") and len(k) == 64


@pytest.mark.parametrize("mode", ["auth", "garbage", "drop"])
def test_llm_failures_never_break_the_pipeline(tmp_path, mode) -> None:
    fake = FakeClient(mode)
    res = nx.extract_all(
        NOTES,
        mode="llm",
        cache=nx.NotesCache(tmp_path / "c.jsonl"),
        cfg=nx.LLMConfig(model="m", api_key="k"),
        client_factory=lambda c: fake,
    )
    assert res.usage.errors and res.usage.calls == 0
    assert all(r["extractor"] == "regex" for r in res.rows if r["used"])
    assert res.fallback_notes == 4
    assert not (tmp_path / "c.jsonl").exists()


def test_no_key_means_no_call(tmp_path) -> None:
    fake = FakeClient()
    res = nx.extract_all(
        NOTES,
        mode="llm",
        cache=nx.NotesCache(tmp_path / "c.jsonl"),
        cfg=nx.LLMConfig(model="m", api_key=None),
        client_factory=lambda c: fake,
    )
    assert fake.calls == 0 and "OPENROUTER_API_KEY not configured" in res.usage.errors
    res = nx.extract_all(
        NOTES,
        mode="regex",
        cache=nx.NotesCache(tmp_path / "c.jsonl"),
        cfg=nx.LLMConfig(model="m", api_key="k"),
        client_factory=lambda c: fake,
    )
    assert fake.calls == 0 and res.chosen == "regex"


def test_config_from_dotenv(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text('# comment\nOPENROUTER_API_KEY="abc"\nMODEL=vendor/model\n', encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setenv(nx.DOTENV_ENV, str(env))
    cfg = nx.load_config()
    assert cfg.model == "vendor/model" and cfg.api_key == "abc" and "abc" not in repr(cfg)
    monkeypatch.setenv("MODEL", "env/model")
    assert nx.load_config().model == "env/model"
    monkeypatch.setenv(nx.DOTENV_ENV, str(tmp_path / "missing.env"))
    monkeypatch.delenv("MODEL")
    assert not nx.load_config().usable


def test_committed_cache_covers_every_collections_note(resolve_build) -> None:
    import duckdb

    con = duckdb.connect(str(resolve_build["db"]), read_only=True)
    try:
        used = dict(
            con.execute("SELECT extractor, COUNT(*) FROM notes_extractions WHERE used GROUP BY 1").fetchall()
        )
        n = con.execute("SELECT COUNT(*) FROM collections_std").fetchone()[0]
    finally:
        con.close()
    assert sum(used.values()) == n
