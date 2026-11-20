"""换证相关回归测试：写入量与展图数量无关、连续同署名递补、停供原因、失败回滚、重启。"""
import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import db, logic

MAPPING_CSV = """展图号,展图名称,原图号
EXH-01,唐代壁画拼版,ORG-001
EXH-01,唐代壁画拼版,ORG-002
EXH-02,宋代山水拼版,ORG-004
"""

BATCH1_CSV = """授权号,原图号,是否允许商用,署名
LIC-001,ORG-001,是,摄影：张三
LIC-002,ORG-001,是,摄影：张三
LIC-003,ORG-002,是,摄影：李四
LIC-004,ORG-002,是,© 某图库
LIC-007,ORG-004,是,摄影：赵六
"""

# ORG-001 的第三张同署名授权；ORG-001 的不同署名授权
BATCH_EXTRA_CSV = """授权号,原图号,是否允许商用,署名
LIC-010,ORG-001,是,摄影：张三
LIC-011,ORG-001,是,© 博物馆
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EXHIBITION_DB", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def upload(client, text):
    return client.post(
        "/upload/licenses",
        files={"file": ("data.csv", text.encode("utf-8"), "text/csv")},
        follow_redirects=False)


def setup_base(client):
    assert client.post(
        "/upload/mapping",
        files={"file": ("m.csv", MAPPING_CSV.encode("utf-8"), "text/csv")},
        follow_redirects=False).status_code == 303
    assert upload(client, BATCH1_CSV).status_code == 303


def status(client, ex_id):
    r = client.get(f"/api/exhibitions/{ex_id}")
    assert r.status_code == 200
    return r.json()


def license_id(client, license_no):
    for lic in client.get("/api/licenses").json():
        if lic["license_no"] == license_no:
            return lic["id"]
    raise AssertionError(f"license {license_no} not found")


def revoke(client, license_no):
    r = client.post(f"/licenses/{license_id(client, license_no)}/revoke",
                    follow_redirects=False)
    assert r.status_code == 303
    return unquote(r.headers["location"])


def confirm(client, ex_id):
    data = {"editor": "王编辑"}
    for it in status(client, ex_id)["items"]:
        if it["license_valid"]:
            data[f"license_{it['original_id']}"] = it["license_no"]
            data[f"attribution_{it['original_id']}"] = it["attribution"]
    return client.post(f"/exhibitions/{ex_id}/confirm", data=data, follow_redirects=False)


def manifest(client, ex_id):
    r = client.get(f"/exhibitions/{ex_id}/manifest.csv")
    assert r.status_code == 200
    return r.content.decode("utf-8-sig")


# ---------------------------------------------------------------- 写入量

@pytest.mark.parametrize("n_exhibitions", [1, 1000])
def test_revoke_writes_one_row_regardless_of_exhibition_count(
        tmp_path, monkeypatch, n_exhibitions):
    """同一授权被 1 张和 1,000 张展图选用时，撤回实际改动的数据库行数都为 1。"""
    monkeypatch.setenv("EXHIBITION_DB", str(tmp_path / f"n{n_exhibitions}.db"))
    db.init_db()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        mapping = "展图号,展图名称,原图号\n" + "".join(
            f"EXH-{i:04d},展图{i},ORG-001\n" for i in range(n_exhibitions))
        logic.import_mapping(conn, mapping.encode("utf-8"))
        logic.import_licenses(conn, BATCH1_CSV.encode("utf-8"))
        for ex in conn.execute("SELECT id FROM exhibitions").fetchall():
            items = logic.get_items(conn, ex["id"])
            submitted = {it["original_id"]: (it["license_no"], it["attribution"])
                         for it in items}
            assert logic.confirm_manifest(conn, ex["id"], "王编辑", submitted) == []
        conn.commit()

        lic_id = conn.execute(
            "SELECT id FROM licenses WHERE license_no='LIC-001'").fetchone()["id"]

        conn.execute("BEGIN IMMEDIATE")
        before = conn.total_changes
        _, message = logic.revoke_license(conn, lic_id)
        changed = conn.total_changes - before
        conn.commit()
        assert changed == 1, f"{n_exhibitions} 张展图时撤回改动了 {changed} 行"
        assert "LIC-002" in message and "署名确认继续有效" in message

        # 读取路径（页面/API/下载共用）不写数据库
        before = conn.total_changes
        usage = logic.license_usage(conn)
        st = logic.exhibition_status(conn, 1)
        assert conn.total_changes == before
        assert len(usage[license_id_of(conn, "LIC-002")]) == n_exhibitions
        assert license_id_of(conn, "LIC-001") not in usage
        assert st["downloadable"] is True
        assert st["items"][0]["license_no"] == "LIC-002"
    finally:
        conn.close()


def license_id_of(conn, license_no):
    return conn.execute("SELECT id FROM licenses WHERE license_no=?",
                        (license_no,)).fetchone()["id"]


# ---------------------------------------------------------------- 连续同署名换证

def test_consecutive_same_attribution_switches(client):
    """连续同署名换证：页面、API、授权使用情况、确认与下载始终用当前授权号。"""
    setup_base(client)
    assert upload(client, BATCH_EXTRA_CSV).status_code == 303
    assert confirm(client, 1).status_code == 303
    assert status(client, 1)["downloadable"] is True

    msg = revoke(client, "LIC-001")
    assert "LIC-002" in msg and "署名确认继续有效" in msg
    assert status(client, 1)["downloadable"] is True
    assert ["ORG-001", "LIC-002", "摄影：张三"] in [
        line.split(",") for line in manifest(client, 1).splitlines()[1:]]
    assert client.get("/exhibitions/1/manifest.csv").status_code == 200

    msg = revoke(client, "LIC-002")
    assert "LIC-010" in msg and "署名确认继续有效" in msg
    assert status(client, 1)["downloadable"] is True

    # API：展图详情使用 LIC-010，且不需要重新确认
    st = status(client, 1)
    org1 = [i for i in st["items"] if i["original_no"] == "ORG-001"][0]
    assert (org1["license_no"], org1["attribution"], org1["confirmed"]) == (
        "LIC-010", "摄影：张三", True)

    # API + 网页：授权使用情况反映当前授权
    lics = {l["license_no"]: l for l in client.get("/api/licenses").json()}
    assert lics["LIC-010"]["used_by"] == ["EXH-01"]
    assert lics["LIC-001"]["used_by"] == []
    assert lics["LIC-002"]["used_by"] == []
    html = client.get("/licenses").text
    assert row_containing(html, "LIC-010") and "EXH-01" in row_containing(html, "LIC-010")
    assert "EXH-01" not in row_containing(html, "LIC-001")
    assert "EXH-01" not in row_containing(html, "LIC-002")

    # 展图详情页展示的是当前授权号
    detail = client.get("/exhibitions/1").text
    assert "LIC-010" in detail

    # CSV 下载用当前有效授权号
    assert "LIC-010" in manifest(client, 1)

    # 另一次需要确认的换证：确认记录与 CSV 都写当前授权号（ORG-001=LIC-010）
    msg = revoke(client, "LIC-003")  # ORG-002 仅剩不同署名的 LIC-004
    assert "停供待确认新署名" in msg
    assert confirm(client, 1).status_code == 303
    latest = client.get("/exhibitions/1").text
    assert "ORG-001=LIC-010" in latest
    assert "ORG-002=LIC-004" in latest
    assert ["ORG-001", "LIC-010", "摄影：张三"] in [
        line.split(",") for line in manifest(client, 1).splitlines()[1:]]


def row_containing(html, text):
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        if text in row:
            return row
    raise AssertionError(f"no row containing {text}")


# ---------------------------------------------------------------- 停供原因

def test_second_revoke_reports_missing_vs_unconfirmed(client):
    setup_base(client)
    assert upload(client, BATCH_EXTRA_CSV).status_code == 303
    assert confirm(client, 1).status_code == 303

    revoke(client, "LIC-001")  # 同署名递补 LIC-002
    revoke(client, "LIC-002")  # 同署名递补 LIC-010
    assert status(client, 1)["downloadable"] is True

    # 第三次撤回：无同署名备选，仅剩不同署名 → 停供，等待确认新署名
    msg = revoke(client, "LIC-010")
    assert "停供待确认新署名" in msg and "LIC-011" in msg
    st = status(client, 1)
    assert st["downloadable"] is False
    assert st["unconfirmed"] == ["ORG-001"]
    org1 = [i for i in st["items"] if i["original_no"] == "ORG-001"][0]
    assert (org1["license_no"], org1["attribution"]) == ("LIC-011", "© 博物馆")
    assert client.get("/exhibitions/1/manifest.csv").status_code == 409
    detail = client.get("/exhibitions/1").text
    assert "需要确认新署名的原图" in detail and "ORG-001" in detail

    # 再撤回：无任何有效商用授权 → 停供，原因是缺授权
    msg = revoke(client, "LIC-011")
    assert "停供缺有效商用授权" in msg
    st = status(client, 1)
    assert st["downloadable"] is False
    assert st["missing"] == ["ORG-001"]
    assert st["unconfirmed"] == []
    org1 = [i for i in st["items"] if i["original_no"] == "ORG-001"][0]
    assert org1["license_no"] is None
    assert org1["license_valid"] is False
    detail = client.get("/exhibitions/1").text
    assert "缺有效商用授权的原图" in detail and "ORG-001" in detail
    # 缺授权时确认无效
    assert "err=" in confirm(client, 1).headers["location"]
    assert status(client, 1)["downloadable"] is False


# ---------------------------------------------------------------- 失败回滚
def test_failed_revoke_rolls_back_and_original_license_stays_in_use(client):
    setup_base(client)
    assert confirm(client, 1).status_code == 303
    lic_id = license_id(client, "LIC-001")

    conn = db.connect()
    try:
        conn.execute(
            "CREATE TRIGGER fail_revoke BEFORE UPDATE ON licenses"
            " WHEN NEW.revoked = 1 BEGIN SELECT RAISE(ABORT, '模拟写入失败'); END")
    finally:
        conn.close()

    location = revoke(client, "LIC-001")
    assert "err=" in location and "原授权继续有效" in location
    # 原授权未被撤回，展图继续使用它
    lics = {l["license_no"]: l for l in client.get("/api/licenses").json()}
    assert lics["LIC-001"]["revoked"] == 0
    assert status(client, 1)["downloadable"] is True
    assert ["ORG-001", "LIC-001", "摄影：张三"] in [
        line.split(",") for line in manifest(client, 1).splitlines()[1:]]
    assert lic_id == lics["LIC-001"]["id"]


# ---------------------------------------------------------------- 重启后按同一规则供数

def test_restart_after_consecutive_switches_and_suspension(tmp_path, monkeypatch):
    monkeypatch.setenv("EXHIBITION_DB", str(tmp_path / "restart.db"))
    from app.main import app

    with TestClient(app) as c1:
        setup_base(c1)
        upload(c1, BATCH_EXTRA_CSV)
        confirm(c1, 1)
        revoke(c1, "LIC-001")  # → LIC-002
        revoke(c1, "LIC-002")  # → LIC-010，同署名，仍可下载
        assert status(c1, 1)["downloadable"] is True

    with TestClient(app) as c2:
        st = status(c2, 1)
        assert st["downloadable"] is True
        org1 = [i for i in st["items"] if i["original_no"] == "ORG-001"][0]
        assert org1["license_no"] == "LIC-010" and org1["confirmed"] is True
        assert "LIC-010" in manifest(c2, 1)
        # 重启后继续撤回：仅剩不同署名 → 停供待确认
        msg = revoke(c2, "LIC-010")
        assert "停供待确认新署名" in msg
        assert status(c2, 1)["unconfirmed"] == ["ORG-001"]

    with TestClient(app) as c3:
        st = status(c3, 1)
        assert st["downloadable"] is False
        assert st["unconfirmed"] == ["ORG-001"]
        assert c3.get("/exhibitions/1/manifest.csv").status_code == 409
        assert confirm(c3, 1).status_code == 303
        assert "LIC-011" in manifest(c3, 1)
