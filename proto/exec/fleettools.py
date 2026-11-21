# -*- coding: utf-8 -*-
"""M47: 舰队协作工具——把 M32 黑板从"手工 API 壳"接进自主回路。

审查发现：黑板/舰队状态从未出现在 AgenticLoop 的工具目录里，
模型根本看不到协作面（两个 agent 的 blackboard 事件恒为 0）。
本模块注册三个 read 级工具：blackboard_read / blackboard_post /
fleet_status（监工人设从此有了真实的监督对象）。
"""
from __future__ import annotations

from proto.exec.contracts import builtin_parameters


def _fmt_age(ts: float, now: float) -> str:
    delta = max(0.0, now - ts)
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{delta / 3600:.1f} 小时前"
    return f"{delta / 86400:.1f} 天前"


class FleetTools:
    """黑板 + 舰队状态（只读安全级：均不触碰主人可见世界）。"""

    def __init__(self, agent):
        self._agent = agent

    def register_all(self, registry) -> None:
        from proto.kernel.kernel import ActionCategory
        registry.register("blackboard_read", self.bb_read,
                          category=ActionCategory.READ_DRAFT,
                          description="读舰队共享黑板（求助帖/分享/回复）",
                          parameters=builtin_parameters("blackboard_read"))
        registry.register("blackboard_post", self.bb_post,
                          category=ActionCategory.READ_DRAFT,
                          description="向舰队黑板发帖（kind=help 求助/share 分享/"
                                      "report 汇报），供其他 agent 或主人查看",
                          parameters=builtin_parameters("blackboard_post"))
        registry.register("fleet_status", self.status,
                          category=ActionCategory.READ_DRAFT,
                          description="舰队各 agent 当前状态（谁在做什么/卡点/预算）",
                          parameters=builtin_parameters("fleet_status"))

    # ---- 工具实现（返回字符串：直接成为循环观察） ----
    def bb_read(self) -> str:
        bb = self._agent.blackboard
        if bb is None:
            return "黑板未启用（独立模式，无舰队）"
        import time
        notes = bb.read()[:8]
        if not notes:
            return "黑板暂无帖子。可用 blackboard_post 发求助/分享/汇报。"
        lines = []
        for n in notes:
            head = (f"[{n.get('kind', 'help')}] {n.get('from', '?')} "
                    f"{_fmt_age(n.get('ts', time.time()), time.time())}："
                    f"{str(n.get('text', ''))[:160]}")
            if n.get("claimed_by"):
                head += f"（{n['claimed_by']} 已认领）"
            lines.append(head)
            for r in (n.get("replies") or [])[-2:]:
                lines.append(f"  ↳ {r.get('from', '?')}：{str(r.get('text', ''))[:120]}")
        return "\n".join(lines)

    def bb_post(self, text: str, kind: str = "report") -> str:
        if not (text or "").strip():
            return "发帖失败：内容为空"
        if self._agent.blackboard is None:
            return "黑板未启用（独立模式，无舰队）"
        nid = self._agent.post_to_blackboard(text, kind=kind)
        return f"已发到舰队黑板（#{nid}，kind={kind}）"

    def status(self) -> str:
        fleet_status = self._agent._fleet_status_fn
        if fleet_status is None:
            return "舰队状态不可用（独立模式）。可先用 blackboard_read 看协作动态。"
        rows = fleet_status()
        if not rows:
            return "舰队暂无其他成员。"
        return "\n".join(
            f"- {r.get('name', r.get('id', '?'))}：{r.get('status', '?')}"
            + (f"｜{r['detail']}" if r.get("detail") else "")
            for r in rows)
