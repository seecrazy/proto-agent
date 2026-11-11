# -*- coding: utf-8 -*-
"""M15: 部署向导——python3 -m proto.setup（交互式或纯参数式）。

产物：
  proto_data/proto.env   全部配置（0600，含 key 与固定 token）
  ~/Library/LaunchAgents/com.proto.agent.plist（可选，macOS 开机自启）
引导：LLM 提供商选择（智谱 Coding Plan 推荐）→ 连通性自检 →
人设/优先级 → 服务安装（可选）→ 备份提醒。
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from proto.config import load_env_file

PERSONA_PRESETS = [
    ("软件专家", "资深软件专家，重视工程质量与可验证性，喜欢把复杂问题拆解清楚"),
    ("技术导师", "耐心的技术导师，善于把复杂概念讲得通俗易懂，重视学习者的理解节奏"),
    ("安全研究员", "严谨的安全研究员，关注攻击面与缓解措施，对信任边界敏感"),
    ("产品全栈", "产品型全栈工程师，关注用户体验与交付价值，反对过度设计"),
    ("毒舌评审", "毒舌但公正的代码评审员，直言不讳但每句批评都给出依据"),
]

WIZARD_STEPS = ["provider", "api_key", "llm_detail", "token", "persona", "priority", "confirm"]

PRIORITY_EXAMPLES = [
    "补齐支付模块的单元测试",
    "学习 agent 编排框架",
    "把个人博客迁移到新框架",
    "调研数据备份方案",
]


def mask_key(key: str) -> str:
    return f"{key[:3]}****{key[-4:]}" if len(key) > 8 else "****"


_pos_index = [0]  # 当前步骤序号（_run_step 内的 UI 输出用）


def step_ui(n: int, total: int, text: str) -> str:
    from proto import ui
    return ui.step(n, total, text)


def dim_text(text: str) -> str:
    from proto import ui
    return ui.dim(text)


def ok_line(text: str) -> str:
    from proto import ui
    return ui.ok(text)


def run_wizard(ask, secure_ask, initial=None) -> dict:
    """可回退多步向导：任意步输入 < 回上一步；确认页掩码显示并支持数字跳步。

    ask(prompt, default)  普通输入（回车取默认）
    secure_ask(prompt)    机密输入（getpass，不回显）
    """
    values = {"provider": "", "base_url": "", "api_key": "", "model": "",
              "fast_model": "", "token": "proto-console",
              "persona": "", "priority": ""}
    values.update({k: v for k, v in (initial or {}).items() if v})
    i = 0
    while i < len(WIZARD_STEPS):
        step = WIZARD_STEPS[i]
        _pos_index[0] = i + 1
        pos = f"[{i + 1}/{len(WIZARD_STEPS)}]"
        result = _run_step(step, pos, values, ask, secure_ask)
        if result == "back":
            i = max(0, i - 1)
            continue
        if isinstance(result, int):      # 确认页跳步
            i = result
            continue
        i += 1
    return values


def _run_step(step, pos, values, ask, secure_ask):
    if step == "provider":
        hint = {"zhipu-coding": "（已选 智谱 Coding Plan）", "": ""}.get(values["provider"], "")
        print()
        print(step_ui(_pos_index[0], len(WIZARD_STEPS), f"选择 LLM 提供商 {hint}"))
        print("  1) 智谱 Coding Plan（GLM-5.3 系列，推荐）")
        print("  2) 自定义 OpenAI 兼容端点")
        print("  3) 离线模式（稍后配置）")
        default = {"zhipu-coding": "1", "": "3"}.get(values["provider"], "2")
        answer = ask("选择 [1/2/3，回车保持默认]（< 回上一步）: ", default)
        if answer == "<":
            return "back"
        values["provider"] = {"1": "zhipu-coding", "2": "", "3": ""}.get(
            answer, values["provider"] or "")
        values["offline_menu"] = answer == "3" or not values["provider"]
        return None

    if step == "api_key":
        if not values["provider"]:
            return None                   # 离线/自定义无 key 步（自定义在 llm_detail 问）
        print(step_ui(_pos_index[0], len(WIZARD_STEPS), "API Key（getpass 输入，屏幕不回显）"))
        key = secure_ask("粘贴 API Key（< 回上一步）: ")
        if key == "<":
            return "back"
        values["api_key"] = key
        print(ok_line(f"已收到 key：{mask_key(key)}（确认页可回改）"))
        return None

    if step == "llm_detail":
        if not values["provider"] and values.get("offline_menu"):
            return None                    # 离线模式：跳过
        if values["provider"] == "zhipu-coding":
            model = ask("主模型 [glm-5.3]: ", values["model"] or "glm-5.3")
            if model == "<":
                return "back"
            fast = ask("快模型（抽取/反思/学习/规划）[glm-5.3-flash]: ",
                       values["fast_model"] or "glm-5.3-flash")
            if fast == "<":
                return "back"
            values["model"], values["fast_model"] = model, fast
            return None
        if values["provider"]:
            return None
        base = ask("Base URL（OpenAI 兼容，如 https://api.deepseek.com/v1）: ",
                   values["base_url"])
        if base == "<":
            return "back"
        values["base_url"] = base
        key = secure_ask("API Key（不回显，本地服务直接回车）: ")
        if key == "<":
            return "back"
        values["api_key"] = key
        model = ask("模型名: ", values["model"] or "gpt-4o-mini")
        if model == "<":
            return "back"
        values["model"] = model
        return None
        # 离线模式：跳过

    if step == "token":
        print()
        print(step_ui(_pos_index[0], len(WIZARD_STEPS), "控制台固定访问令牌（重启不变的访问口令）"))
        token = ask(f"令牌 [{values['token']}]（< 回上一步）: ", values["token"])
        if token == "<":
            return "back"
        values["token"] = token
        return None

    if step == "persona":
        print()
        print(step_ui(_pos_index[0], len(WIZARD_STEPS), "人设（数字选预设，或 6 自由填写；回车跳过）"))
        for idx, (name, text) in enumerate(PERSONA_PRESETS, 1):
            mark = " ←当前" if values["persona"] == text else ""
            print(f"  {idx}) {name}{mark}")
        print("  6) 其他（自由填写）")
        answer = ask("选择 [1-6]: ", "")
        if answer == "<":
            return "back"
        if answer.isdigit() and 1 <= int(answer) <= len(PERSONA_PRESETS):
            values["persona"] = PERSONA_PRESETS[int(answer) - 1][1]
        elif answer == "6":
            free = ask("描述你的人设（一句话即可）: ", values["persona"])
            if free == "<":
                return "back"
            values["persona"] = free
        elif answer:                        # 菜单处直接输入文本 = 自由人设
            values["persona"] = answer
        return None

    if step == "priority":
        from proto import ui
        print()
        print(step_ui(_pos_index[0], len(WIZARD_STEPS), "主人优先级 —— 它最近围绕什么为你工作？"))
        print(dim_text(
            "优先级会注入三处：独处学习的选题（GitHub 搜索词）、目标仲裁的对齐项"
            "（0.35 第一权重，跑偏的事它不做）、工作草稿的方向。\n"
            "写你『最近在意的事』即可——一句话，不追求正式；随时可在控制台修改。"))
        print("示例：")
        for idx, example in enumerate(PRIORITY_EXAMPLES, 1):
            mark = " ←当前" if values["priority"] == example else ""
            print(f"  {idx}) {example}{mark}")
        priority = ask("输入数字选示例，或直接输入你的优先级（回车=暂不设置）: ",
                       values["priority"])
        if priority == "<":
            return "back"
        if priority.isdigit() and 1 <= int(priority) <= len(PRIORITY_EXAMPLES):
            values["priority"] = PRIORITY_EXAMPLES[int(priority) - 1]
        else:
            values["priority"] = priority
        return None

    # confirm
    from proto import ui
    print()
    print(ui.bold(ui.cyan("确认配置")))
    print(ui.dim("回车=写入并完成 · 数字=跳回该步重选 · < =上一步"))
    provider_label = values["provider"] or (values["base_url"] or "离线模式")
    print(ui.kv_table([
        ("1 提供商", provider_label, None),
        ("2 API Key", mask_key(values["api_key"]) if values["api_key"] else "（无）", ui.yellow),
        ("3 模型", (values["model"] or "默认")
         + (f" / 快: {values['fast_model']}" if values["fast_model"] else ""), None),
        ("4 令牌", values["token"], None),
        ("5 人设", values["persona"] or "（未设置）", None),
        ("6 优先级", values["priority"] or "（未设置）", None),
    ], key_width=12))
    answer = ask("确认 [回车/1-6/<]: ", "")
    if answer == "<":
        return "back"
    if answer.isdigit() and 1 <= int(answer) <= 6:
        return int(answer) - 1            # 跳到对应步骤（WIZARD_STEPS 前 6 项）
    return None


ENV_KEYS = ("PROTO_LLM_PROVIDER", "PROTO_LLM_BASE_URL", "PROTO_LLM_API_KEY",
            "PROTO_LLM_MODEL", "PROTO_LLM_FAST_MODEL", "PROTO_WEB_TOKEN",
            "PROTO_CAFFEINATE")


def env_file_for(data_dir: Path) -> Path:
    return Path(data_dir) / "proto.env"


def ensure_env_file(data_dir: Path, provider=None, base_url=None, api_key=None,
                    model=None, fast_model=None, token=None) -> Path:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "PROTO_LLM_PROVIDER": provider or "",
        "PROTO_LLM_BASE_URL": base_url or "",
        "PROTO_LLM_API_KEY": api_key or "",
        "PROTO_LLM_MODEL": model or "",
        "PROTO_LLM_FAST_MODEL": fast_model or "",
        "PROTO_WEB_TOKEN": token or "",
    }
    lines = ["# PROTO 部署配置（由 proto.setup 生成；含密钥，权限 0600）"]
    for key in ENV_KEYS:
        value = values.get(key)
        if value:
            lines.append(f"export {key}={shlex.quote(value)}")
        elif key in os.environ and os.environ[key]:
            lines.append(f"export {key}={shlex.quote(os.environ[key])}")
    path = env_file_for(data_dir)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def plist_path_for() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.proto.agent.plist"


def build_plist(data_dir: Path, token: str, python_bin: str = None,
                env_values: dict = None) -> dict:
    data_dir = Path(data_dir).resolve()
    python_bin = python_bin or sys.executable
    if env_values is None:
        env_values = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}
    return {
        "Label": "com.proto.agent",
        "ProgramArguments": [python_bin, "-m", "proto.web",
                             "--data-dir", str(data_dir),
                             "--token", token, "--no-open"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(Path.cwd()),
        "EnvironmentVariables": env_values or {},
        "StandardOutPath": str(data_dir / "proto-web-service.log"),
        "StandardErrorPath": str(data_dir / "proto-web-service.log"),
    }


def install_launchd(data_dir: Path, token: str, env_values: dict = None) -> Path:
    path = plist_path_for()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(build_plist(data_dir, token, env_values=env_values), f)
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(path)], check=True,
                   capture_output=True)
    return path


def run_setup(data_dir: Path, provider=None, base_url=None, api_key=None,
              model=None, fast_model=None, token=None, persona=None,
              priority=None, yes=False, check_llm=True) -> dict:
    """安装核心（纯函数部分，可测）；交互外壳在 main() 里。"""
    data_dir = Path(data_dir)
    env_file = ensure_env_file(data_dir, provider, base_url, api_key,
                               model, fast_model, token)
    load_env_file(env_file)
    from proto.llm.client import create_llm_stack
    stack = create_llm_stack()
    llm_ok = None
    if stack is not None and check_llm:
        try:
            stack["main"].complete("你是连通性探针。", "请只回复：在线")
            llm_ok = True
        except Exception:
            llm_ok = False
    if persona or priority:
        from proto.exec.webtools import WebToolkit
        from proto.runtime.agent import ProtoAgent

        def dead_fetch(url, accept="text/html"):
            raise OSError("部署期禁网")

        agent = ProtoAgent(data_dir=data_dir, llm=stack["main"] if stack else None,
                           goal_interval_s=10 ** 9,
                           webtools=WebToolkit(fetch=dead_fetch))
        if persona:
            agent.set_persona(persona)
        if priority:
            agent.set_priority(priority)
    resolved_token = token or os.environ.get("PROTO_WEB_TOKEN", "")
    return {"env_file": env_file, "token": resolved_token,
            "llm_ok": llm_ok, "llm_configured": stack is not None}


def main() -> int:
    parser = argparse.ArgumentParser(prog="proto.setup", description="PROTO 部署向导")
    parser.add_argument("--data-dir", default="./proto_data")
    parser.add_argument("--provider", default="", help="如 zhipu-coding")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--fast-model", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--persona", default="")
    parser.add_argument("--priority", default="")
    parser.add_argument("--install-service", action="store_true",
                        help="安装 macOS launchd 开机自启服务")
    parser.add_argument("--skip-llm-check", action="store_true")
    parser.add_argument("--yes", action="store_true", help="非交互模式")
    args = parser.parse_args()

    from proto import ui
    print(ui.banner("PROTO 部署向导", f"v{_version()} · 任意步输入 < 回上一步"))

    cli_complete = (args.provider or args.base_url or args.api_key) and args.yes
    if cli_complete:
        values = {"provider": args.provider, "base_url": args.base_url,
                  "api_key": args.api_key, "model": args.model,
                  "fast_model": args.fast_model, "token": args.token,
                  "persona": args.persona, "priority": args.priority}
    else:
        import getpass

        def ask(prompt: str, default: str = "") -> str:
            try:
                answer = input(prompt).strip()
            except EOFError:
                answer = ""
            return answer or default

        values = run_wizard(
            ask=ask, secure_ask=getpass.getpass,
            initial={"provider": args.provider, "base_url": args.base_url,
                     "api_key": args.api_key, "model": args.model,
                     "fast_model": args.fast_model, "token": args.token,
                     "persona": args.persona, "priority": args.priority})

    print()
    print(ui.step(7, 7, "写入配置并自检"))
    result = run_setup(data_dir=Path(args.data_dir),
                       provider=values.get("provider"),
                       base_url=values.get("base_url"),
                       api_key=values.get("api_key"),
                       model=values.get("model"),
                       fast_model=values.get("fast_model"),
                       token=values.get("token"), persona=values.get("persona"),
                       priority=values.get("priority"), yes=True,
                       check_llm=not args.skip_llm_check)
    print(ui.ok(f"配置文件 {result['env_file']}（权限 0600）"))
    if result["llm_configured"]:
        if result["llm_ok"]:
            print(ui.ok("LLM 自检通过"))
        else:
            print(ui.warn("LLM 自检失败——请检查 key/网络，可重跑向导（配置已写入）"))
    else:
        print(ui.warn("离线模式（无 LLM 配置）——功能受限，建议稍后配置"))
    if args.install_service:
        if sys.platform == "darwin":
            env_values = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}
            plist = install_launchd(Path(args.data_dir), result["token"], env_values)
            print(ui.ok(f"开机自启已安装：{plist}"))
            print(ui.dim(f"    停止：launchctl unload {plist}"))
        else:
            print(ui.warn("当前平台不支持 launchd（macOS 专用），跳过"))
    print()
    print(ui.bold("日常启动"))
    print(ui.kv_table([
        ("命令行", f"python3 -m proto.web --data-dir {args.data_dir}", None),
        ("控制台", f"http://127.0.0.1:8420/#token={result['token']}", ui.cyan),
        ("备份", f"python3 -m proto.backup --data-dir {args.data_dir}", None),
        ("身份", "owner.key 即主人身份，请备份", ui.yellow),
    ]))
    return 0


def _version():
    from importlib.metadata import version, PackageNotFoundError
    try:
        return version("proto-agent")
    except PackageNotFoundError:
        return "dev"


if __name__ == "__main__":
    raise SystemExit(main())
