# tosee-birder

原 FeiQiao-Guard，现统一命名为 `tosee-birder`。  
定位：Identity 实例驱动的钉钉桥接系统（同意/拒绝与持续任务结果自动回传终端）。

## Quick Start

```bash
cd /Users/yangxi/claude/codex_project/fqsh
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
```

测试稳定性说明（macOS + conda Python 3.12 环境）：
- 项目在 `pyproject.toml` 中已默认设置 `pytest` 参数 `-p no:capture`，用于规避已知的 `pytest` capture 初始化段错误（`rc=139`）。
- 若你覆盖了 `PYTEST_ADDOPTS`，请保留 `-p no:capture`。

如果环境受限不能联网安装，可直接用源码路径运行：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
PYTHONPATH=src python3 -m tosee_birder --host 0.0.0.0 --port 8765
```

启动审批网关：

```bash
export FQG_HOST=0.0.0.0
export FQG_PORT=8765
export FQG_TIMEOUT_SECONDS=120
export FQG_CALLBACK_BASE_URL=http://127.0.0.1:8765
export FQG_CALLBACK_SIGNING_SECRET=replace-with-secret
export FQG_DEFAULT_APPROVER=zhouqihang
export FQG_DINGTALK_WEBHOOK_URL=
export FQG_DINGTALK_SIGNING_SECRET=
export FQG_LARK_WEBHOOK_URL=
export FQG_LARK_SIGNING_SECRET=
export FQG_SQLITE_PATH=resource/reports/approval_gateway.db
export FQG_AUDIT_LOG_PATH=resource/reports/approval_audit.jsonl
export FQG_ENABLE_HIGH_RISK_DUAL_APPROVAL=0
export FQG_IDENTITY_ROUTES_PATH=.runtime/identity_routes.json
export FQG_CHAT_CONTROL_TIMEOUT_SECONDS=20
export FQG_CHAT_DEFAULT_VERIFY_SECONDS=8
export FQG_CHAT_LEADER_IDENTITY_ID=feiqiao-guard-delivery-lead
export FQG_CHAT_COLLAB_IDENTITY_ID=feiqiao-guard-collab-executor
export FQG_DISABLE_AGENTS_ADD_DIR=0
python3 -m tosee_birder
```

健康检查：

```bash
curl -sS http://127.0.0.1:8765/healthz
```

审批可观测（按 request_id 查看投票时间线）：

```bash
curl -sS http://127.0.0.1:8765/v1/approvals/<request_id>/votes
```

## Wrapper Usage

```bash
python3 -m tosee_birder.wrapper --gateway-base-url http://127.0.0.1:8765 -- codex
```

Wrapper 会检测提权提示，发起审批，并在审批结果返回后自动输入：
- approve -> Enter
- reject/timeout/error -> Esc

推荐直接用一键脚本（默认走线上网关 `http://8.140.215.219:3001`）：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
./scripts/run_codex_guarded.sh
```

该脚本为**强制绝对隔离模式**：
- 使用独立 `HOME/XDG/CODEX_HOME`：`/Users/yangxi/claude/codex_project/fqsh/.runtime/codex_isolated`
- 清空继承环境后启动（`env -i`），不会污染你其他 codex 会话
- 启动前会从主 `CODEX_HOME` 同步 `auth.json/config.toml/version.json` 到隔离目录（只复制，不回写）
- 仅当前脚本拉起的会话受审批接管

若目标环境是只读沙箱且 `--add-dir` 被拒绝，可设置：
- `FQG_DISABLE_AGENTS_ADD_DIR=1`

如果主环境尚未登录，才需要做一次隔离登录初始化（不走审批 wrapper）：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
./scripts/bootstrap_codex_isolated_auth.sh
```

完成登录后，再启动受控会话：

```bash
./scripts/run_codex_guarded.sh
```

