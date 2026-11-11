# PROTO 阶段 6–8 + 质量收尾实现计划

**Goal:** 完成落地路径阶段 6（静默执行+审批）、阶段 7（代码轨沙盒进化）、阶段 8（行为轨影子双跑+分级授权），修复全部遗留 minor，交付专业版设计文档/进度文档/README（Mermaid 图）。

**Spec:** `docs/specs/2026-11-11-proto-v2-design.md` §8 §9 §12；进度记录：`docs/PROGRESS.md`。

## Global Constraints

- 与既有约定一致：Python 3.9、零运行时依赖、TDD、L1 不依赖 L2/L3/L4/L5。
- 沙盒本地实现为 subprocess + 临时目录 + 超时 + 清洁环境（生产目标 gVisor/microVM，文档注明）。
- 测试集治理（人类所有、只增不删、变体不可提案）必须由 OWNER 通道签名强制。

## Tasks

1. **minor 修复批**：M2 审计 append O(n²)→缓存 last seq/hash；M3 Callable 导入；M4 store 路径挡 `..`；M6 Kernel.trip_breaker 公开 + harness 增 verification_rounds 指标 + 注入器 golden 全文测试 + EvalHarness 默认 runs=200；M7 enroll 拒绝静默替换 + rotate 会签新鲜度；M8 未知预算单位不熔断 + 目标拒绝写 DENIAL + to_dict 深拷贝 + Accumulator.audit 必传 + store 提层 proto/storage。
2. **阶段 6**：ScratchWorkspace（隔离+晋升）、ApprovalQueue（kernel 先审、人批、审计、日上限）、CheckpointStore（schema_version@variant_id）。
3. **阶段 7**：TestSet（OWNER 签名追加、无删除方法）、Sandbox（subprocess 隔离执行）、PrivateScorer、VariantArchive（进化树）、EvolutionEngine（代码轨闭环+canary）。
4. **阶段 8**：ShadowRunner（双跑行为对比，复用 evalx 指标）、EvolutionAuthorizer（低/中/高分级，高危 kernel veto）。
5. **demo/集成扩展 + 文档**：demo 增 [10][11] 段；集成测试补阶段 6–8 链路；设计文档 v2.1（实施状态标注 + Mermaid 架构/流程图）；README 专业版；PROGRESS.md 全程维护；pyproject 0.2.0。
