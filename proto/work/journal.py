# -*- coding: utf-8 -*-
"""M45: 接力工作记忆（Handoff Journal）——把"尝试间失忆"变成"接力赛"。

ZCode/Codex 靠单一会话上下文保持推进力；PROTO 每 30 分钟新开一条链，
若不带记忆就会反复从零探索（生产证据：6 次尝试 60 步里 ~50 步重复游荡）。
每轮结束把 发现/产出/下一步 落到目标专属 JOURNAL.md，下一轮开局注入——
探索只做一次，产出逐轮累积。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Callable, List, Optional

from proto.exec.work_context import written_file

_SUMMARY_PROMPT = (
    "为下面这轮工作写接力小结，供下一轮直接续做。只输出三行，格式：\n"
    "发现：本轮摸清的关键事实（文件在哪/结论是什么，一行内）\n"
    "产出：本轮实际写出/修改的文件（没有就写 无）\n"
    "下一步：下一轮第 1 步该做的具体动作（写哪个文件/跑什么验证）"
)

_MAX_ROUNDS_IN_HEAD = 2
_PLACEHOLDER_MARKS = ("待填", "（见 observations", "（见 observations 尾部）")


def _cut_sentence(text: str, limit: int) -> str:
    """按句子边界截断：在 limit 内找最后一个句末标点，找不到才硬切。"""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(mark) for mark in "。！？；;")
    return (window[:cut + 1] if cut > limit // 3 else window.rstrip("，、： ")) + "…"


def _is_placeholder(line: str) -> bool:
    return any(mark in line for mark in _PLACEHOLDER_MARKS)


def _mechanical_findings(trace: List[dict]) -> List[str]:
    """LLM 小结缺席时的机械事实摘要——绝不留『见 observations 尾部』这类死引用。"""
    if not trace:
        return ["本轮没有任何已执行步骤（模型未返回可执行决策）"]
    written = []
    failures = []
    for step in trace:
        path = written_file(step.get("tool"), step.get("args") or {}, step.get("result"))
        if path and path not in written:
            written.append(path)
        if step.get("ok") is False or str(step.get("result", "")).startswith(
                tuple(f"{status}:" for status in ("failed", "error", "denied"))):
            failures.append(step)
    tools = Counter(str(step.get("tool") or "?") for step in trace)
    parts = [f"共 {len(trace)} 步（" + "、".join(f"{name}×{count}" for name, count in tools.most_common(3)) + "）"]
    if written:
        parts.append("落盘 " + "、".join(written[:3]))
    if failures:
        last = failures[-1]
        parts.append(f"{len(failures)} 步失败，最后是 {last.get('tool')}："
                     + _cut_sentence(last.get("result"), 80))
    return ["；".join(parts)]


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
            if block.splitlines()[0].endswith("· model_blocked") and "产出：无" in block:
                continue
            if "产出：无" in block:
                count += 1
            else:
                break
        return count

    def note_model_block(self, trace: List[dict]) -> None:
        self.note_round("model_blocked", ["调用模型受阻；不是实现或验收失败"], self._written_files(trace)[:5],
                        ["模型恢复后从已有文件继续，不重复生成"])

    def latest_round(self) -> Optional[dict]:
        """最近一轮的结构化内容（活动中心用）；占位符行不外显。"""
        blocks = self._round_blocks()
        if not blocks:
            return None
        b = blocks[-1]
        def _field(key):
            line = next((ln for ln in b.splitlines()
                         if ln.startswith(key)), "")
            value = line[len(key):].strip()
            if not value or _is_placeholder(value):
                return None
            return _cut_sentence(value, 160)
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

    def activity_view(self):
        blocks = self._round_blocks()
        return {"rounds": len(blocks),
                "no_output": sum("\n产出：无\n" in block for block in blocks),
                "model_blocks": sum(block.splitlines()[0].endswith("· model_blocked") for block in blocks)}

    def _round_blocks(self) -> List[str]:
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        return [b for b in text.split("\n## ") if b.strip()]

    # ---- 写 ----
    def note_round(self, status: str, findings: List[str], produced: List[str],
                   next_steps: List[str]) -> None:
        # 轮次编号接续已有文件：此前每次新进程都从 1 重开，活动中心里
        # 同一目标出现多组「第1轮/第2轮」，读起来像坏账。
        self._round = max(self._round, self.round_count())
        self._round += 1
        lines = [f"## 第{self._round}轮 · {status}"]
        lines.append("发现：" + ("；".join(_cut_sentence(f, 200) for f in findings if f) or "无"))
        lines.append("产出：" + ("；".join(produced[:5]) or "无"))
        lines.append("下一步：" + ("；".join(_cut_sentence(s, 160) for s in next_steps if s) or "无"))
        self._append("\n".join(lines) + "\n")

    def finish(self, status: str, observations: List[str], trace: List[dict],
               summarize: Optional[Callable[[str, str], str]] = None, missing=None) -> None:
        """一轮结束：优先 LLM 小结，失败/离线用机械摘要（绝不空手，绝不留死引用）。"""
        findings: List[str] = []
        produced: List[str] = self._written_files(trace)
        next_steps: List[str] = []
        if summarize is not None:
            try:
                material = "\n".join(observations[-10:])[:1200]
                raw = summarize(_SUMMARY_PROMPT, material)
                for line in raw.splitlines():
                    line = line.strip()
                    for key, bucket in (("发现：", findings), ("下一步：", next_steps)):
                        if line.startswith(key) and line[len(key):].strip():
                            value = _cut_sentence(line[len(key):], 200)
                            if not _is_placeholder(value):
                                bucket.append(value)
            except Exception:
                pass
        if not findings:
            findings = _mechanical_findings(trace)
        if not next_steps:
            next_steps = (list(missing[:3]) if missing else
                          [f"检查 {produced[0]} 并执行最小验收"] if produced else
                          ["先完成一个可验收增量；避免重复探索"])
        self.note_round(status, findings[:3], produced[:5], next_steps[:3])

    def _append(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")

    @staticmethod
    def _written_files(trace: List[dict]) -> List[str]:
        files = []
        for step in trace or []:
            path = written_file(step.get("tool"), step.get("args") or {}, step.get("result"))
            if path and path not in files:
                files.append(path)
        return files
