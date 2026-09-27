"""HTTP 端到端测试：鉴权、门店隔离、撤权后下架任务的异步推进。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import service as service_mod


def _request(url, method="GET", token=None, payload=None, expect=None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode())
            if expect is not None:
                assert resp.status == expect, resp.status
            return resp.status, body
    except urllib.error.HTTPError as e:
        if expect is not None:
            assert e.code == expect, (e.code, e.read().decode())
        return e.code, json.loads(e.read().decode())


class HttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = str(Path(tempfile.mkdtemp()) / "http.db")
        cls.app = service_mod.App(cls.db_path, start_worker=False)
        cls.httpd = service_mod.make_server(0, cls.app)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.admin = "dev-admin-token"

    @classmethod
    def tearDownClass(cls):
        cls.app.shutdown()
        cls.thread.join(timeout=3)

    def api(self, path, method="GET", token=None, payload=None, expect=None):
        return _request(f"{self.base}/api/{path}", method, token, payload, expect)

    def test_health(self):
        status, body = _request(f"{self.base}/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], service_mod.SERVICE_ID)

    def test_auth_required_and_store_isolation(self):
        self.api("orders", expect=401)
        _, bad = self.api("orders", token="wrong-token", expect=401)
        self.assertEqual(bad["error"], "unauthorized")

        _, s1 = self.api("admin/stores", "POST", self.admin,
                         {"id": "s1", "name": "一号店"}, 200)
        _, s2 = self.api("admin/stores", "POST", self.admin,
                         {"id": "s2", "name": "二号店"}, 200)
        t1, t2 = s1["token"], s2["token"]

        # 门店不能访问管理者接口
        self.api("admin/comparison", token=t1, expect=403)

        _, pkg = self.api("packages", "POST", t1, {"name": "宠物写真"})
        self.api("capacity", "PUT", t1, {"day": "2026-10-01", "total": 3})
        _, order = self.api("orders", "POST", t1,
                            {"package_id": pkg["id"], "shoot_day": "2026-10-01"})
        oid = order["id"]
        for scope in ("现场拍摄", "成片交付", "公开传播"):
            self.api(f"orders/{oid}/consents", "POST", t1, {"scope": scope})
        self.api(f"orders/{oid}/shoot", "POST", t1)
        self.api(f"orders/{oid}/deliver", "POST", t1)
        _, asset = self.api("assets", "POST", t1, {"order_id": oid})
        _, pub = self.api(f"assets/{asset['id']}/publish", "POST", t1,
                          {"channel": "小红书", "audience": "公开"})

        # s2 看不到 s1 的订单（按不存在处理），也不能撤权
        status, _ = self.api(f"orders/{oid}/consents", token=t2, expect=404)
        self.assertEqual(status, 404)
        self.api(f"orders/{oid}/consents/revoke",
                 "POST", t2, {"scope": "公开传播"}, expect=404)

        # s1 撤权 -> 下架任务生成
        _, rev = self.api(f"orders/{oid}/consents/revoke",
                          "POST", t1, {"scope": "公开传播", "reason": "顾客要求"})
        self.assertEqual(len(rev["takedown_tasks"]), 1)
        status, tasks = self.api("takedowns", token=t1)
        self.assertEqual(tasks[0]["status"], "待处理")
        # s2 的下架列表为空
        _, other_tasks = self.api("takedowns", token=t2)
        self.assertEqual(other_tasks, [])

        # 管理者视角能看到全量追踪
        _, admin_tasks = self.api("admin/takedowns", token=self.admin)
        self.assertEqual(len(admin_tasks), 1)

        # 异步推进下架（直接触发核心处理，避免等待轮询）
        result = self.app.core.process_takedown_once()
        self.assertEqual(result["result"], "done")
        _, tasks2 = self.api("takedowns", token=t1)
        self.assertEqual(tasks2[0]["status"], "已完成")

    def test_certification_gate_and_comparison(self):
        _, store = self.api("admin/stores", "POST", self.admin,
                            {"id": "s3", "name": "三号店"})
        t3 = store["token"]
        self.api("staff", "POST", t3, {"id": "li", "name": "小李"})
        self.api("schedules", "POST", t3,
                 {"staff_id": "li", "day": "2026-10-02", "slot": "上午"}, expect=403)
        self.api("admin/staff/li/certify", "POST", self.admin)
        _, sch = self.api("schedules", "POST", t3,
                          {"staff_id": "li", "day": "2026-10-02", "slot": "上午"})
        self.assertFalse(sch["cross_store"])

        _, report = self.api("admin/comparison", token=self.admin)
        names = {p["name"] for p in report["packages"]}
        self.assertIn("宠物写真", names)

    def test_incident_chain_over_http(self):
        _, store = self.api("admin/stores", "POST", self.admin,
                            {"id": "s4", "name": "四号店"})
        t4 = store["token"]
        _, inc1 = self.api("incidents", "POST", t4,
                           {"level": "严重事故", "title": "宠物逃逸",
                            "client_ref": "ticket-1"})
        _, inc2 = self.api("incidents", "POST", t4,
                           {"level": "严重事故", "title": "宠物逃逸",
                            "client_ref": "ticket-1"})
        self.assertTrue(inc2["duplicate"])
        self.assertEqual(inc2["id"], inc1["id"])

        self.api(f"incidents/{inc1['id']}/ack", "POST", t4, {"owner": "店长"})
        self.api(f"incidents/{inc1['id']}/resolve", "POST", t4, {"note": "已找回"})
        # 门店不能自己复核闭环
        self.api("admin/incidents", token=t4, expect=403)
        _, closed = self.api(f"admin/incidents/{inc1['id']}/review", "POST",
                             self.admin, {"approve": True, "note": "同意闭环"})
        self.assertEqual(closed["status"], "已闭环")


if __name__ == "__main__":
    unittest.main()
