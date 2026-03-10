# OpenClaw 经典场景来源与36 Case组（联网核查）

更新时间：2026-03-08

## 一手来源（官方/项目文档）

1. Showcase（社区经典场景总览）
- https://open-claw.bot/docs/start/showcase
- 关键段落：
  - 开发提效：Linear CLI / CodexMonitor / PR Reviews
  - 日常自动化：Tesco / ParentPay / Beeper / Vienna Transport
  - 真实案例：Jira Skill Builder / Slack Auto-Support / Job Search / TradingView
  - 记忆体系：WhatsApp Memory Vault / Karakeep / Inside-Out / xuezh

2. 协同与多Agent
- Sub-Agents: https://docs.openclaw.ai/tools/subagents
- Multi-Agent Routing: https://docs.openclaw.ai/concepts/multi-agent
- Multi-Agent Sandbox & Tools: https://docs.openclaw.ai/tools/multi-agent-sandbox-tools

3. 浏览器能力
- Browser: https://docs.openclaw.ai/tools/browser
- Browser Login: https://docs.openclaw.ai/tools/browser-login

4. 调度与触发
- Cron Jobs: https://docs.openclaw.ai/automation/cron-jobs
- Cron vs Heartbeat: https://docs.openclaw.ai/automation/cron-vs-heartbeat
- Webhook: https://docs.openclaw.ai/automation/webhook
- Gmail PubSub: https://docs.openclaw.ai/automation/gmail-pubsub

5. 多模态能力
- Media Understanding: https://docs.openclaw.ai/nodes/media-understanding
- Image & Media Support: https://docs.openclaw.ai/nodes/images

## 36 Case 分组（已落地到脚本）

脚本：`/Users/yangxi/claude/codex_project/fqsh/scripts/run_36case_openclaw_classic_suite.py`

- C01-C06 生产力与研发
  - Linear CLI / CodexMonitor / PR Review / Jira Skill / Slack Auto-Support / Todoist via Telegram
- C07-C12 日常自动化
  - Tesco / ParentPay / Vienna Transport / Beeper / Padel / Accounting Intake
- C13-C18 浏览器自动化
  - TradingView / Job Search / Couch Potato Dev / Missing Skill / Browser Login / Managed Browser
- C19-C24 知识与记忆
  - WhatsApp Memory Vault / Karakeep / Inside-Out / xuezh / 偏好写入 / 偏好召回
- C25-C30 多Agent协同
  - subagent并行 / depth2编排 / 路由隔离 / 工具最小权限 / 回传协议 / 并发超时
- C31-C36 触发与调度+多模态
  - cron / heartbeat / 选型 / webhook / gmail pubsub / media understanding

## 说明

- 这版 case 已全部改为“自然语义任务”，并且每条测试都带 `category + scenario_source`。
- 钉钉 START 消息会显示来源链接，便于人工追溯。
