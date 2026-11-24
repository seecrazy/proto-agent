# M82 编码循环可见性与 iOS 交付可达性 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让执行循环在修编译错误时同时看得见诊断与文件正文，并让 iOS 应用的模拟器构建与 XCTest 在沙盒内可达。

**Architecture:** 命令观察由纯函数整形（诊断优先）；上下文压缩改为按信息类别保留；重复验证命令只读步之间重放；新增 `ios-app-project` 技能自带的标准库 Xcode 工程生成器，并补齐 xcodebuild 的宏沙盒旗标与四条本机 IPC 放行。

**Tech Stack:** Python 3.9 标准库；pytest；macOS sandbox-exec / xcodebuild（可选）。

**Spec:** `docs/specs/2026-11-24-coding-loop-visibility.md`

## Global Constraints

- Python 3.9 语法（系统解释器为 3.9.2，沙盒内执行同一解释器）；零第三方运行时依赖。
- 不更换执行模型、不开放 TCP/IP、不放宽 owner-only/不可逆/可见写审批；模拟器 `.app` 与真实测试回执仍是交付硬条件。
- 每个任务结束跑 `python3 -m pytest -q tests/<本任务测试文件>` 与相关既有测试文件，并本地提交到 `main`。

## Review Focus

1. 诊断行里的绝对路径很长（`/private/var/…/file.swift:7:6`）——应裁成工位相对路径，否则 12 条就撑爆摘要预算。（任务 1）
2. 成功的命令（`xcodebuild -list`、`simctl list devices`）关键信息在输出头部——整形不能只留尾部。（任务 1）
3. 同一文件在一轮里被多次读取——只保留最新的一次，旧读取即使未过期也不重复占预算。（任务 2）
4. 被重放的构建命令此前失败——重放观察必须带回错误摘要，而不是只说「结果相同」。（任务 3）
5. 生成器遇到中文路径、带空格的文件名、符号链接目录——应正确加引号或拒绝，不能产出损坏的工程。（任务 6）

---

### Task 1: 命令观察整形

**Files:**
- Create: `proto/exec/observations.py`
- Modify: `proto/exec/agentic.py:189-262`（`_observe`）
- Test: `tests/test_m82_coding_loop_visibility.py`

**Interfaces:**
- Produces: `strip_ansi(text) -> str`；`diagnostics(text, *, base="", limit=12, budget=2400) -> List[str]`；`command_view(result: Dict, limit: int = 4000, *, base: str = "") -> str`；`failure_key(result: Dict, *, base: str = "") -> str`。

- [x] **Step 1: 写失败测试** `test_command_view_puts_diagnostics_first`：`swift build` 输出以 SwiftPM 缓存警告开头、末尾才有 `Sources/Wengu/Models.swift:7:6: error: 'Unique' …`；断言视图首行 `退出码 1（nonzero_exit）`，`错误摘要` 段含 `Models.swift:7:6: error:` 与源码摘录行，警告只出现在尾部段；`base` 为绝对工位前缀时输出不含该前缀。`test_command_view_keeps_head_for_success`：`-list` 输出含 `Schemes:` 与 `FixtureApp`，两处都在视图里。`test_diagnostics_covers_xcodebuild_pytest_python_npm`：四类输出各识别出关键行。
- [x] **Step 2: 跑测试确认失败**（`ImportError: proto.exec.observations`）
- [x] **Step 3: 实现** `observations.py`，并在 `_observe` 里对 `run_command`/`run_python` 的字典结果用 `command_view(observation, self._agent.tools.obs_limit(tool), base=<工位绝对路径>)` 代替 `_clip` 的字典字符串。
- [x] **Step 4: 跑测试通过；跑 `tests/test_engineering_skills.py`、`tests/test_m48_capability.py` 确认既有契约不受影响**
- [x] **Step 5: 提交** `feat(exec): 命令观察诊断优先——退出码/错误摘要/输出尾部`

### Task 2: 压缩策略按类别保留

**Files:**
- Modify: `proto/exec/work_context.py:8-11,86-147`
- Test: `tests/test_m82_coding_loop_visibility.py`

**Interfaces:**
- Produces: 常量 `MAX_HISTORY_BYTES = 24*1024`、`COMPACT_HISTORY_BYTES = 12*1024`、`TIGHT_HISTORY_BYTES = 3*1024`、`MAX_ITEM_BYTES = 6000`；`actor_input_budget()` 默认 65536；`observation_target(text) -> Tuple[str, str]`（`(tool, path)`，解析不了返回 `("", "")`）；`_compact_observations(observations, trace, tight)` 返回按时间顺序的列表。

