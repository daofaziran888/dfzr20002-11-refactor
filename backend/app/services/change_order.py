"""变更指令领域服务：签发变更传播回退进度和验收项。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.services import exhibition


class ChangeOrderService(BaseModuleService):
    MODULE = "change_order"
    ENTITY = "变更单"
    OPERATION_TERM = "签发变更"
    LIST_FIELDS = ["变更内容", "影响展位", "施工班组", "签收时限", "登记编号", "负责人", "生效版本", "备注说明"]
    REQUIRED_FIELDS = ["变更内容", "影响展位", "施工班组"]
    STATUS_ORDER = ["草稿", "待确认", "已确认", "已撤回"]
    ACTION_RULES = {"签发变更": "已确认", "提交确认": "待确认", "撤回记录": "已撤回"}
    NEGATIVE_ACTIONS = []
    RELATED_MODULES = ["construction", "checklist", "booth"]

    def validate_action(self, entry_id: int, action: str) -> tuple[bool, str]:
        if action == "签发变更":
            change = store.find(self.MODULE, entry_id)
            if change is None:
                return False, "变更单不存在"
        return True, ""

    def run_action(
        self, entry_id: int, action: str, receipt_no: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if action == "签发变更":
            result = exhibition.propagate_change_order(entry_id, receipt_no=receipt_no)
            if not result["ok"]:
                return None, result["reason"]
            entry = store.find(self.MODULE, entry_id)
            return entry, result["reason"]
        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["issued_count"] = sum(1 for r in rows if r.get("status") == "已确认")
        return base