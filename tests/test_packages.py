"""交付包需求覆盖测试:

1. 建包:项目齐全时冻结结果与修订号,凭包号下载 CSV
2. 项目缺失/已撤回时列明原因,不建包;参数校验(1~20、不重复)
3. 增量改变包内项目的结果/修订号/有效性 -> 包永久停用,下载拒绝
4. 被忽略的旧增量、未选项目、其他报告的变更不影响包
5. 全量切换改变包内项目 -> 停用;未改变 -> 不影响
6. 结果变更与包停用同事务:中断重启后一致;恢复原值不复活旧包
7. 建包与结果变更并发:不留"已过时却可用"的包
"""
import io
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app          # noqa: E402
from app import db, loader, logic   # noqa: E402


@pytest.fixture()
def app(tmp_path):
    return create_app({
        "TESTING": True,
        "DATA_DIR": str(tmp_path / "data"),
        "SAMPLES_DIR": os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "samples"),
        "CHUNK_SIZE": 3,
        "CHUNK_DELAY": 0.0,
    })


@pytest.fixture()
def client(app):
    return app.test_client()


def csv_upload(client, rows, kind="full"):
    header = (["report_no", "item", "result", "revision"] if kind == "full"
              else ["report_no", "item", "action", "result", "revision"])
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for r in rows:
        buf.write(",".join(str(c) for c in r) + "\n")
    data = {"file": (io.BytesIO(buf.getvalue().encode("utf-8")), "up.csv")}
    return client.post(f"/upload/{kind}", data=data,
                       content_type="multipart/form-data")


def finish_pending(app):
    loader.run_pending(app.config["DATA_DIR"], app.config["CHUNK_SIZE"])


def make_package(client, report_no, items):
    return client.post("/packages", json={"report_no": report_no, "items": items})


def pkg_row(app, pid):
    conn = db.connect(app.config["DATA_DIR"])
    try:
        return logic.get_package(conn, pid)
    finally:
        conn.close()


def active_packages(app):
    conn = db.connect(app.config["DATA_DIR"])
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM packages WHERE status='active'").fetchall()]
    finally:
        conn.close()


def baseline(client, app, rows):
    csv_upload(client, rows)
    finish_pending(app)


# ---------------------------------------------------------------- 建包与下载

def test_create_package_freezes_and_downloads_csv(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 1), ("R1", "镉", "0.2", 2),
                           ("R2", "铅", "0.3", 1)])
    r = make_package(client, "R1", ["镉", "铅"])   # 录入顺序:镉在前
    assert r.status_code == 200
    pid = r.get_json()["package"]["id"]

    pkgs = client.get("/api/packages").get_json()["packages"]
    assert len(pkgs) == 1 and pkgs[0]["status"] == "active"
    assert pkgs[0]["report_no"] == "R1" and pkgs[0]["item_count"] == 2

    detail = client.get(f"/api/packages/{pid}").get_json()["package"]
    assert [(i["item"], i["result"], i["revision"]) for i in detail["items"]] == \
           [("镉", "0.2", 2), ("铅", "0.1", 1)]     # 冻结值,保持录入顺序

    dl = client.get(f"/packages/{pid}/download")
    assert dl.status_code == 200
    assert "text/csv" in dl.headers["Content-Type"]
    assert "attachment" in dl.headers["Content-Disposition"]
    lines = dl.data.decode("utf-8-sig").strip().split("\n")
    assert lines == ["report_no,item,result,revision",
                     "R1,镉,0.2,2", "R1,铅,0.1,1"]


def test_create_package_missing_and_withdrawn_items_listed(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 1), ("R1", "镉", "0.2", 1)])
    csv_upload(client, [("R1", "镉", "withdraw", "", 3)], kind="inc")  # 撤回镉
    r = make_package(client, "R1", ["铅", "镉", "汞"])
    assert r.status_code == 422
    body = r.get_json()
    assert body["ok"] is False
    reasons = {p["item"]: p["reason"] for p in body["problems"]}
    assert "缺失" in reasons["汞"] and "已撤回" in reasons["镉"]
    assert "铅" not in reasons                       # 齐全的项目不列出
    assert client.get("/api/packages").get_json()["packages"] == []   # 未建包


