#!/usr/bin/env node
/**
 * build_aibase_ppt.mjs — 用 pptxgenjs 按 docs/PPT-AIBASE-INTRO-OUTLINE.md 生成
 * docs/PPT-AIBASE-INTRO-pptxgenjs.pptx（20 页，深色科技风）。
 *
 * 运行：cd docs/ppt && node build_aibase_ppt.mjs
 * 依赖：pptxgenjs@4.x（TASK-061 安装）
 */
import { fileURLToPath } from "node:url";
import pptxgen from "pptxgenjs";

// ---------- 主题 ----------
const SW = 13.333, SH = 7.5;
const FONT = "Microsoft YaHei";
const MONO = "Consolas";
const C = {
  bg: "0B1220", panel: "131C30", card: "1A2740", cardAlt: "16223A", line: "24324A",
  accent: "22D3EE", blue: "3B82F6", purple: "A78BFA", green: "34D399",
  red: "F87171", amber: "FBBF24", text: "E2E8F0", muted: "8CA3C2",
};
const OUT = fileURLToPath(new URL("../PPT-AIBASE-INTRO-pptxgenjs.pptx", import.meta.url));

const pptx = new pptxgen();
pptx.defineLayout({ name: "WIDE", width: SW, height: SH });
pptx.layout = "WIDE";
pptx.author = "hb";
pptx.company = "aibase";
pptx.title = "aibase — AI Agent Operating System + Reliable AI Engineering Framework";
pptx.subject = "AIOS + Reliable AI Engineering Framework 介绍（20 页）";
pptx.lang = "zh-CN";

let pageNo = 0;
const PAGE = {};

// ---------- 通用构件 ----------
function bg(title, accent = C.accent) {
  const s = pptx.addSlide();
  PAGE[++pageNo] = s;
  s.background = { color: C.bg };
  s.addShape(pptx.ShapeType.rect, { x: 0, y: 0, w: SW, h: 0.06, fill: { color: accent }, line: { type: "none" } });
  if (title) {
    s.addText(title, {
      x: 0.6, y: 0.32, w: 11.2, h: 0.62, fontFace: FONT, fontSize: 25, bold: true,
      color: C.text, align: "left", valign: "middle",
    });
    s.addShape(pptx.ShapeType.rect, { x: 0.62, y: 0.98, w: 1.25, h: 0.045, fill: { color: accent }, line: { type: "none" } });
  }
  s.addText("aibase · AIOS Framework", { x: 0.6, y: SH - 0.52, w: 3.2, h: 0.3, fontFace: FONT, fontSize: 10, color: C.muted, align: "left", valign: "middle" });
  s.addText(String(pageNo).padStart(2, "0"), { x: SW - 1.0, y: SH - 0.52, w: 0.5, h: 0.3, fontFace: FONT, fontSize: 11, color: C.muted, align: "right", valign: "middle" });
  return s;
}

function card(s, x, y, w, h, title, body, accent = C.accent, bodyColor = C.muted) {
  s.addShape(pptx.ShapeType.roundRect, { x, y, w, h, rectRadius: 8, fill: { color: C.card }, line: { color: C.line, width: 1 } });
  if (title) {
    s.addShape(pptx.ShapeType.rect, { x, y, w: 0.07, h, fill: { color: accent }, line: { type: "none" } });
    s.addText(title, { x: x + 0.22, y: y + 0.1, w: w - 0.4, h: 0.42, fontFace: FONT, fontSize: 15, bold: true, color: C.text, align: "left", valign: "middle" });
  }
  if (body) {
    const bodyRuns = Array.isArray(body)
      ? body.map((b, i) => ({ text: b.t, options: { color: b.c, breakLine: i < body.length - 1 } }))
      : body;
    s.addText(bodyRuns, {
      x: x + 0.22, y: y + (title ? 0.52 : 0.12), w: w - 0.42, h: h - (title ? 0.62 : 0.22),
      fontFace: FONT, fontSize: 12, color: bodyColor, align: "left", valign: "top",
      lineSpacing: 17, fit: "shrink",
    });
  }
  return s;
}

function chip(s, x, y, w, text, bgColor, fgColor = C.text, fontSize = 12) {
  s.addShape(pptx.ShapeType.roundRect, { x, y, w, h: 0.42, rectRadius: 6, fill: { color: bgColor }, line: { type: "none" } });
  s.addText(text, { x, y: y - 0.01, w, h: 0.42, fontFace: FONT, fontSize, bold: true, color: fgColor, align: "center", valign: "middle" });
}

function arrow(s, x, y, w, color = C.blue) {
  s.addShape(pptx.ShapeType.rightArrow, { x, y, w, h: 0.3, fill: { color }, line: { type: "none" } });
}

function table(s, x, y, w, headers, rows, colW, opts = {}) {
  const hdr = headers.map((h) => ({ text: h, options: { bold: true, color: C.text, fill: { color: C.blue }, align: "center", valign: "middle" } }));
  const body = rows.map((r, i) => r.map((cell) => {
    const isObj = typeof cell === "object" && cell !== null && "text" in cell;
    return {
      text: isObj ? cell.text : String(cell),
      options: {
        color: C.text, fill: { color: i % 2 === 0 ? C.card : C.cardAlt },
        align: isObj && cell.align ? cell.align : "left", valign: "middle",
        fontSize: opts.fontSize || 11.5,
      },
    };
  }));
  s.addTable([hdr, ...body], {
    x, y, w, colW, rowH: opts.rowH || 0.46,
    border: { type: "solid", pt: 0.5, color: C.line },
    fontFace: FONT, margin: 4, valign: "middle",
  });
}

