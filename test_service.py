"""服务身份与 HTTP API 集成测试。"""

import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from urllib.parse import quote

from service import SERVICE_ID, build_server
from service import health


class HealthTest(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(health(), {"status": "ok", "service": SERVICE_ID})


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cls.server, _ = build_server(0, cls.db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        os.unlink(cls.db_path)

    def req(self, method, path, body=None, role="manager", store_id=None, name=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-Actor-Role": role}
        if store_id is not None:
            headers["X-Store-Id"] = str(store_id)
        if name:
            headers["X-Actor-Name"] = name
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        if payload:
            headers["Content-Type"] = "application/json; charset=utf-8"
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def test_full_flow_via_http(self):
        status, body = self.req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], SERVICE_ID)

        _, s1 = self.req("POST", "/stores", {"name": "总店"})
        _, s2 = self.req("POST", "/stores", {"name": "分店"})
        _, plan = self.req("POST", f"/stores/{s1['id']}/plans",
                           {"name": "宠物摄影", "capacity": 5})
        pid = plan["id"]
        _, v = self.req("POST", f"/plans/{pid}/versions",
                        {"label": "基础套餐", "price_cents": 19900})
        self.req("POST", f"/versions/{v['id']}/activate")
        self.req("POST", f"/plans/{pid}/phase", {"phase": "小范围开放"})

        _, order = self.req("POST", "/orders", {
            "store_id": s1["id"], "plan_id": pid, "version_id": v["id"],
            "customer_name": "王女士", "pet_name": "布丁"})
        oid = order["id"]

        # 门店角色只能接触本店数据。
        status, body = self.req("GET", f"/orders/{oid}", role="store",
                                store_id=s2["id"])
        self.assertEqual(status, 403)
        status, _ = self.req("GET", f"/orders/{oid}", role="store",
                             store_id=s1["id"])
        self.assertEqual(status, 200)

        # 授权撤回 -> 下架任务链路。
        self.req("POST", f"/orders/{oid}/consents",
                 {"scope": "公开传播", "action": "grant"},
                 role="store", store_id=s1["id"])
        _, content = self.req("POST", "/contents",
                              {"order_id": oid, "title": "布丁写真"},
                              role="store", store_id=s1["id"])
        self.req("POST", f"/contents/{content['id']}/publish",
                 {"channel": "小红书"}, role="store", store_id=s1["id"])
        self.req("POST", f"/orders/{oid}/consents",
                 {"scope": "公开传播", "action": "withdraw"})
        _, tasks = self.req("GET", f"/takedowns?status={quote('待下架')}")
        self.assertEqual(len(tasks), 1)
        status, _ = self.req("POST", f"/takedowns/{tasks[0]['id']}/complete",
                             role="store", store_id=s1["id"])
        self.assertEqual(status, 200)

        # 重复报送并入同一事件。
        inc = {"store_id": s1["id"], "category": "宠物应激",
               "level": "一般记录", "occurred_at": "2026-09-27",
               "order_id": oid, "incident_key": "same-accident-1"}
        _, first = self.req("POST", "/incidents", inc)
        _, second = self.req("POST", "/incidents", inc)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["id"], first["id"])

        _, analytics = self.req("GET", "/analytics/plans")
        self.assertEqual(analytics[0]["incidents"]["total"], 1)
        _, pending = self.req("GET", "/pending-work")
        self.assertEqual(len(pending["pending_takedowns"]), 0)
        self.assertEqual(len(pending["open_incidents"]), 1)


if __name__ == "__main__":
    unittest.main()
