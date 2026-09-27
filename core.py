"""核心业务逻辑：试验方案、授权、下架、事件处置、排班与经营分析。

所有写操作通过 Core 的方法完成，鉴权由 Actor 统一约束：
- 总部管理者（manager）可见并操作全部门店；
- 门店角色（store）只能接触本店数据。
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import domain


class DomainError(Exception):
    """业务错误，status 对应 HTTP 状态码。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class BadRequest(DomainError):
    def __init__(self, message):
        super().__init__(400, message)


class Forbidden(DomainError):
    def __init__(self, message="门店只能接触本店业务数据"):
        super().__init__(403, message)


class NotFound(DomainError):
    def __init__(self, message="资源不存在"):
        super().__init__(404, message)


class Conflict(DomainError):
    def __init__(self, message):
        super().__init__(409, message)


@dataclass
class Actor:
    """请求操作者。role 为 manager（总部）或 store（门店）。"""

    role: str = "manager"
    store_id: int | None = None
    name: str = "system"

    @property
    def is_manager(self):
        return self.role == "manager"

    def check_store(self, store_id):
        """门店角色操作具体门店资源前校验归属。"""
        if not self.is_manager and self.store_id != store_id:
            raise Forbidden()

    def scoped_store_id(self, requested=None):
        """列表查询的门店过滤：门店角色强制本店，总部可指定或查看全部。"""
        if self.is_manager:
            return requested
        if requested is not None and int(requested) != self.store_id:
            raise Forbidden()
        return self.store_id


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dict(row):
    return dict(row) if row is not None else None


def _dicts(rows):
    return [dict(r) for r in rows]


