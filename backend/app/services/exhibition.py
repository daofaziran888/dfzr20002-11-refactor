"""会展布展协作引擎：跨模块面积/功率配额校验、DAG拓扑推进、变更传播回退、
交场门禁、冲突检测和执行历史重建。

所有领域规则集中在这里，每个模块 service 通过本引擎完成跨模块校验和联动。
"""
from __future__ import annotations

import copy
import threading
import time
from collections import deque
from typing import Any

from app.store import store


# ---------- 面积配额 ----------

def check_venue_capacity(vendor_id: int) -> dict[str, Any]:
    """校验场馆内所有展位面积之和是否超过场馆可用面积。"""
    venue = store.find("venue", vendor_id)
    if venue is None:
        return {"ok": False, "reason": f"场馆 {vendor_id} 不存在", "quota": {}}

    try:
        venue_area = float(str(venue.get("可用面积", "0")).replace("平米", "").replace("㎡", "").strip())
    except (ValueError, AttributeError):
        return {"ok": False, "reason": "场馆可用面积字段无法解析", "quota": {}}

    all_booths = store.rows("booth")
    booth_areas: list[dict[str, Any]] = []
    total_booth_area = 0.0
    for booth in all_booths:
        booth_venue = str(booth.get("所属区域", ""))
        if booth_venue == str(venue.get("场馆名称", "")):
            try:
                area_text = str(booth.get("面积尺寸", "0")).replace("平米", "").replace("㎡", "").strip()
                area = float(area_text)
            except (ValueError, AttributeError):
                area = 0.0
            total_booth_area += area
            booth_areas.append({
                "booth_no": booth.get("展位编号", ""),
                "area": area,
                "status": booth.get("status", ""),
            })

    ok = total_booth_area <= venue_area
    return {
        "ok": ok,
        "reason": "" if ok else f"展位总面积 {total_booth_area}㎡ 超过场馆可用面积 {venue_area}㎡",
        "quota": {
            "venue_area": venue_area,
            "total_booth_area": total_booth_area,
            "remaining": venue_area - total_booth_area,
            "booths": booth_areas,
        },
    }


# ---------- 功率配额 ----------

def check_electrical_quota(booth_no: str) -> dict[str, Any]:
    """校验展位用电回路额定功率之和是否超过展位分配的设计负荷。"""
    booth = store.find_booth(booth_no)
    if booth is None:
        return {"ok": False, "reason": f"展位 {booth_no} 不存在", "quota": {}}

    designs = store.rows("design")
    design_load = 0.0
    for design in designs:
        d_booth = str(design.get("备注说明", ""))
        if d_booth == booth_no:
            try:
                design_load = float(str(design.get("用电负荷", "0")).replace("kW", "").strip())
            except (ValueError, AttributeError):
                design_load = 0.0
            break

    circuits = store.booth_electrical(booth_no)
    total_power = 0.0
    circuit_details: list[dict[str, Any]] = []
    for circuit in circuits:
        try:
            power = float(str(circuit.get("额定功率", "0")).replace("kW", "").replace("W", "").strip())
        except (ValueError, AttributeError):
            power = 0.0
        total_power += power
        circuit_details.append({
            "circuit_id": circuit.get("回路编号", ""),
            "power": power,
            "status": circuit.get("status", ""),
        })

    ok = True
    reason = ""
    if design_load > 0 and total_power > design_load:
        ok = False
        reason = f"用电回路总功率 {total_power}kW 超过展位 {booth_no} 的设计负荷 {design_load}kW"

    return {
        "ok": ok,
        "reason": reason,
        "quota": {
            "booth_no": booth_no,
            "design_load": design_load,
            "total_circuit_power": total_power,
            "over_draw": total_power - design_load if total_power > design_load else 0,
            "circuits": circuit_details,
        },
    }


# ---------- DAG 拓扑推进 ----------

def detect_cycle(nodes: list[dict[str, Any]]) -> list[str] | None:
    """检测前置节点依赖图中是否存在环，有环返回环上节点名称列表。"""
    name_to_id: dict[str, int] = {}
    for node in nodes:
        name = str(node.get("节点名称", ""))
        if name:
            name_to_id[name] = int(node.get("id", 0))

    graph: dict[str, list[str]] = {}
    for node in nodes:
        name = str(node.get("节点名称", ""))
        if not name:
            continue
        pred_text = str(node.get("前置节点", "")).strip()
        preds = [p.strip() for p in pred_text.split(",") if p.strip()]
        graph[name] = preds

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in graph}
    parent: dict[str, str | None] = {name: None for name in graph}

    def dfs(u: str) -> bool:
        color[u] = GRAY
        for v in graph.get(u, []):
            if v not in color:
                color[v] = WHITE
                parent[v] = u
                if dfs(v):
                    return True
            elif color.get(v) == GRAY:
                return True
        color[u] = BLACK
        return False

    for node_name in graph:
        if color.get(node_name) == WHITE:
            if dfs(node_name):
                # Find cycle by traversing back
                return [node_name]
    return None


