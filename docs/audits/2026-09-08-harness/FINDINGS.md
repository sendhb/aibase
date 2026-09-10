# AIOS / AIMonitor：审计发现与复现说明

对应[总结报告](../../AIOS-HARNESS-AUDIT-2026-09-08.md)。基线：AIOS `1adfb26`，AIMonitor `b93d5b2`；日期：2026-09-08。以下行号对应此版本，升级后可能变化。

本附件区分三种证据：**动态复现**是调用真实实现观察到的结果；**静态确认**是代码路径直接成立；**架构判断**是面向用户目标的工程评价，不等于已经发生生产事故。严重度含义见总结报告。安全问题的实际暴露程度取决于部署权限和网络边界，本次未访问生产服务。

## 复现与测试说明

从 aibase 根目录运行：

```bash
python3 -B docs/audits/2026-09-08-harness/reproduce.py
python3 -B docs/audits/2026-09-08-harness/run_monitor_tests.py
```

需要 Python 3.12、Bash、Node，以及同级 `../aimonitor` 源码。本次在 Linux 上执行；进程树探针是 POSIX 场景，不代表已完成 Windows 行为验收。脚本使用临时目录、合成任务、假模型函数、假凭据和临时本机 HTTP 服务；不会启动真实模型，也不修改 AIMonitor 源码或现有数据库。脚本需要允许创建子进程和绑定回环端口。

两个入口都是**观察型审计工具**，不是新增的产品回归门禁：它们正常退出不代表缺陷消失。第一个应检查各 JSON 字段，第二个应检查每条记录的 `exit` 和 `runner_error`。将来修复后，应把这些案例改写成有明确期望值和失败退出码的正式测试，而不是把当前异常结果设为正确答案。

本次实测输出保存于 [observations.json](observations.json) 和 [test-results.json](test-results.json)。其中时间、临时路径和耗时可能随重跑变化；文件未收录真实业务会话内容。

AIOS 原测试执行了 `python3 -B -m unittest discover -s kit/tests`，结果 775 项、774 通过、1 跳过。注意 [test_agent_smoke.py](/home/hb/code/aibase/kit/tests/test_agent_smoke.py:91) 将真实仓库设为遥测输入；[agent_loop.py](/home/hb/code/aibase/kit/tools/telemetry/agent_loop.py:312) 在测试 HTTP 服务确认后仍可能更新项目游标。原测试并非全部无状态；再次运行前应先改为合成项目，或在完整隔离副本中执行。

## F01 — 旧通过记录可使已经验证失败的代码进入 done

**阻断；动态复现 + 静态确认。**

位置：[tasklib.py：证据检索](/home/hb/code/aibase/kit/cli/lib/tasklib.py:244)、[完成前置检查](/home/hb/code/aibase/kit/cli/lib/tasklib.py:308)、[task：verify](/home/hb/code/aibase/kit/cli/task:336)。

`evidence_exists` 检查日期、任务引用、pass 和若干非空字段，没有检查记录中的 commit 是否对应当前代码，也没有检查工作区改动、验证命令版本和最新验证结果。`cmd_verify` 失败只写失败日志，不撤销旧 pass；成功文件按“日期+任务”命名，也没有独立 attempt 标识。

探针先正常执行验证得到 pass，再修改实际执行的脚本使其退出 7，再次 verify 返回 1；随后正常调用 done 返回 0，最终状态为 `done`。没有手工伪造证据，也没有使用 `--force`。

影响：自动合并、发布、完成率与依赖解锁若信任 done，就会把实际失败的工作当成交付。修复不能只比较日期或 HEAD：同一 HEAD 下也可能有未提交改动。应绑定固定 commit/tree、输入及验收配置 hash，由控制器保存不可由实现者任意覆盖的验证结果，并使后续失败或工件变化使旧证据失效。

回归门槛：首次通过后修改代码、测试、配置或依赖锁文件，均不得复用旧验证；最新一次失败后不得通过旧 pass 关闭任务。

## F02 — AIMonitor 下行协议被重复定义破坏

**阻断；独立真实 HTTP 复现 + 原测试失败。**

