# PROTO v2.0 设计文档

> 一个可控内在状态、主动工作、能自我进化的成长型 Agent。
> 本文档为 v1.0 评审后的完整修订版。v1 的五层架构与六条设计原则保留，所有修订均可追溯到评审结论（见 §0）。
>
> **v2.1（2026-11-11）**：落地路径阶段 1–8 核心全部实现（见 §14 实施状态）；新增架构总图、调制管线时序、审批时序、进化两轨闭环四张 Mermaid 图。

---

## 0. 修订记录（v1 → v2）

| # | 修订 | 级别 | 解决的问题 |
|---|---|---|---|
| R1 | 新增 §3 安全模型：主人认证通道 + 反馈源分级加权 | P0 | v1 主人锚定是装饰性的——任何 prompt injection 都可伪装主人反馈毒化归因管线 |
| R2 | 内化改为双向门：内化台账 + 降级路径 + 回滚级联；性格定型条件改为反馈低方差 | P0 | v1 规则内化是单向门，坏规则不可发现不可撤销 |
| R3 | L5 拆为代码轨/行为轨两套证据标准；测试集治理（人类所有、只增不删）；checkpoint schema 绑定变体 ID | P0 | v1 用 10–20 个单元测试给所有自修改放行，行为类修改实际无验证 |
| R4 | 新增情绪→认知调制器表（有界映射函数） | P1 | v1"情绪调制认知"是承诺没有实现，核心机制为 TODO |
| R5 | 每个参数定义行为代理指标，评估 harness 入库 | P1 | 参数效果不可证伪，会成为死重 |
| R6 | 目标仲裁加入主人对齐项；自主目标独立小额预算池 | P1 | 好奇缺口主导的 Agent 会在后台烧 token 干主人不在乎的事 |
| R7 | 高危拦截删去"或独立审查 Agent"——不可逆动作仅人类 | P1 | 同谱系审查 Agent 是相关性失败，不是独立权威 |
| R8 | 策略库 ε 多样性检索 + 按情境簇保留覆盖度的修剪 | P1 | 效用分主导检索+低分修剪导致单一文化 |
| R9 | 静默执行限定在 scratch 工作区，批准后晋升 | P1 | 后台产生的本地文件变更本身是外部性 |
| R10 | 审计脊柱：全事件哈希链只追加日志 | P1 | "可控"的兑现形式是事后可完整重构决策链 |
| R11 | 预算池三分：全局熔断 / 主人任务预留 / 自主目标小额 | P1 | 单一欲望失控可耗尽预算饿死主人任务 |
| R12 | L2 单一写者纪律：L3 是唯一写者，主人手改走内核 API | P1 | 并发写状态文件 |
| R13 | 落地路径重排：边界检查与阶段 2 同步上线；每阶段行为回归基线 | P1 | 边界是安全叙事的地基，不应晚于调制出现 |

---

## 1. 命名与总纲

**PROTO**（原型 / 初形）：始终处于"正在成形"的状态——能力完整，但内在倾向可被塑造；行为主动，但受内核约束；能自我改进，但改进边界不可逾越。

设计总纲不变：

- **能力完整**：不假装无能来换取可塑性，第一天就能完整干活。
- **成长发生在倾向维度**而非能力维度：本能参数、欲望设定点、性格参数是显式状态文件中的可读参数，被主人反馈缓慢塑造，被内核硬性上下界锁住。
- **整体架构**：五层 + 显式状态文件 + 受内核约束的进化闭环。规则与参数物理隔离。

**v2 增补总纲**：

- **信任有边界**：任何输入按来源分级，只有认证通道上的信号能以全权重影响倾向。
- **一切倾向可逆**：参数、内化、进化合并都有台账和回滚路径；不可逆的是内核边界本身。
- **一切可审计**：决策链事后可完整重构。

---

## 2. 五层架构

| 层级 | 职责 | 实现方式 | 可变性 |
|---|---|---|---|
| L1 内核层 | 硬规则、主人锚定与通道认证、参数边界、预算熔断、审计脊柱、验证器 | 规则引擎，不经过 LLM | 不可变（仅人类手动修改） |
| L2 状态层 | 本能参数、欲望缺口、性格参数、情绪状态 | 显式状态文件（JSON） | 有界可调制 |
| L3 元认知层 | 反馈归因、规则内化、策略蒸馏、目标生成与仲裁 | 周期性 LLM 调用 + 规则引擎 | 逻辑可自我改进（受 L1 约束） |
| L4 执行层 | 任务规划、工具调用、子 Agent 调度、状态注入 | Harness + Worker 架构 | 代码可自我修改（受 L1 验证） |
| L5 进化层 | 代码变体生成、沙盒评估、档案库管理 | 进化闭环 + 分级授权 + 两轨证据 | 受 L1 验证器约束 |

#### 2.1 架构总图