function codeBlock(s, x, y, w, h, lines, fontSize = 11.5) {
  s.addShape(pptx.ShapeType.roundRect, { x, y, w, h, rectRadius: 6, fill: { color: "0D1526" }, line: { color: C.line, width: 1 } });
  const runs = lines.map((ln, i) => ({ text: ln, options: { breakLine: i < lines.length - 1 } }));
  s.addText(runs, {
    x: x + 0.2, y: y + 0.12, w: w - 0.4, h: h - 0.24,
    fontFace: MONO, fontSize, color: C.green, align: "left", valign: "top", lineSpacing: 16, fit: "shrink",
  });
}

// ---------- 第 1 页 · 封面 ----------
function p01() {
  const s = bg(null, C.accent);
  s.addShape(pptx.ShapeType.ellipse, { x: 10.6, y: -1.6, w: 4.2, h: 4.2, fill: { color: C.blue, transparency: 88 }, line: { type: "none" } });
  s.addShape(pptx.ShapeType.ellipse, { x: 12.0, y: 4.9, w: 2.8, h: 2.8, fill: { color: C.purple, transparency: 88 }, line: { type: "none" } });
  s.addShape(pptx.ShapeType.ellipse, { x: -0.9, y: 5.6, w: 2.6, h: 2.6, fill: { color: C.accent, transparency: 92 }, line: { type: "none" } });
  // logo 块
  s.addShape(pptx.ShapeType.roundRect, { x: 0.95, y: 1.55, w: 1.35, h: 0.72, rectRadius: 10, fill: { color: "0E2038" }, line: { color: C.accent, width: 1.5 } });
  s.addText("AIOS", { x: 0.95, y: 1.6, w: 1.35, h: 0.62, fontFace: FONT, fontSize: 22, bold: true, color: C.accent, align: "center", valign: "middle" });
  s.addText("aibase", { x: 2.5, y: 1.5, w: 6, h: 0.8, fontFace: FONT, fontSize: 34, bold: true, color: C.text, align: "left", valign: "middle" });
  s.addText("AI Agent Operating System\n+ Reliable AI Engineering Framework", {
    x: 0.95, y: 2.6, w: 11.4, h: 1.15, fontFace: FONT, fontSize: 27, bold: true, color: C.text, align: "left", valign: "top", lineSpacing: 38,
  });
  s.addText("让 AI 从「会写代码」到「可靠地做成事」", { x: 0.97, y: 4.05, w: 11, h: 0.55, fontFace: FONT, fontSize: 17, color: C.muted, align: "left", valign: "middle" });
  s.addShape(pptx.ShapeType.rect, { x: 0.99, y: 4.78, w: 2.1, h: 0.05, fill: { color: C.accent }, line: { type: "none" } });
  s.addText("hb · MIT License · 2026", { x: 0.95, y: 5.0, w: 6, h: 0.4, fontFace: FONT, fontSize: 14, color: C.muted, align: "left" });
  chip(s, 0.95, 5.85, 3.4, "🧠 AIOS 内核", "1A2740");
  chip(s, 4.5, 5.85, 3.4, "🔒 Governance 协议", "1A2740");
  chip(s, 8.05, 5.85, 3.4, "🔁 可靠执行闭环", "1A2740");
  s.addText("20 页 · 45-60 分钟", { x: 0.95, y: 6.5, w: 6, h: 0.35, fontFace: FONT, fontSize: 12, color: C.muted, align: "left" });
}

// ---------- 第 2 页 · 背景与动机 ----------
function p02() {
  const s = bg("背景与动机：为什么需要它");
  card(s, 0.6, 1.45, 5.6, 3.3, "第一代假设", "AI 工具 → 读规则 → 执行 → 完成\n\n把 AI 当成一个「会读规则的助手」：规则写好，AI 照着做就行。", C.muted, C.text);
  const right = card(s, 6.75, 1.45, 6.0, 3.3, "2026 现实：没有治理约束时会失控", [
    { t: "• 乱改不该改的文件（生成目录）", c: C.red },
    { t: "• 忽略系统架构关系（不在上下文里）", c: C.red },
    { t: "• 无法自评（AI 不能审查自己的工作）", c: C.red },
    { t: "• 验证失败时没有修复机制", c: C.red },
  ], C.red, C.text);
  s.addText([
    { text: "2026：AI 已能自主规划、多步执行、调用工具", options: { breakLine: true, color: C.text, bold: true, fontSize: 14 } },
    { text: "但能力越大，越需要「操作系统」级的治理约束", options: { color: C.muted, fontSize: 13 } },
  ], { x: 0.6, y: 4.95, w: 12.15, h: 0.7, fontFace: FONT, align: "left", valign: "middle" });
  card(s, 0.6, 5.75, 12.15, 1.15, "结论", "定义 AI 如何 理解目标 → 建模 → 规划 → 执行 → 验证 → 学习", C.blue, C.text);
  return s;
}