位置：同一 [monitor_server.py](/home/hb/code/aimonitor/server/monitor_server.py:2100) 在 2100、2921 行各定义一个 `DownlinkStore`；`ApiHandler` 的 enqueue、pickup、status、result 也各定义两次。Python 实际使用后出现的定义，不会因为前一套有测试或注释而保留其行为。

| 操作 | 代码不一致 | 本次真实请求结果 |
|---|---|---|
| POST 入队 | 3501 行旧形状调用与 2921 行新 store 参数不兼容 | `TypeError: 'int' object is not iterable`，客户端断连 |
| GET 领取 | 3199 行分发传 `agent_id`，3541 行生效方法不接收该参数 | `TypeError`，客户端断连 |
| POST 回报 | 正则接受 `dl-000001`，3288 附近仍执行整数转换 | `ValueError`，客户端断连 |

另有 result 方法名/签名不兼容，不能只修第一处异常。独立探针三条路径均得到 `RemoteDisconnected`；临时副本中的 `test/downlink_test.py` 同样在首个入队请求失败。

建议只保留一套版本化命令 schema 与 store/API 实现，统一 command_id 类型、认证主体、允许项目、状态转换和返回形状，再做真实 HTTP 全链路测试。拆分 4500 余行服务模块有助于降低覆盖和合并错误，但根本门禁应是契约联调，不是单纯限制文件长度。

## F03 — 有界遥测截掉真正需要执行的任务和最新事件

**高；动态复现 + 静态确认。**

位置：[agent_payload.py](/home/hb/code/aibase/kit/tools/telemetry/agent_payload.py:32)、[agent_runtime.py](/home/hb/code/aibase/kit/tools/telemetry/agent_runtime.py:53)、[dispatcher probe](/home/hb/code/aibase/kit/tools/dispatcher/probe.py:98)。

任务按文件名顺序读取后取前 50 条；长内容从开头截到 4096 字符。探针构造前 50 个任务 done、后 11 个 open，发送端最终传 50 个任务、其中 open 为 0。角色事件文件超过上限时，末尾的最新标记没有发送。

这不是仅影响 UI 的小问题：远端 dispatcher 从 AIMonitor 快照重建可执行任务。任务清单不完整会改变调度决策。独立的 task-event 游标机制确实存在，但不能修复已经缺失的任务快照，也不能等同于角色事件增量已正确实现。

建议控制面使用分页完整任务集或增量权威状态协议；监控展示可限定数量，但需公开截断标识、总数和游标。活动任务优先是短期缓解，不替代完整调度协议；日志使用明确的末尾窗口或偏移增量。

## F04 — 两个事件生产者共用序号空间，导致静默丢事件

**高；动态复现 + 静态确认。**

位置：[store_task_events](/home/hb/code/aimonitor/server/monitor_server.py:1183)、[append_server_event](/home/hb/code/aimonitor/server/monitor_server.py:1228)。

agent 自行产生递增 seq，服务端追加事件又使用当前 `MAX(seq)+1`。两者都写主键 `(project_id, seq)`；agent 写入采用 `INSERT OR IGNORE`，冲突时仍可推进接收游标。

探针先写 agent 事件，再写一个服务端事件占据下一号，随后到来的 agent 同号事件被忽略。数据库事务只解决同一存储操作的原子性，不能使两个独立序号生成器共享命名空间。

建议事件 ID 使用 `(project_id, producer_id, producer_epoch, seq)` 或全局唯一 event_id；服务端另设接收顺序。重复 ID 且内容不同应报错，不应当作成功重放。测试应覆盖双生产者、重启换代、乱序和重放。

## F05 — 调度领取不是事务，缺租约和过期执行隔离

**高；确定性交错复现 + 静态确认。**

位置：[SchedulerState.load/save](/home/hb/code/aibase/kit/tools/dispatcher/state.py:164)、[allocate](/home/hb/code/aibase/kit/tools/dispatcher/state.py:269)、[超时处理](/home/hb/code/aibase/kit/tools/dispatcher/state.py:378)、[cmd_run](/home/hb/code/aibase/kit/tools/dispatcher/dispatcher.py:395)。