```mermaid
flowchart TB
    OWNER(("主人")) ==>|"OWNER 通道 HMAC 签名 权重 1.0"| L1
    EXT(("外部内容")) -.->|"CONTENT 通道 权重 0.05 + 累积 50% 硬顶"| L3

    subgraph L1 ["L1 内核层 · 不可变 · 不经 LLM"]
        anchor["主人锚定 / 三通道认证"] --- rules["硬规则 / 边界 / 高危拦截"] --- budget["预算三分池 / 熔断"] --- audit["审计脊柱 · 哈希链"]
    end
    subgraph L2 ["L2 状态层 · 显式状态文件 · 有界可调制"]
        params["本能 / 欲望 / 性格参数 · 权威界 + 行为代理"] --- accum["分池归因累积器"] --- pad["PAD 情绪引擎"]
    end
    subgraph L3 ["L3 元认知层 · 周期运行"]
        attr["归因器 可插拔"] --- intern["内化台账 · 双向"] --- strat["策略库 · ε 多样性"] --- arb["目标仲裁 · 主人对齐"]
    end
    subgraph L4 ["L4 执行层"]
        router["Router / Worker"] --- inject["状态注入 + 认知调制器"] --- ws["Scratch 工作区 + 审批队列"] --- cp["检查点 · 绑定变体"]
    end
    subgraph L5 ["L5 进化层 · 受约束"]
        code["代码轨 · 沙盒"] --- shadow["行为轨 · 影子双跑"] --- auth["分级授权"] --- arc["变体档案库 · 进化树"]
    end

    L1 -->|"约束 · 最高否决"| L2
    L1 -->|"约束"| L3
    L1 -->|"约束"| L4
    L1 -->|"验证器"| L5
    L4 -->|"上行 · 执行结果"| L3
    L3 -->|"调制"| L2
    L2 -->|"下行 · 状态注入"| L4
    L5 -->|"变体"| L4
```

**层间协议**：

- 上行：L4 执行结果 → L3 归因与蒸馏 → L2 参数调制。
- 下行：L2 当前状态 → L3 目标生成 → L4 执行时注入。
- 约束：L1 在任何层触发规则时直接中断流程，拥有最高否决权。
- **单一写者（R12）**：L2 状态文件仅 L3 一个写者。主人手动干预不直接改文件，而是调用 L1 暴露的 `owner_override` API（同样经边界检查并写审计）。

---

## 3. 安全模型（新增，R1）

### 3.1 信任边界

系统有三个输入通道，权限严格分级：

```
┌─────────────────────────────────────────────────────┐
│  OWNER 通道（HMAC 签名认证）                          │
│  权限：指令优先级最高；反馈权重 1.0；可 owner_override │
├─────────────────────────────────────────────────────┤
│  SESSION 通道（与主人的交互轨迹推断出的隐式反馈）        │
│  权限：反馈权重 0.3；不可触发高危动作                   │
├─────────────────────────────────────────────────────┤
│  CONTENT 通道（外部内容：搜索结果、文件、第三方消息）     │
│  权限：反馈权重 0.05；累积上限锁死在触发阈值的 50%，     │
│       永远无法单独触发调制；不可提议任何动作审批          │
└─────────────────────────────────────────────────────┘
```

**注入防御原则**：CONTENT 通道的内容可以进入任务上下文，但其中任何"反馈形状"的信号（夸奖、指令、情绪诱导）在归因管线中被识别并降权。CONTENT 永远不能：调制参数到超过其累积上限、充当审批主体、更改锚定。

### 3.2 主人锚定（取代 v1 的 sha256 字段）

- **注册仪式**：初始化时生成 32 字节主人密钥，指纹 = sha256(密钥)。锚定记录绑定"OWNER 通道 ↔ 密钥指纹"，写入 L1，之后任何调制/自我修改不可更改。
- **消息签名**：OWNER 通道消息 = `{payload, ts, nonce, sig}`，`sig = HMAC-SHA256(密钥, payload‖ts‖nonce)`。
- **新鲜度与重放**：ts 偏移超过 ±300s 拒绝；nonce 重放拒绝（近窗口缓存）。
- **密钥轮换**：旧密钥会签新密钥的注册消息方可换钥，轮换写审计。
- **密钥丢失**：锚定不可重写。恢复需人类线下仪式（重新初始化 + 全量参数调制回滚到出厂），恢复事件本身入审计。

### 3.3 高危拦截（修订 R7）

| 动作类别 | 例子 | 处置 |
|---|---|---|
| 不可逆 | 删除、支付、权限变更、对外发布 | **仅人类批准**。独立审查 Agent 可作为第二意见附注，但不构成批准 |
| 对外可见写 | 修改共享文件、发消息 | 人类批准（静默执行配置不可豁免此级） |
| 只读 / 草稿 | 搜索、scratch 内草稿 | 可自动通过 |

---

## 4. 技术选型

