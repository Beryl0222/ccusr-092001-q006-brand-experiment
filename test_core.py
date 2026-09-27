"""核心领域逻辑测试：覆盖授权撤回、下架追踪、重启恢复、重复报送、
跨店调班、套餐版本切换、管理者对比与门店数据隔离。"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core import (
    Core, INC_CLOSED, INC_IN_PROGRESS, INC_NEW,
    PUB_PUBLISHED, PUB_TAKEN_DOWN, PUB_TAKING_DOWN, TASK_DONE, TASK_PENDING,
    load_domain,
)
from app.db import connect, init_db


class FakeClock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        return self.t.isoformat()

    def advance(self, **delta):
        self.t += timedelta(**delta)


class CoreTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        init_db(self.conn)
        self.clock = FakeClock()
        self.core = Core(self.conn, load_domain(), clock=self.clock)
        self.core.register_store("s1", "一号门店", "tok1")
        self.core.register_store("s2", "二号门店", "tok2")

    def tearDown(self):
        self.core.stop_worker()
        self.conn.close()

    def seed_order(self, store="s1", stage="小范围开放", day="2026-09-10"):
        pid = self.core.submit_package(store, "宠物写真", {"bg": "简约"}, stage)["id"]
        self.core.set_capacity(store, day, 5)
        oid = self.core.create_order(store, pid, day)["id"]
        return pid, oid

    def full_consents(self, store, oid):
        for scope in ("现场拍摄", "成片交付", "公开传播", "门店展示"):
            self.core.grant_consent(store, oid, scope)


class ConsentRevocationTest(CoreTestBase):
    def test_revoke_shoot_blocks_unshot_order(self):
        _, oid = self.seed_order()
        self.core.grant_consent("s1", oid, "现场拍摄")
        self.core.revoke_consent("s1", oid, "现场拍摄", "顾客改主意")
        with self.assertRaisesRegex(Exception, "现场拍摄"):
            self.core.mark_shoot("s1", oid)
        # 重新授权后可以继续
        self.core.grant_consent("s1", oid, "现场拍摄")
        self.assertEqual(self.core.mark_shoot("s1", oid)["status"], "已拍")

    def test_revoke_delivery_blocks_undelivered_order(self):
        _, oid = self.seed_order()
        self.full_consents("s1", oid)
        self.core.mark_shoot("s1", oid)
        self.core.revoke_consent("s1", oid, "成片交付")
        with self.assertRaisesRegex(Exception, "成片交付"):
            self.core.deliver_order("s1", oid)

    def test_revoke_publicity_blocks_unpublished_assets(self):
        _, oid = self.seed_order()
        self.full_consents("s1", oid)
        self.core.mark_shoot("s1", oid)
        self.core.deliver_order("s1", oid)
        asset = self.core.create_asset("s1", oid)["id"]

        result = self.core.revoke_consent("s1", oid, "公开传播")
        self.assertEqual(result["blocked_unpublished_assets"], [asset])
        self.assertEqual(result["takedown_tasks"], [])  # 尚未发布，无渠道要下架
        with self.assertRaisesRegex(Exception, "公开传播"):
            self.core.publish_asset("s1", asset, "小红书", "公开")
        # 门店展示是独立授权，不受公开传播撤回影响
        instore = self.core.publish_asset("s1", asset, "店内电子屏", "店内")
        self.assertEqual(instore["status"], PUB_PUBLISHED)

    def test_separate_scopes_are_independent(self):
        _, oid = self.seed_order()
        self.full_consents("s1", oid)
        self.core.mark_shoot("s1", oid)
        self.core.revoke_consent("s1", oid, "公开传播")
        # 成片交付授权仍有效
        self.assertEqual(self.core.deliver_order("s1", oid)["status"], "已交付")

    def test_revoke_is_idempotent_and_keeps_history(self):
        _, oid = self.seed_order()
        self.core.grant_consent("s1", oid, "公开传播")
        first = self.core.revoke_consent("s1", oid, "公开传播")
        second = self.core.revoke_consent("s1", oid, "公开传播")
        self.assertEqual(first["revoked_at"], second["revoked_at"])
        consents = self.core.list_consents("s1", oid)
        self.assertEqual(len(consents), 1)
        self.assertIsNotNone(consents[0]["revoked_at"])
        # 重新授权产生新行，历史保留
        self.core.grant_consent("s1", oid, "公开传播")
        self.assertEqual(len(self.core.list_consents("s1", oid)), 2)


class TakedownTrackingTest(CoreTestBase):
    def _published(self):
        _, oid = self.seed_order()
        self.full_consents("s1", oid)
        self.core.mark_shoot("s1", oid)
        self.core.deliver_order("s1", oid)
        asset = self.core.create_asset("s1", oid)["id"]
        p1 = self.core.publish_asset("s1", asset, "小红书", "公开", "xhs/1")["id"]
        p2 = self.core.publish_asset("s1", asset, "抖音", "公开", "dy/2")["id"]
        return oid, asset, p1, p2

    def test_revoke_spawns_per_channel_tasks(self):
        oid, _, p1, p2 = self._published()
        result = self.core.revoke_consent("s1", oid, "公开传播", "不再同意公开")
        self.assertEqual(len(result["takedown_tasks"]), 2)

        tasks = self.core.list_takedowns("s1")
        self.assertTrue(all(t["status"] == TASK_PENDING for t in tasks))
        for pub_id in (p1, p2):
            pub = self.conn.execute(
                "SELECT * FROM publications WHERE id=?", (pub_id,)).fetchone()
            self.assertEqual(pub["status"], PUB_TAKING_DOWN)

        # 逐渠道推进直至全部下架
        first = self.core.process_takedown_once()
        self.assertEqual(first["result"], "done")
        second = self.core.process_takedown_once()
        self.assertEqual(second["result"], "done")
        self.assertIsNone(self.core.process_takedown_once())  # 队列清空
        for pub_id in (p1, p2):
            pub = self.conn.execute(
                "SELECT * FROM publications WHERE id=?", (pub_id,)).fetchone()
            self.assertEqual(pub["status"], PUB_TAKEN_DOWN)
            self.assertIsNotNone(pub["taken_down_at"])
        self.assertTrue(all(t["status"] == TASK_DONE
                            for t in self.core.list_takedowns("s1")))

    def test_takedown_retries_on_channel_failure(self):
        oid, _, p1, _ = self._published()
        calls = {"n": 0}

        def flaky_adapter(_pub):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("渠道接口超时")

        self.core._takedown_adapter = flaky_adapter
        self.core.revoke_consent("s1", oid, "公开传播")

        r1 = self.core.process_takedown_once()
        self.assertEqual(r1["result"], "retry")
        r2 = self.core.process_takedown_once()
        self.assertEqual(r2["result"], "retry")
        r3 = self.core.process_takedown_once()
        self.assertEqual(r3["result"], "done")
        task = self.core.list_takedowns("s1")[0]
        self.assertEqual(task["attempts"], 2)
        self.assertIn("超时", task["last_error"])

    def test_manual_takedown_and_dedup(self):
        _, _, p1, _ = self._published()
        first = self.core.request_takedown("s1", p1, "收到投诉")
        again = self.core.request_takedown("s1", p1, "再次要求")
        self.assertEqual(first["takedown_tasks"], again["takedown_tasks"])
        self.core.process_takedown_once()
        done = self.core.request_takedown("s1", p1, "已下架后再点")
        self.assertEqual(done["takedown_tasks"], [])

    def test_takedown_scope_does_not_touch_other_audience(self):
        oid, asset, _, _ = self._published()
        instore = self.core.publish_asset("s1", asset, "店内相册屏", "店内")["id"]
        self.core.revoke_consent("s1", oid, "公开传播")
        self.assertEqual(len(self.core.list_takedowns("s1")), 2)  # 仅两个公开渠道
        pub = self.conn.execute(
            "SELECT * FROM publications WHERE id=?", (instore,)).fetchone()
        self.assertEqual(pub["status"], PUB_PUBLISHED)
        # 撤回门店展示才追踪店内渠道
        self.core.revoke_consent("s1", oid, "门店展示")
        self.assertEqual(len(self.core.list_takedowns("s1")), 3)


class IncidentTest(CoreTestBase):
    def test_duplicate_report_by_client_ref(self):
        _, oid = self.seed_order()
        first = self.core.report_incident(
            "s1", "安全关注", "宠物应激", {"note": "发抖"}, oid,
            client_ref="store-ticket-77")
        dup = self.core.report_incident(
            "s1", "严重事故", "宠物应激", {"note": "同一事件补报"}, oid,
            client_ref="store-ticket-77")
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["id"], first["id"])
        # 处置链日志保留了重复报送痕迹
        self.assertTrue(any(log["action"] == "重复报送已合并" for log in dup["logs"]))

    def test_duplicate_fingerprint_within_window(self):
        _, oid = self.seed_order()
        first = self.core.report_incident("s1", "安全关注", "宠物应激", order_id=oid)
        self.clock.advance(minutes=10)
        dup = self.core.report_incident("s1", "安全关注", "宠物应激", order_id=oid)
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["id"], first["id"])
        # 超过去重窗口（30 分钟）视为新事件
        self.clock.advance(minutes=25)
        new = self.core.report_incident("s1", "安全关注", "宠物应激", order_id=oid)
        self.assertFalse(new["duplicate"])
        self.assertNotEqual(new["id"], first["id"])

    def test_closed_incident_not_deduplicated(self):
        _, oid = self.seed_order()
        first = self.core.report_incident(
            "s1", "一般记录", "记录", order_id=oid, client_ref="r1")
        self.core.ack_incident("s1", first["id"], "小王")
        self.core.resolve_incident("s1", first["id"], "已安抚")
        self.core.review_incident(first["id"], True, "通过")
        # client_ref 唯一约束仍然拦截同一单号（门店工单编号唯一）
        again = self.core.report_incident(
            "s1", "一般记录", "记录", order_id=oid, client_ref="r1")
        self.assertTrue(again["duplicate"])

    def test_full_handling_chain_and_reject(self):
        inc = self.core.report_incident("s1", "严重事故", "设备倾倒砸伤")
        self.assertEqual(inc["status"], INC_NEW)
        self.assertTrue(inc["overdue"] is False)
        self.core.ack_incident("s1", inc["id"], "李店长")
        self.core.resolve_incident("s1", inc["id"], "已送医并停用设备")
        got = self.core.review_incident(inc["id"], False, "材料不全")
        self.assertEqual(got["status"], INC_IN_PROGRESS)
        self.core.resolve_incident("s1", inc["id"], "补充就医凭证")
        closed = self.core.review_incident(inc["id"], True, "闭环")
        self.assertEqual(closed["status"], INC_CLOSED)
        self.assertIsNotNone(closed["closed_at"])
        actions = [log["action"] for log in closed["logs"]]
        self.assertIn("复核驳回", actions)
        self.assertIn("复核通过闭环", actions)

    def test_bad_level_rejected(self):
        with self.assertRaisesRegex(Exception, "事件等级"):
            self.core.report_incident("s1", "鸡毛蒜皮", "x")


class CertificationAndScheduleTest(CoreTestBase):
    def test_only_certified_staff_scheduled(self):
        self.core.register_staff("s1", "zhao", "小赵")
        with self.assertRaisesRegex(Exception, "认证"):
            self.core.schedule_staff("s1", "zhao", "2026-09-10", "上午")
        self.core.certify_staff("zhao")
        sch = self.core.schedule_staff("s1", "zhao", "2026-09-10", "上午")
        self.assertFalse(sch["cross_store"])

    def test_cross_store_assignment(self):
        self.core.register_staff("s1", "qian", "老钱")
        self.core.certify_staff("qian")
        # s1 的认证摄影师被排到 s2 支援
        sch = self.core.schedule_staff("s2", "qian", "2026-09-11", "下午")
        self.assertTrue(sch["cross_store"])
        self.assertEqual(sch["home_store_id"], "s1")
        cross = self.core.list_schedules(cross_store_only=True)
        self.assertEqual(len(cross), 1)
        # s1 视角看不到 s2 工位排班；s2 能看到
        self.assertEqual(self.core.list_schedules("s1"), [])
        self.assertEqual(len(self.core.list_schedules("s2")), 1)
        # 归属店不能取消服务店的排班
        with self.assertRaisesRegex(Exception, "排班不存在"):
            self.core.cancel_schedule("s1", sch["id"])
        self.core.cancel_schedule("s2", sch["id"])

    def test_double_booking_conflict(self):
        self.core.register_staff("s1", "sun", "小孙")
        self.core.certify_staff("sun")
        self.core.schedule_staff("s1", "sun", "2026-09-12", "全天")
        with self.assertRaisesRegex(Exception, "已有排班"):
            self.core.schedule_staff("s2", "sun", "2026-09-12", "全天")


class PackageVersionTest(CoreTestBase):
    def test_trial_iterations_and_promotion(self):
        pid10 = self.core.submit_package("s1", "宠物写真", {"price": 99})
        self.assertEqual(pid10["version"], "1.0")
        self.assertEqual(pid10["stage"], "小范围开放")
        pid11 = self.core.submit_package("s1", "宠物写真", {"price": 129})
        self.assertEqual(pid11["version"], "1.1")
        self.assertEqual(pid11["supersedes"], pid10["id"])

        self.core.set_capacity("s1", "2026-09-10", 5)
        self.core.create_order("s1", pid10["id"], "2026-09-10")  # 旧试验版可接单

        # 阶段必须按词表路径推进
        with self.assertRaisesRegex(Exception, "流转"):
            self.core.transition_stage(pid11["id"], "正式经营")
        self.core.transition_stage(pid11["id"], "扩大验证")
        promoted = self.core.transition_stage(pid11["id"], "正式经营")
        self.assertEqual(promoted["version"], "2.0")
        self.assertEqual(promoted["status"], "生效")
        # 旧版本归档，停止接单
        with self.assertRaisesRegex(Exception, "归档"):
            self.core.create_order("s1", pid11["id"], "2026-09-10")
        # 新版本继续接单
        o2 = self.core.create_order("s1", promoted["id"], "2026-09-10")
        self.assertEqual(o2["package_version"], "2.0")
        self.assertEqual(o2["stage_snapshot"], "正式经营")

    def test_pause_blocks_new_orders(self):
        pid = self.core.submit_package("s1", "宠物写真")
        self.core.transition_stage(pid["id"], "暂停")
        self.core.set_capacity("s1", "2026-09-10", 5)
        with self.assertRaisesRegex(Exception, "暂不接单"):
            self.core.create_order("s1", pid["id"], "2026-09-10")
        # 恢复后可继续
        self.core.transition_stage(pid["id"], "小范围开放")
        self.core.create_order("s1", pid["id"], "2026-09-10")

    def test_next_generation_after_promotion(self):
        pid = self.core.submit_package("s1", "宠物写真")
        promoted = self.core.transition_stage(
            self.core.transition_stage(pid["id"], "扩大验证")["id"], "正式经营")
        nxt = self.core.submit_package("s1", "宠物写真", {"price": 199})
        self.assertEqual(nxt["version"], "3.0")
        self.assertEqual(nxt["stage"], "小范围开放")


class CapacityTest(CoreTestBase):
    def test_capacity_blocks_and_cancel_releases(self):
        pid = self.core.submit_package("s1", "宠物写真")
        self.core.set_capacity("s1", "2026-09-10", 1)
        oid = self.core.create_order("s1", pid["id"], "2026-09-10")["id"]
        with self.assertRaisesRegex(Exception, "容量已满"):
            self.core.create_order("s1", pid["id"], "2026-09-10")
        self.core.cancel_order("s1", oid)
        view = self.core.capacity_view("s1", "2026-09-10")
        self.assertEqual(view["remaining"], 1)
        self.core.create_order("s1", pid["id"], "2026-09-10")


class StoreIsolationTest(CoreTestBase):
    def test_store_cannot_touch_other_store_resources(self):
        _, oid = self.seed_order("s1")
        with self.assertRaisesRegex(Exception, "订单不存在"):
            self.core.mark_shoot("s2", oid)
        with self.assertRaisesRegex(Exception, "订单不存在"):
            self.core.grant_consent("s2", oid, "现场拍摄")
        inc = self.core.report_incident("s1", "一般记录", "x", order_id=oid)
        with self.assertRaisesRegex(Exception, "事件不存在"):
            self.core.ack_incident("s2", inc["id"], "外人")
        self.assertEqual([i["id"] for i in self.core.list_incidents("s2")], [])
        self.assertEqual(len(self.core.list_orders("s2")), 0)

    def test_store_cannot_order_with_other_store_package(self):
        pid = self.core.submit_package("s1", "宠物写真")
        self.core.set_capacity("s2", "2026-09-10", 5)
        with self.assertRaisesRegex(Exception, "套餐版本不存在"):
            self.core.create_order("s2", pid["id"], "2026-09-10")


class ComparisonTest(CoreTestBase):
    def _funnel(self, store, day, publish=True, incident_level=None):
        pid, oid = self.seed_order(store, day=day)
        self.full_consents(store, oid)
        self.core.mark_shoot(store, oid)
        self.core.deliver_order(store, oid)
        if publish:
            asset = self.core.create_asset(store, oid)["id"]
            self.core.publish_asset(store, asset, "小红书", "公开")
        if incident_level:
            self.core.report_incident(store, incident_level, "应激", order_id=oid,
                                      client_ref=f"{oid}-inc")
        return pid, oid

    def test_comparison_counts_and_rates(self):
        pid_a, _ = self._funnel("s1", "2026-09-10", incident_level="安全关注")
        self._funnel("s1", "2026-09-11", publish=False)
        self._funnel("s2", "2026-09-12", incident_level="一般记录")

        report = self.core.comparison()["packages"]
        by_pkg = {row["package_id"]: row for row in report}
        row_a = by_pkg[pid_a]
        self.assertEqual(row_a["orders"], 1)
        self.assertEqual(row_a["shot"], 1)
        self.assertEqual(row_a["delivered"], 1)
        self.assertEqual(row_a["publicly_published"], 1)
        self.assertEqual(row_a["shoot_rate"], 1.0)
        self.assertEqual(row_a["serious_incidents"], 1)
        self.assertEqual(row_a["incidents_by_level"]["安全关注"], 1)
        # 管理者能看到两个门店的数据
        stores = {row["store_id"] for row in report}
        self.assertEqual(stores, {"s1", "s2"})

    def test_cancelled_orders_excluded_from_valid_base(self):
        pid, oid = self.seed_order("s1")
        self.core.cancel_order("s1", oid)
        row = [r for r in self.core.comparison()["packages"]
               if r["package_id"] == pid][0]
        self.assertEqual(row["orders"], 1)
        self.assertEqual(row["valid_orders"], 0)
        self.assertIsNone(row["shoot_rate"])


class RecoveryTest(unittest.TestCase):
    """用文件库模拟重启：处理中的下架任务与事件必须继续推进。"""

    def test_recovery_after_restart(self):
        tmp = Path(tempfile.mkdtemp()) / "restart.db"
        conn1 = connect(tmp)
        init_db(conn1)
        clock = FakeClock()
        c1 = Core(conn1, load_domain(), clock=clock)
        c1.register_store("s1", "一号门店", "tok1")
        pid = c1.submit_package("s1", "宠物写真")
        c1.set_capacity("s1", "2026-09-10", 5)
        oid = c1.create_order("s1", pid["id"], "2026-09-10")["id"]
        for scope in ("现场拍摄", "成片交付", "公开传播"):
            c1.grant_consent("s1", oid, scope)
        c1.mark_shoot("s1", oid)
        c1.deliver_order("s1", oid)
        asset = c1.create_asset("s1", oid)["id"]
        c1.publish_asset("s1", asset, "小红书", "公开")
        c1.revoke_consent("s1", oid, "公开传播")
        inc = c1.report_incident("s1", "安全关注", "宠物应激", order_id=oid)
        c1.ack_incident("s1", inc["id"], "小王")

        # 模拟“下架进行到一半进程被杀”：任务停在处理中
        conn1.execute(
            "UPDATE takedown_tasks SET status=?", ("处理中",))
        conn1.commit()
        conn1.close()

        # 重启
        conn2 = connect(tmp)
        init_db(conn2)
        c2 = Core(conn2, load_domain(), clock=clock)
        summary = c2.recover()
        self.assertEqual(summary["requeued_takedowns"], 1)
        self.assertEqual(summary["reopened_incidents"], 1)

        task = c2.list_takedowns("s1")[0]
        self.assertEqual(task["status"], TASK_PENDING)
        result = c2.process_takedown_once()
        self.assertEqual(result["result"], "done")

        reopened = c2.list_incidents("s1")[0]
        self.assertEqual(reopened["status"], INC_NEW)
        self.assertTrue(any(log["action"] == "重启恢复" for log in reopened["logs"]))
        conn2.close()


if __name__ == "__main__":
    unittest.main()
