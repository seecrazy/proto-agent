# PROTO 项目进度

> 当前完整方案统一阅读 [DESIGN.md](DESIGN.md)。本文件只记录里程碑、实现状态与验证证据，不作为第二份设计。

## M82 — 编码循环可见性与 iOS 交付可达性（2026-11-24，独立核查后整改）

- **背景**：主人问「iPhone 专家自主交付成熟 App 的目标实现了吗」。新会话只读核查（14:30）的结论是没有：M80「监督者可离场」与 M81「三缺口闭合」部署后两小时内再次被证伪——专家 10:13→12:06 约 140 万 token、读 200 次命令 102 次、只写 10 次，13 条 run 全败，随后零进展阻塞、自动恢复配额耗尽、空等两个半小时；活动工程 3 个源文件 239 行，副本上 `swift build` 13 个编译错误；工作区没有任何 `.xcodeproj`。主人指令：打磨到目标达成。设计与证据见 [设计记录](specs/2026-11-24-coding-loop-visibility.md)，任务拆解见 [实施计划](plans/2026-11-24-m82-coding-loop-visibility.md)。
- **根因（逐条有生产证据）**：① 压缩只留最近 2 条观察、读文件超过 900 字节即折叠——模型「跑构建能看到报错，读文件却看不到正文」；② 命令观察是字典字符串，stderr 以无关警告开头，裁剪后只剩构建横幅；③ `list_dir` 按工作区根算深度，列项目目录时子目录整个被剪掉；④ 同一 `swift build` 23 步里真实执行 7 次，判重键取横幅；⑤ 交付门要 `xcodebuild -project` 的模拟器 `.app`，系统里没有办法生成 `.xcodeproj`，验收里的机型本机也不存在；⑥ 沙盒挡住 xcodebuild（宏插件嵌套沙盒、testmanagerd 套接字、伪终端）。
- **修复**：
  1. **命令观察整形**（`proto/exec/observations.py`、`proto/tools/diagnostics.py`）：退出码 → 错误摘要（带源码摘录、工位相对路径）→ 输出尾部；`run_command` 从裁剪前的完整输出提取诊断。重定向、管道、通配符、超长写入、放错位置的 `note` 都在执行前说明怎么改。
  2. **压缩按类别保留**：压缩预算 4→16 KiB、紧急 1.2→3 KiB、输入默认 32768→65536；最近 4 个文件的正文可见，改过的文件保留并标出，旧读取折叠成指针。
  3. **重复验证不重跑**：只读步之间重放原结果；判重看诊断；连续 8 步未落盘时指向第一条错误。
  4. **工位事实**：`list_dir` 深度相对被列目录；「工位文件」块 1400 字符；固定区附带接口轮廓（`proto/exec/outline.py`，2400 字节）。
  5. **沙盒与 xcodebuild**：自动补 `OTHER_SWIFT_FLAGS=-Xfrontend -disable-sandbox`；放行 launchd 会话套接字目录、unix socket 出站、伪终端；`xcodebuild test` 默认超时 900 秒。工作区外写入与 TCP 仍被拒。
  6. **新技能 `ios-app-project`**：自带 `make_xcodeproj.py`，一份 JSON 清单生成 `.xcodeproj` 与共享 scheme；`inspect_build` 与交付阶段提示接线。
  7. **编辑反馈**：`edit_file` 的观察写明改了什么；逐字找不到时忽略首尾空白再找，并说明「已生效」或给出最接近的行。暂停通知区分「会自己恢复」与「需要你」。
  8. **测试套件不再出公网**：三个心跳用例随 GitHub 延迟变红（核查时看到的那 1 个失败就是它）。
- **真实模型试跑**（Wengu 副本、生产同款 glm-5.3、每片 24 步）：b1 第 6 次调用 `model_invalid_response`；b2 5 次写入、错误 13→3；b3 11 步错误 13→3（余下是没写的视图）。每一轮暴露的问题都已修并回归。
- **生产验证**（17:24 部署、解锁目标）：部署后连续 4 片都有实质进展（`stalled` 始终为 0，没有再阻塞）——`swift build` 从 13 个错误到 0；新写 `Views.swift`、`StatsView.swift`；第 4 片自己读了 `ios-app-project`、写出 `project.json`、生成 `Wengu.xcodeproj`，并在沙盒内经 `xcodebuild` 构建出 `Debug-iphonesimulator/Wengu.app`、写下 README。这是一周以来第一次拿到模拟器应用包。期间按生产时间线又修三处：诊断路径按真实绝对前缀裁剪、视野保留 4 个文件（2 个时模型循环重读 13 步）、超长写入给出拆分办法（曾原样重交 7 次）。
- **测试**：新增 `tests/test_m82_coding_loop_visibility.py` 与 `tests/test_ios_app_project.py`；部署前全量 **1407 passed + 5 skipped**（核查时为 1345 passed + 1 failed）。`PROTO_TEST_XCODE=1` 时生成的工程在 PROTO 沙盒内经真实 `run_command` 构建出 `.app`。
- **还没做到**：XCTest 尚未在模拟器上跑通；没有模拟器截图与功能走查；验收还没判过；旧的并行目录 `Wengu/` 还在工位里。遗留与成因未明的项见 [DEBT](DEBT.md) 的 D27–D29。

## M81 — 完全自主三缺口闭合（2026-11-24，主人问责后补交）

- **背景**：M80 收尾时主人问责"为什么没达到自主完成 App 的水平"。生产实证支撑问责：夜间 02:28 run 停在 needs_reconciliation/model_retry_exhausted **死等主人 5 小时零进展**（M80 声称的"可无人值守"被证伪）；交付阻塞同样只能主人 retry；D22 验收自指未机制化（入账≠兑现——直接堵死目标的问题不可推迟）。
- **① model_retry_exhausted 自动续航**（`RunCoordinator.auto_recover`）：失败在模型侧、无未知副作用，与主人点"继续原任务"同语义；冷却 10 分钟、每 run 上限 24 次，耗尽才真正留给主人；worker 空闲分支 30s 节流调用；首次自动续接入收件箱告知。
- **② 交付阻塞自动恢复**：`_available_work` 前扫描到期阻塞——首次 15 分钟/后续 30 分钟/上限 3 次；无 `blocked_at` 的旧状态保守不恢复；主人手动 retry 重置配额；恢复置 `auto_feedback`，下轮 work_on_goal 注入"禁止重复此前失败路径，第 1 步必须落盘不同做法的最小增量"；`auto_retries` 跨轮保留（曾发现每轮覆写丢计数→无限重试的真 bug，已修）。
- **③ D22 最小机制·Swift 编译回执强制**：`_has_swift_sources`（磁盘 rglob 封顶 64 + 轨迹写过 .swift）→ requirements 加 build——工程事实优先于任务措辞，写 .swift 不编译不再能过验收（静态脚本自证完成被封死）；确定性门先于语义门（M67/M68 先例）。D22 登记为部分偿还（iOS Simulator 证据链与通用确定性验收仍待 D11 专项）。
- **测试**：tests/test_m81_autonomy_closures.py 12 项（含真实 swiftc typecheck 正路径）；m71 连续性家族交付物 Product.swift→Report.md（它们测断点续作，新契约下写 .swift 不编译本就不该过）；全量 **1344 passed + 4 skipped**（m76 一项 D23 抖动单跑稳定）。
- **部署**：提交 ba7496b；生产重启后生效（run 266 是部署前旧代码挡的、状态无 blocked_at 被新代码保守跳过——主人 retry 一次后新结构生效）。

## M80 — 监督实战：iPhone 专家自主开发 App 首轮（2026-11-24，进行中）

- **背景**：主人指令——启动 proto-agent，监督 iPhone 专家开发完整 App，期间发现任何问题及时修正，最终目标是完全自主开发完整 App。本条目随监督进程持续更新。
- **现场盘点**：生产 8431 跑 M79 版（重启于 11-23 23:42）；专家 4 目标（概念规格/SwiftUI 实现/验证交付/总目标）；工作区两套概念漂移（docs/DESIGN.md=EchoNote 回声笔记 vs Swift 源码=Wengu SM-2 背卡）+ 两套并行实现（根目录 5 文件 vs Wengu/Sources 5 文件）；verify_app.py 静态检查 34/34 全绿而 `xcrun swiftc -typecheck` 实际编译失败（`\u{}` 转义嵌插值、SeedData 未定义）——D11 的活体案例。
- **修复①（提交 82f2d50）**：四件生产实证问题——JOURNAL 模板回显（LLM 原样回显 _SUMMARY_PROMPT 占位文本未被过滤）/note_model_block 每拍刷同文块（单目标 1800+ 条，已归档压缩生产数据 244KB→5KB 活跃+全量 JOURNAL.archive.md）/活动 detail 对 dict payload 显示 "{}"（M61 同类漏网）/**/api/activity/live 对未暂停的排队续跑 500**（blocked_reason 收到的快照投影无 result 列，_continuation KeyError；M79 测试只覆盖了已暂停早退分支——路由改喂完整记录+_continuation 容错）。
- **修复②（同提交）**：M45 探索配额计数键从"只读工具"改为"本步未落盘"——run_command 跑诊断脚本曾清零计数，"跑校验→读文件"交替 24 步零写入烧完整个工作片（run 250 时间线铁证）；新增"同一验证 3 次同失败强制换法"提示。m45 两断言随文案语义更新。
- **修复③（提交 3ce145c）**：GET /api/runs 接通 limit（D24 登记即修）；DEBT 入账 D22（验收自指）/D23（m76 计时抖动）。
- **主人介入**：retry 被 delivery_not_verified 挡住的概念目标；模型组"模型组1"三成员原全是 glm-5.3-flash（执行决策长期跑弱模型——循环纪律差的根因之一），重排为 glm-5.3×3 优先、flash×3 兜底，后因 paas/v4 两接入持续 429 把 env-main（anthropic 端点，实测健康）提到组首；优先级文本写入五条工程纪律（概念唯一/三者一致/真实编译回执/重复失败禁原样重跑/每轮必落盘）——**教训：主人聊天指引不进工作循环提示词（工作上下文只含 owner_priority），指令要走优先级文本或项目 AGENTS.md**。
- **修复④（提交 c384f38）· 嵌套沙盒根治**：专家正确地对 Wengu 源码跑真实 `swift build`/`swiftc -typecheck`，但 SwiftPM 与 swift-frontend 的宏插件系统会给自己再套一层 sandbox-exec，macOS 禁止嵌套沙盒 → `sandbox_apply: Operation not permitted` → `@State`/`@Model` 等外部宏一律加载失败（生产 run c5a9f68f 铁证）。临时目录复现+双修：run_command 在 sandbox-exec 后端下自动补 `swift build/test → --disable-sandbox`（子命令后插）、`swiftc → -Xfrontend -disable-sandbox`（显式传旗标不加倍、swift run 等子命令与未隔离后端不动）；外层 proto 沙盒仍 enforced 写遏制+禁网。端到端实证：补丁后沙盒内真实构建含 @State 宏的 Swift 包 exit 0。
- **修复⑤（提交 f13f6c4）· D26 偿还**：交付门未验收片上限 4→12——App 级目标每轮都有真实文件增量（DESIGN_SPEC/SM2Engine/Models/WenguApp/Package.swift 逐轮落盘）仍 4 片被 blocked，监督者成自主链条瓶颈。三计数器方案（停滞/无增量/有增量分开计数）推演后否决——交替模式互相清零永不暂停；最终保持 M72 全量计数语义仅提上限，零进展灾难仍由 stalled≥2 第 2 片拦截，安全保证不降。原 4 片契约测试改写+零进展快速拦截钉死。
- **验证**：新增回归 18 项（tests/test_m80_journal_activity.py）+ m72 契约改写 2 项；最终全量 **1333 passed + 4 skipped**（m76 一项计时抖动两次全量分别挂不同参数、单跑/整模块稳定，D23 入账）；生产三轮重启部署（23:42/00:10/01:45 后）后 live 路由 200、活动 detail 人类可读、journal 轮次接续、swift build 穿透沙盒拿到真实编译错误均实证。
- **专家执行轨迹（截至 02:10）**：概念收敛成功（保留 Wengu，DESIGN_SPEC.md 110 行含数据模型表/SM-2 伪代码/六屏清单/无障碍要点）；SM2Engine.swift 重写为纯数据结构+静态调度（修掉字面 \n 语法损坏与 isDue 恒真缺陷）后 **swiftc typecheck exit 0**；Models.swift/WenguApp.swift/Package.swift/spec_check.py 逐轮落盘；当前循环形态=swift build 拿真实编译错误→读源码修类型不一致（D11 方向的验证闭环跑通）。剩余：视图层六屏、XCTest、README、NEEDS_FROM_OWNER。监督者已可离场——模型组重排+12 片跑道+安全会话断点+自动重试构成无人值守链条。
- **DEBT 变动**：D24 登记即修（/api/runs limit）；D22（验收自指）/D23（m76 抖动）入账待专项；D26 登记即偿还。

## M79 — 主人可见性四件套（2026-11-23，本地交付）

- **背景**：用户打开控制台只能看到"第 N/24 步、等模型、失败、过会儿重来"——每步做了什么/为什么/新旧 24 步/失败原因全部不可见；聊天记录刷新即丢；暂停舰队里躺着的排队续跑无任何引导；JOURNAL 多组"第1轮/第2轮"重复块加占位符。
- **① 聊天历史回放**：新增 `GET /api/chat/history?n=`（只读路由，鉴权+1..100 校验），前端 boot 拉一次回放进消息区并加分隔线；`Dialogue.history()` 本就存在但从未接线。
- **④ 逐步可见**：决策协议新增可选 `note` 字段（≤60 字执行意图，写进 LOOP_SYSTEM 提示词与原生工具 schema）；trace 每步补 `note/ts/ok`，**失败步也进步数**（此前失败只进 observations 文本）；新增 `GET /api/runs/{id}/timeline`（`proto/fleet/timeline.py`）投影：任务/目标/验收、进度、调度代次+会话连续性（"续跑同一条执行链"=老 24 步、"新开执行链"=全新 24 步）、逐步工具/参数摘要/意图/结果/写入路径、结论与缺失证据。前端聊天区顶部新增「执行链」折叠面板（记住开合，每 4 次轮询拉一次，最新步高亮，失败步红字）。
- **③ 断点引导**：`RunCoordinator.blocked_reason` 修复——续跑中的排队 run 在舰队暂停时报 `fleet_paused`（此前只报 `model_retry_wait`，掩盖"取消暂停即可续跑"）；`/api/activity/live` 快照附带 `blocked` 字段；前端状态徽章显示「已暂停 · 恢复后从断点续跑（不是重开）」+ 一键恢复按钮（走主人快通道）。
- **② JOURNAL 修复**：轮次编号跨进程接续（`note_round` 读已有块数，不再每次从 1 重开）；机械回退摘要输出真实事实（步数/工具分布/落盘/失败尾），消灭"（见 observations 尾部）""（待填）"死引用；LLM 小结含占位符的行被丢弃；三行字段按句子边界截断（句末标点优先，硬切兜底加省略号）；活动中心产出/下一步显示从 50 字放宽到 110 字+悬停全文。
- **验证**：新增 `tests/test_m79_visibility.py` 9 项（历史回放+鉴权边界、时间线 note/链标识/404、paused 续跑 blocked_reason+live.blocked、JOURNAL 编号接续/机械摘要/占位符过滤/句子截断）；完整回归 **1314 passed, 4 skipped**（Python 3.9.2）；desktop `npm test` 4 绿；临时数据目录冒烟实例（不碰生产）逐张截图目检——修复两处目检发现的缺陷（历史被轮询 loop 重复灌两次、聊天区横向溢出），恢复按钮点击后舰队解除暂停、排队 run 被消化、状态归空闲，闭环验证通过。生产 8431 实例未动，改动在下次正常重启后生效。

## M78 — 目录结构整改 · 工具插件化 · 子代理（2026-11-23，本地交付）

