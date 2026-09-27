"""老字号宠物摄影试验服务入口。

提供 /health 运维检查与 /api 下的业务接口：
- 门店令牌（Authorization: Bearer <token>）只能访问本门店数据；
- 管理者令牌（环境变量 ADMIN_TOKEN，默认 dev-admin-token）可跨店对比、
  认证员工、复核事件、查看全量下架追踪。
"""

import argparse
import json
import os
import secrets
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app import core as core_mod
from app.db import connect, init_db

SERVICE_ID = "heritage-brand-experiment"
DEFAULT_DB = os.environ.get("PET_DB", "data/petphoto.db")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")


def health():
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}


# ---------- 应用装配 ----------

class App:
    """持有数据库连接与领域核心，便于测试中替换。"""

    def __init__(self, db_path: str = DEFAULT_DB, start_worker: bool = True):
        self.domain = core_mod.load_domain()
        self.conn = connect(db_path)
        init_db(self.conn)
        self.core = core_mod.Core(self.conn, self.domain)
        # 重启恢复：处理中的下架任务重新入队，未闭环事件继续推进
        self.recovery = self.core.recover()
        self.server: ThreadingHTTPServer | None = None
        if start_worker:
            self.core.start_worker()

    def shutdown(self):
        self.core.stop_worker()
        if self.server:
            self.server.shutdown()
        self.conn.close()

    def authenticate(self, token: str | None) -> tuple[str, str | None]:
        """返回 (角色, 门店id)。失败抛出 PermissionError。"""
        if not token:
            raise PermissionError("missing_token")
        if secrets.compare_digest(token, ADMIN_TOKEN):
            return "admin", None
        store_id = self.core.store_id_for_token(token)
        if store_id is None:
            raise PermissionError("bad_token")
        return "store", store_id


# ---------- HTTP 处理 ----------

