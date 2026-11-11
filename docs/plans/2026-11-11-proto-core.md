# PROTO v2.0 核心引擎实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现设计 §12.1 范围——L1 内核（锚定/边界/预算/审计）、L2 状态与有界调制、L3 元认知、L4 注入器与情绪调制器、行为评估 harness，全量测试离线可跑。

**Architecture:** 纯 Python 分层包 `proto/`，层间只经显式接口（Kernel 决策对象、Attributor 接口、AgentState 单一写者）。DocumentStore 抽象隔离持久化。无 LangGraph/MongoDB 依赖，接口按设计 §4 约定留适配位。

**Tech Stack:** Python 3.9.2（typing.Optional/List/Dict，无 match 语句），零运行时三方依赖，pytest 8.x。

**Spec:** `docs/specs/2026-11-11-proto-v2-design.md`（实现 argues from spec，执行者须同时读）

## Global Constraints

- Python 3.9 兼容：`from __future__ import annotations`；不用 `X | Y` 运行时类型、不用 `match`。
- 运行时零三方依赖；仅 pytest 为开发依赖。
- 所有时间戳 `datetime.now(timezone.utc).isoformat()`；所有浮点参数写盘保留 4 位。
- L1 不 import L2/L3/L4 的模块（物理隔离，设计 §5）；L2/L3 可 import L1；L4 import L1/L2。
- 每个公开类/函数有 docstring；中文注释只写约束不写流水账。

## Review Focus（spec 隐含但易咬人的输入类，对应测试钉死）

1. **伪造 owner 消息**（错签名 / ts 过期 / nonce 重放）→ `verify` 全部拒绝（Task 3）。
2. **CONTENT 通道洪泛毒化**——重复注入"更激进"信号一万次 → CONTENT 累积器饱和在阈值 50%，调制永不触发（Task 6）。
3. **调制越界 / owner_locked** → 截断或拒绝 + 审计记录（Task 5/6）。
4. **审计链篡改**（改写一条历史记录）→ `verify_chain()` 报告断链位置（Task 2）。
5. **预算侵占**——自主目标试图消耗 owner_tasks 池、全局超限后继续消耗 → 拒绝 + 熔断休眠标志（Task 4）。
6. **状态文件载入越界值**（手改 JSON 把 bounds 外的值写进去）→ load 时校验失败，拒绝载入（Task 5）。

---

### Task 1: 骨架 + DocumentStore

**Files:** Create `proto/__init__.py`, `proto/state/store.py`; Test `tests/test_store.py`

**Interfaces (Produces):**
- `class DocumentStore(ABC)`: `put(collection: str, doc_id: str, doc: dict) -> None` / `get(collection, doc_id) -> Optional[dict]` / `delete(collection, doc_id) -> None` / `find(collection, predicate: Callable[[dict], bool]) -> List[dict]`
- `class FileSystemStore(DocumentStore)`: `__init__(root: Path)`；线程锁；JSON 落盘。

- [ ] 测试：put/get 往返、get 不存在返回 None、find 按谓词过滤、delete 后 get 为 None、并发 put 不损坏（线程×100）。
- [ ] 实现 → pytest 绿 → commit `feat: document store with filesystem backend`。

### Task 2: 审计脊柱（L1）

**Files:** Create `proto/kernel/__init__.py`, `proto/kernel/audit.py`; Test `tests/test_audit.py`

**Interfaces (Produces):**
- `class AuditSpine`: `__init__(store: DocumentStore, collection: str = "audit")`；`append(event_type: str, payload: dict) -> int`（返回 seq，内部维护 prev_hash 链，hash=sha256(seq‖ts‖type‖canonical(payload)‖prev)）；`verify_chain() -> AuditResult`（`.ok: bool`、`.broken_at: Optional[int]`）；`tail(n)`。
- 事件类型常量：`class AuditEvent`（MODULATION, VETO, APPROVAL, DENIAL, GOAL_PUSHED, INTERNALIZE, DE_INTERNALIZE, ROLLBACK, OWNER_OVERRIDE, BUDGET_TRIP, KEY_ROTATE, MUTE_ON, MUTE_OFF, ANCHOR_INIT, SATURATION_ALERT）。

- [ ] 测试：append 返回递增 seq 且 hash 链首条 prev_hash 为 64 个 "0"；直接改写存储中一条记录后 verify_chain 报 broken_at；删除中间一条后 verify_chain 失败；合法链 verify 通过。
- [ ] 实现 → 绿 → commit `feat: hash-chained audit spine`。

