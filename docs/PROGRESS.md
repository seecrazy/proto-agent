# PROTO 项目进度

> 单一事实来源：里程碑、状态、验证证据。每次合并不只是代码——本文件同步更新。

## 当前状态

| 项 | 状态 |
|---|---|
| 版本 | 0.3.0（引擎完全体：设计内机制全部落地） |
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
