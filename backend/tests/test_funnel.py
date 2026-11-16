"""事件驱动漏斗分析的完整口径测试。

覆盖：事件去重、同刻矛盾不靠次序、接收顺序颠倒、流失后重开、重复成交、
孤儿事件、跨日停留、右删失不补零、迟到更正重建、批量重建隔离、
队列成员不跨期借用、预测消费留痕与对账自洽。
"""
from __future__ import annotations

import unittest
from datetime import datetime

from app.funnel.domain import (
    EVT_CHURN,
    EVT_ENTER,
    EVT_EXIT,
    EVT_REOPEN,
    EVT_WIN,
    FunnelEvent,
    REASON_BUSINESS_DUPLICATE,
    REASON_CONFLICT,
    REASON_DUP_TERMINAL,
    REASON_ENTER_WHILE_OPEN,
    REASON_ILLEGAL_REOPEN,
    REASON_ORPHAN_EXIT,
    REASON_ORPHAN_TERMINAL,
    build_cohort,
    parse_dt,
    reconstruct_opportunity,
)
from app.funnel.repository import FunnelRepository
from app.funnel.service import FunnelService


def ev(eid, opp, etype, occurred, received=None, stage=None):
    return FunnelEvent(
        event_id=eid,
        opportunity_id=opp,
        event_type=etype,
        stage=stage,
        occurred_at=parse_dt(occurred),
        received_at=parse_dt(received or occurred),
        raw={},
    )


CUTOFF = "2026-09-30T23:59:59Z"


class ParseTests(unittest.TestCase):
    def test_z_suffix_and_timezone_normalized_to_utc(self):
        self.assertEqual(parse_dt("2026-09-01T08:00:00Z"), datetime(2026, 9, 1, 8, 0, 0))
        self.assertEqual(
            parse_dt("2026-09-01T16:00:00+08:00"),
            datetime(2026, 9, 1, 8, 0, 0),
        )

    def test_invalid_time_raises(self):
        with self.assertRaises(ValueError):
            parse_dt("not-a-time")


