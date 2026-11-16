"""漏斗分析领域口径：事件标准化、同刻矛盾识别、商机路径与队列重建。

本模块只做纯计算，不持有任何可变状态，因此：

- 迟到更正后可以拿同一批事件幂等重放，得到确定结果（可重试、可审计）；
- 同刻矛盾事件不靠数组次序裁决，而是按稳定键排序后整组识别、显式排除；
- 未知停留时长一律输出 ``None``，绝不补零。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 口径常量
# ---------------------------------------------------------------------------

# 漏斗阶段按业务先后排序；阶段序号即漏斗层级。
FUNNEL_STAGES: list[str] = ["线索", "商机确认", "方案报价", "商务谈判", "成交"]

# 五类阶段事件。
EVT_ENTER = "进入"
EVT_EXIT = "退出"
EVT_WIN = "成交"
EVT_CHURN = "流失"
EVT_REOPEN = "重新开启"

EVENT_TYPES = {EVT_ENTER, EVT_EXIT, EVT_WIN, EVT_CHURN, EVT_REOPEN}
# 终止类事件：结束当前一轮，必须在重新开启之后才能开始新一轮。
TERMINAL_TYPES = {EVT_WIN, EVT_CHURN}

# 排除原因（同时作为事件审计状态，前端与测试直接引用这些中文口径）。
REASON_DUPLICATE_EVENT = "重复事件（事件编号去重，只计一次）"
REASON_BUSINESS_DUPLICATE = "重复业务事件（同商机同阶段同刻同类型，仅保留一条）"
REASON_CONFLICT = "同刻矛盾事件（同一商机同一时刻存在互斥事件，不靠数组次序裁决）"
REASON_UNKNOWN_TYPE = "未知事件类型"
REASON_UNKNOWN_STAGE = "未知漏斗阶段"
REASON_ORPHAN_EXIT = "孤儿退出（缺少匹配的进入事件）"
REASON_ORPHAN_TERMINAL = "孤儿终止事件（缺少处于进行中的阶段）"
REASON_DUP_TERMINAL = "重复终止事件（上一终止结果保留，后续终止不重复计数）"
REASON_ILLEGAL_REOPEN = "非法重新开启（仅流失后允许重开，不能当作新客户）"
REASON_ENTER_WHILE_OPEN = "重复进入（该阶段已有进行中的周期，进入事件不重复计数）"
REASON_BEYOND_CUTOFF = "事件发生时间晚于观测截止点（不计入本期，留存事件库）"


@dataclass(frozen=True)
class FunnelEvent:
    """标准化后的阶段事件（不可变，保证重放稳定）。"""

    event_id: str
    opportunity_id: str
    event_type: str
    stage: str | None
    occurred_at: datetime
    received_at: datetime
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass
class Visit:
    """一次完整的“进入—退出”阶段周期；同一商机可在同一阶段产生多轮 Visit。"""

    round_no: int
    stage: str
    stage_order: int
    entered_at: datetime
    exited_at: datetime | None
    # 退出方式：正常退出/成交/流失/隐式关闭（被下一次进入或终止事件关闭）。
    close_reason: str | None
    # 停留时长（秒）。未结束阶段为 None（右删失），绝不补零。
    dwell_seconds: float | None
    censored: bool  # True 表示观测截止时仍停留在该阶段
    converted: bool  # 是否有效转化到下一阶段或成交
    cross_day: bool


@dataclass
class OppPath:
    """单个商机跨多轮的完整漏斗路径。"""

    opportunity_id: str
    cohort_period: str
    valid_event_ids: list[str]
    visits: list[Visit]
    rounds: int  # 经历轮次（重新开启会增加轮次，但仍是同一个队列成员）
    churned_ever: bool  # 历史上是否流失过——重开不会抹去这一经历
    won_ever: bool
    open_at_cutoff: bool  # 截止点是否仍有未结束阶段（观察期不足，不能当最终流失）


# ---------------------------------------------------------------------------
# 时间与事件标准化
# ---------------------------------------------------------------------------

def parse_dt(value: Any) -> datetime:
    """解析事件时间；支持 ISO8601 字符串与 datetime，统一为朴素 UTC。

    带时区的时间一律换算成 UTC 再去掉时区信息；朴素时间视为已经是 UTC，
    保证接收顺序颠倒、跨时区的事件仍可按同一时间轴比较。
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def period_of(dt: datetime) -> str:
    """入组周期按自然月归集（YYYY-MM）。"""
    return f"{dt.year:04d}-{dt.month:02d}"


