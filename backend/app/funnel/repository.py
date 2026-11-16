"""漏斗分析内存仓储：事件库、接入审计、队列快照与预测消费记录。

设计要点：

- 重建是纯函数式重放，快照按「入组周期 + 观测截止点」整体覆盖写入，
  因此批量重建中断后重试，已成功周期的完整路径结果依然可用；
- 每个周期各自持有独立成员集合，仓储层不提供任何跨周期借用成员的入口；
- 迟到事件（接收时间晚于其所属快照的生成时间）会把受影响快照标记为待重建。
"""
from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any

from app.funnel.domain import FunnelEvent, build_cohort, iso, normalize_event, parse_dt
from app.funnel.seed_events import SEED_FUNNEL_EVENTS


class FunnelRepository:
    def __init__(self) -> None:
        self._lock = RLock()
        self._events: dict[str, FunnelEvent] = {}  # event_id -> 标准化事件
        self._ingest_log: list[dict[str, Any]] = []
        # 快照键：(入组周期, 观测截止时间 ISO)
        self._snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        self._consumptions: list[dict[str, Any]] = []
        self._load_seed()

    # ------------------------------------------------------------------ 初始化
    def _load_seed(self) -> None:
        for raw in SEED_FUNNEL_EVENTS:
            event, error = normalize_event(raw)
            if event is None:
                self._ingest_log.append({
                    "事件编号": raw.get("事件编号"), "结果": "拒绝", "原因": error,
                })
                continue
            self._events[event.event_id] = event
            self._ingest_log.append({"事件编号": event.event_id, "结果": "接入", "原因": None})

    # ------------------------------------------------------------------ 事件接入
    def ingest(self, raw: dict[str, Any]) -> dict[str, Any]:
        """接入单条事件；事件编号去重，重复编号只计一次。"""
        event, error = normalize_event(raw)
        with self._lock:
            if event is None:
                self._ingest_log.append({
                    "事件编号": raw.get("事件编号") or raw.get("event_id"),
                    "结果": "拒绝", "原因": error,
                })
                return {"ok": False, "事件编号": raw.get("事件编号"), "原因": error}
            if event.event_id in self._events:
                reason = "重复事件（事件编号已存在，只计一次）"
                self._ingest_log.append({"事件编号": event.event_id, "结果": "重复", "原因": reason})
                return {"ok": False, "事件编号": event.event_id, "原因": reason}
            self._events[event.event_id] = event
            self._ingest_log.append({"事件编号": event.event_id, "结果": "接入", "原因": None})
            # 迟到更正：接收时间晚于已有快照生成时间时，标记受影响快照待重建。
            stale = self._mark_stale(event)
            return {"ok": True, "事件编号": event.event_id, "原因": None, "受影响快照": stale}

    def _mark_stale(self, event: FunnelEvent) -> list[dict[str, str]]:
        affected: list[dict[str, str]] = []
        for (period, cutoff_iso), snapshot in self._snapshots.items():
            cutoff = parse_dt(cutoff_iso)
            watermark = parse_dt(snapshot["事件水位时间"])
            # 迟到更正：事件落在截止点内，但接收时间晚于快照构建时的事件水位，
            # 说明该快照没有纳入这条事件，受影响路径需要重建。
            if event.occurred_at <= cutoff and event.received_at > watermark:
                snapshot["待重建"] = True
                affected.append({"入组周期": period, "观测截止时间": cutoff_iso})
        return affected

    def list_events(
        self,
        *,
        opportunity_id: str | None = None,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events.values())
        if opportunity_id:
            events = [e for e in events if e.opportunity_id == opportunity_id]
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        events.sort(key=lambda e: (e.occurred_at, e.event_id))
        return [
            {
                "事件编号": e.event_id,
                "商机编号": e.opportunity_id,
                "事件类型": e.event_type,
                "阶段": e.stage,
                "发生时间": e.occurred_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "接收时间": e.received_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "迟到": e.received_at > e.occurred_at,
            }
            for e in events
        ]

    def ingest_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._ingest_log)

    # ------------------------------------------------------------------ 队列重建
    def rebuild_cohort(self, cohort_period: str, cutoff_iso: str, *, generated_at: str) -> dict[str, Any]:
        """重建单个周期快照并整体覆盖；纯重放保证中断重试后结果仍完整。"""
        cutoff = parse_dt(cutoff_iso)
        with self._lock:
            grouped: dict[str, list[FunnelEvent]] = {}
            watermark: datetime | None = None
            for event in self._events.values():
                grouped.setdefault(event.opportunity_id, []).append(event)
                if event.occurred_at <= cutoff and (
                    watermark is None or event.received_at > watermark
                ):
                    watermark = event.received_at
            snapshot = build_cohort(cohort_period, cutoff, grouped)
            snapshot["生成时间"] = generated_at
            # 事件水位：纳入本快照的事件中最大的接收时间，作为迟到更正的判断基准。
            snapshot["事件水位时间"] = iso(watermark) if watermark is not None else generated_at
            snapshot["待重建"] = False
            snapshot["版本"] = self._snapshots.get((cohort_period, cutoff_iso), {}).get("版本", 0) + 1
            self._snapshots[(cohort_period, cutoff_iso)] = snapshot
            return snapshot

    def get_snapshot(self, cohort_period: str, cutoff_iso: str) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self._snapshots.get((cohort_period, cutoff_iso))
            return dict(snapshot) if snapshot is not None else None

    def list_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "入组周期": period,
                    "观测截止时间": cutoff_iso,
                    "有效样本量": snap["有效样本量"],
                    "版本": snap["版本"],
                    "生成时间": snap["生成时间"],
                    "事件水位时间": snap["事件水位时间"],
                    "待重建": snap["待重建"],
                }
                for (period, cutoff_iso), snap in sorted(self._snapshots.items())
            ]

    # ------------------------------------------------------------------ 预测消费
    def add_consumption(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._consumptions.append(record)

    def list_consumptions(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._consumptions)


funnel_repository = FunnelRepository()
