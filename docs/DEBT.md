# 结构债账本（Known Debt Register)

> 规则：任何"已知未修"的问题必须在本账本登记——未登记的拖延是流程违规。
> 每条债必须写清：触发条件（什么情况下会咬人）、风险、修复方案、建议时机。
> 修复后移入底部"已偿还"区，保留记录。

## D2 · force_act 失败文案"预算/条件不足"误导

- **现状**：行动未产生提议时统一报"预算/条件不足"，实际原因可能是冷却/门控/无目标。
- **触发条件**：主人点"立即行动"失败时看到误导文案。
- **风险**：低（仅文案），但损害信任——用户按文案去充值，钱花了问题还在。
- **修法**：force_act 返回具体 reason 分支，前端按分支提示。
- **建议时机**：下次触碰 force_act/前端按钮时顺手修；或专门 30 分钟。
- **登记**：v0.30.x 期。

## D3 · method_invent 每轮重复发明方法

- **现状**：同名去重存在，但 LLM 每次起略不同的名字（repo_pattern_mine / repo_pattern_mining），15 分钟限频挡不住 30 分钟节律的心跳，每轮 method_invent 白烧一次快模型调用。
- **触发条件**：每次 work_on_goal 前的方法策展。
- **风险**：低（浪费小额 token + 审计噪音）。
- **修法**：发明 prompt 注入已有方法清单（相似名视为已存在）；或相似度>0.8 拒绝入库。
- **建议时机**：与 D2 同批。
- **登记**：M45 期。

## D5 · run_python 沙盒 profile 仅覆盖 macOS sandbox-exec

- **现状**：Linux（runsc）路径存在但无 CI/无本机验证；sandbox-exec profile 对新工具链（如 swift concurrent runtime 的 aux 进程）可能过紧。
- **触发条件**：换机器/换工具链。
- **风险**：低（单机产品当前明确 macOS）。
- **建议时机**：Linux 部署需求出现时。
- **登记**：M36 期。

## 已偿还

- ~~D1 · mut_lock 整段持锁~~（2026-11-19 偿还，用户"现在就修"）：落地为 **per-agent 单写者锁 + 主人快通道**而非字面队列线程——锁的正确语义就是单写者，队列线程只是把同样的争用换个地方。变更：①mut_lock 全局 → per-agent ops_lock（A 的长链不再挡全舰队，心跳逐 agent try-lock）；②快通道免锁扩容（mute/awake/refill/priority 文本/pause/select/goals drop），安全性由资源级 RLock 支撑（BudgetPools/GoalTree/ApprovalQueue，M43/M49 的 spine RLock 同理）；③set_priority 拆分：文本即存（免锁），目标播种（LLM）推迟到空闲心跳补做。同 agent 的长操作（chat/task/evolve/decide-resume）仍串行=单写者语义，409 提示已改为"不影响其他 agent"。顺手修 drop_goal 在无 projects 目录时 500 的潜伏 bug。tests/test_m50_per_agent_locks.py 6 项，全套 599 绿。

- ~~D4 · local_build_check 未实证~~（2026-11-19 偿还，账本建立当天：实测发现沙盒确挡 xcrun——DARWIN 用户级临时/缓存目录（/var/folders/…，不受 TMPDIR 控制）未放行；profile 按 confstr CS_DARWIN_USER_TEMP_DIR/CACHE_DIR 动态加白后 `swiftc -typecheck` 沙盒内 exit 0，遏制测试全绿。这条登记时标注"拖延最久、正是等暴露的坏例子"，当天偿还。）
- ~~审计链并发覆写~~（M43 修复：revision 重载 + reseal；进程内线程竞态 M49 补 RLock）
- ~~紧急控制与长链抢锁~~（M49：CONTROL_POSTS 免锁直通）
- ~~预算计费失真~~（M48：API usage 实报实销）
- ~~read_file 观察截断读不全~~（M47/M48：分级+分页+批量）
- ~~工具参数错整链暴毙~~（M48：单步失败韧性）