- [x] **Step 1: 写失败测试** `test_compact_keeps_recent_file_bodies_and_errors`：观察序列为「读 Models.swift（1.5KB）→ 读 WenguApp.swift（5KB）→ 构建失败（错误摘要 2 条）」，`decision_input(...)` 输出同时含两份文件正文开头标记与两条 `error:`；`test_compact_marks_stale_reads_after_write`：读 A → edit_file A → 构建，输出中 A 的读取带「已过期」；`test_compact_keeps_only_latest_read_of_same_file`；`test_budgets_match_spec`（四个常量与默认预算）；`test_tight_context_still_folds_all_bodies`。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**：从最新观察向前遍历，按 `observation_target` 分类与预算裁剪，命令 ≤3000、最近 2 个文件 ≤6000、其余 ≤800，过期读取折叠为指针；`render_history` 的单项上限改用 `MAX_ITEM_BYTES`。
- [x] **Step 4: 跑新测试与 `tests/test_m71_continuity.py tests/test_m77_scoped_exploration.py tests/test_subagent.py`；若有断言与新契约冲突，按规格显式迁移并在提交说明里列出**
- [x] **Step 5: 提交** `feat(exec): 压缩按类别保留——错误摘要、最近文件正文、过期读取失效`

### Task 3: 重复验证重放与判重

**Files:**
- Modify: `proto/exec/agentic.py:238-253,654-721`
- Test: `tests/test_m82_coding_loop_visibility.py`

**Interfaces:**
- Produces: `AgenticLoop._repeated_verification(tool, arguments) -> Optional[dict]`（返回可重放的轨迹行）；`AgenticLoop._observe_replay(row)`。

- [x] **Step 1: 写失败测试** `test_repeated_verification_replays_after_read_only_steps`：`swift build`（失败）→ `read_file` → 同一 `swift build`，断言 `run_command` 只真实执行一次、第二次观察含「与第 1 步结果相同」且带回原 `error:` 摘要；`test_repeated_verification_reruns_after_write`：中间有 `edit_file` → 真实执行两次；`test_fail_repeat_key_tracks_diagnostics`。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**：只读判定用 `self._agent.tools.read_only_tools()`；`ios_required` 时不重放；`fail_repeat` 的键改为 `failure_key`。无落盘步数到 8 时追加一句指向错误摘要第一条的提示。
- [x] **Step 4: 跑新测试与 `tests/test_m45_*.py tests/test_m80_journal_activity.py`**
- [x] **Step 5: 提交** `feat(exec): 只读步之间重复验证不重跑，判重看诊断`

### Task 4: 工位清单与接口轮廓

**Files:**
- Modify: `proto/tools/codetools.py:209-237`（`list_dir`）、`proto/plugins/pack_files.py:48-52`（`list_dir` 观察上限 2000）、`proto/exec/agentic.py:727-770`（工位文件块 1400 字符、轮廓块）
- Create: `proto/exec/outline.py`
- Test: `tests/test_m82_coding_loop_visibility.py`

**Interfaces:**
- Produces: `outline.swift_outline(text) -> List[Tuple[str, List[str]]]`；`outline.source_outline(workspace, project_dir, *, budget=2400) -> str`（无源码返回空串）；`AgenticLoop._outline_block() -> str`（会话内按指纹缓存）。

- [x] **Step 1: 写失败测试** `test_list_dir_project_shows_nested_sources`；`test_prompt_block_lists_sources`（`session["prompt_blocks"]` 的工位文件块含 `Sources/Wengu/Models.swift`）；`test_outline_refreshes_on_change_and_is_bounded`（新增方法后固定区出现新签名；超过 2400 字节被截断并带提示；非代码工位无轮廓块）。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**：`list_dir` 深度相对 `base`（上限 2）；`_prepare_prompt_blocks` 的清单给 1400 字符并注明是开工快照；`_fixed_prompt` 末尾追加 `[工位接口轮廓]`。
- [x] **Step 4: 跑新测试与 `tests/test_m45_*.py tests/test_m48_capability.py tests/test_plugins.py`**
- [x] **Step 5: 提交** `feat(exec): 工位清单可见源码，固定区附带接口轮廓`

### Task 5: 沙盒放行与 xcodebuild 旗标

**Files:**
- Modify: `proto/tools/codetools.py:274-293`（profile）、`339-426`（`run_command`）
- Test: `tests/test_m82_coding_loop_visibility.py`

- [x] **Step 1: 写失败测试** `test_xcodebuild_gets_macro_sandbox_flag_under_sandbox_exec`（`build`/`test` 动作追加 `OTHER_SWIFT_FLAGS=-Xfrontend -disable-sandbox`；已带 `OTHER_SWIFT_FLAGS=v` 时变成 `v -Xfrontend -disable-sandbox`；`-list` 不加；非 sandbox-exec 后端不加）；`test_sandbox_profile_adds_only_local_ipc_rules`（含四条放行；仍含 `(deny file-write*)` 与 `(deny network*)`；工作区外写入仍被拒——用真实 sandbox-exec 写临时目录外文件断言 `PermissionError`，无 sandbox-exec 则跳过）。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**
- [x] **Step 4: 跑新测试与 `tests/test_m49b_*.py tests/test_m80_journal_activity.py` 及 `grep -rl sandbox proto tests` 列出的沙盒测试**
- [x] **Step 5: 提交** `fix(sandbox): xcodebuild 宏插件沙盒旗标 + 测试通道本机 IPC 放行`

