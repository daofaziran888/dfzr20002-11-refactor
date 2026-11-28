"""模块服务基类：共享查询、登记、动作和统计骨架，业务差异留在各模块文件。

子类需要覆写 validate_entry / validate_action 实现领域校验，
也可覆写 create_entry / run_action 实现跨模块联动。
"""
from __future__ import annotations

import copy
from typing import Any

from app.store import store


class BaseModuleService:
    MODULE = ""
    ENTITY = ""
    OPERATION_TERM = ""
    LIST_FIELDS: list[str] = []
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER: list[str] = []
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []
    # 本模块引用的其他模块（用于 domain_snapshot 扩展）
    RELATED_MODULES: list[str] = []

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def validate_entry(self, values: dict[str, Any]) -> list[str]:
        """子类覆写：返回校验错误列表，空列表表示通过。"""
        return []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        validation_errors = self.validate_entry(values)
        if validation_errors:
            return None, validation_errors

        rows = store.rows(self.MODULE)
        entry = {"id": store.next_id(self.MODULE)}
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)

        store.event_log.record(
            module=self.MODULE,
            entry_id=entry["id"],
            action="登记",
            after=copy.deepcopy(entry),
        )
        store.rebuild_indices()
        return entry, []

    def validate_action(self, entry_id: int, action: str) -> tuple[bool, str]:
        """子类覆写：返回 (是否允许, 拒绝原因)。"""
        return True, ""

    def run_action(
        self, entry_id: int, action: str, receipt_no: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """执行状态动作；receipt_no 供需要受理编号的模块（如签发变更）使用。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        allowed, reason = self.validate_action(entry_id, action)
        if not allowed:
            return None, reason

        before_state = copy.deepcopy(entry)
        entry["status"] = target
        entry["pending"] = target != self.STATUS_ORDER[-1]
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.event_log.record(
            module=self.MODULE,
            entry_id=entry_id,
            action=action,
            before=before_state,
            after=copy.deepcopy(entry),
        )
        store.rebuild_indices()
        return entry, f"{self.ENTITY}已{action}"

    def domain_snapshot(self) -> dict[str, Any]:
        """返回本模块的状态统计与业务对象，供其他模块读取。"""
        rows = store.rows(self.MODULE)
        by_status = {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in self.STATUS_ORDER
        }
        result = {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}状态记录",
            "count": len(rows),
            "by_status": by_status,
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
        }
        # 合并关联模块快照
        for related in self.RELATED_MODULES:
            from app.services import service_for
            svc = service_for(related)
            if svc:
                result[f"related_{related}"] = svc.domain_snapshot()
        return result

    def related_rows(self, module: str) -> list[dict[str, Any]]:
        """读取关联模块数据。"""
        return store.rows(module)