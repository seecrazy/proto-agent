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

    print("=" * 58)
    print(" PROTO 部署向导")
    print("=" * 58)

    def ask(prompt: str, default: str = "") -> str:
        if args.yes:
            return default
        try:
            answer = input(prompt).strip()
        except EOFError:
            return default
        return answer or default

    provider = args.provider
    if not provider:
        print("\n[1/4] 选择 LLM 提供商：")
        print("  1) 智谱 Coding Plan（GLM-5.3 系列，推荐）")
        print("  2) 自定义 OpenAI 兼容端点")
        print("  3) 离线模式（稍后配置）")
        choice = ask("选择 [1/2/3，默认1]: ", "1")
        provider = {"1": "zhipu-coding", "2": "", "3": "offline"}[choice]
        if provider == "offline":
            provider = ""
    api_key = args.api_key
    if provider and not api_key:
        api_key = ask("API Key（输入后不回显，直接粘贴）: ")
    model = args.model or ("glm-5.3" if provider == "zhipu-coding" else "")
    fast_model = args.fast_model
    if provider == "zhipu-coding" and not fast_model:
        fast_model = "glm-5.3-flash"
    token = args.token or ask("[2/4] 控制台固定访问令牌（直接回车自动生成）: ",
                              "proto-console")
    persona = args.persona or ask("[3/4] 人设（如：资深软件专家，回车跳过）: ")
    priority = args.priority or ask("      主人优先级（回车跳过）: ")

    print("\n[4/4] 写入配置并自检……")
    result = run_setup(data_dir=Path(args.data_dir), provider=provider,
                       base_url=args.base_url, api_key=api_key, model=model,
                       fast_model=fast_model, token=token, persona=persona,
                       priority=priority, yes=True,
                       check_llm=not args.skip_llm_check)
    print(f"  配置文件 : {result['env_file']}（0600）")
    if result["llm_configured"]:
        print(f"  LLM 自检 : {'通过' if result['llm_ok'] else '失败——请检查 key/网络，可重跑本向导'}")
    else:
        print("  LLM     : 离线模式")
    if args.install_service:
        if sys.platform == "darwin":
            env_values = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}
            plist = install_launchd(Path(args.data_dir), result["token"], env_values)
            print(f"  开机自启 : 已安装 {plist}")
            print("            停止: launchctl unload " + str(plist))
        else:
            print("  开机自启 : 当前平台不支持（macOS 专用），跳过")
    print("\n日常启动：")
    print(f"  source {result['env_file']} && python3 -m proto.web --data-dir {args.data_dir}")
    print(f"  控制台  : http://127.0.0.1:8420/#token={result['token']}")
    print(f"  备份提醒: python3 -m proto.backup --data-dir {args.data_dir}（owner.key 即身份）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
