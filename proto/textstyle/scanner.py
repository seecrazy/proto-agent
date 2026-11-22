"""Offline Chinese prose rules and offsets. This module never generates text."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Optional


PROFILES = ("dialogue", "technical", "formal", "creative")
MAX_TEXT = 16000
MAX_HITS = 48
CLEAN = "这段不需要去模板腔"
KEEP = "不要把长句拆短，不要重排段落，不要改语序"
REWRITE = "需要改写"
_MASK = "\0"
_HAN = re.compile(r"[\u3400-\u9fff]")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_MARKDOWN = re.compile(r"^(?: {4}|\t| {0,3}(?:>|\||#{1,6}[ \t]|[-+*][ \t]|\d{1,4}[.)][ \t]|"
                       r"\d{1,4}、|\[[^\]\r\n]+\]:|\$[ \t]|(?:-{3,}|\*{3,}|_{3,})\s*$))")
_STRUCTURED = re.compile(r"^(?:\{|\[\s*(?:[\[{\"\]\d-]|true\b|false\b|null\b)|<(?:/?[a-zA-Z][\w:-]*(?=[\s/>])|!--|\?xml))")
_INLINE = re.compile(
    r"(?P<ticks>`{1,24})(?!`)[^`\r\n\x00]*(?P=ticks)(?!`)|`[^\r\n\x00]*|"
    r"“[^”\x00]*(?:”|(?=\x00|\Z))|‘[^’\x00]*(?:’|(?=\x00|\Z))|"
    r"「[^」\x00]*(?:」|(?=\x00|\Z))|『[^』\x00]*(?:』|(?=\x00|\Z))|《[^》\x00]*(?:》|(?=\x00|\Z))|"
    r'"(?:\\[^\r\n]|[^"\\\r\n\x00])*(?:"|(?=[\r\n\x00]|\Z))|'
    r"(?<!\w)'[^'\r\n\x00]*(?:'|(?=[\r\n\x00]|\Z))|"
    r"!?\[[^\[\]\r\n\x00]+\](?:\([^\r\n\x00)]*\)|\[[^\[\]\r\n\x00]*\])|"
    r"https?://[^\s<>\"'`\x00。，、；！？）]+|\*\*[^*\r\n\x00]+\*\*|__[^_\r\n\x00]+__"
)
_END_SENTENCE = re.compile(r"[。！？!?；;]+|\.(?=[ \t]+[A-Z\u3400-\u9fff]|[ \t]*$)")


@dataclass(frozen=True)
class Rule:
    pattern_id: str
    profiles: tuple[str, ...]
    detector: str
    pattern: Optional[re.Pattern]
    edit_pattern: Optional[re.Pattern]
    minimum: int
    severity: str
    deduction: int
    primary: tuple[str, str]
    alternative: tuple[str, str]
    forbidden: str

    @classmethod
    def from_config(cls, node):
        return cls(node["id"], tuple(node["profiles"]), node["detector"],
                   re.compile(node["pattern"]) if "pattern" in node else None,
                   re.compile(node["edit_pattern"]) if "edit_pattern" in node else None,
                   node.get("minimum", 1), node["severity"], node["deduction"],
                   tuple(node["primary"]), tuple(node["alternative"]), node["forbidden"])


@dataclass(frozen=True)
class Sentence:
    start: int
    end: int
    paragraph: int
    number: int
    visible: str


@dataclass(frozen=True)
class Hit:
    rule: Rule
    sentence: Sentence
    start: int
    end: int

    def payload(self, text):
        return {
            "pattern_id": self.rule.pattern_id,
            "location": f"第{self.sentence.paragraph}段第{self.sentence.number}句",
            "matched_text": text[self.start:self.end],
            "start": self.start, "end": self.end,
            "sentence_start": self.sentence.start, "sentence_end": self.sentence.end,
            "severity": self.rule.severity, "deduction": self.rule.deduction,
            "prescription": {"primary": self.rule.primary[1], "alternative": self.rule.alternative[1],
                             "forbidden": self.rule.forbidden},
        }


@dataclass(frozen=True)
class Scan:
    version: str
    profile: str
    threshold: int
    hits: tuple[Hit, ...] = ()
    error: Optional[str] = None

    @property
    def total_score(self):
        return None if self.error else max(0, 100 - sum(hit.rule.deduction for hit in self.hits))

    @property
    def verdict(self):
        if self.error:
            return "未完成扫描"
        if not self.hits:
            return CLEAN
        if self.total_score < self.threshold or any(hit.rule.severity == "high" for hit in self.hits):
            return REWRITE
        return "有可疑模板，建议核对"

    def payload(self, text):
        result = {"rules_version": self.version, "profile": self.profile,
                  "hits": [hit.payload(text) for hit in self.hits], "total_score": self.total_score,
                  "verdict": self.verdict, "complete": self.error is None}
        if self.error:
            result["reason"] = self.error
        elif not self.hits:
            result["instruction"] = KEEP
        return result


def _opaque(value):
    return re.sub(r"[^\r\n]", _MASK, value)


def _masked(text):
    if _STRUCTURED.match(text.lstrip()):
        return _opaque(text)
    lines, fence, block = [], None, False
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        marker = _FENCE.match(body)
        if fence is not None:
            lines.append(_opaque(line))
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
            lines.append(_opaque(line))
        elif not body.strip():
            block = False
            lines.append(line)
        elif block or _MARKDOWN.match(body) or _STRUCTURED.match(body.lstrip()) or body.count("|") >= 2:
            block = True
            lines.append(_opaque(line))
        else:
            lines.append(line)
    return _INLINE.sub(lambda match: _opaque(match[0]), "".join(lines))


def _sentences(text):
    paragraph, number, offset = 0, 0, 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if not body.strip():
            number = 0
            offset += len(line)
            continue
        if not number:
            paragraph += 1
        start = 0
        for stop in [*(match.end() for match in _END_SENTENCE.finditer(body)), len(body)]:
            fragment = body[start:stop]
            visible = fragment.strip()
            if visible:
                number += 1
                begin = offset + start + len(fragment) - len(fragment.lstrip())
                yield Sentence(begin, begin + len(visible), paragraph, number, visible)
            start = stop
        offset += len(line)


def _rhythm(rule, sentences):
    previous = anchor = None
    last_length, count = 0, 0
    for index, sentence in enumerate(sentences):
        length = len(_HAN.findall(sentence.visible))
        if (sentence.number != 1 or _MASK in sentence.visible or not 6 <= length <= 24
                or (index + 1 < len(sentences) and sentences[index + 1].paragraph == sentence.paragraph)
                or not re.search(r"[。！!?？]$", sentence.visible)):
            previous, count = None, 0
            continue
        if previous and sentence.paragraph == previous.paragraph + 1 and abs(last_length - length) <= 1:
            count += 1
        else:
            anchor, count = sentence, 1
        if count == rule.minimum:
            yield Hit(rule, anchor, anchor.start, anchor.end)
        previous, last_length = sentence, length


def _sentence_hits(rule, sentence):
    visible = sentence.visible
    matches = [match for match in rule.pattern.finditer(visible) if _MASK not in match[0]]
    if len(matches) < rule.minimum:
        return
    if rule.detector == "chain":
        if len(_HAN.findall(visible)) >= 42 and _MASK not in visible:
            first = max(0, matches[0].start() - 8)
            last = min(len(visible), matches[rule.minimum - 1].end() + 6, first + 140)
            yield Hit(rule, sentence, sentence.start + first, sentence.start + last)
        return
    for match in matches:
        yield Hit(rule, sentence, sentence.start + match.start(), sentence.start + match.end())


class Scanner:
    def __init__(self, config):
        self.version = config["version"]
        self.thresholds = config["profiles"]
        self.rules = tuple(Rule.from_config(node) for node in config["rules"])
        if (not re.fullmatch(r"[a-z0-9-]+", self.version) or set(self.thresholds) != set(PROFILES)
                or any(type(value) is not int or not 0 <= value <= 100 for value in self.thresholds.values())
                or not self.rules or len({rule.pattern_id for rule in self.rules}) != len(self.rules)
                or any(rule.detector not in {"lead", "tail", "contrast", "phrase", "repeated", "chain", "rhythm"}
                       or (rule.pattern is None) != (rule.detector == "rhythm")
                       or not rule.profiles or set(rule.profiles) - set(PROFILES)
                       or type(rule.minimum) is not int or rule.minimum <= 0
                       or type(rule.deduction) is not int or not 0 < rule.deduction <= 100
                       or rule.severity not in {"low", "medium", "high"}
                       or len(rule.primary) != 2 or len(rule.alternative) != 2 for rule in self.rules)):
            raise ValueError("invalid_style_rules")

    def scan(self, text, profile):
        threshold = self.thresholds[profile]
        if len(text) > MAX_TEXT:
            return Scan(self.version, profile, threshold, error="text_limit")
        if not _HAN.search(text):
            return Scan(self.version, profile, threshold)
        sentences = list(_sentences(_masked(text)))
        matches = []
        limited = False
        for rule in self.rules:
            if profile not in rule.profiles:
                continue
            if rule.detector == "rhythm":
                hits = _rhythm(rule, sentences)
            elif rule.detector == "tail":
                prior = any(_HAN.search(sentence.visible) for sentence in sentences[:-1])
                hits = _sentence_hits(rule, sentences[-1]) if prior else ()
            else:
                hits = (hit for sentence in sentences if _HAN.search(sentence.visible)
                        for hit in _sentence_hits(rule, sentence))
            for hit in hits:
                if len(matches) == MAX_HITS:
                    limited = True
                    break
                matches.append(hit)
            if limited:
                break
        matches.sort(key=lambda hit: (hit.start, hit.end, hit.rule.pattern_id))
        return Scan(self.version, profile, threshold, tuple(matches), "hit_limit" if limited else None)


@lru_cache(maxsize=1)
def _bundled_scanner():
    with files(__package__).joinpath("rules.json").open(encoding="utf-8") as stream:
        return Scanner(json.load(stream))


def bundled_scan(text, profile):
    if not isinstance(text, str) or profile not in PROFILES:
        raise ValueError("invalid_style_input")
    try:
        scanner = _bundled_scanner()
    except (OSError, TypeError, ValueError, KeyError, AttributeError, re.error):
        return None, Scan("unavailable", profile, 100, error="rules_unavailable")
    return scanner, scanner.scan(text, profile)


def scan_style(text, profile="dialogue"):
    """Return positions, deductions and fixed prescriptions without applying any edit."""
    return bundled_scan(text, profile)[1].payload(text)
