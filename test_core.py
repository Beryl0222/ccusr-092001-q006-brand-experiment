"""核心业务逻辑测试：覆盖授权撤回、下架追踪、重复报送、跨店调班、
套餐版本切换、数据隔离与重启恢复。"""

import os
import tempfile
import unittest

import domain
from core import Actor, Conflict, Core, DomainError, Forbidden
from db import connect, init_db

MANAGER = Actor(role="manager", name="总部")
DATE = "2026-09-27"


class CoreTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = connect(self.path)
        init_db(self.conn)
        self.core = Core(self.conn)
        self.s1 = self.core.create_store(MANAGER, "南京东路总店")["id"]
        self.s2 = self.core.create_store(MANAGER, "陆家嘴分店")["id"]
        self.staff1 = Actor(role="store", store_id=self.s1, name="总店店长")
        self.staff2 = Actor(role="store", store_id=self.s2, name="分店店长")
        self.pid, self.vid = self._open_plan()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    # -- 工具 ----------------------------------------------------------

    def _plan(self, store_id=None, capacity=2):
        store_id = store_id or self.s1
        return self.core.create_plan(
            MANAGER, store_id, "宠物摄影试验", capacity)["id"]

    def _open_plan(self, store_id=None, capacity=2):
        pid = self._plan(store_id, capacity)
        vid = self.core.add_version(MANAGER, pid, "基础套餐", 19900)["id"]
        self.core.activate_version(MANAGER, vid)
        self.core.transition_plan(MANAGER, pid, domain.PHASE_SMALL)
        return pid, vid

    def _order(self, pid=None, vid=None, store_id=None):
        pid = pid or self.pid
        vid = vid or self.vid
        return self.core.create_order(
            MANAGER, store_id or self.s1, pid, vid, "王女士", "布丁", "猫")["id"]

    # -- 方案阶段与容量 ------------------------------------------------

    def test_phase_flow_and_capacity(self):
        # 提案阶段不接单。
        pid2 = self._plan()
        with self.assertRaises(Conflict):
            self._order(pid=pid2, vid=self.vid)
        # 门店可暂停本店方案，但不能自行推进阶段。
        self.core.transition_plan(self.staff1, self.pid, domain.PHASE_PAUSED)
        with self.assertRaises(Forbidden):
            self.core.transition_plan(self.staff1, self.pid, domain.PHASE_SMALL)
        self.core.transition_plan(MANAGER, self.pid, domain.PHASE_SMALL)
        o1 = self._order()
        o2 = self._order()
        with self.assertRaises(Conflict):  # 容量 2 已满
            self._order()
        # 订单走完后释放容量。
        self.core.set_consent(MANAGER, o1, domain.SCOPE_SHOOT, "grant")
        self.core.advance_order(MANAGER, o1, "拍摄完成")
        self.core.advance_order(MANAGER, o1, "成片制作")
        self.core.advance_order(MANAGER, o1, "待交付")
        self.core.set_consent(MANAGER, o1, domain.SCOPE_DELIVER, "grant")
        self.core.advance_order(MANAGER, o1, "已交付")
        self.core.advance_order(MANAGER, o1, "已完成")
        self.assertTrue(self._order())  # 容量释放，可再接单

    def test_illegal_phase_jump_rejected(self):
        with self.assertRaises(Conflict):
            self.core.transition_plan(MANAGER, self.pid, domain.PHASE_FORMAL)

    # -- 认证与排班 ----------------------------------------------------

    def test_certification_required_and_cross_store(self):
        e = self.core.create_employee(MANAGER, "老李", self.s1)["id"]
        with self.assertRaises(Conflict):  # 未认证不能排班
            self.core.create_assignment(MANAGER, e, self.s1, DATE, "全天")
        with self.assertRaises(Forbidden):  # 认证由总部核发
            self.core.add_certification(self.staff1, e)
        self.core.add_certification(MANAGER, e, expires_at="2026-12-31")
        self.core.create_assignment(MANAGER, e, self.s1, DATE, "全天")
        # 跨店调班：总店店长不能操作分店，总部可协调。
        with self.assertRaises(Forbidden):
            self.core.create_assignment(self.staff1, e, self.s2, DATE, "全天")
        a = self.core.create_assignment(MANAGER, e, self.s2, DATE, "上午")
        self.assertEqual(a["cross_store"], 1)
        # 过期认证不再有效。
        self.core.add_certification(MANAGER, e, completed_at="2025-01-01",
                                    expires_at="2026-01-01")
        with self.assertRaises(Conflict):
            self.core.create_assignment(MANAGER, e, self.s1, "2026-06-01", "全天")

    # -- 分项授权 ------------------------------------------------------

    def test_shoot_and_delivery_consent_gates(self):
        oid = self._order()
        with self.assertRaises(Conflict):
            self.core.advance_order(MANAGER, oid, "拍摄完成")
        self.core.set_consent(self.staff1, oid, domain.SCOPE_SHOOT, "grant")
        self.core.advance_order(MANAGER, oid, "拍摄完成")
        self.core.advance_order(MANAGER, oid, "成片制作")
        self.core.advance_order(MANAGER, oid, "待交付")
        with self.assertRaises(Conflict):
            self.core.advance_order(MANAGER, oid, "已交付")
        self.core.set_consent(MANAGER, oid, domain.SCOPE_DELIVER, "grant")
        self.core.advance_order(MANAGER, oid, "已交付")

    def test_withdraw_locks_unpublished_content(self):
        oid = self._order()
        cid = self.core.create_content(MANAGER, oid, "布丁写真")["id"]
        self.core.set_consent(MANAGER, oid, domain.SCOPE_PUBLIC, "grant")
        # 撤回时内容尚未发布：进入发布锁定，不产生下架任务。
        self.core.set_consent(MANAGER, oid, domain.SCOPE_PUBLIC, "withdraw")
        content = self.core.get_content(MANAGER, cid)
        self.assertEqual(content["status"], domain.CONTENT_LOCKED)
        self.assertEqual(self.core.list_takedowns(MANAGER), [])
        with self.assertRaises(Conflict):
            self.core.publish_content(MANAGER, cid, "小红书")
        # 重新授权后解除锁定，可正常发布。
        self.core.set_consent(MANAGER, oid, domain.SCOPE_PUBLIC, "grant")
        self.assertEqual(self.core.get_content(MANAGER, cid)["status"],
                         domain.CONTENT_READY)
        self.core.publish_content(MANAGER, cid, "小红书")
        self.assertEqual(self.core.get_content(MANAGER, cid)["status"],
                         domain.CONTENT_PUBLISHED)

    def test_display_scope_independent_from_public(self):
        oid = self._order()
        cid = self.core.create_content(MANAGER, oid, "布丁写真")["id"]
        self.core.set_consent(MANAGER, oid, domain.SCOPE_DISPLAY, "grant")
        self.core.publish_content(MANAGER, cid, domain.DISPLAY_CHANNEL)
        # 仅有门店展示授权，公开渠道不能发布。
        with self.assertRaises(Conflict):
            self.core.publish_content(MANAGER, cid, "抖音")
        # 撤回公开传播不影响门店展示；撤回门店展示才生成下架任务。
        self.core.set_consent(MANAGER, oid, domain.SCOPE_PUBLIC, "withdraw")
        self.assertEqual(self.core.list_takedowns(MANAGER), [])
        self.core.set_consent(MANAGER, oid, domain.SCOPE_DISPLAY, "withdraw")
        tasks = self.core.list_takedowns(MANAGER)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["channel"], domain.DISPLAY_CHANNEL)

    def test_withdraw_creates_and_tracks_takedowns(self):
        oid = self._order()
        cid = self.core.create_content(MANAGER, oid, "布丁写真")["id"]
        self.core.set_consent(MANAGER, oid, domain.SCOPE_PUBLIC, "grant")
        self.core.publish_content(MANAGER, cid, "小红书")
        self.core.publish_content(MANAGER, cid, "抖音")
        self.core.set_consent(MANAGER, oid, domain.SCOPE_PUBLIC, "withdraw")
        tasks = self.core.list_takedowns(MANAGER, status=domain.TAKEDOWN_PENDING)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(self.core.get_content(MANAGER, cid)["status"],
                         domain.CONTENT_TAKING_DOWN)
        # 完成一个渠道：内容仍在下架中（另一渠道未完成）。
        self.core.complete_takedown(MANAGER, tasks[0]["id"])
        self.assertEqual(self.core.get_content(MANAGER, cid)["status"],
                         domain.CONTENT_TAKING_DOWN)
        with self.assertRaises(Conflict):  # 不能重复完成
            self.core.complete_takedown(MANAGER, tasks[0]["id"])
        self.core.complete_takedown(MANAGER, tasks[1]["id"])
        self.assertEqual(self.core.get_content(MANAGER, cid)["status"],
                         domain.CONTENT_TAKEN_DOWN)
        done = self.core.list_takedowns(MANAGER, status=domain.TAKEDOWN_DONE)
        self.assertEqual(len(done), 2)

    # -- 事件处置链 ----------------------------------------------------

    def test_duplicate_incident_report_merged(self):
        oid = self._order()
        first = self.core.report_incident(
            MANAGER, self.s1, "宠物应激", "服务中断", DATE,
            order_id=oid, detail="拍摄中猫咪哈气")
        second = self.core.report_incident(
            MANAGER, self.s1, "宠物应激", "服务中断", DATE,
            order_id=oid, detail="再次报送同一情况")
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["report_count"], 2)
        # 更严重的重复报送升级等级，更轻的不降级。
        third = self.core.report_incident(
            MANAGER, self.s1, "宠物应激", "严重事故", DATE, order_id=oid)
        self.assertEqual(third["level"], "严重事故")
        fourth = self.core.report_incident(
            MANAGER, self.s1, "宠物应激", "一般记录", DATE, order_id=oid)
        self.assertEqual(fourth["level"], "严重事故")
        detail = self.core.incident_detail(MANAGER, first["id"])
        self.assertEqual(len(detail["reports"]), 4)

    def test_serious_incident_requires_hq_review(self):
        inc = self.core.report_incident(
            self.staff1, self.s1, "现场事故", "安全关注", DATE,
            detail="灯具倾倒擦伤顾客")["id"]
        self.core.advance_incident(self.staff1, inc, domain.INCIDENT_HANDLING)
        with self.assertRaises(Conflict):  # 安全关注必须先复核
            self.core.advance_incident(self.staff1, inc, domain.INCIDENT_CLOSED)
        self.core.advance_incident(MANAGER, inc, domain.INCIDENT_REVIEW)
        with self.assertRaises(Forbidden):  # 复核后闭环须总部确认
            self.core.advance_incident(self.staff1, inc, domain.INCIDENT_CLOSED)
        self.core.advance_incident(MANAGER, inc, domain.INCIDENT_CLOSED)
        # 闭环后同键报送视为新事件。
        again = self.core.report_incident(
            self.staff1, self.s1, "现场事故", "安全关注", DATE)
        self.assertFalse(again["duplicate"])
        self.assertNotEqual(again["id"], inc)

    def test_minor_incident_store_can_close(self):
        inc = self.core.report_incident(
            self.staff1, self.s1, "宠物应激", "一般记录", DATE)["id"]
        self.core.advance_incident(self.staff1, inc, domain.INCIDENT_HANDLING)
        closed = self.core.advance_incident(self.staff1, inc, domain.INCIDENT_CLOSED)
        self.assertEqual(closed["status"], domain.INCIDENT_CLOSED)

    # -- 套餐版本切换 --------------------------------------------------

    def test_version_switching(self):
        v2 = self.core.add_version(MANAGER, self.pid, "精修套餐", 39900)["id"]
        # 草稿不能直接下单。
        with self.assertRaises(Conflict):
            self._order(vid=v2)
        oid = self._order()
        self.core.activate_version(MANAGER, v2)
        versions = {v["id"]: v["status"]
                    for v in self.core.list_versions(MANAGER, self.pid)}
        self.assertEqual(versions[self.vid], domain.VERSION_RETIRED)
        self.assertEqual(versions[v2], domain.VERSION_ACTIVE)
        # 老版本停售，新单只能下在售版本。
        with self.assertRaises(Conflict):
            self._order()
        self._order(vid=v2)
        # 在途订单可切到新在售版本并留痕。
        self.core.switch_version(MANAGER, oid, v2, reason="顾客升级精修")
        self.assertEqual(self.core.get_order(oid)["version_id"], v2)
        with self.assertRaises(Conflict):  # 已是该版本
            self.core.switch_version(MANAGER, oid, v2)
        # 交付后不能再切换。
        self.core.set_consent(MANAGER, oid, domain.SCOPE_SHOOT, "grant")
        for status in ("拍摄完成", "成片制作", "待交付"):
            self.core.advance_order(MANAGER, oid, status)
        self.core.set_consent(MANAGER, oid, domain.SCOPE_DELIVER, "grant")
        self.core.advance_order(MANAGER, oid, "已交付")
        with self.assertRaises(Conflict):
            self.core.switch_version(MANAGER, oid, self.vid)

    # -- 数据隔离 ------------------------------------------------------

    def test_store_isolation(self):
        pid2 = self._plan(self.s2)
        # 门店只能看到本店方案、订单、事件与待办。
        self.assertEqual([p["id"] for p in self.core.list_plans(self.staff1)],
                         [self.pid])
        plan2 = self.core.get_plan(pid2)
        with self.assertRaises(Forbidden):
            self.staff1.check_store(plan2["store_id"])
        with self.assertRaises(Forbidden):
            self.core.transition_plan(self.staff1, pid2, domain.PHASE_PAUSED)
        with self.assertRaises(Forbidden):
            self.core.list_incidents(self.staff1, store_id=self.s2)
        # 总部可见全部。
        self.assertEqual(len(self.core.list_plans(MANAGER)), 2)

    # -- 经营分析 ------------------------------------------------------

    def test_analytics_compare_versions(self):
        v2 = self.core.add_version(MANAGER, self.pid, "精修套餐", 39900)["id"]
        o1 = self._order()  # 先在 v1 在售期下单
        self.core.activate_version(MANAGER, v2)
        o2 = self._order(vid=v2)
        self.core.switch_version(MANAGER, o1, v2, reason="升级")
        self.core.set_consent(MANAGER, o2, domain.SCOPE_SHOOT, "grant")
        self.core.advance_order(MANAGER, o2, "拍摄完成")
        self.core.report_incident(MANAGER, self.s1, "宠物应激", "服务中断", DATE,
                                  order_id=o2)
        va = {v["version_id"]: v for v in
              self.core.version_analytics(MANAGER, plan_id=self.pid)}
        self.assertEqual(va[v2]["orders_current"], 2)
        self.assertEqual(va[v2]["switches_in"], 1)
        self.assertEqual(va[self.vid]["orders_current"], 0)
        pa = {p["plan_id"]: p for p in self.core.plan_analytics(MANAGER)}
        self.assertEqual(pa[self.pid]["orders_total"], 2)
        self.assertEqual(pa[self.pid]["conversion"]["shot_rate"], 0.5)
        self.assertEqual(pa[self.pid]["incidents"]["by_level"]["服务中断"], 1)

    # -- 重启恢复 ------------------------------------------------------

    def test_recovery_after_restart(self):
        oid = self._order()
        cid = self.core.create_content(MANAGER, oid, "布丁写真")["id"]
        self.core.set_consent(MANAGER, oid, domain.SCOPE_PUBLIC, "grant")
        self.core.publish_content(MANAGER, cid, "小红书")
        self.core.set_consent(MANAGER, oid, domain.SCOPE_PUBLIC, "withdraw")
        self.core.report_incident(MANAGER, self.s1, "现场事故", "安全关注", DATE)
        self.conn.close()
        # 模拟服务重启：重新打开同一个库文件。
        conn2 = connect(self.path)
        core2 = Core(conn2)
        recovered = core2.recover()
        self.assertEqual(recovered["pending_takedowns"], 1)
        self.assertEqual(recovered["open_incidents"], 1)
        pending = core2.pending_work(MANAGER)
        self.assertEqual(len(pending["pending_takedowns"]), 1)
        self.assertEqual(len(pending["open_incidents"]), 1)
        # 重启后仍可继续推进处置链。
        tid = pending["pending_takedowns"][0]["id"]
        core2.complete_takedown(MANAGER, tid)
        iid = pending["open_incidents"][0]["id"]
        core2.advance_incident(MANAGER, iid, domain.INCIDENT_HANDLING)
        core2.advance_incident(MANAGER, iid, domain.INCIDENT_REVIEW)
        core2.advance_incident(MANAGER, iid, domain.INCIDENT_CLOSED)
        self.assertEqual(core2.pending_work(MANAGER),
                         {"pending_takedowns": [], "open_incidents": []})
        conn2.close()


if __name__ == "__main__":
    unittest.main()