def iso(dt: datetime | None) -> str | None:
    """对外输出统一 ISO8601（Z 结尾表示 UTC）。"""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_event(raw: dict[str, Any]) -> tuple[FunnelEvent | None, str | None]:
    """把接入层的原始字典标准化为 FunnelEvent；字段不合法时返回原因。"""
    event_id = str(raw.get("事件编号") or raw.get("event_id") or "").strip()
    opp_id = str(raw.get("商机编号") or raw.get("opportunity_id") or "").strip()
    evt_type = str(raw.get("事件类型") or raw.get("event_type") or "").strip()
    if not event_id:
        return None, "缺少事件编号"
    if not opp_id:
        return None, "缺少商机编号"
    if evt_type not in EVENT_TYPES:
        return None, REASON_UNKNOWN_TYPE
    stage_raw = raw.get("阶段") or raw.get("stage")
    stage = str(stage_raw).strip() if stage_raw not in (None, "") else None
    if evt_type in {EVT_ENTER, EVT_EXIT} and stage not in FUNNEL_STAGES:
        return None, REASON_UNKNOWN_STAGE
    if stage is not None and stage not in FUNNEL_STAGES:
        return None, REASON_UNKNOWN_STAGE
    try:
        occurred = parse_dt(raw.get("发生时间") or raw.get("occurred_at"))
    except (ValueError, TypeError):
        return None, "发生时间无法解析"
    received_raw = raw.get("接收时间") or raw.get("received_at")
    try:
        received = parse_dt(received_raw) if received_raw not in (None, "") else occurred
    except (ValueError, TypeError):
        received = occurred
    return (
        FunnelEvent(
            event_id=event_id,
            opportunity_id=opp_id,
            event_type=evt_type,
            stage=stage,
            occurred_at=occurred,
            received_at=received,
            raw=dict(raw),
        ),
        None,
    )


def _sort_key(ev: FunnelEvent) -> tuple[datetime, str, str]:
    """稳定排序键：发生时间优先，事件编号兜底——绝不依赖接入数组次序。"""
    return ev.occurred_at, ev.event_id, ev.event_type


# ---------------------------------------------------------------------------
# 单商机路径重建
# ---------------------------------------------------------------------------

def _close_visit(
    visit: Visit,
    exited_at: datetime,
    reason: str,
    *,
    converted: bool,
) -> None:
    visit.exited_at = exited_at
    visit.close_reason = reason
    visit.dwell_seconds = (exited_at - visit.entered_at).total_seconds()
    visit.cross_day = exited_at.date() != visit.entered_at.date()
    visit.converted = converted
    visit.censored = False