def build_dag_order(nodes: list[dict[str, Any]]) -> list[str]:
    """返回拓扑排序后的节点名称列表。"""
    name_to_node: dict[str, dict[str, Any]] = {}
    for node in nodes:
        name = str(node.get("节点名称", ""))
        if name:
            name_to_node[name] = node

    pred_counts: dict[str, int] = {}
    successors: dict[str, list[str]] = {}
    for node in nodes:
        name = str(node.get("节点名称", ""))
        if not name:
            continue
        pred_text = str(node.get("前置节点", "")).strip()
        preds = [p.strip() for p in pred_text.split(",") if p.strip()]
        pred_counts[name] = len(preds)
        for pred in preds:
            successors.setdefault(pred, []).append(name)

    queue: deque[str] = deque()
    for name in name_to_node:
        if pred_counts.get(name, 0) == 0:
            queue.append(name)

    result: list[str] = []
    while queue:
        current = queue.popleft()
        result.append(current)
        for succ in successors.get(current, []):
            pred_counts[succ] -= 1
            if pred_counts[succ] == 0:
                queue.append(succ)

    return result


def validate_dag_advance(node_id: int) -> dict[str, Any]:
    """检查进度节点是否可以推进（所有前置节点必须已完成）。"""
    node = store.find("construction", node_id)
    if node is None:
        return {"ok": False, "reason": f"进度节点 {node_id} 不存在"}

    all_nodes = store.rows("construction")
    dag_order = build_dag_order(all_nodes)

    node_name = str(node.get("节点名称", ""))
    pred_text = str(node.get("前置节点", "")).strip()
    preds = [p.strip() for p in pred_text.split(",") if p.strip()]

    # 检测环
    cycle = detect_cycle(all_nodes)
    if cycle:
        return {"ok": False, "reason": f"前置依赖存在环：{cycle}", "cycle": cycle}

    # 检查每个前置节点是否已确认
    unfinished_preds: list[str] = []
    for pred_name in preds:
        pred_node = None
        for n in all_nodes:
            if str(n.get("节点名称", "")) == pred_name:
                pred_node = n
                break
        if pred_node and pred_node.get("status") != "已确认":
            unfinished_preds.append(pred_name)

    if unfinished_preds:
        return {
            "ok": False,
            "reason": f"前置节点未完成：{'、'.join(unfinished_preds)}，不能跳过前置节点",
            "unfinished_predecessors": unfinished_preds,
        }

    return {
        "ok": True,
        "reason": "前置条件已满足",
        "node": node_name,
        "predecessors": preds,
        "dag_order": dag_order,
    }


def topological_advance(node_id: int) -> dict[str, Any]:
    """推进进度节点（含 DAG 前置校验）。"""
    validation = validate_dag_advance(node_id)
    if not validation["ok"]:
        return validation

    node = store.find("construction", node_id)
    before_state = copy.deepcopy(node)
    node["status"] = "已确认"
    node["pending"] = False
    node["完成日期"] = validation.get("timestamp", "")

    store.event_log.record(
        module="construction",
        entry_id=node_id,
        action="拓扑推进",
        before=before_state,
        after=copy.deepcopy(node),
        extra={"node_name": node.get("节点名称", ""), "dag_order": validation.get("dag_order", [])},
    )
    return {"ok": True, "reason": f"节点 {node.get('节点名称', '')} 已推进", "node": node, "validation": validation}


# ---------- 安全巡检门禁 ----------

def check_safety_gate(booth_no: str) -> dict[str, Any]:
    """检查展位安全巡检是否全部通过（所有巡检记录状态为已确认）。"""
    inspections = store.booth_safety(booth_no)
    failed: list[dict[str, Any]] = []
    for insp in inspections:
        if insp.get("status") != "已确认":
            failed.append({
                "id": insp.get("id"),
                "point": insp.get("检查点位", ""),
                "level": insp.get("隐患等级", ""),
                "status": insp.get("status", ""),
            })

    return {
        "ok": len(failed) == 0,
        "reason": "" if len(failed) == 0 else f"展位 {booth_no} 有 {len(failed)} 条巡检未通过",
        "total_inspections": len(inspections),
        "failed_inspections": failed,
    }


