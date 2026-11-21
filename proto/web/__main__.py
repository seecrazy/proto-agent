# -*- coding: utf-8 -*-
"""PROTO Web 控制台入口：python3 -m proto.web

一条命令拉起本地管理界面（默认 http://127.0.0.1:8420，仅绑定本机回环）。
终端会打印带一次性令牌的完整 URL——令牌在 URL 片段里，不会发往服务器，
本机其他网页无法跨站调用受保护 API。

参数：--port 8420 --data-dir ./proto_data
LLM：与 proto.run 相同的环境变量（PROTO_LLM_BASE_URL / API_KEY / MODEL）。
"""
from __future__ import annotations

import argparse
import logging
import os
import webbrowser
from pathlib import Path

from proto.config import load_default_env
from proto.llm.client import create_llm_stack
from proto.runtime.agent import ProtoAgent
from proto.web.server import ProtoWebServer


def _persist_web_token(data_dir: Path, token: str) -> None:
    """首次生成的令牌持久化到 proto.env（0600）——重启不再换令牌，
    书签/已打开页面不因重启失效。持久化失败不阻断启动（仅本次随机）。"""
    env_path = Path(data_dir) / "proto.env"
    try:
        if env_path.exists():
            if "PROTO_WEB_TOKEN" in env_path.read_text(encoding="utf-8"):
                return
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\nexport PROTO_WEB_TOKEN='{token}'\n")
        os.chmod(env_path, 0o600)
    except OSError:
        pass


def _replace_web_token(env_path: Path, token: str) -> bool:
    """proto.env 内已有 PROTO_WEB_TOKEN 时原位替换为强令牌（原子写，0600）。
    返回是否写入成功；文件不存在返回 False。"""
    try:
        text = env_path.read_text(encoding="utf-8")
        if "PROTO_WEB_TOKEN" not in text:
            return False
        lines = [(f"export PROTO_WEB_TOKEN='{token}'\n"
                  if line.strip().startswith("export PROTO_WEB_TOKEN") else line)
                 for line in text.splitlines(keepends=True)]
        tmp = env_path.with_suffix(env_path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, env_path)
        return True
    except OSError:
        return False


def resolve_token(arg_token: str, data_dir: Path) -> str:
    """令牌解析顺序：命令行显式 > proto.env/环境 > 生成并持久化。
    注意 proto.env 必须先 load_default_env 加载后再调用（--token 的
    argparse 默认值捕获过早，拿不到 proto.env 里的令牌）。
    proto.env 里的弱令牌（<16 字符）按 M23.1 废除弱默认原则就地升级，
    启动日志提示主人用新 URL 重新打开。"""
    import secrets as _secrets
    token = (arg_token or "").strip() or os.environ.get("PROTO_WEB_TOKEN", "").strip()
    if token and len(token) >= 16:
        return token
    strong = _secrets.token_urlsafe(16)
    if token:   # 弱令牌：proto.env 原位升级；命令行显式弱令牌仅本次会话放行并提示
        replaced = _replace_web_token(Path(data_dir) / "proto.env", strong)
        if replaced:
            print("  令牌   : 检测到弱令牌，已自动更换强随机令牌并写回 proto.env")
            return strong
        return token
    _persist_web_token(data_dir, strong)
    return strong


def _open_fleet(data_root: Path, llm, fast_llm, models):
    from proto.runtime.manager import AgentManager

    legacy = (data_root / "owner.key").is_file()
    root = data_root / ".fleet" if legacy else data_root
    manager = AgentManager(root=root, llm=llm, fast_llm=fast_llm, models=models)
    if not manager.initialized:
        if legacy:
            manager.adopt(ProtoAgent(data_dir=data_root, llm=llm, fast_llm=fast_llm), name="默认")
        else:
            manager.create(name="主 Agent")
    return manager


def main() -> int:
    parser = argparse.ArgumentParser(prog="proto.web", description="PROTO Web 控制台")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--data-dir", default="./proto_data")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--caffeinate", action="store_true",
                        default=bool(os.environ.get("PROTO_CAFFEINATE")),
                        help="主人显式开启防休眠（caffeinate 绑定本进程；合盖仍会睡）")
    parser.add_argument("--token", default=os.environ.get("PROTO_WEB_TOKEN", ""),
                        help="固定访问令牌（默认随机；设 PROTO_WEB_TOKEN 可固定，便于长驻）")
    args = parser.parse_args()
    # 部署配置绑定数据目录加载（与启动目录无关——修"部署后仍离线"）
    load_default_env(data_dir=args.data_dir)

    stack = create_llm_stack()
    llm = stack["main"] if stack else None
    fast_llm = stack["fast"] if stack else None
    data_root = Path(args.data_dir)
    from proto.system.instance_lock import InstanceLock
    instance_lock = InstanceLock(data_root)
    if not instance_lock.acquire():
        print(f"  数据目录 {data_root} 已有 proto.web 实例在运行；拒绝启动第二个。")
        return 1
    from proto.llm.catalog import ModelCatalog
    models = ModelCatalog(data_root, llm, fast_llm)
    manager = _open_fleet(data_root, llm, fast_llm, models)
    from proto.system.keepawake import KeepAwake
    keeper = KeepAwake()
    server = ProtoWebServer(manager=manager, host="127.0.0.1", port=args.port,
                            token=resolve_token(args.token, data_root),
                            keep_awake=keeper)
    if args.caffeinate:
        if keeper.start():
            print("  防休眠  : 已开启（主人显式授权；进程退出自动解除；合盖仍会睡眠）")
        else:
            print("  防休眠  : 当前平台不可用（需 macOS caffeinate）")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = server.url()
    llm = server.agent.llm if server.agent is not None else llm
    print(f"\n  PROTO Web 控制台已启动")
    print(f"  地址   : {url}")
    print(f"  模型   : {llm.describe() if llm else '离线模式（配置 PROTO_LLM_BASE_URL 启用真实对话）'}")
    print(f"  数据   : {args.data_dir}/（owner.key 请备份）")
    print(f"  Ctrl+C 停止\n")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已停止。")
    finally:
        server.server_close()
        instance_lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