// ---------- 第 3 页 · 定位 ----------
function p03() {
  const s = bg("定位：aibase 是什么");
  s.addText("不是「AI 配置模板」，是 AI Agent Operating System —— 完整工程治理框架", {
    x: 0.6, y: 1.2, w: 12.1, h: 0.5, fontFace: FONT, fontSize: 16, bold: true, color: C.accent, align: "left",
  });
  const data = [
    ["1", "🧠 AIOS 内核", "Governance + Cognition + Execution + Context + Memory", C.accent],
    ["2", "🤖 角色式 Agent", "按角色（Manager/Coder/Reviewer…）而非按工具定义", C.blue],
    ["3", "🔒 Governance 协议", "明确「AI 不可以做什么」的边界", C.purple],
    ["4", "🔁 可靠执行闭环", "Plan → Impact → Execute → Reflect → Verify → Repair", C.green],
    ["5", "📊 评估体系", "成功率 / 返工 / token / 模型对比", C.amber],
  ];
  let y = 1.85;
  for (const [no, t, d, color] of data) {
    s.addShape(pptx.ShapeType.roundRect, { x: 0.6, y, w: 12.15, h: 0.92, rectRadius: 8, fill: { color: C.card }, line: { color: C.line, width: 1 } });
    s.addShape(pptx.ShapeType.ellipse, { x: 0.82, y: y + 0.23, w: 0.46, h: 0.46, fill: { color }, line: { type: "none" } });
    s.addText(no, { x: 0.82, y: y + 0.25, w: 0.46, h: 0.42, fontFace: FONT, fontSize: 16, bold: true, color: "0B1220", align: "center", valign: "middle" });
    s.addText(t, { x: 1.5, y: y + 0.12, w: 4.4, h: 0.68, fontFace: FONT, fontSize: 15, bold: true, color: C.text, align: "left", valign: "middle" });
    s.addText(d, { x: 6.0, y: y + 0.12, w: 6.55, h: 0.68, fontFace: FONT, fontSize: 12.5, color: C.muted, align: "left", valign: "middle" });
    y += 1.02;
  }
  return s;
}

// ---------- 第 4 页 · 整体架构（kit 布局） ----------
function p04() {
  const s = bg("整体架构：kit 布局");
  s.addText("aibase 自身就是 kit 布局（自举 / dogfood）——升级 = 整体替换 kit/", {
    x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left",
  });
  const tree = [
    "AGENTS.md        项目入口（25+ AI 工具原生读取）",
    "kit/aios/        AIOS 内核：governance / cognition / execution / context / memory / policy",
    "kit/agents/      7 个角色定义",
    "kit/profiles/    项目类型模板（backend/game-server/unity/unreal/frontend/data/design/novel）",
    "kit/knowledge/   项目知识图谱",
    "kit/cli/         统一控制入口（task / mkproject / persona / protect / sandbox…）",
    "kit/evaluation/  质量评估体系",
    "runtime/         文件即数据库：任务/状态/验证/审查全在文件系统",
  ];
  codeBlock(s, 0.6, 1.85, 12.15, 4.5, tree, 12.5);
  card(s, 0.6, 6.5, 12.15, 0.75, null, "「文件即数据库」——所有协作状态零依赖、可移植。", C.blue, C.text);
  return s;
}

// ---------- 第 5 页 · 治理层 ----------
function p05() {
  const s = bg("治理层：AI 不可以做什么");
  s.addText("治理先于执行——任何 AI 动作前先过 governance 检查", {
    x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left",
  });
  table(s, 0.6, 1.85, 12.15,
    ["协议", "管什么"],
    [
      ["task-policy", "任务状态机；分级治理 fast-path；返工上限"],
      ["modification-policy", "目录可写/禁写；高风险文件分级"],
      ["security-policy", "Rule of Two；密钥不进提示词；训练退出"],
      ["risk-policy", "风险分级 P0-P3 与审批"],
      ["roadmap-policy", "路线图唯一权威与变更控制"],
    ],
    [2.6, 9.55], { rowH: 0.72, fontSize: 13 });
  return s;
}

