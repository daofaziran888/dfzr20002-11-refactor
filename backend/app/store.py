"""内存数据仓库：带事件日志和跨模块索引，支持执行历史重建和配额校验。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import copy
import time
from typing import Any

from app.seed import SEED_ROWS


class EventLog:
    """记录所有跨模块有意义的业务事件，用于执行历史重建。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(
        self,
        module: str,
        entry_id: int,
        action: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": len(self.entries) + 1,
            "timestamp": time.time(),
            "module": module,
            "entry_id": entry_id,
            "action": action,
            "before": copy.deepcopy(before) if before else None,
            "after": copy.deepcopy(after) if after else None,
            "extra": extra or {},
        }
        self.entries.append(event)
        return event

    def for_module(self, module: str, entry_id: int | None = None) -> list[dict[str, Any]]:
        if entry_id is not None:
            return [e for e in self.entries if e["module"] == module and e["entry_id"] == entry_id]
        return [e for e in self.entries if e["module"] == module]

    def for_booth(self, booth_no: str) -> list[dict[str, Any]]:
        """反查与指定展位编号相关的全部事件（跨模块执行历史）。"""
        results: list[dict[str, Any]] = []
        for event in self.entries:
            extra = event.get("extra", {})
            if extra.get("booth_no") == booth_no:
                results.append(event)
        # Also search in booth_service related events
        for event in self.entries:
            if event["module"] == "booth":
                after = event.get("after") or {}
                if after.get("展位编号") == booth_no:
                    results.append(event)
        # deduplicate by id
        seen: set[int] = set()
        deduped: list[dict[str, Any]] = []
        for event in results:
            if event["id"] not in seen:
                seen.add(event["id"])
                deduped.append(event)
        return sorted(deduped, key=lambda e: e["timestamp"])

    def all_events(self) -> list[dict[str, Any]]:
        return list(self.entries)


class MemoryStore:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._id_counters: dict[str, int] = {}
        for name, rows in self._tables.items():
            if rows:
                self._id_counters[name] = max(int(row.get("id", 0)) for row in rows)
            else:
                self._id_counters[name] = 0
        self.event_log = EventLog()
        # 变更签发受理回执：受理编号 → 回执（工作台与集成入口共用）
        self._change_receipts: dict[str, dict[str, Any]] = {}
        # 索引：展位编号 → 展位记录
        self._booth_index: dict[str, dict[str, Any]] = {}
        for row in self._tables.get("booth", []):
            if row.get("展位编号"):
                self._booth_index[str(row["展位编号"])] = row
        # 展位 → 用电回路索引
        self._booth_electrical_index: dict[str, list[dict[str, Any]]] = {}
        for row in self._tables.get("electrical", []):
            booth_no = str(row.get("所属展位", ""))
            if booth_no:
                self._booth_electrical_index.setdefault(booth_no, []).append(row)
        # 展位 → 安全巡检索引
        self._booth_safety_index: dict[str, list[dict[str, Any]]] = {}
        for row in self._tables.get("safety", []):
            booth_no = str(row.get("检查点位", ""))
            if booth_no:
                self._booth_safety_index.setdefault(booth_no, []).append(row)
        # 展位 → 验收清单索引
        self._booth_checklist_index: dict[str, list[dict[str, Any]]] = {}
        for row in self._tables.get("checklist", []):
            booth_no = str(row.get("负责班组", ""))
            if booth_no:
                self._booth_checklist_index.setdefault(booth_no, []).append(row)
        # 展位 → 交场单索引
        self._booth_handover_index: dict[str, list[dict[str, Any]]] = {}
        for row in self._tables.get("handover", []):
            booth_no = str(row.get("遗留事项", ""))
            if booth_no:
                self._booth_handover_index.setdefault(booth_no, []).append(row)

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        if module not in self._tables:
            self._tables[module] = []
        return self._tables[module]

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        counter = self._id_counters.get(module, 0) + 1
        self._id_counters[module] = counter
        return counter

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows_list = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows_list),
                "pending": sum(1 for row in rows_list if row.get("pending")),
                "abnormal": sum(1 for row in rows_list if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---------- 变更签发受理回执 ----------

    def save_change_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """保存签发受理回执；回执以受理编号（默认为变更单登记编号）为键。"""
        self._change_receipts[str(receipt["receipt_no"])] = receipt
        return receipt

    def find_change_receipt(self, receipt_no: str) -> dict[str, Any] | None:
        return self._change_receipts.get(str(receipt_no))

    def change_receipt_aliases(self, receipt: dict[str, Any]) -> list[str]:
        """返回指向同一份回执的全部受理编号（含别名）。"""
        return sorted(
            no for no, stored in self._change_receipts.items() if stored is receipt
        )

    def change_receipt_for_change(
        self, change_id: int, fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """按变更单反查受理回执；同一变更只保留一份有效受理。"""
        seen: set[int] = set()
        for receipt in self._change_receipts.values():
            if id(receipt) in seen:
                continue
            seen.add(id(receipt))
            if int(receipt.get("change_id", 0)) != int(change_id):
                continue
            if fingerprint is not None and receipt.get("fingerprint") != fingerprint:
                continue
            return receipt
        return None

    def alias_change_receipt(
        self, receipt_no: str, receipt: dict[str, Any],
    ) -> None:
        """把同一变更的另一个受理编号别名到已有回执，保证原号可查。"""
        if receipt_no:
            self._change_receipts[str(receipt_no)] = receipt

    # ---------- 跨模块索引查询 ----------

    def find_booth(self, booth_no: str) -> dict[str, Any] | None:
        return self._booth_index.get(booth_no)

    def booth_electrical(self, booth_no: str) -> list[dict[str, Any]]:
        return self._booth_electrical_index.get(booth_no, [])

    def booth_safety(self, booth_no: str) -> list[dict[str, Any]]:
        return self._booth_safety_index.get(booth_no, [])

    def booth_checklist(self, booth_no: str) -> list[dict[str, Any]]:
        return self._booth_checklist_index.get(booth_no, [])

    def booth_handover(self, booth_no: str) -> list[dict[str, Any]]:
        return self._booth_handover_index.get(booth_no, [])

    def rebuild_indices(self) -> None:
        """重建所有跨模块索引（在数据变更后调用）。"""
        self._booth_index = {}
        for row in self._tables.get("booth", []):
            if row.get("展位编号"):
                self._booth_index[str(row["展位编号"])] = row
        self._booth_electrical_index = {}
        for row in self._tables.get("electrical", []):
            booth_no = str(row.get("所属展位", ""))
            if booth_no:
                self._booth_electrical_index.setdefault(booth_no, []).append(row)
        self._booth_safety_index = {}
        for row in self._tables.get("safety", []):
            booth_no = str(row.get("检查点位", ""))
            if booth_no:
                self._booth_safety_index.setdefault(booth_no, []).append(row)
        self._booth_checklist_index = {}
        for row in self._tables.get("checklist", []):
            booth_no = str(row.get("负责班组", ""))
            if booth_no:
                self._booth_checklist_index.setdefault(booth_no, []).append(row)
        self._booth_handover_index = {}
        for row in self._tables.get("handover", []):
            booth_no = str(row.get("遗留事项", ""))
            if booth_no:
                self._booth_handover_index.setdefault(booth_no, []).append(row)


store = MemoryStore()