### Task 6: `ios-app-project` 技能与生成器

**Files:**
- Create: `proto/skills/ios-app-project/SKILL.md`、`proto/skills/ios-app-project/make_xcodeproj.py`（已有初版）
- Modify: `proto/skills/__init__.py`（`_NAMES`、`read_skill` 增 `scripts`）、`pyproject.toml`（`"*/*.py"` 包数据）、`proto/exec/engineering.py:73-136`（`inspect_build` 提示）、`proto/runtime/agent.py:1145-1147`（交付阶段提示）
- Test: `tests/test_ios_app_project.py`、`tests/test_m82_coding_loop_visibility.py`

**Interfaces:**
- Produces: `make_xcodeproj.load_spec(path, root)`、`build(spec, root) -> (doc, project, app, tests)`、`generate(manifest, root) -> dict`、`main(argv=None) -> int`；`read_skill(name)` 返回值新增 `scripts: Dict[str, str]`（脚本名→绝对路径，无脚本为空字典）；`inspect_build` 报告新增 `hints: List[str]`。

- [x] **Step 1: 写失败测试**（`tests/test_ios_app_project.py`）`test_generated_project_passes_plutil_and_is_stable`（两次生成字节一致；`plutil -lint` 通过，无 plutil 则跳过）；`test_groups_mirror_directories_and_tests_target_links_host`；`test_spec_rejects_bad_inputs`（越界路径、缺 sources、同名 target、非法 info 键、bundle id 重复各报 `SpecError`）；`test_quotes_paths_with_spaces_and_non_ascii`；`test_catalog_lists_skill_with_script_path`；`test_inspect_build_hints_when_only_swift_package`；`test_delivery_task_mentions_project_skill`。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**；可选真实构建 `PROTO_TEST_XCODE=1` 时跑沙盒内 `xcodebuild … build` 断言退出 0 与 `.app`。
- [x] **Step 4: 跑新测试与 `tests/test_engineering_skills.py tests/test_textstyle.py tests/test_plugins.py`；`python3 -m build --wheel` 后确认 wheel 含 `proto/skills/ios-app-project/make_xcodeproj.py`**
- [x] **Step 5: 提交** `feat(skills): ios-app-project——专家自生成 .xcodeproj，iOS 交付结构可达`

### Task 7: 阻塞通知

**Files:**
- Modify: `proto/runtime/agent.py:1656-1680`（`_auto_recover_delivery`）及主人 retry 重置处
- Test: `tests/test_m82_coding_loop_visibility.py`

- [x] **Step 1: 写失败测试** `test_blocked_delivery_notifies_owner_once`（配额耗尽 → 收件箱恰一条含「需要你」；再跑心跳不重复；主人 retry 后标记清除）。
- [x] **Step 2: 跑测试确认失败**
- [x] **Step 3: 实现**（状态字段 `owner_notified`）
- [x] **Step 4: 跑新测试与 `tests/test_m81_autonomy_closures.py`**
- [x] **Step 5: 提交** `feat(delivery): 自动恢复配额耗尽时通知主人一次`

### Task 8: 真实验证与文档

- [x] 用生产同款模型配置在 Wengu 三文件副本上跑一个工作片：改造前零写入、`swift build` 失败；改造后 `swift build` 退出 0。记录步数与用量。
- [x] 部署生产（重启 8431），解锁专家目标，盯至少 3 个工作片并记录轨迹。
- [x] 更新 `docs/PROGRESS.md`（M82 条目与证据）、`docs/DESIGN.md` §7.3 与沙盒小节、`docs/DEBT.md`（D12 补充放行说明；冷启动模拟器首测挂起登记）、`README.md` 版本号。

### Task 9: 盯生产时追加的修复（均已在生产时间线里验证）

- [x] 诊断里的绝对路径按真实绝对前缀裁剪（舰队工作区根是相对路径，曾显示成假路径）。
- [x] 视野保留最近 4 个文件、压缩预算 16 KiB（2 个文件时模型循环重读 13 步）。
- [x] 不允许的命令指向正确的工具（模型两次用 `run_command` 跑 `grep`）。
- [x] 超长 `write_file` 报出长度与拆分办法；`note` 放进 `args` 时挪回（曾原样重交 7 次）。
- [x] 模型协议错误在执行日志里写明被打破的假设；修掉它引入的回归（`HTTPError` 的 `__getattr__` 对未知属性抛 `KeyError`，不能在失败处理路径里 `getattr`）。