Leader 模式（避免“是否继续”停顿）：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
./scripts/run_codex_guarded_lead.sh
```

该模式会开启防停顿 auto-nudge，检测到“如果你同意我再继续 / Would you like me to ...”类话术时，自动发送继续指令（默认最多 3 次）：
- `FQG_AUTO_CONTINUE_NUDGE=1`
- `FQG_CONTINUE_NUDGE_TEXT='继续执行直到任务完成后再汇报结果，不要等待我确认。'`
- `FQG_CONTINUE_NUDGE_COOLDOWN_SECONDS=8`
- `FQG_CONTINUE_NUDGE_MAX_COUNT=3`

指定其他网关地址：

```bash
FQG_GATEWAY_URL='http://127.0.0.1:3001' ./scripts/run_codex_guarded.sh
```

受控启动会自动记录最近一次 Codex 会话 ID 到：

```text
/Users/yangxi/claude/codex_project/fqsh/.runtime/codex_isolated/codex_home/last_codex_session_id
```

直接恢复最近一次受控会话：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
FQG_GATEWAY_URL='http://8.140.215.219:3001' ./scripts/resume_last_guarded_session.sh
```

会话主动监控（状态变化自动推送钉钉）：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
chmod +x ./scripts/watch_guarded_session.sh
./scripts/watch_guarded_session.sh --watch --interval-seconds 5 \
  --notify-dingtalk-webhook 'https://oapi.dingtalk.com/robot/send?access_token=***'
```

只看一次当前状态（不持续监控）：

```bash
./scripts/watch_guarded_session.sh
```

指定会话监控：

```bash
./scripts/watch_guarded_session.sh --session-id 019cb379-59ff-72f1-9380-2e1b687257a6
```

会话卡在 `WAITING_INPUT` 时，一键自动继续（tmux 主路径 + resume 兜底）：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
python3 ./scripts/guarded_session_control.py continue \
  --session-id 019cb379-59ff-72f1-9380-2e1b687257a6 \
  --text '继续执行' \
  --json
```

说明：
- 脚本会优先向已存在的 tmux 会话发送按键（`send-keys`）。
- 若会话不存在或不可用，会自动新建 tmux 会话并执行 `run_codex_guarded_lead.sh resume <sid> <text>`。
- tmux socket 默认写入：`/Users/yangxi/claude/codex_project/fqsh/.runtime/codex_isolated/codex_home/session_monitor/tmux/<sid>.sock`

watchdog 自动恢复（持续监控 + 冷却控制）：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
./scripts/watch_guarded_session.sh --watch \
  --session-id 019cb379-59ff-72f1-9380-2e1b687257a6 \
  --auto-continue \
  --auto-continue-text '继续执行' \
  --auto-continue-cooldown-seconds 180
```

openclaw 风格自然语言驱动（多 identity 路由）：

1. 准备 identity 路由文件（默认 `.runtime/identity_routes.json`）：

```json
{
  "identities": {
    "feiqiao-guard-delivery-lead": {
      "session_id": "019cb379-59ff-72f1-9380-2e1b687257a6",
      "codex_home": "/Users/yangxi/claude/codex_project/fqsh/.runtime/codex_isolated/codex_home_lead",
      "session_name_prefix": "fqg-lead",
      "verify_seconds": 8
    },
    "feiqiao-guard-collab-executor": {
      "session_id": "02d4ad3a-bf3d-4dc6-aa5a-7fc8a74ad201",
      "codex_home": "/Users/yangxi/claude/codex_project/fqsh/.runtime/codex_isolated/codex_home_collab",
      "session_name_prefix": "fqg-collab",
      "verify_seconds": 8
    }
  }
}
```

2. 查看当前路由：

```bash
curl -sS http://127.0.0.1:8765/v1/chat/routes
```

3. 向指定 identity 下发自然语言任务（服务会转发到对应 Codex 会话并触发继续执行）：

```bash
curl -sS -X POST http://127.0.0.1:8765/v1/chat/inbound \
  -H 'Content-Type: application/json' \
  -d '{
    "identity_id": "feiqiao-guard-collab-executor",
    "message": "继续执行当前任务，完成后把测试结果和证据路径回传。"
  }'
