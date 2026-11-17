# PROTO 项目进度

> 单一事实来源：里程碑、状态、验证证据。每次合并不只是代码——本文件同步更新。

## 当前状态

| 项 | 状态 |
|---|---|
| 版本 | 0.8.0（独处生活：服务端心跳/自主草稿/深度回顾） |
| 测试 | 见下方里程碑记录 |
| 分支 | main（feat/engine-completion 已并入） |
| 远程 | 仅本地提交，未推送（用户指示） |

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
- [x] Electron 取舍：不引入（~200MB Chromium/Node 依赖，违背零依赖立身之本）；原生壳以 2MB swiftc 产物获得窗口/Dock/生命周期管理。
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