状态从 JSON 读入各进程内存，分配后通过临时文件替换保存；没有覆盖“读→判空闲→分配→保存”的共享事务。探针用两个独立加载实例模拟确定性交错，两次 allocate 都接受，最后文件只保留 worker-2。此探针证明竞态，不是实机压力测试。

现有角色文件锁能减少同一目录同一角色重复执行，但不能保证中央记录、多个 worker 或不同路径副本只有一个合法所有者。timeout 仅改变分配记录，缺失带身份的续租和 fencing；旧执行仍可能返回并修改工作区。状态文件损坏后的空状态回退也应作为恢复事件处理，而不是正常可分配状态。

此外，[候选选择](/home/hb/code/aibase/kit/tools/dispatcher/policy.py:136) 每轮从注册表开头扫描；探针三轮均选择 a，b 没有获得机会。`cmd_run` 顺序执行候选，`max-workers` 不是实际并发池。

建议事务式 claim、状态版本、run_id、租约与 fencing token；状态变化与 outbox 同事务提交。重分配前明确取消旧 run，并拒收旧 token 的结果。公平调度先采用简单轮转和项目配额即可。

## F06 — 选择、领取与执行没有绑定同一个任务

**高；动态复现 + 静态确认。**

位置：[pick_task](/home/hb/code/aibase/kit/cli/lib/tasklib.py:112)、[coder.run_once](/home/hb/code/aibase/kit/cli/lib/autoloop_coder.py:570)、[candidate_commands](/home/hb/code/aibase/kit/tools/dispatcher/agent_adapter.py:185)、[_run_candidate](/home/hb/code/aibase/kit/tools/dispatcher/dispatcher.py:269)。

本地选第一个 open/in-progress，不检查依赖是否完成，也不尊重已有负责人。探针中 TASK-001 依赖 blocked 的 TASK-002，另有可执行高优先级 TASK-003，仍选 TASK-001，并把 human-alice 改成 autoloop-coder。

给 start 注入失败返回码 1 后，本地仍调用 LLM，且本轮返回 0、任务保持 open。中央 `_run_candidate` 同样在 task_start 失败后调用 autoloop_coder。其命令序列是 `task_start [指定任务]`、`autoloop_coder []`；后者重新自行选任务，中央记录与真实执行缺少硬绑定。

建议 worker 接收不可省略的 task_id/run_id 和已验证的工作区；领取前检查依赖、负责人和风险，start 失败立即停止。不要通过“先修改一个文件，再启动自由选题循环”传递中央任务。加入两任务竞争、人工占用和依赖未完成场景。

## F07 — 子进程生命周期不完整，角色可能互相误杀

**高；动态复现 + 静态确认。**

位置：[llm._kill](/home/hb/code/aibase/kit/cli/lib/llm.py:111)、[coder._run_argv](/home/hb/code/aibase/kit/cli/lib/autoloop_coder.py:163)、[LLM PID 记录](/home/hb/code/aibase/kit/cli/lib/autoloop_launcher.py:498)、[record_llm_pid](/home/hb/code/aibase/kit/cli/lib/autoloop_launcher.py:512)。

超时调用直接子进程的 kill，没有统一管理其派生 shell、构建和工具进程。POSIX 探针中父进程返回 timeout 124，派生进程仍写出标记。项目内 coder/reviewer 共用 `autoloop-llm.pid`；记录另一个存活 PID 时，会尝试杀掉之前 PID。探针拦截 kill 调用，确认第二个角色注册时尝试杀第一个角色；未实际误杀用户进程。

影响：编辑器、构建和网络工具可在“任务已停止”后继续占资源或修改文件；独立启动的角色互相干扰。`both` 当前串行会降低后一个问题出现概率，但不能修复共享 PID 的设计。

建议每个 run 独立进程组；POSIX 使用 session/group 管理，Windows 使用 Job Object 等对应机制。记录 PID 之外还需启动时间和 run 身份，避免 PID 复用；停止覆盖整棵受管进程树，并验证确已退出。