# ---------- 验收签收门禁 ----------

def validate_checklist_signoff(checklist_id: int) -> dict[str, Any]:
    """验收签收前检查：安全巡检未通过时对应验收项不可签收。"""
    item = store.find("checklist", checklist_id)
    if item is None:
        return {"ok": False, "reason": f"验收项 {checklist_id} 不存在"}

    booth_no = str(item.get("负责班组", ""))
    safety_result = check_safety_gate(booth_no)
    if not safety_result["ok"]:
        return {
            "ok": False,
            "reason": f"展位 {booth_no} 安全巡检未通过，验收项「{item.get('验收条目', '')}」不可签收",
            "safety_check": safety_result,
        }

    return {"ok": True, "reason": "巡检已通过，可以签收", "item": item, "safety_check": safety_result}


# ---------- 交场门禁 ----------

def validate_handover(handover_id: int) -> dict[str, Any]:
    """交场确认必须消费验收清单的完整结论，未签收的验收项导致交场被拒。"""
    handover = store.find("handover", handover_id)
    if handover is None:
        return {"ok": False, "reason": f"交场单 {handover_id} 不存在"}

    booth_no = str(handover.get("遗留事项", ""))
    checklist_items = store.booth_checklist(booth_no)
    unsigned: list[dict[str, Any]] = []
    for item in checklist_items:
        if item.get("status") != "已确认":
            unsigned.append({
                "id": item.get("id"),
                "item": item.get("验收条目", ""),
                "status": item.get("status", ""),
            })

    if unsigned:
        return {
            "ok": False,
            "reason": f"展位 {booth_no} 有 {len(unsigned)} 条验收项未签收，交场被拒绝",
            "unsigned_items": unsigned,
            "checklist_total": len(checklist_items),
        }

    return {
        "ok": True,
        "reason": "验收清单全部签收，可以交场",
        "handover": handover,
        "checklist_total": len(checklist_items),
    }


def do_handover(handover_id: int) -> dict[str, Any]:
    """执行交场确认（含验收门禁校验）。"""
    validation = validate_handover(handover_id)
    if not validation["ok"]:
        return validation

    handover = store.find("handover", handover_id)
    before_state = copy.deepcopy(handover)
    handover["status"] = "已确认"
    handover["pending"] = False

    booth_no = str(handover.get("遗留事项", ""))
    store.event_log.record(
        module="handover",
        entry_id=handover_id,
        action="完成交场",
        before=before_state,
        after=copy.deepcopy(handover),
        extra={"booth_no": booth_no, "checklist_validated": True,
                "checklist_total": validation.get("checklist_total", 0)},
    )
    return {"ok": True, "reason": "交场完成", "handover": handover, "validation": validation}


# ---------- 变更签发受理与回退 ----------

# 工作台（/api/change_order/{id}/actions）和集成入口（/api/exhibition/change/{id}/issue）
# 共用同一把签发锁和同一份受理回执，避免两处同时回退同一个完工节点。
_CHANGE_ISSUE_LOCK = threading.RLock()


def _change_booths(change: dict[str, Any]) -> list[str]:
    booths = [b.strip() for b in str(change.get("影响展位", "")).split(",")]
    return [b for b in booths if b]


def change_receipt_no(change: dict[str, Any]) -> str:
    """受理编号：优先使用变更单登记编号，缺省时按变更单 id 生成。"""
    registered = str(change.get("登记编号", "")).strip()
    if registered:
        return registered
    return f"CHAN-{int(change.get('id', 0)):04d}"


def _change_fingerprint(change: dict[str, Any]) -> str:
    return "|".join(
        str(change.get(field, "")).strip()
        for field in ("变更内容", "影响展位", "施工班组")
    )


def _target_matches(module: str, row: dict[str, Any], booths: list[str]) -> bool:
    if module == "construction":
        node_booth = str(row.get("备注说明", ""))
        return any(booth in node_booth for booth in booths)
    return str(row.get("负责班组", "")) in booths


def _target_label(module: str, row: dict[str, Any]) -> str:
    if module == "construction":
        return str(row.get("节点名称", ""))
    return str(row.get("验收条目", ""))