class Handler(BaseHTTPRequestHandler):
    app: App  # 由 make_server 注入到类属性

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError:
            raise core_mod.DomainError("bad_json", "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise core_mod.DomainError("bad_json", "请求体必须是 JSON 对象")
        return data

    def _auth(self):
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else None
        return self.app.authenticate(token)

    def _require_admin(self):
        role, store_id = self._auth()
        if role != "admin":
            raise core_mod.DomainError("forbidden", "需要管理者权限", 403)

    def _require_store(self):
        role, store_id = self._auth()
        if role != "store" or store_id is None:
            raise core_mod.DomainError("forbidden", "需要门店身份", 403)
        return store_id

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                self._send(200, health()); return
            if not path.startswith("/api/"):
                raise core_mod.DomainError("not_found", "接口不存在", 404)
            segments = [s for s in path[len("/api/"):].split("/") if s]
            body = self._read_json() if method in ("POST", "PUT", "DELETE") else {}
            result = self._route(method, segments, query, body)
            self._send(200, result)
        except PermissionError:
            self._send(401, {"error": "unauthorized", "message": "缺少或无效的令牌"})
        except core_mod.DomainError as exc:
            self._send(exc.status, {"error": exc.code, "message": exc.message})
        except Exception as exc:  # 不让单条请求的异常拖垮服务
            self._send(500, {"error": "internal", "message": str(exc)})

    def _route(self, method, seg, query, body):
        c = self.app.core
        q = lambda k, default=None: query.get(k, [default])[0]

        # ---- 管理者接口 ----
        if seg and seg[0] == "admin":
            self._require_admin()
            rest = seg[1:]
            if method == "POST" and rest == ["stores"]:
                token = secrets.token_urlsafe(18)
                store = c.register_store(body["id"], body.get("name", body["id"]), token)
                store["token"] = token  # 仅在创建时返回一次
                return store
            if method == "GET" and rest == ["stores"]:
                return c.list_store_directory()
            if method == "POST" and len(rest) == 3 and rest[0] == "staff" \
                    and rest[2] == "certify":
                return c.certify_staff(rest[1])
            if method == "GET" and rest == ["comparison"]:
                return c.comparison()
            if method == "GET" and rest == ["incidents"]:
                return c.list_incidents(include_closed=q("include_closed", "1") != "0")
            if method == "POST" and len(rest) == 3 and rest[0] == "incidents" \
                    and rest[2] == "review":
                return c.review_incident(rest[1], bool(body.get("approve", True)),
                                         body.get("note", ""))
            if method == "GET" and rest == ["schedules"]:
                return c.list_schedules(cross_store_only=q("cross_store") == "1")
            if method == "GET" and rest == ["packages"]:
                return c.list_packages()
            if method == "GET" and rest == ["takedowns"]:
                return c.list_takedowns()
            if method == "POST" and rest == ["recover"]:
                return c.recover()
            raise core_mod.DomainError("not_found", "接口不存在", 404)

        # ---- 门店接口 ----
        store_id = self._require_store()

        if method == "POST" and seg == ["staff"]:
            return c.register_staff(store_id, body["id"], body.get("name", body["id"]))
        if method == "GET" and seg == ["staff"]:
            return c.list_staff(store_id)

        if method == "POST" and seg == ["packages"]:
            return c.submit_package(store_id, body["name"], body.get("config"),
                                    body.get("stage", "小范围开放"))
        if method == "GET" and seg == ["packages"]:
            return c.list_packages(store_id)
        if method == "POST" and len(seg) == 3 and seg[0] == "packages" \
                and seg[2] == "transition":
            self._owned(c, store_id, seg[1])
            return c.transition_stage(seg[1], body["stage"])

        if method == "PUT" and seg == ["capacity"]:
            return c.set_capacity(store_id, body["day"], int(body["total"]))
        if method == "GET" and seg == ["capacity"]:
            return c.capacity_view(store_id, q("day", ""))

        if method == "POST" and seg == ["orders"]:
            return c.create_order(store_id, body["package_id"], body["shoot_day"],
                                  body.get("id"))
        if method == "GET" and seg == ["orders"]:
            return c.list_orders(store_id)
        if len(seg) >= 2 and seg[0] == "orders":
            oid = seg[1]
            if method == "POST" and seg == ["orders", oid, "cancel"]:
                return c.cancel_order(store_id, oid)
            if method == "POST" and seg == ["orders", oid, "shoot"]:
                return c.mark_shoot(store_id, oid)
            if method == "POST" and seg == ["orders", oid, "deliver"]:
                return c.deliver_order(store_id, oid)
            if method == "GET" and seg == ["orders", oid, "consents"]:
                return c.list_consents(store_id, oid)
            if method == "POST" and seg == ["orders", oid, "consents"]:
                return c.grant_consent(store_id, oid, body["scope"])
            if method == "POST" and len(seg) == 4 and seg[2] == "consents" \
                    and seg[3] == "revoke":
                return c.revoke_consent(store_id, oid, body["scope"],
                                        body.get("reason", ""))

        if method == "POST" and seg == ["assets"]:
            return c.create_asset(store_id, body["order_id"])
        if method == "POST" and len(seg) == 3 and seg[0] == "assets" \
                and seg[2] == "publish":
            return c.publish_asset(store_id, seg[1], body["channel"],
                                   body["audience"], body.get("external_ref"))

        if method == "GET" and seg == ["publications"]:
            return c.list_publications(store_id)
        if method == "POST" and len(seg) == 3 and seg[0] == "publications" \
                and seg[2] == "takedown":
            return c.request_takedown(store_id, seg[1], body.get("reason", "门店要求下架"))

        if method == "GET" and seg == ["takedowns"]:
            return c.list_takedowns(store_id)

        if method == "POST" and seg == ["incidents"]:
            return c.report_incident(
                store_id, body["level"], body["title"], body.get("detail"),
                body.get("order_id"), body.get("client_ref"), body.get("occurred_at"))
        if method == "GET" and seg == ["incidents"]:
            return c.list_incidents(store_id, include_closed=q("include_closed", "1") != "0")
        if method == "POST" and len(seg) == 3 and seg[0] == "incidents" \
                and seg[2] == "ack":
            return c.ack_incident(store_id, seg[1], body.get("owner", "门店值班经理"))
        if method == "POST" and len(seg) == 3 and seg[0] == "incidents" \
                and seg[2] == "resolve":
            return c.resolve_incident(store_id, seg[1], body.get("note", ""))

        if method == "POST" and seg == ["schedules"]:
            return c.schedule_staff(store_id, body["staff_id"], body["day"],
                                    body["slot"], body.get("order_id"))
        if method == "GET" and seg == ["schedules"]:
            return c.list_schedules(store_id, q("day"))
        if method == "POST" and len(seg) == 3 and seg[0] == "schedules" \
                and seg[2] == "cancel":
            return c.cancel_schedule(store_id, seg[1])

        raise core_mod.DomainError("not_found", "接口不存在", 404)

    def _owned(self, c, store_id, resource_id):
        """门店只能操作本门店套餐版本。"""
        if c.package_store_id(resource_id) != store_id:
            raise core_mod.DomainError("package_not_found", "套餐版本不存在", 404)
        return True

    def log_message(self, *_args):
        return


def make_server(port: int, app: App) -> ThreadingHTTPServer:
    Handler.app = app
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    app.server = httpd
    return httpd


def main():
    parser = argparse.ArgumentParser(description="老字号宠物摄影试验后端")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--check", action="store_true", help="校验词表与配置后退出")
    parser.add_argument("--no-worker", action="store_true", help="不启动下架后台线程")
    args = parser.parse_args()

    domain = core_mod.load_domain()
    if args.check:
        required = {"试验阶段", "授权范围", "事件等级"}
        missing = required - set(domain)
        if missing:
            raise SystemExit(f"domain.json 缺少词表：{'、'.join(sorted(missing))}")
        print("基础检查通过")
        print(f"  试验阶段：{'、'.join(domain['试验阶段'])}")
        print(f"  授权范围：{'、'.join(domain['授权范围'])}")
        print(f"  事件等级：{'、'.join(domain['事件等级'])}")
        return

    app = App(args.db, start_worker=not args.no_worker)
    httpd = make_server(args.port, app)

    def _stop(*_args):
        # shutdown() 必须在 serve_forever 之外的线程调用，否则自死锁
        import threading
        threading.Thread(target=app.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        httpd.serve_forever()
    finally:
        app.core.stop_worker()


if __name__ == "__main__":
    main()