## F08 — “独立审查/批准”主要是可修改文本，不是可信边界

**高；指定 reviewer 绕过有动态复现，其余为静态确认与信任模型判断。**

位置：[review_exists](/home/hb/code/aibase/kit/cli/lib/tasklib.py:283)、[P0 检查](/home/hb/code/aibase/kit/cli/lib/tasklib.py:197)、[reviewer 选题](/home/hb/code/aibase/kit/cli/lib/autoloop_reviewer.py:156)、[review context](/home/hb/code/aibase/kit/cli/lib/review_context.py:133)。

REVIEW 只检查 reviewer/implementation-author 字符串不同等条件；未验证真实会话身份，也未强制等于任务指定 reviewer。探针给任务指定 bob，eve 仍能选中。批准引用检查非空，不确认批准工件来源、内容和适用版本。本地 P0 判断与中央 policy 还不一致：priority=P0、risk=P2 时本地不拦、中央 p0-blocked。

实现者通常可修改任务、验证记录、审查记录及验收命令。分两个会话确有助于减少自我确认偏差，但不是权限隔离。审查上下文主要查看当前 HEAD 的工作区 diff，没有固定任务 base/head；已提交实现可能不在该 diff 中。

建议明确可信控制器、实现者、验证器、审查者和批准者的权限；证据由对应主体签发并绑定同一工件。统一风险计算。低风险快路径可以保留，但必须如实称为自动检查，不承诺独立评审。无需为了字符串不同而强制两种模型品牌。

## F09 — 原始会话读接口没有访问控制

**高；未认证读取动态复现；实际公网暴露未验证。**

位置：[GET 路由](/home/hb/code/aimonitor/server/monitor_server.py:3199)、[会话读取](/home/hb/code/aimonitor/server/monitor_server.py:4369)、[监听地址](/home/hb/code/aimonitor/server/monitor_server.py:4501)。

服务默认绑定 `0.0.0.0`；GET status、项目 events/sessions 没有与 ingest 类似的读授权。临时服务中，不提供 token，status 返回 200，指定合成会话返回 200 和 `fixture-private-code`。这是可复现的读边界缺失，不表示已发现真实密钥泄露。

ingest token、HMAC、注册密码和部分敏感日志检查是有效基础，但保护写入口不能自动保护读入口。模型原始会话可能包含源码、工具结果、个人信息或偶然输出的凭据；简单敏感词替换不足以证明安全。

建议默认 loopback 或要求显式监听配置，所有会话/项目数据读取使用用户认证与项目授权；部署 HTTPS、访问日志、留存期限和删除机制。默认最小化收集，原始 transcript 明确 opt-in。若由反向代理统一鉴权，需验证无可绕过的后端入口，并写入可测试的部署约束。

## F10 — 执行结果回报丢失后无法可靠恢复

**高；丢回报动态复现，队列回收为静态确认。**

位置：[DownlinkWorker._run](/home/hb/code/aibase/kit/tools/telemetry/agent_downlink.py:288)、[服务端 pickup/sweep](/home/hb/code/aimonitor/server/monitor_server.py:3047)。

worker 执行完成后报告一次；报告异常只记日志并释放 busy，没有持久 pending-result。探针模拟回报异常，后续三次 tick 都是 empty，没有再报告，未产生结果 spool。服务端对 queued 命令的回收主要依赖 pickup；目标持续离线时缺少独立主动清理路径。

结果不明不等于执行失败：如果已改代码或生成素材，再派一次可能产生重复副作用。协议还需约束谁领取、谁能完成、何时接受结果，不能仅依赖客户端自报 success。

建议本地 WAL/outbox 先落盘再回报，重启和断网后按 command_id 重试；服务端幂等接受同一执行结果并校验租约与领取主体。独立维护队列超时和人工可见的 outcome-unknown 状态；对不可安全重试操作要求显式策略。

## F11 — 普通输出内容被当作模型服务致命故障

**高；动态复现。**