def _target_detail(module: str, row_id: int, label: str) -> dict[str, Any]:
    """保持既有输出口径：进度节点用 name，验收项用 item。"""
    return {"id": row_id, "name" if module == "construction" else "item": label}


def _capture_change_targets(
    module: str, booths: list[str],
) -> tuple[list[int], list[dict[str, Any]]]:
    """首次受理时固定回退目标，后续补办/重发只处理这份目标清单。

    这样签发之后才完工的施工行、才重签的验收项不会被旧单再次回退。
    """
    target_ids: list[int] = []
    details: list[dict[str, Any]] = []
    for row in store.rows(module):
        if row.get("status") != "已确认":
            continue
        if not _target_matches(module, row, booths):
            continue
        row_id = int(row.get("id", 0))
        if row_id in target_ids:
            continue
        target_ids.append(row_id)
        details.append(_target_detail(module, row_id, _target_label(module, row)))
    return target_ids, details


def _reconfirmed_after(module: str, entry_id: int, since: float) -> bool:
    """判断目标行在本次受理之后是否已被现场重新确认（重做/重签）。"""
    for event in reversed(store.event_log.for_module(module, entry_id)):
        if event.get("timestamp", 0) <= since:
            break
        after = event.get("after") or {}
        if after.get("status") == "已确认":
            return True
    return False