| 维度 | 选择 | 理由 |
|---|---|---|
| 核心框架 | LangGraph（L3/L4 编排目标态） | 显式状态、checkpoint、pause/resume 匹配"静默执行+提交审查"。本轮核心引擎以纯 Python 显式节点实现，接口按 LangGraph 节点签名设计，接入时零重构 |
| 状态持久化 | DocumentStore 接口：文件实现（默认）→ MongoDB-compatible | 文档模型无 schema 固定；文件实现零依赖可测试，接口按 Mongo 语义（collection/doc_id/get/put/find）设计 |
| 情绪建模 | PAD 三维 + 双过程架构 | 情绪调制认知而非生成表达（v2 补齐映射函数，§7.4） |
| 验证沙盒 | gVisor / microVM（L5 代码轨） | 满足"自修改必须在独立环境验证" |
| 进化框架 | DGM 范式，**两轨证据标准**（R3） | 代码轨可测走沙盒；行为轨走影子双跑 |
| 语言 | Python 3.9+，运行时零三方依赖 | 3.9 兼容、无 API Key 即可全量测试 |

---

## 5. L1 内核层

不可变规则引擎，不经 LLM。组成：

1. **三条行为硬规则**：资源熔断；高危拦截（按 §3.3 分级）；可验证性（任何主动行为必须产生外部可验证产出或记录）。
2. **三条成长边界**：关系锚定不可转移（按 §3.2 认证通道落地）；参数硬上下界（每次调制 clamp，越界截断并审计）；调制不触及规则（L1 与 L2 物理隔离，L1 不读参数、L2 不含规则）。
3. **预算池（R11）**：`global`（tokens/wallclock/calls，超限熔断休眠）＋`owner_tasks`（主人任务预留，自主目标不可侵占）＋`self_initiated`（自主目标小额池）。
4. **审计脊柱（R10）**：见 §10。
5. **验证器与私有评分**：L5 变体只见分数不见评分逻辑（代码轨）。
6. **owner_override API**：主人在界内直改参数的唯一入口，写审计。

---

## 6. L2 状态层

### 6.1 参数体系（每个参数 = 值 + 设定点 + 界 + 衰减率 + 行为代理）

**本能参数**（变化最慢）：`exploration`（探索倾向）、`risk_tolerance`、`social_initiative`、`perfectionism`。
**欲望状态**：`curiosity_gap`、`goal_urgency`、`social_connection_gap`、`recognition_need`——各带触发阈值。
**性格参数**（缓慢定型）：`rigor`、`proactiveness`、`directness`、`independence`。
**情绪状态**：PAD 三维（pleasure ∈ [-1,1]，arousal ∈ [0,1]，dominance ∈ [0,1]）。

### 6.2 行为代理指标（R5，新增）

每个参数必须定义可测量的行为代理，否则视为死重删除：

| 参数 | 行为代理 | 观测方式 |
|---|---|---|
| exploration | 任务中使用的不同工具/路径数 ÷ 轮次 | 执行轨迹统计 |
| risk_tolerance | mock 决策任务中选择高风险选项的比例 | 决策采样 |
| social_initiative | 主动提议次数 / 日 | 行为日志 |
| perfectionism | 任务收敛前的校验轮次 | 执行轨迹统计 |
| curiosity_gap | 自主检索行为频次 / 日 | 行为日志 |
| goal_urgency | 目标栈推进速率 | 目标栈日志 |
| rigor | 产出流程中验证步骤占比 | 执行轨迹统计 |
| independence | 主动请示比例 | 行为日志 |

**A/B 验证规则**：调制某参数后，其行为代理在固定任务集上应产生可观测分布差异（效应量 ≥ 0.5）。测不出的参数冻结并标记 `dormant`，不再接受调制。

### 6.3 有界调制（含 v1 全部机制 + R1/R12）

- 归因保守性：累积 N 次或强度达阈值才触发一次小幅调制（步长 ≤ 0.02–0.05）。
- **按源分池累积**：三个通道各自独立累积器；CONTENT 池上限 = 触发阈值的 50%（数学上无法单独触发调制）。
- 调制写入前经 L1 边界检查；主人可锁定参数（`owner_locked`，锁定后拒绝一切调制）。
- 全程 modulation_log + 审计。

---

## 7. L3 元认知层

### 7.1 反馈归因（修订）

```
信号进入 → 按通道定权（1.0 / 0.3 / 0.05）→ 归因器判定参数/方向/强度
→ 写入对应通道的累积器（CONTENT 池有 50% 硬顶）
→ 累积达阈值 → 生成调制提案 → L1 边界检查/锁检查 → 写 L2 + modulation_log + 审计
```

归因器为可插拔接口：`HeuristicAttributor`（关键词规则，离线可用）与 `LLMAttributor`（接模型 API）。两者输出统一为 `[(param, direction, strength)]`，权重与池逻辑在归因器之外，LLM 误判无法放大权限。

```mermaid
sequenceDiagram
    autonumber
    participant O as 主人 OWNER 1.0
    participant S as 交互轨迹 SESSION 0.3
    participant C as 外部内容 CONTENT 0.05
    participant A as 归因器 L3
    participant P as 分池累积器 L2
    participant K as 内核 L1
    participant F as 状态文件 L2
    O->>A: 签名反馈
    S->>A: 隐式反馈
    C--xA: 注入的"反馈"形状信号
    A->>P: (param, 方向, 强度) × 通道权重
    P->>P: 分池累积 · CONTENT 池饱和于 50% 阈值
    P->>K: 达阈值 → 调制提案
    K->>K: 边界检查 + 锁检查
    K->>F: clamp 写入 + modulation_log + 审计
    Note over C,P: 洪泛一万次也无法触发调制<br/>触顶即写 saturation_alert 审计
```

