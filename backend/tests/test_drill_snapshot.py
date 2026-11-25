"""下钻数据快照与图表报告测试：补录待证、来源补齐、历史报告复核。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import SERVICES
from app.services.analysis_chart import AnalysisChartService
from app.services.drill_down import DrillDownService
from app.snapshots import DrillSnapshotStore

client = TestClient(app)

OLD_BATCH = "drill_down-0000"  # 旧批次


def make_services(path=None):
    snap_store = DrillSnapshotStore(path)
    drill = DrillDownService(snapshot_store=snap_store)
    chart = AnalysisChartService(snapshot_store=snap_store)
    return drill, chart, snap_store


def seed_snapshot(drill):
    """同一父组的两份子数据（12、8）都有旧批次，固化为已确认快照。"""
    return drill.create_snapshot(1, groups=[{
        "parent": "父组A",
        "dimension": "渠道",
        "records": [
            {"child": "子项甲", "value": 12, "batch": OLD_BATCH, "source": "来源凭据-甲"},
            {"child": "子项乙", "value": 8, "batch": OLD_BATCH, "source": "来源凭据-乙"},
        ],
    }])


class TestDrillSnapshotScenario:
    """场景：两个子值 12、8 的图已发初版；补录 9 暂留来源（待证）；
    值班员先刷新图表核对，历史图与待证项分开查；补上来源后重跑，
    再核对前后两次数据。"""

    def test_full_scenario(self, tmp_path):
        drill, chart, _ = make_services(tmp_path / "snap.json")

        # 1) 旧批次两个子项 12、8 固化为已确认快照
        snap = seed_snapshot(drill)
        assert "error" not in snap
        assert snap["status"] == "已确认"
        sid = snap["snapshot_id"]
        statuses = [r["status"] for g in snap["groups"] for r in g["records"]]
        assert statuses == ["已确认", "已确认"]
        batches = [r["batch"] for g in snap["groups"] for r in g["records"]]
        assert batches == [OLD_BATCH, OLD_BATCH]

        # 2) 初版图表：父值按已确认子项逐子项复算 = 12 + 8 = 20
        v1 = chart.render_from_snapshot(1, sid)
        assert v1["version"] == 1
        assert v1["snapshot_id"] == sid
        assert v1["series"][0]["parent"] == "父组A"
        assert v1["series"][0]["value"] == 20
        assert {c["child"]: c["value"] for c in v1["series"][0]["children"]} == {
            "子项甲": 12, "子项乙": 8}
        assert len(v1["used_records"]) == 2
        assert v1["pending_records"] == []

        # 3) 补录 9，暂留来源凭据 → 只能记为待证记录
        sup = drill.supplement_record(sid, parent="父组A", child="子项乙", value=9)
        assert sup["ok"] is True
        assert sup["record"]["status"] == "待证"
        assert sup["record"]["source"] is None
        pending_record_id = sup["record"]["record_id"]

        # 4) 值班员先刷新图表核对：待证记录不计入，父值仍为 20
        v2 = chart.render_from_snapshot(1, sid)
        assert v2["version"] == 2
        assert v2["series"][0]["value"] == 20
        assert {c["child"]: c["value"] for c in v2["series"][0]["children"]} == {
            "子项甲": 12, "子项乙": 8}
        assert [r["value"] for r in v2["pending_records"]] == [9]
        assert v2["pending_records"][0]["status"] == "待证"

        # 5) 刷新后查历史初版报告：实际用的子项和来源不被当前下钻结果覆盖
        v1_again = chart.get_report(1, v1["report_id"])
        assert v1_again["series"][0]["value"] == 20
        assert {c["child"]: c["value"] for c in v1_again["series"][0]["children"]} == {
            "子项甲": 12, "子项乙": 8}
        sources_v1 = {s for c in v1_again["series"][0]["children"] for s in c["sources"]}
        assert sources_v1 == {"来源凭据-甲", "来源凭据-乙"}
        assert {r["record_id"] for r in v1_again["used_records"]} == {
            r["record_id"] for r in v1["used_records"]}

        # 6) 待证项单独可查
        pending = drill.pending_records(sid)
        assert pending["pending_count"] == 1
        assert pending["pending_records"][0]["record_id"] == pending_record_id
        assert pending["pending_records"][0]["value"] == 9

        # 7) 补上来源凭据 → 待证转已确认
        att = drill.attach_source(sid, pending_record_id, "来源凭据-乙-补录")
        assert att["ok"] is True
        assert att["record"]["status"] == "已确认"
        assert drill.pending_records(sid)["pending_count"] == 0

        # 8) 重跑图表：子项乙 = 8 + 9 = 17，父值复算 = 12 + 17 = 29
        v3 = chart.render_from_snapshot(1, sid)
        assert v3["version"] == 3
        assert v3["series"][0]["value"] == 29
        assert {c["child"]: c["value"] for c in v3["series"][0]["children"]} == {
            "子项甲": 12, "子项乙": 17}
        assert len(v3["used_records"]) == 3
        assert v3["pending_records"] == []

        # 9) 核对前后两次数据：差值即补录的 9；初版报告仍未被覆盖
        assert v3["series"][0]["value"] - v1["series"][0]["value"] == 9
        assert chart.get_report(1, v1["report_id"])["series"][0]["value"] == 20
        reports = chart.list_reports(1)
        assert reports["total"] == 3
        assert [r["version"] for r in reports["items"]] == [1, 2, 3]


class TestDrillSnapshotStore:
    def test_snapshot_persisted_across_reload(self, tmp_path):
        """快照与报告写穿到文件，重载（模拟进程重启）后仍可复核。"""
        path = tmp_path / "snap.json"
        drill, chart, _ = make_services(path)
        snap = seed_snapshot(drill)
        v1 = chart.render_from_snapshot(1, snap["snapshot_id"])

        drill2, chart2, _ = make_services(path)
        snap2 = drill2.get_snapshot(snap["snapshot_id"])
        assert snap2["groups"] == snap["groups"]
        assert snap2["history"]
        report = chart2.get_report(1, v1["report_id"])
        assert report["series"][0]["value"] == 20
        assert chart2.list_reports(1)["total"] == 1

    def test_snapshot_from_drill_execution(self, tmp_path):
        """不显式登记时，执行下钻并把首层结果展开为父子组事件明细。"""
        drill, chart, _ = make_services(tmp_path / "snap.json")
        snap = drill.create_snapshot(1)
        assert "error" not in snap
        assert snap["groups"]
        for group in snap["groups"]:
            assert group["records"]
            for rec in group["records"]:
                assert rec["status"] == "已确认"
                assert rec["source"]
                assert rec["record_id"]
        report = chart.render_from_snapshot(1, snap["snapshot_id"])
        assert report["series"]
        for s in report["series"]:
            # 父值可逐子项复算
            assert s["value"] == round(sum(c["value"] for c in s["children"]), 2)

    def test_snapshot_from_drill_execution_deterministic(self, tmp_path):
        drill, _, _ = make_services(tmp_path / "snap.json")
        snap_a = drill.create_snapshot(1)
        snap_b = drill.create_snapshot(1)

        def content(groups):
            return [
                {
                    "parent": g["parent"],
                    "records": [
                        {k: r[k] for k in ("child", "value", "batch", "source")}
                        for r in g["records"]
                    ],
                }
                for g in groups
            ]

        assert content(snap_a["groups"]) == content(snap_b["groups"])

    def test_supplement_with_source_confirmed_immediately(self, tmp_path):
        drill, chart, _ = make_services(tmp_path / "snap.json")
        snap = seed_snapshot(drill)
        sid = snap["snapshot_id"]
        sup = drill.supplement_record(
            sid, parent="父组A", child="子项丙", value=5, source="来源凭据-丙")
        assert sup["record"]["status"] == "已确认"
        report = chart.render_from_snapshot(1, sid)
        assert report["series"][0]["value"] == 25
        assert {c["child"]: c["value"] for c in report["series"][0]["children"]} == {
            "子项甲": 12, "子项乙": 8, "子项丙": 5}

    def test_error_paths(self, tmp_path):
        drill, chart, _ = make_services(tmp_path / "snap.json")
        assert "error" in drill.create_snapshot(99999)
        assert "error" in drill.supplement_record(9999, parent="父组A", child="子项", value=1)
        assert "error" in drill.supplement_record(1, parent="", child="子项", value=1)
        assert "error" in drill.attach_source(1, "S1-R9999", "来源")
        assert "error" in drill.attach_source(1, "S1-R0001", "")
        assert "error" in drill.pending_records(9999)
        assert "error" in chart.render_from_snapshot(1, 9999)
        assert "error" in chart.render_from_snapshot(9999, 1)
        assert "error" in chart.get_report(1, 9999)

    def test_invalid_groups_rejected(self, tmp_path):
        drill, _, _ = make_services(tmp_path / "snap.json")
        assert "error" in drill.create_snapshot(1, groups=[{"parent": "", "records": []}])
        assert "error" in drill.create_snapshot(1, groups=[
            {"parent": "父组A", "records": [{"child": "", "value": 1}]}])
        assert "error" in drill.create_snapshot(1, groups=[
            {"parent": "父组A", "records": [{"child": "子项", "value": "非数值"}]}])


class TestSnapshotAPI:
    """接口层：快照固化、补录待证、来源补齐、图表重跑与历史报告复核。"""

    @pytest.fixture
    def isolated_store(self, monkeypatch, tmp_path):
        snap_store = DrillSnapshotStore(tmp_path / "api_snap.json")
        monkeypatch.setattr(SERVICES["drill_down"], "snapshot_store", snap_store)
        monkeypatch.setattr(SERVICES["analysis_chart"], "snapshot_store", snap_store)
        return snap_store

    def test_scenario_via_api(self, isolated_store):
        # 1) 固化快照：同一父组两个子项 12、8（旧批次、带来源）
        resp = client.post("/api/domain/drill_down/1/snapshot", json={"groups": [{
            "parent": "父组A",
            "dimension": "渠道",
            "records": [
                {"child": "子项甲", "value": 12, "batch": OLD_BATCH, "source": "来源凭据-甲"},
                {"child": "子项乙", "value": 8, "batch": OLD_BATCH, "source": "来源凭据-乙"},
            ],
        }]})
        assert resp.status_code == 200
        snap = resp.json()
        assert snap["status"] == "已确认"
        sid = snap["snapshot_id"]

        # 快照可复核
        resp = client.get(f"/api/domain/drill_down/snapshots/{sid}")
        assert resp.status_code == 200
        assert len(resp.json()["groups"][0]["records"]) == 2
        resp = client.get("/api/domain/drill_down/1/snapshots")
        assert resp.json()["total"] == 1

        # 2) 初版图表：父值 20
        resp = client.post("/api/domain/analysis_chart/1/render-snapshot",
                           json={"snapshot_id": sid})
        assert resp.status_code == 200
        v1 = resp.json()
        assert v1["version"] == 1
        assert v1["series"][0]["value"] == 20

        # 3) 补录 9，暂留来源 → 待证
        resp = client.post(f"/api/domain/drill_down/snapshots/{sid}/supplement",
                           json={"parent": "父组A", "child": "子项乙", "value": 9})
        assert resp.status_code == 200
        sup = resp.json()
        assert sup["record"]["status"] == "待证"
        rec_id = sup["record"]["record_id"]

        # 4) 刷新图表：待证不计入，父值仍 20；待证项单列
        resp = client.post("/api/domain/analysis_chart/1/render-snapshot",
                           json={"snapshot_id": sid})
        v2 = resp.json()
        assert v2["version"] == 2
        assert v2["series"][0]["value"] == 20
        assert [r["value"] for r in v2["pending_records"]] == [9]

        resp = client.get(f"/api/domain/drill_down/snapshots/{sid}/pending")
        assert resp.json()["pending_count"] == 1

        # 5) 历史初版报告不被覆盖
        resp = client.get(f"/api/domain/analysis_chart/1/reports/{v1['report_id']}")
        assert resp.status_code == 200
        assert resp.json()["series"][0]["value"] == 20

        # 6) 补上来源 → 重跑图表 → 父值 29
        resp = client.post(f"/api/domain/drill_down/snapshots/{sid}/attach-source",
                           json={"record_id": rec_id, "source": "来源凭据-乙-补录"})
        assert resp.status_code == 200
        assert resp.json()["record"]["status"] == "已确认"

        resp = client.post("/api/domain/analysis_chart/1/render-snapshot",
                           json={"snapshot_id": sid})
        v3 = resp.json()
        assert v3["version"] == 3
        assert v3["series"][0]["value"] == 29
        assert {c["child"]: c["value"] for c in v3["series"][0]["children"]} == {
            "子项甲": 12, "子项乙": 17}

        # 7) 报告列表：三个版本都在，前后数据可核对
        resp = client.get("/api/domain/analysis_chart/1/reports")
        reports = resp.json()
        assert reports["total"] == 3
        values = [r["series"][0]["value"] for r in reports["items"]]
        assert values == [20, 20, 29]

    def test_snapshot_auto_generated_via_api(self, isolated_store):
        resp = client.post("/api/domain/drill_down/1/snapshot")
        assert resp.status_code == 200
        snap = resp.json()
        assert snap["groups"]
        resp = client.post("/api/domain/analysis_chart/1/render-snapshot",
                           json={"snapshot_id": snap["snapshot_id"]})
        assert resp.status_code == 200
        report = resp.json()
        assert report["totals"]["used_records"] > 0

    def test_api_error_paths(self, isolated_store):
        resp = client.post("/api/domain/drill_down/99999/snapshot")
        assert "error" in resp.json()
        resp = client.get("/api/domain/drill_down/snapshots/9999")
        assert "error" in resp.json()
        resp = client.post("/api/domain/analysis_chart/1/render-snapshot",
                           json={"snapshot_id": 9999})
        assert "error" in resp.json()
        resp = client.get("/api/domain/analysis_chart/1/reports/9999")
        assert "error" in resp.json()
