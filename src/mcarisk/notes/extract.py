"""Modification and settlement terms from collections-log notes: regex baseline + optional LLM.

What is extracted (per note, :class:`NoteTerms`):
  * ``new_payment`` (dollars): the modified installment (METRICS.md §1 ``modification``: "the payment
    amount changes from that date"), ``payment_period`` (day / week), ``previous_payment``;
  * ``settlement_amount`` (dollars) and ``settlement_installments`` when stated.

Extractors
----------
``regex``  deterministic patterns for the phrasings in the log (``reduced from $A to $B``,
           ``new pmt 248/day``, ``Lowered to $B``, ``settle for 35.7k``, ``settlement approved by mgmt
           $15,900``).
``llm``    an OpenAI-compatible chat model through OpenRouter (``base_url`` below), structured output
           (JSON schema), validated by pydantic. Credentials: ``OPENROUTER_API_KEY`` and ``MODEL`` from the
           environment, else from the dotenv file at ``MCARISK_DOTENV`` (default: the repo's parent
           directory ``.env``). The key is never printed, logged or written anywhere.

Cache: every LLM extraction is stored in ``cache/llm_notes.jsonl`` keyed by
``sha256(model | prompt version | note text)``. The file is committed (synthetic notes only), so the
pipeline runs without a key, without network access and deterministically.

Which extractor the pipeline uses (``MCARISK_NOTES_EXTRACTOR``):
  ``auto`` (default)  LLM results if the cache covers every note, else regex for all notes. Never calls
                      the network.
  ``regex``           regex only.
  ``llm``             LLM; cache misses are sent to the API (needs a key). If the key is missing or a
                      call fails, the missing notes fall back to regex and the fallback is recorded.
Both extractors' outputs are always written (``notes_extractions``) so E9 can compare them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcarisk import paths

PROMPT_VERSION = "notes-v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CACHE_PATH = paths.ROOT / "cache" / "llm_notes.jsonl"
DOTENV_ENV = "MCARISK_DOTENV"
EXTRACTOR_ENV = "MCARISK_NOTES_EXTRACTOR"
DEFAULT_DOTENV = paths.ROOT.parent / ".env"
BATCH_SIZE = 20

E_REGEX = "regex"
E_LLM = "llm"


class NoteTerms(BaseModel):
    """Structured terms of one note. Amounts are dollars as written (None when not stated)."""

    model_config = ConfigDict(extra="forbid")

    new_payment: float | None = Field(None, ge=0)
    payment_period: Literal["day", "week"] | None = None
    previous_payment: float | None = Field(None, ge=0)
    settlement_amount: float | None = Field(None, ge=0)
    settlement_installments: int | None = Field(None, ge=1)


class _Item(NoteTerms):
    id: int


class _Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[_Item]


def _num(s: str) -> float:
    return float(Decimal(s.replace(",", "")))


_AMT = r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_RE_FROM_TO = re.compile(r"(?:reduced|lowered|changed)\s+from\s+" + _AMT + r"\s+to\s+" + _AMT, re.I)
_RE_NEW_PMT = re.compile(
    r"new\s+(?:pmt|payment)\s*(?:of\s*)?" + _AMT + r"(?:\s*/\s*(day|wk|week|daily|weekly))?", re.I
)
_RE_LOWERED = re.compile(r"(?:lowered|reduced)\s+to\s+" + _AMT, re.I)
_RE_APPROVED_PMT = re.compile(r"approved\s+pmt\s+" + _AMT, re.I)
_RE_PERIOD = re.compile(r"/\s*(day|wk|week)|\b(daily|weekly)\b", re.I)
_RE_SETTLE_K = re.compile(r"settle\w*\s+(?:for\s+)?\$?\s*(\d+(?:\.\d+)?)\s*k\b", re.I)
_RE_SETTLE_AMT = re.compile(r"settle\w*[^$\d]{0,40}" + _AMT + r"(?!\s*k\b)", re.I)
_RE_INSTALL = re.compile(r"(\d+)\s+(?:installments|instalments|payments|pmts)", re.I)


def _period(tok: str | None) -> str | None:
    if not tok:
        return None
    t = tok.lower()
    return "day" if t in ("day", "daily") else "week"


def extract_regex(text: str | None) -> NoteTerms:
    """Regex baseline (deterministic)."""
    t = text or ""
    out: dict[str, Any] = {}
    m = _RE_FROM_TO.search(t)
    if m:
        out["previous_payment"], out["new_payment"] = _num(m.group(1)), _num(m.group(2))
    else:
        m = _RE_NEW_PMT.search(t) or _RE_LOWERED.search(t) or _RE_APPROVED_PMT.search(t)
        if m:
            out["new_payment"] = _num(m.group(1))
            if m.re is _RE_NEW_PMT and m.group(2):
                out["payment_period"] = _period(m.group(2))
    if "new_payment" in out and "payment_period" not in out:
        p = _RE_PERIOD.search(t)
        if p:
            out["payment_period"] = _period(p.group(1) or p.group(2))
    m = _RE_SETTLE_K.search(t)
    if m:
        out["settlement_amount"] = round(_num(m.group(1)) * 1000, 2)
    else:
        m = _RE_SETTLE_AMT.search(t)
        if m:
            out["settlement_amount"] = _num(m.group(1))
    if "settlement_amount" in out:
        m = _RE_INSTALL.search(t)
        if m:
            out["settlement_installments"] = int(m.group(1))
    return NoteTerms(**out)


# ------------------------------------------------------------------------------------------- LLM

SYSTEM_PROMPT = (
    "You extract payment terms from short collections-log notes written by staff at a merchant cash "
    "advance funder. For each note return:\n"
    "- new_payment: the NEW (modified/reduced) remittance amount in dollars, if the note states one; "
    "otherwise null. If the note says 'from $A to $B', new_payment is B and previous_payment is A.\n"
    "- payment_period: 'day' or 'week' if the period of new_payment is stated, else null.\n"
    "- previous_payment: the old remittance amount if stated, else null.\n"
    "- settlement_amount: the total settlement amount in dollars if stated (expand '35.7k' to 35700), "
    "else null.\n"
    "- settlement_installments: number of settlement installments if a number is stated, else null.\n"
    "Copy numbers exactly as written (do not round or guess). Never invent values that are not in the "
    "note. Return one item per input id."
)


def _json_schema() -> dict[str, Any]:
    num = {"type": ["number", "null"]}
    item = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "new_payment": num,
            "payment_period": {"type": ["string", "null"], "enum": ["day", "week", None]},
            "previous_payment": num,
            "settlement_amount": num,
            "settlement_installments": {"type": ["integer", "null"]},
        },
        "required": [
            "id",
            "new_payment",
            "payment_period",
            "previous_payment",
            "settlement_amount",
            "settlement_installments",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item}},
        "required": ["items"],
        "additionalProperties": False,
    }


def cache_key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}|{PROMPT_VERSION}|{text}".encode()).hexdigest()


def read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class LLMConfig:
    model: str | None
    api_key: str | None = field(default=None, repr=False)

    @property
    def usable(self) -> bool:
        return bool(self.model and self.api_key)


def load_config() -> LLMConfig:
    """``OPENROUTER_API_KEY`` / ``MODEL`` from the environment, else from the dotenv file."""
    env = read_dotenv(Path(os.environ.get(DOTENV_ENV, str(DEFAULT_DOTENV))))
    key = os.environ.get("OPENROUTER_API_KEY") or env.get("OPENROUTER_API_KEY")
    model = os.environ.get("MODEL") or env.get("MODEL")
    return LLMConfig(model=model or None, api_key=key or None)


class NotesCache:
    """Append-only JSONL cache: {"key", "model", "prompt_version", "text", "terms", "usage"}."""

    def __init__(self, path: Path = CACHE_PATH) -> None:
        self.path = Path(path)
        self.entries: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    e = json.loads(line)
                    self.entries[e["key"]] = e

    def get(self, model: str, text: str) -> NoteTerms | None:
        e = self.entries.get(cache_key(model, text))
        return None if e is None else NoteTerms(**e["terms"])

    def put(self, model: str, text: str, terms: NoteTerms, usage: dict[str, Any]) -> None:
        k = cache_key(model, text)
        e = {
            "key": k,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "text": text,
            "terms": terms.model_dump(),
            "usage": usage,
        }
        self.entries[k] = e
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")

    def models(self) -> set[str]:
        return {e["model"] for e in self.entries.values()}


def make_client(cfg: LLMConfig) -> Any:
    from openai import OpenAI

    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=cfg.api_key, timeout=60.0, max_retries=1)


@dataclass
class LLMUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cache_hits: int = 0
    errors: list[str] = field(default_factory=list)


def _usage_dict(resp: Any) -> dict[str, Any]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    extra = getattr(u, "model_extra", None) or {}
    cost = extra.get("cost", getattr(u, "cost", None))
    return {
        "input_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "completion_tokens", 0) or 0),
        "cost_usd": float(cost or 0.0),
    }


def llm_extract_batch(client: Any, model: str, texts: list[str]) -> tuple[list[NoteTerms], dict[str, Any]]:
    """One structured-output call for a batch of notes. Raises on transport/validation failure."""
    payload = [{"id": i, "note": t} for i, t in enumerate(texts)]
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "note_terms", "strict": True, "schema": _json_schema()},
        },
        extra_body={"usage": {"include": True}},
    )
    content = resp.choices[0].message.content or ""
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`").removeprefix("json").strip()
    batch = _Batch.model_validate_json(content)
    by_id = {it.id: it for it in batch.items}
    missing = [i for i in range(len(texts)) if i not in by_id]
    if missing:
        raise ValueError(f"LLM returned no item for ids {missing}")
    terms = [NoteTerms(**by_id[i].model_dump(exclude={"id"})) for i in range(len(texts))]
    return terms, _usage_dict(resp)


def run_llm(
    texts: Iterable[str],
    *,
    cache: NotesCache,
    cfg: LLMConfig,
    allow_network: bool,
    client_factory: Callable[[LLMConfig], Any] = make_client,
    batch_size: int = BATCH_SIZE,
) -> tuple[dict[str, NoteTerms], LLMUsage]:
    """LLM terms for every text that is cached or (if allowed) fetched. Missing texts are omitted."""
    usage = LLMUsage()
    out: dict[str, NoteTerms] = {}
    uniq = sorted(set(texts))
    model = cfg.model
    if not model:
        usage.errors.append("MODEL not configured")
        return out, usage
    todo = []
    for t in uniq:
        hit = cache.get(model, t)
        if hit is not None:
            out[t] = hit
            usage.cache_hits += 1
        else:
            todo.append(t)
    if not todo or not allow_network:
        return out, usage
    if not cfg.api_key:
        usage.errors.append("OPENROUTER_API_KEY not configured")
        return out, usage
    client = client_factory(cfg)
    for i in range(0, len(todo), batch_size):
        chunk = todo[i : i + batch_size]
        try:
            terms, u = llm_extract_batch(client, model, chunk)
        except (ValidationError, ValueError) as exc:
            usage.errors.append(f"batch {i // batch_size}: invalid output: {str(exc)[:200]}")
            continue
        except Exception as exc:  # transport / auth / model errors: record and stop calling
            usage.errors.append(f"batch {i // batch_size}: {type(exc).__name__}: {str(exc)[:200]}")
            break
        usage.calls += 1
        usage.input_tokens += u["input_tokens"]
        usage.output_tokens += u["output_tokens"]
        usage.cost_usd += u["cost_usd"]
        share = {k: (v / len(chunk) if isinstance(v, (int, float)) else v) for k, v in u.items()}
        for t, term in zip(chunk, terms, strict=True):
            cache.put(model, t, term, share)
            out[t] = term
    return out, usage


@dataclass
class ExtractionResult:
    rows: list[dict]  # notes_extractions: one row per (collection_id, extractor)
    chosen: str  # extractor used for collections_terms
    usage: LLMUsage
    model: str | None
    fallback_notes: int  # notes that fell back to regex in llm mode
    detail: str


def extract_all(
    notes: list[tuple[int, str | None]],  # (collection_id, notes_raw)
    *,
    mode: str | None = None,
    cache: NotesCache | None = None,
    cfg: LLMConfig | None = None,
    client_factory: Callable[[LLMConfig], Any] = make_client,
) -> ExtractionResult:
    mode = (mode or os.environ.get(EXTRACTOR_ENV, "auto")).strip().lower()
    if mode not in ("auto", "regex", "llm"):
        raise ValueError(f"{EXTRACTOR_ENV} must be auto, regex or llm; got {mode!r}")
    cache = cache if cache is not None else NotesCache()
    cfg = cfg if cfg is not None else load_config()
    if cfg.model is None and cache.models():
        cfg = LLMConfig(model=sorted(cache.models())[0], api_key=cfg.api_key)
    texts = [t for _, t in notes if t]
    llm_terms, usage = (
        run_llm(texts, cache=cache, cfg=cfg, allow_network=(mode == "llm"), client_factory=client_factory)
        if mode != "regex"
        else ({}, LLMUsage())
    )
    covered = all(t in llm_terms for t in texts)
    if mode == "regex":
        chosen, detail = E_REGEX, "regex (configured)"
    elif mode == "auto":
        chosen = E_LLM if covered and texts else E_REGEX
        detail = (
            f"auto: LLM cache covers all {len(set(texts))} distinct notes"
            if chosen == E_LLM
            else f"auto: LLM cache covers {sum(t in llm_terms for t in set(texts))}/{len(set(texts))} notes -> regex"
        )
    else:
        chosen = E_LLM
        detail = "llm (configured)" + ("" if covered else "; missing notes fall back to regex")
    rows: list[dict] = []
    fallback = 0
    for cid, text in notes:
        rx = extract_regex(text)
        rows.append(
            {"collection_id": cid, "extractor": E_REGEX, "used": chosen == E_REGEX, **rx.model_dump()}
        )
        if text and text in llm_terms:
            rows.append(
                {
                    "collection_id": cid,
                    "extractor": E_LLM,
                    "used": chosen == E_LLM,
                    **llm_terms[text].model_dump(),
                }
            )
        elif chosen == E_LLM:
            fallback += 1
            rows[-1]["used"] = True  # regex row stands in for the missing LLM result
    return ExtractionResult(rows, chosen, usage, cfg.model, fallback, detail)


def main(argv: list[str] | None = None) -> int:
    """Populate the LLM cache for every collections note: ``python -m mcarisk.notes.extract --llm``."""
    import duckdb

    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", type=Path, default=paths.DB_PATH)
    ap.add_argument("--llm", action="store_true", help="call the API for cache misses")
    args = ap.parse_args(argv)
    con = duckdb.connect(str(args.db), read_only=True)
    try:
        notes = con.execute("SELECT collection_id, notes_raw FROM collections_std ORDER BY 1").fetchall()
    finally:
        con.close()
    t0 = time.perf_counter()
    res = extract_all(notes, mode="llm" if args.llm else "auto")
    u = res.usage
    print(
        f"[notes] model={res.model} chosen={res.chosen} calls={u.calls} cache_hits={u.cache_hits} "
        f"tokens_in={u.input_tokens} tokens_out={u.output_tokens} cost_usd={u.cost_usd:.6f} "
        f"fallback={res.fallback_notes} ({time.perf_counter() - t0:.1f}s)"
    )
    for e in u.errors:
        print(f"[notes] error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
