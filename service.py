"""宠物摄影新业务后端服务入口。

保留稳定服务身份与 ``python3 service.py --check``；启动时执行恢复盘点，
使待下架任务与未闭环事件在重启后继续可追踪推进。

鉴权通过请求头：
- X-Actor-Role: manager（总部管理者）/ store（门店，需配合 X-Store-Id）
- X-Store-Id: 门店角色所属门店
- X-Actor-Name: 操作者姓名（可选，用于留痕）
"""

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import domain
from core import Actor, BadRequest, Core, DomainError, Forbidden, NotFound
from db import connect, init_db

SERVICE_ID = "heritage-brand-experiment"
DEFAULT_DB = os.environ.get("PETSTUDIO_DB", "petstudio.db")


def health():
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}


def make_core(path=DEFAULT_DB):
    """建库建表、执行恢复盘点并返回 Core。"""
    conn = connect(path)
    init_db(conn)
    core = Core(conn)
    core.recover()
    return core


class Handler(BaseHTTPRequestHandler):
    """JSON API 处理器。core 与 lock 由 build_server 注入。"""

    core = None
    lock = None

    # -- 基础工具 -------------------------------------------------------

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self):
        role = self.headers.get("X-Actor-Role", "manager")
        if role not in ("manager", "store"):
            raise Forbidden("X-Actor-Role 只能是 manager 或 store")
        store_id = self.headers.get("X-Store-Id")
        if role == "store":
            if store_id is None:
                raise Forbidden("门店角色需要 X-Store-Id")
            store_id = int(store_id)
        return Actor(role=role, store_id=store_id,
                     name=self.headers.get("X-Actor-Name") or role)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError as exc:
            raise BadRequest(f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return data

    def _query(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def log_message(self, *_args):
        return

    # -- 请求分发 -------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            with self.lock:
                actor = self._actor()
                path = urlparse(self.path).path.rstrip("/") or "/"
                body = self._body() if method == "POST" else {}
                query = self._query() if method == "GET" else {}
                self._route(method, path, actor, body, query)
        except DomainError as exc:
            self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError) as exc:
            self._send(400, {"error": f"参数错误: {exc}"})

    def _route(self, method, path, actor, body, query):
        core = self.core

        if method == "GET" and path == "/health":
            return self._send(200, health())
        if method == "GET" and path == "/domain":
            return self._send(200, {
                "试验阶段": domain.PHASES,
                "授权范围": domain.SCOPES,
                "事件等级": domain.LEVELS,
            })

        # 门店
        if path == "/stores":
            if method == "POST":
                return self._send(201, core.create_store(actor, body.get("name")))
            return self._send(200, core.list_stores(actor))

        # 方案
        if path == "/plans" and method == "GET":
            return self._send(200, core.list_plans(
                actor, store_id=query.get("store_id"), phase=query.get("phase")))

        m = re.fullmatch(r"/stores/(\d+)/plans", path)
        if m and method == "POST":
            return self._send(201, core.create_plan(
                actor, int(m.group(1)), body.get("name"), body.get("capacity")))

        m = re.fullmatch(r"/plans/(\d+)", path)
        if m and method == "GET":
            plan = core.get_plan(int(m.group(1)))
            actor.check_store(plan["store_id"])
            return self._send(200, plan)

        m = re.fullmatch(r"/plans/(\d+)/phase", path)
        if m and method == "POST":
            return self._send(200, core.transition_plan(
                actor, int(m.group(1)), body.get("phase"), body.get("reason")))

        m = re.fullmatch(r"/plans/(\d+)/phase-history", path)
        if m and method == "GET":
            return self._send(200, core.plan_phase_history(actor, int(m.group(1))))

        # 套餐版本
        m = re.fullmatch(r"/plans/(\d+)/versions", path)
        if m:
            if method == "POST":
                return self._send(201, core.add_version(
                    actor, int(m.group(1)), body.get("label"),
                    body.get("price_cents", 0)))
            return self._send(200, core.list_versions(actor, int(m.group(1))))

        m = re.fullmatch(r"/versions/(\d+)/activate", path)
        if m and method == "POST":
            return self._send(200, core.activate_version(actor, int(m.group(1))))

        # 员工 / 认证 / 排班
        if path == "/employees" and method == "POST":
            return self._send(201, core.create_employee(
                actor, body.get("name"), body.get("home_store_id")))

        m = re.fullmatch(r"/employees/(\d+)", path)
        if m and method == "GET":
            return self._send(200, core.get_employee(actor, int(m.group(1))))

        m = re.fullmatch(r"/employees/(\d+)/certifications", path)
        if m and method == "POST":
            return self._send(201, core.add_certification(
                actor, int(m.group(1)), body.get("skill"),
                body.get("completed_at"), body.get("expires_at")))

        if path == "/assignments":
            if method == "POST":
                return self._send(201, core.create_assignment(
                    actor, body.get("employee_id"), body.get("store_id"),
                    body.get("work_date"), body.get("shift")))
            return self._send(200, core.list_assignments(
                actor, store_id=query.get("store_id"), work_date=query.get("work_date")))

        # 订单
        if path == "/orders":
            if method == "POST":
                return self._send(201, core.create_order(
                    actor, body.get("store_id"), body.get("plan_id"),
                    body.get("version_id"), body.get("customer_name"),
                    body.get("pet_name"), body.get("pet_species")))
            return self._send(200, core.list_orders(
                actor, store_id=query.get("store_id"), status=query.get("status")))

        m = re.fullmatch(r"/orders/(\d+)", path)
        if m and method == "GET":
            order = core.get_order(int(m.group(1)))
            actor.check_store(order["store_id"])
            return self._send(200, order)

        m = re.fullmatch(r"/orders/(\d+)/advance", path)
        if m and method == "POST":
            return self._send(200, core.advance_order(
                actor, int(m.group(1)), body.get("status")))

        m = re.fullmatch(r"/orders/(\d+)/switch-version", path)
        if m and method == "POST":
            return self._send(200, core.switch_version(
                actor, int(m.group(1)), body.get("version_id"), body.get("reason")))

        # 授权
        m = re.fullmatch(r"/orders/(\d+)/consents", path)
        if m:
            if method == "POST":
                return self._send(200, core.set_consent(
                    actor, int(m.group(1)), body.get("scope"), body.get("action")))
            return self._send(200, core.get_consents(actor, int(m.group(1))))

        # 内容发布与下架
        if path == "/contents" and method == "POST":
            return self._send(201, core.create_content(
                actor, body.get("order_id"), body.get("title")))

        m = re.fullmatch(r"/contents/(\d+)", path)
        if m and method == "GET":
            return self._send(200, core.get_content(actor, int(m.group(1))))

        m = re.fullmatch(r"/contents/(\d+)/publish", path)
        if m and method == "POST":
            return self._send(200, core.publish_content(
                actor, int(m.group(1)), body.get("channel")))

        if path == "/takedowns" and method == "GET":
            return self._send(200, core.list_takedowns(
                actor, status=query.get("status"), store_id=query.get("store_id")))

        m = re.fullmatch(r"/takedowns/(\d+)/complete", path)
        if m and method == "POST":
            return self._send(200, core.complete_takedown(actor, int(m.group(1))))

        # 事件处置
        if path == "/incidents":
            if method == "POST":
                return self._send(201, core.report_incident(
                    actor, body.get("store_id"), body.get("category"),
                    body.get("level"), body.get("occurred_at"),
                    body.get("order_id"), body.get("detail"),
                    body.get("incident_key"), body.get("reporter")))
            return self._send(200, core.list_incidents(
                actor, store_id=query.get("store_id"),
                status=query.get("status"), open_only=query.get("open") == "1"))

        m = re.fullmatch(r"/incidents/(\d+)", path)
        if m and method == "GET":
            return self._send(200, core.incident_detail(actor, int(m.group(1))))

        m = re.fullmatch(r"/incidents/(\d+)/advance", path)
        if m and method == "POST":
            return self._send(200, core.advance_incident(
                actor, int(m.group(1)), body.get("status"), body.get("note")))

        # 经营分析与待办恢复
        if path == "/analytics/plans" and method == "GET":
            return self._send(200, core.plan_analytics(
                actor, store_id=query.get("store_id")))
        if path == "/analytics/versions" and method == "GET":
            return self._send(200, core.version_analytics(
                actor, plan_id=query.get("plan_id"), store_id=query.get("store_id")))
        if path == "/pending-work" and method == "GET":
            return self._send(200, core.pending_work(
                actor, store_id=query.get("store_id")))

        raise NotFound(f"未知路径: {method} {path}")


def build_server(port, db_path=DEFAULT_DB):
    """组装服务器（测试可直接使用，不绑定端口）。"""
    core = make_core(db_path)
    handler = type("BoundHandler", (Handler,),
                   {"core": core, "lock": threading.RLock()})
    return ThreadingHTTPServer(("0.0.0.0", port), handler), core


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="宠物摄影新业务后端")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--check", action="store_true", help="初始化并检查配置")
    args = parser.parse_args()
    if args.check:
        core = make_core(args.db)
        pending = core.recover()
        print("基础检查通过")
        print(f"词表: {len(domain.PHASES)} 个阶段, {len(domain.SCOPES)} 类授权,"
              f" {len(domain.LEVELS)} 级事件")
        print(f"恢复盘点: 待下架 {pending['pending_takedowns']} 项,"
              f" 未闭环事件 {pending['open_incidents']} 起")
    else:
        server, _core = build_server(args.port, args.db)
        print(f"服务已启动: http://0.0.0.0:{args.port} (db={args.db})")
        server.serve_forever()