位置：[FATAL_PATTERNS/scan_fatal_output](/home/hb/code/aibase/kit/cli/lib/llm.py:38)、[coder 输出扫描](/home/hb/code/aibase/kit/cli/lib/autoloop_coder.py:163)。

当前对自由文本做大小写不敏感的子串匹配，包含 `http 401`、`http 403`、`欠费` 等。探针中的正常子进程输出 `Added test for HTTP 401 handling`，实际被终止为 86，而非正常退出 0。

这些字符串是登录、权限、网关和支付功能开发的正常内容；不只恶意提示才会触发。应优先使用 provider 的结构化错误/进程状态；纯文本兼容层也要区分控制错误流与模型正文、代码、日志引用。错误信号、重试与熔断状态应统一建模。

回归至少覆盖：描述 401 测试不失败；真实鉴权错误快速终止；故障解除前不被 watchdog 立即无限重新启动。

## F12 — 循环没有统一的进展、重试和预算约束

**高；静态确认 + F06 的无进展成功观察。**

位置：[coder 主循环](/home/hb/code/aibase/kit/cli/lib/autoloop_coder.py:672)、[run_once 返回值](/home/hb/code/aibase/kit/cli/lib/autoloop_coder.py:570)、[返工限制](/home/hb/code/aibase/kit/cli/lib/tasklib.py:394)、[usage 入口](/home/hb/code/aibase/kit/cli/lib/llm.py:162)、[dispatcher 完成判定](/home/hb/code/aibase/kit/tools/dispatcher/dispatcher.py:395)。

本地循环按固定间隔无限启动，LLM 返回 0 并不要求任务进入下一状态或产生有效产物。已有 `rework-count` 限制只覆盖 in-review→in-progress 的返工，不覆盖一直停留 in-progress 的反复实现、调用失败或无进展。中央分配也主要按命令退出码判本轮完成。

框架已有 usage 辅助函数，但实际 coder/reviewer 包装没有统一走该入口；输出字节估算也无法准确覆盖输入、缓存和工具调用费用。没有可见的项目/任务成本上限与可验证的预算停止链路。

建议分离模型运行成功、工件生成成功和任务验收成功；记录 attempt、进展指纹、实际 usage、最长时间及连续无进展次数。达到上限进入 blocked/needs-human，不自动重置预算。故障重试应指数退避并区分永久故障。

## F13 — 上下文装配和 checkpoint 只能提供弱恢复

**中；预算超限动态复现，其余为静态确认。**

位置：[context_loader.render/collect](/home/hb/code/aibase/kit/cli/lib/context_loader.py:83)、[allocate](/home/hb/code/aibase/kit/cli/lib/context_loader.py:218)、[write_checkpoint](/home/hb/code/aibase/kit/cli/lib/autoloop_coder.py:500)。

装配结果是带建议读取行数的路径表，不是实际已经加载并受限的模型上下文。检索基于任务引用、路径、少量匹配和 git diff，没有实现可验证的语义检索、相关性评估或工具调用预算。高优先级任务/checkpoint 条目允许超预算；探针配置 600 行，分配 668 行。行数也不能可靠映射 token。

checkpoint 提取 Markdown 勾选项、文本进展和日志尾部，且在会话返回后写入。它是有用的人类交接摘要，不是可恢复的 run 状态：进程崩溃时不能确保保留最后一个工具动作、未提交工件及外部副作用。

建议保留交接摘要，另持久化结构化运行事件和工件；在 provider 支持范围内控制实际 token 和读取范围。先用真实任务验证检索是否减少遗漏，再决定是否需要向量库，不能把增加数据库当作上下文质量本身。

## F14 — 测试数量可观，但发布门禁没有覆盖主链路

**高；静态确认 + 隔离执行。**

位置：[AIOS CI](/home/hb/code/aibase/.github/workflows/verify.yml:13)、[AIMonitor CI](/home/hb/code/aimonitor/.github/workflows/verify.yml:13)、[AIMonitor 测试命令](/home/hb/code/aimonitor/aios.config.yaml:14)、[覆盖率范围](/home/hb/code/aimonitor/test/registration_test.py:66)、[真实任务依赖](/home/hb/code/aimonitor/test/server_smoke.py:3936)。