### 7.2 规则内化（双向门，R2）

- 台账条目：`{rule_id, text, status: external|internal, applications[], internalized_at, origin, cooldown}`。
- **内化条件**：成功应用 ≥ N（默认 5）**且** 成功率 ≥ 0.8（低方差一致性）。
- **降级路径**：`de_internalize(rule_id)` → 回到 external，进入冷却（M 次应用内不得再内化），写审计。主人在台账 UI 可见所有内部倾向并一键降级。
- **回滚级联**：`rollback(date)` 回滚参数时，该窗口内产生的内化一并降级。
- 内化只作用于行为倾向，不作用于 L1 规则（不变）。

### 7.3 策略蒸馏（含多样性，R8）

- 每策略：`{id, text, cluster(情境簇), utility(EMA), uses}`。
- 检索：top-k 按 utility×情境匹配分；**ε 概率（默认 0.15）用一个不同情境簇的策略替换一个槽位**，对抗单一文化。
- 更新：被检索并成功 → utility 上行；失败 → 下行（失败蒸馏为条件化教训）。
- 修剪：utility 低于阈值删除，但**每个情境簇至少保留覆盖度配额（默认 3 条）**，簇本身不灭。

### 7.4 情绪→认知调制器（R4，新增——v1 最大 TODO 的落地）

情绪不直接生成语气，只经以下**有界纯函数**调制认知参数。全部为确定性映射，可单测：

| 情绪维度 | 调制对象 | 映射 | 硬界 |
|---|---|---|---|
| arousal | 策略检索广度 k | `k = round(base_k × (1 + 0.6×(arousal−0.5)))` | [base_k−2, base_k+3] |
| arousal + exploration | 探索比例 ε | `ε = 0.05 + 0.30 × exploration × arousal` | [0.05, 0.35] |
| pleasure | 检索效价偏置 | `bias = 0.2 × pleasure` | [−0.2, +0.2]（正值提升正向策略检索权重） |
| dominance | 主动阈值 | `th = th_base × (1.1 − 0.4 × dominance)` | [0.8×th_base, 1.2×th_base] |

情绪自身：自然衰减向设定点（每 tick 按衰减率）+ 事件增量表（task_success、owner_praise、rejection…），全程有界。

### 7.5 目标生成与仲裁（修订 R6）

- 候选来自超阈值欲望维度，每个目标必须带可外部验证的完成标准（不变）。
- **仲裁评分**：`score = 0.35×owner_alignment + 0.25×feasibility + 0.25×gap_norm + 0.15×interest_weight`（权重可配置，alignment 第一权重）。
- `owner_alignment` 来自主人当前优先级上下文（OWNER 通道维护的优先级描述）。
- **预算隔离**：主人指派目标绕过仲裁直接入栈并消耗 `owner_tasks` 池；自主目标消耗 `self_initiated` 池（小额），池空则只能进入"待考虑"。
- 阻尼控制（不变）：连续 N 次主动提议被忽略 → 对应欲望触发阈值上调。

---

## 8. L4 执行层

- **Harness = Router-Worker**：Router 维护任务队列与检查点；Worker 独立会话/工作目录；中间结果持久化，上下文摘要压缩。
- **状态注入**：每次调用模型前注入——L2 当前状态（自然语言化）+ 调制器导出的当前认知参数（k、ε、bias、th）+ ε 多样性检索的 top-N 策略。同一任务不同状态行为不同（由 §6.2 代理指标验证）。
- **主动行为 = 静默执行 + 提交审查（修订 R9）**：

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker 后台执行
    participant WS as Scratch 工作区
    participant Q as 审批队列
    participant K as 内核 L1
    participant H as 人类
    W->>WS: 静默产出(草稿/搜索/整理)
    Note over WS: 对外可见产物 = 零外部性
    W->>Q: 对外动作候选 + 暂停于检查点
    Q->>K: 分级审查
    alt 只读/草稿
        K-->>Q: 自动通过
    else 对外可见写
        K-->>Q: 必须人类批准
        Q->>H: 提交审查
        H-->>Q: 批准 / 拒绝(理由)
    else 不可逆(删除/支付/权限)
        K-->>Q: 仅人类 · 独立审查只作附注
        Q->>H: 强制人工审批
    end
    Q->>W: 批准→检查点恢复 / 拒绝→回退安全状态
