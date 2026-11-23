# -*- coding: utf-8 -*-
"""兼容入口：部署向导本体已迁至 proto.deploy；`python3 -m proto.setup` 与旧引用保持可用。"""
from proto.deploy import *  # noqa: F401,F403
from proto.deploy import _boot_agent, main  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