def reconstruct_opportunity(
    events: list[FunnelEvent],
    cutoff: datetime,
) -> tuple[OppPath | None, list[dict[str, Any]]]:
    """重建单个商机在给定观测截止点之前的完整路径。

    返回 ``(路径, 被排除事件)``。路径为 None 表示该商机没有任何有效进入事件，
    不具备队列成员资格。
    """
    excluded: list[dict[str, Any]] = []

    def reject(ev: FunnelEvent, reason: str) -> None:
        excluded.append({
            "事件编号": ev.event_id,
            "商机编号": ev.opportunity_id,
            "事件类型": ev.event_type,
            "阶段": ev.stage,
            "发生时间": iso(ev.occurred_at),
            "接收时间": iso(ev.received_at),
            "排除原因": reason,
        })

    # 1) 截止点过滤：晚于截止点的事件不参与本期统计，但保留在事件库里。
    in_window: list[FunnelEvent] = []
    for ev in events:
        if ev.occurred_at > cutoff:
            reject(ev, REASON_BEYOND_CUTOFF)
        else:
            in_window.append(ev)

    # 2) 稳定排序：发生时间 + 事件编号，同刻事件的识别不依赖数组次序。
    in_window.sort(key=_sort_key)

    # 3) 同刻业务去重：同商机、同刻、同类型、同阶段只保留事件编号最小的一条。
    deduped: list[FunnelEvent] = []
    seen_biz: set[tuple[datetime, str, str | None]] = set()
    for ev in in_window:
        biz_key = (ev.occurred_at, ev.event_type, ev.stage)
        if biz_key in seen_biz:
            reject(ev, REASON_BUSINESS_DUPLICATE)
            continue
        seen_biz.add(biz_key)
        deduped.append(ev)

    # 4) 同刻矛盾识别：同一时刻出现互斥事件，整组排除（不靠次序悄悄裁决）。
    conflict_ids: set[str] = set()
    by_ts: dict[datetime, list[FunnelEvent]] = {}
    for ev in deduped:
        by_ts.setdefault(ev.occurred_at, []).append(ev)
    for ts, group in by_ts.items():
        types = {e.event_type for e in group}
        stages = {e.stage for e in group if e.stage is not None}
        conflict = False
        if EVT_WIN in types and EVT_CHURN in types:  # 成交与流失互斥
            conflict = True
        if len(types & TERMINAL_TYPES) >= 1 and EVT_REOPEN in types:  # 终止与重开互斥
            conflict = True
        if EVT_EXIT in types and (EVT_WIN in types or EVT_CHURN in types):  # 退出与终止互斥
            conflict = True
        if len(stages) >= 2:  # 同一时刻进入/退出两个不同阶段，状态不唯一
            conflict = True
        if conflict:
            for ev in group:
                conflict_ids.add(ev.event_id)
                reject(ev, REASON_CONFLICT)
    valid = [ev for ev in deduped if ev.event_id not in conflict_ids]

    # 5) 状态机重放。
    visits: list[Visit] = []
    valid_ids: list[str] = []
    open_visit: Visit | None = None
    round_no = 1
    round_terminal_seen: dict[int, str] = {}  # round_no -> 成交/流失
    churned_ever = False
    won_ever = False

    def new_visit(ev: FunnelEvent) -> Visit:
        return Visit(
            round_no=round_no,
            stage=ev.stage or "",
            stage_order=FUNNEL_STAGES.index(ev.stage or ""),
            entered_at=ev.occurred_at,
            exited_at=None,
            close_reason=None,
            dwell_seconds=None,
            censored=True,
            converted=False,
            cross_day=False,
        )

    for ev in valid:
        if ev.event_type == EVT_ENTER:
            # 上一轮已终止、又没有重新开启：进入事件不允许偷偷开启新一轮。
            if round_no in round_terminal_seen:
                reject(ev, REASON_ILLEGAL_REOPEN)
                continue
            if open_visit is not None and open_visit.stage == ev.stage:
                # 同阶段已有进行中的周期：重复进入，只计一次。
                reject(ev, REASON_ENTER_WHILE_OPEN)
                continue
            if open_visit is not None:
                # 跳到其它阶段：没有退出事件也隐式关闭上一阶段，时长按观测到的
                # 两个时间点计算（不丢时长，也不把未知时长补零）。
                _close_visit(
                    open_visit, ev.occurred_at, "隐式关闭（下一次阶段进入）",
                    converted=ev.stage is not None
                    and FUNNEL_STAGES.index(ev.stage) > open_visit.stage_order,
                )
                visits.append(open_visit)
                open_visit = None
            open_visit = new_visit(ev)
            valid_ids.append(ev.event_id)

        elif ev.event_type == EVT_EXIT:
            if open_visit is None or open_visit.stage != ev.stage:
                reject(ev, REASON_ORPHAN_EXIT)  # 缺少匹配的进入事件
                continue
            # 正常退出即视为有效转化（业务上退出阶段意味着流转到下一环节）。
            _close_visit(open_visit, ev.occurred_at, "正常退出", converted=True)
            visits.append(open_visit)
            open_visit = None
            valid_ids.append(ev.event_id)

        elif ev.event_type in TERMINAL_TYPES:
            if round_no in round_terminal_seen:
                # 重复成交/重复流失：上一轮结果保留，不重复计数。
                reject(ev, REASON_DUP_TERMINAL)
                continue
            if open_visit is None:
                reject(ev, REASON_ORPHAN_TERMINAL)  # 没有进行中的阶段可终止
                continue
            if ev.event_type == EVT_WIN:
                # 成交：关闭当前阶段并记为有效转化；成交本身是漏斗终点。
                _close_visit(open_visit, ev.occurred_at, "成交", converted=True)
                won_ever = True
            else:
                # 流失：关闭当前阶段，不算转化；流失经历必须保留。
                _close_visit(open_visit, ev.occurred_at, "流失", converted=False)
                churned_ever = True
            visits.append(open_visit)
            open_visit = None
            round_terminal_seen[round_no] = ev.event_type
            valid_ids.append(ev.event_id)

        elif ev.event_type == EVT_REOPEN:
            # 只有“流失”之后允许重开；重开的是同一商机，绝不生成新客户。
            if round_terminal_seen.get(round_no) != EVT_CHURN:
                reject(ev, REASON_ILLEGAL_REOPEN)
                continue
            if open_visit is not None:  # 理论上不会出现，防御性处理
                reject(ev, REASON_ILLEGAL_REOPEN)
                continue
            round_no += 1  # 开启新一轮，但商机身份与队列归属不变
            valid_ids.append(ev.event_id)

    # 6) 截止点仍未结束的阶段：右删失。时长未知（None），不能补零、不能当流失。
    open_at_cutoff = open_visit is not None
    if open_visit is not None:
        open_visit.censored = True
        open_visit.dwell_seconds = None
        visits.append(open_visit)

    if not visits:
        return None, excluded

    cohort_period = period_of(visits[0].entered_at)
    return (
        OppPath(
            opportunity_id=events[0].opportunity_id,
            cohort_period=cohort_period,
            valid_event_ids=valid_ids,
            visits=visits,
            rounds=round_no,
            churned_ever=churned_ever,
            won_ever=won_ever,
            open_at_cutoff=open_at_cutoff,
        ),
        excluded,
    )


