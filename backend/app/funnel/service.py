"""漏斗分析应用服务：事件接入、多周期批量重建、数据集读取与预测模型消费。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.funnel.domain import iso, parse_dt
from app.funnel.repository import funnel_repository


class FunnelService:
    def __init__(self, repository=None) -> None:
        # 允许注入仓储，保证批量重建与测试之间互不污染；默认使用全局单例。
        self.repository = repository or funnel_repository

    @staticmethod
    def _normalize_cutoff(cutoff_raw: Any) -> str:
        """统一观测截止点口径：任何合法时间写法都归一到 UTC ISO（Z 结尾）。"""
        return iso(parse_dt(cutoff_raw))

    # ------------------------------------------------------------ 事件接入
    def ingest_event(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        required = ["事件编号", "商机编号", "事件类型", "发生时间"]
        missing = [name for name in required if not str(values.get(name) or "").strip()]
        if missing:
            return None, missing
        return self.repository.ingest(values), []

    def list_events(self, *, opportunity_id: str | None = None, event_type: str | None = None):
        return self.repository.list_events(opportunity_id=opportunity_id, event_type=event_type)

    def ingest_log(self):
        return self.repository.ingest_log()

    # ------------------------------------------------------------ 批量重建
    def rebuild(self, cohorts: list[dict[str, Any]]) -> dict[str, Any]:
        """多选入组周期批量重建。

        每条队列在同一次提交中分别固定自己的观测截止点；周期之间互不共用成员
        集合。单条失败不影响其它周期，失败可原样重试，已成功周期的完整路径
        结果保留可用。
        """
        if not cohorts:
            return {"ok": False, "message": "至少选择一个入组周期", "results": []}
        generated_at = iso(datetime.now(timezone.utc).replace(tzinfo=None))
        results: list[dict[str, Any]] = []
        success = 0
        for item in cohorts:
            period = str(item.get("入组周期") or "").strip()
            cutoff_raw = item.get("观测截止时间")
            if not period:
                results.append({"ok": False, "入组周期": period, "message": "缺少入组周期"})
                continue
            try:
                cutoff_dt = parse_dt(cutoff_raw)
            except (ValueError, TypeError):
                results.append({"ok": False, "入组周期": period, "message": "观测截止时间无法解析"})
                continue
            cutoff_iso = iso(cutoff_dt)
            try:
                snapshot = self.repository.rebuild_cohort(period, cutoff_iso, generated_at=generated_at)
            except Exception as exc:  # 单条失败隔离，其它周期照常返回
                results.append({
                    "ok": False, "入组周期": period, "观测截止时间": cutoff_iso,
                    "message": f"重建失败：{exc}",
                })
                continue
            success += 1
            results.append({
                "ok": True,
                "入组周期": period,
                "观测截止时间": cutoff_iso,
                "有效样本量": snapshot["有效样本量"],
                "有效成员": [m["商机编号"] for m in snapshot["成员"]],
                "成员明细": snapshot["成员"],
                "被排除事件": snapshot["被排除事件"],
                "截止点之外事件": snapshot["截止点之外事件"],
                "版本": snapshot["版本"],
                "被排除事件数": len(snapshot["被排除事件"]),
                "截止点之外事件数": len(snapshot["截止点之外事件"]),
                "对账": snapshot["对账"],
                "message": "队列重建完成",
            })
        return {
            "ok": success > 0,
            "message": f"批量重建结束：成功 {success} 个周期，失败 {len(cohorts) - success} 个",
            "results": results,
        }

    # ------------------------------------------------------------ 数据集读取
    def get_dataset(self, period: str, cutoff_iso: str) -> dict[str, Any] | None:
        return self.repository.get_snapshot(period, self._normalize_cutoff(cutoff_iso))

    def list_datasets(self):
        return self.repository.list_snapshots()

    def get_path(self, period: str, cutoff_iso: str, opportunity_id: str) -> dict[str, Any] | None:
        snapshot = funnel_repository.get_snapshot(period, self._normalize_cutoff(cutoff_iso))
        if snapshot is None:
            return None
        return snapshot["路径明细"].get(opportunity_id)

    # ------------------------------------------------------------ 预测模型消费
    def consume_for_forecast(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """经营预测模型跨模块读取漏斗转化分析数据集。

        消费记录同时固化入组周期、观测截止时间与有效样本量，并单列右删失样本，
        避免把观察期不足的商机当成最终流失。
        """
        period = str(values.get("入组周期") or "").strip()
        cutoff_raw = values.get("观测截止时间")
        consumer = str(values.get("消费方") or "经营预测模型").strip()
        task_no = str(values.get("任务编号") or "").strip()
        if not period or cutoff_raw in (None, ""):
            return None, "消费漏斗数据集需提供入组周期与观测截止时间"
        try:
            cutoff_iso = self._normalize_cutoff(cutoff_raw)
        except (ValueError, TypeError):
            return None, "观测截止时间无法解析"
        snapshot = self.repository.get_snapshot(period, cutoff_iso)
        if snapshot is None:
            return None, f"入组周期 {period} 在截止点 {cutoff_iso} 的漏斗数据集尚未重建，请先重建队列"

        censored = snapshot["对账"]["观察期不足商机数（右删失，未当最终流失）"]
        record = {
            "记录编号": f"FCC-{len(self.repository.list_consumptions()) + 1:04d}",
            "消费方": consumer,
            "任务编号": task_no,
            "入组周期": period,
            "观测截止时间": cutoff_iso,
            "有效样本量": snapshot["有效样本量"],
            "右删失样本量": censored,
            "最终流失口径": "仅统计已确认流失且未在截止点前重新开启的商机，右删失样本不计入最终流失",
            "数据集版本": snapshot["版本"],
            "数据集待重建": snapshot["待重建"],
            "消费时间": iso(datetime.now(timezone.utc).replace(tzinfo=None)),
        }
        self.repository.add_consumption(record)
        return record, None

    def list_consumptions(self):
        return self.repository.list_consumptions()


funnel_service = FunnelService()
