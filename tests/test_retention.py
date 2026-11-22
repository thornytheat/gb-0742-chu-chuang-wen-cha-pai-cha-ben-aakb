import pytest

from app.manifest import json_sha
from app.modules.archive.pipeline import evaluate
from app.modules.intake.catalog import load_sample
from app.modules.retention.decision import LOCK, KEEP, DOWNGRADE, decide
from app.modules.retention.ledger import build_ledger, evidence_references, row_hash


def _run(client, sample):
    response = client.post("/api/runs", json={"sample": sample})
    assert response.status_code == 201
    return response.json()["manifest"]["id"]


def test_loan_cabinet_and_sustained_episode_lock_shift(client):
    run_id = _run(client, "shift")
    conclusion = client.get(f"/api/retention/runs/{run_id}/conclusion").json()
    assert conclusion["verdict"] == LOCK
    codes = [item["code"] for item in conclusion["reasons"]]
    assert "LOAN_CABINET" in codes and "SUSTAINED_EPISODE" in codes
    assert conclusion["released_bytes"] == 0
    assert conclusion["retained_row_count"] == conclusion["total_row_count"]


def test_transient_episode_keeps_full_original_with_referenced_rows(client):
    run_id = _run(client, "transient")
    conclusion = client.get(f"/api/retention/runs/{run_id}/conclusion").json()
    assert conclusion["verdict"] == KEEP
    assert [item["code"] for item in conclusion["reasons"]][0] == "TRANSIENT_EPISODE"
    assert conclusion["released_bytes"] == 0
    # 瞬时段两行原读数被引用
    assert set(conclusion["referenced"]) == {"transient-points.csv:4", "transient-points.csv:5"}


def test_clean_shift_downgrades_and_releases_space(client):
    run_id = _run(client, "clean")
    conclusion = client.get(f"/api/retention/runs/{run_id}/conclusion").json()
    assert conclusion["verdict"] == DOWNGRADE
    assert conclusion["reasons"][0]["code"] == "NO_OPEN_EVIDENCE"
    assert conclusion["released_bytes"] > 0
    assert conclusion["retained_row_count"] == 0
    assert conclusion["original_file_sha256"]


def test_carry_over_writes_one_batch_with_per_shift_basis_and_totals(client):
    shift_id = _run(client, "shift")
    transient_id = _run(client, "transient")
    clean_id = _run(client, "clean")
    response = client.post("/api/retention/carry-over", json={"season": "2026-close"})
    assert response.status_code == 201
    batch = response.json()["batch"]
    assert batch["verdict_counts"] == {"LOCK": 1, "KEEP": 1, "DOWNGRADE": 1}
    by_run = {item["run_id"]: item for item in batch["conclusions"]}
    assert by_run[shift_id]["verdict"] == LOCK
    assert by_run[transient_id]["verdict"] == KEEP
    assert by_run[clean_id]["verdict"] == DOWNGRADE
    # 释放量只来自降级班次
    assert batch["totals"]["released_bytes"] == by_run[clean_id]["released_bytes"]
    assert (batch["totals"]["stored_bytes_before"] - batch["totals"]["stored_bytes_after"]
            == batch["totals"]["released_bytes"])
    assert batch["batch_sha256"]


def test_rerun_same_carry_over_returns_same_batch_and_creates_nothing_new(client):
    _run(client, "clean")
    first = client.post("/api/retention/carry-over", json={"season": "2026-close"})
    second = client.post("/api/retention/carry-over", json={"season": "2026-close"})
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["batch"]["id"] == first.json()["batch"]["id"]
    assert len(client.get("/api/retention/batches").json()) == 1


