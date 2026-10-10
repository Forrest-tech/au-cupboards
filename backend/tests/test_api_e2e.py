"""端到端 API 测试 —— 覆盖需求 1/2/3 的用户可见行为。

这些用例通过 FastAPI TestClient 打真实HTTP，验证的不是「函数返回
对了」而是「用户点一下能得到什么」，与单元测试互补。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PLAN = Path("/workspace/var/extract/plan.pdf")


@pytest.fixture(scope="module")
def client():
    """起一个指向临时 DB 的 TestClient（不污染开发库）。"""
    if not PLAN.exists():
        pytest.skip("缺少测试图纸")

    tmp = tempfile.mkdtemp(prefix="aucup-api-")
    os.environ["AUCUP_VAR_DIR"] = tmp

    from fastapi.testclient import TestClient

    import app.api.server as srv

    srv.VAR_DIR = Path(tmp)
    srv.UPLOAD_DIR = srv.VAR_DIR / "uploads"
    srv.EXPORT_DIR = srv.VAR_DIR / "export"
    srv.DB_PATH = srv.VAR_DIR / "aucup.db"
    # 缩略图目录也必须重定向 —— 否则删除用例会把开发库的 var/thumbs/ 删掉。
    # 早期版本漏了这条，测试一跑开发环境的缩略图就消失了。
    srv.THUMB_DIR_PATH = srv.VAR_DIR / "thumbs"
    srv.THUMB_DIR_PATH.mkdir(parents=True, exist_ok=True)
    for d in (srv.UPLOAD_DIR, srv.EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)

    with TestClient(srv.app) as c:
        yield c


@pytest.fixture(scope="module")
def parsed(client):
    with PLAN.open("rb") as f:
        r = client.post(
            "/api/parse?kind=building",
            files={"file": ("plan.pdf", f, "application/pdf")},
            data={"project": "74 KEELER ST CARLINGFORD"},
        )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 需求 1


def test_health_reports_config_and_dwg_backends(client):
    d = client.get("/api/health").json()
    assert d["ok"] is True
    assert d["config_version"], "必须回报规则版本"
    assert "dwg_backends" in d and "dwg_ready" in d


def test_parse_returns_floor_matrix(parsed):
    """需求 1：右侧要能按楼层按行显示 units 数。"""
    assert parsed["kind"] == "pdf"
    assert parsed["summary"]["total_units"] == 44
    assert parsed["summary"]["communal_units"] == 2
    floors = {f["floor_code"]: f for f in parsed["floor_summary"]}
    assert set(floors) == {"GROUND", "L1", "L2", "L3", "L4"}
    assert floors["GROUND"]["unit_count"] == 8
    assert floors["L1"]["unit_count"] == 11
    assert floors["L4"]["unit_count"] == 3
    # 每层必须给出该层的全部单元编号（需求 1「每层 units 数」）
    assert floors["L4"]["labels"] == ["401", "402", "403"]
    assert "CLA2" in floors["L4"]["communal_labels"]


def test_units_carry_variant_size_and_description(parsed):
    """需求 1：每层要显示 cupboard 的类型/样式/尺寸/描述。"""
    units = parsed["units"]
    assert len(units) == 46
    with_variant = [u for u in units if u["variant"]]
    assert len(with_variant) == 46, "每个单元都应匹配到柜型"
    u = with_variant[0]
    for key in ("code", "name", "grid", "positions", "w", "h", "d", "description", "reason"):
        assert key in u["variant"], f"variant缺字段 {key}"
    assert u["variant"]["w"] > 0 and u["variant"]["h"] > 0


def test_each_floor_has_multiple_variants(parsed):
    """需求 1 明确要求「一楼 5 units → 对应 2 种 cupboard」。"""
    by_floor: dict[str, set[str]] = {}
    for u in parsed["units"]:
        by_floor.setdefault(u["floor"], set()).add(u["variant"]["code"])
    assert len(by_floor["GROUND"]) >= 2, by_floor
    assert len(by_floor["L4"]) >= 2, by_floor
    assert len({c for s in by_floor.values() for c in s}) >= 3, by_floor


def test_cross_validation_is_agree(parsed):
    """双源交叉验证必须给出 AGREE 结论。"""
    cv = parsed["cross_validation"]
    assert cv["verdict"] == "AGREE"
    assert cv["plan_units"] == cv["schedule_units"] == 46
    assert cv["only_in_plan"] == []
    assert cv["only_in_schedule"] == []
    assert cv["area_mismatch"] == []
    assert cv["consistency"] == 1.0


def test_no_ai_used(parsed):
    """需求 4：矢量文本层场景不得使用 AI。"""
    assert parsed["used_ai"] is False
    assert parsed["multi_signals"]["used_ai"] is False


def test_all_five_export_formats_produced(parsed):
    """需求 1：导出 PDF / Word / JPG（另附 Excel / JSON 机读格式）。"""
    out = parsed["outputs"]
    for key in ("pdf", "word", "jpg", "excel", "json"):
        assert key in out, f"缺少导出格式 {key}"
        assert out[key], f"{key} 导出路径为空"
    assert len(out["jpg"]) >= 1
    for key in ("pdf", "word", "excel", "json"):
        assert Path(out[key]).exists(), f"{key} 文件不存在"
    for p in out["jpg"]:
        assert Path(p).exists(), f"JPG 文件不存在: {p}"
        assert Path(p).suffix.lower() in (".jpg", ".jpeg"), f"JPG 缺扩展名: {p}"


def test_raw_pdf_served_for_preview(client, parsed):
    """需求 1 中栏：原样显示 PDF 字节。"""
    r = client.get(f"/api/file/{parsed['job_id']}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"
    assert len(r.content) > 100_000


def test_job_detail_endpoint(client, parsed):
    jid = parsed["job_id"]
    d = client.get(f"/api/jobs/{jid}").json()
    assert d["id"] == jid
    assert len(d["floors"]) == 5
    assert len(d["units"]) == 46
    assert d["rule_desc"], "必须返回规则说明供溯源页展示"
    assert d["cross_validation"]["verdict"] == "AGREE"


def test_exports_endpoint_lists_downloadable_files(client, parsed):
    ex = client.get(f"/api/jobs/{parsed['job_id']}/exports").json()
    for k in ("pdf", "docx", "jpg"):
        assert k in ex, f"exports缺 {k}"
        r = client.get("/api/download", params={"path": ex[k]})
        assert r.status_code == 200
        assert len(r.content) > 500


def test_download_rejects_path_outside_export_dir(client):
    r = client.get("/api/download", params={"path": "/etc/passwd"})
    assert r.status_code == 403


def test_page_list_includes_floor_plans_and_schedule(parsed):
    pages = parsed["pages"]
    assert len(pages) == parsed["multi_signals"]["page_count"] == 35
    roles = [p["role"] for p in pages]
    assert roles.count("floor_plan") == 5, "应识别 5 张权威平面图"
    assert roles.count("unit_schedule") == 1


# ---------------------------------------------------------------- 需求 2


def test_cupboards_grouped_by_meter_combo(client):
    """需求 2：左侧按「含几套 water+gas」分组。"""
    gs = client.get("/api/cupboards").json()
    assert gs, "柜型库不应为空"
    for g in gs:
        assert "water+gas" in g["key"], g["key"]
        assert g["count"] == len(g["variants"])
        assert g["layout_forms"], "每组必须给出排布形式（1/2/3…）"
    # 至少有一个组合含多种排布（实测 3 套有 1x3 与 3x1 两种）
    assert any(len(g["layout_forms"]) >= 2 for g in gs), gs


def test_variant_crud_lifecycle(client):
    """需求 2：柜型的新增 / 编辑 / 删除。"""
    created = client.post(
        "/api/variants",
        json={
            "code": "TEST-CRUD-2x2",
            "rows": 2, "cols": 2,
            "w": 1250, "h": 1150, "d": 210,
            "spacing_h": 320, "spacing_v": 260,
            "description": "临时测试柜",
        },
    )
    assert created.status_code == 200, created.text
    vid = created.json()["id"]

    # 重复 code 应 409
    dup = client.post("/api/variants", json={"code": "TEST-CRUD-2x2", "rows": 2, "cols": 2})
    assert dup.status_code == 409

    # 列表里能查到
    assert any(v["id"] == vid for v in client.get("/api/variants").json())

    # 编辑文字与尺寸
    up = client.patch(f"/api/variants/{vid}",
                      json={"code": "TEST-CRUD-2x2", "description": "已编辑描述",
                            "w": 1300})
    assert up.status_code == 200
    vs = client.get("/api/variants").json()
    v = next(x for x in vs if x["id"] == vid)
    assert v["description"] == "已编辑描述"
    assert v["w"] == 1300

    # 对比
    cmp_ = client.post("/api/variants/compare",
                       json={"ids": [vid, vs[0]["id"]]})
    assert cmp_.status_code == 200, cmp_.text
    fields = {f["field"]: f for f in cmp_.json()}
    assert fields["w"]["differs"] is True
    assert fields["positions_total"]["differs"] in (True, False)

    # 删除
    assert client.delete(f"/api/variants/{vid}").status_code == 200
    assert not any(v["id"] == vid for v in client.get("/api/variants").json())


def test_variant_rename_keeps_positions(client):
    """改名**只改名字，不能顺带把套数改错**。

    实测踩坑：复用通用端点 ``PATCH /api/variants/{id}`` 改名，
    它会用 ``rows × cols`` 覆写 ``positions_total`` —— 但网格容量
    不是套数（真实图纸里13 Units 的柜网格是 5×4=20，只填了 13 套）。
    走它改个名字，套数就从 13 变成 20 了。所以单开了
    ``PATCH /api/variants/{id}/name``，这里守住这条边界。
    """
    r = client.post("/api/variants",
                    json={"code": "TEST-RENAME-2x3", "rows": 2, "cols": 3})
    assert r.status_code in (200, 409), r.text
    vid = r.json()["id"] if r.status_code == 200 else next(
        v["id"] for v in client.get("/api/variants").json()
        if v["code"] == "TEST-RENAME-2x3")

    before = next(v for v in client.get("/api/variants").json() if v["id"] == vid)
    up = client.patch(f"/api/variants/{vid}/name", json={"name": "改过的名字"})
    assert up.status_code == 200, up.text
    assert up.json()["name"] == "改过的名字"

    after = next(v for v in client.get("/api/variants").json() if v["id"] == vid)
    assert after["label"] == "改过的名字", "改名没被API 透出来"
    # 关键断言：套数、尺寸、排布都不能动
    assert after["positions"] == before["positions"], (
        f"改名把套数改了：{before['positions']} -> {after['positions']}")
    assert (after["rows"], after["cols"]) == (before["rows"], before["cols"])
    assert (after["w"], after["h"]) == (before["w"], before["h"])

    # 空名要拒绝
    assert client.patch(f"/api/variants/{vid}/name", json={"name": "  "}).status_code == 400
    assert client.patch("/api/variants/999999/name",
                        json={"name": "x"}).status_code == 404

    client.delete(f"/api/variants/{vid}")


def test_variant_label_survives_in_tree_data(client):
    """改名要能反映到列表接口，前端树靠它显示自定义名。"""
    r = client.post("/api/variants",
                    json={"code": "TEST-LABEL-X", "rows": 1, "cols": 2})
    assert r.status_code in (200, 409), r.text
    vid = r.json()["id"] if r.status_code == 200 else next(
        v["id"] for v in client.get("/api/variants").json()
        if v["code"] == "TEST-LABEL-X")

    assert client.patch(f"/api/variants/{vid}/name",
                        json={"name": "A 型"}).status_code == 200
    got = next(v for v in client.get("/api/variants").json() if v["id"] == vid)
    assert got["label"] == "A 型"

    client.delete(f"/api/variants/{vid}")


def test_variant_in_use_cannot_be_deleted(client):
    """已选用的柜型不允许直接删（保护已生成的清单）。"""
    units = client.get("/api/units", params={"job_id": 1}).json()
    if not units or not units[0]["variant"]:
        pytest.skip("尚无选型记录")
    vid = units[0]["variant"]["id"]
    r = client.delete(f"/api/variants/{vid}")
    assert r.status_code == 409


def test_compare_requires_two_ids(client):
    vs = client.get("/api/variants").json()
    r = client.post("/api/variants/compare", json={"ids": [vs[0]["id"]]})
    assert r.status_code == 400


# ---------------------------------------------------------------- 需求 3


def test_projects_lists_building_with_drawing(client, parsed):
    ps = client.get("/api/projects").json()
    assert ps
    p = next(x for x in ps if x["name"] == "74 KEELER ST CARLINGFORD")
    assert p["drawings"], "Building 下应挂有 Drawing"
    d = p["drawings"][0]
    assert d["page_count"] == 35
    assert d["latest_job_id"] == parsed["job_id"]


def test_create_building_node(client):
    r = client.post("/api/buildings",
                    data={"name": "TEST BUILDING", "address": "1 Test St"})
    assert r.status_code == 200
    assert r.json()["name"] == "TEST BUILDING"
    # 幂等：同名不应重复创建
    again = client.post("/api/buildings", data={"name": "TEST BUILDING"})
    assert again.json()["id"] == r.json()["id"]


def test_unsupported_suffix_rejected(client):
    r = client.post("/api/parse",
                    files={"file": ("x.txt", b"hello", "text/plain")},
                    data={"project": "T"})
    assert r.status_code == 400


def test_docx_input_returns_actionable_message(client):
    """DOCX 已接收并预览，但明确告知解析器状态（不静默失败）。"""
    buf = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    buf.write(b"PK\x03\x04dummy")
    buf.close()
    try:
        with open(buf.name, "rb") as f:
            r = client.post("/api/parse?kind=building",
                            files={"file": ("x.docx", f,
                                    "application/vnd.openxmlformats-officedocument."
                                    "wordprocessingml.document")},
                            data={"project": "T"})
        assert r.status_code == 200
        d = r.json()
        assert d["kind"] == "docx"
        assert d["ok"] is False
        assert "PDF" in d["error"], "必须给出可执行的下一步指引"
    finally:
        os.unlink(buf.name)


# ---------------------------------------------------------------- 修正字典


def test_corrections_roundtrip(client, parsed):
    jid = parsed["job_id"]
    c = client.post(f"/api/jobs/{jid}/corrections",
                    json={"kind": "fix_variant", "target_ref": "G01",
                          "after_value": {"variant_code": "CP-TRI-1x3"},
                          "note": "人工改派"})
    assert c.status_code == 200
    lst = client.get(f"/api/jobs/{jid}/corrections").json()
    assert any(x["target_ref"] == "G01" for x in lst)

# ---------------------------------------------------------------- 删除功能


def test_delete_variant_removes_thumbnail(client):
    """删除柜型要连它的缩略图文件一起删 ——
    只删库会留下 404 图片，只删文件会留下悬空引用。"""
    from app.api import server as srv

    # 造一条带图的变体
    r = client.post("/api/cupboards/from-dwg", json={
        "source_file": "t.dxf",
        "variants": [{"code": "DEL-TEST-1x1", "rows": 1, "cols": 1,
                      "w": 900, "h": 600}],
        "renders": [{"block_name": "DEL-TEST-1x1", "ok": True,
                     "image_name": "del_test.jpg", "width_mm": 900,
                     "height_mm": 600, "entity_count": 4}],
    })
    assert r.status_code == 200, r.text
    thumb = srv.THUMB_DIR_PATH / "del_test.jpg"
    thumb.write_bytes(b"\xff\xd8\xff" + b"x" * 900)     # 造一个真文件
    vid = next(v["id"] for v in client.get("/api/variants").json()
               if v["code"] == "DEL-TEST-1x1")
    assert thumb.is_file()

    d = client.delete(f"/api/variants/{vid}")
    assert d.status_code == 200
    assert d.json()["deleted"] == 1
    assert not any(v["id"] == vid for v in client.get("/api/variants").json())
    assert not thumb.exists(), "缩略图文件没被删除，会留下 404 图片"


def test_delete_variant_404(client):
    assert client.delete("/api/variants/999999").status_code == 404


def test_batch_delete_skips_missing(client):
    """批量删除里不存在的 id 不能让整批失败。"""
    vs = client.get("/api/variants").json()
    if not vs:
        pytest.skip("无变体")
    ids = [vs[0]["id"], 999998, 999999]
    r = client.post("/api/variants/delete", json={"ids": ids})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] >= 1
    assert set(body["missing"]) == {999998, 999999}


def test_batch_delete_empty_is_noop(client):
    r = client.post("/api/variants/delete", json={"ids": []})
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


def test_delete_by_code(client):
    client.post("/api/cupboards/from-dwg", json={
        "source_file": "t.dxf",
        "variants": [{"code": "DEL-CODE-2x2", "rows": 2, "cols": 2,
                      "w": 1400, "h": 1800}],
        "renders": [],
    })
    r = client.post("/api/variants/delete-by-code", json={"codes": ["DEL-CODE-2x2"]})
    assert r.status_code == 200
    assert r.json()["deleted"] == 1
    assert not any(v["code"] == "DEL-CODE-2x2"
                   for v in client.get("/api/variants").json())


def test_clear_keeps_seed_data(client):
    """清空解析结果必须保留种子数据 ——
    用户是「这份图纸认错了，重传」，不是「整个库都不要了」。"""
    before = {v["code"]: v for v in client.get("/api/variants").json()}
    seed_before = [c for c, v in before.items() if v["source"] != "dwg"]
    client.post("/api/cupboards/from-dwg", json={
        "source_file": "t.dxf",
        "variants": [{"code": "CLR-TEST-1x1", "rows": 1, "cols": 1}],
        "renders": [],
    })
    r = client.post("/api/variants/clear", json={"only_dwg": True})
    assert r.status_code == 200
    after = {v["code"]: v for v in client.get("/api/variants").json()}
    assert not any(c.startswith("CLR-TEST") for c in after), "DWG 变体没清掉"
    for c in seed_before:
        assert c in after, f"种子柜型 {c} 被误删"


def test_batch_delete_reports_in_use(client):
    """被单元选用的柜型在批量删除里应跳过，而不是让整批报错。"""
    units = client.get("/api/units", params={"job_id": 1}).json()
    if not units or not units[0].get("variant"):
        pytest.skip("尚无选型记录")
    used = units[0]["variant"]["id"]
    vs = client.get("/api/variants").json()
    free = next((v["id"] for v in vs if v["id"] != used), None)
    ids = [used] + ([free] if free else [])
    r = client.post("/api/variants/delete", json={"ids": ids})
    assert r.status_code == 200
    body = r.json()
    assert used in body["in_use"], "被选用的柜型应进 in_use 而不是被删"
    assert any(v["id"] == used for v in client.get("/api/variants").json())


# ---------------------------------------------------------------- 重复入库


def test_commit_replace_updates_existing_variant(client):
    """「库里已有相同的图」→ 用户选「替换」时改写原记录，不新建重复。

    用户诉求原文：「如果库里面已经有一样的图，那么就提示说已经存在，
    是否要替换，或者放弃这个入库。」

    关键边界：替换**只许改图与尺寸**，绝不许改套数 —— 套数由柜体几何
    决定，换一张图不该改变一个柜能装几套。
    """
    # 先造一条已存在的柜型
    r = client.post("/api/variants", json={
        "code": "TEST-DUP-A", "rows": 1, "cols": 2, "w": 1000, "h": 2000,
        "positions": 2,
    })
    assert r.status_code in (200, 409), r.text
    vid = r.json()["id"] if r.status_code == 200 else next(
        v["id"] for v in client.get("/api/variants").json()
        if v["code"] == "TEST-DUP-A")
    before = next(v for v in client.get("/api/variants").json() if v["id"] == vid)
    n_before = len(client.get("/api/variants").json())

    # 用「替换」模式提交一条同尺寸但新图/新尺寸的柜型
    up = client.post("/api/cupboards/from-dwg", json={
        "source_file": "test.dwg",
        "source_name": "test",
        "dup_mode": "replace",
        "renders": [],
        "variants": [{
            "code": "TEST-DUP-A-NEW", "rows": 1, "cols": 2,
            "w": 1010, "h": 2010, "positions": 2,
            "replace_id": vid,
        }],
    })
    assert up.status_code == 200, up.text
    assert up.json()["replaced"] == 1, "替换分支没被走到"
    assert up.json()["created"] == 0, "替换模式不该新建记录"

    after = next(v for v in client.get("/api/variants").json() if v["id"] == vid)
    assert after["w"] == 1010 and after["h"] == 2010, "尺寸没被替换"
    # 套数必须原样不动
    assert after["positions"] == before["positions"], (
        f"替换把套数改了：{before['positions']} -> {after['positions']}")
    assert len(client.get("/api/variants").json()) == n_before, "替换却新增了记录"
    client.delete(f"/api/variants/{vid}")


def test_commit_without_replace_still_creates(client):
    """默认（add）模式行为不变 —— 首次入库必须能正常新增。"""
    n_before = len(client.get("/api/variants").json())
    up = client.post("/api/cupboards/from-dwg", json={
        "source_file": "test.dwg",
        "source_name": "test",
        "variants": [{"code": "TEST-ADD-1", "rows": 1, "cols": 1,
                      "w": 800, "h": 1800, "positions": 1}],
    })
    assert up.status_code == 200, up.text
    assert up.json()["created"] == 1
    assert len(client.get("/api/variants").json()) == n_before + 1
    vid = next(v["id"] for v in client.get("/api/variants").json()
               if v["code"] == "TEST-ADD-1")
    client.delete(f"/api/variants/{vid}")


def test_replace_with_unknown_id_falls_back(client):
    """replace_id 指向不存在的记录时不能崩，也不能新建。"""
    up = client.post("/api/cupboards/from-dwg", json={
        "source_file": "test.dwg",
        "source_name": "test",
        "dup_mode": "replace",
        "variants": [{"code": "TEST-GHOST-1", "rows": 1, "cols": 1,
                      "w": 900, "h": 1900, "positions": 1,
                      "replace_id": 999999}],
    })
    assert up.status_code == 200, up.text
    # 找不到目标 → replaced 不计数
    assert up.json()["replaced"] == 0
    vid = next((v["id"] for v in client.get("/api/variants").json()
                if v["code"] == "TEST-GHOST-1"), None)
    if vid:
        client.delete(f"/api/variants/{vid}")