class ReconstructionTests(unittest.TestCase):
    def test_duplicate_event_id_distinct_biz_key_is_business_duplicate(self):
        # 同商机同刻同类型同阶段、不同事件编号：业务重复，只保留一条
        events = [
            ev("E1", "OPP-X", EVT_ENTER, "2026-09-01T09:00:00Z", stage="线索"),
            ev("E2", "OPP-X", EVT_ENTER, "2026-09-01T09:00:00Z", stage="线索"),
        ]
        path, excluded = reconstruct_opportunity(events, parse_dt(CUTOFF))
        self.assertIsNotNone(path)
        self.assertEqual(len(path.visits), 1)
        self.assertEqual(excluded[0]["排除原因"], REASON_BUSINESS_DUPLICATE)
        self.assertEqual(excluded[0]["事件编号"], "E2")  # 编号较大者被排除，与数组次序无关

    def test_duplicate_entry_while_open_counted_once(self):
        events = [
            ev("E1", "OPP-X", EVT_ENTER, "2026-09-01T09:00:00Z", stage="线索"),
            # 不同时刻再次进入同一进行中阶段
            ev("E2", "OPP-X", EVT_ENTER, "2026-09-02T09:00:00Z", stage="线索"),
        ]
        path, excluded = reconstruct_opportunity(events, parse_dt(CUTOFF))
        self.assertEqual(len(path.visits), 1)
        self.assertTrue(all(item["排除原因"] == REASON_ENTER_WHILE_OPEN for item in excluded))

    def test_same_timestamp_conflict_not_decided_by_array_order(self):
        # 成交与流失同刻；调换数组次序结果必须一致
        def build(reverse=False):
            group = [
                ev("W", "OPP-X", EVT_WIN, "2026-09-10T15:00:00Z"),
                ev("L", "OPP-X", EVT_CHURN, "2026-09-10T15:00:00Z"),
            ]
            if reverse:
                group.reverse()
            return reconstruct_opportunity(
                [ev("E", "OPP-X", EVT_ENTER, "2026-09-01T09:00:00Z", stage="线索"), *group],
                parse_dt(CUTOFF),
            )
        path_a, excl_a = build(False)
        path_b, excl_b = build(True)
        ids_a = sorted(item["事件编号"] for item in excl_a)
        ids_b = sorted(item["事件编号"] for item in excl_b)
        self.assertEqual(ids_a, ids_b)
        self.assertEqual(ids_a, ["L", "W"])
        self.assertTrue(all(item["排除原因"] == REASON_CONFLICT for item in excl_a))
        # 矛盾整组排除后，进入阶段仍在，截止点右删失
        self.assertTrue(path_a.open_at_cutoff)
        self.assertFalse(path_a.won_ever)
        self.assertFalse(path_a.churned_ever)

    def test_received_order_reversed_uses_occurred_time(self):
        # 退出先被接收，但发生时间晚于进入：按发生时间裁决，退出有效
        events = [
            ev("EXIT", "OPP-X", EVT_EXIT, "2026-09-02T08:00:00Z", received="2026-09-01T07:00:00Z", stage="线索"),
            ev("ENTER", "OPP-X", EVT_ENTER, "2026-09-01T08:00:00Z", received="2026-09-02T20:00:00Z", stage="线索"),
        ]
        path, excluded = reconstruct_opportunity(events, parse_dt(CUTOFF))
        self.assertEqual(excluded, [])
        self.assertEqual(len(path.visits), 1)
        visit = path.visits[0]
        self.assertFalse(visit.censored)
        self.assertTrue(visit.converted)
        self.assertEqual(visit.dwell_seconds, 86400.0)

    def test_churn_then_reopen_same_member_keeps_churn_history(self):
        events = [
            ev("E1", "OPP-X", EVT_ENTER, "2026-09-01T09:00:00Z", stage="线索"),
            ev("L1", "OPP-X", EVT_CHURN, "2026-09-05T09:00:00Z"),
            ev("R1", "OPP-X", EVT_REOPEN, "2026-09-08T09:00:00Z"),
            ev("E2", "OPP-X", EVT_ENTER, "2026-09-08T10:00:00Z", stage="线索"),
            ev("W1", "OPP-X", EVT_WIN, "2026-09-12T10:00:00Z"),
        ]
        path, excluded = reconstruct_opportunity(events, parse_dt(CUTOFF))
        self.assertEqual(excluded, [])
        self.assertEqual(path.rounds, 2)
        self.assertTrue(path.churned_ever)  # 上一轮流失经历不抹去
        self.assertTrue(path.won_ever)
        self.assertEqual(len(path.visits), 2)  # 两轮各一次阶段周期，仍是同一个商机

    def test_reopen_without_churn_is_rejected(self):
        events = [
            ev("E1", "OPP-X", EVT_ENTER, "2026-09-01T09:00:00Z", stage="线索"),
            ev("R1", "OPP-X", EVT_REOPEN, "2026-09-02T09:00:00Z"),
        ]
        path, excluded = reconstruct_opportunity(events, parse_dt(CUTOFF))
        self.assertEqual(excluded[0]["排除原因"], REASON_ILLEGAL_REOPEN)
        self.assertEqual(path.rounds, 1)

    def test_duplicate_win_counted_once(self):
        events = [
            ev("E1", "OPP-X", EVT_ENTER, "2026-09-01T09:00:00Z", stage="成交"),
            ev("W1", "OPP-X", EVT_WIN, "2026-09-05T09:00:00Z"),
            ev("W2", "OPP-X", EVT_WIN, "2026-09-06T09:00:00Z"),
        ]
        path, excluded = reconstruct_opportunity(events, parse_dt(CUTOFF))
        self.assertTrue(path.won_ever)
        self.assertEqual(len([v for v in path.visits if v.close_reason == "成交"]), 1)
        self.assertEqual(excluded[0]["排除原因"], REASON_DUP_TERMINAL)

    def test_orphan_exit_and_terminal_excluded_no_member(self):
        events = [
            ev("X1", "OPP-X", EVT_EXIT, "2026-09-01T09:00:00Z", stage="线索"),
            ev("W1", "OPP-X", EVT_WIN, "2026-09-02T09:00:00Z"),
        ]
        path, excluded = reconstruct_opportunity(events, parse_dt(CUTOFF))
        self.assertIsNone(path)  # 没有任何有效进入，不具备成员资格
        reasons = {item["排除原因"] for item in excluded}
        self.assertIn(REASON_ORPHAN_EXIT, reasons)
        self.assertIn(REASON_ORPHAN_TERMINAL, reasons)

    def test_cross_day_dwell_and_open_stage_never_zero_filled(self):
        events = [
            ev("E1", "OPP-X", EVT_ENTER, "2026-09-18T09:00:00Z", stage="线索"),
            ev("X1", "OPP-X", EVT_EXIT, "2026-09-19T09:00:00Z", stage="线索"),  # 跨日 24h
            ev("E2", "OPP-X", EVT_ENTER, "2026-09-19T10:00:00Z", stage="商务谈判"),  # 未结束
        ]
        path, _ = reconstruct_opportunity(events, parse_dt(CUTOFF))
        first, second = path.visits
        self.assertTrue(first.cross_day)
        self.assertEqual(first.dwell_seconds, 86400.0)
        self.assertTrue(second.censored)
        self.assertIsNone(second.dwell_seconds)  # 未知时长不补零
        self.assertTrue(path.open_at_cutoff)

    def test_implicit_close_on_stage_jump_keeps_dwell(self):
        events = [
            ev("E1", "OPP-X", EVT_ENTER, "2026-09-01T09:00:00Z", stage="线索"),
            # 没有退出事件，直接进入后续阶段：隐式关闭，时长保留
            ev("E2", "OPP-X", EVT_ENTER, "2026-09-03T09:00:00Z", stage="方案报价"),
        ]
        path, excluded = reconstruct_opportunity(events, parse_dt(CUTOFF))
        self.assertEqual(excluded, [])
        first = path.visits[0]
        self.assertEqual(first.close_reason, "隐式关闭（下一次阶段进入）")
        self.assertEqual(first.dwell_seconds, 2 * 86400.0)
        self.assertTrue(first.converted)

    def test_events_beyond_cutoff_excluded_and_listed(self):
        events = [
            ev("E1", "OPP-X", EVT_ENTER, "2026-09-01T09:00:00Z", stage="线索"),
            ev("E2", "OPP-X", EVT_ENTER, "2026-10-05T09:00:00Z", stage="线索"),
        ]
        path, excluded = reconstruct_opportunity(events, parse_dt(CUTOFF))
        self.assertEqual(len(path.visits), 1)
        self.assertIn("晚于观测截止点", excluded[0]["排除原因"])


