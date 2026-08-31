# ROADMAP — aibase（kit 源仓库）路线图（L2 执行层）

> 唯一权威：本文件（`docs/ROADMAP.md`），由 Manager 唯一写盘。
> 阶段是"方向航图"，执行一律以 `runtime/tasks/` 的 TASK 为准。
> 治理规则见 `kit/aios/governance/roadmap-policy.md`。

| 阶段 | 状态 | 目标 | owner | 对应 TASK | 完成标志 |
|------|------|------|-------|-----------|----------|
| Phase 1: 框架自举 | done | 框架模板/CLI/autoloop/多机同步/Windows 安装/kit 布局 | hb-session | TASK-001..021 | 21/21 done |
| Phase 2: 遥测 agent 组件 | done（含缺口收口：071 ✅ / 070 已销账 2026-08-30） | `kit/tools/agent/`：AIOS 通用遥测推送组件（读取 runtime → 推送到监控端），随 mkproject 自动分发，消除被监控端安装动作 | hb-session | TASK-022..029, 035 ＋ 缺口 TASK-070（已销账 2026-08-30：远端部署 hb 人工完成）、TASK-071 ✅（事件流服务端消费，2026-08-29 收口） | 组件文档 + 集成验证通过 |
| Phase 3: 多项目自动调度 | done | 调度器读多项目 `runtime/tasks/` 分配 coder/reviewer；中央不直写远端文件系统，下行指令由目标项目本地 agent 落盘（Ralph 模式跨项目成立） | hb-session | TASK-068..076 ✅（调度器核心）＋ TASK-071 ✅（事件流服务端消费）＋ TASK-084 ✅（D-3 aibase dogfood 自指环，2026-08-29） ｜遗留已清：TASK-070/080 已由人工解决并取消销账（2026-08-30） | 调度器可用：多项目任务自动分配/回收/超时；跨项目下 governance 闸门仍生效（P0 人工批准、返工超限转人工） |
| Phase 4: 点子→成品流水线 | open | 跨项目 DAG + 阶段人工闸门 + 成品验收 | hb-session | —（前置 Phase 3 ✅；TASK-070/080 已销账 2026-08-30，前置已清） | 愿景，需逐步验证 |

## 变更记录

- 2026-08-30：Phase 3 遗留跟踪项收口：TASK-070（远端 agent 上线）与 TASK-080（claude CLI 重登）由 hb 人工解决，按会话指令取消销账（blocked → cancelled 终态；原卷宗在 Phase 3 执行机不入库，本机已补档 runtime/tasks/TASK-070/080）。Phase 4 前置已清。人工确认：hb（本会话指令）。Manager 落盘。
- 2026-08-29：Phase 3 由 in-progress → done：D-3 aibase dogfood 已由 TASK-084 收口（dispatcher 以 aibase 自身为下行目标跑通 allocate→downlink→本地链，设计稿 §八 D-3 已标注「已执行（2026-08-29，TASK-084）」；独立 REVIEW pass）。Phase 3 完成标志达成 = 调度器可用（多项目自动分配/回收/超时 + governance 闸门 + westhill/x1design 集成验证 + aibase 自指环 dogfood）。TASK-070（远端 agent 上线，blocked-人工：4 个 Windows 项目待 hb 部署）与 TASK-080（claude CLI 重登，blocked-人工）转为 Phase 4 前置/外围跟踪，非 Phase 3 交付物。TASK-083（server_smoke 时序 flake）已于同日由 aimonitor b015dc7 修复闭环。人工确认：hb（本会话指令）。Manager 落盘。
- 2026-08-29：Phase 3 由 open → in-progress：核心交付 TASK-068..076 已 9/9 done（调度器 scan/allocate/downlink/dispatch + governance 闸门 + 2 真实项目闭环验证，见 `docs/PHASE3-SCHEDULER-DESIGN.md` §七）；剩余完成标志项 = 远端链路（TASK-070，blocked-人工：4 个 Windows 项目 agent 未上线）＋ aibase 自身 dogfood 补验（设计稿 D-3）。TASK-080（claude CLI 重登，blocked-人工）与 TASK-083（server_smoke 时序 flake）为外围依赖跟踪项，非 Phase 3 交付物。Phase 2 保持 done，缺口收口状态并入行内标注（TASK-071 2026-08-29 done，REVIEW-2026-08-29-task-071 pass；TASK-070 待 hb 远端部署）。人工确认：hb（本会话指令）。Manager 落盘。
- 2026-08-28：新增 Phase 3（多项目自动调度，open）与 Phase 4（点子→成品流水线，open 愿景）。提议来源：PPT 演进路线四阶段表（TASK-059/060）+ glossary Harness 选型结论（TASK-064，Ralph 跨项目扩展原则）；人工确认：hb；Manager 落盘。Phase 3 对应 TASK 已创建：TASK-068（范围定义 + 架构设计，P2）。
- 2026-08-18：Phase 2 标记 done（TASK-022..029 8/8 done + TASK-035）。依据：组件文档（`kit/tools/agent/README.md`）+ 集成验证（mkproject 自动分发 TASK-029、aimonitor 双机推送/离线实测 TASK-042、双平台部署手册 `docs/DEPLOY-DUAL-PLATFORM.md` 逐项核对实现通过）。人工确认：hb（resume 会话）。Manager 落盘。
- 2026-08-16：Phase 1 标记 done（21/21）；新增 Phase 2（遥测 agent 组件）。提议来源：aimonitor 多机监控架构分析（agent 归属修订为框架通用组件，随 mkproject 分发）；人工确认：hb。Manager 落盘。
