# Mission Lock: 龟仙人 E2E 真实交互链路

Updated: 2026-03-08  
Owner: `feiqiao-guard-delivery-lead`

## 1. 我们到底要做什么

唯一主目标（不可替代）：

- 打通并稳定运行这条链路：`用户 -> 龟仙人机器人 -> DingTalk Stream Bridge -> 本地 Codex/tmux identity 实例 -> 过程回传 -> 最终结果回传给用户`。

该目标的核心是：

- 过程中能互动，不是只推一条最终通知。
- 由对应 identity 实例执行，不是由主实例“假装代答”。
- 可追溯、可复放、可验收。

## 2. 验收通过标准（必须同时满足）

1. 触发来源：用户在钉钉会话里对 `龟仙人` 发消息。
2. 路由身份：leader 固定为 `feiqiao-guard-delivery-lead`，不得漂移到其他 identity。
3. 过程可见：至少包含 `accepted -> dispatch_summary -> followup/progress -> final_result`。
4. 结果回传：最终答案在钉钉会话可见，不依赖本地终端解释。
5. 可复放：保留 `trace_id/task_id/question_tag` 与日志证据。
6. 审批闭环：高危动作出现提权时，必须可在 `龟仙人` 会话内直接审批，不依赖本地窗口弹窗。

## 3. 禁止路径（Fail-Close）

以下路径一律不算通过：

- 把 `127.0.0.1` 本地 API 回归当成“外部真实验收”。
- 用机器人自己主动发群消息当作“用户入站触发”。
- 使用 `n8n自动化通知机器人` 进行交互测试。
- 没有过程消息，仅给单条最终结果。

## 4. 机器人通道策略（当前强约束）

- `龟仙人`: `ACTIVE`（唯一允许交互）
- `n8n自动化通知机器人`: `PAUSED`（保留，不删除，不参与交互）

对应配置：

- `FQG_DINGTALK_PAUSED_WEBHOOK_TOKENS` 必须包含 n8n webhook token。
- 命中 paused token 时，发送侧直接拒绝（fail-close）。

## 5. 为什么会“记不住”（identity 记忆漂移说明）

identity 实例不是单一记忆体，它至少有三层：

1. 对话上下文记忆（短时、易被中断覆盖）
2. 运行态记忆（tmux/session 状态，受重启和切换影响）
3. 持久化记忆（文件/配置/日志，稳定但需要显式读取并门禁执行）

历史漂移的根因不是“没有记忆”，而是：

- 目标没有被写成硬门禁，导致执行时切到更容易跑通的替代路径。
- 中断后未先重载 mission lock，就直接续跑旧动作。
- 把“内部稳定性回归”和“外部真实验收”混在一起执行。

## 6. 防漂移执行门禁（每轮必检）

执行任何动作前，先检查并记录：

1. `mission`: 是否为 `guixianren_e2e_only`
2. `channel`: 是否为 `龟仙人 ACTIVE`
3. `trigger_source`: 是否为“用户消息入站”
4. `evidence`: 是否可生成 trace/task/question 证据

任一不满足：立即停止执行并回报，不进入测试步骤。

## 7. 当前执行策略

- 先确保通道状态正确（龟仙人 active，n8n paused）。
- 再执行 30+ 场景真实交互测试。
- 每个场景都要求过程回传和最终回传。
- 失败场景修复后回归重跑，直到达到通过标准。

## 7.1 调度纠偏准则（2026-03-10 新增）

正确路径（必须执行）：

1. 用户消息进入后，先把任务交给 `tmux` 容器中的目标 `codex` 实例执行。
2. 若执行偏了，优先在同一会话内继续对话纠偏（同一 identity、同一 session）。
3. 若偏差较大且实例自身无法收敛，再升级为 `leader` 介入：给出“教学式指令/约束/证据要求”，而不是替代执行。

错误路径（禁止）：

1. 在桥接层按关键词硬编码结果分支。
2. leader 直接兜底代答，绕开目标 identity 实例。
3. 将“临时脚本直接产出答案”当成实例真实能力完成。

## 8. 本地常驻运行面（根治沙箱回收）