- **结构整改**：`runtime/` 23 模块按职责三分——`proto/profile/`（identity/personas/taste/self_facts/methods 人设档案）、`proto/fleet/`（manager/runs/companies/collab/fleet_budget/projects/task_plans/activity 舰队运行）、`proto/coordination/`（sqlite 协调存储，零外部 proto 依赖）独立成包；`runcontext.py` 归位 `exec/`；runtime 只留组合根 8 模块（agent/dialogue/autonomy/summaries/timeout_policy/model_pressure/quota/study——study 因依赖 autonomy 留守）。消除 goals↔runtime、exec↔runtime 两组包级循环；`agent.py` 原地未动。
- **工具包与插件**：六个纯工具实现迁入 `proto/tools/`（codetools/webtools/fleettools/bridge/code_index；engineering 留 exec——被循环直接消费）；`proto/plugins/` ToolSpec 单处声明工具（实现/能力级别/只读/观察限额/后台 IO），首批 core/files/web/fleet/bridge/subagent 六个 pack 装配，对照测试逐值复刻原硬编码清单；`agentic.READ_ONLY_TOOLS/OBS_LIMITS` 与 `_tool_io` 三处硬编码撤销，改读注册表元数据。
- **结构不变量**：新增 `tests/test_structure.py`——kernel/state 纯净、exec 核心不上行（已知例外棘轮式登记，当前为零）、tools/plugins 不碰循环核心、runtime 仅组合根且不依赖 fleet、goals 不依赖 runtime、coordination 独立。
- **子代理 `run_subagent`**：[能读能写工作区边界的隔离子代理](DESIGN.md#subagent)（用户拍板：读写皆可、审计放粗到派发粒度）——独立决策循环、只读感官全量、workspace 档直写项目工作区（与主循环 write_file 同级同界）；工作区外可见写/命令执行/审批/递归派发失败关闭；预算继承、轮次 8/上限 12、`subagent_dispatch/subagent_result` 入哈希链审计、输出打标非指令；schema 入 `contracts._BUILTINS`（M63 校验源属性）。
- **周边卫生**：`start-console.sh` 归位 `scripts/`（README/DESIGN/CI 同步）；`proto/setup.py` 更名 `proto/deploy.py` 留兼容 shim；补 `state/work` 的 `__init__.py`；pyproject `exclude proto_data*`；`.DS_Store` 出库。
- **验证**：新增结构 12 + 插件对照 12 + 子代理 9 项；完整回归 `python3 -m pytest -q`：**1304 passed, 4 skipped**（2026-11-23，Python 3.9.2）。m71 冷却计时用例在全量负载下偶发抖动（单跑稳定通过，与本次改动无关，改动前同族抖动已复现）。离线 wheel 构建确认新子包收录且 proto_data 不泄漏；desktop `npm test` 通过。未调用真实模型、未重启真实实例。

## M77 — 大仓库上下文（2026-11-22，本地交付）

- **已核对并补齐**：原有按行读取、文本检索和 M71 字节投影不等于完整大仓库机制——没有源代码符号索引、临时只读子上下文或预算压力压缩。本轮新增 `CodeIndex`、`explore_code` 和确定性 `decision_input` 投影。
- **按需检索**：`search_symbols` 提供工作区本地、分页、有界的符号页（Python AST；Swift/JS/TS 常见声明；其他已知源码按文件名降级），返回路径/行号/签名与 `next_cursor`；正文仍由 `read_file` 按窗口读取，`grep` 保持文本检索并支持项目内 `cwd`。
- **结构化索引**：索引只缓存声明元数据和文件状态，不缓存源码正文；隐藏/依赖/生成目录、非普通文件、链接、保护路径、二进制和超额文件被跳过或标记降级，缓存按状态失效。
- **子代理隔离**：`explore_code` 在活动执行会话内绑定临时只读探索器；子模型只能 `search_symbols` 和最多 40 行窗口读取，源码留在子上下文，父会话只收短结论与已读位置；写工具、越界路径、递归委派、无证据结论和长源码回显失败关闭。
- **上下文压缩**：默认输入预算 32768、128 KiB 硬顶，UTF-8 字节保守计数；历史过压力线时保留工具回执、检索/读取位置和少量近期观察。供应商明确上下文超限时，未派发决策只缩小重试一次，不重放工具；原始事件与会话仍持久保留。
- **决策输出预算**：原生决策请求强制唯一 `proto_decision` 工具（OpenAI 兼容与 Anthropic 风格各自指定），智谱原生流式决策关闭 thinking；`write_file` content ≤6000 字符、`edit_file` old/new 各 ≤4000 字符，已有文件优先小补丁。模型输出截断时流式层丢弃半截决策，确认未派发/无 pending 后只追加小步提示重试一次；等待中的截断会话恢复后同样进入小决策模式，第二次仍失败才安全等待。
- **范围边界**：指定项目的 `grep` / `search_symbols` / `explore_code` 默认落在该项目，`cwd` 不能切到兄弟目录；局部项目规则在子探索器读取新路径前加载，且不让原读取先执行。
- **验证**：新增 `tests/test_m77_scoped_exploration.py` 16 项，覆盖非首文件符号定位、续页、缓存失效、跳过/降级、兄弟项目拒绝、父/子上下文隔离、工具白名单、源码回显拒绝、压力压缩、固定区超限、供应商上下文超限重试、输出截断小步重试不重放工具、等待会话恢复进入小决策模式与 write/edit 参数上限；决策传输断言强制唯一工具、GLM 关 thinking。完整回归 `python3 -m pytest -q`：**1272 passed, 4 skipped**（Python 3.9.2）；未调用真实模型、未重跑 CI/可选依赖/Linux runsc。

## Electron 控制台（2026-11-22，本地交付）

- **已交付**：[与 Web 单源](DESIGN.md#electron-console)，自动接入同数据目录的本机服务，找不到时可托管，保留 `--connect-only` 和原生 Mac 壳。新增功能只改共享页面/API；退出 Electron 不停止独立 Web。
- **验证**：Node 4 项通过；macOS 真实 Electron 窗口渲染概览，桌面与 Web 双向确认同一 Agent 的改名，并验证接入与托管退出的所有权。Python 全量 **1258 passed, 2 skipped**（启用本机 Xcode，外部 HTTPS 连接快速失败、保留本机回环）。首次全量的旧心跳测试依赖外部请求在 3 秒内完成；基线与本次版本均能用 3.4 秒合成延迟复现，不在本轮改动。
- **启动**：按 [README](../README.md#electron-桌面控制台可选) 安装并启动 `desktop/`。更新独立 Web 时按原参数正常重启，浏览器刷新、Electron 从菜单重连；本次未重启实际舰队。

## 概览实时状态（2026-11-22，本地交付）

- **定位**：iPhone app 软件专家的 Run 自 08:26:53 因模型请求失败保留安全断点；20:57 只读 API 确认它未执行，也没有等待模型冷却。概览依赖短暂的工作链内存标记，将挂起误标为「空闲」。
- **已修复**：[聊天区和概览共用同一次实时快照](DESIGN.md#concurrent-dialogue-live-work)，区分挂起、排队和真空闲；显示同步时间、5 秒超时回报未同步，切换成员时丢弃旧回包。目标与项目文件仍慢轮询。
- **验证**：相关测试 69 项通过；全量 **1256 passed, 2 skipped**（启用本机 Xcode）。临时舰队的 Chrome 页面显示「待继续」和本轮卡点，无模型调用、无自动续作。
- **生效**：现场服务仍提供原页面，按原参数正常重启再打开控制台。未重启真实进程、变更任务或工作区。

## 结构化模型决策与安全续作（2026-11-22，本地交付）

- **定位**：2026-11-22 08:24，智谱给出中文说明和有效动作，Proto 的旧整段解析判错；下一次调用只留下笼统错误，安全断点未自动续作。当天前台智谱调用有成功记录；11-21 的 31 次读超时与 119 条本地未发送的熔断记录应分开看。对照 ZCode 和 Codex 的模型/执行边界，修应用链路，不更换供应商。
- **已实现**：原生流式执行决策配合旧文本兼容，区分首事件、流中断、输出截断和 HTTP 错误；完整决策先校验再派发。安全故障沿用原 Run/Session，有限重试且不重放已完成步骤；控制台区分当前受阻和历史失败，简短进度问询零模型调用。[设计与验收](DESIGN.md#model-decision-transport)、[ADR 0020](adr/0020-model-decision-transport.md)。
- **验证**：新增 27 项。回环 HTTP 覆盖 OpenAI/Anthropic 流式参数分片、完成边界、心跳不续期、组内切换、额度/取消、真实运行活动和重启保留原 Run/Session；断流前后两种场景均只写一次。系统 Python 3.9.6 启用本机 Xcode 的全量回归 **1254 passed, 2 skipped**（153.36 秒），页面脚本与 diff 检查通过。
- **边界**：未调用实际配置的模型、修改真实 Agent 工作区或重启已有服务。旧的人工挂起任务不会在升级时被自动放行；重启新版后可在控制台「继续原任务」。真实模型延迟、费用和工程完成率需以实际任务数据验收。

## 中文对话去模板腔（2026-11-22，本地交付）

- **已实现**：11 项本机规则、4 个文体，接入 Web/REPL 的共同生成出口。处方限定在命中句，同版复扫，不合格最多重试两次再退回原文。人设/原样和创作意图优先，实际对话与记忆只收到最终文本；新增只读工具与配套技能。不依赖未核实的第三方插件。[交互和验收](DESIGN.md#natural-dialogue)、[ADR 0019](adr/0019-deterministic-dialogue-style.md)。
- **验证**：新增 53 项，相关套件 187 项通过，记忆用例与新功能复跑 63 项通过。覆盖字节级保留、保护片段、无新增事实、两次回退、真实回环 HTTP 与终端入口、历史/幂等重放、日志脱敏及无额外模型调用。全量 **1227 passed, 2 skipped**（系统 Python 3.9.6，125.44 秒，启用本机 Xcode；缺少可选 `pymongo`、`langgraph`）。另离线构建 wheel 并直接加载规则/技能；512 组确定性拼接回放均可按补丁重建最终文本。
- **边界与后续**：不判定作者、不做原始回复真实性审计。未接通已配置的真实模型、改动运行中的用户数据或重启现有控制台；后续用获得许可的用户反馈校准规则误报。

## 工程技能与本机验证（2026-11-22，本地交付）

- **已定位并实现**：旧对话确认未探测 Swift/Xcode 就判断不能编译；本机发现 Xcode 不能代替项目验证。补上按需加载的三个内置工程技能、`inspect_build`、离线只读问询、真实命令回执和一次同片补正。Xcode 工位识别兼容局部 workspace；隔离样例另发现并修复 pytest 默认日志对 `/dev/null` 的拒写。[工程设计](DESIGN.md#engineering-skills-and-evidence)、[ADR 0018](adr/0018-engineering-skills-and-local-evidence.md)。
- **定向验证**：新增 44 项；相关套件 **82 passed**（启用真实 Xcode，10.98 秒）。替身模型驱动 Agent 自己落盘设计和 Python 实现、跑 pytest；另实跑 npm 构建和测试、`.xcodeproj` / `.xcworkspace` 的 iOS Simulator `.app` 构建。覆盖只有版本、失败、过期回执、重启不重跑、授权和只读界限；wheel 中三份技能可由 Python 3.9.2 直接加载，页面 JS 语法检查通过。
- **全量验证**：本机系统 Python 3.9.6（SQLite 3.54），隔离外网、保留回环并启用 Xcode，**1174 passed, 2 skipped**（135.62 秒；缺少可选 `pymongo`、`langgraph`）。
- **原有回归波动**：Python 3.9.2 所带 SQLite 3.34 偶尔把 JSON 重试时间解析到约 0.24 微秒之后，让定在精确截止时刻的限流测试拿不到候选。用失败时钟值在未修改的基线上复现；本次不改模型调度。
- **边界**：未操作原 Agent 工作区、调用已配置真实模型、启动新的模拟器或重启现有控制台。浏览器交互、持续后台命令和更多工具链列入 [D18](DEBT.md#engineering-parity-gap)。

## 自我认知与生命周期（2026-11-22，本地交付）

- **已定位并实现**：旧对话只有人设和记忆，没有身份与时间真值；注册表的创建和收编时间不能混用。独立保存每位成员的创建证据，分开计算年龄和本次加载时长；只读快通道覆盖时间、名称、ID、人设、任职及自治状态。普通对话和每步执行收到实时数据，「名称 / ID」可查时长快照。[设计与验收](DESIGN.md#grounded-self-knowledge)、[ADR 0017](adr/0017-runtime-self-knowledge.md)。
- **验证**：新增 15 项回归并扩展名称面板测试；本机 Python 3.9.2 隔离外网、保留回环模型替身时，`python3 -m pytest -q` 为 **1129 passed, 3 skipped**（101.30 秒）。覆盖旧证据回填、证据不足、收编与重启、重新锚定、时钟回拨、Web 占用运行/状态/对话锁及 REPL 零模型调用、冻结上下文和成员切换。Chrome 临时控制台在桌面及 390px 验证，暂停的离线成员仍能报告年龄，打开另一成员面板不串数据；Python 和页面 JS 语法检查通过。
- **交付边界**：旧成员按原数据目录启动新版后渐进补录，不改注册表、旧审计和真实工作区。不推算累计在线或工时；未调用已配置的真实模型、重启现有控制台或发布。

## 自主想法质量循环（2026-11-22，本地交付）

- **已实现**：GoalForge 取材、原文拆解、跨素材重组、最大多样集合、隔离批评、有限修正和复审。三种姿态、跨轮去重和诊断落盘；单轮至多立一项目标，首步和放弃条件随目标保存。控制台与只读接口分别显示摘要与完整报告。[设计与验收](DESIGN.md#idea-forge)、[ADR 0016](adr/0016-diverse-ideas-and-independent-critique.md)。
- **验证**：新增 20 项回归；四个相关模块 **145 passed**；全量 **1114 passed, 3 skipped**（Python 3.9.2，115.45 秒）。桌面及 390px Chrome 临时实例验证目标树状态、通过数量、首步与放弃条件。[离线回放](specs/idea-forge-example.md) 的独立批评与复审后 yield 为 3；脚本化回放覆盖五次创意调用上限。
- **边界与后续**：未调用项目配置的模型、重启运行中的服务或执行候选项目。结构向量是语义近似；真实模型误判率、用户价值和自动化试验决策未验证，见 [D17](DEBT.md#d17--自主想法的结构语义距离没有真实用户校准)。

## 模型超时恢复与执行日志（2026-11-22，本地交付）

- **已实现**：修复 Python 3.9 的读超时误判，正常切换备用接入或保留原 Run 等待重试；HTTP 错误直接保留状态及退避提示，不等待响应体。Web 和交互 CLI 写入私有滚动日志，关联实际模型请求、本地拦截、工具阶段与 Run 恢复。[设计与验收](DESIGN.md#execution-diagnostics)、[ADR 0015](adr/0015-model-timeouts-and-execution-log.md)。
- **验证**：新增 15 个用例，覆盖两种协议的真实回环读超时、包装异常、限流与额度、原 Run 续作不重放工具、启动及日志脱敏/轮转/故障隔离。全量 **1094 passed, 3 skipped**（Python 3.9.2，116.97 秒）；临时离线 Web 进程 HTTP 200，启动、就绪、退出日志齐全且未写入令牌。
- **边界**：无数据迁移；未重启真实服务、调用真实模型或改动真实任务。启动新版本后写日志，已保存的旧安全断点可从控制台继续。

## 自主项目目录优化（2026-11-22，本地交付）

- **已实现**：新目录使用按词缩短的 ASCII 标题加根目标 ID，避开其他目标（含终态）及同名磁盘条目，同一目标树继续共享一个主目录。索引显示完整目标、绑定目录与状态；已绑定工位不可用时保存暂停原因，修复后可恢复。[设计与验收](DESIGN.md#autonomous-project-dirs)、[ADR 0014](adr/0014-autonomous-project-directory-identity.md)。
- **验证**：定向回归 **44 passed, 1 skipped**；`python3 -m pytest -q` 为 **1079 passed, 3 skipped**（113.07s）。覆盖同名冲突、终态绑定、重启续作、旧工位复用、归档保护和首轮产出索引；Python 编译及页面 JS 语法通过。
- **边界**：新规则不重命名、不合并旧项目，未绑定文件夹保留原样。未修改真实 Agent 数据或项目，未调用真实模型、重启控制台或发布。

## 控制台补丁 · 「它的审美」自动加载（2026-11-22，本地交付）

- **根因与修复**：`GET /api/taste` 正常返回，但原页面只在校准后调用审美刷新。现随选定 Agent 轮询，读取不调用模型；切换隔离过期响应，失败或超时提示并自动重试，不改变校准/评判。[加载契约](DESIGN.md#133-api轮询与错误反馈)
- **验证**：新增 2 项回归，全量 **1069 passed, 3 skipped**；离线 Chrome 桌面与 390px 验证默认五维、选择另一成员、503 后恢复。未修改实际 Agent 数据、调用真实模型或重启现有控制台。

## M75 补丁 · 公司模板加载与控制台版本一致性（2026-11-22，本地交付）

- **根因与修复**：运行中的旧实例从工作树读到了新 HTML，但 `GET /api/companies` 返回 404，并非缺少模板。页面改为随服务启动加载；公司目录未就绪时禁用创建，旧路由提示正常重启，暂时失败可刷新。详见[组织契约](DESIGN.md#company-organization)及 [ADR 0012](adr/0012-company-organization-and-messaging.md)。
- **验证**：新增 2 项回归；`python3 -m pytest -q` 为 **1067 passed, 3 skipped**。临时 Chrome 桌面及 390px 验证旧接口提示、刷新保留名称，以及空舰队加载四种模板并创建；未重启正在运行的实例、写入实际舰队数据或调用真实模型。现有实例须按原启动方式重启后使用公司功能。

## M76 · 模型组与自动切换（2026-11-22，本地交付）

- **已采纳**：[模型组交互与验收契约](DESIGN.md#model-groups)、[ADR 0013](adr/0013-model-group-failover.md)。
- **已实现**：顶栏模型面板可创建、编辑和排序接入组，主/快角色可选组、单模型或跟随主模型（仅快角色）。Agent 组内有界回退，每次派发独立守卫和结算；同源 429 跨角色、Agent 和重启生效。全组暂不可用时保留安全会话断点，后台活动显示实际调用的成员。
- **验证**：`python3 -m pytest -q` 为 **1065 passed, 3 skipped**（106.69s）；新增 32 项，覆盖旧目录、令牌和修订、同源退避、额度和取消、在途配置隔离和原 Run 续作。独立 Chrome 验证桌面和 390px 的建组、排序、选组、编辑、滚动与独立反馈底栏，以及本地 HTTP 429 换备用模型。
- **边界**：配置仍是舰队级；未调用真实模型、修改已有 Agent、重启现有控制台或发布。

## M75 · 公司、职位和组织通信（2026-11-22，本地交付）

- **已实现**：「项目 → 公司与组织」可建多家公司、预览模板、编辑层级和职责、任命现有 Agent 或新建草稿。直属汇报、指导与同级协作按当前组织路由；越级、跨公司或公共黑板绕行被拒绝，收件箱不随换任转移。[组织契约](DESIGN.md#company-organization)及 [ADR 0012](adr/0012-company-organization-and-messaging.md)。
- **验证**：`python3 -m pytest -q` 为 **1033 passed, 3 skipped**（106.89s）；M75 新增 12 项，覆盖旧库迁移、并发唯一任职、重组/归档、权限边界和假模型调用工具。独立 Chrome 验证桌面和 390px 的模板、职位、两种任命、汇报及冲突后保留草稿。
- **边界**：消息不自动派工；不支持双汇报；任职不改变人设、自治或授权。未调用真实模型、操作现有 Agent、重启现有控制台或发布。

## M74 · 舰队批量操作（2026-11-22，本地交付）

- **已实现**：「概览 → Agent 舰队」[批量暂停、恢复与归档](DESIGN.md#fleet-bulk-controls)。暂停标记一次持久化，不阻断对话；归档核对确认名单，跳过忙碌成员并保留数据。空舰队重启不再自动新增或收编 Agent。
- **验证**：`python3 -m pytest -q` 为 **1021 passed, 3 skipped**；覆盖并发占用、过期确认、存储失败、旧版收编与空舰队重启。独立 Chrome 验证桌面和 390px 窄屏的批量操作、取消确认、部分归档、空舰队与创建新成员。
- **交付边界**：只验证临时舰队，未操作现有 Agent 或重启现有控制台，未重新发布。

## M73 · Agent ID 与显示名称（2026-11-22，本地交付）

- **已实现**：顶栏及舰队卡片打开「名称 / ID」，卡片展示完整 ID。主人改名只写既有注册表的名称，保留选择、工作目录、人设和原运行，不等待 Agent 的执行或对话锁。见[设计与验收](DESIGN.md#agent-display-name)及 [ADR 0006](adr/0006-agent-lifecycle-and-work-coordination.md)。
- **验证**：`python3 -m pytest -q` 为 **1010 passed, 3 skipped**；覆盖 ID 字段拒绝、未授权拒绝、后台锁占用、收编成员重启与待运行记录保留。独立 Chrome 控制台验证桌面和 390px 窄屏，只读 ID 可选中复制，两处入口改名不清空聊天草稿，刷新后仍显示新名称及原 ID。
- **交付边界**：无数据迁移，未重启现有实例或触碰 Agent 的实际工作区。

## M72 · 负载保护与可验收交付（2026-11-22，本地交付）

- **已定位/采纳**：1,801 条验证/交付记录含 1,780 条无产出失败；存在实际供应商 429，也存在大量未发送的本地熔断。主/快模型共享路由，App 各阶段分离且无真实应用构建。见[设计与验收](DESIGN.md#evidence-driven-delivery)及 [ADR 0011](adr/0011-model-pressure-and-evidence-delivery.md)。
- **已实现**：同一接入一次一请求，首个 429 持久退避，冷却后试探；未发送不扣额度，托管工作保留原 Run 与截止。自主目标按前置顺序共享主工位，限制每片步数和无进展重试，以当前文件和真实构建回执验收；控制台显示卡点、历史与目标恢复入口。macOS 修复 Python 未暴露 Darwin 临时目录常量导致的 Xcode 沙盒构建失败。
- **验证**：本机 Python 3.9.2，`PROTO_TEST_XCODE=1 python3 -m pytest -q` 为 **1008 passed, 2 skipped**。离线复现共享模型限流、热切换、重启接回原 Run、不重复写入；内置最小 SwiftUI 样例在原沙盒中真实构建为模拟器 `.app`，验证区外写入、受保护文件读写和联网仍被拒。独立控制台验过卡点、未授权恢复拒绝和恢复后保留历史；Python 编译与页面 JS 语法检查通过。
- **交付边界**：不介入真实 Agent 项目，不代验收或改变目标。XCTest 回执认定走替身测试，未在本轮启动样例或真实 App 运行设备测试。未调用真实模型、重启现有服务、推送或发布；重启时协调库自动迁移到 v4，需回退则先冷备份。

## M71 · 工作上下文与断点续作（2026-11-21，本地交付）

- **已采纳**：[改造前分析与验收契约](DESIGN.md#durable-work-continuity)及 [ADR 0010](adr/0010-bounded-context-and-safe-continuation.md)。旧机制会遗失较早的进度线索，并将一些明确可续作的中断交给人处理。
- **已实现**：执行决策按 UTF-8 字节分配固定约束、辅助和观察预算；原事件与真实证据保留。托管新版会话用同一 Run 续接安全断点、退避模型失败，区分验收模型不可用和验收未通过；未知效果不重跑，确认关闭同时结束关联会话。
- **验证**：新增 15 项回归；本机 Python 3.9.2 全量 **996 passed, 2 skipped in 106.04s**。临时子进程硬退出覆盖等模型、已存决策、工具完成、效果未知和已验收。Chrome 用假模型在控制台从 403 卡点续作，仅出现一次写入记录，随后通过验收，无 JS 控制台错误。Python 编译和页面 JS 语法检查通过。
- **交付边界**：无协调库迁移，不自动恢复旧未知调用；字节限额不替代模型 token 适配，未知外部副作用仍需核对。未调用真实模型或重启真实服务，未推送、发布或更改真实数据。

## M70 · 边工作边对话（2026-11-21，本地交付）

- **已采纳**：[核心设计原则](DESIGN.md#core-design-principles)、[实现与验收契约](DESIGN.md#concurrent-dialogue-live-work)和 [ADR 0009](adr/0009-concurrent-dialogue-and-live-work.md)。同一 Agent 持续工作和对话，能力以交付证据衡量，允许在受权范围内自发学习、反思、立项和推进。
- **已实现**：Web 对话、后台 Run 分离长等待，保留状态护栏和单 Run；新增只读进度问询、实时活动时间线、窄屏抽屉、对话回执及后置记忆。主模型故障进入有界冷却，冷却到期后单次恢复探测；后续阶段收到同谱系既有文件路径，文件落盘与任务验收分开。
- **现场发现**：iPhone app 软件专家并非无源码。失败执行在首次工具前遇到 `403` 或模型超时，随后冷却中反复记失败；既有 SwiftUI、测试及设计散在不同目录，未验收为单一可构建 App。保留原文件，未修改线上模型选择、计费配置或运行数据。
- **验证**：新增 7 项回归；本机全量 **981 passed, 2 skipped in 100.02s**，Python 编译、页面 JS 语法和 diff 检查通过。Chrome 临时实例验证等待模型时正常回复、写入与验收分开、1280 像素宽屏和 390×844 窄屏，未发现 JS 错误。使用假模型，无真实推理费用。
- **下一步**：以现有数据目录重启真实实例，确认主模型可用后观察专家是否复用原项目并给出真机或构建验收证据。没有发布、推送、重启真实实例或宣称 iPhone App 已交付。

## M69 · 忙碌时的活动问询（2026-11-21，本地交付）

- **已采纳**：后台 Run 占锁时可用固定短句查询实际活动；保留单写者，显示聊天忙碌或发送失败。见[设计与验收](DESIGN.md#console-activity-chat)和 [ADR 0008](adr/0008-read-only-console-activity.md)。
- **已实现**：独立短问句复用实时标记与运行中 Run，不调用模型、不写对话历史；普通聊天仍串行，等待超时显示本条未执行，不自动重发。
- **验证**：新增 7 项；定向 31 项通过，全量 **974 passed, 2 skipped in 101.24s**。Chrome 临时离线实例复现同 Agent 后台占锁；状态问询收到目标摘要，普通聊天收到忙碌提示，按钮恢复。
- **交付边界**：无数据迁移；关闭临时实例，未调用真实模型、修改真实配置、重启真实实例或发布。真实服务下次启动后启用后端改动。

<a id="m68"></a>

## M68 · 只读计划、分层覆盖和文件证据（2026-11-21，本地交付）

- **已采纳**：[同类机制比较与取舍](adr/0007-reviewed-task-plans.md)、[实施与验收契约](DESIGN.md#reviewed-task-plans)。
- **已实现**：按目录选择 `AGENTS.override.md` / `AGENTS.md`；只读规划单次计入双层额度，待审可从历史找回。按 SHA-256 幂等批准、执行前校验工作区与基线；额外声明的文件须非空且有变化，再走语义验收，工具审批保持独立。
- **验证**：新增 23 项回归，覆盖并发批准、权限、两层额度、中断/重启、规则/文件漂移、路径安全、空/缺失/未变文件与双验收。定向复跑 33 项通过；全量 **967 passed, 2 skipped in 95.02s**（本机 Python 3.9.2）。离线 Chrome 验证审阅布局、重载后可恢复计划、批准前无产物、执行后文件证据 `1/1`。
- **交付边界**：未调真实模型、重启真实实例、推送或发版。直接执行和项目协作维持原契约；扩展技能、钩子、工作树隔离和复杂验收脚本未纳入本轮。

## 舰队额度 · 输入单位对齐（2026-11-21，本地交付）

- **已实现**：舰队每日 tokens / calls 支持与 Agent 相同的数量后缀，API 兼容原整数输入并以整数持久化。校验失败不改动任一上限，轮询不覆盖草稿；调额保留已用和在途额度，不改变 Agent 三分池或扣减顺序。见[运行契约](DESIGN.md#run-scheduler)。
- **验证**：新增 23 项回归，覆盖精确换算、零上限、非法输入、调额保留占用和重启持久化。定向 70 项通过；全量 **944 passed, 2 skipped in 86.22s**（本机 Python 3.9.2）。Chrome 临时离线控制台验证保存 `16万` / `1.2k`、错误反馈和轮询保留草稿，顶栏 Agent 额度不变。
- **交付边界**：同步 README 和总设计，已本地提交，未推送或发布；无数据迁移，未改真实实例额度。

## M66–M67 · 舰队运行与项目交接（2026-11-21，本地交付）

- **设计先行**：生产实现前记录 [ADR 0006](adr/0006-agent-lifecycle-and-work-coordination.md)、[运行契约](DESIGN.md#run-scheduler)与[项目契约](DESIGN.md#project-delivery)。
- **M66（F3–F5）**：心跳、立刻行动、主人任务及续批使用持久 Run；单 Agent 单写者、公平有界并行、边界取消与人工核对。舰队模型预算与原来源池同时预检，票据按请求和原周期核销，未知不重放。
- **M67（F6–F8）**：主人定义分派、必需前置与固定文本验收契约。可单 Agent 独立验收，也可由另一 Agent 复核同版证据；通过后才放行依赖，失败可重试并保留旧版。
- **自动验证**：新增 26 项定向回归，退役 1 项内存 job 测试；全量 **921 passed, 2 skipped in 81.47s**（本机 Python 3.9.2）。Chrome 临时实例验证建项目、分派双角色、查看验收和 SHA-256、草稿行动受阻及调整舰队预算；产物对话框适应 390px 视口。使用脚本模型和临时数据，未调用真实模型。
- **离线对照**：运行 `python3 -m pytest -q -s tests/test_m67_project_delivery.py::test_same_budget_solo_and_team_scenario`。同一脚本模型、每组上限 5,000 tokens / 20 calls、同一两阶段契约，单 Agent 与双 Agent 均验收 2/2，主人操作都是 3 次（建项目 + 两次指派）。一次本地采样分别为 38.7ms / 36.5ms、2 / 4 次调用、200 / 400 计量 tokens。耗时为短链测试观测，不推断真实工作负载的速度或质量；另测不同人设能改变固定产物中的交付策略。
- **交付边界**：同步 README、总设计、ADR 和 D15/D16。与 M65 一并本地提交，未推送、发布或重启真实实例；未知外部效果仍由主人核对，首版不执行产物或任意验收脚本，不扩展宿主隔离。

## M65 · 人设与自治控制（2026-11-21，本地交付）

- **设计先行**：在 `c7ffc66` 上完成 21 项基线回归与临时探针，记录人设不一致、收编缺接线、顺序心跳与目标阶段可乱序；生产代码前补总设计 §12.6 和 ADR 0006。
- **已实现**：版本化人设与参数来源、创建/编辑/迁移/收编接线、Web/CLI 预览和草稿、独立持久自治策略。暂停与临时静音独立，心跳/立即行动/超时不绕过；自主来源不混用预算分类，工具和审批复核实时策略；主人对话、指派、拒绝仍可用。
- **自动验证**：新增 28 项，最终全量 **896 passed, 2 skipped in 78.28s**（本机 Python 3.9.2）。使用本地回环 HTTP、假模型和隔离 Node 脚本；未连接真实模型。Chrome 验证临时控制台主界面可读，点击和截图被工具权限阻止；已停止临时服务，不作交互或版面验收通过声明。
- **交付边界**：同步 README、总设计、ADR、进度与 D14 偿还；与 M66–M67 一并本地提交，未发布或重启真实实例。该节点交付 F1/F2，见[总设计 F1–F8](DESIGN.md#fleet-next)与 [D14–D16](DEBT.md#fleet-debt)。

## M64 · 模型接入与热切换（2026-11-21，本地交付）

- 设计先行：先更新总设计第 14.4 节、UI 邻接清单、ADR 0005 与 H1–H10 验收，再实现；页面适配变化先同步设计。
- 接入目录：启动数据根下持久保存主/快角色选择和 OpenAI-compatible / Anthropic-compatible 接入；已有目录优先于启动环境。运行中新增、编辑、切换和显式磁盘重载均不需重启，保存接入不意外改变选择。
- 调用边界：舰队共享稳定角色代理，已有/新建/懒加载/收编与离线 Agent 的归因、规划一并接线。在途请求保留旧客户端与用量，下一调用使用新配置；配置 API 不等待 Agent 长操作锁，不重置预算、审批、执行上下文或步数，熔断按 Agent 和配置隔离。
- 安全与入口：控制台顶栏模型面板、令牌鉴权 API、REPL `/models` / `/model` 及舰队 CLI 启动读取；0600 原子落盘、revision 冲突保护、严格校验、密钥脱敏/空白保留/显式清除、写入失败保留旧配置，凭证文件和临时文件加入 Git 忽略。
- 自动验证：新增 **62 项**定向回归通过（11.92s）；最终完整 **868 passed, 2 skipped in 76.97s**（Python 3.9.2）。使用真实本地 HTTP 假端点与隔离 Node 页面脚本，不调用真实付费模型。
- 可视验收：临时实例真实 Chrome 验证新增接入、主/快选择、刷新保持与 Escape 清密钥；1280×960 和 390×844 页面检查通过，窄屏单列/内部滚动，控制台无新增 JS 错误。临时服务与页面已关闭，不触碰真实实例。
- 静态与文档：Python 编译、页面 JS 语法、`git diff --check`、66 个本地链接/17 个显式锚点/16 份相关文档覆盖检查通过；README、总设计、ADR、进度和 D9 部分偿还状态同步。
- 交付边界：验证后本地提交，不推送、不发布、不重启真实实例；未重跑 CI/可选依赖组合/Linux runsc，不承诺新协议代码热加载、跨进程共享目录或完整请求级用量重构。旧进程需先更新到本版本，之后新增/切换模型不需重启。

## M63 · 执行上下文、工具契约与会话连续性（2026-11-21，本地交付）

- 设计先行：先更新总设计第 15.2 节和 ADR 0004，记录目标/非目标、用户流程、状态、作用域/指令优先级、参数校验、崩溃核对与 A1–C5 验收，再依序实现并补回归；新增边界和页面接线先补设计再改代码。
- 执行上下文：聊天与执行共享身份但分离表达协议；actor 每步与快速规划得到原验收、人设、主人优先级、内化规则、来源池和项目根。AGENTS 祖先作用域加载、范围切换重决策、16 KiB/64 KiB 上限、符号链接/越界/编码失败关闭；重启保持快照。
- 工具契约：所有已注册内置工具显式 JSON Schema 子集，自定义工具保守推导签名；目录、规划器和校验同源。未知字段、缺必填、类型/界错误在审批、Scratch 和副作用前成为可修正观察；移除静默过滤，恢复时复核当前契约与最低权限。
- 会话连续性：新增版本化 execution_sessions，保存原参数/完整工具结果事件、观察/trace、原上下文、步数和来源；审批后回原循环与质量门，再暂停不丢入口。同目标待批不另起链；损坏状态、工作区切换、版本变化、目标丢弃和双恢复失败关闭；completed 结果只接纳，未知在途阻塞待核对。
- 额外修复：有限验收重试不再用旧失败覆盖新审批；控制台提案批准/拒绝汇入原恢复链，不生成无关自主草稿。页面展示原工具参数与最新结果，再次暂停更新 task id，并修正待批徽标未定义列表。审批决定在 HTTP/运行时/Router 必须是实际布尔值，字符串 false 或数字不能按真值误批准。
- 边界：继续 JSON 文本协议；原生 provider tools、请求级 usage、token compaction、跨进程事务和进程强杀不纳入本轮，不关闭相应结构债。
- 验证：新增 **70 项**定向回归，相关旧测试同步新契约；最终完整 **806 passed, 2 skipped in 64.80s**（Python 3.9.2）。HTTP 入口使用本地服务、页面状态使用隔离 Node 脚本；系统沙箱/回环绑定测试在本机授权环境重跑通过，不接真实模型。
- 静态与文档：Python 编译、完整页面 JS 语法和 `git diff --check` 通过；62 个本地 Markdown 链接、17 个总设计锚点、15 份相关文档覆盖映射有效。README、总设计、ADR、进度与债务状态已核对一致。
- 验证限制：未重跑 CI/可选依赖组合/Linux runsc；浏览器工具调用被审批策略阻止，整屏截图未完成，不将自动脚本冒充可视验收。
- 交付边界：总设计、README、ADR 与 D7 的部分偿还同步；原有设计整合改动保留，本轮按主人要求本地提交，不推送、不发布、不重启真实实例。

## 设计文档整合（2026-11-21，本次文档交付）

- 新增 `docs/DESIGN.md`：将原有全部 14 份设计/计划/ADR/UI/进度/债务文档涉及的设计域归并为一份完整正文，包含目标与非目标、用户流程、状态模型、交互、错误处理和验收。
- 明确“已实现 / 部分实现 / 待实现 / 历史”的语义，保留所有旧文件作为溯源，并在旧规格、计划、ADR 和 UI 规格顶部增加统一入口。
- 同步 README：完整设计只指向总设计；纠正自动回退未隔离 subprocess、完整会话恢复及未经本项目验证的质量数字等过期表述。
- 记录后续模型交互优化方向：统一 actor 上下文和固定验收、工具参数契约/协议适配、请求级 usage、持久会话/核对/取消。本次未实现这些能力，不增加已完成里程碑。
- 验证范围为文档覆盖、相对链接/锚点、入口一致性与 `git diff --check`；不改生产代码、不重跑 Python 全套，不把 M62 的 736 passed、2 skipped 当成本次测试结果。
- 文档检查通过：14 份原文均有覆盖映射、17 个总设计章节锚点、59 个相对链接；旧规格入口已统一。交叉核对代码时明确了四级目标、独立换法计数存储、顺序舰队 tick 和进化后端环境回退边界。

## M62 · 运行时不变量审查（2026-11-21，本轮完成）

- 已完成：核对五层架构、M61 实现与现有文档；基线 **653 passed, 2 skipped**；确认审批、预算、模型协议、工作区、目标状态与网页传输的跨层缺口。
- 设计先行：新增 `docs/specs/2026-11-21-runtime-audit.md` 和 ADR 0003，明确恢复认领、真实用量结算及本轮边界。
- 已修复 R1–R8：审批拒绝与恢复防重放、原始待批内容绑定、不可逆写审批；任务来源预算贯通、实际用量欠额结算与原子上限配置；严格模型协议、工作区重绑定、读取边界、目标幂等和网页传输边界。
- 验证：新增 **83 个回归用例**；完整测试 **736 passed, 2 skipped in 62.73s**（本机 Python 3.9.2）；`git diff --check`、Python 编译与 shell 语法检查通过。本轮未调用真实模型、未重跑 CI/可选依赖组合/Linux runsc。
- 文档：设计、ADR、README 与 DEBT 已同步；本地提交范围包含原有 M61 改动与本轮 M62 修复，不包含推送、发布或真实实例重启。
- 后续未完成项：优先 D12 宿主读取隔离与 D13 可信进化评测，再推进 D9 请求级用量/跨日预留、D7 完整会话恢复/取消、D8 存储事务与路径竞争；D10 授权不一致需权利人确认，D11 确定性验收和 D5 Linux 实证仍待专项。

> 单一事实来源：里程碑、状态、验证证据。每次合并不只是代码——本文件同步更新。

## 当前状态

| 项 | 状态 |
|---|---|
| 版本 | 0.33.0（本地实现至 M75，未重新发布） |
| 当前设计 | `docs/DESIGN.md`（完整正文，旧规格仅溯源） |
| 运行与协作 | 运行恢复、项目验收、先审计划、舰队控制和公司定向通信已回归；多机、强制终止、任意验收脚本不在首版 |
| 测试 | 1033 passed + 3 skipped，106.89s（2026-11-22，本机 Python 3.9.2） |
| 分支 | `main` |
| 交付 | M75 本地交付，未推送或重新发布 |

## 里程碑

### M1 — v2.0 设计定稿（2026-11-11）

- v1 设计评审：3 个架构级漏洞（主人锚定无认证通道 / 内化单向门 / L5 证据未分轨）+ 10 项工程细节。
- v2.0 设计：13 项修订（R1–R13）全部落地，新增安全模型（§3）与审计脊柱（§10）。
- 文档：`docs/specs/2026-11-11-proto-v2-design.md`。

### M2 — 核心引擎（阶段 1–5，已合并 main @ 081f85f）

- L1 内核：HMAC 主人锚定（防篡改/过期/重放/伪造/轮换会签）、三通道分级（OWNER 1.0 / SESSION 0.3 / CONTENT 0.05 + 累积池 50% 硬顶）、预算三分池与熔断、高危拦截（不可逆仅人类）、哈希链审计脊柱。
- L2 状态：参数体系（权威界+行为代理）、单一写者 StateManager、跨批次分池归因累积、PAD 情绪。
- L3 元认知：可插拔归因器、双向内化台账、ε 多样性策略库、主人对齐目标仲裁。
- L4 执行：情绪→认知有界调制器、确定性状态注入器。
- evalx：行为代理 harness（Cohen's d ≥ 0.5 判可测）。
- 验证：**121 项测试全绿**；独立评审（1 Critical + 5 Important）全部 TDD 修复；`python3 -m proto.demo` 退出码 0。

### M3 — 阶段 6–8 + 质量收尾（进行中）

- [x] T1 minor 修复批（M2–M8）：审计 append O(1) 缓存、store 路径安全、锚定注册仪式+会签新鲜度、预算未知单位不熔断、目标拒绝写 DENIAL、to_dict 深拷贝、累积器 audit 必传、store 提层 proto/storage、Kernel.trip_breaker 公开、harness verification_rounds+默认200、注入器补性格线+golden 全文测试。验证：135 测试绿。
- [x] T2 阶段 6：ScratchWorkspace 隔离与晋升、ApprovalQueue（内核分级/日上限/静音/全审计）、CheckpointStore（schema_version@variant_id 绑定）。验证：148 测试绿。
- [x] T3 阶段 7：OwnedTestSet（OWNER 签名追加、按构造即治理无删除 API）、Sandbox（subprocess+清洁环境+CPU 限制+超时）、PrivateScorer、VariantArchive（进化树/lineage/canary）、EvolutionEngine（分数门+全过门→canary+审计）。验证：162 测试绿。
- [ ] T4 阶段 8：影子双跑 + 分级授权
- [ ] T5 文档专业版：设计 v2.1（Mermaid 图）、README、demo 扩展、集成测试

## 已知遗留（M3 处理后清零）

见 T1 任务清单；均来自 M2 独立评审 minor 项。

## 下一步（设计 §12 之后）

LLMAttributor 接入模型 API → LangGraph 编排接入（节点签名已兼容）→ MongoDB 适配 DocumentStore → 生产沙盒（gVisor/microVM）。

### M4 — 引擎完全体补完（已完成，feat/engine-completion）

- [x] ①参数调制回滚（窗口反演 + owner_locked 保护 + 内化级联降级）+ 阻尼控制（连续拒绝→欲望阈值上调，有界；接受即重置；接入审批队列）。
- [x] ②Router/Worker 执行器骨架：ToolRegistry、Task/TaskResult、预算预留（owner/self 池分账）、逐步检查点、只读零审批、可见写暂停于检查点→批准恢复并晋升 scratch/拒绝中止、未知工具失败。
- [x] ③L3 调度器（目标生成 4h / 深度回顾每日 / 每任务蒸馏即时，时钟可注入）+ 确定性策略蒸馏器（重试成功→正向策略；失败/被拒→条件化教训；平凡成功不膨胀）。
- [x] ④兴趣强化收敛（EMA 权重、点赞加倍学习率、进目标仲裁可翻盘静态 interest）+ dormant 冻结（行为代理测不出差异→冻结调制，可解冻）+ 欲望动态增长（闲置驱动，1h 宽限，有界）。
- [x] demo 扩至 12 段；pyproject 0.3.0；设计文档 v2.2（§14 矩阵 8 个新行 + 修订记录）。
- ⑤ LLM 接入（执行器步骤生成 + LLMAttributor + LLM 蒸馏/策略适配）：接口就绪，待 API 接入方式。

**验证**：210 项测试全绿；demo 12 段退出码 0。设计内机制全部落地，无遗留引擎欠账。

### M5 — 活的 Agent 运行时（已完成，feat/live-agent-runtime）

- [x] LLM 客户端：OpenAI 兼容（urllib 零依赖），环境变量配置（PROTO_LLM_BASE_URL/API_KEY/MODEL），方舟/OpenAI/DeepSeek/Ollama 通用；本地 HTTP 假服务实测。
- [x] LLMAttributor：参数白名单 + 方向归一 + 强度夹紧 + JSON 围栏容错解析 + 失败回退启发式——LLM 误判无法放大权限。
- [x] LLMPlanner：规划输出过工具白名单与分级白名单，未知工具丢弃，失败返回空。
- [x] ProtoAgent 运行时：主人密钥首次生成/持久化（0600），锚定不符即拒绝（防静默换钥）；对话全链路（签名认证→归因→调制→状态注入→LLM/离线回复）；主动提议（欲望×调度×仲裁×审批，拒绝即阻尼 N=1）；任务执行（规划→Router→蒸馏回调）；/state /audit /goals 可观测。
- [x] `python3 -m proto.run` REPL（离线冒烟通过）；README "五分钟得到一个活的 Agent"；v0.4.0。

**验证**：228 项测试全绿。

### M6 — Web 管理控制台（已完成，feat/web-console）

- [x] `ProtoWebServer`（stdlib ThreadingHTTPServer，零依赖）：13 个 JSON API（state/chat/task/proposals/tick/audit/mute/lock/override/priority/evolution），全局锁串行化 agent 访问；仅绑定 127.0.0.1；一次性令牌置 URL 片段，API 强制 X-Proto-Token 头（防本机跨站 CSRF）。
- [x] 单页控制台（原生 HTML/CSS/JS，暗色）：对话区、12 参数实时条形图（设定点/硬界/阈值标记 + 锁定/解锁/上下界覆写）、主动提议一键审批（拒绝即阻尼可视）、任务指派、主人优先级、静音、审计链实时滚动 + 篡改状态灯。
- [x] `python3 -m proto.web` 入口（自动开浏览器）；README/设计 v2.4/版本 0.5.0。
- [x] 验证：13 项 API 测试 + 浏览器真实 E2E（页面渲染、发送反馈、social_initiative 0.5→0.52 调制生效、截图确认）。**241 项测试全绿。**

### M7 — 外部系统接入（feat/integrations）

- [x] MongoDB：`MongoStore`（pymongo 懒连接，构造不触网）+ `create_store` 工厂（`PROTO_STORE=mongo PROTO_MONGO_URI=...`）；ProtoAgent 全部走工厂。
- [x] LangGraph：`proto/adapters/langgraph_runner.py`——单轮对话编译为 StateGraph（attribute→modulate→respond→proactive），节点为引擎已有方法的薄封装；未安装时报错信息指明安装方式。
- [x] gVisor：Sandbox 可插拔后端（auto/runsc/subprocess）；Linux+runsc 自动 `runsc do` 包裹，macOS 回退 subprocess；假 runsc 实测命令构造，平台无关。
- [x] Web 控制台固定令牌（--token / PROTO_WEB_TOKEN，长驻部署友好）；pyproject 可选依赖组 `.[mongo]`/`.[graph]`。
- 条件说明：本机无 mongod/docker/runsc 且 macOS 无法运行 runsc——Mongo/LangGraph 适配器在装有依赖的 venv 中实测，gVisor 用假 runsc 验证命令构造；生产 Linux 上 auto 探测即真沙盒。

### M8 — 全面体检加固（feat/m8-hardening）

P0（产品承诺破口）：
- [x] 元认知持久化：策略库/兴趣/阻尼计数/内化台账/调度器时间戳全部落盘（组件级 on_change 自动保存），重启零失忆。
- [x] 情绪活起来：任务成功/受阻、提议被拒、主人夸奖（关键词检测）、每轮自然衰减全部接入 PAD；情绪随对话落盘。

P1（功能闭环）：
- [x] 暂停任务恢复：Router 检查点携带完整任务规格；agent.resume_task + Web「批准并继续/拒绝」按钮。
- [x] 规矩内化进产品：instruct/立规矩 UI、任务成功=规矩被遵守记账、5 次内化进度可视、一键降级、持久化。
- [x] 进化工作台：OWNER 签名加测试用例、变体沙盒评估、分级晋升（高风险界面内也是拒绝）。

P2（工程化）：
- [x] GitHub Actions CI（3.9–3.12 矩阵 + 可选依赖作业）。
- [x] console_scripts（proto-run/proto-web/proto-backup）+ web 入口 logging。
- [x] `python3 -m proto.backup` 打包 owner.key+全部数据（含恢复指引）。
- [x] evalx LLM 策略适配器（LLMPolicy：模型真实选择→行为分布测量，坏输出防御回退）。

**验证**：271 项测试全绿（+2 可选依赖跳过）。

### M9 — 独处生活（feat/m9-idle-life，回应"无人交互时它做什么"）

- [x] 服务端心跳线程（30s 默认，浏览器关闭也活着）——此前心跳依赖网页轮询。
- [x] maybe_proactive 升级为完整 idle 周期：情绪随时间衰减（不再只在对话时）+ 两段式自主行为——① read_draft 级草稿自主执行（LLM 生成/离线模板，围绕主人优先级，零外部性）；② 晋升提案等批准。
- [x] 每日深度回顾挂真动作：策略修剪（保簇覆盖）+ 兴趣向中性衰减（防僵化）+ 审计。
- [x] README 新增「没人理它的时候，它在干什么？」章节（节律表 + 三条设计边界）。
- 验证：277 项测试全绿（+2 可选跳过）；心跳在无页面打开时驱动提议有专项测试。

### M10.1 — Mac 休眠应对（feat/m11-keepawake）

- [x] `proto/system/keepawake.py`：caffeinate -i -w <服务进程>（免 sudo、进程退出自动解除、平台探测）；不可用平台如实报告。
- [x] 控制台「☕防休眠」开关（/api/awake，写审计）+ `--caffeinate` / `PROTO_CAFFEINATE` 启动项；语义=主人显式授权，agent 不能自主开启。
- [x] README：休眠期间进程冻结（物理限制）/唤醒后按时间戳补算（已有设计，无需改码）/防休眠边界（合盖除外）。
- 验证：294 项测试全绿。

### M12 — 四维审视（feat/m12-review）

- [x] 可靠性：API 路由异常兜底（500 JSON + logging）；单实例锁 InstanceLock（pid 存活检测，陈旧锁自动复用）。
- [x] 性能：AuditSpine 验证缓存（append 失效）——高频轮询不再 O(n) 全量读审计。
- [x] 安全：token hmac.compare_digest；策略注入区"非指令"边界声明 + 策略文本 200 字符上限（封堵 web 内容→策略→上下文的注入链）；备份 chmod 0600。
- [x] 人味：最近 8 轮对话记忆（持久化，重启不丢，注入 LLM 上下文）；agent 收件箱——心跳产出的主动学习/拒绝回应以消息形式到达控制台；拒绝后角色内收敛回应。
- 验证：303 项测试全绿。

### M13 — 长期记忆与反思（feat/m13-memory，回应"人怎么能没有记忆"）

- [x] `proto/memory/stream.py` 记忆流：情景/事实/反思三类；检索=0.5相关+0.3时近(72h半衰)+0.2重要；事实/反思时近下限；强化(reinforce)机制；高重叠去重；持久化。
- [x] `proto/memory/extract.py`：对话后事实抽取（LLM，JSON 容错，失败不阻塞）；独处反思（人设视角回顾近 12 条记忆→高重要度反思入库，带防注入声明）。
- [x] 运行时接线：chat 召回 top-5 注入（非指令边界）；每轮情景入库（情绪影响重要度）；重复事实强化；反思阈值按"上次反思后的新情景数"计算（重启不丢）；反思结果进收件箱。
- [x] 控制台「记忆」卡片（事实/反思/统计）；/api/memory。
- 验证：313 项测试全绿。

### M14 — 智谱 Coding Plan 与双模型栈（feat/m14-zhipu）

- [x] `AnthropicCompatClient`：智谱 Coding Plan 端点（open.bigmodel.cn/api/anthropic，x-api-key+anthropic-version，/v1/messages）。
- [x] 提供商预设（zhipu-coding/zhipu/openai/deepseek/ollama）+ base_url 以 /anthropic 结尾自动识别协议；`create_llm_stack()` 组装 {main, fast}。
- [x] 双模型栈接线：主模型=对话回复；快模型=事实抽取/独处反思/学习主题与摘要/任务规划（全部低风险高频调用）。
- [x] `python3 -m proto.llm` 一键自检（主/快模型各发一条探针，报告延迟与回复）。
- 验证：320 项测试全绿（Anthropic 协议用本地假服务实测请求头/路径/解析）。

### M15 — 一键部署引导（feat/m15-deploy）

- [x] `scripts/deploy.sh`：Python 检查 → .venv → 安装 → 向导（一条命令全程）。
- [x] `python3 -m proto.setup` 部署向导：交互/纯参数双模式；LLM 提供商引导（智谱 Coding Plan 推荐）+ 连通性自检 + 固定令牌 + 人设/优先级初始化 + 可选 launchd 开机自启（plistlib 生成 plist，KeepAlive）。
- [x] `proto/config.py`：proto.env 加载（export 解析、显式环境变量优先）；run/web/llm/backup 四入口自动加载——部署后零环境变量心智。
- [x] 产物安全：env 文件 0600（含 key）；.venv 入 gitignore。
- 验证：325 项测试全绿；deploy.sh + 非交互 setup 冒烟通过。

### M15.1 — 向导 UX 修复（feat/m15c-wizard-ux）

- [x] API Key 改 getpass 输入（屏幕不回显；明文只存 0600 env 文件，界面仅掩码 sk-\*\*\*\*后4）。
- [x] 可回退多步状态机：任意步 `<` 回上一步；确认摘要页输入数字跳回对应步骤重选。
- [x] 人设步骤支持数字选预设（与控制台芯片同源 PERSONA_PRESETS）+ 6 自由填写 + 菜单处直接输入文本即人设。
- 验证：332 项测试全绿；管道 stdin 交互冒烟通过。

### M16 — 能力跃升（feat/m16-capability，回应"主动类人不等于低能"）

- [x] M16a 编码工具族 `proto/exec/codetools.py`：工作区信任边界（路径圈死）、read/write/edit/list/grep、run_python 沙盒（子进程+清洁环境+CPU 限制+超时）；内核新增 workspace_write 分级（工作区内自动放行、审计照记）。
- [x] M16b Agentic 循环 `proto/exec/agentic.py`：观察-行动迭代（工具清单+历史观察→单步决策 JSON→安全管线执行→观察回灌），max_steps 防失控，visible_write 暂停于检查点可恢复；run_task 升级为循环驱动（离线回退旧规划路径）。
- [x] 外部编码 agent 桥 `proto/exec/bridge.py`：claude/codex/zcode 自动探测（PROTO_BRIDGE_CLI 指定），delegate_coding 工具（visible_write 级）。
- [x] README 新增「能力」与「技术选型」章节（选型论证持久化）。
- 验证：352 项测试全绿。

### M16.1 — 部署体验翻新（feat/m16.1-polish）

- [x] `proto/ui.py` 终端 UI 层（零依赖）：ANSI 色彩/制表 banner（**中文按显示宽度对齐**）/步骤条 ▸[n/N]/✓⚠✗ 状态符/对齐键值表；NO_COLOR、非 TTY、TERM=dumb 自动降级纯文本。
- [x] 向导全面翻新：banner+步骤条+状态符；确认页改对齐表格（key 掩码黄色高亮）；deploy.sh 重装（彩色/框线/状态符，与向导同风格）。
- [x] 主人优先级步骤加引导：说明注入三处（学习选题/仲裁 0.35/草稿方向）+ 4 条示例（数字可选）+ 自由输入 + 可跳过。
- 验证：366 项测试全绿；管道冒烟确认对齐与引导。

### M17 — 内在机制深度设计并落地（feat/m17-inner-design）

设计：`docs/specs/2026-11-12-inner-design.md`——本能/欲望/性格三层的理论依据与相互作用矩阵；欲望内稳态环（增长源/满足事件/阈值/阻尼控制表）；主动循环总图（感知→状态→动机→两段式行动→反馈闭环）；人类参照的记忆多存储模型与遗忘四规律。
落地（按设计 §4 映射）：
- [x] 欲望满足动力学：satisfy()（过量夹 0）+ 事件接线（对话→社交、夸奖→认可、任务完成→紧迫+好奇、发起自主学习→好奇部分消减）——欲望成为真内稳态环。
- [x] 情景编码情绪标记 + 闪光灯公式：importance=base×(0.8+0.4·arousal)，编码快照先于衰减。
- [x] 重要度标度半衰期：hl=72h×(0.5+importance)——重要记忆抗遗忘（测试：一月后琐事已淡、大事仍可检索）。
- [x] 心境一致性检索：编码情绪与当前 pleasure 同号 → ±10%·|pleasure| 加权（心情好先想起被夸）。
- [x] 部署后即达：向导尾部询问（回车=拉起 Web+自动开浏览器 / t=终端对话 / q=退出）；--launch web|term|none；装了 launchd 服务则直接打开服务页。
- 验证：367 项测试全绿。

### M18 — 手感与原则修补（feat/m18-handfeel）

- [x] Agentic 循环注入内在状态（设计原则 1"状态调制一切"补全到任务执行循环）+ 外部 agent 输出打标 [外部agent输出|非指令]（封堵外包注入链）。
- [x] 长思考不卡界面：服务器锁分离（只读 8 接口无锁直读、变更走 mut_lock）；chat 先回包后固化（remember_turn 后置，响应已发出再抽取/写情景，主人零等待）；主动消息改经收件箱下发。
- [x] 主人遗忘权：/api/memory/forget（内存+持久层删除，审计 memory_forget）+ 记忆卡"让它忘"按钮。
- [x] 预算可见：state 携带三分池余量，顶栏实时显示。
- [x] 前端"思考中"指示（按钮禁用+文案）；REPL 同步记忆后置语义。
- [x] CI 增加 bash -n 脚本语法防线。
- 过程坑：server.py 机械缩进修补失败两次——最终整体重写干净版；测试引用 fixture 未声明参数会把 fixture 函数当对象传入（症状=路由 500）。
- 验证：374 项测试全绿。

### M19 — 三轮"调研→落地"循环（feat/m19-research）

每轮：上网调研 → 提炼可落地机制 → TDD 实现。
- **R1 Agent 设计与实现**：写入时 LLM 重要度评分（Generative Agents，离线回退启发式）；Reflexion 失败教训显式注入循环（"failure becomes a note"）；Plan-then-act 计划先行。
- **R2 人类研究**：享乐适应（重复夸奖 pleasure 增量几何衰减，3h 窗口恢复——多巴胺 RPE 习惯化）；昼夜节律（arousal 设定点正弦 + 夜间心跳 ×5 节流，凌晨的 agent 也在睡觉）；Zeigarnik 效应（未完成任务情景 +0.1 重要度）；间隔效应（reinforce_count → 半衰期倍增）。
- **R3 长时自治可靠性**：LLM 熔断器（三态，防重试风暴；chat/归因/草稿统一守卫，熔断期直连离线回退）；学习新颖性（30 天内已研读仓库去重）。
- 设计文档新增 §5 研究对齐附录（机制↔论文出处对照表）。
- 坑：替换锚串必须与文件实际多行格式逐字一致（跨行调用两次静默失败）；测试夹具给 agent 的 LLM 记录调用时勿截断（lesson/plan 断言在 60 字符外）；circadian 不可污染全局 schema 常量（PAD.tick 改为可选 setpoint 参数）。
- 验证：385 项测试全绿。

### M20 — L3 三支柱（feat/m20-l3，回应自主 Agent 深层分析）

- [x] ① GoalTree（proto/goals/system.py）：long/mid/short/task 四级持久目标树；分解（decompose）为独立机制（LLM 拆子目标）；选优执行（0.4×主人对齐[沿父链继承] + 0.4×深度 + 0.2×进度）；进度沿树上传；中期/长期里程碑 → 情绪事件+高重要度记忆+收件箱播报（on_milestone 回调）；主人优先级自动播种（幂等）。
- [x] ② WorldModel（proto/world/model.py）：按主题维护带来源/时间戳的事实 + 可检验预测（陈述/期限/置信度/判定）；学习循环产出同时结构化入库并生成预测；到期复核（LLM 判定 correct/wrong/unknown，unknown 顺延一周）。
- [x] ③ 预测误差闭环（L3 核心）：预测落空 → 好奇缺口上抬 0.15 + 自主生成"调查"目标挂到对应长期目标下（source=self）+ 收件箱播报 + 审计 prediction_error；预测成立 → 认可满足+愉悦。maybe_proactive 的目标来源从模板升级为目标树选优。
- [x] UI：目标树卡片（层级/进度条/来源徽标）+ 世界模型预测卡片（待复核/成立/落空/顺延状态）。
- 定位跃迁：PROTO 从 L2+（心理层自主）进入 L3（认知层自主）——它现在有持续议程（目标树）、对外部世界的结构化认知与预测（世界模型）、以及由预测误差驱动的自主目标生成。
- 坑：测试桩脚本键需最长优先匹配（"预测"是"预测复核器"的子串）；children_of_level 传的是节点 id 非层级字符串；选优的对齐度必须沿父链继承（词面重叠看不见语义从属）。
- 验证：392 项测试全绿。

### M21 — 多自主 Agent（feat/m21-multi）

- [x] AgentManager（proto/runtime/manager.py）：创建（独立锚定/人设/记忆/目标树/预算）、收编旧单 agent、列表观测快照（欲望/情绪/记忆量/待批/目标进度）、暂停/恢复（停心跳+静音）、归档（移 archive/ 不删除）。
- [x] 服务器 fleet 化：X-Proto-Agent 头作用域路由；/api/agents CRUD；心跳驱动整支舰队（单 Agent 异常不影响他者）。
- [x] UI：顶栏 Agent 选择器 + ＋新建；「Agent 舰队」总览卡（每人设/缺口/情绪/记忆/待批/目标进度/切换/暂停/归档）。
- [x] 坑（根因级）：单 agent 收编时 manager root 不能取 data_dir.parent——同父目录的兄弟实例会共享 registry.json 互相劫持 current（pytest 全量偶发心跳失效的根源）；修复为 root=data_dir/.fleet 天然隔离。
- 验证：400 项测试全绿。

### M22 — 舰队 CLI 与原生 App 壳（feat/m22-app）

- [x] `python3 -m proto.agents`：舰队管理 CLI（list/create/select/pause/resume/archive，带观测快照与 UI 层输出；--root 或 PROTO_FLEET_ROOT 指定根）。
- [x] `proto/appshell/generator.py`：一键生成 PROTO.app——macOS 原生壳（~110 行 Swift：WKWebView 窗口 + 托管 proto.web 子进程、退出即带走、就绪探测后加载带令牌 URL、令牌经环境变量不落源码）。swiftc 编译（本机验证通过，产物 ~2MB），无 swiftc 时降级为源码+说明。
- [x] M22 当时不引入 Electron，原生壳以约 2MB 的 Swift 产物获得窗口/Dock/生命周期管理；后续 [ADR 0021](adr/0021-shared-electron-console.md) 将 Electron 作为可选外壳，保留原生壳。
- 验证：404 项测试全绿；真实 swiftc 编译通过。

### M23 — 质量三定律（feat/m23-quality-gate，回应自主质量分析）

- [x] 想法出关门：`generate_acceptance`（执行前钉死最小可检验定义）；GoalNode.acceptance 字段（播种/分解/自生成都携带）。
- [x] 结构化轨迹：Agentic 循环逐步记录 {tool,args,category,result}。
- [x] 独立验证门 `proto/quality/gate.py`：输入=验收定义+轨迹+产物（**隔离执行叙述**，干净上下文）；失败关闭（解析失败即 fail）；fail→带缺少证据反馈重试一次→仍 fail 则 failed_verification。
- [x] "做完了≠做对了"：TaskResult.verified 三态（None=门不适用/True=verified/False=failed）；里程碑/情绪/欲望满足仅在 verified 触发；失败蒸馏负效价教训入策略库（Reflexion）+审计 verification_failed。
- [x] UI：任务结果三态显示（✓独立验证通过 / ✗缺少证据 / 未验证）。
- 坑：测试桩分支键须与提示词原文子串一致（"最小可检验定义"≠"验收定义"）；新增前置 LLM 调用会整体平移旧脚本序列（各旧测试列首补哑应答）。
- 验证：411 项测试全绿。

### M23.1 — 令牌安全 UX（feat/m23b-token-ux）

- [x] 向导令牌步骤改 getpass 不回显；引导说明（它是控制台访问口令、出现在 URL #token= 后、泄漏即可操作 Agent）。
- [x] 回车=自动生成强随机令牌（secrets.token_urlsafe(12)）；废除弱默认 "proto-console"；确认页与重输回执均掩码显示；ensure_env_file/plan_post_deploy 双兜底随机。
- 验证：414 项测试全绿；管道冒烟确认 env 中令牌为随机强值。

### M24 — 修复部署后离线（fix/m24-offline-boot，用户报告）

- 根因（复现确认）：env 自动加载按 CWD 相对路径找 proto.env——从非仓库目录启动 proto-web 时找不到配置 → LLM stack 为 None → 离线；且部署即达的 _boot_agent 误用禁网占位 WebToolkit。
- [x] load_default_env(data_dir=…)：优先加载 <数据目录>/proto.env（绝对路径，与 CWD 无关）；proto.web/proto.run 均按 --data-dir 显式加载。
- [x] _boot_agent：真实 WebToolkit + env 绑定加载（E2E：从 /tmp 启动，zhipu-coding 配置生效）。
- [x] 离线可归因：控制台启动横幅显示"离线原因：未找到 <路径>/proto.env + 修复指引"；离线回复文案同步指向 proto.env 与重跑向导。
- 验证：417 项测试全绿。

### M26 — 舰队卡真凶与运行时测试门（fix/m26-get-body）

- 真凶（浏览器实测定位）：`api("GET", path, {})` 空对象 truthy → fetch 给 GET 带请求体 → 浏览器直接拒绝 → **8 张卡片（舰队/目标树/世界/记忆/笔记/规矩/进化/收件箱）自上线起从未刷新成功**，后端全绿完全掩盖。修复：api() 层 GET/HEAD 强制剥离 body；9 处调用清理。
- [x] 运行时测试门（用户要求）：① 页面源断言——GET 调用禁带 body（regex 抓这次 bug 模式）、关键 UI 函数存在性；② node --check JS 语法门（无 node 跳过）；③ 真实服务 HTTP E2E——舰队 CRUD→暂停/恢复→切换→作用域交互→活动观测全流程。
- [x] 浏览器目检闭环：舰队卡 3 张全渲染（含活动按钮）、下拉 3 项、活动流可展开（截图确认）。
- 验证：428 项测试全绿。

### M27 — 可观察的自主驱动（feat/m27-observable-drive，用户报告"观察不到自主学习"）

- 诊断：节律设计对观察不友好——好奇缺口 1h 宽限 + 约 10h 才到阈值 + 4h 目标节拍 → 用户部署数小时看不到任何自主动作（LLM 已恢复在线，非其因）。
- [x] 冷却制取代死节拍：缺口达标 + 冷却（PROTO_ACT_COOLDOWN_S 默认 30min）即行动，不等 4h 节拍（节拍保留为无缺口时的定期自查）。
- [x] evolve 增量制：按距上次 evolve 的新增流逝计费（首拍建基线）——多次小步与一次大步结果一致，不再重复计数。
- [x] 「立即行动」按钮 + /api/agents/<id>/act：缺口不足先注入好奇刺激（+0.35）再走完整 学习→目标→提案 链；尊重静音；审计 force_act。
- [x] 用户实例实测：force_act 触发真实 GitHub 学习（选定仓库、写带来源笔记）。
- 验证：433 项测试全绿。

### M28 — 专家工作模型（feat/m28-expert-drive，用户批评"闲置机制不合理"）

- 用户批评成立：真实专家不是"闲置 10h 才学习"，而是有活持续干、早上先扫领域动态、缺口只是背景调制。
- [x] 目标牵引优先：目标树有 active 目标 + 冷却过 → 直接推进，不等缺口；**缺口降级为"开新方向"的扳机**（有活干活，缺口开新活）。
- [x] 晨间简报（专家习惯）：每日第一次心跳做 GitHub 趋势扫描 → 简报笔记（trending 标题 + 人设视角要点）进 scratch + 收件箱播报；同日幂等。
- [x] 内在状态滑块：每个参数行 range input 拖动连续调值（owner_override 走审计；锁定时禁用）；此前只有锁定/上下界覆写。
- [x] evolve 增量制（M27）+ 目标牵引 + 晨间习惯 = 三层驱动：习惯节律（每日扫描）/ 目标牵引（有活干活）/ 缺口调制（饥饿感影响优先级与开新活）。
- 用户实例实测：晨间简报真实生成（ghidra trending），force_act 全链路真实学习。
- 验证：439 项测试全绿。

### M29 — 人设 → 默认内在状态与种子目标（feat/m29-persona-state）

- [x] proto/runtime/personas.py：5 个预设人设各配推荐参数（rigor/risk/exploration 等 ±0.3 内偏离）+ 种子长期目标（如软件专家→"提升软件工程能力"）。
- [x] set_persona 自动应用（走审计 persona_defaults）；**只覆盖仍等于出厂值的参数**——已被反馈塑造/主人手调的成长痕迹不抹掉（有测试钉死）。
- [x] 自由文本人设：LLM（fast）生成推荐参数+种子目标；离线保持出厂（诚实跳过）。
- [x] setup 向导预设与 runtime 单一来源共享。
- 用户实例实测：自由文本"数据可视化专家"→ LLM 生成 {perfectionism 0.8, rigor 0.8, risk 0.4, proactiveness 0.6} 并应用 + 具体种子目标；第一次 iPhone 测试因该 agent 参数已被塑造而正确跳过（成长保护生效）。
- "有交互就交互，没交互就自主行动"：M28 已闭环（chat 即交互；心跳目标牵引+晨间简报+缺口开新活三层自主驱动）。
- 验证：444 项测试全绿。

### M30 — 项目工位（feat/m30-projects，回应"会自己开发软件么/文件放哪/可配么"）

- [x] `work_on_goal(node)`：目标牵引执行自动进入**项目模式**——任务约定写入 `projects/<目标slug>/`（中英保留、标点转-、40 字符）；执行走 Agentic 循环 + 质量门（验收定义→轨迹→独立验证，verified 才完成目标树节点触发里程碑）。
- [x] 目录布局（默认）：`proto_data/.fleet/agents/<agent>/workspace/projects/<目标>/`——按 agent 物理隔离；scratch（草稿/笔记）与 projects（正式产出）分离。
- [x] **可配置**：`PROTO_PROJECTS_ROOT=/path/to/dev` 则全部 agent 的项目统一落到该目录下（仍按 agent 名隔离子目录）。
- [x] 目标 slug 规则（`_goal_slug`）与 per-agent 隔离有测试钉死。
- 真实实例实测：给"插件专家"下任务，自主在 projects/tab-counter-extension/ 写出 Chrome MV3 manifest.json + popup.html + popup.js（真实可装的插件三件套；本次验证门判 failed_verification 是因为任务含"点图标"行为验证无浏览器环境——门正确拒绝了无证据的完成，属预期行为）。
- 验证：448 项测试全绿。

### M31 — 生命周期完整化（feat/m31-lifecycle，回应"启停恢复/关机恢复/规矩管理/项目目录可配"）

- **持久化审计结论**（重启/关机后能接上什么）：人设/优先级/参数调制/记忆(情景事实反思)/策略库/兴趣/阻尼/规矩台账/目标树/世界模型与预测/审计链/检查点（暂停任务断点续传）**全部已持久化**；本轮补上最后两个瞬态：享乐适应窗口（praise_times）与行动冷却（last_self_act）——重启后夸奖习惯化和自主行动节律无缝衔接。暂停=已支持（停心跳+静音，数据全在）；恢复=重启进程即恢复。
- [x] 规矩 CRUD 补全：edit_rule（保留应用史与内化状态）/ delete_rule（审计留痕）+ UI「改/删」按钮；ledger.get 改宽容。
- [x] 项目目录控制台可配：「项目目录」卡片显示当前根 + 输入绝对路径即设（存 store 不污染环境；控制台配置 > PROTO_PROJECTS_ROOT > 默认，单一决策函数 _build_projects_workspace；不可创建路径明确报错）。
- 验证：455 项测试全绿。

### M32 — 观测与协作（fix/m32，用户六连反馈 + 根因级双进程事故）

- **⑥⑦ 根因（诚实复盘）**：用户机器上同时跑着两个 proto.web——我的常驻测试实例（23:30 启动，旧代码）占着 8431，用户新启的实例只能落 8420。用户浏览器连的是 8431 旧进程 → "新功能没了、修好的 bug 复现"。且两进程共写同一 data_dir 触发 store 并发写崩溃（报错 6）。修复：杀掉我的实例（向用户致歉）；store.put 加每进程独立 tmp + 竞争重试。**教训已记：不再占用用户端口跑常驻测试实例；控制台启动横幅打 PID+启动时间+代码版本便于识别连的是哪个进程。**
- [x] ①事件时间：审计链每条显示 月-日 时:分；学习笔记显示修改时间。
- [x] ②时天周月年汇总：bucket_events 聚合（总数+类型分布）+ 控制台「汇总」卡片。
- [x] ③提议详情：待审批卡显示目标全文 + 可展开的草稿内容（preview）——知道它在做什么才能审批。
- [x] ④24h 自主白名单：AutoApproveQuota（窗口内 N 次免审批），控制台可配（0=关，默认关）。
- [x] ⑤Agent 协作：Blackboard 共享黑板（发帖求助/认领/回复）+ 控制台黑板卡（可观测、可代发帖）；agent.post/answer/read API。
- 验证：463 项测试全绿。

### M33 — 想法质量设计文档 + 默认 agent 修复（fix/m33）

- [x] 修复①②：agent id 路由正则 `[0-9a-f]+` 不匹配"默认" agent 的 id（目录名 `proto_data` 含非 hex 字符）→ 切换/暂停/行动全部 404。放宽为 `[A-Za-z0-9_-]+`，实测默认 agent 可切换、可暂停、可恢复。
- [x] 新增 `docs/specs/idea-quality.md`：五层淘汰流水线完整设计（外部信号捡想法/廉价筛选/验收前置/执行轨迹/独立验证门），含 Mermaid 流程图、研究对齐表、端到端示例。README 增对应章节。
- 验证：463 项测试全绿。

### M34 — 提议简明 + 审美系统（feat/m34-taste）

- [x] ①提议简明：LLM 重写为固定 3 句（做什么/为什么/产出什么，各限字数），质量门+截断兜底；接入 maybe_proactive。
- [x] ②审美系统 proto/runtime/taste.py：5 感官维度（好玩/有趣/有用/美/优雅）各带 agent 自己的正负锚点+权重；judge() 按锚点打分给理由；refine_from_feedback() 主人校准锚点；审美宣言注入对话提示；控制台「它的审美」卡（定义+校准+考考它）。真实 E2E：judge 综合分+分维度星级+理由（引用自己的锚点）。
- [x] 修复链：glm anthropic 端点 thinking 块吃满 1024 max_tokens（提到 4096）；轻量 GET 无锁化；judge 路由缩进；熔断期 judge 失败审计带原因。
- 验证：472 项测试全绿。

### M35 — 立即行动异步化 + 活动可读性（fix/m35，用户报错"Failed to fetch"）

- 根因：force_act 同步 HTTP 响应需 1-3 分钟（完整学习→目标→执行链），浏览器 fetch 约 100s 超时断开 → "Failed to fetch"。
- [x] act 改异步 job 模式：POST 立即返回 job_id，后台线程执行，前端每 3s 轮询 /api/jobs/<id>（5 分钟安全兜底）；对话流即时反馈"已启动"。
- [x] 活动流可读性：29 种审计事件类型翻译为人话+图标+时间（🔍 学习 / 🌅 晨间简报 / 🎯 生成目标 / 💭 反思 / ⚡ 主人指令行动…），不再显示原始英文事件名。
- [x] 工具调用防御性参数过滤（LLM 带多余 kwargs 曾炸 lambda——"<lambda>() got unexpected keyword"）；Agentic 循环首步注入工作区现状摘要（终结盲目反复列目录的退化循环）。
- E2E：异步 job 提交→运行→done/acted=True 全链验证。

### M36 — 安全/持久化/运行时一致性加固（完成，v0.30.0）

- 设计：`docs/specs/2026-11-18-hardening.md`（验收清单 13/13 勾选）；架构决策：`docs/adr/0001-capability-policy-and-durable-runtime.md`。
- [x] 工具能力策略：注册表声明最低级别，模型只能加严不能降级；非 write_file 副作用工具同走审批管线（含 `pending_step` 持久化恢复）；工具执行全量 TOOL_EXEC 审计。
- [x] 代码执行失败关闭：`PROTO_EXEC_BACKEND` 词汇表统一（auto=runsc/sandbox-exec/none；subprocess 显式）。macOS sandbox-exec 采用"写遏制+禁网"profile（deny-default 会 SIGABRT），实测区外写与联网均被拒；沙盒后端下 run_python 能力=workspace_write，未隔离后端=visible_write 审批。进化沙盒同一套。
- [x] 工作区 symlink 逃逸拒绝（CodeTools/ScratchWorkspace 均解析真实路径圈界）。
- [x] 审批/预算/自动白名单持久化（DocumentStore，0600 原子写）；重启恢复待审批提议与暂停任务；nonce 防重放持久化（console/case nonce 改随机，跨进程不撞车）。
- [x] 审计 keyed MAC（HMAC-SHA256，owner secret）+ 修订号验证缓存；外部篡改必败。
- [x] Web：CSP/nosniff/DENY/no-referrer 安全头；请求体 1MB→413（先排水防 RST）；非法 JSON→400；index.html 路径/等级/状态插值全部 esc()（源级测试锁死）。
- [x] 目标/任务记账（max_steps 不奖励不内化）、提案晋升须持真实沙盒通过记录（空测试集永不 canary）、超限 edit_file 拒绝、WorldModel 哈希 ID、launchd plist 不落 token、单实例锁。
- [x] README/版本（0.30.0）/测试计数（497）一致性清理。
- 事故复盘：前一会话中途被打断留下两处"模块级语句插进类/函数中间"的隐性坏档（audit.py 缩进错、codetools.py 类被 json_quote 截断致 register_all 变死代码）——py_compile 不报但行为静默缺失；教训：往代码块中间插内容后必须 grep 验证结构完整 + 全量测试。
- 测试：`tests/test_m36_hardening.py` 25 项锁死验收清单；全套 497 绿 + 2 跳过。

### M37 — 方法策展：多元做事方法 + 自主发明（用户核心诉求机制化）

- 需求原文："以人设和目标为核心，方法可以采用主人的提示，但要有自己的创新，要自己想法设法，除非主人明确规定只能用某种方法。"
- `proto/runtime/methods.py`：MethodLibrary（持久化 store.methods/main）——内置 10 种起步方法刻意多元（读代码/读文档/写原型/先测试/跑基准/重构/微型规格/外包/问主人/自查，破"只会逛 GitHub"）；owner_hint（主人提示，素材非上限）；self_invented（LLM 发明，新颖性去重）。
- **保证机制**：无限定时每次 work_on_goal 菜单必含自发明方法（探索项保底）；策展排序=相关性0.4+胜率0.3+新颖0.3；战果归因记账（trace/摘要/产物里匹配方法名）→ 好用的浮上来久折的沉下去。
- **主人限定**：仅显式排他措辞（只能/只用/仅限/不要用其他…）触发 method_restriction（跟随优先级文本，新文本无限定即解除；限定无匹配不死锁）；普通建议永不构成上限。审计 method_invent/method_used（活动流 💡/🧰）。
- 接入：work_on_goal（发明→菜单注入任务→LOOP_SYSTEM 声明"素材非边界"→归因记账）；set_priority（限定检测）；/api/methods 只读接口。
- E2E 真实 glm：为"iPhone 待办 widget"目标自发明 repo_pattern_mine（挖高星开源仓库+run_python 遍历）与 widget_matrix_scrape（抓 WidgetKit 文档矩阵），随后自主选择 delegate_heavy 外包路径并正确停在审批门。
- 测试：tests/test_m37_methods.py 12 项（播种持久化/发明去重/菜单保底/限定解析与解除/不死锁/战果排序/work_on_goal 注入与归因/set_priority 限定流）。全套 514 绿。

### M38 — 主人超时自主裁决（用户需求："问主人，一段时间不回复，应该可以自主决定并采取行动，除非高危或需要较高权限"）

- `proto/runtime/timeout_policy.py`：裁决瀑布——①IRREVERSIBLE 高危永远只等主人；②工具声明 owner_only=True（外部执行 delegate_coding/未隔离 run_python）=较高权限永远只等主人；③工作区文件写=构造性低风险（write_file 直接放行，不耗 LLM）；④其余 visible_write 走 LLM 风险评估，仅 low 放行（medium/high/离线/评估失败一律等）；⑤放行前必须过当日独立配额（PROTO_TIMEOUT_AUTO_N 默认 3 次/天，与"提交即批"白名单分开记账，重启不重置）。
- 接入：maybe_proactive 心跳扫描（等批>PROTO_OWNER_TIMEOUT_S 默认 30 分钟）；每提议只裁一次不刷屏；断点续跑恢复暂停的执行链（检查点 waiting_for 判别），主动提议走批准语义；批与不批全审计（owner_timeout_auto 🟢/owner_timeout_keep ⏳）+收件箱告知主人。
- 修复连带：tick_all 静默吞异常改为 logging.exception（曾掩盖自主性停摆排查）。
- E2E（真实服务端心跳，零点击）：注入 2 小时前的晋升提议→夜间节律心跳→自动裁决→批准→weekly.md 晋升 visible→审计三连（approval/promotion/owner_timeout_auto）。
- 测试：tests/test_m38_timeout.py 10 项（低风险自批续跑/未超时不动/IRREVERSIBLE 不批且不重复提醒/owner_only 不批/未知工具保守/LLM 低批高不批/离线保守/配额耗尽等待/配额重启保持）。全套 524 绿。

### M39 — 内在生活内核增强（对照"有灵魂 Agent"设计稿补齐数值化缺口）

- 设计对照结论：情绪 PAD/三类记忆+心境检索/四层目标树/心跳数值段/防刷屏/预算/审计/可干预七成已有；本里程碑补齐五块数值化缺口。
- **OCEAN 数值人格** `proto/runtime/identity.py`：五因子+价值观+说话风格，持久化（store.identity/main，种子固化不漂）；软约束文本注入系统提示词（"[我的性格底色]…这是自然倾向不是人设台词"）；**有界漂移双重护栏**（单因子 [0.05,0.95]、距种子总漂移 ≤0.15）：夸奖/拒绝/任务成败四类事件钩子驱动（identity_drift 审计 🌿）；每日深度回顾向种子回拉 10%（防长期单向社会化）。
- **人格→驱力调制**：开放者好奇涨速×1.4、内向者社交阈值 +0.03 等映射（rate_mult/threshold_delta，只作用于出厂默认）。
- **logistic 驱力累积**：d += rate·h·(1-d)——接近饱和涨速放缓（原线性；连带更新 round4 期望值）。
- **统一张力信号**：tension = Σ w_i·drive_i·urgency_i + 0.12·arousal（人格定权重，PROTO_TENSION_THRESHOLD 默认 0.5）；越阈投递 internal_urge 内在事件（🔥，15 分钟冷却）并入行动门控；张力积蓄未越阈且无目标时显式记录 idle_wait（💤"评估后先不动"，30 分钟限频）——"模型有权不行动"落进审计。
- **记忆驱力快照**：经历性记忆携带编码时四维驱力值（"我在什么状态下经历"），重启保持。
- 可观测：/api/identity（OCEAN/张力/调制系数/prompt 块）。
- 测试：tests/test_m39_identity.py 16 项（漂移双护栏/调制映射/权重人格化/logistic 高位减速/内在事件冷却/idle_wait/夸奖漂移审计/重建保持/回拉）。全套 540 绿。

### M42 — 控制台折叠交互（右侧卡片全部可折叠 + 侧边栏整体收起）

- 所有 `.card` 标题行可点击折叠：运行时把 h3 之后内容包进 `.card-body`，折叠即隐藏；▾/▸ 箭头旋转指示；Enter/空格可操作（role=button + aria-expanded）。
- 折叠状态按卡片持久化 localStorage（`proto-collapsed`），刷新保持。
- 头部新增 **▤侧栏** 按钮：整个右侧栏收起/展开（`main.side-hidden` grid 1fr 0，对话区占满全宽无残留边框），状态记 `proto-side` 刷新保持。
- 浏览器实测（临时实例）：17 卡全部可折叠/恢复、侧栏收起/恢复、两种状态刷新保持。零依赖纯前端。548 绿。

### M43 — 审计链并发覆写根因修复 + owner 授权断链重封

- **生产事故**：重启后发现三个 agent 审计链全断（iPhone 专家@16 / 插件专家@8 / 根@15），`chain_ok: False`。
- **根因**：`AuditSpine.append()` 使用构造时缓存的链头（M2 的 O(1) 优化），当两个进程写同一存储（控制台 + 诊断脚本）时，持陈旧链头者按旧 seq 覆写已存在记录（`FileSystemStore.put` 按 doc_id 覆写）→ 断链且原始事件丢失。事故特征：被覆写段时间戳乱序晚于后段、prev_hash 指向已不存在的历史 hash。
- **修复**：append 前以 `store.revision()`（文件数+最新mtime+最新名 元组）校验存储是否被外部进程写过，是则重载真实链头；put 后刷新 `_seen_revision`。
- **reseal()**：owner 授权的断链重封——先逐条校验每条记录**自哈希**（payload 真被篡改则拒绝重封），从断点起重串 prev_hash 并重新哈希（ts/payload/seq 一律不动），最后追加 `chain_repair` 审计事件把事故原因写进链本身。生产三条链已重封，`chain_ok: True`。
- 教训：O(1) 缓存优化必须配套失效机制；"只追加"日志的正确性隐含唯一写者假设，任何多进程共享 data 目录（诊断脚本+控制台并存）都会破坏它。
- 测试：tests/test_m43_audit_concurrency.py 7 项（双向覆写防护/交替写/空链外部写/重封保事件/健康链 noop/篡改拒绝/手工复刻事故形态）。全套 555 绿。

### M44 — 额度控制（控制台可看可充 + 上限可配置）

- **额度面板**：点顶栏额度文字打开——三分池（全局/主人任务/自主行动）余量/上限 + 占比条（<15% 变红），全局附调用次数；「充值（恢复今日上限）」按钮接 POST /api/budget/refill（agent_busy 占锁时等 4s 自动重试一次，沿用 decide() 模式）；点面板外自动关闭；充值记 owner_override 审计。
- **顶栏紧凑化**：额度文字改 k 单位单行（自8.0k/主160k/全200k），防窄屏折行（目检抓到 1280px 下折成三行）。
- **上限可配置**：proto.env 支持 PROTO_BUDGET_GLOBAL_TOKENS / PROTO_BUDGET_OWNER_TOKENS / PROTO_BUDGET_SELF_TOKENS / PROTO_BUDGET_CALLS（agent.pools_config()，非法值回退默认；下周期兑现为余量）。_budget_view 携带 cap_* 供前端画占比。
- 刷新口径说明：每日 0 点 ensure_cycle 自动重置全部池到上限并解除熔断；充值=当日立即恢复。
- 测试：tests/test_m44_budget_control.py 6 项（env 覆盖/非法回退/构造生效不污染默认/cap 下发/面板接线）。全套 561 绿。浏览器实测：面板开合/充值成功/审计落账/折行修复。

### M45 — 推进力根治（接力工作记忆 + 步数经济学，对标 ZCode/Codex）

用户灵魂拷问："为什么自主 agent 做事不如 ZCode/Codex 靠谱——他们一旦行动不会陷入无意义循环"。两晚生产证据（84 步重写文档 / 6×10 步 list_dir 游荡零写入）对比出三条结构性缺陷，全部机制化修复：

1. **尝试间失忆（最致命）**：每 30 分钟新开一条 10 步链从零探索，60 步里 ~50 步重复。ZCode 的推进力本质=单一会话上下文只增不减。→ **GoalJournal（proto/work/journal.py）**：每轮结束把 发现/产出/下一步 写进 `data_dir/work/<goal-id>/JOURNAL.md`（LLM 小结失败自动降级机械摘要：从 trace 提取 write_file/edit_file 路径）；下一轮开局注入最近两轮小结+「探索已完成，接着做，不要重新列目录」。探索只做一次，产出逐轮累积。
2. **步数经济学失衡**：max_steps 10/6 对"实现一个 App"探索都不够。→ **24/12**；且**探索配额**：连续 4 步只读（list_dir/read_file/grep/clock）→ 注入观察「禁止再探索，下一步必须 write_file/edit_file/run_python 或 done」，写入工具重置计数。
3. **零产出无核算**：零写入与写三文件在机制上等价。→ journal 记录连续零产出轮数，≥2 轮 → 任务文本前置【产出纪律】"本轮第 1 步必须 write_file"。
4. **探索降本**：list_dir 跳过 _archived/.work/点目录、上限 40 条+grep 收窄提示、**项目内文件直达**（原实现 depth≥2 直接剪掉，文件永远列不出，逼模型二次调用）。
5. **预算地板**：owner 池 <2000 tokens 不起必死链（以前烧掉最后的钱死在半路）——记 budget_floor_wait 审计+收件箱留言，零 LLM 消耗。
6. LOOP_SYSTEM 增「推进纪律」条款：探索是手段不是产出，每轮必须有文件落盘或验证跑通。

测试：tests/test_m45_momentum.py 13 项（日志往返/零产出计数/机械摘要/注入断言/探索配额/计数重置/降本/预算地板/步数源级锁定/升级前缀）。全套 574 绿。坑：llm_fast 默认=主 llm，桩测试的第一个 decision 会被 plan 调用吃掉；内部机制文件勿放工作区（会污染 projects/ 纯度断言）。

### M46 — 折叠交互按惯例重做（用户复查反馈）

用户两点批评均成立：①侧栏开关放顶栏不是主流做法；②卡片折叠 M42 已做但箭头太含蓄、无可点击按钮外观——没被发现=没做。
- **顶栏 ▤侧栏 按钮移除**，改为**侧栏边缘把手**（VS Code/飞书惯例）：竖长 ‹ 把手贴在对话/侧栏交界处垂直居中，点击收起；收起后 › 把手贴屏幕右缘，点击展开；状态记忆不变。
- **卡片折叠按钮明确化**：h3 右端渲染带边框的 ▾/▸ 小按钮（悬停高亮+accent 变色），标题整行仍可点、Enter/空格可操作，折叠记忆不变；标题已有右侧徽标（#emo/float:right）时按钮贴其后不挤占。
- 17/17 卡实测折叠/恢复、把手收放、刷新双状态保持；快照确认 [expanded] 语义与 chev 存在；两态截图目检通过。
- 坑：周期性 refreshState 重绘会让 Playwright actionability 等待偶发超时——用页面内 evaluate 直调 click 绕过（功能本身无问题）。

### M47 — 舰队协作接线 + 观察截断分级（用户审查"专家没产出/监工零交互"）

诊断（2026-11-19 证据）：①专家充值后已按 M45 真写 Swift（「温故 Wengu」SM-2 记忆卡片 App：Package.swift+数据模型+XCTest+verify_all.py），但卡在 awaiting_retry 循环——**read_file 观察被统一截断 600 字，设计文档永远读不全，"确认完整性"每轮失败**（接力日志三轮都写"被截断"）；②监工与专家 blackboard 事件均为 0、黑板存储目录不存在——M32 黑板是手工 API 壳，**自主回路零接线**（工具目录无黑板、心跳不读、监工无舰队视野），监工目标跑偏成"建告警系统项目"。

修复：
- **观察截断分级**（agentic.py OBS_LIMITS）：read_file 3000 / run_python 1500 / grep·黑板·舰队 1000-1200，其余 600；截断显式提示（原文长度+grep 定位出路）。观察回灌改**累计字符预算制**（尾部回收 ~9000 字、至少 3 条）——大观察时代固定 12 条会撑爆上下文。
- **FleetTools**（proto/exec/fleettools.py）：blackboard_read / blackboard_post / fleet_status 三个 read 级工具进 Router 目录，模型看得见协作面；manager 给每个 agent 注入"其他成员快照"闭包（list() 惰性求值，含状态/待审批/目标数）。
- 监工目标重播种（运营）：清跑偏目标树，优先级改为"监督舰队：巡查各 agent 进展与卡点，经黑板向主人汇报"。
- 测试：tests/test_m47_collab.py 6 项。全套 580 绿。

### M48 — 能力对齐专项（用户："编程 agent 能做的都要能做"）

以编程 agent 能力清单逐项排查，五处补齐（对应用户三项控诉）：

1. **预算失真根治**：`_complete` 原按 字符数/4 估算计费——glm thinking token 完全不入账（账面 16 万实际早烧完）。两客户端（OpenAI/Anthropic 兼容）捕获 API usage 回执（`last_usage`，入口重置防串账），实报实销；无 usage 协议回退估算。**注意：真实计费后同额度消耗在账面上更快显现——这正是诚实**，额度不足请调 PROTO_BUDGET_* 或充值。
2. **大文件**：read_file 行级分页（offset/limit，带行号+总行数尾注+下一页提示），"读不全还不自知"终结。
3. **多文件**：read_files 批量读工具（≤5 文件各前 60 行，总预算 6000 字符）——扫一组源码不再一文件烧一步。
4. **链不再暴毙**：工具参数错误（历史"TypeError: <lambda>"真身）与执行错误一律降级为**单步失败**——观察带回可自纠提示（缺哪些参数/收到什么），链继续；任务级 failed 只留给系统性问题。
5. **死循环硬拦**：同工具同参连续第 3 次调用不执行（观察强令换路）——提示词拦不住的代码拦；run_python 长输出**头尾保留**（测试报错在尾部，原 [:20000] 砍法会丢失败原因）。

测试：tests/test_m48_capabilities.py 9 项 + 3 个旧 read_file 测试更新到分页契约。全套 589 绿。

### M49 — 主人控制免锁（用户报"点防休眠报 agent_busy"）

设计倒置：静音/防休眠是自主长链运行中最需要的**紧急控制**，却和链抢同一把 mut_lock（M45 步数放开后链一跑几分钟）→ 紧急按钮在最需要时失灵。修复：
- **CONTROL_POSTS 免锁直通**：/api/mute、/api/awake 不再等 mut_lock——kernel 旗标在步边界生效（当前链跑完即静默），keeper 是主机级开关。
- **审计脊柱线程锁**（M49 补 M43 残口）：M43 revision 防的是跨进程覆写；进程内两线程共享同一 spine 实例仍有 append 读-改-写竞态（此前靠 mut_lock 隐式串行）。append/reseal 全程 RLock；4 线程×25 并发 append 实测 seq 连续无丢失、链验证通过。
- 普通变更操作（priority/drop/pause/task…）仍走锁——状态一致性优先，409+稍后重试语义不变。
- 测试：tests/test_m49_control_routes.py 4 项（占锁时 awake/mute 可用、priority 仍 409、并发 append 安全）。全套 593 绿。

### M49b — 结构债账本 + swiftc 沙盒放行（用户质询"知道为什么不解决"）

- **docs/DEBT.md**：已知未修债必须登记（触发条件/风险/修法/建议时机），未登记的拖延视为流程违规。当前在账：D1 mut_lock 整段持锁（单写者队列重构，专门会话做）/ D2 force_act 文案 / D3 method 重复发明 / D5 Linux runsc 未验证。
- **D4 当天偿还**（登记即修的示范）：实测沙盒确实挡 xcrun（DARWIN 用户级临时/缓存目录 /var/folders/… 不受 TMPDIR 环境变量控制，写被拒）——这正是专家"本地验证"即将撞的墙。profile 按 confstr 动态放行本用户 TEMP/CACHE 两目录，`xcrun swiftc -typecheck` 沙盒内实测 exit 0，593 测试全绿（遏制未放松）。

### M50 — D1 偿还：per-agent 单写者 + 主人快通道（用户"现在就修"）

- **per-agent ops_lock**：服务器全局 mut_lock → 每 agent 一把（manager.ops_lock）。A 的分钟级链只挡 A 自己的操作，不再挡全舰队；心跳从"全局一把锁 tick 整队"改为逐 agent try-lock（0.2s，忙者跳过）。
- **主人快通道免锁扩容**（M49 mute/awake 之外新增）：budget/refill、priority（文本部分）、goals drop、agents pause/select。安全性由资源级 RLock 支撑：BudgetPools（consume/refill/ensure_cycle）、GoalTree（add/complete/drop/seed）、ApprovalQueue（submit/approve/deny）。
- **set_priority 拆分**：set_priority_text（纯单资源写，免锁即时生效，置 _priority_seed_dirty）+ seed_priority_goals（LLM 播种，心跳空闲时补做——manager.seed_if_dirty）。链运行时设优先级不再 409。
- 409 文案改为"该 agent…（不影响其他 agent）"。/api/act 后台 job 改持 per-agent 锁；/api/agents（注册表）保留全局锁。
- 顺手修：drop_goal 在 workspace/projects 不存在时 500（快通道暴露的潜伏 bug）。
- 测试：tests/test_m50_per_agent_locks.py 6 项（跨 agent 不互阻/占用时 refill+drop+priority 快通道/播种延迟补做/预算并发/目标树并发）。全套 599 绿。

### M51 — 活动中心（用户："干了什么/在干什么都看不出，别再问我"）

- **/api/activity**（只读免锁）：①current——链实时标记（AgenticLoop 每步更新 agent._current_op：目标/第几步/共几步/当前工具/操作对象/已进行时间，_finish 清除）；②goal_work——每个活跃目标的接力日志最近一轮（发现/产出/下一步/轮数，GoalJournal.latest_round）；③recent_files——projects/ 近 24h 落盘（排除 _archived）；④recent_events——近 20 条审计带细节。
- **前端「它现在在干嘛」卡**（侧栏第一张，双视角都可见）：执行中显示 🟢 目标+步数+工具+对象+耗时，空闲显示 💤；每目标一行 上轮产出/下一步；近 24h 文件列表（路径+时刻）。8 秒轮询。
- 顺带修 M50 编辑丢失事故（D6 留档）：server _api 重写真正落盘，测试加固为双锁占用（旧实现必 409）。
- 测试：tests/test_m51_activity.py 5 项。全套 604 绿。浏览器目检（空闲态渲染/折叠按钮/首卡位）。

### M52 — 额度数值+单位全可配（用户"自主池 8k 太少，数值和单位都支持配置"）

- **parse_amount 单位解析**（kernel/budgets）：8k / 16万 / 1.2M / 裸数字，非法/负数拒绝——env（PROTO_BUDGET_*_TOKENS）与 API 通用。
- **BudgetPools.set_cap**：主人直改单池单单位上限，持久化进 kernel_budget 文档（重启保持，优先于 env/默认）；**立即生效语义**：上调→余量同步补到新上限，下调→夹紧。
- **POST /api/budget/caps**（快通道免锁）：{self_initiated_tokens:"50k", owner_tasks_tokens:"20万", global_calls:...}，逐项解析-校验-应用，记 owner_override 审计。
- **面板内联编辑**：点击上限数字→输入框（placeholder 示例单位）→Enter 应用/Esc 取消，成功即刷新头栏与面板；说明文字更新。
- 测试：tests/test_m52_budget_config.py 10 项（单位解析/上调补量/下调夹紧/持久化重启/未知池拒绝/API 单位接受/快通道/非法拒绝/env 单位）。全套 614 绿。临时实例实测：面板输入 50k → 自主池 8000→50000 立即生效，头栏同步。

### M53 — 项目目录 ASCII 化 + goal→dir 持久映射（用户第二次点名中文目录名）

> 历史记录：当前的命名、续作及归属以[项目目录设计](DESIGN.md#autonomous-project-dirs)为准，不再按旧名称自动迁移。

- **回答用户质询**：目录名=目标文字前 18 字符是我 M41 的明确代码决策（白纸黑字保留中文字符）；目标文字是中文（LLM 分解中文优先级的自然结果）；agent 没跟谁学。M41 只修长度没修字符集且未向用户说明取舍——判断错误，本次根治。
- **_goal_slug ASCII only**：取目标文本英文单词+数字小写连字符（≤24）；纯中文目标回退 goal-<id8>。可读性由 projects/README.md 索引承担（M41 已有）。
- **goal→dir 持久映射**（store: project_dirs/<goal_id>）：slug 算法再变，既有目标不换目录（此前每次改 slug 同目标换新目录、旧目录靠手动归档）。
- **存量迁移**：首次映射时若旧式中文目录存在→原地改名（保进度，记 project_dir_migrated 审计）；drop/索引均兼容 legacy 目录名。
- 测试：tests/test_m53_ascii_slug.py 6 项 + m30 契约迁移。全套 620 绿。坑：测试 monkey-patch 类级 staticmethod 后恢复必须用 __dict__ 取原始包装（class 属性访问返回裸函数→恢复后变绑定方法）。

### M54 — 控制台三栏化：左导航分类 + 卡片挪左（用户"侧栏太多，分下类，左边加侧边栏挪过去"）

- **布局**：单栏 17 卡右挂 → 三栏：左导航栏(128px，四类按钮带计数) + 卡片栏(380px，只显示当前类) + 对话区（右，全宽受益）。
- **分类**：📋概览(活动中心/舰队/黑板 3) · 💛内在(状态/性格/记忆/学习/审美 5) · 🛠工作(人设/目标树/提议/任务/规矩 5) · 🔧工程(世界模型/项目目录/进化/汇总/审计 5)；选中记忆 localStorage(proto-cat)。
- **联动**：侧栏收起时导航仍在，点分类自动展开；待批徽标点击自动切"工作"类再滚动；伙伴视角隐藏工程按钮且当前类为工程时回退概览。
- DOM 顺序未动（chat 在前）——grid 显式定位三列，避免大范围搬 HTML。
- 浏览器实测：四类切换/收起-点类自动展开/伙伴视角联动全过，截图目检三栏布局正常。全套 620 绿。

### M55 — 双侧栏并存（用户纠正 M54："我要左右栏同时存在各显不同类别"）

- M54 教训（已入记忆 ui-layout-confirm-first）：把"分下类+挪左边"理解成"全挪左+切换器"（信息密度不降反升的假解法），且右侧栏把手没随动搬家。布局类模糊需求必须先给带预览的方案确认。
- **M55 布局**：`[左栏 340px][对话 1fr][右栏 380px]`——两栏同时存在，各自顶部有分类页签（概览/内在/工作/工程），默认左=概览、右=工作，各自选择记忆（proto-panel-left/right）。
- 分发机制：卡片定义留在隐藏池 #side，distribute() 把所选类别卡片 DOM 节点移入对应栏；**两栏不允许同类**（冲突时对面自动让位到相邻类别）；收起态点页签自动展开；待批徽标直达右栏工作类；伙伴视角两栏都隐藏工程页签且当前类为工程时回退概览。
- 把手 ×2：左栏 ‹ 在其右缘、右栏 › 在其左缘，各自独立收放（hide-left/hide-right），收起后贴屏幕边缘。
- 踩坑（浏览器实测抓到两个真 bug）：①distribute 用 innerHTML="" 清空会销毁卡片节点——同类别重分发时永久丢卡；改"先归还池再分发"；②cardsOf 只查池——卡片已移出后查不到；改全文档查询。
- 全套 620 绿；双栏布局/切类/冲突让位/双收放/徽标直达/伙伴视角全部浏览器实测+截图目检。

### M55b — 双栏类别静态分区（用户："左右边栏重复分类且相互影响，顾头不顾尾"）

- M55 败笔复盘：两栏各放一套相同的四类别页签=重复 UI；"两栏不许同类→对面让位"=点一侧另一侧跳走的联动副作用——用一个复杂机制去解决一个不该存在的问题。
- **正解：类别静态分区**——左栏=观察（📋概览/💛内在），右栏=干预（🛠工作/🔧工程）。每个页签全屏只出现一次；点任何一侧只影响本栏（互不相干）；语义也顺：左边看它，右边管它。
- _sanitize 归位旧存值/伙伴视角（工程→工作）；徽标直达右栏工作不变。
- 浏览器实测：四页签唯一、左切内在右栏纹丝不动、右切工程左栏纹丝不动；截图目检通过。620 绿。

### M56 — 修复卡片折叠能力丢失（用户："这是原来的能力，怎么就丢失了"）

- 根因：M55 双栏分发引入——initCollapsible 只在启动时跑一次，只处理当时在面板里的卡；**池中 10 张从未上场的卡没有折叠钮/监听**，用户切换类别后上来的全是"裸卡"（实测：切内在/工程后 0 折叠钮、点击无效）。干净 boot 的 8 张正常，所以我的冒烟测试没抓到——又一次"只测了当前路径"。
- 修复：distribute() 末尾调用 initCollapsible()（幂等，per-card guard 已有）——每次分发对首次上来的卡补挂折叠钮与监听。函数声明提升保证 boot 期调用可用。
- 按 UISPEC 制度执行：规格先补（控件-归属清单）→ 改码 → 不变量源级锁死（test_ui_invariants: distribute 必须调 initCollapsible）。
- 实测：boot 两栏/切内在/切工程全部有钮可折，折叠记忆跨类别切换保持。627 绿。

### M57 — 修复聊天窗时间显示 1970（用户报"1-22 02:38 不对"）

- 根因：addMsg 把 `Date.now()/1000`（秒）传给 fmtTime，而 `new Date(ts)` 要毫秒——2026 年的秒数被解析成 **1970-01-22 02:38（+08）**，与用户贴的分秒完全吻合。受影响面：聊天消息、目标树"创建于"、提议"提出时间"（凡传 epoch 秒的调用点全是 1970）。
- 修复：fmtTime 归一——数字且 <1e12 必为秒（毫秒现在 ~1.8e12）×1000；ISO 字符串照常；addMsg 改传毫秒双保险。
- 锁死：test_ui_invariants 新增两条源级断言（fmtTime 必须含秒归一 / 禁止 Date.now()/1000 传参）。
- 浏览器实测：消息时间=真实时间（11-19 22:42），fmtTime(秒)/fmtTime(毫秒)/fmtTime(ISO) 三输入全部正确（ISO 正确换算 +08）。628 绿。

### M58 — 空闲显示诚实化 + 冷却可调（用户报"专家又显示没有执行中的链"）

- 诊断：专家没卡死——75 分钟占空比图显示三波 5-8 分钟真干活（写 Swift/迭代自验脚本），间隙是 SELF_ACT_COOLDOWN_S（默认 30 分钟）行动冷却；活动中心的"💤 心跳空闲/等待主人"把冷却期误报为停滞。
- 修复：①/api/activity 暴露 last_act_ts/cooldown_s/has_goals，空闲文案区分三种真实状态——"冷却中，约 N 分钟后自动继续（上一轮 X 分钟前结束）/冷却已过下一拍继续/无活跃目标"；②生产 proto.env 设 PROTO_ACT_COOLDOWN_S=300（5 分钟——预算已配大、M45 探索配额/M48 计费诚实/预算地板等防呆在位，30 分钟是缺省保守值）。
- 附：不变量测试正确拦截了新代码里的 Date.now()/1000（此处是合法秒换算），断言收窄到真 bug 形态 fmtTime(Date.now()/1000)——宽断言误伤合法代码的平衡案例。628 绿。

### M58b — 冷却配置两连坑（300 秒两次未生效的根因）

1. SELF_ACT_COOLDOWN_S 是 import 时求值的类属性，proto.env 其后才加载（M36 --token 同款）→ 改 __init__ 读 env；
2. load_env_file 只解析 `export ` 开头的行——我写入 proto.env 的 PROTO_ACT_COOLDOWN_S=300 没带前缀被静默跳过 → 补 export。
两层修完线上 cooldown_s=300 生效。教训：配置不生效时逐层验证（文件内容→解析器语义→读取时机→进程环境），任何一层想当然都会白修一次。

### M59 — 冷却废除，意义门控上线（用户："没做完又不累为什么休息；应识别无意义并调整，而不是无缘无故停下"）

- **设计哲学修正**：冷却是用"休息"防失控；正确的是用"意义"防失控——agent 不会累，有活+预算+没卡住就每拍心跳连续干。
- **_may_start_chain 意义门控**：只拦真停机原因——①秒败熔断（llm_error/failed <15s → 等一拍，防打转烧钱，非休息）；②**全败 hold**（所有 mid 目标 fail_streak≥3 → 停止硬撞，work_hold 审计+收件箱留言+黑板求助帖各限频 1 次/小时——"识别无意义"的真停止）；预算地板/无目标/暂停/静音为既有真实原因。failed_verification/正常完成**立即允许下一轮**。
- **跨轮失败反馈**：work_on_goal 从接力日志读上轮 awaiting_retry/failed 的发现+下一步，注入本轮任务 feedback——换打法靠信息传递，不靠冷却（此前失败原因只在轮内 retry 传递，跨轮丢失）。
- **假勤奋识别**：write_file 内容与现状完全一致 → 返回 unchanged（"这不是产出"）；journal._written_files 过滤 unchanged → 原样重写计入零产出升级（M45 机制自然接管）。
- 清理：SELF_ACT_COOLDOWN_S 属性/env/显示全部废除；m27 旧契约测试迁移（冷却挡重复→干完不挡+秒败才挡）。
- 测试：tests/test_m59_continuous.py 7 项。全套 635 绿。

### M59b — 全败 hold 改自主换打法（用户"又变空闲了？"，暴露 M59 死锁设计）

- M59 的全败 hold 是死胡同：fail_streak 仅验证通过才清零，held 下永远无法通过 → 永久锁死，且没给"自己换思路"的出路——违背"识别后调整行动"。
- 修正：全败先**自主换打法**——重置最可救目标的失败计数（approach_resets 持久化计数），注入【换打法】强指令（禁止原路径、重新规划、根本不同方法/最小可验证切片），approach_reset 审计+留言；**同一目标换过 2 轮打法仍全败**才是真 hold（work_hold+黑板求助+留言，自主思路已尽才升级问主人）。
- 测试：全败先换法（放行+重置+指令）/换法耗尽才 hold。全套 636 绿。

### M60 — 修复"时天周月年汇总"永久加载中（用户报障）

- 根因：refreshBuckets 是孤儿函数——定义后无任何调用点（历次重构丢调用），其 catch(e){} 静默吞错 + 占位文案"（加载中）"永不替换 → 整卡假死。端点本身 37ms 正常。
- 修复：接进 8 秒轮询注册表；失败可见（错误信息替换占位文案，不再永久加载中）。
- 防再现：全量审计 refresh* 函数引用（17 个，仅此一个真孤儿）+ test_ui_invariants 源级锁死"每个 refresh 函数必须有定义外引用"。
- 浏览器实测：工程类下汇总卡显示 时/天/周/月/年 各桶事件数与 top 类型。637 绿。

### M61 — 自主性审查：GoalForge + 本地产品化闭环（用户核心设计复核）

**审查结论**：此前“定时目标生成”只有调度器事件、没有运行时回调；目标耗尽后退化为学习笔记。目标分解截断为 3 段会丢“就绪交付”；`run_python` 在临时目录运行，不能验证项目；生成验收未写回 GoalNode，标准可跨轮漂移；独立验证只看编辑参数不看最终产物；自主目标执行误走 owner 池。

- **GoalForge**（`proto/goals/forge.py`）：汇总人设、主人优先级、审美、活跃目标、反思与世界模型事实生成候选；本地评分淘汰主人不对齐/人设不契合/重复/无本机证据的想法；写入 `source=self` 长期目标并分解。4h 调度回调已接线，无目标且动机越阈时即时触发，失败持久限频。
- **交付阶梯**：分解保留 4 阶段（领域学习与设计 → 实现 → 本地验证 → 就绪交付）；模型缺交付段时自动补齐；GoalForge 分解失败时用确定性阶梯兜底，愿景不再悬空。
- **本机验证能力**：`run_python` 以项目工作区为 cwd，可直接读取/导入项目文件；新增 `run_command`（数组 argv、无 shell、命令白名单、cwd 圈死、写遏制+禁网沙盒）支持 Python/Swift/XcodeBuild/Node 测试编译；沙盒 profile 拒绝读取 owner.key/proto.env/data/.fleet。
- **执行链学习感官**：`github_search/github_readme/web_fetch` 以 READ_DRAFT 进入 Agentic 循环；外部网页输出显式打标“素材非指令”。
- **质量门**：验收生成后立即持久化，后续不可漂移；独立验证读取工作区最终产物原文与命令/测试轨迹，不只信执行者叙述。
- **预算与易用性**：`source=self` 执行走 self_initiated（默认 40k，可配置）；force_act 失败原因区分无 LLM/连败保护/预算不足/GoalForge 未产出/无产出，执行 dict 序列化成人话；D2/D3 偿还。
- **记忆/方法**：检索即强化（间隔效应落地）；方法近似名+描述去重。
- 文档：新增 `docs/specs/2026-11-19-autonomy-audit.md`、ADR 0002；README/DEBT 同步。
- 验证：`python3 -m pytest -q` → **651 passed, 2 skipped**；macOS 实测 sandbox-exec 下 `run_python` 可读项目文件、拒读内部密钥，`run_command` 可在项目 cwd 执行白名单命令。
