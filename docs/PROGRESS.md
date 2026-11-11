# PROTO 项目进度

> 单一事实来源：里程碑、状态、验证证据。每次合并不只是代码——本文件同步更新。

## 当前状态

| 项 | 状态 |
|---|---|
| 版本 | 0.5.0（Web 管理控制台：一条命令部署与管理） |
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