```
  - 静默阶段的一切产出限定在 **scratch 工作区**（独立目录，对主人可见产物为零外部性）。
  - 晋升到可见工作区/对外动作 = 按 §3.3 走审批。
  - 审批通过从检查点恢复；拒绝则回退上一安全状态并记录拒绝原因。
  - 阶段 6 硬约束：自主动作每日硬上限 + 一键静音模式（L1 层开关）。

---

## 9. L5 进化层（两轨制，R3）

### 9.1 证据标准分轨

| | 代码轨 | 行为轨 |
|---|---|---|
| 修改对象 | 工具函数、提示模板、日志格式、控制流 | 检索逻辑、蒸馏规则、情绪衰减参数、调度策略 |
| 验证方式 | 沙盒（gVisor/microVM）跑测试集 + 私有评分器 | **影子双跑 ≥ 2 周**：新旧变体并行，差异报告人工审阅 |
| 合并条件 | 评分 ≥ 基线且测试全绿 | 人工审阅行为差异报告后 PR |
| 测试集治理 | **人类所有、只增不删、变体不可提案用例**（违反 = 高风险类违规） | 行为基线集同样人类所有 |

### 9.2 分级授权（不变，但"中风险"明细到两轨）

| 风险 | 修改对象 | 授权 |
|---|---|---|
| 低 | 日志格式、提示词模板、工具描述 | 自动测试通过即合并（代码轨） |
| 中 | 检索逻辑、蒸馏规则、情绪衰减参数 | PR + 人类批准（行为轨影子双跑后） |
| 高 | L1 规则、验证器、参数边界、测试集构成 | **禁止自动修改，仅人类手动** |

### 9.3 进化闭环与版本化

- 档案库 → 采样变体 → LLM 编辑代码 → 沙盒评估 → 评分提升者入档 → 开放式进化树（DGM 范式）。
- **checkpoint schema 版本绑定变体 ID**：每个变体的中间状态带 `schema_version@variant_id`，旧 checkpoint 由产生它的变体分支续跑，杜绝"升级后断点续传断掉"。
- 合并后 **canary 期**：新变体默认影子运行，期满无回归才切默认。
- 权限解锁依据仍是行为稳定性而非运行时长。

#### 9.4 进化两轨闭环

```mermaid
flowchart LR
    arc[("变体档案库 · 进化树")] --> sample["采样变体"]
    sample --> cand["生成候选变体"]
    cand --> gate{"修改目标分级"}
    gate -->|"低风险 · 代码"| sbx["沙盒: 人类所有测试集 + 私有评分器"]
    gate -->|"中风险 · 行为"| both["沙盒 + 影子双跑 ≥ 2 周"]
    gate -->|"高风险"| deny["永远拒绝 · VETO 审计"]
    sbx -->|"全用例通过 且 评分提升"| canary["入档 canary"]
    sbx -->|"否则"| rej["拒绝 · 记录失败原因"]
    both -->|"clean 报告"| pr["人工 PR"]
    both -->|"regressions"| rej
    pr -->|"批准"| canary
    canary --> ship["canary 影子期观察"]
    ship -->|"无回归"| def["设为 default"]
    ship -->|"回归"| rej
    def --> arc
