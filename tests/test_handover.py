"""交接收表：暂存、逐级换算、缺上游/未覆盖诊断、改交、循环、幂等、并发、重启。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# 原录音表 O1：R1[1000,5000)->成片[0,4000)；R2[0,3000)->成片[4000,7000)
CSV_ORIGINAL = """recording_id,src_start_ms,src_end_ms,timeline_start_ms
R1,1000,5000,0
R2,0,3000,4000
"""


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "handover.db"))
    with TestClient(app) as c:
        yield c


def post_ho(client, no, name, kind, csv_text, upstream=None):
    return client.post("/api/handovers", json={
        "handover_no": no, "name": name, "kind": kind,
        "upstream_no": upstream, "csv_text": csv_text,
    })


def detail(client, no):
    return client.get(f"/api/handovers/{no}").json()


def versions(client):
    return client.get("/api/versions").json()


# ---------------------------------------------------------------- 原录音表

def test_original_handover_admitted_with_original_csv(client):
    r = post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    assert r.status_code == 201
    h = r.json()
    assert h["status"] == "admitted" and h["trace_complete"]
    vs = versions(client)
    assert len(vs) == 1
    segs = client.get(f"/api/versions/{vs[0]['id']}").json()["segments"]
    assert [(s["recording_id"], s["tl_start_ms"], s["tl_end_ms"]) for s in segs] == [
        ("R1", 0, 4000), ("R2", 4000, 7000)]


def test_original_rejects_upstream_and_bad_columns(client):
    r = post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL, upstream="H-000")
    assert r.status_code == 422
    r = post_ho(client, "H-001", "一版", "original", "1000,2000,0\n")
    assert r.status_code == 422


# ---------------------------------------------------------------- 二剪 + 暂存

RECUT = """upstream_start_ms,upstream_end_ms,timeline_start_ms
0,2000,0
3000,7000,3000
"""


def test_recut_waits_for_missing_upstream_then_traces(client):
    # 二剪先到馆：暂存，不产生版本
    r = post_ho(client, "H-002", "二版", "recut", RECUT, upstream="H-001")
    assert r.status_code == 201
    h = r.json()
    assert h["status"] == "pending"
    assert h["missing_upstream_nos"] == ["H-001"]
    assert all(row["status"] == "missing_upstream" for row in h["rows"])
    assert versions(client) == []

    # 补交上游后自动换算：跨剪接点拆段
    r = post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    assert r.json()["status"] == "admitted"
    h = detail(client, "H-002")
    assert h["status"] == "admitted" and h["trace_complete"]
    got = [(p["recording_id"], p["src_start_ms"], p["src_end_ms"],
            p["tl_start_ms"], p["tl_end_ms"]) for p in h["resolved_segments"]]
    # 上游 [0,2000) 全在 R1；上游 [3000,7000) 跨剪接点：R1 尾 [3000,4000)->R2 [4000,7000)
    assert got == [
        ("R1", 1000, 3000, 0, 2000),
        ("R1", 4000, 5000, 3000, 4000),
        ("R2", 0, 3000, 4000, 7000),
    ]
    assert len(versions(client)) == 2


def test_uncovered_rows_flagged_and_block_only_dependent_version(client):
    post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    # 上游只到 7000：第二行 [5000,9000) 有 [7000,9000) 无法追溯
    bad = "upstream_start_ms,upstream_end_ms,timeline_start_ms\n0,2000,0\n5000,9000,2000\n"
    r = post_ho(client, "H-002", "二版", "recut", bad, upstream="H-001")
    h = r.json()
    assert h["status"] == "pending"
    assert h["missing_upstream_nos"] == []
    statuses = [row["status"] for row in h["rows"]]
    assert statuses == ["ok", "uncovered"]
    assert h["rows"][1]["missing_ranges"] == [[7000, 9000]]
    assert len(versions(client)) == 1

    # 改交为完整覆盖后入库
    fixed = "upstream_start_ms,upstream_end_ms,timeline_start_ms\n0,2000,0\n5000,7000,2000\n"
    r = client.put("/api/handovers/H-002", json={
        "handover_no": "H-002", "name": "二版", "kind": "recut",
        "upstream_no": "H-001", "csv_text": fixed,
    })
    assert r.status_code == 200 and r.json()["status"] == "admitted"
    assert len(versions(client)) == 2


def test_independent_branch_admitted_while_other_waits(client):
    post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    waiting = post_ho(client, "H-009", "缺上游", "recut", RECUT, upstream="H-XYZ")
    ok = post_ho(client, "H-002", "二版", "recut", RECUT, upstream="H-001")
    assert waiting.json()["status"] == "pending"
    assert ok.json()["status"] == "admitted"
    assert len(versions(client)) == 2


def test_multi_level_chain_and_cut_split(client):
    post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    # 二剪：上游 [0,7000) 全表 -> 本版 [0,7000)
    post_ho(client, "H-002", "二版", "recut", "0,7000,0\n", upstream="H-001")
    # 三剪：从二剪 [2500,5500) 取 [0,3000)
    r = post_ho(client, "H-003", "三版", "recut", "2500,5500,0\n", upstream="H-002")
    h = r.json()
    assert h["status"] == "admitted"
    got = [(p["recording_id"], p["src_start_ms"], p["src_end_ms"],
            p["tl_start_ms"], p["tl_end_ms"]) for p in h["resolved_segments"]]
    # 二剪成片 2500 落在 R1 源 3500；5500 落在 R2 源 1500（二剪 4000..7000 对应 R2 0..3000）
    assert got == [
        ("R1", 3500, 5000, 0, 1500),
        ("R2", 0, 1500, 1500, 3000),
    ]


def test_middle_of_chain_missing_keeps_descendants_waiting(client):
    post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    top = post_ho(client, "H-003", "三版", "recut", "0,1000,0\n", upstream="H-002")
    assert top.json()["status"] == "pending"
    assert top.json()["missing_upstream_nos"] == ["H-002"]
    post_ho(client, "H-002", "二版", "recut", "0,1000,0\n", upstream="H-001")
    # H-002 入库后自动放行 H-003
    assert detail(client, "H-002")["status"] == "admitted"
    assert detail(client, "H-003")["status"] == "admitted"
    assert len(versions(client)) == 3


# ---------------------------------------------------------------- 改交 / 循环

def test_cycle_rejected_and_original_kept(client):
    post_ho(client, "A", "A版", "original", CSV_ORIGINAL)
    post_ho(client, "B", "B版", "recut", "0,1000,0\n", upstream="NOPE")
    # C -> B 暂存
    post_ho(client, "C", "C版", "recut", "0,1000,0\n", upstream="B")
    before = detail(client, "B")
    assert before["status"] == "pending"
    # 把 B 的上游改成 C 会形成 B->C->B 循环：拒收，B 原表保留
    r = client.put("/api/handovers/B", json={
        "handover_no": "B", "name": "B版", "kind": "recut",
        "upstream_no": "C", "csv_text": "0,1000,0\n",
    })
    assert r.status_code == 409
    assert "循环" in r.json()["detail"]
    after = detail(client, "B")
    assert after["upstream_no"] == "NOPE"
    assert after["status"] == "pending"
    assert after["csv_text"] == before["csv_text"]

    # 新建时自引用也拒收
    r = post_ho(client, "D", "D版", "recut", "0,1000,0\n", upstream="D")
    assert r.status_code == 409


def test_admitted_handover_cannot_be_resubmitted(client):
    post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    r = client.put("/api/handovers/H-001", json={
        "handover_no": "H-001", "name": "改", "kind": "original", "csv_text": CSV_ORIGINAL,
    })
    assert r.status_code == 409
    r = post_ho(client, "H-001", "改", "original", CSV_ORIGINAL)
    assert r.status_code == 409
    # 路径与提交内容不一致时不误改其他表
    r = client.put("/api/handovers/H-001", json={
        "handover_no": "H-OTHER", "name": "改", "kind": "original", "csv_text": CSV_ORIGINAL,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------- 幂等 / 并发

def test_identical_resubmit_is_idempotent(client):
    r1 = post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    r2 = post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.json()["version_id"] == r1.json()["version_id"]
    assert len(versions(client)) == 1


def test_concurrent_same_handover_admitted_once(tmp_path):
    app = create_app(str(tmp_path / "race.db"))

    def submit(i):
        with TestClient(app) as c:
            return post_ho(c, "H-001", "一版", "original", CSV_ORIGINAL)

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = [f.result() for f in [ex.submit(submit, i) for i in range(6)]]
    assert all(r.status_code in (200, 201) for r in results)
    with TestClient(app) as c:
        assert len(versions(c)) == 1


def test_concurrent_upstream_and_dependent_never_double_admit(tmp_path):
    app = create_app(str(tmp_path / "race2.db"))

    def submit(no, csv_text, upstream):
        with TestClient(app) as c:
            return post_ho(c, no, no, "recut" if upstream else "original",
                           csv_text, upstream)

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(submit, "H-002", RECUT, "H-001")
        f2 = ex.submit(submit, "H-001", CSV_ORIGINAL, None)
        f1.result(); f2.result()
    with TestClient(app) as c:
        assert {v["name"] for v in versions(c)} == {"H-001", "H-002"}
        assert detail(c, "H-002")["status"] == "admitted"


# ---------------------------------------------------------------- 重启 / 与既有功能

def test_pending_survives_restart_and_resumes(tmp_path):
    dbp = str(tmp_path / "restart.db")
    with TestClient(create_app(dbp)) as c:
        post_ho(c, "H-002", "二版", "recut", RECUT, upstream="H-001")
        assert versions(c) == []
    # 重启后仍是暂存待办；补交上游自动入库
    with TestClient(create_app(dbp)) as c:
        assert detail(c, "H-002")["status"] == "pending"
        post_ho(c, "H-001", "一版", "original", CSV_ORIGINAL)
        assert detail(c, "H-002")["status"] == "admitted"


def test_startup_pass_admits_ready_pending_handover(tmp_path):
    """模拟上次进程在暂存后中断：启动时自动接着处理待办。"""
    import sqlite3

    dbp = str(tmp_path / "startup.db")
    app = create_app(dbp)
    with TestClient(app) as c:
        post_ho(c, "H-001", "一版", "original", CSV_ORIGINAL)
        post_ho(c, "H-002", "二版", "recut", RECUT, upstream="H-001")
        assert len(versions(c)) == 2
        # 人为把 H-002 退回暂存并删掉其版本，模拟中断留下的待办
        conn = sqlite3.connect(dbp)
        assert conn.execute(
            "SELECT version_id FROM handovers WHERE handover_no = 'H-002'"
        ).fetchone()[0]
        # 直接构造：先删 H-002 的版本，再置回 pending（外键手工关闭）
        conn.execute("PRAGMA foreign_keys = OFF")
        vid = conn.execute(
            "SELECT version_id FROM handovers WHERE handover_no = 'H-002'"
        ).fetchone()[0]
        conn.execute("DELETE FROM segments WHERE version_id = ?", (vid,))
        conn.execute("DELETE FROM versions WHERE id = ?", (vid,))
        conn.execute(
            "UPDATE handovers SET status = 'pending', version_id = NULL"
            " WHERE handover_no = 'H-002'"
        )
        conn.commit()
        conn.close()

    # 重启：启动时放行，原编号只入库一次
    with TestClient(create_app(dbp)) as c:
        h = detail(c, "H-002")
        assert h["status"] == "admitted"
        assert h["version_id"] is not None
        assert len(versions(c)) == 2
        assert [v["handover_no"] for v in versions(c)].count("H-002") == 1


def test_concurrent_same_recut_admitted_once(tmp_path):
    app = create_app(str(tmp_path / "race3.db"))
    with TestClient(app) as c:
        post_ho(c, "H-001", "一版", "original", CSV_ORIGINAL)

    def submit(_):
        with TestClient(app) as c:
            return post_ho(c, "H-002", "二版", "recut", RECUT, upstream="H-001")

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = [f.result() for f in [ex.submit(submit, i) for i in range(6)]]
    assert all(r.status_code in (200, 201) for r in results)
    with TestClient(app) as c:
        assert [v["handover_no"] for v in versions(c)].count("H-002") == 1


def test_admitted_version_feeds_marker_and_muting(client):
    post_ho(client, "H-001", "一版", "original", CSV_ORIGINAL)
    post_ho(client, "H-002", "二版", "recut", RECUT, upstream="H-001")
    vid = detail(client, "H-002")["version_id"]
    # 版本列表与明细可核对交接编号
    listed = {v["name"]: v["handover_no"] for v in versions(client)}
    assert listed == {"一版": "H-001", "二版": "H-002"}
    assert client.get(f"/api/versions/{vid}").json()["handover_no"] == "H-002"
    m = client.post("/api/markers", json={
        "version_id": vid, "start_ms": 0, "end_ms": 7000,
    }).json()
    got = {(r["recording_id"], r["src_start_ms"], r["src_end_ms"])
           for r in m["source_ranges"]}
    assert got == {("R1", 1000, 3000), ("R1", 4000, 5000), ("R2", 0, 3000)}
    muted = client.get(f"/api/versions/{vid}/muted").json()["muted"]
    assert muted == [
        {"start_ms": 0, "end_ms": 2000, "marker_ids": [m["id"]]},
        {"start_ms": 3000, "end_ms": 7000, "marker_ids": [m["id"]]},
    ]
