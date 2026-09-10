# Phase A 固定实现契约

状态：任务包内的设计基线，尚未实现。局部实现可分卡交付，整个阶段验收前不部署中间版本。
重大改动先提交设计冲突，不让实现模型自行更换协议。所有绝对根路径由测试注入；数据只落在所属项目 runtime 或 AIMonitor data，不落入可替换 kit。

## C1 · 下行 HTTP 与单任务命令

保留 URL，收敛到后定义的新队列的数据模型；只保留一个 store、一组路由、一组 handler。
新 command_id 为不透明字符串，例如 dl-000001；客户端不能 int()、%d 或假定 seq 等于 ID。
seq 是队列顺序，不是任务事件序号。

| 操作 | 请求/返回与校验 |
|---|---|
| POST /api/downlink/commands | body 为 project_id、dedup_key、command:{name,args}、timeout_secs；成功 200，返回 command_id、seq、status=queued |
| GET /api/downlink/pickup | 成功 200，返回 command:null 或完整命令；事务内 queued→running，保存 picked_by |
| GET /api/downlink/commands/{id} | 成功 200，返回 command；必须校验项目读取授权 |
| POST /api/downlink/commands/{id}/result | status、exit_code、stdout_tail、stderr_tail、finished_at；仅领取主体可报告 running；成功 200 |
| 重复入队 | 相同项目、相同 key、相同指令且在途：409，携带既有 command_id；相同 key 不同内容：409 conflict，不执行、不伪装为复用 |
| 重复回报 | 同主体、相同终态结果：200 幂等确认；不同结果：409，不覆盖；queued 未领取：409 |
| 错误 | 无/错凭据 401；越项目或非领取主体 403；未知 ID 404；格式 400；超体积 413；限流 429；不兼容旧队列 503；必须返回 JSON 而非断连 |

鉴权沿用现有 agent token/project allowlist，服务端与 worker 都检查项目白名单。暂不造完整角色 RBAC；这是阶段 B/C 剩余能力。未配置授权集合不得默认所有项目。请求体必须是对象，exit_code/timeout 为真正整数而非 bool，timeout 有上限；保持当前体积/限流上限，不增大权限。

最终命令只有三种；wire args 必须恰好 [TASK-ID]：
- task_start → task start TASK-ID。
- autoloop_coder → autoloop coder --once --task TASK-ID。
- autoloop_reviewer → autoloop reviewer --once --task TASK-ID。

A02 先修 HTTP/store，暂保留字符串数组形状校验；A03 才在 server/adapter/worker 三端收紧单任务参数。
禁止把 wire args 当任意 CLI flags，更不能作为 shell 字符串执行。
--task 仅用于 --once；指定项不存在、不可执行或被占用时明确失败，不另选任务。
不指定 --task 的本地旧用法可保留，但必须经过 C2 的选择逻辑。

dedup 按 project + task + role（包括参数/超时一致性）生成；复用前比较内容。队列 done 仅表示这一条命令完成，不表示 TASK 验收通过。start 非零不得继续 coder；skipped 不得冒充新的成功产出。阶段 A 的 dispatcher 必须把异常、超时、结果未知与已完成区分，不自动重派未知结果。

旧数据库：先识别 schema。对已知新表采用显式兼容校验；若发现旧 downlink_commands 表含数据、混合 schema 或未知版本，禁用下行并返回 503 与迁移原因，不删表、清队列、猜测转换或阻断无关只读监控。迁移在后续批准任务中做。升级说明必须包含此限制。

## C2 · 候选、依赖、负责人和角色

统一一个可复用的 eligibility 判定；本地循环、指定任务执行与 dispatcher 使用相同测试向量。
- coder 只接 open/in-progress；reviewer 只接需要审查的 in-review。
- 自动认领只允许 assignee 为空/any/none 或等于当前 worker ID；具体其他负责人不覆盖。
- reviewer 指定了具体 ID 时只由该 ID 接单；reviewer 不得等于实现者。本阶段 ID 仍是配置标签，不声称是可信身份认证。
- depends-on 每项必须可解析、存在且 done；missing、blocked、cancelled、循环依赖均不可执行。普通拒绝不得擅自修改目标和依赖文件。
- 依赖语法支持现有空列表和 TASK 短 ID 列表。格式不明明确拒绝；跨项目引用暂返回 unsupported-cross-project-dependency，不当作已满足。
- 风险等级取 risk/priority 中更高者；仅缺省使用既有 P2，显式未知值拒绝。P0 无 approval-ref 不得自动执行；该文本引用并不是可信人工批准，仍需监督。
- 全部候选逐项判定，不让第一个不合格任务阻塞后面的合格项。可执行集合按优先级、数字任务号稳定排序；不引入岗位内容理解式自由调度。
- 状态读取、start 成功确认、负责人更新采用现有单项目锁边界，并避免重复取得非可重入锁。进入执行前再次检查；这不替代跨机租约。
- 手动修复/人工 start 的语义若与自动领取不同需显式区分，不能为了保护自动循环破坏合法人工恢复入口。
- 不按岗位默认要求引擎；工具不可用则阻塞该任务。岗位工件交接边界见 ROLE-INDEPENDENCE。