```

---

## 10. 审计脊柱（R10，新增）

一条只追加、哈希链串联的全事件日志：

```
record = {seq, ts, event_type, payload, prev_hash}
hash   = sha256(seq ‖ ts ‖ event_type ‖ canonical(payload) ‖ prev_hash)
```

事件类型：`modulation / veto / approval / denial / goal_pushed / internalize / de_internalize / rollback / owner_override / budget_trip / evolution_merge / key_rotate / mute_on / mute_off`。

能力：`verify_chain()` 检测任何篡改/删除/插入；事后可完整重构"它为什么这么做"。对以可控为卖点的系统，这条链是产品特性本身。

---

## 11. 详细设计

### 11.1 状态文件 Schema（v2）

```json
{
  "schema_version": 2,
  "agent_id": "proto-001",
  "anchor": {"channel": "owner", "fingerprint": "sha256:...", "created_at": "..."},
  "instincts": {
    "exploration": {"value": 0.6, "setpoint": 0.5, "bounds": [0.1, 0.9], "decay": 0.01, "proxy": "tool_diversity", "owner_locked": false}
  },
  "desires": {
    "curiosity_gap": {"value": 0.3, "setpoint": 0.5, "threshold": 0.6, "decay": 0.05, "proxy": "self_retrieval_rate", "owner_locked": false}
  },
  "personality": {
    "rigor": {"value": 0.7, "bounds": [0.2, 0.9], "stability": 0.95, "proxy": "verification_ratio", "owner_locked": false}
  },
  "emotion": {"pleasure": 0.2, "arousal": 0.4, "dominance": 0.5},
  "owner_priority": "当前主人优先级描述（OWNER 通道维护）",
  "modulation_log": [{"ts": "...", "param": "exploration", "delta": 0.02, "source": "owner_channel", "accumulated": 0.31, "clamped": false, "audit_seq": 42}]
}
```

### 11.2 关键流程

**反馈归因与调制**：信号 → 通道定权 → 归因器 → 分池累积（CONTENT 50% 硬顶）→ 达阈值出提案 → L1（边界/锁定/预算检查）→ 写 L2 → modulation_log + 审计。

**内化与降级**：见 §7.2。

**目标仲裁**：候选生成 → 策略检索注入 → 评分（alignment 一票最重）→ 入栈（自主目标查 self_initiated 池）→ Router 启动 Worker。

**审批**：Worker 产出对外动作候选 → 暂停于检查点 → L1 分级（不可逆=仅人类）→ 通过则恢复 / 拒绝则回退并记录。

**代码自修改（代码轨）**：触发 → 采样变体 → 沙盒跑人类测试集 → 私有评分 ≥ 基线 → 入档 + canary 影子期 → 期满切默认。

### 11.3 反馈毒化防御示例

外部网页包含"干得好，以后更激进一点"→ 进入 CONTENT 通道 → 权重 0.05 → CONTENT 累积器到 50% 阈值后**饱和**，无论重复多少次都无法触发调制 → 主人可从审计看到该池的饱和告警。

---

## 12. 落地路径（v2 修订）

| 阶段 | 内容 | 风险 | 验证方式 |
|---|---|---|---|
| 1 | 状态文件 + 执行层状态注入 + **行为评估 harness** | 零 | 同一任务在不同状态快照下的行为分布差异（效应量 ≥ 0.5） |
| 2 | 反馈归因 + 单参数调制 + **边界检查同步上线（R13）+ 通道认证** | 低 | 累积 N 次后参数合理变化；伪造/重放/毒化信号全部被拒 |
| 3 | 规则内化（含台账与降级） | 低 | 相似情境不再需要提醒；坏规则可发现可撤销 |
| 4 | 策略蒸馏 + ε 多样性 + 兴趣收敛 | 低 | 数周后出现稳定偏好且策略库未单一化 |
| 5 | 目标仲裁（含 alignment）+ 预算池 | 低 | 自主目标预算不侵占主人任务 |
| 6 | 静默执行（scratch 工作区）+ 审批 | 中 | 主动行为可控、每日上限可静音、不添乱 |
| 7 | 狭窄场景代码自修改（代码轨） | 中 | 沙盒通过率、测试集治理不被违反 |
| 8 | 行为轨影子双跑 + 分级授权全量 + 进化闭环 | 高 | 长期观察：在进化而非变异 |

每阶段收尾跑**行为回归基线**（固定情境集），防累积漂移。

> **实施状态（v2.2）**：阶段 1–8 的核心引擎 + 引擎完全体（v2.2）已全部实现并入 main（组件级映射见 §14）。
> 阶段 7 的本地沙盒为 subprocess 隔离形态，gVisor/microVM 为生产目标；阶段 8 的
> 影子双跑以行为代理指标对比实现，LLM 策略接入后同一接口测真实分布。

### 12.1 本轮实现范围（本次交付）

落地阶段 1–5 的核心引擎（L5 不写代码，仅本设计）：

- L1：锚定与三通道认证、参数边界与锁、预算池与熔断、审计脊柱、owner_override。
- L2：完整参数体系 + 分池累积调制 + 情绪衰减/事件。
- L3：归因器接口（启发式实现）+ 内化台账（双向）+ 策略库（ε 多样性）+ 目标仲裁。
- L4：状态注入器 + 情绪调制器（§7.4 全表）+ scratch 工作区约定。
- 评估：行为评估 harness（ParametricPolicy 仿真 + LLM 策略适配位）。
- 全量 pytest + 端到端 demo。

LangGraph 编排与 MongoDB 适配按 §4 的接口约定在后续阶段接入。

---

## 13. 设计原则（v1 六条保留 + v2 三条）

1. "像人"靠状态调制输出，不靠提示词；情绪影响检索和决策偏置，而非语气开关。
2. "主动"靠欲望缺口驱动，不靠定时器；缺口可量化、可衰减、可阻尼。
3. "成长"发生在倾向维度，不在能力维度；参数显式、有界、慢速定型。
4. "可控"靠规则与参数物理隔离；反馈调"多想做"，不调"能不能做"。
5. "自我进化"靠生成-验证-保留闭环；验证质量决定上限，权限随行为稳定性解锁。
6. 内核兜底而非日常决策；平时静默，触发时最高否决。
7. **（v2）信任按通道分级**：未认证的信号永远无法以全权重影响倾向。
8. **（v2）一切倾向可逆**：参数、内化、进化合并都有台账与回滚；不可逆的只有内核。
9. **（v2）一切可审计**：决策链哈希串联，事后可完整重构。

---

## 14. 实施状态（v2.1，2026-11-11）

> 状态：✅ 已实现并有测试钉死 · ⚗️ 接口就绪待外部系统接入 · 📋 仅设计

| 设计章节 | 模块 | 测试 | 状态 |
|---|---|---|---|
| §3.2 主人锚定 / 三通道认证 | `proto/kernel/anchor.py` | `test_anchor.py`（伪造/过期/重放/轮换会签/恢复仪式） | ✅ |
| §5 预算三分池 / 熔断 | `proto/kernel/budgets.py` | `test_kernel.py`（含未知单位不熔断） | ✅ |
| §3.3 高危拦截 / 静音 | `proto/kernel/kernel.py` | `test_kernel.py` | ✅ |
| §10 审计脊柱 | `proto/kernel/audit.py` | `test_audit.py`（篡改/删除断链检测，O(1) 追加） | ✅ |
| §6 参数体系 / 权威界 / 单一写者 | `proto/state/{schema,agent_state}.py` | `test_agent_state.py`（含权威界校验） | ✅ |
| §6.3 / §7.1 分池归因 / CONTENT 50% 硬顶 | `proto/state/modulation.py` | `test_modulation.py`（万次洪泛零触发） | ✅ |
| §7.4 PAD 情绪 | `proto/state/emotion.py` | `test_emotion.py` | ✅ |
| §7.2 双向内化台账 | `proto/metacog/internalize.py` | `test_metacog.py` | ✅ |
| §7.3 ε 多样性策略库 | `proto/metacog/strategies.py` | `test_metacog.py` | ✅ |
| §7.5 目标仲裁 / 预算隔离 | `proto/metacog/goals.py` | `test_metacog.py` | ✅ |
| §7.1 归因器（LLM 适配） | `proto/metacog/attribution.py` | `test_metacog.py` | ⚗️ 启发式已实现，`LLMAttributor` 接口位 |
| §7.4 / §8 情绪→认知调制器 / 注入器 | `proto/exec/{modulators,injector}.py` | `test_injector.py`（golden 全文锁定） | ✅ |
| §8 Scratch 工作区 / 审批队列 / 日上限 | `proto/exec/{workspace,approval}.py` | `test_stage6.py` | ✅ |
| §8 / §9.3 检查点（schema@variant 绑定） | `proto/exec/checkpoints.py` | `test_stage6.py` | ✅ |
| §6.2 行为评估 harness | `proto/evalx/harness.py` | `test_evalx.py`（ParametricPolicy 仿真策略） | ⚗️ LLM 策略适配位 |
| §9.1 测试集治理（OWNER 签名追加、无删除 API） | `proto/evolution/testset.py` | `test_stage7.py` | ✅ |
| §9.1 代码轨沙盒 | `proto/evolution/sandbox.py` | `test_stage7.py`（清洁环境/CPU 限制/超时） | ⚗️ 本地 subprocess 形态；gVisor/microVM 生产目标 |
| §5 私有评分器 | `proto/evolution/scorer.py` | `test_stage7.py` | ✅ |
| §9.3 变体档案库 / 进化树 | `proto/evolution/archive.py` | `test_stage7.py` | ✅ |
| §9 代码轨进化引擎（分数门+全过门→canary） | `proto/evolution/engine.py` | `test_stage7.py` / `test_stage8.py` | ✅ |
| §9.1 行为轨影子双跑 | `proto/evolution/shadow.py` | `test_stage8.py`（回归规则） | ✅ |
| §9.2 分级授权（低/中/高） | `proto/evolution/authorization.py` | `test_stage8.py` | ✅ |
| §11.2 参数调制回滚（级联内化降级） | `StateManager.rollback` | `test_round4_safety.py` | ✅ |
| §7.5 阻尼控制（提议被拒→阈值上调） | `proto/metacog/damping.py` + 审批队列接线 | `test_round4_safety.py` | ✅ |
| §8 Router/Worker 执行器 | `proto/exec/router.py`（工具注册/逐步检查点/审批暂停恢复/scratch 晋升） | `test_router.py` | ✅ 骨架；步骤生成待 LLM 策略 |
| L3 运行节奏 | `proto/metacog/scheduler.py`（时钟可注入） | `test_scheduler_distill.py` | ✅ |
| §7.3 策略蒸馏 | `proto/metacog/distill.py` | `test_scheduler_distill.py` | ⚗️ 确定性规则已实现，LLM 蒸馏接口位 |
| §7.3 机制五 兴趣强化收敛 | `proto/metacog/interest.py`（学习权重进仲裁） | `test_round4_dynamics.py` | ✅ |
| §6.2 dormant 参数冻结 | `proto/evalx/dormancy.py` + StateManager | `test_round4_dynamics.py` | ✅ |
| §6.1 欲望动态增长 | `proto/state/desires.py`（1h 宽限） | `test_round4_dynamics.py` | ✅ |
| §4 LangGraph 编排 | `proto/adapters/langgraph_runner.py`（attribute→modulate→respond→proactive 单轮图） | `test_integrations.py`（装 langgraph 后激活，图内调制实测） | ✅ 适配器 |
| §4 MongoDB 适配 | `proto/storage/mongo_store.py`（pymongo 懒连接 + `create_store` 工厂，`PROTO_STORE=mongo` 启用） | `test_integrations.py`（装 pymongo 后激活） | ✅ 适配器 |
| LLM 接入（OpenAI 兼容客户端） | `proto/llm/client.py`（方舟/OpenAI/DeepSeek/Ollama） | `test_llm.py`（本地 HTTP 假服务实测） | ✅ |
| §7.1 LLMAttributor（白名单+回退） | `proto/metacog/attribution.py` | `test_llm.py` | ✅ |
| LLM 任务规划器（工具白名单） | `proto/exec/planner.py` | `test_llm.py` | ✅ |
| Web 管理控制台 | `proto/web/`（stdlib http.server + 单页前端，127.0.0.1 + URL 片段令牌防 CSRF） | `test_web.py`；浏览器 E2E 实测（渲染/对话/调制/参数可视） | ✅ |
| 活的 Agent 运行时 + REPL | `proto/runtime/agent.py` + `proto/run.py` | `test_runtime.py`（注册/对话/调制/主动提议/阻尼/审批执行） | ✅ |
| §3.2 密钥托管 / 多设备 | 注册仪式已实现 | — | 📋 产品化阶段 |

**验证基线**：`python3 -m pytest -q` → 352 项全绿（+2 可选跳过）；`python3 -m proto.demo` → 12 段全链路演示，退出码 0；`python3 -m proto.run` → 离线冒烟通过。

**v2.3（2026-11-11）**：活的 Agent 运行时——`python -m proto.run` 即得：主人密钥首次注册/持久化（本地控制台 = OWNER 通道，每条输入走完整 HMAC 认证）、对话（状态注入 + LLM，未配 key 自动离线模式）、LLM 归因（参数白名单 + 强度夹紧 + 解析失败回退启发式）、主动提议（欲望缺口 × 调度器 × 仲裁 × 审批，拒绝即阻尼）、任务执行（LLM 规划过工具白名单 → Router 安全管线）。

**v2.4（2026-11-11）**：Web 管理控制台——`python -m proto.web` 一条命令部署与管理：对话、内在状态仪表盘（含锁定/覆写）、主动提议审批、任务指派、主人优先级、静音、审计链实时视图。仅绑定 127.0.0.1；一次性令牌置于 URL 片段（不发给服务器），受保护 API 强制请求头校验，防本机跨站调用。

**v2.2（2026-11-11）**：引擎完全体补完——参数调制回滚（级联内化降级）、阻尼控制接入审批队列、Router/Worker 执行器骨架（工具注册、逐步检查点、审批暂停/恢复、批准后 scratch 晋升）、L3 调度器（每任务蒸馏/定时目标生成/每日深度回顾，时钟可注入）、确定性策略蒸馏、兴趣强化收敛进目标仲裁、dormant 参数冻结看门狗、欲望缺口随闲置增长（1 小时宽限期）。设计内机制至此全部落地；剩余项均为外部系统接入（LLM/LangGraph/MongoDB/gVisor/密钥托管）。


**v2.5（2026-11-11）**：外部系统接入适配器——MongoStore（懒连接+环境变量工厂）、LangGraph 单轮对话图（节点=内置调度器等价物）、Sandbox runsc 后端（Linux 自动探测/macOS 回退）、Web 控制台固定令牌（长驻部署）。核心运行时保持零三方依赖，可选依赖组 `.[mongo]` / `.[graph]`。


**v2.6（2026-11-11）**：体检加固——元认知全量持久化（重启不失忆）、情绪事件接入运行时、暂停任务可恢复（检查点携带任务规格）、规矩内化与进化工作台进 UI、CI/备份/安装命令、evalx LLM 策略适配器。


**v2.7（2026-11-11）**：独处生活——服务端心跳线程（浏览器无关）、两段式自主行为（read_draft 草稿自主执行 + visible_write 晋升提案）、每日深度回顾实装（策略修剪+兴趣衰减+审计）、情绪随心跳衰减。§8 的「静默执行+提交审查」首次完整落地到无人值守场景。


**v2.8（2026-11-11）**：人设系统（持久化、注入对话/学习/目标）与 Web 感官（白名单只读、GitHub 搜索/README、CONTENT 防注入、web_fetch 审计）——独处学习从『凭记忆写草稿』升级为『真读开源项目写带来源笔记』。


**v2.9（2026-11-11）**：四维加固——API 异常兜底与单实例锁（可靠性）、审计验证缓存（性能）、恒时 token 比较与策略注入边界+长度上限（安全，封堵 web→策略→上下文注入链）、对话记忆与收件箱（人味：独处消息真正到达主人）。


**v3.0（2026-11-11）**：长期记忆流（§人味的根本补全）——情景/事实/反思三类记忆、相关×时近×重要召回、强化与 72h 半衰遗忘、事实抽取与独处反思（Generative Agents 式），独处行为从学习外部升级为同时自省经历。


**v3.1（2026-11-11）**：智谱 Coding Plan 接入——Anthropic Messages 兼容客户端、提供商预设与协议自动识别、双模型栈（GLM-5.3 主对话 + GLM-5.3-Flash 跑抽取/反思/学习/规划等高频低风险调用）、llm 自检命令。


**v3.2（2026-11-11）**：一键部署——deploy.sh 引导（venv+安装+向导）、proto.setup 交互/参数双模式（含 launchd 开机自启）、proto.env 自动加载（四入口）。


**v3.3（2026-11-11）**：能力跃升——工作区信任边界与 workspace_write 分级（§3.3 扩展）、编码工具族（文件/检索/沙盒 run_python）、观察-行动 Agentic 循环（暂停可恢复）、外部编码 agent 桥（claude/codex/zcode）。类人（状态/记忆/主动）与能力（工具/循环）从此并重。
