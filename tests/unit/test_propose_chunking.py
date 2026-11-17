"""LLM chunking in the hydration proposer (specs/hydration.md step 4, LLM propose).

chunk_columns splits the unresolved columns into near-equal chunks of at most max_per_call; propose_llm makes one
client.chat call per chunk. Every chunk sees the whole table's column names (table_columns); only the proposed
columns are chunked. A bad response for one chunk rejects that chunk's columns only.

Imports of the new API are done inside helpers so each test fails on its own (ImportError on chunk_columns,
TypeError on the max_columns_per_call keyword) rather than one collection error for the whole file.
"""

import json
import math

import pytest

import gates.hydration.propose as propose_mod
from gates.common.llm_client import LLMResponse
from gates.hydration.match import Candidate
from gates.hydration.profile import ColumnProfile
from gates.hydration.propose import ColumnInput, build_messages, max_tokens_for, split_tokens


def _chunk(items, max_per_call):
    from gates.hydration.propose import chunk_columns

    return chunk_columns(items, max_per_call)


# --- fixtures (synthetic names only) ----------------------------------------------------------------------------

def _profile(col, hits=()):
    return ColumnProfile(col, "string", 1.5, 99.0, list(hits), "f" * 64, list(hits), 12.0)


def _cands():
    return [Candidate(t, t.replace("_", " ").title(), f"Definition of {t}.", "internal", 80.0 - n)
            for n, t in enumerate(("term_alpha", "term_beta", "term_gamma"))]


def _names(n):
    return [f"col_{i:03d}" for i in range(1, n + 1)]


def _inputs(n):
    return [ColumnInput(c, c.replace("_", " "), _profile(c), _cands()) for c in _names(n)]


# Fixture prompt with {{table_columns}}, so each call's request names its own columns and the whole table.
V8 = ("## SYSTEM\nFixture system NO_MATCH.\n\n## USER\n\nTable: {{table}}\n\nAll columns: {{table_columns}}"
      "\nEND_NAMES\n\nColumns:\n{{columns_json}}\n")
V7 = "## SYSTEM\nFixture system NO_MATCH.\n\n## USER\n\nTable: {{table}}\n\nColumns:\n{{columns_json}}\n"
VERSION = "propose_v8"
TABLE = "cat.raw.widgets"


@pytest.fixture(autouse=True)
def prompt_dir(tmp_path, monkeypatch):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "propose_v8.md").write_text(V8, encoding="utf-8")
    (d / "propose_v7.md").write_text(V7, encoding="utf-8")
    monkeypatch.setattr(propose_mod, "PROMPTS_DIR", d)
    return d


def _requested(messages):
    return [c["column"] for c in json.loads(messages[1]["content"].split("Columns:\n", 1)[1])]


def _entry(col, term="term_alpha", sens="internal", conf=0.85):
    return {"column": col, "term_id": term, "description": f"{col} desc", "sensitivity": sens,
            "confidence": conf, "rationale": "because"}


def _answer(cols, extra=()):
    return json.dumps({"columns": [_entry(c) for c in cols] + list(extra)})


class ChunkClient:
    """Answers each call for exactly the columns it was asked about, unless `overrides[call_index]` says otherwise.

    Token counts differ per call so per-chunk token sums can be told apart.
    """

    model = "fake-model"

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.requests: list[dict] = []

    def chat(self, messages, temperature=0.0, max_tokens=4000):
        assert temperature == 0.0
        i = len(self.requests)
        cols = _requested(messages)
        self.requests.append({"messages": messages, "max_tokens": max_tokens, "columns": cols})
        o = self.overrides.get(i)
        text = o(cols) if callable(o) else (o if o is not None else _answer(cols))
        return LLMResponse(text, prompt_tokens=500 + 100 * i, completion_tokens=37 * len(cols) + i,
                           model=self.model)

    @staticmethod
    def total(i, n_cols):
        return 500 + 100 * i + 37 * n_cols + i