## C3 · 验证尝试与当前工件绑定

A05 新增 attempt 事实，A06 绑定指纹；禁止仅删除旧 VERIFY 或只比较今天日期。
建议小模块 verification_state.py，路径 runtime/verification/state/<TASK-ID>.json 与 attempts/<TASK-ID>/<UUID>.json。

attempt 最少：schema_version、verification_id、task_id、started_at、finished_at、status(running/pass/fail)、steps 与 return codes、input_fingerprint、task_fingerprint、command_fingerprint、失败原因。
开始前原子更新 current 指针为新 running；后续异常/非零变 fail，崩溃残留 running 不可作为 pass。
完成与批准仅接受 current 指向的最终 pass；按 attempt 身份而非日期/mtime 选择。
未知 schema、损坏状态、未绑定旧 Markdown VERIFY、写盘失败全部阻止新的 done/approve，给出重新验证指引。
保留历史记录，旧 done 任务不会因升级被自动重开；历史展示可以显示 legacy/unbound。
成功仍可导出 Markdown VERIFY，但导出不是可独立修改后获得信任的第二事实源。

指纹算法阶段 A 明确定义为 sha256 的确定性文件清单摘要：
1. 支持 Git 工作区；使用 Git 枚举 tracked（含删除）与 non-ignored untracked 文件，不能只看 source_dirs、HEAD 或 git diff --stat。
2. 覆盖代码、测试、配置、锁文件、文档、策划数据、资源元数据等所有枚举输入，纳入 HEAD、index 状态、相对路径、文件类型、可执行位与实际文件内容 hash。不能只 hash 代码扩展名。
3. 排除 .git 与 runtime 的运行输出；目标 TASK 的说明、验收正文和元数据另算 task_fingerprint，只排除 status 与 updated 这两个受控流转字段。解析不明则拒绝，不对全文做正则随意删字段。
4. 暂排除已知旧指标输出 kit/evaluation/metrics/*.jsonl，单列技术债；不排除整个 kit/evaluation。生成目录仅在配置明确且没有任何 tracked 输入时可排除；包含 tracked 文件时拒绝冲突配置。
5. commands 及配置文件进入指纹；改变验收命令、generated_dirs 或过滤规则必须失效。验证前后重新取指纹，不一致则失败，不能验收自动“修好”的另一个版本。
6. 验证后到 done/approve 再取指纹；改变任一输入、TASK 验收口径或 current attempt 则拒绝。纯心跳/日志/VERIFY 导出不导致自身失效。
7. 文件读取、权限、超大小策略错误 fail-closed；流式 hash，不能截断后当作完整。符号链接/子模块阶段 A 若不能安全证明完整输入，则明确不支持并拒绝验收，不越根读取。
8. 无 Git 的目录可运行验证命令并出诊断，但不生成可用于自动关闭任务的可信 pass；提示纳入 Git 或等待后续 manifest 工件模式。不偷偷 git init，也不伪造 commit。

本阶段不保证忽略文件中的外部依赖、未记录工具版本或恶意同用户篡改的安全性；完整环境与独立验证权限属于 B/C。
大体积二进制资产后续需独立 manifest/工件服务；设计上不能强迫美术把所有源文件塞进代码 Git。Phase A 的受限试点可使用 Git 中的资源元数据与固定导出样例，并保留人工签收。

## C4 · 遥测完整性与远端选择

保留 files.tasks 等展示字段的兼容形状。新增 task_snapshot：
version=1、total、returned、complete、errors、items。
items 是未截断的结构化候选元数据，覆盖全状态（包括用于依赖判断的 done）。
每项最少含 id/name/status/priority/risk/assignee/reviewer/depends_on/approval_ref/rework_count/updated。
字段含义使用 C2，不得用空 approval_ref、rework_count=0 或 assignee=any 填补缺失事实。

complete=true 仅当所有任务解析成功、枚举无误、元数据全部传完且 returned=total=items 长度。
容量仍受现有 256 KiB 限制；优先保留该元数据，先减少可选会话内容。仍装不下时 complete=false，提供总数、原因和部分项，不能提升上限或把部分任务假称完整。
files.tasks 展示可活动任务优先、限 50；展示截断与完整元数据是两件事。
角色事件只发送末尾完整行并说明截断；不要把首 4096 字符当最近事件。

服务端保存/校验/向 /api/status 透传 task_snapshot，并记录服务端接收时间；总数不一致和类型错误不能接受为 complete。
dispatcher 只从 complete=true 且新鲜的快照选题，按服务端接收时间/离线标记检查，阈值使用现有 heartbeat stale 配置。
旧 payload/旧服务无此能力时可展示，但调度明确 skipped(incomplete-or-legacy-snapshot)。
完整空集可以“无任务”；不完整、离线、认证失败不能伪装成完整空集。
远端快照不是租约；worker 执行前仍重查本地 C2。

## C5 · 服务端/agent 事件身份

服务端事件不能消费 agent 的 seq。新增事件存储命名空间 source_kind + producer_id + seq，另有服务端 ingestion_order 用于跨源排序。
agent 的 producer_id 来自已认证主体，不接受 payload 自报；server 的 producer_id 固定为 server。
agent 确认 cursor 只针对该 agent 流，不受 server 事件影响。
同 ID 同内容重放幂等；同 ID 异内容明确冲突，不推进 cursor。
旧事件迁入/读取为 legacy 命名空间，不猜测来源，不删旧表；旧 cursor 保留，已丢事件不能声称恢复。
本阶段不处理 agent 身份重置后的 epoch 或双机同项目接管，该限制列入 B。
写状态与事件之间的跨表/跨库 outbox 一致性也不是本卡完成目标。

## C6 · 监控只读权限与客户端接入

默认 bind=127.0.0.1，新增显式 --host 才允许其他监听地址；不能通过“监听本地”代替认证。
新增独立只读凭据文件，由 --readers-config 指定：
{"version":1,"readers":[{"id":"local-viewer","token":"<运行时秘密>","projects":["project-a"]}]}。
token 不写入仓库；沿用已有安全文件加载原则（POSIX 600，Windows 按可用 ACL 检查或明确配置指引），无效文件拒绝加载。空项目列表表示无授权，不支持隐式 *。
使用 Authorization: Bearer；常量时间比较；这个 token 不能调用 ingest、downlink、注册批准或其他写入口，也不复用 admin/agent token。

保护 /api/status、/api/history、项目 events/sessions 等所有返回项目内容的读接口。
聚合响应只含授权项目，其 alerts/counts/历史/名称等衍生字段也必须重新按范围计算；直接请求未授权项目 403。
缺失/错误凭据 401；配置缺失或错误时接口保持拒绝，不回退匿名；日志不输出请求头、token 或完整凭据文件。

浏览器保留静态页面入口，新增最小 token 输入/退出；token 仅存 JS 内存，通过统一只读 fetch 包装加头，刷新后重新输入。
禁止 localStorage、URL query、HTML 内嵌或日志保存 token。已有管理操作的授权逻辑单独保留，不让 read token 获得写权限。
dispatcher 的读请求从 AIOS_MONITOR_READ_TOKEN 环境变量取凭据，显式透传到 probe；不把 AIOS_DOWNLINK_TOKEN 当作读凭据兜底。
配置说明给出 TLS/反向代理要求；任务只提供示例与本地测试，不更改真实服务、防火墙或密钥。

## C7 · 普通模型文本不是服务错误控制信号

自由文本含 HTTP 401/403、欠费、gateway_error 等不得导致强制 kill。
优先解析 provider 已证实的结构化控制事件与退出码；引用/模型正文/工具输出不算控制事件。
没有可靠错误格式的 provider，使用明确非零退出或 timeout，不继续扩大子串正则。可以保留告警，但不能据此终止正常运行。
适配器单测使用 fake envelope，真实 provider 格式没有本地依据时注明未验证，不能声称全 provider 均实现快速故障识别。
本阶段不新增全局熔断、成本预算或 watchdog 恢复状态机；未知外部故障在监督模式下停止并交接。

## C8 · 任务编号

短 ID 为 TASK- 后至少三位十进制数字；新建编号至少补齐三位，超过 999 不截断。
匹配需要完整边界（结束、合法 slug 分隔等），不能让 TASK-1000 匹配 TASK-100。
比较顺序用数字值；旧文件名/编号不批量重命名，不把别名猜成新任务。
本阶段只统一 ID 解析/传播，不顺手重写整个 YAML 解析器。

## C9 · 非引擎岗位

遵循 ROLE-INDEPENDENCE。项目验证命令可用于服务、文档/数据、资源元数据等工件；不强制启动引擎或生成程序代码。
本阶段只验证现有项目级适配能力，不实现岗位自动路由和跨项目工件 DAG。
done 代表本任务约定验收完成，不代表全游戏集成；需要人工签收的产物不能靠四个 true 命令宣称通过。