```

说明：
- `/v1/chat/inbound` 底层复用 `scripts/guarded_session_control.py continue`，不是新增一套并行控制栈。
- 若 route 存在 session/codex_home 冲突（多 identity 复用同一执行上下文），服务会 fail-close 返回 `409`，并在 `/v1/chat/routes` 标记 `route_status=error`。
- 为避免多 identity 串线，建议每个 identity 使用独立 `codex_home`；仅在显式 `allow_shared_session=true + switch_ack_ref` 一致时允许共享。
- 每次请求会写审计事件到 `FQG_AUDIT_LOG_PATH`，方便回放“谁在何时把什么消息发给哪个 identity”。
- 返回字段 `delivery_state` 语义：
  - `confirmed`：rollout 已观测到推进（强确认）
  - `queued`：tmux 投递成功，但 verify 窗口内未观测到 rollout 推进（弱确认，建议继续 watch）
  - `failed`：tmux/控制链路失败（需人工介入）

leader 编排入口（聊天窗只指挥 leader，系统自动拉 collab 协作）：

```bash
curl -sS -X POST http://127.0.0.1:8765/v1/chat/leader/command \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "继续推进本次需求，完成实现与测试后回传证据路径。",
    "auto_collab": true
  }'
```

说明：
- endpoint 会先派发到 `FQG_CHAT_LEADER_IDENTITY_ID`，再按 `auto_collab=true` 自动派发到 `FQG_CHAT_COLLAB_IDENTITY_ID`。
- 返回中的 `leader_result/collab_result` 分别展示两条投递链路的状态；`orchestration_notes` 用于标记弱确认或协作异常。

交付矩阵一键验证（构建 + 单测 + continue 联调 + watchdog + 可选远端 smoke）：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
SSH_PASSWORD='***' ./scripts/run_delivery_matrix.sh \
  --enable-continue \
  --enable-server-smoke \
  --session-id 019cb379-59ff-72f1-9380-2e1b687257a6 \
  --codex-home /Users/yangxi/claude/codex_project/fqsh/.runtime/codex_isolated/codex_home
```

输出产物：
- `status.json`：READY/NOT_READY 结论
- `command_summary.tsv`：逐命令 `rc` 总表
- 其余 `*.log`：每一步原始执行证据
- 默认目录：`/tmp/fqg_matrix_YYYYmmdd_HHMMSS`

说明：
- 矩阵已纳入 `tests/test_approval_flow.py`、`tests/test_dingtalk_client.py`、`tests/test_chat_inbound.py`、`tests/test_chat_bridge.py`，覆盖双人审批、重复投票幂等、拒绝短路、decision-link、自然语言路由分发、Stream 桥接策略/幂等。
- `--enable-continue` 会先做 watchdog 状态预检；仅当状态为 `WAITING_INPUT` 才执行 required continue，其余状态记录 `skip_not_waiting_input`（optional）。
- `--enable-server-smoke` 支持 SSH 优先、公网 HTTP 回退（`--server-public-base-url`），避免仅因 SSH 凭据漂移导致矩阵误判。

## DingTalk Mode

钉钉自定义群机器人是单向推送，无法原生“回推消息”给你的服务。  
本项目采用按钮链接回调方案：

1. 服务创建审批后发送 actionCard 到钉钉群。
2. 群里点击 `同意执行/拒绝执行` 按钮。
3. 按钮打开服务的 `/v1/decision-link`，服务完成审批并回写终端。

卡片标题包含 `risk_level + request_id 前缀`，用于在钉钉会话中快速区分同类审批消息。

确保 `FQG_CALLBACK_BASE_URL` 是公网可访问地址（当前服务器实测为 `http://8.140.215.219:3001`）。

### DingTalk Stream Bridge（手机自然语言双向）

如果你要实现“手机发一句话 -> leader/collab 自动执行 -> 手机收到执行结果”，请使用 Stream 模式桥接进程（不是 webhook 群机器人）。

桥接脚本：
- `/Users/yangxi/claude/codex_project/fqsh/scripts/run_dingtalk_stream_bridge.py`

