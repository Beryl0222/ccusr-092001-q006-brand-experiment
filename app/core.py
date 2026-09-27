"""宠物摄影试验业务的核心领域逻辑。

设计要点：
- 所有规则以 domain.json 词表为准（试验阶段 / 授权范围 / 事件等级）。
- 写操作统一在锁内串行化，配合单 SQLite 连接适配多线程 HTTP 服务。
- 下架任务与事件处置状态持久化，重启后可恢复推进。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

DOMAIN_PATH = Path(__file__).resolve().parent.parent / "domain.json"

# 订单状态
ORDER_BOOKED = "预约"
ORDER_SHOT = "已拍"
ORDER_DELIVERED = "已交付"
ORDER_CANCELLED = "已取消"

# 套餐版本状态
PKG_TRIAL = "试用"
PKG_ACTIVE = "生效"
PKG_ARCHIVED = "归档"

# 发布 / 下架 / 任务 / 事件状态
PUB_PUBLISHED = "已发布"
PUB_TAKING_DOWN = "下架中"
PUB_TAKEN_DOWN = "已下架"

TASK_PENDING = "待处理"
TASK_PROCESSING = "处理中"
TASK_DONE = "已完成"

INC_NEW = "待响应"
INC_IN_PROGRESS = "处理中"
INC_REVIEW = "待复核"
INC_CLOSED = "已闭环"

# 试验阶段允许的流转
STAGE_TRANSITIONS: dict[str, set[str]] = {
    "提案": {"小范围开放", "暂停"},
    "小范围开放": {"扩大验证", "暂停"},
    "扩大验证": {"正式经营", "暂停"},
    "正式经营": {"暂停"},
    # 暂停恢复需明确去向；恢复到正式经营即按正式版本继续经营
    "暂停": {"小范围开放", "扩大验证", "正式经营"},
}

ORDERABLE_STAGES = {"小范围开放", "扩大验证", "正式经营"}

# 授权范围 -> 可触发的公开发布受众
SCOPE_AUDIENCE = {"公开传播": "公开", "门店展示": "店内"}
SCOPE_FUNNEL = {"现场拍摄": "到店拍摄", "成片交付": "成片交付"}

# 事件等级的处置时限（小时），仅用于风险度量与超时提示
SLA_HOURS = {"一般记录": 72, "服务中断": 24, "安全关注": 8, "严重事故": 2}

# 无 client_ref 时，同店同订单同等级同标题的未闭环事件在该窗口内视为重复报送
DUP_WINDOW = timedelta(minutes=30)


class DomainError(Exception):
    """业务规则冲突。code 供接口与测试稳定断言。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def load_domain(path: str | Path = DOMAIN_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("试验阶段", "授权范围", "事件等级"):
        if key not in data or not isinstance(data[key], list):
            raise ValueError(f"domain.json 缺少词表：{key}")
    return data


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


TakedownAdapter = Callable[[sqlite3.Row], None]


def default_takedown_adapter(_pub: sqlite3.Row) -> None:
    """模拟外部渠道下架接口：默认成功。测试可注入会失败的实现。"""
    return None


class Core:
    def __init__(
        self,
        conn: sqlite3.Connection,
        domain: dict[str, Any] | None = None,
        clock: Callable[[], str] = _now,
        takedown_adapter: TakedownAdapter | None = None,
    ):
        self.conn = conn
        self.domain = domain or load_domain()
        self.stages = set(self.domain["试验阶段"])
        self.scopes = set(self.domain["授权范围"])
        self.levels = set(self.domain["事件等级"])
        self.now = clock
        self._takedown_adapter = takedown_adapter or default_takedown_adapter
        self.lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._worker_stop = threading.Event()

    # ---------- 基础工具 ----------

    def _validate(self, value: str, allowed: set[str], label: str, code: str):
        if value not in allowed:
            raise DomainError(code, f"{label}必须是：{'、'.join(sorted(allowed))}")

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def store_id_for_token(self, token: str) -> str | None:
        with self.lock:
            row = self._one("SELECT id FROM stores WHERE token=?", (token,))
            return row["id"] if row else None

    def package_store_id(self, package_id: str) -> str | None:
        with self.lock:
            row = self._one("SELECT store_id FROM packages WHERE id=?", (package_id,))
            return row["store_id"] if row else None

    def _store_or_404(self, store_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM stores WHERE id=?", (store_id,))
        if row is None:
            raise DomainError("store_not_found", "门店不存在", 404)
        return row

    def _owned_order(self, store_id: str, order_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM orders WHERE id=?", (order_id,))
        if row is None:
            raise DomainError("order_not_found", "订单不存在", 404)
        if row["store_id"] != store_id:
            # 隔离原则：跨店访问一律按不存在处理，不泄露存在性
            raise DomainError("order_not_found", "订单不存在", 404)
        return row

    def _funnel(self, order_id: str, store_id: str, package_id: str, event: str):
        self.conn.execute(
            "INSERT INTO funnel_events(order_id, store_id, package_id, event, created_at)"
            " VALUES(?,?,?,?,?)",
            (order_id, store_id, package_id, event, self.now()),
        )

    # ---------- 门店与员工 ----------

    def register_store(self, store_id: str, name: str, token: str) -> dict:
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO stores(id,name,token,created_at) VALUES(?,?,?,?)",
                    (store_id, name, token, self.now()),
                )
                self.conn.commit()
            except sqlite3.IntegrityError:
                raise DomainError("store_exists", "门店已存在", 409)
            return row_to_dict(self._one("SELECT * FROM stores WHERE id=?", (store_id,)))

    def register_staff(self, store_id: str, staff_id: str, name: str) -> dict:
        with self.lock:
            self._store_or_404(store_id)
            try:
                self.conn.execute(
                    "INSERT INTO staff(id,store_id,name,certified,created_at)"
                    " VALUES(?,?,?,0,?)",
                    (staff_id, store_id, name, self.now()),
                )
                self.conn.commit()
            except sqlite3.IntegrityError:
                raise DomainError("staff_exists", "员工已存在", 409)
            return row_to_dict(self._one("SELECT * FROM staff WHERE id=?", (staff_id,)))

    def certify_staff(self, staff_id: str) -> dict:
        """完成宠物摄影能力认证。认证是个人资质，跨店调班同样有效。"""
        with self.lock:
            row = self._one("SELECT * FROM staff WHERE id=?", (staff_id,))
            if row is None:
                raise DomainError("staff_not_found", "员工不存在", 404)
            if not row["certified"]:
                self.conn.execute(
                    "UPDATE staff SET certified=1, certified_at=? WHERE id=?",
                    (self.now(), staff_id),
                )
                self.conn.commit()
            return row_to_dict(self._one("SELECT * FROM staff WHERE id=?", (staff_id,)))

    # ---------- 容量 ----------

    def set_capacity(self, store_id: str, day: str, total: int) -> dict:
        with self.lock:
            self._store_or_404(store_id)
            if total < 0:
                raise DomainError("bad_capacity", "容量不能为负数")
            self.conn.execute(
                "INSERT INTO capacity(store_id,day,total) VALUES(?,?,?)"
                " ON CONFLICT(store_id,day) DO UPDATE SET total=excluded.total",
                (store_id, day, total),
            )
            self.conn.commit()
            return self.capacity_view(store_id, day)

    def capacity_view(self, store_id: str, day: str) -> dict:
        with self.lock:
            self._store_or_404(store_id)
            cap = self._one(
                "SELECT total FROM capacity WHERE store_id=? AND day=?", (store_id, day)
            )
            total = cap["total"] if cap else 0
            booked = self._one(
                "SELECT COUNT(*) c FROM orders"
                " WHERE store_id=? AND shoot_at=? AND status!=?",
                (store_id, day, ORDER_CANCELLED),
            )["c"]
            return {"store_id": store_id, "day": day, "total": total,
                    "booked": booked, "remaining": total - booked}

    # ---------- 套餐试验与版本 ----------

    def submit_package(
        self, store_id: str, name: str, config: dict | None = None,
        stage: str = "小范围开放",
    ) -> dict:
        """门店提交试验方案；同名套餐再次提交即产生新的试验小版本。"""
        with self.lock:
            self._store_or_404(store_id)
            self._validate(stage, {"提案", "小范围开放", "扩大验证"},
                           "试验阶段", "bad_stage")
            latest = self._one(
                "SELECT * FROM packages WHERE store_id=? AND name=?"
                " ORDER BY major DESC, minor DESC LIMIT 1",
                (store_id, name),
            )
            if latest is None:
                major, minor, supersedes = 1, 0, None
            elif latest["status"] == PKG_ACTIVE:
                # 正式版之后开启下一代试验
                major, minor, supersedes = latest["major"] + 1, 0, latest["id"]
            else:
                major, minor, supersedes = latest["major"], latest["minor"] + 1, latest["id"]
            pkg_id = _new_id("pkg")
            self.conn.execute(
                "INSERT INTO packages(id,store_id,name,stage,major,minor,status,"
                "supersedes,config_json,trial_start_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (pkg_id, store_id, name, stage, major, minor, PKG_TRIAL, supersedes,
                 json.dumps(config or {}, ensure_ascii=False), self.now(), self.now()),
            )
            self.conn.commit()
            return self._package_dict(pkg_id)

    def _package_or_404(self, package_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM packages WHERE id=?", (package_id,))
        if row is None:
            raise DomainError("package_not_found", "套餐版本不存在", 404)
        return row

    def _package_dict(self, package_id: str) -> dict:
        row = self._package_or_404(package_id)
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        d["version"] = f"{d['major']}.{d['minor']}"
        return d

    def transition_stage(self, package_id: str, target_stage: str) -> dict:
        """推进（或暂停）试验阶段；进入正式经营即生成生效大版本并归档旧版。"""
        with self.lock:
            pkg = self._package_or_404(package_id)
            self._validate(target_stage, self.stages, "目标阶段", "bad_stage")
            current = pkg["stage"]
            if target_stage == current:
                return self._package_dict(package_id)
            if target_stage not in STAGE_TRANSITIONS.get(current, set()):
                raise DomainError(
                    "bad_transition", f"不允许从「{current}」流转到「{target_stage}」"
                )
            if target_stage == "正式经营":
                return self._promote(pkg)
            self.conn.execute(
                "UPDATE packages SET stage=? WHERE id=?", (target_stage, package_id)
            )
            self.conn.commit()
            return self._package_dict(package_id)

    def _promote(self, pkg: sqlite3.Row) -> dict:
        """正式推广：复制方案生成下一主版本，旧试验版本全部归档。"""
        new_id = _new_id("pkg")
        self.conn.execute(
            "INSERT INTO packages(id,store_id,name,stage,major,minor,status,"
            "supersedes,config_json,trial_start_at,promo_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, pkg["store_id"], pkg["name"], "正式经营",
             pkg["major"] + 1, 0, PKG_ACTIVE, pkg["id"],
             pkg["config_json"], None, self.now(), self.now()),
        )
        self.conn.execute(
            "UPDATE packages SET status=? WHERE store_id=? AND name=? AND id!=?",
            (PKG_ARCHIVED, pkg["store_id"], pkg["name"], new_id),
        )
        self.conn.commit()
        return self._package_dict(new_id)

    def list_packages(self, store_id: str | None = None, name: str | None = None) -> list[dict]:
        with self.lock:
            sql, params = "SELECT * FROM packages", []
            if store_id:
                sql += " WHERE store_id=?"
                params.append(store_id)
            if name:
                sql += (" WHERE" if not store_id else " AND") + " name=?"
                params.append(name)
            sql += " ORDER BY created_at"
            return [self._pkg_row_to_dict(r) for r in self._all(sql, tuple(params))]

    def _pkg_row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["config"] = json.loads(d.pop("config_json"))
        d["version"] = f"{d['major']}.{d['minor']}"
        return d

    # ---------- 订单与转化漏斗 ----------

    def create_order(
        self, store_id: str, package_id: str, shoot_day: str, order_id: str | None = None
    ) -> dict:
        with self.lock:
            self._store_or_404(store_id)
            pkg = self._package_or_404(package_id)
            if pkg["store_id"] != store_id:
                raise DomainError("package_not_found", "套餐版本不存在", 404)
            if pkg["status"] == PKG_ARCHIVED:
                raise DomainError("package_archived", "该套餐版本已归档，不能接单")
            if pkg["stage"] not in ORDERABLE_STAGES:
                raise DomainError("package_not_orderable",
                                  f"套餐处于「{pkg['stage']}」阶段，暂不接单")
            cap = self._one(
                "SELECT total FROM capacity WHERE store_id=? AND day=?",
                (store_id, shoot_day),
            )
            total = cap["total"] if cap else 0
            booked = self._one(
                "SELECT COUNT(*) c FROM orders"
                " WHERE store_id=? AND shoot_at=? AND status!=?",
                (store_id, shoot_day, ORDER_CANCELLED),
            )["c"]
            if total <= booked:
                raise DomainError("capacity_full",
                                  f"{shoot_day} 容量已满（{booked}/{total}）", 409)
            order_id = order_id or _new_id("ord")
            self.conn.execute(
                "INSERT INTO orders(id,store_id,package_id,package_name,package_version,"
                "stage_snapshot,status,shoot_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (order_id, store_id, package_id, pkg["name"],
                 f"{pkg['major']}.{pkg['minor']}", pkg["stage"],
                 ORDER_BOOKED, shoot_day, self.now()),
            )
            self._funnel(order_id, store_id, package_id, "预约")
            self.conn.commit()
            return row_to_dict(self._one("SELECT * FROM orders WHERE id=?", (order_id,)))

    def cancel_order(self, store_id: str, order_id: str) -> dict:
        with self.lock:
            order = self._owned_order(store_id, order_id)
            if order["status"] != ORDER_CANCELLED:
                self.conn.execute(
                    "UPDATE orders SET status=? WHERE id=?", (ORDER_CANCELLED, order_id)
                )
                self.conn.commit()
            return row_to_dict(self._one("SELECT * FROM orders WHERE id=?", (order_id,)))

    def list_orders(self, store_id: str) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self._all(
                "SELECT * FROM orders WHERE store_id=? ORDER BY created_at", (store_id,))]

    def list_staff(self, store_id: str | None = None) -> list[dict]:
        with self.lock:
            if store_id:
                rows = self._all(
                    "SELECT id,store_id,name,certified,certified_at FROM staff"
                    " WHERE store_id=? ORDER BY id", (store_id,))
            else:
                rows = self._all(
                    "SELECT id,store_id,name,certified,certified_at FROM staff ORDER BY id")
            return [dict(r) for r in rows]

    def list_store_directory(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self._all(
                "SELECT id,name,created_at FROM stores ORDER BY id")]

    def _active_consent(self, order_id: str, scope: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM consents WHERE order_id=? AND scope=? AND revoked_at IS NULL",
            (order_id, scope),
        )

    def _require_consent(self, order_id: str, scope: str, action: str):
        if self._active_consent(order_id, scope) is None:
            raise DomainError(
                "consent_required",
                f"缺少有效的「{scope}」授权，无法{action}；请先取得顾客授权", 403,
            )

    # ---------- 分项授权 ----------

    def grant_consent(self, store_id: str, order_id: str, scope: str) -> dict:
        with self.lock:
            self._owned_order(store_id, order_id)
            self._validate(scope, self.scopes, "授权范围", "bad_scope")
            existing = self._active_consent(order_id, scope)
            if existing:
                return dict(existing)  # 授权可重复提交，保持幂等
            cid = _new_id("cse")
            self.conn.execute(
                "INSERT INTO consents(id,order_id,scope,granted,granted_at)"
                " VALUES(?,?,?,1,?)",
                (cid, order_id, scope, self.now()),
            )
            self.conn.commit()
            return dict(self._active_consent(order_id, scope))

    def revoke_consent(self, store_id: str, order_id: str, scope: str,
                       reason: str = "") -> dict:
        """撤回分项授权。

        影响分两类：
        - 尚未发布/尚未发生的环节：立即阻断（拍摄、交付、发布时强校验）；
        - 已在外部渠道挂出的内容：按受众逐渠道生成下架任务并持续追踪。
        重复撤回保持幂等。
        """
        with self.lock:
            self._owned_order(store_id, order_id)
            self._validate(scope, self.scopes, "授权范围", "bad_scope")
            active = self._active_consent(order_id, scope)
            if active is None:
                revoked = self._one(
                    "SELECT * FROM consents WHERE order_id=? AND scope=?"
                    " ORDER BY revoked_at DESC LIMIT 1",
                    (order_id, scope),
                )
                if revoked is None:
                    raise DomainError("consent_not_found", "该授权从未授予，无法撤回", 404)
                # 已撤回：幂等返回现状，但仍补齐可能遗漏的下架任务
                active = revoked
            else:
                self.conn.execute(
                    "UPDATE consents SET revoked_at=?, revoke_reason=? WHERE id=?",
                    (self.now(), reason, active["id"]),
                )
            task_ids = self._spawn_takedowns_for_scope(
                order_id, scope, f"顾客撤回「{scope}」授权" + (f"：{reason}" if reason else "")
            )
            blocked_assets = [
                r["id"] for r in self._all(
                    "SELECT ma.id FROM media_assets ma"
                    " LEFT JOIN publications p ON p.asset_id=ma.id"
                    "   AND p.status!=?"
                    " WHERE ma.order_id=? AND p.id IS NULL",
                    (PUB_TAKEN_DOWN, order_id),
                )
            ]
            self.conn.commit()
            return {
                "order_id": order_id,
                "scope": scope,
                "revoked": True,
                "revoked_at": self._one(
                    "SELECT revoked_at FROM consents WHERE id=?", (active["id"],)
                )["revoked_at"],
                "blocked_unpublished_assets": blocked_assets,
                "takedown_tasks": task_ids,
            }

    def list_consents(self, store_id: str, order_id: str) -> list[dict]:
        with self.lock:
            self._owned_order(store_id, order_id)
            return [dict(r) for r in self._all(
                "SELECT * FROM consents WHERE order_id=? ORDER BY granted_at", (order_id,))]

    def mark_shoot(self, store_id: str, order_id: str) -> dict:
        """到店拍摄：现场拍摄授权被撤回后不得开拍。"""
        with self.lock:
            order = self._owned_order(store_id, order_id)
            if order["status"] == ORDER_CANCELLED:
                raise DomainError("order_cancelled", "订单已取消", 409)
            self._require_consent(order_id, "现场拍摄", "开拍")
            if order["status"] == ORDER_BOOKED:
                self.conn.execute(
                    "UPDATE orders SET status=? WHERE id=?", (ORDER_SHOT, order_id)
                )
                self._funnel(order_id, store_id, order["package_id"], "到店拍摄")
                self.conn.commit()
            return row_to_dict(self._one("SELECT * FROM orders WHERE id=?", (order_id,)))

    def deliver_order(self, store_id: str, order_id: str) -> dict:
        """成片交付：须先完成拍摄；成片交付授权被撤回后停止交付；已交付的不追回。"""
        with self.lock:
            order = self._owned_order(store_id, order_id)
            if order["status"] == ORDER_BOOKED:
                raise DomainError("not_shot_yet", "订单尚未完成拍摄，不能交付", 409)
            self._require_consent(order_id, "成片交付", "交付成片")
            if order["status"] != ORDER_DELIVERED and order["status"] != ORDER_CANCELLED:
                self.conn.execute(
                    "UPDATE orders SET status=? WHERE id=?", (ORDER_DELIVERED, order_id)
                )
                self._funnel(order_id, store_id, order["package_id"], "成片交付")
                self.conn.commit()
            return row_to_dict(self._one("SELECT * FROM orders WHERE id=?", (order_id,)))

    # ---------- 成片、发布与下架 ----------

    def create_asset(self, store_id: str, order_id: str) -> dict:
        with self.lock:
            self._owned_order(store_id, order_id)
            aid = _new_id("ast")
            self.conn.execute(
                "INSERT INTO media_assets(id,order_id,store_id,created_at)"
                " VALUES(?,?,?,?)",
                (aid, order_id, store_id, self.now()),
            )
            self.conn.commit()
            return dict(self._one("SELECT * FROM media_assets WHERE id=?", (aid,)))

    def publish_asset(self, store_id: str, asset_id: str, channel: str,
                      audience: str, external_ref: str | None = None) -> dict:
        """挂出成片。授权已撤回（且未重新授权）的内容一律不得发布。"""
        with self.lock:
            asset = self._one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
            if asset is None or asset["store_id"] != store_id:
                raise DomainError("asset_not_found", "成片不存在", 404)
            if audience not in ("公开", "店内"):
                raise DomainError("bad_audience", "受众只能是 公开/店内")
            scope = "公开传播" if audience == "公开" else "门店展示"
            self._require_consent(asset["order_id"], scope, f"在{channel}发布")
            dup = self._one(
                "SELECT * FROM publications WHERE asset_id=? AND channel=? AND audience=?"
                " AND status!=?",
                (asset_id, channel, audience, PUB_TAKEN_DOWN),
            )
            if dup is not None:
                raise DomainError("already_published",
                                  f"该成片已在{channel}发布且未下架", 409)
            pid = _new_id("pub")
            self.conn.execute(
                "INSERT INTO publications(id,asset_id,store_id,channel,audience,"
                "external_ref,status,published_at) VALUES(?,?,?,?,?,?,?,?)",
                (pid, asset_id, store_id, channel, audience, external_ref,
                 PUB_PUBLISHED, self.now()),
            )
            if audience == "公开":
                self._funnel(asset["order_id"], store_id,
                             self._one("SELECT package_id FROM orders WHERE id=?",
                                       (asset["order_id"],))["package_id"],
                             "公开发布")
            self.conn.commit()
            return dict(self._one("SELECT * FROM publications WHERE id=?", (pid,)))

    def _spawn_takedowns_for_scope(self, order_id: str, scope: str,
                                   reason: str) -> list[str]:
        """对仍挂在相应受众渠道的发布逐一生成下架任务（已存在未完成任务则不重复）。"""
        audience = SCOPE_AUDIENCE.get(scope)
        if audience is None:
            return []
        task_ids: list[str] = []
        pubs = self._all(
            "SELECT p.* FROM publications p JOIN media_assets ma ON p.asset_id=ma.id"
            " WHERE ma.order_id=? AND p.audience=? AND p.status!=?",
            (order_id, audience, PUB_TAKEN_DOWN),
        )
        for pub in pubs:
            existing = self._one(
                "SELECT id FROM takedown_tasks WHERE publication_id=? AND status!=?",
                (pub["id"], TASK_DONE),
            )
            if existing:
                task_ids.append(existing["id"])
                continue
            tid = _new_id("tdn")
            self.conn.execute(
                "UPDATE publications SET status=?, takedown_started_at=? WHERE id=?",
                (PUB_TAKING_DOWN, self.now(), pub["id"]),
            )
            self.conn.execute(
                "INSERT INTO takedown_tasks(id,publication_id,store_id,reason,status,"
                "attempts,created_at,updated_at) VALUES(?,?,?,?,?,0,?,?)",
                (tid, pub["id"], pub["store_id"], reason, TASK_PENDING,
                 self.now(), self.now()),
            )
            task_ids.append(tid)
        return task_ids

    def request_takedown(self, store_id: str, publication_id: str,
                         reason: str) -> dict:
        """非撤权原因（投诉、自查等）手动要求下架。"""
        with self.lock:
            pub = self._one("SELECT * FROM publications WHERE id=?", (publication_id,))
            if pub is None or pub["store_id"] != store_id:
                raise DomainError("publication_not_found", "发布记录不存在", 404)
            if pub["status"] == PUB_TAKEN_DOWN:
                return {"takedown_tasks": [], "note": "该内容已下架"}
            existing = self._one(
                "SELECT id FROM takedown_tasks WHERE publication_id=? AND status!=?",
                (publication_id, TASK_DONE),
            )
            if existing:
                return {"takedown_tasks": [existing["id"]], "note": "下架任务已存在"}
            tid = _new_id("tdn")
            self.conn.execute(
                "UPDATE publications SET status=?, takedown_started_at=? WHERE id=?",
                (PUB_TAKING_DOWN, self.now(), publication_id),
            )
            self.conn.execute(
                "INSERT INTO takedown_tasks(id,publication_id,store_id,reason,status,"
                "attempts,created_at,updated_at) VALUES(?,?,?,?,?,0,?,?)",
                (tid, publication_id, store_id, reason, TASK_PENDING,
                 self.now(), self.now()),
            )
            self.conn.commit()
            return {"takedown_tasks": [tid]}

    def list_takedowns(self, store_id: str | None = None) -> list[dict]:
        with self.lock:
            if store_id:
                rows = self._all(
                    "SELECT * FROM takedown_tasks WHERE store_id=? ORDER BY created_at",
                    (store_id,))
            else:
                rows = self._all(
                    "SELECT * FROM takedown_tasks ORDER BY created_at")
            return [dict(r) for r in rows]

    def list_publications(self, store_id: str | None = None) -> list[dict]:
        with self.lock:
            if store_id:
                rows = self._all(
                    "SELECT * FROM publications WHERE store_id=? ORDER BY published_at",
                    (store_id,))
            else:
                rows = self._all(
                    "SELECT * FROM publications ORDER BY published_at")
            return [dict(r) for r in rows]

    def process_takedown_once(self) -> dict | None:
        """推进一条待下架任务：调外部渠道、成功则闭环，失败则留痕重试。"""
        with self.lock:
            task = self._one(
                "SELECT * FROM takedown_tasks WHERE status=? ORDER BY created_at LIMIT 1",
                (TASK_PENDING,),
            )
            if task is None:
                return None
            self.conn.execute(
                "UPDATE takedown_tasks SET status=?, updated_at=? WHERE id=?",
                (TASK_PROCESSING, self.now(), task["id"]),
            )
            self.conn.commit()
            pub = self._one("SELECT * FROM publications WHERE id=?",
                            (task["publication_id"],))
            try:
                self._takedown_adapter(pub)
            except Exception as exc:  # 外部渠道暂时失败：回到队列稍后重试
                self.conn.execute(
                    "UPDATE takedown_tasks SET status=?, attempts=attempts+1,"
                    " last_error=?, updated_at=? WHERE id=?",
                    (TASK_PENDING, str(exc), self.now(), task["id"]),
                )
                self.conn.commit()
                return {"task_id": task["id"], "result": "retry", "error": str(exc)}
            self.conn.execute(
                "UPDATE takedown_tasks SET status=?, updated_at=? WHERE id=?",
                (TASK_DONE, self.now(), task["id"]),
            )
            self.conn.execute(
                "UPDATE publications SET status=?, taken_down_at=? WHERE id=?",
                (PUB_TAKEN_DOWN, self.now(), pub["id"]),
            )
            self.conn.commit()
            return {"task_id": task["id"], "result": "done",
                    "publication_id": pub["id"], "channel": pub["channel"]}

    def start_worker(self, interval: float = 0.2) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._worker_stop.clear()

        def loop():
            while not self._worker_stop.is_set():
                result = None
                try:
                    result = self.process_takedown_once()
                except Exception:
                    result = None
                self._worker_stop.wait(interval if result is None else 0)

        self._worker = threading.Thread(target=loop, name="takedown-worker", daemon=True)
        self._worker.start()

    def stop_worker(self) -> None:
        self._worker_stop.set()
        if self._worker:
            self._worker.join(timeout=2)

    # ---------- 现场事件处置链 ----------

    def report_incident(self, store_id: str, level: str, title: str,
                        detail: dict | None = None, order_id: str | None = None,
                        client_ref: str | None = None,
                        occurred_at: str | None = None) -> dict:
        """报送宠物应激/现场事故。同一事故重复报送会合并到既有处置链。"""
        with self.lock:
            self._store_or_404(store_id)
            self._validate(level, self.levels, "事件等级", "bad_level")
            if order_id is not None:
                self._owned_order(store_id, order_id)
            ts = self.now()
            existing = None
            if client_ref:
                existing = self._one(
                    "SELECT * FROM incidents WHERE store_id=? AND client_ref=?",
                    (store_id, client_ref),
                )
            if existing is None and order_id is not None:
                # 内容指纹兜底：短时间内同店同单同等级同标题的未闭环事件视为重复
                since = (datetime.fromisoformat(self.now()) - DUP_WINDOW).isoformat()
                existing = self._one(
                    "SELECT * FROM incidents WHERE store_id=? AND order_id=? AND level=?"
                    " AND title=? AND status!=? AND created_at>=?",
                    (store_id, order_id, level, title, INC_CLOSED, since),
                )
            if existing is not None:
                self._incident_log(existing["id"], "重复报送已合并",
                                   f"等级={level}；client_ref={client_ref or '-'}",
                                   f"store:{store_id}")
                self.conn.commit()
                d = self._incident_dict(existing["id"])
                d["duplicate"] = True
                return d
            iid = _new_id("inc")
            self.conn.execute(
                "INSERT INTO incidents(id,store_id,order_id,level,title,detail_json,"
                "status,client_ref,occurred_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (iid, store_id, order_id, level, title,
                 json.dumps(detail or {}, ensure_ascii=False), INC_NEW,
                 client_ref, occurred_at or ts, ts),
            )
            self._incident_log(iid, "事件接收", f"等级={level}", f"store:{store_id}")
            self.conn.commit()
            d = self._incident_dict(iid)
            d["duplicate"] = False
            return d

    def _incident_log(self, incident_id: str, action: str, note: str, actor: str):
        self.conn.execute(
            "INSERT INTO incident_logs(incident_id,action,note,actor,created_at)"
            " VALUES(?,?,?,?,?)",
            (incident_id, action, note, actor, self.now()),
        )

    def _incident_or_404(self, incident_id: str, store_id: str | None = None
                         ) -> sqlite3.Row:
        row = self._one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if row is None or (store_id is not None and row["store_id"] != store_id):
            raise DomainError("incident_not_found", "事件不存在", 404)
        return row

    def _incident_dict(self, incident_id: str) -> dict:
        row = self._incident_or_404(incident_id)
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json"))
        d["sla_hours"] = SLA_HOURS.get(d["level"])
        if d["status"] != INC_CLOSED and d["sla_hours"]:
            deadline = datetime.fromisoformat(d["created_at"]) + timedelta(
                hours=d["sla_hours"])
            d["overdue"] = datetime.fromisoformat(self.now()) > deadline
        else:
            d["overdue"] = False
        d["logs"] = [dict(r) for r in self._all(
            "SELECT action,note,actor,created_at FROM incident_logs"
            " WHERE incident_id=? ORDER BY id", (incident_id,))]
        return d

    def _advance_incident(self, incident_id: str, allowed: set[str], new_status: str,
                          action: str, note: str, actor: str) -> dict:
        row = self._one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if row is None:
            raise DomainError("incident_not_found", "事件不存在", 404)
        if row["status"] not in allowed:
            raise DomainError(
                "bad_incident_transition",
                f"事件当前为「{row['status']}」，不能执行{action}", 409,
            )
        self.conn.execute("UPDATE incidents SET status=? WHERE id=?",
                          (new_status, incident_id))
        if new_status == INC_CLOSED:
            self.conn.execute("UPDATE incidents SET closed_at=? WHERE id=?",
                              (self.now(), incident_id))
        self._incident_log(incident_id, action, note, actor)
        self.conn.commit()
        return self._incident_dict(incident_id)

    def ack_incident(self, store_id: str, incident_id: str, owner: str) -> dict:
        """门店认领并开始处置。"""
        with self.lock:
            self._incident_or_404(incident_id, store_id)
            row = self._incident_or_404(incident_id, store_id)
            if row["status"] not in {INC_NEW, INC_IN_PROGRESS}:
                raise DomainError("bad_incident_transition",
                                  f"事件当前为「{row['status']}」", 409)
            self.conn.execute(
                "UPDATE incidents SET status=?, owner=? WHERE id=?",
                (INC_IN_PROGRESS, owner, incident_id),
            )
            self._incident_log(incident_id, "认领处置", f"负责人={owner}",
                               f"store:{store_id}")
            self.conn.commit()
            return self._incident_dict(incident_id)

    def resolve_incident(self, store_id: str, incident_id: str, note: str) -> dict:
        """门店完成处置，提交管理者复核；复核通过才闭环。"""
        with self.lock:
            self._incident_or_404(incident_id, store_id)
            return self._advance_incident(
                incident_id, {INC_IN_PROGRESS, INC_NEW}, INC_REVIEW,
                "提交复核", note, f"store:{store_id}")

    def review_incident(self, incident_id: str, approve: bool, note: str,
                        actor: str = "admin") -> dict:
        """管理者复核：通过则闭环，驳回则退回处置。"""
        with self.lock:
            if approve:
                return self._advance_incident(
                    incident_id, {INC_REVIEW}, INC_CLOSED,
                    "复核通过闭环", note, actor)
            row = self._incident_or_404(incident_id)
            if row["status"] != INC_REVIEW:
                raise DomainError("bad_incident_transition",
                                  f"事件当前为「{row['status']}」，不能复核", 409)
            self.conn.execute(
                "UPDATE incidents SET status=? WHERE id=?", (INC_IN_PROGRESS, incident_id)
            )
            self._incident_log(incident_id, "复核驳回", note, actor)
            self.conn.commit()
            return self._incident_dict(incident_id)

    def list_incidents(self, store_id: str | None = None,
                       include_closed: bool = True) -> list[dict]:
        with self.lock:
            sql = "SELECT id FROM incidents"
            conditions = []
            params: list[Any] = []
            if store_id:
                conditions.append("store_id=?")
                params.append(store_id)
            if not include_closed:
                conditions.append("status!=?")
                params.append(INC_CLOSED)
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at"
            return [self._incident_dict(r["id"]) for r in self._all(sql, tuple(params))]

    # ---------- 排班与跨店调班 ----------

    def schedule_staff(self, store_id: str, staff_id: str, day: str, slot: str,
                       order_id: str | None = None) -> dict:
        """排班；只有通过认证的员工可排班，home_store 与服务门店不同即跨店调班。"""
        with self.lock:
            self._store_or_404(store_id)
            staff = self._one("SELECT * FROM staff WHERE id=?", (staff_id,))
            if staff is None:
                raise DomainError("staff_not_found", "员工不存在", 404)
            if not staff["certified"]:
                raise DomainError("not_certified",
                                  "员工尚未通过宠物摄影能力认证，不能排班", 403)
            if order_id is not None:
                self._owned_order(store_id, order_id)
            clash = self._one(
                "SELECT id FROM schedules WHERE staff_id=? AND day=? AND slot=?"
                " AND status='已排'",
                (staff_id, day, slot),
            )
            if clash:
                raise DomainError("schedule_conflict",
                                  f"该员工 {day} {slot} 已有排班", 409)
            sid = _new_id("sch")
            self.conn.execute(
                "INSERT INTO schedules(id,store_id,staff_id,home_store_id,day,slot,"
                "status,order_id,created_at) VALUES(?,?,?,?,?,?,'已排',?,?)",
                (sid, store_id, staff_id, staff["store_id"], day, slot,
                 order_id, self.now()),
            )
            self.conn.commit()
            d = dict(self._one("SELECT * FROM schedules WHERE id=?", (sid,)))
            d["cross_store"] = d["home_store_id"] != d["store_id"]
            return d

    def cancel_schedule(self, store_id: str, schedule_id: str) -> dict:
        with self.lock:
            row = self._one("SELECT * FROM schedules WHERE id=?", (schedule_id,))
            if row is None or row["store_id"] != store_id:
                raise DomainError("schedule_not_found", "排班不存在", 404)
            self.conn.execute(
                "UPDATE schedules SET status='已取消' WHERE id=?", (schedule_id,))
            self.conn.commit()
            return dict(self._one("SELECT * FROM schedules WHERE id=?", (schedule_id,)))

    def list_schedules(self, store_id: str | None = None, day: str | None = None,
                       cross_store_only: bool = False) -> list[dict]:
        with self.lock:
            sql = "SELECT * FROM schedules WHERE 1=1"
            params: list[Any] = []
            if store_id:
                sql += " AND store_id=?"
                params.append(store_id)
            if day:
                sql += " AND day=?"
                params.append(day)
            if cross_store_only:
                sql += " AND home_store_id!=store_id"
            sql += " ORDER BY day, slot"
            rows = self._all(sql, tuple(params))
            out = []
            for r in rows:
                d = dict(r)
                d["cross_store"] = d["home_store_id"] != d["store_id"]
                out.append(d)
            return out

    # ---------- 重启恢复 ----------

    def recover(self) -> dict:
        """重启后恢复：处理中的下架任务回到队列，处理中的事件退回待响应重新认领。"""
        with self.lock:
            stuck_tasks = self._all(
                "SELECT id FROM takedown_tasks WHERE status=?", (TASK_PROCESSING,))
            for t in stuck_tasks:
                self.conn.execute(
                    "UPDATE takedown_tasks SET status=?, updated_at=? WHERE id=?",
                    (TASK_PENDING, self.now(), t["id"]),
                )
            stuck_incidents = self._all(
                "SELECT id FROM incidents WHERE status=?", (INC_IN_PROGRESS,))
            for i in stuck_incidents:
                self.conn.execute(
                    "UPDATE incidents SET status=? WHERE id=?", (INC_NEW, i["id"]))
                self._incident_log(i["id"], "重启恢复",
                                   "服务重启，事件退回待响应以继续推进", "系统")
            self.conn.commit()
            return {"requeued_takedowns": len(stuck_tasks),
                    "reopened_incidents": len(stuck_incidents)}

    # ---------- 管理者：版本对比 ----------

    def comparison(self) -> dict:
        """跨店、跨套餐版本的真实转化与风险对比（管理者视角）。"""
        with self.lock:
            packages = [self._pkg_row_to_dict(r)
                        for r in self._all("SELECT * FROM packages ORDER BY created_at")]
            result = []
            for p in packages:
                pid = p["id"]
                order_rows = self._all(
                    "SELECT id,status FROM orders WHERE package_id=?", (pid,))
                order_ids = [r["id"] for r in order_rows]
                valid_orders = [r["id"] for r in order_rows
                                if r["status"] != ORDER_CANCELLED]
                n_booked = len(order_ids)
                n_valid = len(valid_orders)

                def reached(event: str) -> int:
                    if not valid_orders:
                        return 0
                    placeholders = ",".join("?" * len(valid_orders))
                    return self._one(
                        f"SELECT COUNT(DISTINCT order_id) c FROM funnel_events"
                        f" WHERE event=? AND order_id IN ({placeholders})",
                        (event, *valid_orders),
                    )["c"]

                n_shot = reached("到店拍摄")
                n_delivered = reached("成片交付")
                n_published = reached("公开发布")
                if valid_orders:
                    placeholders = ",".join("?" * len(valid_orders))
                    incidents = self._all(
                        f"SELECT level, COUNT(*) c FROM incidents"
                        f" WHERE order_id IN ({placeholders}) GROUP BY level",
                        tuple(valid_orders),
                    )
                    pub_rows = self._all(
                        "SELECT p.status, COUNT(*) c FROM publications p"
                        " JOIN media_assets ma ON p.asset_id=ma.id"
                        f" WHERE ma.order_id IN ({placeholders}) GROUP BY p.status",
                        tuple(valid_orders),
                    )
                    revoked = self._one(
                        f"SELECT COUNT(*) c FROM consents"
                        f" WHERE revoked_at IS NOT NULL AND order_id IN ({placeholders})",
                        tuple(valid_orders),
                    )["c"]
                else:
                    incidents, pub_rows, revoked = [], [], 0
                level_counts = {r["level"]: r["c"] for r in incidents}
                pub_counts = {r["status"]: r["c"] for r in pub_rows}
                result.append({
                    "package_id": pid,
                    "store_id": p["store_id"],
                    "name": p["name"],
                    "version": p["version"],
                    "stage": p["stage"],
                    "status": p["status"],
                    "orders": n_booked,
                    "valid_orders": n_valid,
                    "shot": n_shot,
                    "delivered": n_delivered,
                    "publicly_published": n_published,
                    "shoot_rate": round(n_shot / n_valid, 4) if n_valid else None,
                    "delivery_rate": round(n_delivered / n_shot, 4) if n_shot else None,
                    "publish_rate": round(n_published / n_delivered, 4)
                    if n_delivered else None,
                    "incidents_by_level": level_counts,
                    "incidents_total": sum(level_counts.values()),
                    "serious_incidents": level_counts.get("严重事故", 0)
                    + level_counts.get("安全关注", 0),
                    "publications_by_status": pub_counts,
                    "taken_down": pub_counts.get(PUB_TAKEN_DOWN, 0),
                    "revoked_consents": revoked,
                })
            return {"generated_at": self.now(), "packages": result}