- 问题根因：在 sandbox 内拉起的后台进程会随命令结束被回收，导致 tmux/socket 无法跨命令长驻。
- 根治方式：将本地运行面托管到宿主级守护层（launchd），而不是依赖会话内临时后台进程。
- 落地脚本：
  - `scripts/local_stack_supervisor.sh`（循环健康检查 + 异常自动重启）
  - `scripts/local_stack_launchd.sh`（install/start/stop/uninstall/status）
- 结论：业务调度仍通过同一 API/bridge 链路，但进程生命周期不再受 sandbox 命令生命周期影响。

## 9. 长连接稳定性记录（2026-03-08）

当前观测结论：

- 长连接（DingTalk Stream + Bridge + tmux runtime）稳定，不再出现“命令结束即误杀后台进程”的旧问题。
- 关键原因：运行面已经由 launchd 托管，不再依赖 sandbox 命令生命周期。
- 触发验证：`龟仙人`通道可持续接收过程消息与结果消息，且服务重启后可自动恢复。

后续固定要求：

1. 每次测试前，必须先执行运行面状态核验（healthz + routes + leader snapshot）。
2. 若检测到掉线，优先走 `local_stack_launchd.sh restart`，禁止临时手工后台拉起替代。
3. 每轮测试都必须产出可复放证据：`run_id`、`summary.tsv`、`report.json`、钉钉过程消息。

## 10. 龟仙人文本审批指令（2026-03-08 新增）

为消除“本地弹窗无法处理”的阻断，桥接层支持在龟仙人会话内直接审批：

- `待审批` / `审批列表` / `/approvals`：查看当前待审批队列。
- `同意 <request_id>` / `/approve <request_id>`：批准指定审批项。
- `拒绝 <request_id>` / `/reject <request_id>`：拒绝指定审批项。

运行前置（API 与 Bridge 需共享同一 token）：

- `FQG_TRUSTED_BRIDGE_DECISION_TOKEN=<your_token>`
- `FQG_BRIDGE_TRUSTED_DECISION_TOKEN=<same_token>`

配套行为：

- bridge 会在检测到 `PENDING` 审批时主动回推 `request_id` 与指令提示；
- 审批结果会立即回传 `status/terminal_action/reason`；
- 审批后自动继续等待并回传最终执行结果。

## 11. Stream 稳定性硬门禁（2026-03-08 增补）

为避免“看起来在线但收不到消息”的假健康，运行前后都必须满足以下门禁：

1. 启动门禁：
- `bridge.log` 同时出现 `bridge_started` 与 `endpoint is {...}` 才算桥接启动成功。
- 若仅有进程拉起但没有 endpoint，直接判失败，不进入业务测试。

2. 守卫门禁：
- supervisor 读取 `bridge_heartbeat.json`（由 bridge 周期写入），校验新鲜度。
- 心跳缺失或过期按 `bridge_heartbeat_missing_or_stale` 判故障。

3. 抖动门禁：
- 健康检查失败采用“连续失败阈值”再重启，禁止一次瞬时失败就重启。
- 日志必须输出失败细项：`api_session_missing / bridge_session_missing / healthz_unreachable / bridge_heartbeat_missing_or_stale`。

4. 官方协议门禁（联网核对）：
- 群聊消息回调仅在 **@ 机器人** 时触发。
- 机器人回调 topic 必须订阅 `/v1.0/im/bot/messages/get`。
- 长连接断线必须可自动重连并继续 ACK 回调。

## 12. Skill 绑定与滚动上下文（2026-03-10 增补）

为防止执行时再次漂移，任务强制绑定以下 skill：

1. 主链路稳定执行：
- `skills/identity-longlink-stability-orchestration/SKILL.md`

2. 单题最终答案收口：
- `skills/identity-dispatch-final-answer/SKILL.md`

3. 身份实例三段验证：
- `skills/identity-instance-triple-verify/SKILL.md`

滚动上下文策略：

- 维护最近 60 条运行摘要，不保留无限历史；
- 分层窗口：
  - 前 20 条：最新高频交互（用于实时调度）
  - 中 20 条：稳定基线行为（用于防漂移对比）
  - 后 20 条：故障与修复留底（用于复盘与追溯）
- 每轮写入新摘要时，自动淘汰最旧摘要，保持固定上限 60。