关键能力：
- 入站消息过滤：群聊必须 `@bot`（可关闭）、发送人/会话白名单。
- 幂等防重：按 `msgId` 做本地去重，避免同一消息重复触发执行。
- 执行复用：调用现有 `/v1/chat/leader/command`，不新增并行控制栈。
- 出站回执：先回“已受理（含 `trace_id/task_id`）”，再回“leader/collab 分发结果”，并连续推送进度快照（`progress=i/N`）。

本地启动示例（绝对路径）：

```bash
cd /Users/yangxi/claude/codex_project/fqsh
python3 -m pip install dingtalk-stream
FQG_DINGTALK_STREAM_CLIENT_ID='***' \
FQG_DINGTALK_STREAM_CLIENT_SECRET='***' \
FQG_BRIDGE_BASE_URL='http://127.0.0.1:3001' \
FQG_BRIDGE_ALLOW_USER_IDS='zhouqihang' \
FQG_BRIDGE_ALLOW_CHAT_IDS='cid***' \
FQG_BRIDGE_COMMAND_PREFIXES='/run,/cmd' \
FQG_BRIDGE_REQUIRE_PREFIX=true \
FQG_BRIDGE_FOLLOWUP_SECONDS=8 \
FQG_BRIDGE_PROGRESS_PUSH_COUNT=3 \
python3 /Users/yangxi/claude/codex_project/fqsh/scripts/run_dingtalk_stream_bridge.py
```

说明：
- `FQG_BRIDGE_REQUIRE_PREFIX=true` 时，只接受 `/run xxx` 或 `/cmd xxx`；可减少误触发。
- 若希望群里自然语言直接触发，可关闭前缀约束（默认关闭），保留 `@bot` + 白名单。
- 默认会在分发后按 `FQG_BRIDGE_FOLLOWUP_SECONDS` 间隔推送 `FQG_BRIDGE_PROGRESS_PUSH_COUNT` 次会话快照，避免“看起来没动”。
- 隔离依赖 `.runtime/identity_routes.json` 的 lead/collab 路由，不会影响其他 Codex 会话。

## Callback Signature

签名原文（换行连接）：

```text
request_id
action
token
nonce
approver
timestamp
```

签名算法：
- HMAC-SHA256
- key = `FQG_CALLBACK_SIGNING_SECRET`
- hexdigest 放入 `signature`

`FQG_BYPASS_SIGNATURE_VERIFICATION=true` 可用于本地调试。

## Server Deploy

```bash
cd /Users/yangxi/claude/codex_project/fqsh
FQG_SERVICE_PORT=3001 \
FQG_PUBLIC_BASE_URL='http://8.140.215.219:3001' \
FQG_DINGTALK_WEBHOOK_URL='https://oapi.dingtalk.com/robot/send?access_token=***' \
FQG_DINGTALK_SIGNING_SECRET='' \
FQG_CHAT_CONTROL_TIMEOUT_SECONDS=90 \
FQG_CHAT_DEFAULT_VERIFY_SECONDS=8 \
FQG_DISABLE_AGENTS_ADD_DIR=1 \
SSH_PASSWORD='***' \
./scripts/deploy_fqg_server.sh 8.140.215.219 root
```

说明：
- 部署脚本会自动写入 `FQG_IDENTITY_ROUTES_PATH/FQG_CHAT_CONTROL_TIMEOUT_SECONDS/FQG_CHAT_DEFAULT_VERIFY_SECONDS/FQG_DISABLE_AGENTS_ADD_DIR` 到远端 `.env`。
- 若远端不存在路由文件，会自动生成 `/root/feiqiao-guard/.runtime/identity_routes.json`（`lead/collab` 双 identity 默认模板）。
- `FQG_DINGTALK_WEBHOOK_URL` 为空时，`codex_wrapper` 来源的审批会被快速拒绝（`no_notification_channel`），避免终端静默卡在 `PENDING`。
- 如果 webhook 可用但发送失败，会快速拒绝（`notification_delivery_failed`），wrapper 会自动回退 `ESC`。

当前已验证公网地址：

```bash
curl -sS http://8.140.215.219:3001/healthz
```
