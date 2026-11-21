# PROTO 安全、持久化与运行时一致性加固（2026-11-18）

> **历史审查与验收记录。** 当前累计方案、授权/恢复/隔离边界统一阅读 [PROTO 总设计](../DESIGN.md)；本文件保留当次设计与修复证据，不作为并列的当前规格。

## 1. 目标

本轮修复 2026-11-18 审查中发现的高风险问题，优先保证：

1. 模型输出不能降级工具审批级别；所有有副作用的工具都经过统一授权。
2. 代码执行、文件访问和外部编码 CLI 默认处于可审计、可拒绝的边界内。
3. 审批队列、预算、自动白名单、目标验收定义和记忆元数据跨重启保持。
4. 暂停任务在进程重启后仍可恢复，且不会因审批记录丢失而 `KeyError`。
5. Web 控制台不执行不可信 HTML；访问令牌不再硬编码或写入命令行参数。
6. 任务结果、规矩、黑board、兴趣和策略反馈只在真实证据成立时记账。
7. 文档、版本、许可证和测试计数与实际代码一致。

## 2. 非目标

- 不把 PROTO 改造成多租户云服务。
- 不在本轮引入新的运行时依赖；可选适配器仍按需安装。
- 不实现完整向量数据库或分布式事务；文件存储先保证单机正确性和重启恢复。
- 不伪造“影子双跑”或代码变体执行结果。无法可靠自动执行的能力会明确标记为未接入，而不是静默宣称完成。

## 3. 用户流程

### 3.1 主人指派任务

1. 前端提交长任务到异步 job。
2. 服务端在对应 Agent 的锁内生成验收定义并运行 Agentic 循环。
3. 每个工具步骤根据工具注册表中的能力级别授权：
   - `READ_DRAFT`：只读，自动放行。
   - `WORKSPACE_WRITE`：仅项目工作区内写，自动放行。
   - `VISIBLE_WRITE` / `IRREVERSIBLE`：持久化审批，暂停；批准后继续原步骤。
4. 任务完成时只按实际状态记账：`verified`/`completed` 才算成功；`max_steps`、验证失败和异常都不得触发成功奖励或规矩内化。

### 3.2 暂停与重启

1. 审批队列、每日自主动作计数和自动白名单历史写入同一个 `DocumentStore`。
2. 重启后 `pending_proposals()` 能恢复。
3. `resume_task()` 使用已恢复的审批记录；重复批准是幂等的。
4. 只读接口不使用全局长锁；长任务按 Agent 分开串行。

### 3.3 代码执行

1. 默认只启用平台已有的隔离执行后端：Linux `runsc`、macOS `sandbox-exec`。
2. 没有隔离后端时，默认拒绝任意代码执行；只有显式环境变量允许时才回退到未隔离 subprocess。
3. 所有文件工具和 Scratch 工作区在解析路径后检查真实路径仍在允许根目录内，拒绝 symlink 逃逸。

## 4. 数据与状态模型

### 4.1 工具能力

`ToolRegistry.register(name, fn, policy=ToolPolicy(...))`。策略至少包含：

- `category`：最低动作级别；
- `timeout_bounds`；
- `description`。

Router 的最终级别取“工具策略级别”和“请求显式级别”的较高者，模型只能加严，不能降级。审批状态写入 `checkpoints`，包含待执行步骤和 `waiting_for`。

### 4.2 持久化审批

- `approvals/main`：当前待审批提议、日期、当日自主动作计数。
- `approvals/quota`：自动白名单历史。
- `kernel_budget/main`：全局/主人/自主预算余额、熔断状态和日期。

所有写入通过 `DocumentStore`，文件后端使用原子替换和 0600 权限。

### 4.3 审计

- 审计记录保留向后兼容的 SHA-256 链；新记录使用 owner secret 的 HMAC-SHA256 签名。
- 验证缓存绑定存储修订号；目录内容变化后必须重新验证。
- 提案提交、工具执行、规矩新增和黑板发帖均写审计事件。

### 4.4 Agent 隔离

MongoDB 集合按数据目录命名空间前缀隔离；文件后端继续按目录隔离。多个 Agent 不得共享 `state/main`、`audit`、`memory` 等集合。

## 5. 关键交互与错误处理

- 非法工具类别 → 任务失败，不降级为 `read_draft`。
- 工具异常 → 形成结构化失败结果并写检查点，不冒泡成未处理 500。
- 超长文件编辑 → 明确拒绝，不截断后写回。
- WorldModel topic 作为普通字段保存，文档 ID 使用哈希。
- 令牌缺失 → 部署向导生成并返回；App 壳在运行时生成随机令牌或读取显式注入。
- launchd plist 不保存 API key/token，权限 0600。
- Web 动态文本全部 `textContent` 或 HTML 转义；响应增加 CSP 等安全头。
- 请求体超过上限 → 413；JSON 类型错误 → 400。

## 6. 验收清单

- [x] 非写文件工具标成 `visible_write`/`irreversible` 时会暂停并等待审批。
- [x] 模型把 `run_python` 标成 `read_draft` 时仍按工具策略要求审批（未隔离 subprocess 后端下策略=visible_write，测试锁定）。
- [x] 工作区文件工具拒绝 symlink 逃逸（CodeTools 与 ScratchWorkspace 均按解析后真实路径圈界）。
- [x] 未经显式允许且无隔离后端时，`run_python` 返回不可用而不是执行（macOS 默认 sandbox-exec=写遏制+禁网，已实测逃逸写入与联网均被拒）。
- [x] 重启后待审批提议、暂停任务恢复、预算和自动白名单继续有效（`test_m36_hardening.py::TestDurableRuntime`）。
- [x] 审计文件被外部修改后，已有 AuditSpine 下一次验证返回失败（修订号缓存失效 + HMAC 重算不符）。
- [x] `max_steps` 不降低欲望、不触发规矩内化（仅 `completed` 记账）。
- [x] `edit_file` 对超过上限文件拒绝而非截断写回。
- [x] Memory emotion/reinforce_count、Goal acceptance 重启后保持。
- [x] 空测试集不能产生 canary；promotion 必须引用真实沙盒通过记录（门 0 + 档案凭证核验）。
- [x] 无隔离后端时 `run_python`/代码变体执行默认拒绝（进化沙盒同一后端词汇表：runsc/sandbox-exec/subprocess 显式/none）。
- [x] Web 提案描述、路径和事件类型均不产生 HTML 注入（esc() 全覆盖 + 源级断言锁死；CSP/nosniff/DENY 安全头；413/400）。
- [x] 全量测试通过（497 绿 + 2 可选跳过），README/PROGRESS/版本（0.30.0）/许可证/测试计数一致。
