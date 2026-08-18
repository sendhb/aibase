# ROADMAP — aibase（kit 源仓库）路线图（L2 执行层）

> 唯一权威：本文件（`docs/ROADMAP.md`），由 Manager 唯一写盘。
> 阶段是"方向航图"，执行一律以 `runtime/tasks/` 的 TASK 为准。
> 治理规则见 `kit/aios/governance/roadmap-policy.md`。

| 阶段 | 状态 | 目标 | owner | 对应 TASK | 完成标志 |
|------|------|------|-------|-----------|----------|
| Phase 1: 框架自举 | done | 框架模板/CLI/autoloop/多机同步/Windows 安装/kit 布局 | hb-session | TASK-001..021 | 21/21 done |
| Phase 2: 遥测 agent 组件 | done | `kit/tools/agent/`：AIOS 通用遥测推送组件（读取 runtime → 推送到监控端），随 mkproject 自动分发，消除被监控端安装动作 | hb-session | TASK-022..029, 035 | 组件文档 + 集成验证通过 |

## 变更记录

- 2026-08-18：Phase 2 标记 done（TASK-022..029 8/8 done + TASK-035）。依据：组件文档（`kit/tools/agent/README.md`）+ 集成验证（mkproject 自动分发 TASK-029、aimonitor 双机推送/离线实测 TASK-042、双平台部署手册 `docs/DEPLOY-DUAL-PLATFORM.md` 逐项核对实现通过）。人工确认：hb（resume 会话）。Manager 落盘。
- 2026-08-16：Phase 1 标记 done（21/21）；新增 Phase 2（遥测 agent 组件）。提议来源：aimonitor 多机监控架构分析（agent 归属修订为框架通用组件，随 mkproject 分发）；人工确认：hb。Manager 落盘。
