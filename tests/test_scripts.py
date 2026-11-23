# -*- coding: utf-8 -*-
"""运维脚本回归：start-console.sh 必须从仓库根解析 proto 包并转发正确参数。

M78 教训：脚本移入 scripts/ 后 `cd "$(dirname "$0")"` 落在 scripts/ 内，
`python3 -m proto.web` 找不到包——`bash -n` 只查语法，查不了工作目录。
本测试用 PATH 桩替换 python3 记录 cwd 与 argv，不起服务、不占端口。
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_start_console_runs_from_repo_root_and_forwards_args(tmp_path):
    shim = tmp_path / "bin"
    shim.mkdir()
    recorded = tmp_path / "recorded.txt"
    (shim / "python3").write_text(
        "#!/bin/sh\n"
        'pwd >> "$PROTO_RECORD"\n'
        'printf "%s\\n" "$@" >> "$PROTO_RECORD"\n',
        encoding="utf-8")
    (shim / "python3").chmod(0o755)
    env = dict(os.environ,
               PATH=f"{shim}{os.pathsep}{os.environ['PATH']}",
               PROTO_RECORD=str(recorded),
               PROTO_CONSOLE_PORT="1")  # 端口 1 无监听，稳定绕开防双开分支
    proc = subprocess.run(["bash", str(REPO / "scripts" / "start-console.sh")],
                          env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    lines = recorded.read_text(encoding="utf-8").splitlines()
    assert lines[0] == str(REPO), "工作目录必须是仓库根（proto 包按 cwd 解析）"
    assert lines[1:] == ["-u", "-m", "proto.web",
                         "--data-dir", "./proto_data", "--port", "1"]
