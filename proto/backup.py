# -*- coding: utf-8 -*-
"""M8: 备份与导出——python3 -m proto.backup [--data-dir ./proto_data] [-o backup.zip]。

打包 owner.key（主人身份，丢了就要线下仪式）+ 全部状态/审计/检查点/进化档案。
恢复 = 解压到数据目录后照常启动。
"""
from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path


def create_backup(data_dir: Path, output: Path) -> str:
    data_dir = Path(data_dir)
    if not data_dir.exists():
        raise FileNotFoundError(f"数据目录不存在: {data_dir}")
    key_path = data_dir / "owner.key"
    if not key_path.exists():
        raise FileNotFoundError("缺少 owner.key——没有主人密钥的备份没有意义")
    fingerprint = "sha256:" + hashlib.sha256(bytes.fromhex(
        key_path.read_text(encoding="utf-8").strip())).hexdigest()
    output = Path(output)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(data_dir.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(data_dir))
    output.chmod(0o600)   # 内含 owner.key
    return fingerprint


def main() -> int:
    parser = argparse.ArgumentParser(prog="proto.backup", description="备份 PROTO 数据目录")
    parser.add_argument("--data-dir", default="./proto_data")
    parser.add_argument("-o", "--output", default="proto-backup.zip")
    args = parser.parse_args()
    load_default_env()  # 部署配置（proto.env）自动生效
    fingerprint = create_backup(args.data_dir, args.output)
    print(f"备份完成: {args.output}")
    print(f"主人指纹: {fingerprint}")
    print(f"恢复方式: 解压到数据目录后 python3 -m proto.web（owner.key 即身份，请妥善保管）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