def test_create_package_validation(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 1)])
    assert make_package(client, "", ["铅"]).status_code == 400          # 缺报告号
    assert make_package(client, "R1", []).status_code == 400            # 0 个项目
    assert make_package(client, "R1", ["  ", ""]).status_code == 400    # 全空
    assert make_package(client, "R1", ["铅", "铅"]).status_code == 400  # 重复
    r = make_package(client, "R1", [f"项{i}" for i in range(21)])
    assert r.status_code == 400 and "20" in r.get_json()["error"]       # 超 20 个
    ok = make_package(client, "R1", ["铅"] + [f"项{i}" for i in range(19)])
    assert ok.status_code == 422        # 恰好 20 个:通过数量校验,仅因项目缺失被拒
    assert client.post("/packages", json={"report_no": "R1"}).status_code == 400


def test_download_unknown_package_404(app, client):
    assert client.get("/packages/999/download").status_code == 404
    assert client.get("/api/packages/999").status_code == 404


# ---------------------------------------------------------------- 增量触发停用

def test_increment_correction_disables_package_and_blocks_download(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 1), ("R1", "镉", "0.2", 1),
                           ("R2", "铅", "0.3", 1)])
    pid = make_package(client, "R1", ["铅"]).get_json()["package"]["id"]
    pid_same_report_other_item = \
        make_package(client, "R1", ["镉"]).get_json()["package"]["id"]
    pid_other_report = make_package(client, "R2", ["铅"]).get_json()["package"]["id"]

    csv_upload(client, [("R1", "铅", "correct", "0.9", 2)], kind="inc")

    pkg = pkg_row(app, pid)
    assert pkg["status"] == "disabled" and pkg["disabled_at"]
    assert "更正" in pkg["disabled_reason"] and "铅" in pkg["disabled_reason"]
    dl = client.get(f"/packages/{pid}/download")          # 变更提交后发起的下载须拒绝
    assert dl.status_code == 410 and "已停用" in dl.get_json()["error"]
    # 未选项目、其他报告的包不受影响
    assert pkg_row(app, pid_same_report_other_item)["status"] == "active"
    assert pkg_row(app, pid_other_report)["status"] == "active"
    assert client.get(
        f"/packages/{pid_same_report_other_item}/download").status_code == 200


def test_stale_increment_does_not_disable(app, client):
    """被忽略的旧增量(修订号不更高)不改变正式结果,不影响包。"""
    baseline(client, app, [("R1", "铅", "0.1", 2)])
    pid = make_package(client, "R1", ["铅"]).get_json()["package"]["id"]
    csv_upload(client, [("R1", "铅", "correct", "0.5", 1)], kind="inc")   # 旧修订
    csv_upload(client, [("R1", "铅", "correct", "0.55", 2)], kind="inc")  # 同修订
    assert pkg_row(app, pid)["status"] == "active"
    assert client.get(f"/packages/{pid}/download").status_code == 200


def test_withdrawal_validity_change_disables_but_recorded_does_not(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 5), ("R1", "镉", "0.2", 5)])
    pid1 = make_package(client, "R1", ["铅"]).get_json()["package"]["id"]
    pid2 = make_package(client, "R1", ["镉"]).get_json()["package"]["id"]
    # 撤回修订号 < 当前修订:仅记录水位,有效性不变 -> 不停用
    csv_upload(client, [("R1", "铅", "withdraw", "", 3)], kind="inc")
    assert pkg_row(app, pid1)["status"] == "active"
    # 撤回修订号 >= 当前修订:结果失效 -> 停用
    csv_upload(client, [("R1", "镉", "withdraw", "", 5)], kind="inc")
    pkg2 = pkg_row(app, pid2)
    assert pkg2["status"] == "disabled" and "撤回" in pkg2["disabled_reason"]
    assert client.get(f"/packages/{pid2}/download").status_code == 410