def test_failed_carry_over_keeps_old_versions_and_allows_retry(client):
    run_id = _run(client, "clean")
    store = client.app.state.archives.store
    with store.connect() as db:
        before = db.execute("SELECT input FROM shift_archives WHERE id=?", (run_id,)).fetchone()[0]
        db.execute("CREATE TRIGGER boom BEFORE INSERT ON retention_batches "
                   "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    failed = client.post("/api/retention/carry-over", json={"season": "2026-close"})
    assert failed.status_code == 503
    with store.connect() as db:
        db.execute("DROP TRIGGER boom")
        after = db.execute("SELECT input FROM shift_archives WHERE id=?", (run_id,)).fetchone()[0]
    assert before == after
    assert store.list_batches() == []
    retry = client.post("/api/retention/carry-over", json={"season": "2026-close"})
    assert retry.status_code == 201


def test_downgraded_archive_keeps_verifiable_summary_and_original_digest(client):
    run_id = _run(client, "clean")
    client.post("/api/retention/carry-over", json={"season": "2026-close"})
    manifest, evidence, source = client.app.state.archives.store.fetch(run_id)
    assert source["mode"] == "summary" and "csv_base64" not in source
    assert source["original_file_sha256"] == manifest["hashes"]["file_sha256"]
    # 复算与导出走摘要核验，不报错
    assert client.post(f"/api/runs/{run_id}/rerun").json()["mode"] == "summary"
    export = client.get(f"/api/runs/{run_id}/export")
    assert export.status_code == 200 and export.json()["input"]["mode"] == "summary"


def test_tampered_summary_row_is_detected_by_replay(client):
    run_id = _run(client, "clean")
    client.post("/api/retention/carry-over", json={"season": "2026-close"})
    store = client.app.state.archives.store
    with store.connect() as db:
        db.execute("UPDATE shift_archives SET evidence='{}' WHERE id=?", (run_id,))
    reply = client.post(f"/api/runs/{run_id}/rerun")
    assert reply.status_code == 409 and reply.json()["code"] == "ARCHIVE_DAMAGED"


def test_second_season_keeps_already_downgraded_shift_without_new_release(client):
    run_id = _run(client, "clean")
    first = client.post("/api/retention/carry-over", json={"season": "2026-close"}).json()["batch"]
    released = first["totals"]["released_bytes"]
    second = client.post("/api/retention/carry-over", json={"season": "2027-close"}).json()["batch"]
    assert second["id"] != first["id"]
    assert second["totals"]["released_bytes"] == 0
    conclusion = next(item for item in second["conclusions"] if item["run_id"] == run_id)
    assert conclusion["mode"] == "summary" and conclusion["verdict"] == DOWNGRADE
    assert conclusion["reasons"][0]["code"] == "ALREADY_DOWNGRADED"
    assert conclusion["original_bytes"] == released + conclusion["summary_bytes"]


def test_episode_ticket_and_ledger_share_the_same_row_identifiers():
    package = load_sample("shift")
    evidence = evaluate(package)
    ledger = build_ledger(evidence)
    references = evidence_references(evidence)
    ledger_ids = {item["row_id"] for item in ledger["rows"]}
    episode_rows = {slot[level] for episode in evidence["episodes"] for slot in episode["evidence"]
                    for level in ("upper", "lower") if slot[level]}
    ticket_rows = {row_id for ticket in evidence["audit"]["tickets"] for row_id in ticket["evidence"]}
    assert episode_rows <= set(references) <= ledger_ids
    assert ticket_rows <= set(references)
    # 台账行摘要可独立重算
    rows_by_id = {row["id"]: row for row in evidence["rows"]}
    for entry in ledger["rows"]:
        row = rows_by_id[entry["row_id"]]
        rebuilt = {"id": row["id"], "line": row["line"], "end_line": row["end_line"],
                   "raw": row["raw"], "disposition": row["disposition"], "code": row["code"]}
        assert row_hash(rebuilt) == entry["row_sha256"]
    assert json_sha(ledger["rows"]) == ledger["ledger_sha256"]


def test_conclusion_hash_is_deterministic(client):
    run_id = _run(client, "shift")
    first = client.get(f"/api/retention/runs/{run_id}/conclusion").json()
    second = client.get(f"/api/retention/runs/{run_id}/conclusion").json()
    assert first == second
    archive = client.app.state.archives.store.fetch(run_id)
    conclusion, _, _ = decide(archive)
    assert conclusion["conclusion_sha256"] == first["conclusion_sha256"]


def test_unknown_batch_and_bad_season_are_rejected(client):
    assert client.get("/api/retention/batches/missing").status_code == 404
    bad = client.post("/api/retention/carry-over", json={"season": "bad season!"})
    assert bad.status_code == 422