AIOS 原套件 775 项未发现本附件的跨模块问题。AIMonitor 默认 test 命令未包含已有 downlink_test，尽管它能在首个真实 HTTP 操作发现 F02。AIMonitor Git 不跟踪 `kit/`，但 CI checkout 后直接调用 `kit/cli/check` 和 `kit/tests`，没有安装步骤；按当前仓库内容，干净 CI 工作区无法满足这些路径。

临时副本执行 server_smoke 时，最终硬性要求工作区含真实 TASK-023 而失败；这是测试隔离和可重复性问题，不能据此声称生产服务业务失败。registration 的功能检查通过，覆盖门禁却以硬编码旧行号计得 58.60%；行号漂移使它不再可靠代表命名的注册子系统。

建议 clean-checkout CI 显式安装固定版本 kit，并执行 AIMonitor 自己的测试；构建前做重复类/方法等静态检查；测试使用合成 fixture，不读写真实 runtime；覆盖按模块/函数计算。增加真正的多组件 contract、断网重放、进程回收和任务验收端到端门禁。测试通过率和产品有效完成率应分开统计。

## F15 — 项目根定位与升级边界仍有旧布局残留

**中；静态确认，未执行真实发布/覆盖操作。**

位置：[publish](/home/hb/code/aibase/kit/cli/publish:27)、[sync](/home/hb/code/aibase/kit/cli/sync:24)、[evaluation_dir](/home/hb/code/aibase/kit/cli/lib/tasklib.py:430)、[re-init](/home/hb/code/aibase/kit/cli/re-init:125)。

publish/sync 用脚本路径的上两层作为 ROOT；当前 `项目/kit/cli/脚本` 布局下结果是 `项目/kit`，不是项目根。因此默认发布的是 kit/docs、kit/knowledge，默认同步目录也落入 kit。任务评估指标同样写在 kit/evaluation，与框架整体替换/下发边界冲突。

publish 同名版本允许覆盖且删除重拷目标目录，manifest 只有文件清单而无内容 hash；版本名不能保证内容不可变。re-init 有备份值得保留，但未形成运行中 worker 的版本固定、协议兼容与状态迁移事务。

建议所有 CLI 统一项目根定位；项目运行数据移入 runtime 或专用数据目录。发布包不可变且包含摘要与 schema 版本；升级先验证、暂停领取、迁移、切换并可回退。读取外来 manifest 的路径需验证根边界；本次未进行恶意包利用测试。

## F16 — 多套简化解析和三位任务 ID 限制不适合长期项目

**中；TASK ID 动态复现，解析限制静态确认。**

位置：[TASK_RE](/home/hb/code/aibase/kit/cli/lib/tasklib.py:29)、[frontmatter parser](/home/hb/code/aibase/kit/cli/lib/tasklib.py:58)、[short_id](/home/hb/code/aibase/kit/cli/lib/tasklib.py:127)、[task new](/home/hb/code/aibase/kit/cli/task:186)、[dispatcher metadata parser](/home/hb/code/aibase/kit/tools/dispatcher/policy.py:68)。

创建编号可以超过 999，但枚举正则只识别三位；探针中的 TASK-1000 不被 task_files 看见，short_id 又截成 TASK-100。长期游戏项目或自动拆任务很容易达到该数量。

frontmatter/config/monitor/dispatcher 存在不同的手写解析逻辑，不等价于完整 YAML，也没有统一的字段类型、版本和未知值处理契约。格式兼容不一致会改变治理和选择结果；F08 的风险判定差异已给出实例。

建议使用明确 schema 的单一解析/校验模块，或者正式声明并强制校验一个受限格式；任务 ID 支持不限三位的稳定标识。保存前验证、错误明确呈现，不把损坏状态静默解释为默认低风险或空闲。

## F17 — 默认模型执行没有接上机械权限边界

**高；静态确认与信任模型判断，未尝试绕过真实权限。**