def _run(n, max_per_call=None, client=None, table_columns=None, version=VERSION):
    client = client or ChunkClient()
    inputs = _inputs(n)
    tc = table_columns if table_columns is not None else _names(n) + ["noise_x1", "col_fuzzy_ok"]
    kwargs = {} if max_per_call is None else {"max_columns_per_call": max_per_call}
    out = propose_mod.propose_llm(TABLE, inputs, client, version=version, table_columns=tc, **kwargs)
    return out, client, inputs, tc


# --- chunk_columns -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("n,max_per_call,sizes", [
    (1, 40, [1]), (40, 40, [40]), (41, 40, [21, 20]), (60, 40, [30, 30]), (81, 40, [27, 27, 27]), (7, 3, [3, 2, 2]),
])
def test_chunk_sizes(n, max_per_call, sizes):
    items = list(range(n))
    chunks = _chunk(items, max_per_call)
    assert [len(c) for c in chunks] == sizes
    assert [x for c in chunks for x in c] == items


def test_chunk_empty_is_empty():
    assert _chunk([], 40) == []


@pytest.mark.parametrize("bad", [0, -1, -40, 1.5, 40.0, "3", None, True, False])
def test_chunk_bad_max_raises(bad):
    with pytest.raises(ValueError):
        _chunk(list(range(5)), bad)


def test_chunk_bad_max_raises_even_for_empty_input():
    with pytest.raises(ValueError):
        _chunk([], 0)