def test_disabled_is_permanent_even_if_value_restored(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 1)])
    pid = make_package(client, "R1", ["铅"]).get_json()["package"]["id"]
    csv_upload(client, [("R1", "铅", "correct", "0.9", 2)], kind="inc")
    assert pkg_row(app, pid)["status"] == "disabled"
    # 后来把结果改回原值(更高修订号):旧包不能重新可用
    csv_upload(client, [("R1", "铅", "correct", "0.1", 3)], kind="inc")
    assert pkg_row(app, pid)["status"] == "disabled"
    assert client.get(f"/packages/{pid}/download").status_code == 410
    # 但可以基于当前正式结果另建新包
    r = make_package(client, "R1", ["铅"])
    assert r.status_code == 200
    new_pid = r.get_json()["package"]["id"]
    assert client.get(f"/packages/{new_pid}/download").status_code == 200


# ---------------------------------------------------------------- 全量切换触发停用

def test_baseline_switch_disables_only_changed_packages(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 1), ("R1", "镉", "0.2", 1)])
    pid_changed = make_package(client, "R1", ["铅"]).get_json()["package"]["id"]
    pid_same = make_package(client, "R1", ["镉"]).get_json()["package"]["id"]
    # 新基线:铅改值改修订,镉原样保留
    baseline(client, app, [("R1", "铅", "0.5", 2), ("R1", "镉", "0.2", 1)])
    assert pkg_row(app, pid_changed)["status"] == "disabled"
    assert "基线切换" in pkg_row(app, pid_changed)["disabled_reason"]
    assert pkg_row(app, pid_same)["status"] == "active"
    assert client.get(f"/packages/{pid_changed}/download").status_code == 410
    assert client.get(f"/packages/{pid_same}/download").status_code == 200


def test_switch_removing_or_withdrawing_item_disables(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 1), ("R2", "镉", "0.2", 1)])
    pid_removed = make_package(client, "R1", ["铅"]).get_json()["package"]["id"]
    pid_kept = make_package(client, "R2", ["镉"]).get_json()["package"]["id"]
    baseline(client, app, [("R2", "镉", "0.2", 1)])   # 新基线不再含 R1 铅
    assert pkg_row(app, pid_removed)["status"] == "disabled"   # 项目被移除 -> 停用
    assert pkg_row(app, pid_kept)["status"] == "active"


def test_switch_with_during_load_increment_disables(app, client):
    """装载期间确认的增量合入新基线,同样触发停用。"""
    baseline(client, app, [("R1", "铅", "0.1", 1)])
    pid = make_package(client, "R1", ["铅"]).get_json()["package"]["id"]
    csv_upload(client, [("R1", "铅", "0.1", 1)])          # 新全量开始装载(值未变)
    csv_upload(client, [("R1", "铅", "correct", "0.9", 2)], kind="inc")  # 装载期间
    assert pkg_row(app, pid)["status"] == "disabled"      # 增量确认时即已停用
    finish_pending(app)
    assert pkg_row(app, pid)["status"] == "disabled"


# ---------------------------------------------------------------- 崩溃一致性