def _apply_change_targets(
    module: str, receipt: dict[str, Any], since: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按固定目标清单执行回退，只处理尚未办理过的目标。"""
    id_key = f"target_{module}_ids"
    processed_key = f"processed_{module}_ids"
    reverted_key = f"reverted_{module}_ids"
    detail_key = f"target_{module}"
    redo_key = "respected_redo_nodes" if module == "construction" else "respected_redo_checklists"

    details = {int(item["id"]): item for item in receipt.get(detail_key, [])}
    processed: set[int] = set(receipt.get(processed_key, []))
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for row_id in receipt.get(id_key, []):
        row_id = int(row_id)
        if row_id in processed:
            continue

        row = store.find(module, row_id)
        detail = dict(details.get(row_id) or _target_detail(module, row_id, ""))

        def mark_done() -> None:
            processed.add(row_id)
            receipt[processed_key] = sorted(processed)
            store.save_change_receipt(receipt)

        if row is None or row.get("status") != "已确认":
            # 目标已不在完工态，视为已办理，不再重复回退。
            mark_done()
            continue

        if _reconfirmed_after(module, row_id, since):
            # 现场已在本次受理后重做/重签，保留新进展。
            receipt.setdefault(redo_key, []).append(detail)
            skipped.append(detail)
            mark_done()
            continue

        row["status"] = "待确认"
        row["pending"] = True
        row["abnormal"] = True
        receipt.setdefault(reverted_key, []).append(row_id)
        applied.append(detail)
        mark_done()

    return applied, skipped


def _snapshot_first_issuance(
    receipt: dict[str, Any], attempt: dict[str, Any],
) -> None:
    """固定首办受理的影响明细，后续补办/重发不改写这份口径。"""
    if receipt.get("first_issuance") is not None:
        return
    receipt["first_issuance"] = {
        "receipt_no": receipt["receipt_no"],
        "change_id": receipt["change_id"],
        "issued_at": receipt.get("created_at"),
        "complete": bool(attempt.get("complete")),
        "affected_booths": list(receipt.get("affected_booths", [])),
        "target_nodes": [
            {"id": int(d["id"]), "name": str(d.get("name", ""))}
            for d in receipt.get("target_construction", [])
        ],
        "target_checklists": [
            {"id": int(d["id"]), "item": str(d.get("item", ""))}
            for d in receipt.get("target_checklist", [])
        ],
        "affected_nodes": list(attempt.get("applied_nodes", [])),
        "affected_checklists": list(attempt.get("applied_checklists", [])),
        "reverted_nodes": len(attempt.get("applied_nodes", [])),
        "reverted_checklists": len(attempt.get("applied_checklists", [])),
    }


def propagate_change_order(
    change_id: int, receipt_no: str | None = None,
) -> dict[str, Any]:
    """签发变更指令：受影响展位的进度节点与验收项回退为待确认。

    工作台和集成入口共用本函数。签发结果按受理编号落一份回执：
    - 正常签发保持原有回退口径和返回结构；
    - 同一受理编号重复提交（补办/重发）只续办尚未办理的固定目标，
      不会把已回退后又重做的节点、已重签的验收项再撤一次；
    - 部分目标处理失败时如实返回不完整，回执保留已办理进度供原号补办。
    """
    with _CHANGE_ISSUE_LOCK:
        change = store.find("change_order", change_id)
        if change is None:
            return {"ok": False, "reason": f"变更单 {change_id} 不存在"}

        accepted_no = str(receipt_no or "").strip() or change_receipt_no(change)
        existing = store.find_change_receipt(accepted_no)
        if existing is not None and int(existing.get("change_id", 0)) != int(change_id):
            return {
                "ok": False,
                "reason": f"受理编号 {accepted_no} 已用于变更单 {existing.get('change_id')}，不能重复受理",
            }

        canonical = existing
        if canonical is None:
            # 同一变更换了一个受理编号提交（另一入口补发原单），仍归并到首办回执，
            # 不能当作全新受理再撤一遍已经重做的节点。
            canonical = store.change_receipt_for_change(change_id)
            if canonical is not None:
                store.alias_change_receipt(accepted_no, canonical)

        now = time.time()
        if canonical is None:
            receipt: dict[str, Any] = {
                "receipt_no": accepted_no,
                "change_id": change_id,
                "fingerprint": _change_fingerprint(change),
                "created_at": now,
                "updated_at": now,
                "status": "处理中",
                "complete": False,
                "attempts": [],
                "affected_booths": _change_booths(change),
                "target_construction_ids": None,
                "target_checklist_ids": None,
                "target_construction": [],
                "target_checklist": [],
                "processed_construction_ids": [],
                "processed_checklist_ids": [],
                "reverted_construction_ids": [],
                "reverted_checklist_ids": [],
                "respected_redo_nodes": [],
                "respected_redo_checklists": [],
                "retry_count": 0,
                "failed_stage": "",
                "last_error": "",
                "first_issuance": None,
            }
            attempt_kind = "首次签发"
            store.save_change_receipt(receipt)
        else:
            receipt = canonical
            # 同一变更已签发过：只有首办不完整时才叫补办，否则是重发。
            attempt_kind = "补办" if not receipt.get("complete") else "重发"
            receipt["last_receipt_no"] = accepted_no
            receipt["retry_count"] = int(receipt.get("retry_count", 0)) + 1
            receipt["updated_at"] = now
            receipt["fingerprint"] = _change_fingerprint(change)
            store.save_change_receipt(receipt)

        booth_list = list(receipt.get("affected_booths") or _change_booths(change))
        receipt["affected_booths"] = booth_list
        applied_nodes: list[dict[str, Any]] = []
        applied_checklists: list[dict[str, Any]] = []
        skipped_nodes: list[dict[str, Any]] = []
        skipped_checklists: list[dict[str, Any]] = []
        error = ""
        failed_stage = ""

        try:
            # 首次受理即一次性固定整份影响目标（进度 + 验收）：
            # 之后任一环节失败，补办都只续办未办理项，不会重新扩大影响面。
            if (receipt.get("target_construction_ids") is None
                    or receipt.get("target_checklist_ids") is None):
                if receipt.get("target_construction_ids") is None:
                    ids, details = _capture_change_targets("construction", booth_list)
                    receipt["target_construction_ids"] = ids
                    receipt["target_construction"] = details
                if receipt.get("target_checklist_ids") is None:
                    ids, details = _capture_change_targets("checklist", booth_list)
                    receipt["target_checklist_ids"] = ids
                    receipt["target_checklist"] = details
                store.save_change_receipt(receipt)

            failed_stage = "construction"
            applied_nodes, skipped_nodes = _apply_change_targets(
                "construction", receipt, receipt["created_at"])

            failed_stage = "checklist"
            applied_checklists, skipped_checklists = _apply_change_targets(
                "checklist", receipt, receipt["created_at"])
        except Exception as exc:  # 单项处理失败时保留已办理进度，支持原号补办
            error = str(exc) or exc.__class__.__name__
            receipt["failed_stage"] = failed_stage
            receipt["last_error"] = error

        complete = not error
        receipt["complete"] = complete
        receipt["status"] = "已签发" if complete else "处理中"
        receipt["updated_at"] = time.time()
        receipt["attempts"].append({
            "at": receipt["updated_at"],
            "kind": attempt_kind,
            "complete": complete,
            "failed_stage": failed_stage if error else "",
            "error": error,
            "applied_nodes": list(applied_nodes),
            "applied_checklists": list(applied_checklists),
            "respected_redo_nodes": list(skipped_nodes),
            "respected_redo_checklists": list(skipped_checklists),
        })
        _snapshot_first_issuance(receipt, receipt["attempts"][-1])
        store.save_change_receipt(receipt)

        before_state = copy.deepcopy(change)
        change["status"] = "已确认"
        change["pending"] = False

        if complete:
            reason = (
                f"变更已签发，{len(applied_nodes)} 个进度节点和 "
                f"{len(applied_checklists)} 条验收项已回退"
            )
        else:
            reason = f"变更已受理，{failed_stage} 回退未完成：{error}"

        store.event_log.record(
            module="change_order",
            entry_id=change_id,
            action="签发变更-传播回退",
            before=before_state,
            after=copy.deepcopy(change),
            extra={
                "affected_booths": booth_list,
                "reverted_nodes": len(applied_nodes),
                "reverted_checklists": len(applied_checklists),
                "receipt_no": accepted_no,
                "receipt_kind": attempt_kind,
                "receipt_complete": complete,
                "respected_redo_nodes": len(skipped_nodes),
                "respected_redo_checklists": len(skipped_checklists),
                "error": error,
            },
        )
        store.rebuild_indices()

        result = {
            "ok": complete,
            "reason": reason,
            "affected_booths": booth_list,
            "affected_nodes": applied_nodes,
            "affected_checklists": applied_checklists,
            "receipt_no": accepted_no,
            "receipt_kind": attempt_kind,
            "replayed": attempt_kind != "首次签发",
            "receipt": copy.deepcopy(receipt),
            "first_issuance": copy.deepcopy(receipt.get("first_issuance")),
        }
        if not complete:
            result["failed_stage"] = failed_stage
            result["error"] = error
        return result


def get_change_receipt(
    receipt_no: str | None = None, change_id: int | None = None,
) -> dict[str, Any]:
    """按受理编号（或变更单 id）反查签发回执与首办影响明细。"""
    receipt: dict[str, Any] | None = None
    if receipt_no:
        receipt = store.find_change_receipt(receipt_no)
    elif change_id is not None:
        receipt = store.change_receipt_for_change(int(change_id))
    if receipt is None:
        label = receipt_no or change_id
        return {"ok": False, "reason": f"受理编号 {label} 没有签发回执"}
    return {
        "ok": True,
        "receipt_no": receipt["receipt_no"],
        "queried_receipt_no": receipt_no or receipt["receipt_no"],
        "aliases": store.change_receipt_aliases(receipt),
        "receipt": copy.deepcopy(receipt),
        "first_issuance": copy.deepcopy(receipt.get("first_issuance")),
    }


def change_issuance_receipt(
    receipt_no: str | None = None, change_id: int | None = None,
) -> dict[str, Any]:
    """`get_change_receipt` 的别名，供两处入口共用。"""
    return get_change_receipt(receipt_no=receipt_no, change_id=change_id)


# ---------- 展品运输排期冲突检测 ----------

def detect_schedule_conflicts() -> list[dict[str, Any]]:
    """检测展品运输预计到达晚于布展起始时的排期冲突。"""
    transports = store.rows("transport")
    schedules = store.rows("schedule")
    conflicts: list[dict[str, Any]] = []

    for transport in transports:
        if transport.get("status") != "已确认":
            continue
        arrival = str(transport.get("预计到达", ""))
        for schedule in schedules:
            if schedule.get("status") != "已确认":
                continue
            setup_start = str(schedule.get("布展起始", ""))
            if arrival and setup_start and arrival > setup_start:
                conflicts.append({
                    "transport_id": transport.get("id"),
                    "exhibit": transport.get("展品名称", ""),
                    "arrival": arrival,
                    "schedule_id": schedule.get("id"),
                    "event": schedule.get("活动名称", ""),
                    "setup_start": setup_start,
                    "conflict": f"展品 {transport.get('展品名称', '')} 预计到达 {arrival} 晚于布展起始 {setup_start}",
                })

    return conflicts


# ---------- 执行历史重建 ----------

def booth_execution_history(booth_no: str) -> dict[str, Any]:
    """从事件日志中重建展位的完整执行历史。"""
    booth = store.find_booth(booth_no)
    events = store.event_log.for_booth(booth_no)

    # 也可能包含 booth 自身的事件
    for event in store.event_log.for_module("booth"):
        after = event.get("after") or {}
        if after.get("展位编号") == booth_no:
            if event not in events:
                events.append(event)

    events.sort(key=lambda e: e["timestamp"])

    # 汇总当前状态
    design = None
    for d in store.rows("design"):
        if str(d.get("备注说明", "")) == booth_no:
            design = d
            break

    return {
        "booth_no": booth_no,
        "booth": booth,
        "design": design,
        "events": events,
        "event_count": len(events),
        "current_safety": check_safety_gate(booth_no),
        "current_electrical": check_electrical_quota(booth_no),
        "schedule_conflicts": [c for c in detect_schedule_conflicts()
                                if booth_no in str(c)],
    }


# ---------- 幂等请求识别 ----------

def ensure_idempotent(module: str, entry_id: int, action: str) -> dict[str, Any]:
    """检查请求是否已经处理过（基于事件日志），返回已处理结果或 None。"""
    for event in reversed(store.event_log.for_module(module, entry_id)):
        if event["action"] == action:
            after = event.get("after")
            if after:
                return {"idempotent": True, "cached_result": after, "event_id": event["id"]}
    return {"idempotent": False}


# ---------- 配额综合视图 ----------

# ---------- 跨模块关联查询 ----------

def booth_venue_info(booth_no: str) -> dict[str, Any]:
    """查找展位所属的场馆信息（展位→场馆）。"""
    booth = store.find_booth(booth_no)
    if booth is None:
        return {"ok": False, "reason": f"展位 {booth_no} 不存在"}
    venue_name = str(booth.get("所属区域", ""))
    for venue in store.rows("venue"):
        if str(venue.get("场馆名称", "")) == venue_name:
            return {"ok": True, "booth": booth, "venue": venue, "venue_name": venue_name}
    return {"ok": False, "reason": f"展位 {booth_no} 的场馆 {venue_name} 不存在", "booth": booth}


def exhibitor_booth_info(exhibitor_id: int) -> dict[str, Any]:
    """查找参展商关联的展位信息（参展商→展位）。"""
    exhibitor = store.find("exhibitor", exhibitor_id)
    if exhibitor is None:
        return {"ok": False, "reason": f"参展商 {exhibitor_id} 不存在"}
    remark = str(exhibitor.get("备注说明", ""))
    booth = store.find_booth(remark) if remark else None
    return {
        "ok": True,
        "exhibitor": exhibitor,
        "booth": booth,
        "booth_no": remark,
        "linked": booth is not None,
    }


def design_booth_info(design_id: int) -> dict[str, Any]:
    """查找设计方案对应的展位信息（设计→展位）。"""
    design = store.find("design", design_id)
    if design is None:
        return {"ok": False, "reason": f"设计方案 {design_id} 不存在"}
    remark = str(design.get("备注说明", ""))
    booth = store.find_booth(remark) if remark else None
    return {
        "ok": True,
        "design": design,
        "booth": booth,
        "booth_no": remark,
        "linked": booth is not None,
    }


def material_design_info(material_id: int) -> dict[str, Any]:
    """查找物料对应的设计方案信息（物料→设计）。"""
    material = store.find("material", material_id)
    if material is None:
        return {"ok": False, "reason": f"物料 {material_id} 不存在"}
    remark = str(material.get("备注说明", ""))
    designs = store.rows("design")
    matched_design = None
    for design in designs:
        if str(design.get("备注说明", "")) == remark:
            matched_design = design
            break
    booth = store.find_booth(remark) if remark else None
    return {
        "ok": True,
        "material": material,
        "design": matched_design,
        "booth": booth,
        "booth_no": remark,
        "linked": matched_design is not None,
    }


def incident_venue_info(incident_id: int) -> dict[str, Any]:
    """查找突发事件对应的场馆信息（事件→场馆）。"""
    incident = store.find("incident", incident_id)
    if incident is None:
        return {"ok": False, "reason": f"事件记录 {incident_id} 不存在"}
    location = str(incident.get("发生位置", ""))
    matched_venue = None
    for venue in store.rows("venue"):
        if str(venue.get("场馆名称", "")) in location or location in str(venue.get("场馆名称", "")):
            matched_venue = venue
            break
    booth_no = location.split("顶部")[0] if "顶部" in location else location
    booth = store.find_booth(booth_no) if booth_no else None
    if booth and not matched_venue:
        venue_name = str(booth.get("所属区域", ""))
        for venue in store.rows("venue"):
            if str(venue.get("场馆名称", "")) == venue_name:
                matched_venue = venue
                break
    return {
        "ok": True,
        "incident": incident,
        "venue": matched_venue,
        "booth": booth,
        "linked": matched_venue is not None or booth is not None,
    }


def handover_schedule_info(handover_id: int) -> dict[str, Any]:
    """查找交场确认对应的排期信息（交场→排期）。"""
    handover = store.find("handover", handover_id)
    if handover is None:
        return {"ok": False, "reason": f"交场单 {handover_id} 不存在"}
    schedules = store.rows("schedule")
    active_schedule = None
    for schedule in schedules:
        if schedule.get("status") == "已确认":
            active_schedule = schedule
            break
    booth_no = str(handover.get("遗留事项", ""))
    booth = store.find_booth(booth_no) if booth_no else None
    return {
        "ok": True,
        "handover": handover,
        "schedule": active_schedule,
        "booth": booth,
        "linked": active_schedule is not None,
    }


def cross_module_snapshot(booth_no: str) -> dict[str, Any]:
    """跨模块全局快照：查询展位的全链路关联数据。"""
    booth = store.find_booth(booth_no)
    if booth is None:
        return {"ok": False, "reason": f"展位 {booth_no} 不存在"}

    venue_name = str(booth.get("所属区域", ""))
    venue = None
    for v in store.rows("venue"):
        if str(v.get("场馆名称", "")) == venue_name:
            venue = v
            break

    exhibitors = [e for e in store.rows("exhibitor")
                  if str(e.get("备注说明", "")) == booth_no]
    designs = [d for d in store.rows("design")
               if str(d.get("备注说明", "")) == booth_no]
    materials = [m for m in store.rows("material")
                 if str(m.get("备注说明", "")) == booth_no]
    electricals = store.booth_electrical(booth_no)
    safeties = store.booth_safety(booth_no)
    lightings = [l for l in store.rows("lighting")
                 if booth_no in str(l.get("备注说明", ""))]
    audio_visuals = [av for av in store.rows("audio_visual")
                     if booth_no in str(av.get("备注说明", ""))]
    furnitures = [f for f in store.rows("furniture")
                  if booth_no in str(f.get("备注说明", ""))]
    constructions = [c for c in store.rows("construction")
                     if booth_no in str(c.get("备注说明", ""))]
    checklists = store.booth_checklist(booth_no)
    change_orders = [co for co in store.rows("change_order")
                     if booth_no in str(co.get("影响展位", ""))]
    handovers = store.booth_handover(booth_no)
    incidents = [inc for inc in store.rows("incident")
                 if booth_no in str(inc.get("发生位置", ""))]

    return {
        "ok": True,
        "booth_no": booth_no,
        "booth": booth,
        "venue": venue,
        "exhibitors": exhibitors,
        "designs": designs,
        "materials": materials,
        "electricals": electricals,
        "safeties": safeties,
        "lightings": lightings,
        "audio_visuals": audio_visuals,
        "furnitures": furnitures,
        "constructions": constructions,
        "checklists": checklists,
        "change_orders": change_orders,
        "handovers": handovers,
        "incidents": incidents,
        "module_count": sum(1 for v in [
            booth, venue, exhibitors, designs, materials, electricals,
            safeties, lightings, audio_visuals, furnitures, constructions,
            checklists, change_orders, handovers, incidents
        ] if v),
    }


# ---------- 配额综合视图 ----------

def quota_summary() -> dict[str, Any]:
    """全场面积配额与功率配额的综合结论。"""
    venues = store.rows("venue")
    booths = store.rows("booth")
    quota_results: list[dict[str, Any]] = []

    for venue in venues:
        if venue.get("status") != "已确认":
            continue
        v_result = check_venue_capacity(int(venue.get("id", 0)))
        quota_results.append({
            "venue": venue.get("场馆名称", ""),
            "venue_id": venue.get("id"),
            **v_result,
        })

    booth_power_results: list[dict[str, Any]] = []
    for booth in booths:
        if booth.get("status") != "已确认":
            continue
        booth_no = str(booth.get("展位编号", ""))
        if booth_no:
            p_result = check_electrical_quota(booth_no)
            if p_result.get("quota"):
                booth_power_results.append(p_result)

    schedule_conflicts = detect_schedule_conflicts()
    dag_cycle = detect_cycle(store.rows("construction"))

    return {
        "area_quotas": quota_results,
        "power_quotas": booth_power_results,
        "schedule_conflicts": schedule_conflicts,
        "schedule_conflict_count": len(schedule_conflicts),
        "dag_cycle": dag_cycle,
        "dag_ok": dag_cycle is None,
        "overall_ok": all(q.get("ok", True) for q in quota_results)
                       and all(p.get("ok", True) for p in booth_power_results)
                       and len(schedule_conflicts) == 0
                       and dag_cycle is None,
    }