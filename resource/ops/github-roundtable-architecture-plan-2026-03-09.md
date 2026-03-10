# 飞桥守护：GitHub 对标与执行规划（2026-03-09）

## 1. 结论先行

本项目后续不再走“tmux 临时会话 + 长阻塞等待结果”的旧路径，改为：

1. 线上仅做钉钉中转与任务编排（控制面）。
2. 本机常驻执行器负责 identity 实例执行（数据面）。
3. 全链路改成异步任务状态机（`accepted -> running -> progress -> final|failed|timeout`）。

这条路线的目标是解决三件事：

1. 稳定性：不再依赖单个 tmux 前台窗口存活。
2. 可观测：每条消息都能查到任务状态与证据。
3. 可追溯：identity 调度、会话绑定、回包结果可复放。

## 2. GitHub 关键对标（你强调的重点）

### 2.1 可靠入站（钉钉）

- DingTalk Stream SDK（官方）：
  - `open-dingtalk/dingtalk-stream-sdk-python`
  - README 明确支持 `start_forever()` 常驻模式，且在已有事件循环场景提示网络异常后需要重启连接处理。

### 2.2 任务编排与重试

- BullMQ（Redis 队列，成熟重试/退避/去重）：
  - `taskforcesh/bullmq`
  - 支持 `attempts + backoff + jitter + deduplication + queue events`，非常适合“机器人消息->异步任务->最终回传”。
- Redis Streams（底层原语）：
  - 关键命令链：`XREADGROUP/XACK/XPENDING/XINFO`，可实现 ACK、积压检查、重放。

### 2.3 OpenClaw 风格能力（你要求对标）

- 正确仓库是 `openclaw/openclaw`（不是 `OpenClawTeam/OpenClaw`）。
- README 核心点：
  - Gateway 作为控制面；
  - 多通道接入；
  - 多会话/多代理路由；
  - 显式安全门禁（配对、allowlist、权限策略）。

### 2.4 OpenAI 官方异步模式（官方文档）

- Background mode：长任务异步执行、轮询状态。
- Webhooks：`response.completed` 事件用于最终结果回调。
- 这与“钉钉中转 + 本地执行 + 异步最终回传”架构高度一致。

### 2.5 GitHub 实时指标快照（2026-03-09 17:06 +0800）

数据来源：`api.github.com/repos/*` 实时查询。

- `open-dingtalk/dingtalk-stream-sdk-python`
  - stars: 146
  - pushed_at: 2025-10-24T09:36:19Z
- `taskforcesh/bullmq`
  - stars: 8,528
  - pushed_at: 2026-03-09T05:32:10Z
- `temporalio/documentation`
  - stars: 144
  - pushed_at: 2026-03-08T16:54:22Z
- `openclaw/openclaw`
  - stars: 285,868
  - pushed_at: 2026-03-09T09:05:56Z

## 3. 三种候选方案（按可落地性排序）

### A) Redis Streams + Python Worker（推荐，当前项目最稳）

优点：

1. 与现有 Python 代码栈一致，改造成本最低。
2. 可做严格 ACK/重试/积压治理。
3. 便于快速落地“线上中转、线下执行”分层。

风险：

1. 需要自己补一部分编排逻辑（状态机、超时、重放策略）。

### B) BullMQ（强队列能力，略增技术栈复杂度）

优点：

1. 重试/退避/事件流开箱即用。
2. 运营侧可观测性成熟。

风险：

1. 引入 Node 侧编排，和当前 Python 主链路存在双栈运维复杂度。

### C) Temporal（最强耐久编排，MVP 成本高）

优点：

1. Durable workflow 天然抗进程重启/超时。
2. 复杂长任务编排能力最强。

风险：

1. 引入成本高，不适合当前“先把桥打通”的阶段目标。

## 4. 最终执行方案（现在就按这个做）

采用方案 A（Redis Streams + Python Worker），并吸收 OpenClaw 的控制面思想：

1. `dingtalk-relay`（线上）
   - 只做入站解析、鉴权、写入任务流、读取任务状态并回传。
2. `local-executor-daemon`（本机常驻）
   - 拉取任务流，调度 leader/collab identity 实例执行。
   - 写入 `progress/final` 事件。
3. `route-guard`（强绑定门禁）
   - `identity_id -> session_id -> codex_home` 一致性校验。
   - 不满足即 fail-close（409/拒绝执行）。

## 5. 里程碑（可验收）

### M1（D+1）

1. 打通异步状态机最小链路（文本任务）。
2. 入站 3 秒内返回 `accepted(task_id)`。
3. 任务完成后自动回传 `final(task_id)`。

### M2（D+2）

1. 加入本机常驻守护（不依赖人工开 tmux）。
2. 自动重连、断线重放、积压清理可用。

### M3（D+3）

1. 接入多模态任务（图片识别、浏览器动作）。
2. 同时验证 identity 记忆召回（跨轮/跨窗口一致）。

### M4（D+4）

1. 跑 30+ 实测用例（文本、多模态、多角色、重试场景）。
2. 输出证据包（日志、task timeline、回包截图、失败用例修复记录）。

## 6. 必过门禁（Fail-Close）

1. 机器人唯一绑定：`龟仙人` 为 ACTIVE，`n8n 自动化通知机器人` 为 PAUSED（禁止发送）。
2. 任务必须带 `task_id`，每次回包必须引用同一个 `task_id`。
3. 未通过路由绑定校验不执行（拒绝而不是“假成功”）。
4. 超时必须显式回 `timeout`，不能静默。
5. 失败必须回 `failed + reason + retry_hint`。

## 7. 今日执行项（无歧义）

1. 落地异步任务状态机到现有桥接链路。
2. 落地本机常驻执行器（守护进程模式，不依赖会话窗口）。
3. 用真实钉钉消息回放失败场景，逐个关单，直到 30+ 用例通过。

## 8. 参考链接

- DingTalk Stream SDK（GitHub）：https://github.com/open-dingtalk/dingtalk-stream-sdk-python
- BullMQ（GitHub）：https://github.com/taskforcesh/bullmq
- Temporal docs（GitHub）：https://github.com/temporalio/documentation
- OpenClaw（GitHub）：https://github.com/openclaw/openclaw
- OpenAI Background mode：https://developers.openai.com/api/docs/guides/background/
- OpenAI Webhooks：https://developers.openai.com/api/docs/guides/webhooks/
- OpenAI Webhook 事件 `response.completed`：https://developers.openai.com/api/reference/resources/webhooks/#response.completed