def test_crash_mid_increment_rolls_back_change_and_disable_together(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 1)])
    pid = make_package(client, "R1", ["铅"]).get_json()["package"]["id"]
    # 手工登记增量批次,注入故障模拟确认途中崩溃
    buf = "report_no,item,action,result,revision\nR1,铅,correct,0.9,2\n"
    up = os.path.join(app.config["DATA_DIR"], "uploads")
    os.makedirs(up, exist_ok=True)
    path = os.path.join(up, "inc.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write(buf)
    conn = db.connect(app.config["DATA_DIR"])
    conn.execute("INSERT INTO batches (kind, filename, orig_name, status, total_bytes)"
                 " VALUES ('inc', ?, 'inc.csv', 'processing', ?)",
                 (path, os.path.getsize(path)))
    conn.close()

    def boom(_idx):
        raise RuntimeError("simulated power loss")
    with pytest.raises(RuntimeError):
        loader.run_pending(app.config["DATA_DIR"], 3, chunk_hook=boom)
    # 崩溃:结果变更与包停用整体回滚 -> 旧结果仍在,包仍可用(状态一致)
    assert pkg_row(app, pid)["status"] == "active"
    conn = db.connect(app.config["DATA_DIR"])
    row = conn.execute("SELECT result, revision FROM results"
                       " WHERE report_no='R1' AND item='铅'").fetchone()
    assert (row["result"], row["revision"]) == ("0.1", 1)
    conn.close()
    # 重启恢复:变更与停用一起提交
    loader.run_pending(app.config["DATA_DIR"], 3)
    assert pkg_row(app, pid)["status"] == "disabled"
    assert client.get(f"/packages/{pid}/download").status_code == 410


def test_crash_during_switch_rolls_back_disable_with_results(app, client):
    baseline(client, app, [("R1", "铅", "0.1", 1)])
    pid = make_package(client, "R1", ["铅"]).get_json()["package"]["id"]
    csv_upload(client, [("R1", "铅", "0.5", 2)])           # 新全量,将改变铅
    finish_pending_part = app.config["DATA_DIR"]

    from app import logic as logic_mod
    orig = logic_mod.merge_and_switch

    def boom(conn, batch_id):
        raise RuntimeError("power loss during switch")
    logic_mod.merge_and_switch = boom
    try:
        with pytest.raises(RuntimeError):
            loader.run_pending(finish_pending_part, 500)
    finally:
        logic_mod.merge_and_switch = orig
    # 切换事务回滚:正式表仍是旧基线,包也未停用 -> 一致
    assert pkg_row(app, pid)["status"] == "active"
    conn = db.connect(app.config["DATA_DIR"])
    row = conn.execute("SELECT result FROM results"
                       " WHERE report_no='R1' AND item='铅'").fetchone()
    assert row["result"] == "0.1"
    conn.close()
    # 恢复后重做:切换与停用原子生效
    loader.run_pending(finish_pending_part, 500)
    assert pkg_row(app, pid)["status"] == "disabled"
    assert client.get(f"/packages/{pid}/download").status_code == 410


# ---------------------------------------------------------------- 并发:不留过时可用包

def test_concurrent_create_and_change_never_leaves_stale_active_package(app):
    baseline(app.test_client(), app, [("R1", "铅", "0.1", 1)])
    data_dir = app.config["DATA_DIR"]
    rev_counter = {"n": 10}
    lock = threading.Lock()

    def creator():
        with app.test_client() as c:
            for _ in range(15):
                c.post("/packages", json={"report_no": "R1", "items": ["铅"]})

    def changer():
        with app.test_client() as c:
            for _ in range(15):
                with lock:
                    rev_counter["n"] += 1
                    rev = rev_counter["n"]
                csv_upload(c, [("R1", "铅", "correct", f"v{rev}", rev)], kind="inc")

    threads = ([threading.Thread(target=creator) for _ in range(3)] +
               [threading.Thread(target=changer) for _ in range(2)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 不变式:每个"可用"包的冻结快照都与当前正式结果完全一致
    conn = db.connect(data_dir)
    stale = conn.execute(
        """
        SELECT p.id FROM packages p
         WHERE p.status='active' AND EXISTS (
             SELECT 1 FROM package_items pi
              WHERE pi.package_id = p.id AND NOT EXISTS (
                  SELECT 1 FROM results r
                   WHERE r.report_no = p.report_no AND r.item = pi.item
                     AND r.active = 1 AND r.result = pi.result
                     AND r.revision = pi.revision))
        """).fetchall()
    n_disabled = conn.execute(
        "SELECT COUNT(*) c FROM packages WHERE status='disabled'").fetchone()["c"]
    conn.close()
    assert stale == []
    assert n_disabled > 0        # 并发中确有包被变更停用