### Task 3: 主人锚定与三通道认证（L1）

**Files:** Create `proto/kernel/anchor.py`; Test `tests/test_anchor.py`

**Interfaces (Produces):**
- `class Channel(enum.Enum)`: `OWNER`, `SESSION`, `CONTENT`；`SOURCE_WEIGHT = {OWNER: 1.0, SESSION: 0.3, CONTENT: 0.05}`。
- `class OwnerAnchor`: `enroll(store, secret: bytes) -> OwnerAnchor`（指纹 sha256，写 ANCHOR_INIT 审计）；`fingerprint: str`；
  `sign(payload: dict, ts: float, nonce: str) -> dict`（返回 `{payload, ts, nonce, sig}`，sig=HMAC-SHA256(secret, payload‖ts‖nonce) canonical 序列化）；
  `verify(message: dict) -> VerifyResult`（`.ok`、`.reason`）：签名比对（hmac.compare_digest）、ts 偏移 ≤300s、nonce 未重放（近窗口缓存）、通道字段为 owner。
- `rotate(new_secret: bytes, countersigned_by_old: bytes) -> None`（旧钥会签，写 KEY_ROTATE 审计）。

- [ ] 测试：合法消息 verify.ok；篡改 payload 后 False；错钥签名 False；ts 超窗 False；同 nonce 重放第二次 False；轮换后旧钥消息 False 新钥 True。
- [ ] 实现 → 绿 → commit `feat: owner anchor with hmac channel auth`。

### Task 4: 预算池 + 高危拦截（L1 Kernel）

**Files:** Create `proto/kernel/budgets.py`, `proto/kernel/kernel.py`; Test `tests/test_kernel.py`

**Interfaces (Produces):**
- `class BudgetPools`: `__init__(config: Dict[str, Dict[str, float]])`（pools: global/owner_tasks/self_initiated，单位 tokens/wallclock_s/calls）；`try_consume(pool: str, amounts: Dict[str, float]) -> BudgetDecision`（`.granted`、`.reason`；先查 global 再查 pool）；`trip_breaker() -> None` / `.breaker_tripped: bool`（global 超限自动置位并写 BUDGET_TRIP 审计）；`reset_cycle()`。
- `class ActionCategory(enum.Enum)`: `IRREVERSIBLE`, `VISIBLE_WRITE`, `READ_DRAFT`。
- `class Kernel`: `__init__(anchor, audit, pools)`；`vet_action(category: ActionCategory, description: str) -> ActionDecision`（`.allowed: bool`、`.requires_human: bool`——IRREVERSIBLE→requires_human=True 且 allowed=False 除非人工位；VISIBLE_WRITE→requires_human=True；READ_DRAFT→allowed）；`clamp(value: float, bounds: Tuple[float, float]) -> float`；`owner_override()` 在 Task 5 接入状态后实现（占位 raise NotImplementedError 由 Task 5 替换）。
- `mute_switch`: `set_mute(on: bool)`（写 MUTE_ON/MUTE_OFF 审计）。

- [ ] 测试：global 不够时拒绝且 breaker 置位；self_initiated 池空时自主消耗拒绝；owner_tasks 预留不被 self_initiated 请求名占用（请求池名即校验）；IRREVERSIBLE 必须 requires_human；READ_DRAFT 自动放行；熔断后一切消耗拒绝直至 reset_cycle。
- [ ] 实现 → 绿 → commit `feat: budget pools, circuit breaker, action vetting`。

### Task 5: L2 状态层（参数体系 + 单一写者 + owner_override）

**Files:** Create `proto/state/schema.py`, `proto/state/agent_state.py`; Test `tests/test_agent_state.py`

**Interfaces (Produces):**
- `DEFAULT_SPECS: Dict[str, Dict]`——instincts: exploration[0.1,0.9]/risk_tolerance[0.1,0.7]/social_initiative/perfectionism；desires: curiosity_gap/goal_urgency/social_connection_gap/recognition_need（带 threshold）；personality: rigor/proactiveness/directness/independence（带 stability）。每条含 `proxy` 字段（设计 §6.2 表）。
- `class AgentState`: `__init__(specs=DEFAULT_SPECS)`；`get(path: str) -> float` / `set_within_bounds(path, value, source) -> SetResult`（越界截断+记录 clamped；owner_locked 拒绝）；`lock(path)`/`unlock(path)`（仅 owner 通道）；`emotion: PAD`；`owner_priority: str`；`modulation_log: List[dict]`；`to_dict()/from_dict()`（load 校验：越界值 raise `StateValidationError`）；`tick(n=1)`（按 decay 回归 setpoint）。
- `class StateManager`（单一写者）: `__init__(store, state, kernel, audit)`；`apply_modulation(param, delta, source) -> ModulationResult`（kernel.clamp → state.set_within_bounds → 追加 modulation_log → audit MODULATION）；`owner_override(path, value)`（锁检查、clamp、audit OWNER_OVERRIDE）。