class CohortTests(unittest.TestCase):
    def setUp(self):
        self.repo = FunnelRepository()
        self.service = FunnelService()
        # 仓储单例由种子数据初始化；这里直接基于 repo 的事件分组重建
        self.events_by_opp: dict[str, list[FunnelEvent]] = {}
        for item in self.repo.list_events():
            raw = FunnelEvent(
                event_id=item["事件编号"],
                opportunity_id=item["商机编号"],
                event_type=item["事件类型"],
                stage=item["阶段"],
                occurred_at=parse_dt(item["发生时间"]),
                received_at=parse_dt(item["接收时间"]),
                raw={},
            )
            self.events_by_opp.setdefault(raw.opportunity_id, []).append(raw)

    def test_seed_cohort_2026_09_members_and_reconcile(self):
        cohort = build_cohort("2026-09", parse_dt(CUTOFF), self.events_by_opp)
        member_ids = {m["商机编号"] for m in cohort["成员"]}
        # A1005 无有效进入不成成员；A1008 属于 10 期，不能借用进 09 期
        self.assertEqual(
            member_ids,
            {"OPP-A1001", "OPP-A1002", "OPP-A1003", "OPP-A1004", "OPP-A1006", "OPP-A1007"},
        )
        self.assertEqual(cohort["有效样本量"], 6)
        rec = cohort["对账"]
        # 队列总人数与成员明细人数必须一致
        self.assertEqual(rec["队列总人数"], rec["成员明细人数"])
        # 分阶段周期数之和等于全部成员的阶段周期总数
        self.assertEqual(rec["分阶段周期数之和"], rec["阶段周期总数"])
        # A1002 流失后重开：既计入历史流失，也仍是有效成员
        a1002 = cohort["路径明细"]["OPP-A1002"]
        self.assertTrue(a1002["历史流失"])
        self.assertEqual(a1002["经历轮次"], 2)
        self.assertEqual(rec["重开多轮商机数"], 1)
        # A1004 同刻矛盾：未成交也未流失，截止点未结束
        self.assertTrue(cohort["路径明细"]["OPP-A1004"]["截止点未结束"])
        # A1007 右删失：观察期不足，未计入最终流失。
        # A1002 第二轮成交发生在截止点之后、A1004 同刻矛盾无有效终止，
        # 三者在 09-30 截止点都属于观察期不足，不能当最终流失。
        self.assertEqual(rec["观察期不足商机数（右删失，未当最终流失）"], 3)

    def test_empty_cohort_never_borrows_members(self):
        # 2026-08 没有任何阶段事件：成员必须为空，不能共用 09 期成员集合
        cohort = build_cohort("2026-08", parse_dt(CUTOFF), self.events_by_opp)
        self.assertEqual(cohort["有效样本量"], 0)
        self.assertEqual(cohort["成员"], [])
        self.assertEqual(cohort["路径明细"], {})
        self.assertEqual(cohort["对账"]["队列总人数"], 0)

    def test_cohort_2026_10_isolated_with_own_cutoff(self):
        cohort = build_cohort("2026-10", parse_dt("2026-10-31T23:59:59Z"), self.events_by_opp)
        member_ids = {m["商机编号"] for m in cohort["成员"]}
        self.assertEqual(member_ids, {"OPP-A1008"})
        a1008 = cohort["路径明细"]["OPP-A1008"]
        self.assertTrue(a1008["历史流失"])

    def test_multi_round_same_stage_counted_as_separate_visits_one_member(self):
        # A1002 两轮都进入过“线索”：阶段周期计 2 次，但去重商机数仍为 1，成员只有一个
        cohort = build_cohort("2026-09", parse_dt(CUTOFF), self.events_by_opp)
        lead = next(m for m in cohort["分阶段指标"] if m["阶段"] == "线索")
        a1002_lead_visits = [
            v for v in cohort["路径明细"]["OPP-A1002"]["阶段周期"] if v["阶段"] == "线索"
        ]
        self.assertEqual(len(a1002_lead_visits), 2)
        self.assertGreaterEqual(lead["阶段周期数（含多轮）"], lead["商机数（去重成员）"])
        self.assertEqual(cohort["对账"]["队列总人数"], 6)

    def test_stage_dwell_unknown_is_null_not_zero(self):
        cohort = build_cohort("2026-09", parse_dt(CUTOFF), self.events_by_opp)
        negotiation = next(m for m in cohort["分阶段指标"] if m["阶段"] == "商务谈判")
        # A1002/A1007 的谈判阶段在截止点前都未结束：无已结束样本时平均停留为 None，
        # 绝不把未知时长补成零。
        self.assertGreaterEqual(negotiation["未结束周期数（右删失）"], 1)
        self.assertEqual(negotiation["停留时长"]["样本量"], 0)
        self.assertIsNone(negotiation["停留时长"]["平均停留秒"])
        # 线索阶段存在已结束周期，平均停留必须是真实秒数而非 None/0
        lead = next(m for m in cohort["分阶段指标"] if m["阶段"] == "线索")
        self.assertGreater(lead["停留时长"]["样本量"], 0)
        self.assertIsNotNone(lead["停留时长"]["平均停留秒"])
        self.assertGreater(lead["停留时长"]["平均停留秒"], 0)


