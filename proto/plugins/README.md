# proto/plugins — 工具插件（ToolPack）

## 新增一个工具 = 一个 pack 文件 + 进装配清单

以前新增工具要改 4 处（实现、`contracts._BUILTINS` schema、agent 装配段、agentic 三个
硬编码清单）。M78 之后全部声明收在 `ToolSpec` 一处：

```python
# proto/plugins/pack_my.py
from proto.plugins.base import ToolSpec

def specs(ctx) -> list:
    return [
        ToolSpec(
            name="my_tool",
            fn=my_function,              # 实现（通常放 proto/tools/）
            category="WORKSPACE_WRITE",  # 能力级别（字符串或 ActionCategory）
            description="给模型看的一句话说明",
            parameters={...},            # 可省：回落 contracts.builtin_parameters，再省则按签名推导
            owner_only=False,            # True=主人超时也绝不自主代批
            read_only=True,              # 只读探索步计数（连续探索强制转向产出）
            obs_limit=2000,              # 观察截断字符数（缺省 600）
            background_io=True),         # 耗时 I/O，执行时切后台不阻塞对话
    ]
```

然后在 `proto/plugins/__init__.py` 的 `assemble_tools` 组合处加一行
`pack_my.specs(...)`。

## 内置 pack（首批，行为与 M78 前逐一等价——tests/test_plugins.py 钉值）

| pack | 工具 |
|---|---|
| `pack_core` | clock / scan_style / polish_style |
| `pack_files` | read_file / read_files / write_file / edit_file / list_dir / grep / search_symbols / explore_code / read_skill / inspect_build / run_python / run_command |
| `pack_web` | web_fetch / github_search / github_readme |
| `pack_fleet` | blackboard_read / blackboard_post / fleet_status / company_context / company_inbox / company_send |
| `pack_bridge` | delegate_coding（探测到外部 CLI 才注册） |

## 安全边界（不因插件化放松）

- **category 只由 ToolSpec 声明**；模型在决策里自行申报的类别只能取更高者
  （`exec/router.py` `_effective_category`），不能降级。
- **owner_only 工具**（如 delegate_coding、未隔离的 run_python/run_command）
  属"较高权限"：主人超时也绝不自主代批。
- parameters 无论来自声明、`_BUILTINS` 还是签名推导，都过同一套 schema 校验。
- `BUILTIN_PACKS` 是显式清单——不做自动发现、不接受数据目录投递的工具包。
  L1 内核（kernel/）不感知插件。