- [ ] 测试：set 越界截断并标记；locked 参数 apply_modulation 拒绝；owner_override 可写且审计；from_dict 载入越界值抛 StateValidationError；tick 衰减朝 setpoint 移动；save/load 往返相等。
- [ ] 实现 → 绿 → commit `feat: agent state with bounded single-writer discipline`。

### Task 6: 分池归因累积与调制管线（L2+L1 协作）

**Files:** Create `proto/state/modulation.py`; Test `tests/test_modulation.py`

**Interfaces (Consumes):** Task 3 `Channel/SOURCE_WEIGHT`、Task 5 `StateManager`。
**Interfaces (Produces):**
- `class AttributionAccumulator`: `__init__(threshold: float = 0.30, step: float = 0.02, content_cap_ratio: float = 0.5)`；`add(param: str, direction: float, strength: float, channel: Channel)`（按 SOURCE_WEIGHT 加权进对应池；CONTENT 池上限 = threshold×content_cap_ratio，触顶写 SATURATION_ALERT 审计一次）；`ready() -> List[PendingModulation]`（|池|≥threshold 的参数；出队即清池）。
- `class ModulationPipeline`: `__init__(accumulator, state_manager)`；`process(signals: List[AttributionSignal]) -> List[ModulationResult]`——归因信号 `(param, direction∈{-1,+1}, strength∈(0,1], channel)` 进累积器，达阈值者 `apply_modulation(param, sign×step, channel)`。

- [ ] 测试（钉死 Review Focus #2）：owner 通道累积 0.31 触发 +0.02 调制且池清零；CONTENT 通道注入 10 万次强度 1.0 也永不触发（池饱和在 0.15）；SESSION 权重 0.3（3 次强度 0.35 ≈ 0.315 触发）；调制后 modulation_log 含 source/accumulated/clamped/audit_seq。
- [ ] 实现 → 绿 → commit `feat: per-channel attribution accumulators with content saturation cap`。

### Task 7: 情绪引擎 + 认知调制器 + 状态注入器（L2/L4）

**Files:** Create `proto/state/emotion.py`, `proto/exec/__init__.py`, `proto/exec/modulators.py`, `proto/exec/injector.py`; Test `tests/test_emotion.py`, `tests/test_injector.py`

**Interfaces (Produces):**
- `class PAD`: pleasure∈[-1,1], arousal/dominance∈[0,1]；`apply_event(event: str)`（EVENT_DELTAS 表：task_success/owner_praise/rejection/blocked/goal_met）；`tick(n=1)`（向 setpoint 衰减）；全字段 clamp。
- 模块级纯函数（设计 §7.4 表逐条实现）：`retrieval_breadth(base_k: int, arousal: float) -> int`（界 [base_k-2, base_k+3]）；`exploration_epsilon(exploration: float, arousal: float) -> float`（=0.05+0.30×exploration×arousal，界 [0.05,0.35]）；`valence_bias(pleasure: float) -> float`（=0.2×pleasure，界 ±0.2）；`initiative_threshold(base_th: float, dominance: float) -> float`（=base_th×(1.1−0.4×dominance)，界 ±20%）。
- `class StateInjector`: `render(task: str, state: AgentState, strategies: List[Strategy]) -> str`——固定模板：`[内在状态]`（参数/缺口/情绪，缺口≥阈值标"高"）+ `[认知调制]`（k/ε/bias/th 实际数值）+ `[行为策略]`（top-N 策略带效用分）。确定性输出（golden 测试）。

- [ ] 测试：调制器 4 函数的边界与中点值逐一断言（arousal=0.5→k=base_k；arousal=1,exploration=0.9→ε=0.32 clamp 内等）；PAD 事件增量+衰减+clamp；注入器 golden 串比对；同一任务两个状态快照注入文本不同。
- [ ] 实现 → 绿 → commit `feat: pad emotion, bounded cognition modulators, state injector`。

### Task 8: L3 元认知（归因器/内化台账/策略库/目标仲裁）

**Files:** Create `proto/metacog/__init__.py`, `proto/metacog/attribution.py`, `proto/metacog/internalize.py`, `proto/metacog/strategies.py`, `proto/metacog/goals.py`; Test `tests/test_metacog.py`