class Core:
    def __init__(self, conn):
        self.conn = conn

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------

    def _get(self, table, item_id, label):
        row = self.conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"{label}不存在: {item_id}")
        return row

    def get_store(self, store_id):
        return _dict(self._get("stores", store_id, "门店"))

    def get_plan(self, plan_id):
        return _dict(self._get("plans", plan_id, "试验方案"))

    def get_order(self, order_id):
        return _dict(self._get("orders", order_id, "订单"))

    def get_employee(self, employee_id):
        emp = _dict(self._get("employees", employee_id, "员工"))
        emp["certifications"] = _dicts(
            self.conn.execute(
                "SELECT * FROM certifications WHERE employee_id = ?"
                " ORDER BY completed_at DESC",
                (employee_id,),
            ).fetchall()
        )
        return emp

    # ------------------------------------------------------------------
    # 门店
    # ------------------------------------------------------------------

    def create_store(self, actor, name):
        if not actor.is_manager:
            raise Forbidden("仅总部可创建门店")
        if not name:
            raise BadRequest("门店名称不能为空")
        now = _now()
        cur = self.conn.execute(
            "INSERT INTO stores(name, created_at) VALUES (?, ?)", (name, now)
        )
        self.conn.commit()
        return self.get_store(cur.lastrowid)

    def list_stores(self, actor):
        if actor.is_manager:
            rows = self.conn.execute("SELECT * FROM stores ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM stores WHERE id = ?", (actor.store_id,)
            ).fetchall()
        return _dicts(rows)

    # ------------------------------------------------------------------
    # 试验方案与容量
    # ------------------------------------------------------------------

    def create_plan(self, actor, store_id, name, capacity):
        store_id = int(store_id)
        actor.check_store(store_id)
        self.get_store(store_id)
        if not name:
            raise BadRequest("方案名称不能为空")
        capacity = int(capacity)
        if capacity < 1:
            raise BadRequest("容量至少为 1（同时在途订单上限）")
        now = _now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO plans(store_id, name, capacity, phase, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (store_id, name, capacity, domain.PHASE_PROPOSAL, now, now),
            )
            self.conn.execute(
                "INSERT INTO plan_phase_log(plan_id, from_phase, to_phase, reason, actor, at)"
                " VALUES (?, NULL, ?, ?, ?, ?)",
                (cur.lastrowid, domain.PHASE_PROPOSAL, "门店提交试验方案", actor.name, now),
            )
        return self.get_plan(cur.lastrowid)

    def transition_plan(self, actor, plan_id, to_phase, reason=None):
        plan = self._get("plans", plan_id, "试验方案")
        actor.check_store(plan["store_id"])
        if to_phase not in domain.PHASES:
            raise BadRequest(f"未知试验阶段: {to_phase}，可选: {domain.PHASES}")
        from_phase = plan["phase"]
        if to_phase == from_phase:
            raise Conflict(f"方案已处于 {from_phase}")
        allowed = domain.PHASE_TRANSITIONS.get(from_phase, set())
        if to_phase not in allowed:
            raise Conflict(f"不允许从 {from_phase} 直接切换到 {to_phase}")
        # 门店只能对本店方案踩“暂停”刹车，推进与恢复由总部把关。
        if not actor.is_manager and to_phase != domain.PHASE_PAUSED:
            raise Forbidden("阶段推进与恢复由总部管理者操作，门店可暂停本店方案")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE plans SET phase = ?, updated_at = ? WHERE id = ?",
                (to_phase, now, plan_id),
            )
            self.conn.execute(
                "INSERT INTO plan_phase_log(plan_id, from_phase, to_phase, reason, actor, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, from_phase, to_phase, reason, actor.name, now),
            )
        return self.get_plan(plan_id)

    def list_plans(self, actor, store_id=None, phase=None):
        store_id = actor.scoped_store_id(store_id)
        sql = "SELECT * FROM plans"
        cond, args = [], []
        if store_id is not None:
            cond.append("store_id = ?")
            args.append(int(store_id))
        if phase:
            cond.append("phase = ?")
            args.append(phase)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id"
        return _dicts(self.conn.execute(sql, args).fetchall())

    def plan_phase_history(self, actor, plan_id):
        plan = self._get("plans", plan_id, "试验方案")
        actor.check_store(plan["store_id"])
        return _dicts(
            self.conn.execute(
                "SELECT * FROM plan_phase_log WHERE plan_id = ? ORDER BY id", (plan_id,)
            ).fetchall()
        )

    # ------------------------------------------------------------------
    # 套餐版本
    # ------------------------------------------------------------------

    def add_version(self, actor, plan_id, label, price_cents=0):
        plan = self._get("plans", plan_id, "试验方案")
        actor.check_store(plan["store_id"])
        if not label:
            raise BadRequest("版本名称不能为空")
        cur = self.conn.execute(
            "INSERT INTO package_versions(plan_id, label, price_cents, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (plan_id, label, int(price_cents), domain.VERSION_DRAFT, _now()),
        )
        self.conn.commit()
        return _dict(self._get("package_versions", cur.lastrowid, "套餐版本"))

    def activate_version(self, actor, version_id):
        version = self._get("package_versions", version_id, "套餐版本")
        plan = self._get("plans", version["plan_id"], "试验方案")
        actor.check_store(plan["store_id"])
        if version["status"] == domain.VERSION_ACTIVE:
            raise Conflict("该版本已是在售版本")
        with self.conn:
            # 同方案其他在售版本转为停售，保证任一时刻只有一个在售版本。
            self.conn.execute(
                "UPDATE package_versions SET status = ? WHERE plan_id = ? AND status = ?",
                (domain.VERSION_RETIRED, plan["id"], domain.VERSION_ACTIVE),
            )
            self.conn.execute(
                "UPDATE package_versions SET status = ? WHERE id = ?",
                (domain.VERSION_ACTIVE, version_id),
            )
        return _dict(self._get("package_versions", version_id, "套餐版本"))

    def list_versions(self, actor, plan_id):
        plan = self._get("plans", plan_id, "试验方案")
        actor.check_store(plan["store_id"])
        return _dicts(
            self.conn.execute(
                "SELECT * FROM package_versions WHERE plan_id = ? ORDER BY id", (plan_id,)
            ).fetchall()
        )

    # ------------------------------------------------------------------
    # 员工、能力认证与排班（含跨店调班）
    # ------------------------------------------------------------------

    def create_employee(self, actor, name, home_store_id):
        home_store_id = int(home_store_id)
        actor.check_store(home_store_id)
        self.get_store(home_store_id)
        if not name:
            raise BadRequest("员工姓名不能为空")
        cur = self.conn.execute(
            "INSERT INTO employees(name, home_store_id, created_at) VALUES (?, ?, ?)",
            (name, home_store_id, _now()),
        )
        self.conn.commit()
        return self.get_employee(cur.lastrowid)

    def add_certification(self, actor, employee_id, skill=None, completed_at=None, expires_at=None):
        if not actor.is_manager:
            raise Forbidden("能力认证由总部统一核发")
        self._get("employees", employee_id, "员工")
        skill = skill or domain.REQUIRED_SKILL
        completed_at = completed_at or _now()[:10]
        cur = self.conn.execute(
            "INSERT INTO certifications(employee_id, skill, completed_at, expires_at)"
            " VALUES (?, ?, ?, ?)",
            (employee_id, skill, completed_at, expires_at),
        )
        self.conn.commit()
        return _dict(self._get("certifications", cur.lastrowid, "认证记录"))

    def _has_valid_cert(self, employee_id, on_date):
        rows = self.conn.execute(
            "SELECT * FROM certifications WHERE employee_id = ? AND skill = ?",
            (employee_id, domain.REQUIRED_SKILL),
        ).fetchall()
        for row in rows:
            if row["completed_at"] > on_date:
                continue
            if row["expires_at"] and row["expires_at"] < on_date:
                continue
            return True
        return False

    def create_assignment(self, actor, employee_id, store_id, work_date, shift):
        """排班。跨店调班（门店与员工所属门店不同）仅总部可协调。"""
        store_id = int(store_id)
        actor.check_store(store_id)
        emp = self._get("employees", employee_id, "员工")
        self.get_store(store_id)
        if not work_date or not shift:
            raise BadRequest("排班需要日期与班次")
        if not self._has_valid_cert(employee_id, work_date):
            raise Conflict(
                f"员工 {emp['name']} 在 {work_date} 未持有有效{domain.REQUIRED_SKILL}，不能排班"
            )
        cross = 1 if emp["home_store_id"] != store_id else 0
        if cross and not actor.is_manager:
            raise Forbidden("跨店调班由总部管理者统一协调")
        cur = self.conn.execute(
            "INSERT INTO assignments(employee_id, store_id, work_date, shift, cross_store, actor, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (employee_id, store_id, work_date, shift, cross, actor.name, _now()),
        )
        self.conn.commit()
        return _dict(self._get("assignments", cur.lastrowid, "排班"))

    def list_assignments(self, actor, store_id=None, work_date=None):
        store_id = actor.scoped_store_id(store_id)
        sql = "SELECT * FROM assignments"
        cond, args = [], []
        if store_id is not None:
            cond.append("store_id = ?")
            args.append(int(store_id))
        if work_date:
            cond.append("work_date = ?")
            args.append(work_date)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY work_date, id"
        return _dicts(self.conn.execute(sql, args).fetchall())

    # ------------------------------------------------------------------
    # 订单与套餐版本切换
    # ------------------------------------------------------------------

    def create_order(self, actor, store_id, plan_id, version_id,
                     customer_name, pet_name, pet_species=None):
        store_id = int(store_id)
        actor.check_store(store_id)
        plan = self._get("plans", plan_id, "试验方案")
        if plan["store_id"] != store_id:
            raise BadRequest("方案不属于该门店")
        if plan["phase"] not in domain.ORDERABLE_PHASES:
            raise Conflict(f"方案处于 {plan['phase']} 阶段，暂不接单")
        version = self._get("package_versions", version_id, "套餐版本")
        if version["plan_id"] != plan["id"]:
            raise BadRequest("套餐版本不属于该方案")
        if version["status"] != domain.VERSION_ACTIVE:
            raise Conflict("仅可下单在售套餐版本")
        active = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM orders WHERE plan_id = ? AND status IN"
            f" ({','.join('?' * len(domain.ACTIVE_ORDER_STATUSES))})",
            (plan["id"], *domain.ACTIVE_ORDER_STATUSES),
        ).fetchone()["n"]
        if active >= plan["capacity"]:
            raise Conflict(f"方案容量已满（{plan['capacity']} 单在途），请排队或扩容")
        if not customer_name or not pet_name:
            raise BadRequest("需要顾客与宠物信息")
        now = _now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO orders(store_id, plan_id, version_id, customer_name, pet_name,"
                " pet_species, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, '待拍摄', ?, ?)",
                (store_id, plan["id"], version_id, customer_name, pet_name, pet_species, now, now),
            )
            self.conn.execute(
                "INSERT INTO order_version_log(order_id, from_version_id, to_version_id, reason, actor, at)"
                " VALUES (?, NULL, ?, ?, ?, ?)",
                (cur.lastrowid, version_id, "下单", actor.name, now),
            )
        return self.get_order(cur.lastrowid)

    def _consent_granted(self, order_id, scope):
        row = self.conn.execute(
            "SELECT status FROM consents WHERE order_id = ? AND scope = ?",
            (order_id, scope),
        ).fetchone()
        return row is not None and row["status"] == "granted"

    def advance_order(self, actor, order_id, to_status):
        order = self._get("orders", order_id, "订单")
        actor.check_store(order["store_id"])
        from_status = order["status"]
        allowed = domain.ORDER_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            raise Conflict(f"订单不允许从 {from_status} 推进到 {to_status}")
        # 拍摄与交付分别受对应授权约束，撤回授权即阻断推进。
        if to_status == "拍摄完成" and not self._consent_granted(order_id, domain.SCOPE_SHOOT):
            raise Conflict(f"顾客未授权「{domain.SCOPE_SHOOT}」，不能拍摄")
        if to_status == "已交付" and not self._consent_granted(order_id, domain.SCOPE_DELIVER):
            raise Conflict(f"顾客未授权「{domain.SCOPE_DELIVER}」，不能交付成片")
        self.conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (to_status, _now(), order_id),
        )
        self.conn.commit()
        return self.get_order(order_id)

    def switch_version(self, actor, order_id, to_version_id, reason=None):
        """套餐版本切换：同方案内切到在售版本，全程留痕。"""
        order = self._get("orders", order_id, "订单")
        actor.check_store(order["store_id"])
        if order["status"] in ("已交付", "已完成", "已取消"):
            raise Conflict(f"订单已{order['status']}，不能切换套餐版本")
        target = self._get("package_versions", to_version_id, "套餐版本")
        if target["plan_id"] != order["plan_id"]:
            raise BadRequest("只能切换到同一方案下的套餐版本")
        if target["status"] != domain.VERSION_ACTIVE:
            raise Conflict("目标版本不在售，不能切换")
        if target["id"] == order["version_id"]:
            raise Conflict("订单已在该版本上")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE orders SET version_id = ?, updated_at = ? WHERE id = ?",
                (target["id"], now, order_id),
            )
            self.conn.execute(
                "INSERT INTO order_version_log(order_id, from_version_id, to_version_id, reason, actor, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, order["version_id"], target["id"], reason, actor.name, now),
            )
        return self.get_order(order_id)

    def list_orders(self, actor, store_id=None, status=None):
        store_id = actor.scoped_store_id(store_id)
        sql = "SELECT * FROM orders"
        cond, args = [], []
        if store_id is not None:
            cond.append("store_id = ?")
            args.append(int(store_id))
        if status:
            cond.append("status = ?")
            args.append(status)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id"
        return _dicts(self.conn.execute(sql, args).fetchall())

    # ------------------------------------------------------------------
    # 分项授权：拍摄 / 交付 / 展示 / 传播，可分别授予与撤回
    # ------------------------------------------------------------------

    def set_consent(self, actor, order_id, scope, action):
        order = self._get("orders", order_id, "订单")
        actor.check_store(order["store_id"])
        if scope not in domain.SCOPES:
            raise BadRequest(f"未知授权范围: {scope}，可选: {domain.SCOPES}")
        if action not in ("grant", "withdraw"):
            raise BadRequest("action 只能是 grant 或 withdraw")
        status = "granted" if action == "grant" else "withdrawn"
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO consents(order_id, scope, status, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(order_id, scope) DO UPDATE SET status = excluded.status,"
                " updated_at = excluded.updated_at",
                (order_id, scope, status, now),
            )
            self.conn.execute(
                "INSERT INTO consent_log(order_id, scope, action, actor, at) VALUES (?, ?, ?, ?, ?)",
                (order_id, scope, action, actor.name, now),
            )
            if scope in (domain.SCOPE_DISPLAY, domain.SCOPE_PUBLIC):
                if action == "withdraw":
                    self._apply_publication_withdrawal(order, scope, now)
                else:
                    self._unlock_contents(order_id, scope)
        return self.get_consents(actor, order_id)

    def _apply_publication_withdrawal(self, order, scope, now):
        """撤回传播类授权：未发布内容锁定，已发布渠道生成下架任务。"""
        contents = self.conn.execute(
            "SELECT * FROM contents WHERE order_id = ?", (order["id"],)
        ).fetchall()
        for content in contents:
            if content["status"] == domain.CONTENT_READY:
                locked = set(json.loads(content["locked_scopes"]))
                locked.add(scope)
                self.conn.execute(
                    "UPDATE contents SET status = ?, locked_scopes = ? WHERE id = ?",
                    (domain.CONTENT_LOCKED, json.dumps(sorted(locked), ensure_ascii=False),
                     content["id"]),
                )
            pubs = self.conn.execute(
                "SELECT * FROM publications WHERE content_id = ? AND scope = ? AND status = ?",
                (content["id"], scope, domain.PUB_PUBLISHED),
            ).fetchall()
            for pub in pubs:
                self.conn.execute(
                    "UPDATE publications SET status = ? WHERE id = ?",
                    (domain.PUB_TAKING_DOWN, pub["id"]),
                )
                self.conn.execute(
                    "INSERT INTO takedown_tasks(publication_id, content_id, store_id, channel,"
                    " reason, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (pub["id"], content["id"], order["store_id"], pub["channel"],
                     f"顾客撤回「{scope}」授权", domain.TAKEDOWN_PENDING, now),
                )
                self.conn.execute(
                    "UPDATE contents SET status = ? WHERE id = ? AND status != ?",
                    (domain.CONTENT_TAKING_DOWN, content["id"], domain.CONTENT_TAKEN_DOWN),
                )

    def _unlock_contents(self, order_id, scope):
        """重新授权后，解除该范围造成的发布锁定（其他范围仍锁则保持锁定）。"""
        rows = self.conn.execute(
            "SELECT * FROM contents WHERE order_id = ? AND status = ?",
            (order_id, domain.CONTENT_LOCKED),
        ).fetchall()
        for row in rows:
            locked = set(json.loads(row["locked_scopes"]))
            locked.discard(scope)
            if locked:
                self.conn.execute(
                    "UPDATE contents SET locked_scopes = ? WHERE id = ?",
                    (json.dumps(sorted(locked), ensure_ascii=False), row["id"]),
                )
            else:
                self.conn.execute(
                    "UPDATE contents SET status = ?, locked_scopes = '[]' WHERE id = ?",
                    (domain.CONTENT_READY, row["id"]),
                )

    def get_consents(self, actor, order_id):
        order = self._get("orders", order_id, "订单")
        actor.check_store(order["store_id"])
        rows = self.conn.execute(
            "SELECT scope, status, updated_at FROM consents WHERE order_id = ?", (order_id,)
        ).fetchall()
        current = {r["scope"]: r["status"] for r in rows}
        log = _dicts(
            self.conn.execute(
                "SELECT scope, action, actor, at FROM consent_log WHERE order_id = ? ORDER BY id",
                (order_id,),
            ).fetchall()
        )
        return {
            "order_id": order_id,
            "consents": {scope: current.get(scope, "unset") for scope in domain.SCOPES},
            "log": log,
        }

    # ------------------------------------------------------------------
    # 内容发布与下架追踪
    # ------------------------------------------------------------------

    def create_content(self, actor, order_id, title):
        order = self._get("orders", order_id, "订单")
        actor.check_store(order["store_id"])
        if not title:
            raise BadRequest("内容标题不能为空")
        cur = self.conn.execute(
            "INSERT INTO contents(order_id, store_id, title, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (order_id, order["store_id"], title, domain.CONTENT_READY, _now()),
        )
        self.conn.commit()
        return self.get_content(actor, cur.lastrowid)

    def get_content(self, actor, content_id):
        content = self._get("contents", content_id, "内容")
        actor.check_store(content["store_id"])
        result = _dict(content)
        result["locked_scopes"] = json.loads(result["locked_scopes"])
        result["publications"] = _dicts(
            self.conn.execute(
                "SELECT * FROM publications WHERE content_id = ? ORDER BY id", (content_id,)
            ).fetchall()
        )
        return result

    def publish_content(self, actor, content_id, channel):
        content = self._get("contents", content_id, "内容")
        actor.check_store(content["store_id"])
        if content["status"] not in (domain.CONTENT_READY, domain.CONTENT_PUBLISHED):
            raise Conflict(f"内容处于 {content['status']}，不能发布")
        if not channel:
            raise BadRequest("需要指定发布渠道")
        scope = domain.scope_for_channel(channel)
        if not self._consent_granted(content["order_id"], scope):
            raise Conflict(f"顾客未授权「{scope}」，不能发布到 {channel}")
        live = self.conn.execute(
            "SELECT id FROM publications WHERE content_id = ? AND channel = ?"
            " AND status != ?",
            (content_id, channel, domain.PUB_TAKEN_DOWN),
        ).fetchone()
        if live is not None:
            raise Conflict(f"{channel} 渠道已有在架发布记录")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO publications(content_id, channel, scope, status, published_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (content_id, channel, scope, domain.PUB_PUBLISHED, now),
            )
            self.conn.execute(
                "UPDATE contents SET status = ? WHERE id = ?",
                (domain.CONTENT_PUBLISHED, content_id),
            )
        return self.get_content(actor, content_id)

    def list_takedowns(self, actor, status=None, store_id=None):
        store_id = actor.scoped_store_id(store_id)
        sql = "SELECT * FROM takedown_tasks"
        cond, args = [], []
        if store_id is not None:
            cond.append("store_id = ?")
            args.append(int(store_id))
        if status:
            cond.append("status = ?")
            args.append(status)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id"
        return _dicts(self.conn.execute(sql, args).fetchall())

    def complete_takedown(self, actor, task_id):
        """门店或总部确认渠道已下架；内容的全部渠道下架后内容转为已下架。"""
        task = self._get("takedown_tasks", task_id, "下架任务")
        actor.check_store(task["store_id"])
        if task["status"] != domain.TAKEDOWN_PENDING:
            raise Conflict(f"下架任务已是 {task['status']}")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE takedown_tasks SET status = ?, completed_at = ? WHERE id = ?",
                (domain.TAKEDOWN_DONE, now, task_id),
            )
            self.conn.execute(
                "UPDATE publications SET status = ?, taken_down_at = ? WHERE id = ?",
                (domain.PUB_TAKEN_DOWN, now, task["publication_id"]),
            )
            # 依据剩余发布记录收敛内容状态：全部下架 -> 已下架；
            # 无在下架且有在播渠道 -> 回到已发布；否则保持下架中。
            pubs = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM publications WHERE content_id = ?"
                " GROUP BY status",
                (task["content_id"],),
            ).fetchall()
            counts = {p["status"]: p["n"] for p in pubs}
            if counts.get(domain.PUB_PUBLISHED, 0) == 0 and \
                    counts.get(domain.PUB_TAKING_DOWN, 0) == 0:
                new_status = domain.CONTENT_TAKEN_DOWN
            elif counts.get(domain.PUB_TAKING_DOWN, 0) == 0:
                new_status = domain.CONTENT_PUBLISHED
            else:
                new_status = domain.CONTENT_TAKING_DOWN
            self.conn.execute(
                "UPDATE contents SET status = ? WHERE id = ?",
                (new_status, task["content_id"]),
            )
        return _dict(self._get("takedown_tasks", task_id, "下架任务"))

    # ------------------------------------------------------------------
    # 事件处置链：报送去重、分级流转、总部复核
    # ------------------------------------------------------------------

    def report_incident(self, actor, store_id, category, level, occurred_at,
                        order_id=None, detail=None, incident_key=None, reporter=None):
        """报送事件。同一事故（相同去重键）的重复报送并入未闭环事件，不新建。"""
        store_id = int(store_id)
        actor.check_store(store_id)
        self.get_store(store_id)
        if level not in domain.LEVELS:
            raise BadRequest(f"未知事件等级: {level}，可选: {domain.LEVELS}")
        if not category or not occurred_at:
            raise BadRequest("需要事件类别与发生日期")
        if order_id is not None:
            order = self._get("orders", int(order_id), "订单")
            if order["store_id"] != store_id:
                raise BadRequest("订单不属于该门店")
        dedup_key = incident_key or f"{store_id}|{order_id or '-'}|{category}|{occurred_at}"
        now = _now()
        existing = self.conn.execute(
            "SELECT * FROM incidents WHERE dedup_key = ? AND status != ?",
            (dedup_key, domain.INCIDENT_CLOSED),
        ).fetchone()
        with self.conn:
            if existing is not None:
                # 重复报送：追加报送记录；若新报送等级更严重则升级。
                new_level = existing["level"]
                if domain.level_rank(level) > domain.level_rank(existing["level"]):
                    new_level = level
                self.conn.execute(
                    "UPDATE incidents SET report_count = report_count + 1, level = ?,"
                    " updated_at = ? WHERE id = ?",
                    (new_level, now, existing["id"]),
                )
                self.conn.execute(
                    "INSERT INTO incident_reports(incident_id, reporter, detail, at)"
                    " VALUES (?, ?, ?, ?)",
                    (existing["id"], reporter or actor.name, detail, now),
                )
                result = _dict(self._get("incidents", existing["id"], "事件"))
                result["duplicate"] = True
                return result
            cur = self.conn.execute(
                "INSERT INTO incidents(store_id, order_id, category, level, status, detail,"
                " occurred_at, dedup_key, report_count, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (store_id, order_id, category, level, domain.INCIDENT_OPEN, detail,
                 occurred_at, dedup_key, now, now),
            )
            self.conn.execute(
                "INSERT INTO incident_reports(incident_id, reporter, detail, at)"
                " VALUES (?, ?, ?, ?)",
                (cur.lastrowid, reporter or actor.name, detail, now),
            )
        result = _dict(self._get("incidents", cur.lastrowid, "事件"))
        result["duplicate"] = False
        return result

    def advance_incident(self, actor, incident_id, to_status, note=None):
        incident = self._get("incidents", incident_id, "事件")
        actor.check_store(incident["store_id"])
        from_status = incident["status"]
        allowed = domain.INCIDENT_TRANSITIONS.get(from_status, set())
        if to_status not in allowed:
            raise Conflict(f"事件不允许从 {from_status} 推进到 {to_status}")
        needs_review = incident["level"] in domain.REVIEW_REQUIRED_LEVELS
        if to_status == domain.INCIDENT_CLOSED and needs_review:
            if from_status != domain.INCIDENT_REVIEW:
                raise Conflict(f"{incident['level']}必须先经总部复核才能闭环")
            if not actor.is_manager:
                raise Forbidden(f"{incident['level']}闭环须由总部管理者确认")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE incidents SET status = ?, updated_at = ? WHERE id = ?",
                (to_status, now, incident_id),
            )
            self.conn.execute(
                "INSERT INTO incident_log(incident_id, from_status, to_status, note, actor, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (incident_id, from_status, to_status, note, actor.name, now),
            )
        return _dict(self._get("incidents", incident_id, "事件"))

    def list_incidents(self, actor, store_id=None, status=None, open_only=False):
        store_id = actor.scoped_store_id(store_id)
        sql = "SELECT * FROM incidents"
        cond, args = [], []
        if store_id is not None:
            cond.append("store_id = ?")
            args.append(int(store_id))
        if status:
            cond.append("status = ?")
            args.append(status)
        if open_only:
            cond.append("status != ?")
            args.append(domain.INCIDENT_CLOSED)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id"
        return _dicts(self.conn.execute(sql, args).fetchall())

    def incident_detail(self, actor, incident_id):
        incident = _dict(self._get("incidents", incident_id, "事件"))
        actor.check_store(incident["store_id"])
        incident["reports"] = _dicts(
            self.conn.execute(
                "SELECT * FROM incident_reports WHERE incident_id = ? ORDER BY id",
                (incident_id,),
            ).fetchall()
        )
        incident["log"] = _dicts(
            self.conn.execute(
                "SELECT * FROM incident_log WHERE incident_id = ? ORDER BY id", (incident_id,)
            ).fetchall()
        )
        return incident

    # ------------------------------------------------------------------
    # 经营分析：比较不同试验版本的真实转化与风险
    # ------------------------------------------------------------------

    def plan_analytics(self, actor, store_id=None):
        store_id = actor.scoped_store_id(store_id)
        plans = self.list_plans(actor, store_id=store_id)
        results = []
        for plan in plans:
            orders = _dicts(
                self.conn.execute(
                    "SELECT status FROM orders WHERE plan_id = ?", (plan["id"],)
                ).fetchall()
            )
            total = len(orders)
            shot = sum(1 for o in orders if o["status"] not in ("待拍摄", "已取消"))
            delivered = sum(1 for o in orders if o["status"] in domain.ORDER_DELIVERED_STATUSES)
            consent_rows = self.conn.execute(
                "SELECT c.scope, c.status, COUNT(*) AS n FROM consents c"
                " JOIN orders o ON o.id = c.order_id WHERE o.plan_id = ?"
                " GROUP BY c.scope, c.status",
                (plan["id"],),
            ).fetchall()
            consents = {}
            for scope in domain.SCOPES:
                granted = sum(r["n"] for r in consent_rows
                              if r["scope"] == scope and r["status"] == "granted")
                consents[scope] = round(granted / total, 4) if total else 0
            incident_rows = self.conn.execute(
                "SELECT i.level, i.status, COUNT(*) AS n FROM incidents i"
                " JOIN orders o ON o.id = i.order_id WHERE o.plan_id = ?"
                " GROUP BY i.level, i.status",
                (plan["id"],),
            ).fetchall()
            by_level = {level: 0 for level in domain.LEVELS}
            open_count = 0
            for r in incident_rows:
                by_level[r["level"]] += r["n"]
                if r["status"] != domain.INCIDENT_CLOSED:
                    open_count += r["n"]
            incident_total = sum(by_level.values())
            takedown_rows = self.conn.execute(
                "SELECT t.status, COUNT(*) AS n FROM takedown_tasks t"
                " JOIN contents c ON c.id = t.content_id"
                " JOIN orders o ON o.id = c.order_id WHERE o.plan_id = ?"
                " GROUP BY t.status",
                (plan["id"],),
            ).fetchall()
            takedowns = {domain.TAKEDOWN_PENDING: 0, domain.TAKEDOWN_DONE: 0}
            for r in takedown_rows:
                takedowns[r["status"]] = r["n"]
            results.append({
                "plan_id": plan["id"],
                "store_id": plan["store_id"],
                "name": plan["name"],
                "phase": plan["phase"],
                "capacity": plan["capacity"],
                "orders_total": total,
                "orders_in_flight": sum(1 for o in orders
                                        if o["status"] in domain.ACTIVE_ORDER_STATUSES),
                "conversion": {
                    "shot_rate": round(shot / total, 4) if total else 0,
                    "delivered_rate": round(delivered / total, 4) if total else 0,
                },
                "consent_grant_rate": consents,
                "incidents": {
                    "total": incident_total,
                    "open": open_count,
                    "by_level": by_level,
                    "rate": round(incident_total / total, 4) if total else 0,
                },
                "takedowns": takedowns,
            })
        return results

    def version_analytics(self, actor, plan_id=None, store_id=None):
        store_id = actor.scoped_store_id(store_id)
        sql = ("SELECT v.*, p.store_id AS store_id, p.name AS plan_name"
               " FROM package_versions v JOIN plans p ON p.id = v.plan_id")
        cond, args = [], []
        if plan_id is not None:
            cond.append("v.plan_id = ?")
            args.append(int(plan_id))
        if store_id is not None:
            cond.append("p.store_id = ?")
            args.append(int(store_id))
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY v.plan_id, v.id"
        versions = self.conn.execute(sql, args).fetchall()
        results = []
        for v in versions:
            orders = _dicts(
                self.conn.execute(
                    "SELECT id, status FROM orders WHERE version_id = ?", (v["id"],)
                ).fetchall()
            )
            total = len(orders)
            delivered = sum(1 for o in orders if o["status"] in domain.ORDER_DELIVERED_STATUSES)
            switches_in = self.conn.execute(
                "SELECT COUNT(*) AS n FROM order_version_log"
                " WHERE to_version_id = ? AND from_version_id IS NOT NULL",
                (v["id"],),
            ).fetchone()["n"]
            switches_out = self.conn.execute(
                "SELECT COUNT(*) AS n FROM order_version_log WHERE from_version_id = ?",
                (v["id"],),
            ).fetchone()["n"]
            incidents = self.conn.execute(
                "SELECT COUNT(*) AS n FROM incidents i JOIN orders o ON o.id = i.order_id"
                " WHERE o.version_id = ?",
                (v["id"],),
            ).fetchone()["n"]
            results.append({
                "version_id": v["id"],
                "plan_id": v["plan_id"],
                "plan_name": v["plan_name"],
                "store_id": v["store_id"],
                "label": v["label"],
                "status": v["status"],
                "orders_current": total,
                "switches_in": switches_in,
                "switches_out": switches_out,
                "delivered": delivered,
                "delivered_rate": round(delivered / total, 4) if total else 0,
                "incidents": incidents,
                "incident_rate": round(incidents / total, 4) if total else 0,
            })
        return results

    # ------------------------------------------------------------------
    # 重启恢复：待下架任务与未闭环事件继续推进
    # ------------------------------------------------------------------

    def pending_work(self, actor, store_id=None):
        """待办视图：待下架任务 + 未闭环事件，供门店与总部接续处理。"""
        store_id = actor.scoped_store_id(store_id)
        takedowns = self.list_takedowns(actor, status=domain.TAKEDOWN_PENDING,
                                        store_id=store_id)
        incidents = self.list_incidents(actor, store_id=store_id, open_only=True)
        return {"pending_takedowns": takedowns, "open_incidents": incidents}

    def recover(self):
        """服务启动时执行：盘点并记录待推进工作，重启不丢处置链。"""
        pending = self.conn.execute(
            "SELECT COUNT(*) AS n FROM takedown_tasks WHERE status = ?",
            (domain.TAKEDOWN_PENDING,),
        ).fetchone()["n"]
        open_incidents = self.conn.execute(
            "SELECT COUNT(*) AS n FROM incidents WHERE status != ?",
            (domain.INCIDENT_CLOSED,),
        ).fetchone()["n"]
        self.conn.execute(
            "INSERT INTO recovery_log(at, pending_takedowns, open_incidents) VALUES (?, ?, ?)",
            (_now(), pending, open_incidents),
        )
        self.conn.commit()
        return {"pending_takedowns": pending, "open_incidents": open_incidents}
