import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from app.modules.m8_release.service import release_document
from app.reasons import FabricError

SUBSTITUTE = {"id": "T-901", "fabric": "斜纹棉", "width": 1400, "net": 520, "seam": 5, "quantity": 2}


def archive_run(client, sample="dirty", kerf=5):
    return client.post("/api/runs", json={"sample": sample, "kerf_mm": kerf}).json()["manifest"]["id"]


def release(client, run_id, cancel="C002", substitute=SUBSTITUTE):
    return client.post(f"/api/runs/{run_id}/releases", json={"cancel_order": cancel, "substitute": substitute})


def test_release_transfers_matching_pieces_and_quarantines_the_rest(client):
    run_id = archive_run(client)
    before = client.get(f"/api/export/{run_id}").content
    created = release(client, run_id)
    assert created.status_code == 201 and created.json()["reused"] is False
    manifest = created.json()["manifest"]
    assert (manifest["transferred"], manifest["quarantined"], manifest["cut_new"], manifest["short"]) == (2, 1, 0, 0)
    assert manifest["released_rolls"] == ["R-001", "R-002"] and manifest["substitute"]["length"] == 530

    document = client.get(f"/api/releases/{manifest['id']}").json()
    placements = client.get(f"/api/runs/{run_id}").json()["snapshot"]["changeovers"]["placements"]
    original = {item["id"]: item for item in placements if item["order"] == "C002"}
    assert len(document["pieces"]) == len(original) == 3
    for item in document["pieces"]:  # 每片都能追回原卷原段
        source = original[item["piece"]]
        assert (item["roll"], item["start"], item["end"], item["kerf_end"]) == (
            source["roll"], source["start"], source["end"], source["kerf_end"])
    dispositions = {item["piece"]: item for item in document["pieces"]}
    assert dispositions["C002/01"]["disposition"] == "transferred" and dispositions["C002/01"]["as"] == "T-901/01"
    assert dispositions["C002/02"]["as"] == "T-901/02"
    assert dispositions["C002/03"]["disposition"] == "quarantined" and dispositions["C002/03"]["as"] is None

    taken = document["substitute_pieces"]
    assert [item["id"] for item in taken] == ["T-901/01", "T-901/02"]
    assert all(item["origin"] == "transferred" for item in taken)
    assert [(item["roll"], item["start"], item["end"]) for item in taken] == [("R-001", 0, 530), ("R-001", 1100, 1630)]

    returned = {entry["roll"]: entry["segments"] for entry in document["returned"]}
    assert [(s["start"], s["end"]) for s in returned["R-001"]] == [(860, 990), (1960, 2189)]
    assert [(s["start"], s["end"]) for s in returned["R-002"]] == [(1796, 1800)]
    assert all(s["consumed_by"] == [] for segments in returned.values() for s in segments)

    books = {book["roll"]: book for book in client.get(f"/api/runs/{run_id}").json()["snapshot"]["ledger"]["rolls"]}
    for entry in document["reconciliation"]:
        assert entry["delta"] == 0 and sum(entry["after"].values()) == entry["length"] == books[entry["roll"]]["length"]
        assert entry["before"] == books[entry["roll"]]["lengths"]
    after = {entry["roll"]: entry["after"] for entry in document["reconciliation"]}
    assert after["R-001"]["quarantined"] == 0 and after["R-002"]["quarantined"] == 535  # 隔离片连刀口 530+5
    assert after["R-001"]["piece"] == books["R-001"]["lengths"]["piece"]  # 转用片仍是合格裁片
    assert document["totals"]["delta"] == 0
    assert sum(document["totals"]["after"].values()) == sum(document["totals"]["before"].values())
    assert client.get(f"/api/export/{run_id}").content == before  # 原卷宗不动


def test_same_release_rerun_is_reused_without_second_release(client, settings):
    run_id = archive_run(client)
    first = release(client, run_id)
    again = release(client, run_id)
    assert again.status_code == 200 and again.json() == {"manifest": first.json()["manifest"], "reused": True}
    assert [item["id"] for item in client.get(f"/api/runs/{run_id}/releases").json()["releases"]] == [first.json()["manifest"]["id"]]
    with closing(sqlite3.connect(settings.database)) as db:
        actions = [row[0] for row in db.execute("SELECT action FROM reviews WHERE run=? ORDER BY seq", (run_id,))]
        assert actions == ["committed", "released"]
        assert db.execute("SELECT COUNT(*) FROM releases").fetchone()[0] == 1


def test_concurrent_identical_releases_store_only_one(client):
    run_id = archive_run(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: release(client, run_id), range(4)))
    assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
    assert len({response.json()["manifest"]["id"] for response in responses}) == 1
    assert len(client.get(f"/api/runs/{run_id}/releases").json()["releases"]) == 1


def test_second_different_cancellation_on_same_run_is_rejected(client):
    run_id = archive_run(client)
    assert release(client, run_id).status_code == 201
    other = release(client, run_id, cancel="C001", substitute={**SUBSTITUTE, "id": "T-902", "net": 600})
    assert (other.status_code, other.json()["error"]["code"]) == (409, "RELEASE_EXISTS")
    changed = release(client, run_id, substitute={**SUBSTITUTE, "quantity": 1})
    assert (changed.status_code, changed.json()["error"]["code"]) == (409, "RELEASE_EXISTS")
    assert len(client.get(f"/api/runs/{run_id}/releases").json()["releases"]) == 1