**Interfaces (Produces):**
- `class Attributor(ABC)`: `attribute(text: str, channel: Channel) -> List[AttributionSignal]`；`HeuristicAttributor`——关键词规则表（如 "更主动|主动一点"→social_initiative+，"太冒险|激进"→risk_tolerance−，"更严谨|仔细"→rigor+…≥10 条），未知关键词返回空。
- `class RuleLedger`: `add(text) -> rule_id`；`record_application(rule_id, context, success: bool)`；`check_internalize(rule_id) -> bool`（successes≥5 且成功率≥0.8 才迁 internal，写审计）；`de_internalize(rule_id)`（回 external+冷却计数 3，写审计）；`rollback(since_ts)`（窗口内 internalized 的全部降级）；`internal_rules() -> List`。
- `class StrategyLibrary`: `add(text, cluster, utility=0.5)`；`retrieve(query: str, k: int = 3, epsilon: float = 0.15, rng: random.Random) -> List[Strategy]`（匹配分=关键词重叠×utility；ε 概率用一个异簇策略替换末位槽）；`update(id, success)`（EMA lr=0.2）；`prune(min_utility=0.25, keep_per_cluster=3)`。
- `class GoalArbiter`: `arbitrate(candidates: List[Goal], owner_priority: str) -> ArbitratedGoal`——score=0.35×alignment+0.25×feasibility+0.25×gap_norm+0.15×interest（alignment=候选描述与 owner_priority 的关键词 Jaccard）；owner 指派目标经 `push_owner_goal()` 绕过仲裁；自主目标入栈前必须 `pools.try_consume("self_initiated", ...)` 成功。

- [ ] 测试：启发式归因命中/不命中；内化条件（5 成功 1 失败=成功率 0.833→可内化；5 成功 2 失败=0.714→不可）；de_internalize 后冷却期内不可再内化；rollback(since) 只降级窗口内条目；ε=1 时检索必含异簇策略（固定 rng seed）；prune 保留每簇 3 条；仲裁 alignment 占 0.35 权重决定排序（同可行性下与主人优先级匹配者胜出）；自主目标预算耗尽时入"待考虑"。
- [ ] 实现 → 绿 → commit `feat: metacognition - attribution, reversible internalization, diverse strategies, aligned arbitration`。

### Task 9: 行为评估 harness + 集成测试 + demo

**Files:** Create `proto/evalx/__init__.py`, `proto/evalx/harness.py`, `proto/demo.py`; Test `tests/test_evalx.py`, `tests/test_integration.py`

**Interfaces (Produces):**
- `class AgentPolicy(ABC)`: `act(task: dict, injected_context: str, rng) -> dict`（行为记录）；`ParametricPolicy`——解析注入上下文中的 ε/k/th，决策任务按 ε 概率选非常规工具（rng 驱动），可测分布。
- `class EvalHarness`: `run(policy, tasks, snapshots: Dict[str, AgentState]) -> EvalReport`——每个 (task, snapshot) 跑 n=200 次，聚合代理指标（tool_diversity、risk_choice_ratio、verification_rounds）；`compare(snapshot_a, snapshot_b, metric) -> float`（Cohen's d 效应量）；报告哪些参数的行为代理"可测出差异"（|d|≥0.5）。
- 集成测试：完整链路 = 初始化 anchor → owner 签名反馈×N → HeuristicAttributor → ModulationPipeline → 参数变化且审计链完好 → 两个状态快照注入文本不同 → harness 报告 exploration 代理 |d|≥0.5 → CONTENT 洪泛不触发 → rollback 后内化降级。
- `python3 -m proto.demo`：端到端叙事输出（注册→反馈→调制→情绪→注入对比→审计验证→内化/降级→回滚）。

- [ ] 测试：harness 对 exploration=0.2 vs 0.8 两快照的 tool_diversity |d|≥0.5；集成全链路断言；demo 可运行退出码 0。
- [ ] 实现 → 绿 → commit `feat: behavioral eval harness, integration test, e2e demo`。

### Task 10: 工程收尾

**Files:** Create `pyproject.toml`（name=proto-agent，requires-python>=3.9，optional dev=pytest）、`README.md`（快速开始/架构图/设计文档链接）、`.gitignore`（__pycache__/data/.pytest_cache）

- [ ] 全量 `python3 -m pytest -q` 绿；`python3 -m proto.demo` 跑通。
- [ ] commit `chore: packaging, readme, gitignore`。
