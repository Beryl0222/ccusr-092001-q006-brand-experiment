"""SQLite 持久化。

所有业务状态落盘，服务重启后待下架任务与未闭环事件可继续推进。
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    phase TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_phase_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    from_phase TEXT,
    to_phase TEXT NOT NULL,
    reason TEXT,
    actor TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    label TEXT NOT NULL,
    price_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    home_store_id INTEGER NOT NULL REFERENCES stores(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    skill TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    store_id INTEGER NOT NULL REFERENCES stores(id),
    work_date TEXT NOT NULL,
    shift TEXT NOT NULL,
    cross_store INTEGER NOT NULL DEFAULT 0,
    actor TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    version_id INTEGER NOT NULL REFERENCES package_versions(id),
    customer_name TEXT NOT NULL,
    pet_name TEXT NOT NULL,
    pet_species TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_version_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    from_version_id INTEGER,
    to_version_id INTEGER NOT NULL,
    reason TEXT,
    actor TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(order_id, scope)
);

CREATE TABLE IF NOT EXISTS consent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    scope TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    store_id INTEGER NOT NULL REFERENCES stores(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    locked_scopes TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL REFERENCES contents(id),
    channel TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    published_at TEXT NOT NULL,
    taken_down_at TEXT
);

CREATE TABLE IF NOT EXISTS takedown_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications(id),
    content_id INTEGER NOT NULL REFERENCES contents(id),
    store_id INTEGER NOT NULL REFERENCES stores(id),
    channel TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    order_id INTEGER REFERENCES orders(id),
    category TEXT NOT NULL,
    level TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    occurred_at TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    report_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_dedup ON incidents(dedup_key, status);

CREATE TABLE IF NOT EXISTS incident_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    reporter TEXT,
    detail TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    note TEXT,
    actor TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    pending_takedowns INTEGER NOT NULL,
    open_incidents INTEGER NOT NULL
);
"""


def connect(path):
    """打开数据库连接并启用外键约束。

    check_same_thread=False 配合服务层 RLock 使用：HTTP 线程模型下所有
    写操作在同一把锁内串行执行。
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    """建表（幂等），供启动与 --check 使用。"""
    conn.executescript(SCHEMA)
    conn.commit()
