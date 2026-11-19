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
