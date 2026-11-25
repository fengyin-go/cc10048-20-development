"""下钻数据快照存储：把下钻层级的事件明细持久化为可复核、可重复读取的数据产物。

快照（下钻层级事件明细）与图表报告（每次生成一个不可变版本）都写穿到本地
JSON 文件，默认落在 backend/.data/drill_snapshots.json，可用环境变量
DRILL_SNAPSHOT_PATH 覆盖；进程重启后仍能复核历史报告实际用到的子项与来源。
测试可传 path=None 使用纯内存实例。
"""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".data" / "drill_snapshots.json"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class DrillSnapshotStore:
    """下钻快照 + 图表报告的持久化存储（JSON 写穿）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._reports: dict[int, dict[str, Any]] = {}
        self._snapshot_seq = 0
        self._report_seq = 0
        self._record_seq = 0
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        seq = data.get("seq", {})
        self._snapshot_seq = int(seq.get("snapshot", 0))
        self._report_seq = int(seq.get("report", 0))
        self._record_seq = int(seq.get("record", 0))
        for snap in data.get("snapshots", []):
            self._snapshots[int(snap["snapshot_id"])] = snap
        for report in data.get("reports", []):
            self._reports[int(report["report_id"])] = report

    def _flush(self) -> None:
        if not self._path:
            return
        payload = {
            "seq": {
                "snapshot": self._snapshot_seq,
                "report": self._report_seq,
                "record": self._record_seq,
            },
            "snapshots": [self._snapshots[key] for key in sorted(self._snapshots)],
            "reports": [self._reports[key] for key in sorted(self._reports)],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ---- 下钻数据快照 ----
    def create_snapshot(
        self,
        *,
        path_id: int,
        path_name: str,
        base_metric: str,
        groups: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """把一批父子组事件明细固化为一份已确认的下钻数据快照。"""
        self._snapshot_seq += 1
        snapshot_id = self._snapshot_seq
        now = _now()
        for group in groups:
            for rec in group.get("records", []):
                self._record_seq += 1
                rec["record_id"] = f"S{snapshot_id}-R{self._record_seq:04d}"
                rec.setdefault("created_at", now)
        snapshot = {
            "snapshot_id": snapshot_id,
            "path_id": path_id,
            "path_name": path_name,
            "base_metric": base_metric,
            "status": "已确认",
            "revision": 1,
            "created_at": now,
            "groups": groups,
            "history": [
                {"revision": 1, "event": "创建快照", "at": now,
                 "detail": f"父组 {len(groups)} 个"},
            ],
        }
        self._snapshots[snapshot_id] = snapshot
        self._flush()
        return copy.deepcopy(snapshot)

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        snapshot = self._snapshots.get(int(snapshot_id))
        return copy.deepcopy(snapshot) if snapshot else None

    def list_snapshots(self, path_id: int | None = None) -> list[dict[str, Any]]:
        snapshots = [self._snapshots[key] for key in sorted(self._snapshots)]
        if path_id is not None:
            snapshots = [s for s in snapshots if int(s.get("path_id", 0)) == int(path_id)]
        return copy.deepcopy(snapshots)

    def append_record(
        self,
        snapshot_id: int,
        parent: str,
        dimension: str,
        record: dict[str, Any],
    ) -> dict[str, Any] | None:
        """向快照追加一条补录记录（快照 revision 自增，留痕可复核）。"""
        snapshot = self._snapshots.get(int(snapshot_id))
        if snapshot is None:
            return None
        group = next((g for g in snapshot["groups"] if g.get("parent") == parent), None)
        if group is None:
            group = {"parent": parent, "dimension": dimension, "records": []}
            snapshot["groups"].append(group)
        self._record_seq += 1
        saved = dict(record)
        saved["record_id"] = f"S{snapshot_id}-R{self._record_seq:04d}"
        saved["created_at"] = _now()
        group["records"].append(saved)
        snapshot["revision"] += 1
        snapshot["history"].append({
            "revision": snapshot["revision"],
            "event": "补录记录",
            "at": saved["created_at"],
            "detail": f"{parent}/{saved.get('child')} 值 {saved.get('value')} 状态 {saved.get('status')}",
        })
        self._flush()
        return copy.deepcopy(saved)

    def update_record(
        self,
        snapshot_id: int,
        record_id: str,
        event: str = "更新记录",
        **changes: Any,
    ) -> dict[str, Any] | None:
        """更新快照内单条记录（如补齐来源凭据），留痕可复核。"""
        snapshot = self._snapshots.get(int(snapshot_id))
        if snapshot is None:
            return None
        for group in snapshot["groups"]:
            for rec in group.get("records", []):
                if rec.get("record_id") == record_id:
                    rec.update(changes)
                    snapshot["revision"] += 1
                    snapshot["history"].append({
                        "revision": snapshot["revision"],
                        "event": event,
                        "at": _now(),
                        "detail": f"{record_id} 状态 {rec.get('status')}",
                    })
                    self._flush()
                    return copy.deepcopy(rec)
        return None

    # ---- 图表报告（不可变版本，历史报告不被覆盖） ----
    def create_report(
        self,
        *,
        chart_id: int,
        chart_name: str,
        snapshot_id: int,
        snapshot_revision: int,
        series: list[dict[str, Any]],
        used_records: list[dict[str, Any]],
        pending_records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """留存一版图表报告：冻结本次实际用到的子项、来源与待证记录。"""
        self._report_seq += 1
        report_id = self._report_seq
        version = 1 + max(
            (int(r["version"]) for r in self._reports.values()
             if int(r["chart_id"]) == int(chart_id)),
            default=0,
        )
        report = {
            "report_id": report_id,
            "chart_id": chart_id,
            "chart_name": chart_name,
            "snapshot_id": snapshot_id,
            "snapshot_revision": snapshot_revision,
            "version": version,
            "generated_at": _now(),
            "series": copy.deepcopy(series),
            "totals": {
                "parent_groups": len(series),
                "value": round(sum(float(s.get("value", 0)) for s in series), 2),
                "used_records": len(used_records),
                "pending_records": len(pending_records),
            },
            "used_records": copy.deepcopy(used_records),
            "pending_records": copy.deepcopy(pending_records),
        }
        self._reports[report_id] = report
        self._flush()
        return copy.deepcopy(report)

    def get_report(self, chart_id: int, report_id: int) -> dict[str, Any] | None:
        report = self._reports.get(int(report_id))
        if not report or int(report["chart_id"]) != int(chart_id):
            return None
        return copy.deepcopy(report)

    def list_reports(self, chart_id: int | None = None) -> list[dict[str, Any]]:
        reports = [self._reports[key] for key in sorted(self._reports)]
        if chart_id is not None:
            reports = [r for r in reports if int(r["chart_id"]) == int(chart_id)]
        return copy.deepcopy(reports)


def _default_path() -> str:
    return os.environ.get("DRILL_SNAPSHOT_PATH") or str(DEFAULT_PATH)


snapshot_store = DrillSnapshotStore(_default_path())
