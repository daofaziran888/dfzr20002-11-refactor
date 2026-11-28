"""变更签发受理：工作台与集成入口共用回退/受理逻辑。

覆盖：
1. 正常签发保持既有回退口径与返回结构
2. 同一受理编号重复提交不再撤一次当次完工节点
3. 部分失败（进度已办、验收失败）后按原号补办
4. 补办期间现场重做/重签的新进展不被旧单回退
5. 补办完成后从另一入口重发原单，首办影响明细可查且桁架重做不再退
6. 后续才完工的施工行不被旧单回退
"""
import copy
import sys
import unittest

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import exhibition, service_for
from app.store import store


class ChangeIssuanceTestCase(unittest.TestCase):
    """每个用例前后对内存仓库做完整快照，避免影响其它用例。"""

    def setUp(self):
        self._tables_backup = {
            name: copy.deepcopy(rows) for name, rows in store._tables.items()}
        self._event_backup = len(store.event_log.entries)
        self._id_backup = dict(store._id_counters)
        store._change_receipts.clear()

        self.change = store.find("change_order", 1)
        # 基线：一条已确认的桁架进度、一条已确认的防火验收，均属于 A02。
        self.change["影响展位"] = "BOOT-A02"
        self.change["变更内容"] = "A02 桁架结构调整并重做防火验收"
        self.change["施工班组"] = "结构搭建组"
        self.change["status"] = "草稿"
        self.change["pending"] = True

        self.truss = self._make_row(
            "construction", 101, "桁架结构安装", "BOOT-A02")
        self.fire = self._make_row(
            "checklist", 201, "防火验收", "BOOT-A02")

    def tearDown(self):
        for name, rows in self._tables_backup.items():
            live = store.rows(name)
            live.clear()
            live.extend(copy.deepcopy(rows))
        del store.event_log.entries[self._event_backup:]
        store._id_counters = dict(self._id_backup)
        store._change_receipts.clear()
        store.rebuild_indices()

    def _make_row(self, module: str, row_id: int, label: str, booth: str):
        row = {
            "id": row_id, "status": "已确认", "pending": False, "abnormal": False,
            "登记编号": f"TEST-{row_id}", "负责人": "测试", "生效版本": "v1.0",
            "备注说明": booth,
        }
        if module == "construction":
            row.update({
                "节点名称": label, "计划日期": "2026-09-10",
                "完成日期": "2026-09-11", "前置节点": "",
            })
        else:
            row.update({
                "验收条目": label, "验收标准": "合格", "负责班组": booth,
                "完成时间": "2026-09-12",
            })
        store.rows(module).append(row)
        store.rebuild_indices()
        return row

    def _reconfirm(self, module: str, row: dict) -> None:
        """模拟现场重做/重签：把行重新确认并留下事件。"""
        before = dict(row)
        row["status"] = "已确认"
        row["pending"] = False
        row["abnormal"] = False
        store.event_log.record(
            module=module, entry_id=int(row["id"]), action="现场重新确认",
            before=before, after=dict(row),
        )
        store.rebuild_indices()


class TestNormalIssuance(ChangeIssuanceTestCase):
    def test_issue_reverts_construction_and_checklist(self):
        result = exhibition.propagate_change_order(1)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(self.truss["status"], "待确认")
        self.assertEqual(self.fire["status"], "待确认")
        # 既有输出口径：节点 name、验收项 item。
        self.assertIn(
            {"id": 101, "name": "桁架结构安装"}, result["affected_nodes"])
        self.assertIn(
            {"id": 201, "item": "防火验收"}, result["affected_checklists"])
        self.assertEqual(result["receipt_no"], "CHAN-0001")

    def test_unrelated_construction_untouched(self):
        other = self._make_row("construction", 102, "A01 基础结构", "BOOT-A01")
        result = exhibition.propagate_change_order(1)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(other["status"], "已确认", "原变更没触及的施工行应照常推进")

    def test_workbench_entry_uses_same_logic(self):
        service = service_for("change_order")
        entry, message = service.run_action(1, "签发变更")
        self.assertIsNotNone(entry, message)
        self.assertEqual(self.truss["status"], "待确认")

    def test_issue_endpoint_and_receipt_lookup(self):
        result = exhibition.propagate_change_order(1)
        self.assertTrue(result["ok"])
        looked_up = exhibition.get_change_receipt(receipt_no="CHAN-0001")
        self.assertTrue(looked_up["ok"])
        self.assertEqual(looked_up["first_issuance"]["change_id"], 1)