@pytest.mark.parametrize("n", list(range(0, 131)))
@pytest.mark.parametrize("max_per_call", [1, 2, 3, 7, 40, 45])
def test_chunk_properties(n, max_per_call):
    items = [f"col_{i:03d}" for i in range(n)]
    chunks = _chunk(items, max_per_call)
    k = math.ceil(n / max_per_call)
    assert len(chunks) == k
    assert [x for c in chunks for x in c] == items  # order preserved, nothing lost or repeated
    if k:
        sizes = [len(c) for c in chunks]
        assert max(sizes) <= max_per_call and min(sizes) >= 1
        big, rem = n // k + 1, n % k
        assert sizes == [big] * rem + [n // k] * (k - rem)  # first n % k chunks one larger


def test_chunk_returns_lists_and_leaves_input_alone():
    items = _inputs(7)
    before = list(items)
    chunks = _chunk(items, 3)
    assert items == before
    assert all(isinstance(c, list) for c in chunks)
    assert chunks[0][0] is items[0]  # same objects, not copies


def test_chunk_is_idempotent():
    items = list(range(81))
    assert _chunk(items, 40) == _chunk(items, 40)


# --- propose_llm: one call per chunk ----------------------------------------------------------------------------

@pytest.mark.parametrize("n,max_per_call,sizes", [
    (41, 40, [21, 20]), (60, 40, [30, 30]), (81, 40, [27, 27, 27]), (7, 3, [3, 2, 2]),
])
def test_one_call_per_chunk(n, max_per_call, sizes):
    out, client, _, _ = _run(n, max_per_call)
    assert [len(r["columns"]) for r in client.requests] == sizes
    assert out.calls == len(sizes)
    assert out.tokens == sum(ChunkClient.total(i, s) for i, s in enumerate(sizes))


def test_default_max_is_40():
    out, client, _, _ = _run(41)
    assert [len(r["columns"]) for r in client.requests] == [21, 20] and out.calls == 2
    out, client, _, _ = _run(40)
    assert len(client.requests) == 1 and out.calls == 1


def test_each_call_gets_its_chunk_and_the_full_table_columns():
    out, client, inputs, tc = _run(60, 40)
    expected_chunks = _chunk(inputs, 40)
    assert len(client.requests) == len(expected_chunks)
    for req, chunk in zip(client.requests, expected_chunks):
        assert req["messages"] == build_messages(TABLE, chunk, VERSION, tc)
        assert req["max_tokens"] == max_tokens_for(len(chunk))
        names = json.loads(req["messages"][1]["content"].split("All columns: ", 1)[1].split("\nEND_NAMES", 1)[0])
        assert names == tc  # same full list for every chunk, not just the chunk's columns


@pytest.mark.parametrize("n,max_per_call", [(1, 40), (3, 40), (40, 40), (3, 3)])
def test_small_table_is_a_single_call_identical_to_today(n, max_per_call):
    out, client, inputs, tc = _run(n, max_per_call)
    assert len(client.requests) == 1 and out.calls == 1
    assert client.requests[0]["messages"] == build_messages(TABLE, inputs, VERSION, tc)
    assert client.requests[0]["max_tokens"] == max_tokens_for(n)
    assert out.tokens == ChunkClient.total(0, n)


def test_single_call_without_table_columns_on_a_prompt_without_placeholder():
    client = ChunkClient()
    inputs = _inputs(5)
    propose_mod.propose_llm(TABLE, inputs, client, version="propose_v7", max_columns_per_call=40)
    assert client.requests[0]["messages"] == build_messages(TABLE, inputs, "propose_v7")


def test_one_proposal_per_column_in_input_order():
    out, _, inputs, _ = _run(81, 40)
    assert [p.column for p in out.proposals] == [ci.column for ci in inputs]
    assert all(p.term_id == "term_alpha" and p.method == "llm" for p in out.proposals)
    assert all(p.model == "fake-model" and p.prompt_version == VERSION for p in out.proposals)
    assert not out.rejections


def test_tokens_split_per_chunk():
    out, client, _, _ = _run(81, 40)
    start = 0
    for i, req in enumerate(client.requests):
        size = len(req["columns"])
        chunk_props = out.proposals[start:start + size]
        assert [p.column for p in chunk_props] == req["columns"]
        assert sum(p.tokens for p in chunk_props) == ChunkClient.total(i, size)
        assert [p.tokens for p in chunk_props] == split_tokens(ChunkClient.total(i, size), size)
        start += size
    assert sum(p.tokens for p in out.proposals) == out.tokens


def test_floor_still_applied_across_chunks():
    inputs = _inputs(7)
    inputs[5] = ColumnInput("col_006", "col 006", _profile("col_006", ["email"]), _cands())
    client = ChunkClient()
    out = propose_mod.propose_llm(TABLE, inputs, client, version=VERSION, table_columns=_names(7),
                                  max_columns_per_call=3)
    by = {p.column: p for p in out.proposals}
    assert by["col_006"].sensitivity == "pii"
    assert by["col_001"].sensitivity == "internal"


# --- failure isolation --------------------------------------------------------------------------------------------

def test_garbage_for_one_chunk_rejects_only_that_chunk():
    client = ChunkClient(overrides={1: "Sure! Here you go: {"})
    out, client, inputs, _ = _run(7, 3, client=client)
    chunk1, chunk2, chunk3 = (r["columns"] for r in client.requests)
    assert (chunk1, chunk2, chunk3) == (_names(7)[:3], _names(7)[3:5], _names(7)[5:])
    by = {p.column: p for p in out.proposals}
    assert [p.column for p in out.proposals] == _names(7)
    for c in chunk1 + chunk3:
        assert by[c].term_id == "term_alpha" and by[c].confidence == 0.85
        assert not by[c].rationale.startswith("REJECTED")
    for c in chunk2:
        assert by[c].term_id is None and by[c].confidence == 0.0
        assert by[c].rationale.startswith("REJECTED")
    rejected = {r.column: r.reason for r in out.rejections}
    assert set(rejected) == set(chunk2)
    assert all("unparseable" in reason for reason in rejected.values())
    assert out.calls == 3  # a failed chunk is still a call


def test_garbage_chunk_still_counts_its_tokens():
    client = ChunkClient(overrides={1: "not json"})
    out, client, _, _ = _run(7, 3, client=client)
    sizes = [len(r["columns"]) for r in client.requests]
    assert out.tokens == sum(ChunkClient.total(i, s) for i, s in enumerate(sizes))
    assert sum(p.tokens for p in out.proposals[3:5]) == ChunkClient.total(1, 2)


def test_answer_for_another_chunks_column_is_rejected_and_not_duplicated():
    stray = _entry("col_004", term="term_beta", conf=0.99)  # col_004 belongs to chunk 2

    client = ChunkClient(overrides={0: lambda cols: _answer(cols, extra=[stray])})
    out, client, _, _ = _run(7, 3, client=client)
    assert "col_004" not in client.requests[0]["columns"]
    assert "col_004" in client.requests[1]["columns"]
    assert [p.column for p in out.proposals] == _names(7)  # exactly one each, no duplicate col_004
    by = {p.column: p for p in out.proposals}
    assert by["col_004"].term_id == "term_alpha" and by["col_004"].confidence == 0.85  # its own chunk's answer
    stray_rej = [r for r in out.rejections if r.column == "col_004"]
    assert len(stray_rej) == 1 and "unknown column" in stray_rej[0].reason
    assert all(by[c].term_id == "term_alpha" for c in _names(3))  # chunk 1's own answers unaffected


def test_answer_for_another_chunks_column_does_not_shift_token_split():
    stray = _entry("col_004", term="term_beta")
    client = ChunkClient(overrides={0: lambda cols: _answer(cols, extra=[stray])})
    out, client, _, _ = _run(7, 3, client=client)
    assert sum(p.tokens for p in out.proposals[:3]) == ChunkClient.total(0, 3)
    assert sum(p.tokens for p in out.proposals[3:5]) == ChunkClient.total(1, 2)


def test_invalid_term_in_one_chunk_is_rejected_per_column():
    client = ChunkClient(overrides={2: lambda cols: _answer(cols[:-1], extra=[_entry(cols[-1], term="made_up")])})
    out, _, _, _ = _run(7, 3, client=client)
    by = {p.column: p for p in out.proposals}
    assert by["col_007"].term_id is None and by["col_007"].rationale.startswith("REJECTED")
    assert by["col_006"].term_id == "term_alpha"
    assert [r.column for r in out.rejections] == ["col_007"]
    assert "not a candidate" in out.rejections[0].reason


# --- edges -----------------------------------------------------------------------------------------------------

def test_no_inputs_no_call():
    client = ChunkClient()
    out = propose_mod.propose_llm(TABLE, [], client, version=VERSION, table_columns=[], max_columns_per_call=40)
    assert client.requests == [] and out.calls == 0 and out.proposals == [] and out.tokens == 0


@pytest.mark.parametrize("bad", [0, -1])
def test_bad_max_raises_before_any_call(bad):
    client = ChunkClient()
    with pytest.raises(ValueError):
        _run(5, bad, client=client)
    assert client.requests == []


def test_chunked_run_is_idempotent():
    a, _, _, _ = _run(81, 40)
    b, _, _, _ = _run(81, 40)
    assert a == b


# --- review round 1 ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("n", [1, 15, 40, 48])
def test_job_setting_accepts_up_to_the_ceiling(n):
    assert propose_mod.check_columns_per_call(n) == n
    assert propose_mod.MAX_COLUMNS_CEILING == 48
    assert max_tokens_for(48) <= 16000 < 1500 + 300 * 49


@pytest.mark.parametrize("n", [0, -1, 49, 200])
def test_job_setting_refuses_zero_and_above_the_ceiling(n):
    with pytest.raises(ValueError):
        propose_mod.check_columns_per_call(n)


def test_stray_answer_reason_says_it_belongs_to_another_chunk():
    stray = _entry("col_004", term="term_beta", conf=0.99)
    client = ChunkClient(overrides={0: lambda cols: _answer(cols, extra=[stray])})
    out, _, _, _ = _run(7, 3, client=client)
    (r,) = [r for r in out.rejections if r.column == "col_004"]
    assert "another chunk" in r.reason and "ignored" in r.reason


def test_unknown_column_not_in_any_chunk_keeps_the_original_reason():
    stray = _entry("col_999", term="term_beta")
    client = ChunkClient(overrides={0: lambda cols: _answer(cols, extra=[stray])})
    out, _, _, _ = _run(7, 3, client=client)
    (r,) = [r for r in out.rejections if r.column == "col_999"]
    assert r.reason == "unknown column in response"
