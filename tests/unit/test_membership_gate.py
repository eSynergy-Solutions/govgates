"""Option B (lead, 9 Nov): membership is checked by a job that runs as a person who can read the groups, and
recorded as a `membership_check` audit row; the adapter (run as the CI SP, which can't read groups) refuses to write
unless the newest result passed, is for the same model file, is fresh, and was not written by the adapter's own
principal. Ages, days and future-dated rows come from SQL against the database clock (review round 2)."""

from __future__ import annotations

from datetime import timedelta

from gates.consumption import access_model as am

SHA = am.model_sha("version: 1\n")
CI = "ci-sp"
H = timedelta(hours=1)


def result(status="pass", sha=SHA, failures=()):
    return {"status": status, "model_sha": sha, "failures": list(failures), "warnings": []}


def gate(latest=None, age=H, *, actor="lead", future=0, sha=SHA):
    return am.membership_gate(latest, age, sha, actor=actor, self_actor=CI, future_rows=future)


def test_model_sha_is_stable_and_content_sensitive():
    assert am.model_sha("a") == am.model_sha("a")
    assert am.model_sha("a") != am.model_sha("b")


def test_membership_result_pass_and_fail():
    assert am.membership_result((), (), SHA)["status"] == "pass"
    r = am.membership_result(("w",), ("f",), SHA)
    assert (r["status"], r["failures"], r["warnings"], r["model_sha"]) == ("fail", ["f"], ["w"], SHA)


# ---------------------------------------------------------------- gate

def test_gate_passes_on_fresh_pass_for_this_model_by_a_person():
    assert gate(result(), 25 * H) == []


def test_gate_fails_closed_with_no_check():
    assert any("no membership check" in f for f in gate(None, None))


def test_gate_fails_on_failed_check_and_names_its_failures():
    assert any("isolated member" in f for f in gate(result("fail", failures=["isolated member 'p' in 'g'"])))


def test_gate_fails_on_stale_check():
    assert any("old" in f for f in gate(result(), 27 * H))


def test_gate_window_covers_a_daily_run_and_a_clock_change():
    assert gate(result(), 25 * H + timedelta(minutes=30)) == []


def test_gate_fails_when_check_was_for_another_model_file():
    assert any("different model" in f for f in gate(result(sha="other")))


def test_gate_fails_on_unreadable_result():
    assert gate({"status": "weird"}) != []


def test_gate_fails_on_any_future_dated_row_even_if_newest_passes():
    out = gate(result(), H, future=1)
    assert out and "future" in out[0]


def test_gate_fails_on_negative_age():
    assert any("old" in f for f in gate(result(), -H))


def test_gate_refuses_a_check_written_by_the_adapters_own_principal():
    assert any("own principal" in f for f in gate(result(), actor="CI-SP"))
    assert any("own principal" in f for f in gate(result(), actor=None))


# ---------------------------------------------------------------- writes: A, B, A on one day (dev finding)

def test_same_result_same_day_as_newest_writes_nothing():
    r = result()
    first = am.membership_write(r, "2026-11-09", None)
    assert first is not None
    assert am.membership_write(r, "2026-11-09", (first, r, "2026-11-09")) is None


def test_returning_to_an_earlier_result_the_same_day_writes_a_new_row():
    a, b = result(sha="a"), result(sha="b")
    ea = am.membership_write(a, "2026-11-09", None)
    eb = am.membership_write(b, "2026-11-09", (ea, a, "2026-11-09"))
    ea2 = am.membership_write(a, "2026-11-09", (eb, b, "2026-11-09"))
    assert None not in (ea, eb, ea2)
    assert len({ea, eb, ea2}) == 3


def test_same_result_next_day_writes_a_new_row():
    r = result()
    first = am.membership_write(r, "2026-11-09", None)
    assert am.membership_write(r, "2026-11-10", (first, r, "2026-11-09")) not in (None, first)