class TestDuplicateIssuance(ChangeIssuanceTestCase):
    def test_same_receipt_does_not_revert_reconfirmed_node_again(self):
        self.assertTrue(exhibition.propagate_change_order(1)["ok"])
        # 班组重做桁架节点。
        self._reconfirm("construction", self.truss)
        # 同一受理编号再次提交。
        result = exhibition.propagate_change_order(1, receipt_no="CHAN-0001")
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(self.truss["status"], "已确认", "同一受理编号不可再撤一次完工节点")
        self.assertEqual(result["affected_nodes"], [])

    def test_second_entry_same_change_does_not_revert_again(self):
        self.assertTrue(exhibition.propagate_change_order(1)["ok"])
        self._reconfirm("construction", self.truss)
        # 从另一入口（工作台 action）重发原单。
        entry, message = service_for("change_order").run_action(1, "签发变更")
        self.assertIsNotNone(entry, message)
        self.assertEqual(self.truss["status"], "已确认")
        # 两个入口共享同一份回执。
        self.assertTrue(store.find_change_receipt("CHAN-0001"))


class TestPartialFailureAndRetry(ChangeIssuanceTestCase):
    def _fail_checklist_once(self):
        original = exhibition._apply_change_targets
        calls = {"n": 0}

        def flaky(module, receipt, since):
            if module == "checklist" and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("验收侧写入失败")
            return original(module, receipt, since)

        return original, flaky

    def test_partial_failure_keeps_progress_and_reports_incomplete(self):
        original, flaky = self._fail_checklist_once()
        exhibition._apply_change_targets = flaky
        try:
            result = exhibition.propagate_change_order(1)
        finally:
            exhibition._apply_change_targets = original
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], "checklist")
        self.assertEqual(self.truss["status"], "待确认", "已办的桁架进度保留")
        self.assertEqual(self.fire["status"], "已确认", "失败侧未回退")

    def test_retry_same_receipt_completes_remaining_stage(self):
        original, flaky = self._fail_checklist_once()
        exhibition._apply_change_targets = flaky
        try:
            first = exhibition.propagate_change_order(1)
        finally:
            exhibition._apply_change_targets = original
        self.assertFalse(first["ok"])

        # 现场在签发未完成期间重做桁架节点。
        self._reconfirm("construction", self.truss)

        retry = exhibition.propagate_change_order(1, receipt_no="CHAN-0001")
        self.assertTrue(retry["ok"], retry["reason"])
        self.assertEqual(retry["receipt_kind"], "补办")
        self.assertEqual(self.fire["status"], "待确认", "补办应完成未办的验收回退")
        self.assertEqual(self.truss["status"], "已确认", "桁架重做不能被补办再退")

    def test_field_can_reconfirm_both_sides_while_incomplete(self):
        original, flaky = self._fail_checklist_once()
        exhibition._apply_change_targets = flaky
        try:
            exhibition.propagate_change_order(1)
        finally:
            exhibition._apply_change_targets = original

        # 桁架重做、防火重签都能在这轮签发尚不完整时办成。
        self._reconfirm("construction", self.truss)
        self._reconfirm("checklist", self.fire)
        self.assertEqual(self.truss["status"], "已确认")
        self.assertEqual(self.fire["status"], "已确认")

        retry = exhibition.propagate_change_order(1, receipt_no="CHAN-0001")
        self.assertTrue(retry["ok"], retry["reason"])
        self.assertEqual(self.truss["status"], "已确认", "桁架重做不能因此再退")
        self.assertEqual(self.fire["status"], "已确认", "防火重签不能因此再退")


class TestFirstIssuanceRecord(ChangeIssuanceTestCase):
    def test_first_issuance_survives_reissue_from_other_entry(self):
        first = exhibition.propagate_change_order(1)
        self.assertTrue(first["ok"])
        first_detail = first["first_issuance"]

        self._reconfirm("construction", self.truss)
        second = exhibition.propagate_change_order(1, receipt_no="REQ-9001")
        self.assertTrue(second["ok"], second["reason"])

        looked_up = exhibition.get_change_receipt(receipt_no="REQ-9001")
        self.assertTrue(looked_up["ok"])
        self.assertEqual(looked_up["receipt_no"], "CHAN-0001")
        self.assertEqual(
            looked_up["first_issuance"]["affected_nodes"], first_detail["affected_nodes"])
        self.assertEqual(looked_up["first_issuance"]["reverted_nodes"], 1)
        self.assertEqual(self.truss["status"], "已确认", "桁架重做不能因重发再退")


if __name__ == "__main__":
    unittest.main(verbosity=2)