class RepositoryAndServiceTests(unittest.TestCase):
    def setUp(self):
        # 每个用例独立仓储，避免全局单例的快照与事件跨用例累积。
        self.repo = FunnelRepository()
        self.service = FunnelService(self.repo)

    def test_event_id_dedup_on_ingest(self):
        raw = {
            "事件编号": "EVT-DUP-1", "商机编号": "OPP-NEW", "事件类型": "进入",
            "阶段": "线索", "发生时间": "2026-09-25T09:00:00Z", "接收时间": "2026-09-25T09:00:00Z",
        }
        first = self.repo.ingest(dict(raw))
        second = self.repo.ingest(dict(raw))
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("重复事件", second["原因"])
        stored = [e for e in self.repo.list_events(opportunity_id="OPP-NEW")]
        self.assertEqual(len(stored), 1)

    def test_batch_rebuild_independent_cutoffs_and_partial_failure(self):
        payload = {
            "cohorts": [
                {"入组周期": "2026-08", "观测截止时间": "2026-08-31T23:59:59Z"},  # 无事件的空周期
                {"入组周期": "2026-09", "观测截止时间": CUTOFF},
                {"入组周期": "2026-10", "观测截止时间": "2026-10-31T23:59:59Z"},
                {"入组周期": "2026-11", "观测截止时间": "bad-time"},
            ]
        }
        outcome = self.service.rebuild(payload["cohorts"])
        self.assertTrue(outcome["ok"])
        results = {r["入组周期"]: r for r in outcome["results"]}
        self.assertTrue(results["2026-08"]["ok"])
        self.assertEqual(results["2026-08"]["有效样本量"], 0)
        self.assertEqual(results["2026-08"]["有效成员"], [])  # 空周期不借用别期成员
        self.assertTrue(results["2026-09"]["ok"])
        self.assertTrue(results["2026-10"]["ok"])
        self.assertFalse(results["2026-11"]["ok"])
        # 两个周期各自独立的成员集合，不共用
        self.assertNotEqual(
            results["2026-09"]["有效样本量"],
            results["2026-10"]["有效样本量"],
        )
        # 逐条返回有效成员与被排除事件
        self.assertIn("OPP-A1001", results["2026-09"]["有效成员"])
        self.assertTrue(all("排除原因" in e for e in results["2026-09"]["被排除事件"]))

    def test_rebuild_is_resumable_and_keeps_prior_results(self):
        self.service.rebuild([{"入组周期": "2026-09", "观测截止时间": CUTOFF}])
        snap = self.service.get_dataset("2026-09", "2026-09-30T23:59:59")
        self.assertIsNotNone(snap)
        members_before = snap["有效样本量"]
        # 模拟中断后原样重试：结果仍完整且版本递增
        again = self.service.rebuild([{"入组周期": "2026-09", "观测截止时间": CUTOFF}])
        self.assertTrue(again["ok"])
        snap2 = self.service.get_dataset("2026-09", "2026-09-30T23:59:59")
        self.assertEqual(snap2["版本"], 2)
        self.assertEqual(snap2["有效样本量"], members_before)

    def test_late_event_marks_snapshot_stale_and_rebuild_picks_it_up(self):
        self.service.rebuild([{"入组周期": "2026-09", "观测截止时间": CUTOFF}])
        snap = self.service.get_dataset("2026-09", "2026-09-30T23:59:59")
        members_before = snap["有效样本量"]
        # 迟到事件：发生时间在截止点内，但接收时间晚于快照生成时间
        late = {
            "事件编号": "EVT-LATE-1", "商机编号": "OPP-LATE", "事件类型": "进入",
            "阶段": "线索", "发生时间": "2026-09-28T09:00:00Z",
            "接收时间": "2026-10-10T09:00:00Z",
        }
        ingest = self.repo.ingest(late)
        self.assertTrue(ingest["ok"])
        self.assertTrue(
            any(s["入组周期"] == "2026-09" for s in ingest["受影响快照"])
        )
        stale_snap = self.service.get_dataset("2026-09", "2026-09-30T23:59:59")
        self.assertTrue(stale_snap["待重建"])
        # 受影响路径重建后，新成员进入统计
        self.service.rebuild([{"入组周期": "2026-09", "观测截止时间": CUTOFF}])
        rebuilt = self.service.get_dataset("2026-09", "2026-09-30T23:59:59")
        self.assertFalse(rebuilt["待重建"])
        self.assertEqual(rebuilt["有效样本量"], members_before + 1)
        self.assertIn("OPP-LATE", {m["商机编号"] for m in rebuilt["成员"]})

    def test_forecast_consumption_persists_period_cutoff_sample_size(self):
        self.service.rebuild([{"入组周期": "2026-09", "观测截止时间": CUTOFF}])
        record, error = self.service.consume_for_forecast({
            "入组周期": "2026-09",
            "观测截止时间": CUTOFF,
            "任务编号": "FC-001",
        })
        self.assertIsNone(error)
        self.assertEqual(record["入组周期"], "2026-09")
        self.assertEqual(record["观测截止时间"], "2026-09-30T23:59:59Z")
        self.assertEqual(record["有效样本量"], 6)
        self.assertGreaterEqual(record["右删失样本量"], 1)
        # 消费未重建的数据集必须被拒绝，不能静默使用别期数据
        missing, err = self.service.consume_for_forecast({
            "入组周期": "2026-08", "观测截止时间": CUTOFF,
        })
        self.assertIsNone(missing)
        self.assertIn("尚未重建", err)

    def test_total_members_cross_check_paths(self):
        self.service.rebuild([{"入组周期": "2026-09", "观测截止时间": CUTOFF}])
        snap = self.service.get_dataset("2026-09", "2026-09-30T23:59:59")
        # 队列总人数 == 路径明细条数 == 成员明细条数
        self.assertEqual(snap["对账"]["队列总人数"], len(snap["路径明细"]))
        self.assertEqual(snap["对账"]["队列总人数"], len(snap["成员"]))
        # 每个成员的阶段周期数都能在路径明细里核验
        for member in snap["成员"]:
            path = snap["路径明细"][member["商机编号"]]
            self.assertEqual(member["阶段周期数"], len(path["阶段周期"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
