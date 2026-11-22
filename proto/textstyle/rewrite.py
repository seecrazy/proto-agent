"""Apply fixed, source-relative prescriptions and send the result back to the scanner."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .scanner import Hit, REWRITE, bundled_scan


_BODY = re.compile(r"[\u3400-\u9fffA-Za-z0-9\x00]")
_DASH = re.compile(r"—{1,2}")
_UNSAFE_DASH = re.compile(r"\d|\x00|[零一二三四五六七八九十两][年月日时分秒至–—]|—[零一二三四五六七八九十两]")
_CLAUSE = re.compile(r"[我你他她它]|没有|可以|已经|还没|现在|目前|尚未|准备|需要|这里|这次|这段|接下来")
_RETRY = {
    "hits_not_reduced": "上一次改写未降低命中数，请换用规则库中该模式的备选策略。",
    "rewrite_still_needed": "复验仍判定需要改写；只尝试规则库中安全的备选策略。",
    "facts_added": "删掉所有原文中没有的事实；不得补写正文。",
    "invalid_edit": "撤销越界或不符合处方的改动。",
    "scan_incomplete": "复验未完成，不发布候选。",
}


@dataclass(frozen=True)
class Edit:
    hit: Hit
    start: int
    end: int
    replacement: str
    instruction: str


def _approved_edit(text, hit, attempt, leads):
    sentence = hit.sentence
    visible = sentence.visible
    rule = hit.rule
    action, instruction = rule.primary if attempt == 1 or rule.alternative[0] == "review" else rule.alternative
    start, end, replacement = hit.start, hit.end, ""
    if action == "trim_lead" or action == "trim_stack":
        if rule.edit_pattern and rule.edit_pattern.match(visible) is None:
            return None
        if action == "trim_stack":
            for _ in range(7):
                tail = visible[end - sentence.start:]
                extra = next((match for pattern in leads if (match := pattern.match(tail))), None)
                if extra is None or extra.end() == 0:
                    break
                end += extra.end()
        if not _BODY.search(visible[end - sentence.start:]):
            if (rule.pattern_id != "P03" or text[start:end].rstrip()[-1:] not in (":", "：")
                    or end != sentence.end or not _BODY.search(text[end:])):
                return None
    elif action == "trim_contrast" and rule.edit_pattern:
        edit_match = rule.edit_pattern.match(visible)
        if not edit_match or not _BODY.search(visible[edit_match.end():]):
            return None
        start, end = sentence.start + edit_match.start(), sentence.start + edit_match.end()
    elif action in ("dash_comma", "dash_semicolon"):
        clauses = _DASH.split(visible)
        if (_UNSAFE_DASH.search(visible) or not _DASH.fullmatch(text[start:end])
                or not all(len(clause.strip()) >= 4 and _CLAUSE.search(clause) for clause in clauses)):
            return None
        replacement = "，" if action == "dash_comma" else "；"
    elif action != "drop_tail":
        return None
    return Edit(hit, start, end, replacement, instruction)


def _propose_edits(text, hits, attempt, leads):
    choices = (edit for hit in hits if (edit := _approved_edit(text, hit, attempt, leads)) is not None)
    edits, boundary = [], 0
    for edit in sorted(choices, key=lambda item: (item.start, -item.end, item.hit.rule.pattern_id)):
        if edit.start >= boundary:
            edits.append(edit)
            boundary = edit.end
    return edits


def _check_edits(text, scan, edits, attempt, leads):
    boundary = 0
    for edit in edits:
        if edit.replacement not in ("", "，", "；"):
            return "facts_added"
        sentence = edit.hit.sentence
        if (edit.hit not in scan.hits or not boundary <= edit.start < edit.end <= len(text)
                or not sentence.start <= edit.start < edit.end <= sentence.end
                or "\0" in sentence.visible[edit.start - sentence.start:edit.end - sentence.start]
                or _approved_edit(text, edit.hit, attempt, leads) != edit):
            return "invalid_edit"
        boundary = edit.end
    return None


def _apply_edits(text, edits):
    parts, changes, cursor = [], [], 0
    for edit in edits:
        parts.extend((text[cursor:edit.start], edit.replacement))
        changes.append({"original": text[edit.start:edit.end], "rewritten": edit.replacement,
                        "rule_applied": f"{edit.hit.rule.pattern_id} → {edit.instruction}",
                        "start": edit.start, "end": edit.end})
        cursor = edit.end
    return "".join([*parts, text[cursor:]]), changes


def _result(original, final, before, after, status, attempts, changes=()):
    result = {"status": status, "attempts": len(attempts), "facts_added": [],
              "final_text": final, "rewritten_text": final, "changes": list(changes), "scan_before": before.payload(original),
              "scan_after": after.payload(final), "verification": attempts}
    if status == "manual_required":
        result["message"] = "需人工处理"
    return result


def polish_style(text, profile="dialogue"):
    """Return the original or a locally verified patch, with no extra model calls."""
    scanner, before = bundled_scan(text, profile)
    if before.error or not before.hits:
        return _result(text, text, before, before, "manual_required" if before.error else "passed", [])

    leads = tuple(rule.edit_pattern or rule.pattern for rule in scanner.rules
                  if profile in rule.profiles and rule.primary[0] in ("trim_lead", "trim_contrast"))
    attempts = []
    for attempt in (1, 2):
        edits = _propose_edits(text, before.hits, attempt, leads)
        reason = _check_edits(text, before, edits, attempt, leads)
        after = None
        if reason is None:
            candidate, changes = _apply_edits(text, edits)
            after = scanner.scan(candidate, profile)
            if after.error:
                reason = "scan_incomplete"
            elif len(after.hits) >= len(before.hits):
                reason = "hits_not_reduced"
            elif after.verdict == REWRITE:
                reason = "rewrite_still_needed"
            else:
                attempts.append({"attempt": attempt, "status": "passed", "hit_count": len(after.hits)})
                return _result(text, candidate, before, after, "passed", attempts, changes)
        attempts.append({"attempt": attempt, "status": "rejected", "reason": reason,
                         "retry_instruction": _RETRY[reason],
                         "hit_count": len(after.hits) if after is not None and not after.error else None})
    return _result(text, text, before, before, "manual_required", attempts)
