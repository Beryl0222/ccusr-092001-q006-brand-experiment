"""宠物摄影试验业务的 SQLite 持久化层。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores(
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    token      TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staff(
    id            TEXT PRIMARY KEY,
    store_id      TEXT NOT NULL REFERENCES stores(id),
    name          TEXT NOT NULL,
    certified     INTEGER NOT NULL DEFAULT 0,
    certified_at  TEXT,
    created_at    TEXT NOT NULL
);

-- 套餐试验方案：同名套餐按 major.minor 存版本，门店每次提交都是新版本
CREATE TABLE IF NOT EXISTS packages(
    id             TEXT PRIMARY KEY,
    store_id       TEXT NOT NULL REFERENCES stores(id),
    name           TEXT NOT NULL,
    stage          TEXT NOT NULL,          -- 对齐 domain.json 的试验阶段
    major          INTEGER NOT NULL,
    minor          INTEGER NOT NULL,
    status         TEXT NOT NULL,          -- 试用 / 生效 / 归档
    supersedes     TEXT REFERENCES packages(id),
    config_json    TEXT NOT NULL DEFAULT '{}',
    trial_start_at TEXT,
    promo_at       TEXT,                   -- 进入正式经营的时间
    created_at     TEXT NOT NULL,
    UNIQUE(store_id, name, major, minor)
);

-- 门店每日可接待容量
CREATE TABLE IF NOT EXISTS capacity(
    store_id TEXT NOT NULL REFERENCES stores(id),
    day      TEXT NOT NULL,
    total    INTEGER NOT NULL CHECK(total >= 0),
    PRIMARY KEY(store_id, day)
);

CREATE TABLE IF NOT EXISTS orders(
    id             TEXT PRIMARY KEY,
    store_id       TEXT NOT NULL REFERENCES stores(id),
    package_id     TEXT NOT NULL REFERENCES packages(id),
    package_name   TEXT NOT NULL,          -- 快照，便于按版本对比
    package_version TEXT NOT NULL,         -- "major.minor" 快照
    stage_snapshot TEXT NOT NULL,          -- 下单时套餐所处试验阶段
    status         TEXT NOT NULL,          -- 预约 / 已拍 / 已交付 / 已取消
    shoot_at       TEXT,
    created_at     TEXT NOT NULL
);

-- 转化漏斗事件，用于版本对比
CREATE TABLE IF NOT EXISTS funnel_events(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL REFERENCES orders(id),
    store_id   TEXT NOT NULL,
    package_id TEXT NOT NULL,
    event      TEXT NOT NULL,              -- 预约 / 到店拍摄 / 成片交付 / 公开发布
    created_at TEXT NOT NULL
);

-- 分项授权：同一订单同一范围同时只有一条有效授权；撤回留痕，重新授权另起一行
CREATE TABLE IF NOT EXISTS consents(
    id            TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES orders(id),
    scope         TEXT NOT NULL,           -- 现场拍摄 / 成片交付 / 门店展示 / 公开传播
    granted       INTEGER NOT NULL DEFAULT 1,
    granted_at    TEXT NOT NULL,
    revoked_at    TEXT,
    revoke_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_consents_active
    ON consents(order_id, scope) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS media_assets(
    id         TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL REFERENCES orders(id),
    store_id   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 一次发布 = 一个成片在一个渠道的挂出；撤权后逐渠道追踪下架
CREATE TABLE IF NOT EXISTS publications(
    id                  TEXT PRIMARY KEY,
    asset_id            TEXT NOT NULL REFERENCES media_assets(id),
    store_id            TEXT NOT NULL,
    channel             TEXT NOT NULL,
    audience            TEXT NOT NULL,     -- 公开 / 店内
    external_ref        TEXT,
    status              TEXT NOT NULL,     -- 已发布 / 下架中 / 已下架
    published_at        TEXT NOT NULL,
    takedown_started_at TEXT,
    taken_down_at       TEXT
);

-- 待下架任务，重启后仍须继续推进
CREATE TABLE IF NOT EXISTS takedown_tasks(
    id             TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL REFERENCES publications(id),
    store_id       TEXT NOT NULL,
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL,          -- 待处理 / 处理中 / 已完成
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 现场事故/宠物应激处置链（与普通订单备注分离）
CREATE TABLE IF NOT EXISTS incidents(
    id           TEXT PRIMARY KEY,
    store_id     TEXT NOT NULL REFERENCES stores(id),
    order_id     TEXT REFERENCES orders(id),
    level        TEXT NOT NULL,            -- 一般记录 / 服务中断 / 安全关注 / 严重事故
    title        TEXT NOT NULL,
    detail_json  TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL,            -- 待响应 / 处理中 / 待复核 / 已闭环
    client_ref   TEXT,                     -- 门店侧幂等键，防重复报送
    duplicate_of TEXT REFERENCES incidents(id),
    owner        TEXT,
    occurred_at  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    closed_at    TEXT,
    UNIQUE(store_id, client_ref)
);

CREATE TABLE IF NOT EXISTS incident_logs(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    action      TEXT NOT NULL,
    note        TEXT,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 排班：home_store_id 记录员工归属门店，跨店调班由此留痕
CREATE TABLE IF NOT EXISTS schedules(
    id            TEXT PRIMARY KEY,
    store_id      TEXT NOT NULL REFERENCES stores(id),       -- 服务门店（工位）
    staff_id      TEXT NOT NULL REFERENCES staff(id),
    home_store_id TEXT NOT NULL REFERENCES stores(id),
    day           TEXT NOT NULL,
    slot          TEXT NOT NULL,
    status        TEXT NOT NULL,                           -- 已排 / 已取消
    order_id      TEXT REFERENCES orders(id),
    created_at    TEXT NOT NULL,
    UNIQUE(day, slot, staff_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_store ON orders(store_id);
CREATE INDEX IF NOT EXISTS idx_orders_pkg ON orders(package_id);
CREATE INDEX IF NOT EXISTS idx_publications_asset ON publications(asset_id);
CREATE INDEX IF NOT EXISTS idx_takedown_status ON takedown_tasks(status);
CREATE INDEX IF NOT EXISTS idx_incidents_store ON incidents(store_id);
CREATE INDEX IF NOT EXISTS idx_schedules_store_day ON schedules(store_id, day);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）业务库。":memory:" 用于测试。"""
    path = ":memory:" if db_path == ":memory:" else str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表并写入版本号。"""
    conn.executescript(SCHEMA)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
