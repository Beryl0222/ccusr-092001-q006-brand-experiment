# 老字号新业务试验

项目帮助传统品牌记录新服务（宠物摄影）从提案到正式经营的演变，并统一传播授权和现场事件的口径。`domain.json` 是各门店交换数据时的基础词表。

## 运行

```bash
python3 service.py --check          # 检查配置并初始化数据库
python3 service.py --port 8000      # 启动服务（默认库文件 petstudio.db，可用 --db 指定）
python3 -m unittest -v              # 运行测试（亦兼容 pytest）
```

服务启动时会自动执行恢复盘点：待下架任务与未闭环事件在重启后继续可追踪推进，可通过 `GET /pending-work` 查看并接续处理。

## 角色与数据边界

请求头标识操作者：

- `X-Actor-Role: manager` —— 总部管理者，可见全部门店，负责阶段推进、能力认证核发、跨店调班、严重事件复核闭环；
- `X-Actor-Role: store` + `X-Store-Id: <门店id>` —— 门店角色，只能接触本店业务数据，可暂停本店方案、处理本店下架任务与低等级事件；
- `X-Actor-Name` —— 可选，用于操作留痕。

## 核心规则

- **试验方案与容量**：门店提交方案（含在途订单容量上限），阶段按 `提案 → 小范围开放 → 扩大验证 → 正式经营` 推进，任意阶段可暂停、可恢复；提案与暂停阶段不接单，容量占满即拒单。
- **套餐版本**：同一方案任一时刻只有一个在售版本；新单只能下在售版本，在途订单可在同方案内切换版本并全程留痕。
- **认证与排班**：员工须持有效「宠物服务认证」才能排班；跨店调班由总部协调并标记。
- **分项授权**：拍摄、成片交付、门店展示、公开传播四类授权分别授予/撤回。拍摄与交付须先取得对应授权；发布到「门店展示」渠道须门店展示授权，其余公开渠道须公开传播授权。
- **授权撤回**：撤回传播类授权时，未发布内容进入发布锁定（重新授权后解锁），已发布渠道逐渠道生成下架任务，全部确认下架后内容转为已下架。
- **事件处置链**：报送 → 处置中 →（待复核）→ 已闭环。同一事故（相同去重键）的重复报送并入未闭环事件并计数，更严重等级自动升级；「安全关注」「严重事故」必须经总部复核闭环。
- **经营分析**：`GET /analytics/plans` 比较各方案的转化（拍摄率、交付率）、授权率、事件率与下架量；`GET /analytics/versions` 比较各套餐版本的订单、切换与风险。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` `/domain` | 服务身份 / 基础词表 |
| POST/GET | `/stores` | 门店创建（总部）/ 列表 |
| POST | `/stores/{id}/plans` | 提交试验方案与容量 |
| GET/POST | `/plans` `/plans/{id}/phase` | 方案查询 / 阶段流转 |
| POST/GET | `/plans/{id}/versions` `/versions/{id}/activate` | 套餐版本与上架 |
| POST | `/employees` `/employees/{id}/certifications` | 员工与认证（总部核发） |
| POST/GET | `/assignments` | 排班与跨店调班 |
| POST/GET | `/orders` `/orders/{id}/advance` `/orders/{id}/switch-version` | 订单、推进、版本切换 |
| POST/GET | `/orders/{id}/consents` | 分项授权授予/撤回与留痕 |
| POST/GET | `/contents` `/contents/{id}/publish` | 内容与渠道发布 |
| GET/POST | `/takedowns` `/takedowns/{id}/complete` | 下架任务追踪与确认 |
| POST/GET | `/incidents` `/incidents/{id}/advance` | 事件报送（去重）与处置链 |
| GET | `/analytics/plans` `/analytics/versions` | 转化与风险对比 |
| GET | `/pending-work` | 待下架任务与未闭环事件 |