// ---------- 第 6 页 · 风险分级与权限模型 ----------
function p06() {
  const s = bg("风险分级与权限模型");
  s.addText("治理强度随风险分级，不搞一刀切", { x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left" });
  const risks = [
    ["🔴 P0", "数据库 / 支付 / auth", "必须人工批准", C.red],
    ["🟡 P1", "API 契约 / CI 配置", "reviewer 审查", C.amber],
    ["🟢 P2", "常规修改", "常规流程", C.green],
    ["⚪ P3", "低风险", "fast-path", C.muted],
  ];
  let x = 0.6;
  for (const [lvl, what, act, color] of risks) {
    s.addShape(pptx.ShapeType.roundRect, { x, y: 1.95, w: 2.85, h: 1.5, rectRadius: 8, fill: { color: C.card }, line: { color, width: 1.5 } });
    s.addText(lvl, { x, y: 2.08, w: 2.85, h: 0.5, fontFace: FONT, fontSize: 17, bold: true, color, align: "center" });
    s.addText(what, { x: x + 0.15, y: 2.58, w: 2.55, h: 0.42, fontFace: FONT, fontSize: 12, color: C.text, align: "center", valign: "middle" });
    s.addText(act, { x: x + 0.15, y: 3.0, w: 2.55, h: 0.36, fontFace: FONT, fontSize: 11, color: C.muted, align: "center", valign: "middle" });
    x += 3.1;
  }
  card(s, 0.6, 3.8, 12.15, 1.1, "自动升级", "改动 >5 文件 / 跨 >2 模块 / 多语言 / 有未解决依赖 → 风险自动升一级", C.amber, C.text);
  card(s, 0.6, 5.1, 12.15, 1.65, "修改权限", "generated_dirs 禁写（改生成代码 = 改规格 → 重新生成）；只有 source_dirs 可写；protect 用 chmod 机械锁目录", C.purple, C.text);
  return s;
}

// ---------- 第 7 页 · 执行闭环 ----------
function p07() {
  const s = bg("执行闭环（Reliable Execution Loop）");
  s.addText("验证是流程的一部分，不是 AI 的自觉", { x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left" });
  // §0 分流
  s.addShape(pptx.ShapeType.roundRect, { x: 3.9, y: 1.75, w: 5.5, h: 0.85, rectRadius: 8, fill: { color: C.cardAlt }, line: { color: C.purple, width: 1.5 } });
  s.addText("§0 启动分流\n改规格？→ SDD 分支 / 否则通用路径", { x: 3.9, y: 1.8, w: 5.5, h: 0.78, fontFace: FONT, fontSize: 12, color: C.purple, bold: true, align: "center", valign: "middle", lineSpacing: 16 });
  s.addShape(pptx.ShapeType.line, { x: 6.65, y: 2.6, w: 0, h: 0.45, line: { color: C.purple, width: 2 } });
  // 六环
  const steps = [
    ["§1", "Plan", C.accent], ["§2", "Impact", C.blue], ["§3", "Execute", C.green],
    ["§4", "Reflect", C.amber], ["§5", "Verify 机械强制", C.red], ["§6", "Repair", C.purple],
  ];
  const bw = 1.72, gap = 0.3, startX = 0.6, y = 3.55;
  for (let i = 0; i < steps.length; i++) {
    const [no, label, color] = steps[i];
    const x = startX + i * (bw + gap);
    s.addShape(pptx.ShapeType.roundRect, { x, y, w: bw, h: 1.15, rectRadius: 8, fill: { color: C.card }, line: { color, width: 1.5 } });
    s.addText(no, { x, y: y + 0.12, w: bw, h: 0.35, fontFace: FONT, fontSize: 12, bold: true, color, align: "center" });
    s.addText(label, { x: x + 0.06, y: y + 0.5, w: bw - 0.12, h: 0.55, fontFace: FONT, fontSize: 12.5, bold: true, color: C.text, align: "center", valign: "top", lineSpacing: 15 });
    if (i < steps.length - 1) arrow(s, x + bw + 0.01, y + 0.45, gap - 0.02, C.line);
  }
  s.addShape(pptx.ShapeType.line, { x: 6.65, y: 3.05, w: 0, h: 0.5, line: { color: C.muted, width: 1.5, dashType: "dash" } });
  // 要点
  card(s, 0.6, 5.15, 12.15, 1.6, "关键点", [
    { t: "§5 Verify 机械强制：build / lint / test / check 真跑，cli/task verify 通过才写 VERIFY 记录", c: C.text },
    { t: "§6 Repair：返工上限 3 次，超限人工介入（不无限烧循环）", c: C.text },
  ], C.red, C.text);
  return s;
}

// ---------- 第 8 页 · SDD ----------
function p08() {
  const s = bg("SDD 规格驱动开发（可选详讲）");
  s.addText("规格是唯一真相，规格变更先于实现", { x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left" });
  const forms = [
    ["contract", "API 契约 + 代码生成"],
    ["docs", "工具 / 游戏客户端"],
    ["protocol", "CS 网游"],
  ];
  let x = 0.6;
  for (const [name, desc] of forms) {
    s.addShape(pptx.ShapeType.roundRect, { x, y: 1.85, w: 3.85, h: 1.1, rectRadius: 8, fill: { color: C.card }, line: { color: C.blue, width: 1.2 } });
    s.addText(name, { x, y: 1.98, w: 3.85, h: 0.45, fontFace: MONO, fontSize: 16, bold: true, color: C.accent, align: "center" });
    s.addText(desc, { x: x + 0.15, y: 2.45, w: 3.55, h: 0.4, fontFace: FONT, fontSize: 12, color: C.text, align: "center", valign: "middle" });
    x += 4.15;
  }
  const scenes = [
    ["A / A' / A\"", "规格变更", C.amber],
    ["B", "逻辑修改", C.green],
    ["C", "Bug 修复", C.red],
  ];
  x = 0.6;
  for (const [code, name, color] of scenes) {
    s.addShape(pptx.ShapeType.roundRect, { x, y: 3.35, w: 3.85, h: 0.9, rectRadius: 8, fill: { color: C.card }, line: { color, width: 1.2 } });
    s.addText(code, { x, y: 3.47, w: 1.7, h: 0.6, fontFace: MONO, fontSize: 15, bold: true, color, align: "center", valign: "middle" });
    s.addText(name, { x: x + 1.7, y: 3.47, w: 2.05, h: 0.6, fontFace: FONT, fontSize: 13, color: C.text, align: "center", valign: "middle" });
    x += 4.15;
  }
  card(s, 0.6, 4.6, 12.15, 2.1, "核心约束", [
    { t: "• 规格是唯一真相（single source of truth）", c: C.text },
    { t: "• 生成代码不可手动编辑（改规格 → 重新生成）", c: C.text },
    { t: "• 变更先于实现：先改规格，再实现", c: C.text },
    { t: "• 判断不清时按规格影响处理并在 TASK 记录假设", c: C.muted },
  ], C.purple, C.text);
  return s;
}

// ---------- 第 9 页 · 角色体系 ----------
function p09() {
  const s = bg("角色体系：按角色而非工具");
  s.addText("新问题：Coder 角色需要什么能力？  旧问题：Claude 能做什么？", {
    x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left",
  });
  table(s, 0.6, 1.8, 12.15,
    ["角色", "职责", "写代码", "关键约束"],
    [
      ["Manager", "任务分派、进度跟踪", "❌", "只管流程"],
      ["Analyst", "需求分析、影响评估", "❌", "不明不写"],
      ["Architect", "架构决策、技术选型", "❌", "ADR 记录"],
      ["Coder", "按计划实现", "✅", "必须过 verify"],
      ["Reviewer", "代码审查、安全检查", "❌", "生成者 ≠ 审查者"],
      ["Tester", "测试编写执行", "✅(测试)", "测试先行"],
      ["Researcher", "外部检索、知识录入", "❌", "写入 knowledge/"],
    ],
    [1.85, 5.2, 1.35, 3.75], { rowH: 0.52, fontSize: 12 });
  card(s, 0.6, 5.9, 12.15, 1.05, null, "新 AI 工具（Codex / Gemini CLI / Qwen Code…）不断出现 —— 按角色定义，无需为每个工具写适配器。", C.blue, C.text);
  return s;
}

// ---------- 第 10 页 · 四道墙 ----------
function p10() {
  const s = bg("机械强制层：四道墙");
  s.addText("规则写在文档里是建议，写在文件系统 / Git / CI 里才是纪律", {
    x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left",
  });
  const walls = [
    ["进程沙箱", "sandbox-run（无网络容器）", "Rule of Two：敏感数据不可外传", C.red],
    ["文件权限", "protect（chmod 锁 generated_dirs）", "手动改生成代码", C.amber],
    ["Git hook", "pre-commit / commit-msg", "无 TASK 引用、碰 generated_dirs", C.green],
    ["CI", "GitHub Actions verify.yml", "绕过本地 hook 也挡得住", C.blue],
  ];
  let y = 1.8;
  for (const [layer, mech, stops, color] of walls) {
    s.addShape(pptx.ShapeType.roundRect, { x: 0.6, y, w: 12.15, h: 1.05, rectRadius: 8, fill: { color: C.card }, line: { color: C.line, width: 1 } });
    s.addShape(pptx.ShapeType.rect, { x: 0.6, y, w: 0.07, h: 1.05, fill: { color }, line: { type: "none" } });
    s.addText(layer, { x: 0.95, y: y + 0.12, w: 2.0, h: 0.8, fontFace: FONT, fontSize: 15, bold: true, color: C.text, align: "left", valign: "middle" });
    s.addText(mech, { x: 3.1, y: y + 0.12, w: 5.6, h: 0.8, fontFace: FONT, fontSize: 12.5, color: C.text, align: "left", valign: "middle" });
    s.addText("挡：" + stops, { x: 8.85, y: y + 0.12, w: 3.7, h: 0.8, fontFace: FONT, fontSize: 12, color: C.muted, align: "left", valign: "middle" });
    y += 1.15;
  }
  card(s, 0.6, 6.45, 12.15, 0.75, null, "这是 aibase 与一般 prompt 模板的本质区别——不靠 AI 自觉，靠系统强制。", C.red, C.text);
  return s;
}

// ---------- 第 11 页 · CLI 工具箱 ----------
function p11() {
  const s = bg("CLI 工具箱");
  const cmds = [
    "mkproject — 创建项目（--profile / --persona）",
    "task new | list | start | verify | review | approve | done — 任务生命周期",
    "verify — 真实执行 build / lint / test / check",
    "persona list | use | off | show — 人格系统",
    "check / protect / sandbox-run — 健康检查 / 锁目录 / 沙箱",
    "autoloop-coder / autoloop-reviewer — 无人值守（实验性）",
  ];
  codeBlock(s, 0.6, 1.85, 12.15, 4.3, cmds, 13);
  chip(s, 0.6, 6.35, 6.0, "统一入口：kit/cli/（task / mkproject / persona …）", "1A2740", C.text, 12);
  chip(s, 6.8, 6.35, 5.95, "机械强制：task verify 真跑命令，不靠手写", "1A2740", C.text, 12);
  return s;
}

// ---------- 第 12 页 · 知识图谱 ----------
function p12() {
  const s = bg("知识图谱（knowledge/）");
  s.addText("AI 最大的问题不是不会写代码，而是不知道系统关系", {
    x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left",
  });
  const items = [
    ["architecture/", "架构"],
    ["modules/", "模块清单"],
    ["dependencies/", "依赖图"],
    ["decisions/", "ADR"],
    ["history/", "已知问题 / 技术债"],
    ["glossary/", "术语表"],
  ];
  let x = 0.6, y = 1.85;
  for (const [dir, desc] of items) {
    s.addShape(pptx.ShapeType.roundRect, { x, y, w: 3.85, h: 1.05, rectRadius: 8, fill: { color: C.card }, line: { color: C.blue, width: 1.2 } });
    s.addText(dir, { x, y: y + 0.14, w: 3.85, h: 0.4, fontFace: MONO, fontSize: 14, bold: true, color: C.accent, align: "center" });
    s.addText(desc, { x: x + 0.15, y: y + 0.55, w: 3.55, h: 0.36, fontFace: FONT, fontSize: 12, color: C.text, align: "center", valign: "middle" });
    x += 4.15;
    if (x > 12.4) { x = 0.6; y += 1.2; }
  }
  y += 0.25;
  card(s, 0.6, y, 12.15, 1.4, "原则", [
    { t: "Knowledge invisible to the agent doesn't exist.", c: C.text },
    { t: "Module before change：改代码前先读对应模块知识。", c: C.text },
  ], C.purple, C.text);
  return s;
}

// ---------- 第 13 页 · 评估体系 ----------
function p13() {
  const s = bg("评估体系（evaluation/）");
  s.addText("不评估 = 不知道 AI 做得好不好", { x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left" });
  const metrics = [
    ["成功率", C.green], ["返工次数", C.amber], ["Bug 率", C.red],
    ["Token", C.blue], ["耗时", C.purple], ["模型横向对比", C.accent],
  ];
  let x = 0.6;
  for (const [name, color] of metrics) {
    s.addShape(pptx.ShapeType.roundRect, { x, y: 1.85, w: 1.92, h: 0.85, rectRadius: 8, fill: { color: C.card }, line: { color, width: 1.2 } });
    s.addText(name, { x, y: 1.95, w: 1.92, h: 0.65, fontFace: FONT, fontSize: 13, bold: true, color: C.text, align: "center", valign: "middle" });
    x += 2.05;
  }
  card(s, 0.6, 3.0, 12.15, 1.35, "用途", [
    { t: "任务关闭 → 记 metrics", c: C.text },
    { t: "验证失败 → 记 failures", c: C.text },
    { t: "定期 → 出报告（模型横向对比）", c: C.text },
  ], C.blue, C.text);
  s.addShape(pptx.ShapeType.roundRect, { x: 0.6, y: 4.6, w: 12.15, h: 2.0, rectRadius: 8, fill: { color: C.card }, line: { color: C.line, width: 1 } });
  s.addText("示例：同一任务 Claude Sonnet vs GPT-4o", { x: 0.82, y: 4.75, w: 11.7, h: 0.4, fontFace: FONT, fontSize: 14, bold: true, color: C.text, align: "left" });
  s.addShape(pptx.ShapeType.roundRect, { x: 0.9, y: 5.3, w: 5.5, h: 1.1, rectRadius: 6, fill: { color: C.cardAlt }, line: { color: C.amber, width: 1 } });
  s.addText("Claude Sonnet\n成功率 92% · 返工 1 次 · 38K token", { x: 0.9, y: 5.4, w: 5.5, h: 0.95, fontFace: FONT, fontSize: 12, color: C.text, align: "center", valign: "middle", lineSpacing: 16 });
  s.addShape(pptx.ShapeType.roundRect, { x: 6.95, y: 5.3, w: 5.5, h: 1.1, rectRadius: 6, fill: { color: C.cardAlt }, line: { color: C.blue, width: 1 } });
  s.addText("GPT-4o\n成功率 80% · 返工 3 次 · 52K token", { x: 6.95, y: 5.4, w: 5.5, h: 0.95, fontFace: FONT, fontSize: 12, color: C.text, align: "center", valign: "middle", lineSpacing: 16 });
  return s;
}

// ---------- 第 14 页 · 人格系统 ----------
function p14() {
  const s = bg("人格系统（表达层设定）");
  s.addText("人格只改语气，不改结论；治理纪律不变", { x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left" });
  card(s, 0.6, 1.8, 12.15, 1.25, "人格 = 表达层设定", "语气 / 修辞 / 叙事。绝不改变推理、结论、工程判断与治理纪律。", C.purple, C.text);
  card(s, 0.6, 3.2, 5.9, 1.25, "按需加载", "personas/active.md 存在则激活；缺失则零加载（不占上下文）。", C.blue, C.text);
  card(s, 6.85, 3.2, 5.9, 1.25, "单源真相 + 薄壳指路", "人格库 50+；工具薄壳只指路，不重复内容，避免双源漂移。", C.green, C.text);
  card(s, 0.6, 4.6, 12.15, 1.5, "随时去人格化", "「严谨模式」可随时切换；切换：kit/cli/persona list | use | off | show", C.amber, C.text);
  chip(s, 0.6, 6.35, 6.0, "人格是表达层，不是行为层", "1A2740", C.text, 12);
  chip(s, 6.8, 6.35, 5.95, "工程判断永远优先于语气", "1A2740", C.text, 12);
  return s;
}

// ---------- 第 15 页 · 快速上手 ----------
function p15() {
  const s = bg("快速上手（5 分钟 Demo）");
  const cmds = [
    "git clone <aibase> /tmp/framework",
    "bash /tmp/framework/kit/cli/mkproject ~/code/my-project --profile backend",
    "bash kit/cli/protect",
    'bash kit/cli/task new "实现用户登录" --priority P1 --reviewer claude',
    "bash kit/cli/task start TASK-001",
    "bash kit/cli/task verify TASK-001",
    "bash kit/cli/task review TASK-001",
    "bash kit/cli/task approve TASK-001",
  ];
  codeBlock(s, 0.6, 1.75, 12.15, 4.4, cmds, 13.5);
  card(s, 0.6, 6.35, 12.15, 0.8, null, "重点：verify 是「真跑命令」（build/lint/test/check），不是手写记录。", C.green, C.text);
  return s;
}

// ---------- 第 16 页 · dogfood 实证 ----------
function p16() {
  const s = bg("实际应用：dogfood 实证");
  s.addText("框架自己就是自己的第一个用户", { x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left" });
  card(s, 0.6, 1.8, 6.0, 1.5, "aibase 自举", "自身即 kit 布局；62 个 TASK，任务 / 审查 / 验证记录齐全（runtime/tasks + verification/ + reviews/）。", C.accent, C.text);
  card(s, 6.85, 1.8, 5.9, 1.5, "已注册项目（aimonitor 监控）", "aimonitor / aibase / westhill / x1design / account-1 / baseline / x1prototype / hb-share-*（10 个，含远程 Windows/Linux）", C.blue, C.text);
  const demos = [
    ["cli/task list", "任务全景：状态、分级、耗时"],
    ["REVIEW 记录", "生成者 ≠ 审查者，可审计"],
    ["VERIFY 记录", "build/lint/test/check 真跑证据"],
    ["autoloop 心跳日志", "无人值守循环运行轨迹"],
  ];
  let y = 3.5;
  for (const [cmd, desc] of demos) {
    s.addShape(pptx.ShapeType.roundRect, { x: 0.6, y, w: 12.15, h: 0.75, rectRadius: 6, fill: { color: C.card }, line: { color: C.line, width: 1 } });
    s.addText(cmd, { x: 0.85, y: y + 0.1, w: 4.2, h: 0.55, fontFace: MONO, fontSize: 13, bold: true, color: C.green, align: "left", valign: "middle" });
    s.addText(desc, { x: 5.3, y: y + 0.1, w: 7.2, h: 0.55, fontFace: FONT, fontSize: 12.5, color: C.text, align: "left", valign: "middle" });
    y += 0.85;
  }
  return s;
}

// ---------- 第 17 页 · aimonitor ----------
function p17() {
  const s = bg("aimonitor：框架造出的第一个成品", C.green);
  s.addText("一个实时监控多个 AI 项目的仪表盘，也是框架设计的「活体验证」", {
    x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.green, align: "left",
  });
  // 架构流
  const flow = [
    ["被监控项目", "runtime/ 文件即数据库", C.blue],
    ["采集", "local 直读 / agent 推送", C.purple],
    ["aimonitor 仪表盘", "http://localhost:3113", C.green],
  ];
  let x = 0.6;
  for (let i = 0; i < flow.length; i++) {
    const [t, d, color] = flow[i];
    s.addShape(pptx.ShapeType.roundRect, { x, y: 1.8, w: 3.5, h: 1.0, rectRadius: 8, fill: { color: C.card }, line: { color, width: 1.5 } });
    s.addText(t, { x, y: 1.92, w: 3.5, h: 0.42, fontFace: FONT, fontSize: 14, bold: true, color: C.text, align: "center", valign: "middle" });
    s.addText(d, { x: x + 0.12, y: 2.36, w: 3.26, h: 0.38, fontFace: FONT, fontSize: 10.5, color: C.muted, align: "center", valign: "middle" });
    if (i < flow.length - 1) arrow(s, x + 3.55, 2.15, 0.44, C.line);
    x += 4.05;
  }
  // 四件事
  s.addText("它证明了四件事", { x: 0.6, y: 3.05, w: 6, h: 0.4, fontFace: FONT, fontSize: 14, bold: true, color: C.text, align: "left" });
  const proofs = [
    ["① 框架能造出真成品", "有前后端 + 多机部署 + 运维手册 + 注册审批流程", C.green],
    ["② 「文件即数据库」被真实消费", "读的正是 runtime/ 里的任务、心跳、事件流、VERIFY/REVIEW", C.blue],
    ["③ 跨项目协作不是画饼", "同时监控 10 个 AIOS 项目（含远程 Windows/Linux）", C.purple],
    ["④ 零依赖哲学可行", "Python 3.12 标准库 + 手写前端，无 node_modules/pip 依赖", C.amber],
  ];
  x = 0.6;
  let y = 3.5;
  for (let i = 0; i < proofs.length; i++) {
    const [t, d, color] = proofs[i];
    s.addShape(pptx.ShapeType.roundRect, { x, y, w: 5.9, h: 1.15, rectRadius: 8, fill: { color: C.card }, line: { color: C.line, width: 1 } });
    s.addShape(pptx.ShapeType.rect, { x, y, w: 0.07, h: 1.15, fill: { color }, line: { type: "none" } });
    s.addText(t, { x: x + 0.22, y: y + 0.1, w: 5.5, h: 0.4, fontFace: FONT, fontSize: 13, bold: true, color: C.text, align: "left", valign: "middle" });
    s.addText(d, { x: x + 0.22, y: y + 0.5, w: 5.5, h: 0.55, fontFace: FONT, fontSize: 11, color: C.muted, align: "left", valign: "top", lineSpacing: 15, fit: "shrink" });
    if (i % 2 === 0) x += 6.25; else { x = 0.6; y += 1.28; }
  }
  s.addShape(pptx.ShapeType.roundRect, { x: 0.6, y: 6.42, w: 12.15, h: 0.62, rectRadius: 6, fill: { color: "0E2038" }, line: { color: C.green, width: 1.2 } });
  s.addText("彩蛋：它自己也是用这套框架开发的 —— 66 个 TASK + MONITOR-SPEC 规格驱动。先别问框架能不能做好软件——它已经做好了一个。", {
    x: 0.85, y: 6.48, w: 11.7, h: 0.5, fontFace: FONT, fontSize: 12.5, bold: true, color: C.green, align: "left", valign: "middle",
  });
  return s;
}

// ---------- 第 18 页 · 演进路线 ----------
function p18() {
  const s = bg("演进路线：从单项目自动到跨项目流水线");
  s.addText("AI 负责跑，人负责定方向、批风险、验收果——全自动是手段，可靠是目的", {
    x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 14.5, bold: true, color: C.accent, align: "left",
  });
  table(s, 0.6, 1.8, 12.15,
    ["阶段", "内容", "现状"],
    [
      ["① 单项目自动闭环", "autoloop coder+reviewer，P0/返工超限转人工", "✅ 已实现（实验性）"],
      ["② 多项目监控", "agent 遥测 + aimonitor 仪表盘，心跳/告警", "✅ 已实现"],
      ["③ 多项目自动调度", "一个调度器读多项目 runtime/tasks/ 分配 coder/reviewer", "🟡 架构已就绪，缺编排层"],
      ["④ 点子→成品流水线", "跨项目 DAG + 阶段人工闸门 + 成品验收", "🔵 愿景，需逐步验证"],
    ],
    [3.0, 5.8, 3.35], { rowH: 0.62, fontSize: 12 });
  card(s, 0.6, 4.4, 12.15, 2.4, "已内建的人机分工点（讲信任/安全时展开）", [
    { t: "• P0 风险操作 → 必须人工批准（approval-ref，否则 blocked）", c: C.text },
    { t: "• 返工 ≥3 次 → 机械拒绝，人工介入", c: C.text },
    { t: "• 卡死/阻塞 → task block + aimonitor 告警（blocked-ratio / stale-task）", c: C.text },
    { t: "• 成品验收 → 当前 verify 只验 build/lint/test/check，业务验收仍需人", c: C.text },
  ], C.purple, C.text);
  return s;
}

// ---------- 第 19 页 · 生态标准 ----------
function p19() {
  const s = bg("与生态标准的关系");
  s.addText("不是又一个孤立框架，是与标准生态对接的工程层", { x: 0.6, y: 1.2, w: 12.1, h: 0.45, fontFace: FONT, fontSize: 15, bold: true, color: C.accent, align: "left" });
  const items = [
    ["AGENTS.md", "标准格式，25+ AI 工具原生接入，零配置（Claude Code 用 symlink）", C.accent],
    ["AIOS（agiresearch）", "设计理念同源（Governance/Context/Memory/Storage），作为文件级治理层独立实现", C.blue],
    ["MCP", "tools/ 能力层可包装为 MCP server", C.purple],
    ["A2A", "agents/ 角色可通过 A2A 协议跨框架协作（远期）", C.green],
    ["跨平台", "Windows（PowerShell + python）/ Linux / macOS", C.amber],
  ];
  let y = 1.8;
  for (const [name, desc, color] of items) {
    s.addShape(pptx.ShapeType.roundRect, { x: 0.6, y, w: 12.15, h: 0.92, rectRadius: 8, fill: { color: C.card }, line: { color: C.line, width: 1 } });
    s.addShape(pptx.ShapeType.rect, { x: 0.6, y, w: 0.07, h: 0.92, fill: { color }, line: { type: "none" } });
    s.addText(name, { x: 0.95, y: y + 0.1, w: 3.2, h: 0.72, fontFace: MONO, fontSize: 14, bold: true, color: color, align: "left", valign: "middle" });
    s.addText(desc, { x: 4.4, y: y + 0.1, w: 8.15, h: 0.72, fontFace: FONT, fontSize: 12.5, color: C.text, align: "left", valign: "middle", fit: "shrink" });
    y += 1.02;
  }
  return s;
}

// ---------- 第 20 页 · 总结 ----------
function p20() {
  const s = bg("总结与 Q&A");
  s.addText("治理 + 角色 + 闭环 + 机械强制 + 知识 + 评估 = 可靠 AI 工程", {
    x: 0.6, y: 1.25, w: 12.1, h: 0.6, fontFace: FONT, fontSize: 19, bold: true, color: C.accent, align: "left",
  });
  const ideas = [
    ["1", "AI 不是「收到命令→执行」的助手，而是「理解→建模→规划→执行→验证→学习」的执行者"],
    ["2", "按角色而非工具定义；文件即数据库；治理先于执行"],
    ["3", "自动化有护栏：低风险自动，高风险人审，超限升级人工"],
  ];
  let y = 2.05;
  for (const [no, t] of ideas) {
    s.addShape(pptx.ShapeType.roundRect, { x: 0.6, y, w: 12.15, h: 1.05, rectRadius: 8, fill: { color: C.card }, line: { color: C.line, width: 1 } });
    s.addShape(pptx.ShapeType.ellipse, { x: 0.82, y: y + 0.3, w: 0.45, h: 0.45, fill: { color: C.blue }, line: { type: "none" } });
    s.addText(no, { x: 0.82, y: y + 0.32, w: 0.45, h: 0.42, fontFace: FONT, fontSize: 15, bold: true, color: "0B1220", align: "center", valign: "middle" });
    s.addText(t, { x: 1.5, y: y + 0.1, w: 11.0, h: 0.85, fontFace: FONT, fontSize: 13.5, color: C.text, align: "left", valign: "middle" });
    y += 1.18;
  }
  s.addShape(pptx.ShapeType.roundRect, { x: 0.6, y: 5.65, w: 12.15, h: 1.15, rectRadius: 8, fill: { color: "0E2038" }, line: { color: C.green, width: 1.2 } });
  s.addText("Q&A — 谢谢！", { x: 0.6, y: 5.85, w: 12.15, h: 0.6, fontFace: FONT, fontSize: 22, bold: true, color: C.green, align: "center", valign: "middle" });
  return s;
}

// ---------- 组装 ----------
p01(); p02(); p03(); p04(); p05(); p06(); p07(); p08(); p09(); p10();
p11(); p12(); p13(); p14(); p15(); p16(); p17(); p18(); p19(); p20();

pptx.writeFile({ fileName: OUT }).then(() => {
  console.log(`✓ 已生成 ${OUT}（${pageNo} 页）`);
}).catch((e) => {
  console.error("✗ 生成失败:", e);
  process.exit(1);
});