位置：[provider argv](/home/hb/code/aibase/kit/cli/lib/llm.py:64)、[coder 执行](/home/hb/code/aibase/kit/cli/lib/autoloop_coder.py:163)、[sandbox-run](/home/hb/code/aibase/kit/cli/sandbox-run)、[sandbox-review](/home/hb/code/aibase/kit/cli/sandbox-review:45)、[protect](/home/hb/code/aibase/kit/cli/protect)。

默认 autoloop 直接运行 agent CLI，不强制经过已有 sandbox 包装。Claude unattended 可跳过权限确认，其他工具按各自自动模式执行；仓库未将不同 provider 能力映射成统一可检查的权限集合。这并不证明所有外部 CLI 都没有沙箱，而是 AIOS 本身未保证该边界。

`protect` 的属主文件去写权限可以降低误编辑概率，但同一属主可恢复写权限。sandbox-review 的容器根只读不等于项目只读：workspace 仍可写，且容器有 bridge 网络和必要凭据挂载。不能把提示词“不要改代码”当成能力限制。

建议最小权限 worker、逐项目凭据、可配置网络出站、工具白名单与受保护验收环境；reviewer 读取固定工件，仅允许写审查结果。高风险发布/签名/生产操作由独立批准执行器完成。先明确威胁模型，避免对所有工作强行使用不兼容游戏编辑器的通用容器。

## F18 — 会话内容去重会误删合法重复输出

**中；动态复现。**

位置：[会话增量存储](/home/hb/code/aimonitor/server/monitor_server.py:1318)。

实现通过已有尾部与新内容的相同文本寻找重叠。探针分两批发送相同内容但递增 offset 的两条新行，数据库最后只保存一条。相同错误、轮询消息或重复工具输出本来可能确实发生两次，内容相同不等于重放。

建议按文件身份、轮转代次、明确起止 offset/line sequence 去重；同一区间重发才是重放。测试覆盖相邻相同行、重传整批、日志截断/轮转和相同文件名重新创建。

## F19 — 游戏岗位与引擎支持主要是扩展意图，尚非生产能力

**中；静态确认 + 面向目标的架构判断。**

位置：[Unity profile](/home/hb/code/aibase/kit/profiles/unity/config.template.yaml)、[Unreal profile](/home/hb/code/aibase/kit/profiles/unreal/config.template.yaml)、[Unity 工具目录](/home/hb/code/aibase/kit/tools/unity/README.md)、[Unreal 工具目录](/home/hb/code/aibase/kit/tools/unreal/README.md)、[launcher](/home/hb/code/aibase/kit/cli/lib/autoloop_launcher.py:275)。

引擎 profile 仍以通用源目录和占位命令为主，工具 README 明示未来适配。实际运行角色主要 coder/reviewer；其他岗位说明没有对应的结构化输入输出、执行器、工件类型、工具权限和质量判据。命名为 Manager、Architect 或认知/知识模块，不能作为运行能力已经实现的证据。

这不否定它可用于游戏仓库的文本/代码辅助，也不要求框架自带所有美术工具。但若目标是跨岗位替代/协作，必须补齐引擎版本与 license、Editor 自动化、资源依赖、LFS/锁、可运行场景、确定性测试、性能证据、试玩签收及构建发行边界。

建议先围绕团队主用引擎实现一个垂直切片：需求→任务依赖→隔离实现/资源导入→引擎测试→可玩构建→人工试玩→受控合并。以验收质量和净人工投入衡量收益，再扩展岗位和并发，而不是先增加角色数量。

## 证据解读与后续使用

1. F01/F02 已足以否定“当前版本可直接用于可信无人值守交付”的判断，不依赖对未来规模的猜测。
2. 不把暂未实现的能力描述成已运行故障；不把使用某个模型开发当成缺陷原因；不把同一个根因拆成多个故障来计算事故率。
3. 775 项原测试与这些发现并不矛盾：多数测试覆盖单模块期望行为，本次补的是跨模块契约、故障状态和信任边界。
4. 本次未修复任何发现；修复优先级和目标架构见总结报告。安全修复需根据真实部署进一步核实访问路径，游戏能力需在真实引擎与目标设备上补验。
