# -*- coding: utf-8 -*-
"""M45: 接力工作记忆（Handoff Journal）——把"尝试间失忆"变成"接力赛"。

ZCode/Codex 靠单一会话上下文保持推进力；PROTO 每 30 分钟新开一条链，
若不带记忆就会反复从零探索（生产证据：6 次尝试 60 步里 ~50 步重复游荡）。
每轮结束把 发现/产出/下一步 落到目标专属 JOURNAL.md，下一轮开局注入——
探索只做一次，产出逐轮累积。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional

_SUMMARY_PROMPT = (
    "为下面这轮工作写接力小结，供下一轮直接续做。只输出三行，格式：\n"
    "发现：本轮摸清的关键事实（文件在哪/结论是什么，一行内）\n"
    "产出：本轮实际写出/修改的文件（没有就写 无）\n"
    "下一步：下一轮第 1 步该做的具体动作（写哪个文件/跑什么验证）"
)

_MAX_ROUNDS_IN_HEAD = 2


class GoalJournal:
    """目标工作日志：追加轮次记录，读取时只取最近两轮（上下文经济）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._round = 0

    # ---- 读 ----
    def resume_head(self) -> str:
        """最近两轮小结（发现/产出/下一步），空日志返回空串。"""
        if not self.path.exists():
            return ""
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return ""
        rounds = text.split("\n## ")
        recent = rounds[-_MAX_ROUNDS_IN_HEAD:]
        return "## " + "\n## ".join(r for r in recent if r.strip())

    def consecutive_no_output(self) -> int:
        """尾部连续零产出轮数（用于产出纪律升级）。"""
        if not self.path.exists():
            return 0
        count = 0
        for block in reversed(self._round_blocks()):
            if "产出：无" in block or "产出:" in block.split("产出：")[0]:
                pass
            if "产出：无" in block:
                count += 1
            else:
                break
        return count

    def latest_round(self) -> Optional[dict]:
        """最近一轮的结构化内容（活动中心用）。"""
        blocks = self._round_blocks()
        if not blocks:
            return None
        b = blocks[-1]
        def _field(key):
            line = next((ln for ln in b.splitlines()
                         if ln.startswith(key)), "")
            return line[len(key):].strip() or None
        status = ""
        first = b.splitlines()[0] if b.splitlines() else ""
        if "·" in first:
            status = first.split("·")[-1].strip()
        return {"status": status,
                "findings": _field("发现："),
                "produced": _field("产出："),
                "next": _field("下一步：")}

    def round_count(self) -> int:
        return len(self._round_blocks())

    def _round_blocks(self) -> List[str]:
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        return [b for b in text.split("\n## ") if b.strip()]

    # ---- 写 ----
    def note_round(self, status: str, findings: List[str], produced: List[str],
                   next_steps: List[str]) -> None:
        self._round += 1
        lines = [f"## 第{self._round}轮 · {status}"]
        lines.append("发现：" + ("；".join(findings) or "无"))
        lines.append("产出：" + ("；".join(produced) or "无"))
        lines.append("下一步：" + ("；".join(next_steps) or "无"))
        self._append("\n".join(lines) + "\n")

    def finish(self, status: str, observations: List[str], trace: List[dict],
               summarize: Optional[Callable[[str, str], str]] = None) -> None:
        """一轮结束：优先 LLM 小结，失败/离线用机械摘要（绝不空手）。"""
        findings: List[str] = []
        produced = self._written_files(trace)
        next_steps: List[str] = []
        if summarize is not None:
            try:
                material = "\n".join(observations[-10:])[:1200]
                raw = summarize(_SUMMARY_PROMPT, material)
                for line in raw.splitlines():
                    line = line.strip()
                    for key, bucket in (("发现：", findings),
                                        ("产出：", produced),
                                        ("下一步：", next_steps)):
                        if line.startswith(key) and line[len(key):].strip():
                            bucket.append(line[len(key):].strip()[:160])
            except Exception:
                pass
        if not next_steps:
            next_steps = ([f"先写 {produced[0]} 的剩余部分并验证"]
                          if produced else ["改从 write_file 直接产出，避免纯探索"])
        self.note_round(status, findings[:3] or ["（见 observations 尾部）"],
                        produced[:5], next_steps[:3])

    def _append(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")

    @staticmethod
    def _written_files(trace: List[dict]) -> List[str]:
        files = []
        for step in trace or []:
            if step.get("tool") in ("write_file", "edit_file"):
                path = (step.get("args") or {}).get("path", "")
                if path and path not in files:
                    files.append(path)
        return files