def test_substitute_shortfall_is_cut_from_returned_segments(client):
    run_id = archive_run(client)
    substitute = {"id": "T-101", "fabric": "斜纹棉", "width": 1400, "net": 100, "seam": 5, "quantity": 3}
    created = release(client, run_id, substitute=substitute)
    assert created.status_code == 201
    manifest = created.json()["manifest"]
    assert (manifest["transferred"], manifest["quarantined"], manifest["cut_new"], manifest["short"]) == (0, 3, 2, 1)

    document = client.get(f"/api/releases/{manifest['id']}").json()
    taken = document["substitute_pieces"]
    assert [(item["id"], item["origin"], item["roll"], item["start"], item["end"], item["kerf_end"]) for item in taken] == [
        ("T-101/01", "cut", "R-001", 860, 970, 975), ("T-101/02", "cut", "R-001", 1960, 2070, 2075)]
    returned = {entry["roll"]: entry["segments"] for entry in document["returned"]}
    assert returned["R-001"][0]["consumed_by"] == ["T-101/01"] and returned["R-001"][0]["remaining"] == {"start": 975, "end": 990}
    assert returned["R-001"][1]["remaining"] == {"start": 2075, "end": 2189}
    assert returned["R-002"][0]["remaining"] == {"start": 1796, "end": 1800}

    after = {entry["roll"]: entry["after"] for entry in document["reconciliation"]}
    assert after["R-001"]["unused"] == 359 - 2 * (110 + 5)  # 回退段被新裁片和刀口占用
    assert after["R-001"]["piece"] == 1700 - 2 * 530 + 2 * 110
    assert after["R-001"]["quarantined"] == 2 * (530 + 5)
    assert all(entry["delta"] == 0 for entry in document["reconciliation"])
    assert document["totals"]["delta"] == 0


def test_failed_release_keeps_archive_slips_and_stores_nothing(client, settings):
    run_id = archive_run(client)
    export_before = client.get(f"/api/export/{run_id}").content
    slips_before = client.get(f"/api/slips?run_id={run_id}").json()

    cases = [
        (f"/api/runs/{run_id}/releases", {"cancel_order": "C999", "substitute": SUBSTITUTE}, 422, "CANCEL_UNKNOWN"),
        (f"/api/runs/{run_id}/releases", {"cancel_order": "C002", "substitute": {**SUBSTITUTE, "id": "C001"}}, 422, "SUBSTITUTE_CLASH"),
        (f"/api/runs/{run_id}/releases", {"cancel_order": "C002", "substitute": {**SUBSTITUTE, "fabric": "帆布"}}, 422, "SUBSTITUTE_INCOMPATIBLE"),
        (f"/api/runs/{run_id}/releases", {"cancel_order": "C002", "substitute": {**SUBSTITUTE, "width": 4000}}, 422, "SUBSTITUTE_INCOMPATIBLE"),
        (f"/api/runs/{run_id}/releases", {"cancel_order": "C002", "substitute": {**SUBSTITUTE, "quantity": 13}}, 422, "PLAN_LIMIT"),
        ("/api/runs/missing/releases", {"cancel_order": "C002", "substitute": SUBSTITUTE}, 404, "RUN_UNKNOWN"),
    ]
    for path, body, status, code in cases:
        response = client.post(path, json=body)
        assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    for bad in (0, -1, 2.5, "2", True, None):
        response = release(client, run_id, substitute={**SUBSTITUTE, "quantity": bad})
        assert (response.status_code, response.json()["error"]["code"]) == (422, "FORM_INVALID")
    for bad in ("", "  ", None):
        response = release(client, run_id, substitute={**SUBSTITUTE, "id": bad})
        assert (response.status_code, response.json()["error"]["code"]) == (422, "FORM_INVALID")

    assert client.get(f"/api/runs/{run_id}/releases").json()["releases"] == []
    assert client.get(f"/api/export/{run_id}").content == export_before
    assert client.get(f"/api/slips?run_id={run_id}").json() == slips_before
    with closing(sqlite3.connect(settings.database)) as db:
        assert db.execute("SELECT COUNT(*) FROM releases").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM reviews WHERE action='released'").fetchone()[0] == 0


def test_release_endpoints_require_existing_documents(client):
    assert client.get("/api/runs/missing/releases").status_code == 404
    missing = client.get("/api/releases/missing")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "RELEASE_UNKNOWN")


def test_cancel_order_without_pieces_is_rejected(dirty):
    snapshot = copy.deepcopy(dirty)
    snapshot["changeovers"]["placements"] = [p for p in snapshot["changeovers"]["placements"] if p["order"] != "C004"]
    with pytest.raises(FabricError) as caught:
        release_document("run", snapshot, "C004", SUBSTITUTE, "release", "now")
    assert caught.value.code == "CANCEL_NO_PIECES"


def test_home_offers_release_workbench(client):
    home = client.get("/")
    assert home.status_code == 200 and "撤单释放" in home.text and "生成释放单" in home.text
    run_id = archive_run(client)
    document = json.loads(client.get(f"/api/export/{run_id}").content)
    assert "releases" not in document  # 原卷宗结构不受释放单影响