# ---------------------------------------------------------------------------
# 队列重建
# ---------------------------------------------------------------------------

def _deltas(seconds_list: Iterable[float]) -> dict[str, Any]:
    values = list(seconds_list)
    if not values:
        return {"样本量": 0, "平均停留秒": None, "最短停留秒": None, "最长停留秒": None}
    return {
        "样本量": len(values),
        "平均停留秒": round(sum(values) / len(values), 2),
        "最短停留秒": round(min(values), 2),
        "最长停留秒": round(max(values), 2),
    }


def build_cohort(
    cohort_period: str,
    cutoff: datetime,
    events_by_opp: dict[str, list[FunnelEvent]],
) -> dict[str, Any]:
    """对单个入组周期重建漏斗转化分析数据集。

    每个周期独立计算自己的成员集合：即使本周期缺少阶段事件，也只返回空成员，
    绝不复用其它周期的成员。
    """
    members: list[dict[str, Any]] = []
    paths: dict[str, OppPath] = {}
    excluded_all: list[dict[str, Any]] = []
    beyond_cutoff: list[dict[str, Any]] = []

    for opp_id in sorted(events_by_opp):
        path, excluded = reconstruct_opportunity(events_by_opp[opp_id], cutoff)
        for item in excluded:
            if item["排除原因"] == REASON_BEYOND_CUTOFF:
                beyond_cutoff.append(item)
            else:
                excluded_all.append(item)
        if path is None or path.cohort_period != cohort_period:
            # 成员资格：首条有效进入事件落在本周期。跨周期事件只影响路径细节，
            # 不会把商机搬到别的队列，也不会借用别的队列成员。
            continue
        paths[opp_id] = path
        members.append({
            "商机编号": opp_id,
            "入组周期": path.cohort_period,
            "经历轮次": path.rounds,
            "阶段周期数": len(path.visits),
            "历史流失": path.churned_ever,
            "历史成交": path.won_ever,
            "截止点未结束": path.open_at_cutoff,
            "有效事件编号": list(path.valid_event_ids),
        })

    # 分阶段指标：按“阶段周期（Visit）”统计，同一商机多次进入同一阶段分别计数。
    stage_metrics: list[dict[str, Any]] = []
    for idx, stage in enumerate(FUNNEL_STAGES):
        stage_visits = [v for p in paths.values() for v in p.visits if v.stage == stage]
        opp_entered = {p.opportunity_id for p in paths.values()
                       for v in p.visits if v.stage == stage}
        converted = [v for v in stage_visits if v.converted]
        censored = [v for v in stage_visits if v.censored]
        churned = [v for v in stage_visits if v.close_reason == "流失"]
        finished = [v for v in stage_visits if not v.censored]
        dwell = _deltas(v.dwell_seconds for v in finished if v.dwell_seconds is not None)
        stage_metrics.append({
            "阶段": stage,
            "漏斗层级": idx + 1,
            "商机数（去重成员）": len(opp_entered),
            "阶段周期数（含多轮）": len(stage_visits),
            "有效转化数": len(converted),
            "转化率": round(len(converted) / len(stage_visits), 4) if stage_visits else None,
            "流失周期数": len(churned),
            "未结束周期数（右删失）": len(censored),
            "停留时长": dwell,
        })

    total_members = len(members)
    won_members = sum(1 for p in paths.values() if p.won_ever)
    churn_members = sum(1 for p in paths.values() if p.churned_ever)
    reopen_members = sum(1 for p in paths.values() if p.rounds > 1)
    censored_members = sum(1 for p in paths.values() if p.open_at_cutoff)
    total_visits = sum(len(p.visits) for p in paths.values())

    # 队列总人数与路径明细的对账块：两侧必须能相互核验。
    reconcile = {
        "队列总人数": total_members,
        "成交商机数": won_members,
        "历史流失商机数（重开仍计入）": churn_members,
        "重开多轮商机数": reopen_members,
        "观察期不足商机数（右删失，未当最终流失）": censored_members,
        "阶段周期总数": total_visits,
        "成员明细人数": len(members),
        "分阶段商机数之和": sum(m["商机数（去重成员）"] for m in stage_metrics),
        "分阶段周期数之和": sum(m["阶段周期数（含多轮）"] for m in stage_metrics),
        "被排除事件数": len(excluded_all),
        "截止点之外事件数": len(beyond_cutoff),
    }

    return {
        "入组周期": cohort_period,
        "观测截止时间": iso(cutoff),
        "有效样本量": total_members,
        "成员": members,
        "分阶段指标": stage_metrics,
        "路径明细": {opp: _serialize_path(path) for opp, path in sorted(paths.items())},
        "对账": reconcile,
        "被排除事件": sorted(excluded_all, key=lambda x: (x["发生时间"] or "", x["事件编号"])),
        "截止点之外事件": sorted(beyond_cutoff, key=lambda x: (x["发生时间"] or "", x["事件编号"])),
    }


def _serialize_path(path: OppPath) -> dict[str, Any]:
    return {
        "商机编号": path.opportunity_id,
        "入组周期": path.cohort_period,
        "经历轮次": path.rounds,
        "历史流失": path.churned_ever,
        "历史成交": path.won_ever,
        "截止点未结束": path.open_at_cutoff,
        "阶段周期": [
            {
                "轮次": v.round_no,
                "阶段": v.stage,
                "进入时间": iso(v.entered_at),
                "退出时间": iso(v.exited_at),
                "关闭方式": v.close_reason,
                "停留秒": v.dwell_seconds,
                "跨日": v.cross_day,
                "右删失": v.censored,
                "有效转化": v.converted,
            }
            for v in path.visits
        ],
        "有效事件编号": list(path.valid_event_ids),
    }
