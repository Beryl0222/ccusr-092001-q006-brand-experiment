"""领域词表与状态机定义。

词表来自 domain.json，是各门店交换数据的基础口径；状态机约束新业务
从试验到正式经营的全过程中各类对象的合法流转。
"""

import json
from pathlib import Path

_DOMAIN_PATH = Path(__file__).with_name("domain.json")


def load_domain(path=None):
    """读取基础词表（试验阶段、授权范围、事件等级）。"""
    with open(path or _DOMAIN_PATH, encoding="utf-8") as fh:
        return json.load(fh)


DOMAIN = load_domain()

PHASES = DOMAIN["试验阶段"]
SCOPES = DOMAIN["授权范围"]
LEVELS = DOMAIN["事件等级"]

PHASE_PROPOSAL, PHASE_SMALL, PHASE_EXPAND, PHASE_FORMAL, PHASE_PAUSED = PHASES
SCOPE_SHOOT, SCOPE_DELIVER, SCOPE_DISPLAY, SCOPE_PUBLIC = SCOPES

# 试验阶段流转：逐级推进，可随时暂停，暂停后可恢复到任一经营阶段。
PHASE_TRANSITIONS = {
    PHASE_PROPOSAL: {PHASE_SMALL, PHASE_PAUSED},
    PHASE_SMALL: {PHASE_EXPAND, PHASE_PAUSED},
    PHASE_EXPAND: {PHASE_FORMAL, PHASE_PAUSED},
    PHASE_FORMAL: {PHASE_PAUSED},
    PHASE_PAUSED: {PHASE_SMALL, PHASE_EXPAND, PHASE_FORMAL},
}

# 允许接单的阶段；提案与暂停不接新单。
ORDERABLE_PHASES = {PHASE_SMALL, PHASE_EXPAND, PHASE_FORMAL}

# 套餐版本状态：草稿 -> 在售 -> 停售。同一方案同一时刻最多一个在售版本。
VERSION_DRAFT = "草稿"
VERSION_ACTIVE = "在售"
VERSION_RETIRED = "停售"

# 订单状态机。
ORDER_TRANSITIONS = {
    "待拍摄": {"拍摄完成", "已取消"},
    "拍摄完成": {"成片制作", "已取消"},
    "成片制作": {"待交付"},
    "待交付": {"已交付"},
    "已交付": {"已完成"},
    "已完成": set(),
    "已取消": set(),
}
# 占用方案容量的在途订单状态。
ACTIVE_ORDER_STATUSES = {"待拍摄", "拍摄完成", "成片制作", "待交付", "已交付"}
ORDER_DELIVERED_STATUSES = {"已交付", "已完成"}

# 内容（成片）状态。
CONTENT_READY = "待发布"
CONTENT_PUBLISHED = "已发布"
CONTENT_LOCKED = "发布锁定"
CONTENT_TAKING_DOWN = "下架中"
CONTENT_TAKEN_DOWN = "已下架"

# 发布记录状态。
PUB_PUBLISHED = "已发布"
PUB_TAKING_DOWN = "下架中"
PUB_TAKEN_DOWN = "已下架"

# “门店展示”渠道受门店展示授权约束，其余公开渠道一律受公开传播授权约束。
DISPLAY_CHANNEL = "门店展示"


def scope_for_channel(channel):
    """返回发布到指定渠道所需的授权范围。"""
    return SCOPE_DISPLAY if channel == DISPLAY_CHANNEL else SCOPE_PUBLIC


# 排班（含跨店调班）要求员工持有的能力认证。
REQUIRED_SKILL = "宠物服务认证"

# 事件处置链：报送 -> 处置 -> （复核）-> 闭环。
INCIDENT_OPEN = "待处置"
INCIDENT_HANDLING = "处置中"
INCIDENT_REVIEW = "待复核"
INCIDENT_CLOSED = "已闭环"
INCIDENT_TRANSITIONS = {
    INCIDENT_OPEN: {INCIDENT_HANDLING},
    INCIDENT_HANDLING: {INCIDENT_REVIEW, INCIDENT_CLOSED},
    INCIDENT_REVIEW: {INCIDENT_CLOSED},
    INCIDENT_CLOSED: set(),
}
# 必须经总部复核才能闭环的事件等级。
REVIEW_REQUIRED_LEVELS = {"安全关注", "严重事故"}

# 下架任务状态。
TAKEDOWN_PENDING = "待下架"
TAKEDOWN_DONE = "已下架"


def level_rank(level):
    """事件等级严重度排序，用于重复报送时的等级升级。"""
    return LEVELS.index(level)
