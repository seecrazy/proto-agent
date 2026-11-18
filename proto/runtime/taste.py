# -*- coding: utf-8 -*-
"""M34-②: 审美与价值观系统——agent 对"好玩/有趣/有用/美"等人类描述词的
独特见解 + 主人可配置。

三层设计：
- 感官维度（taste_dims）：每个维度一对 {anchor: 正负锚点描述, weight: 权重}
  ——agent 用自己的语言定义"什么算有趣"（锚点是它自己的审美宣言）；
- 判断函数（judge）：给定候选描述/产出，LLM 按感官维度打分并给出理由；
- 演化：agent 的每次判断写入记忆（kind=reflection 的 taste 子类），
  主人的反馈可调整权重（"你觉得有趣的东西太无聊了"→fun 权重下调）。
"""

DEFAULT_TASTE = {
    "fun": {
        "label": "好玩",
        "anchor": "让人想立刻上手试一试的东西，试完还会想给别人看",
        "anti_anchor": "需要说服自己才能开始用的东西",
        "weight": 0.6,
    },
    "interesting": {
        "label": "有趣",
        "anchor": "推翻了我一个默认假设，或把两件不相干的事连在了一起",
        "anti_anchor": "重复我已经知道的事，只是换了包装",
        "weight": 0.7,
    },
    "useful": {
        "label": "有用",
        "anchor": "解决一个真实会反复遇到的麻烦，而不是想象中的麻烦",
        "anti_anchor": "功能存在但永远用不上的东西",
        "weight": 0.8,
    },
    "beautiful": {
        "label": "美",
        "anchor": "删掉了所有不必要的东西之后剩下的部分",
        "anti_anchor": "靠装饰和动效掩盖结构混乱的东西",
        "weight": 0.5,
    },
    "elegant": {
        "label": "优雅",
        "anchor": "用一个简单机制解决了看起来需要复杂方案的问题",
        "anti_anchor": "堆砌条件分支和特例才勉强工作的方案",
        "weight": 0.6,
    },
}

JUDGE_PROMPT = (
    "你是 PROTO（人设：{persona}）。你对以下每个感官维度有自己的定义（这是你的审美）：\n"
    "{dims}\n\n"
    "对象：{subject}\n\n"
    "按你的定义给每个维度打 1-5 分（整数）并用一句话说明理由（引用你的锚点定义）。\n"
    '只输出 JSON：{{"{first}": {{"score": 3, "reason": "..."}}, ...}}'
)

REFINE_PROMPT = (
    "你是 PROTO 的审美校准器。主人对你的判断给了反馈：'{feedback}'。\n"
    "当前维度定义如下。请调整相关维度的 anchor 描述（保持你的独特视角，"
    "但让它更贴近主人的偏好），输出调整后的完整定义。\n"
    '只输出 JSON：{{"维度名": {{"anchor": "新锚点", "anti_anchor": "...", "weight": 0.x}}, ...}}'
)


def load_taste(store) -> dict:
    doc = store.get("config", "taste")
    return doc["dims"] if doc else {k: dict(v) for k, v in DEFAULT_TASTE.items()}


def save_taste(store, dims: dict) -> None:
    store.put("config", "taste", {"dims": dims})


def taste_prompt_block(dims: dict) -> str:
    """注入对话/学习/目标生成的审美上下文。"""
    lines = []
    for key, d in dims.items():
        lines.append(f"- {d['label']}（{key}）= {d['anchor']}；≠ {d['anti_anchor']}")
    return "[我的审美] 我这样判断（这是我的独特视角）：\n" + "\n".join(lines)


def judge(agent, subject: str) -> "list[dict] | None":
    """按感官维度评估对象。返回 [{dim, score, reason}]；离线/失败返回 None。"""
    if agent.llm_fast is None:
        return None
    import json as _json
    import re as _re
    dims = load_taste(agent.spine.store)
    dims_text = "\n".join(
        f"- {d['label']}（{key}）: {d['anchor']}｜反面：{d['anti_anchor']}"
        for key, d in dims.items())
    first = next(iter(dims))
    try:
        raw = agent._complete(agent.llm_fast,
                              JUDGE_PROMPT.format(persona=agent.persona,
                                                  dims=dims_text,
                                                  subject=subject[:500],
                                                  first=first),
                              subject[:500])
        cleaned = _re.sub(r"```(?:json)?", "", raw).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        data = _json.loads(cleaned[start:end + 1])
    except Exception as exc:
        agent.spine.append("taste_judge_failed",
                           {"error": str(exc)[:100]})
        return None
    out = []
    for key, d in dims.items():
        entry = data.get(key) or data.get(d["label"]) or {}
        score = max(1, min(5, int(entry.get("score", 3))))
        reason = str(entry.get("reason", ""))[:120]
        out.append({"dim": key, "label": d["label"], "score": score,
                    "reason": reason, "weight": d["weight"]})
    return out


def weighted_score(verdicts: "list[dict]") -> float:
    """加权总分 1-5。"""
    if not verdicts:
        return 0.0
    total_w = sum(v["weight"] for v in verdicts)
    return round(sum(v["score"] * v["weight"] for v in verdicts)
                 / max(total_w, 1e-9), 2)


def refine_from_feedback(agent, feedback: str) -> "dict | None":
    """主人的审美反馈 → LLM 调整锚点定义（保留独特视角但贴近主人）。"""
    if agent.llm_fast is None:
        return None
    import json as _json
    import re as _re
    dims = load_taste(agent.spine.store)
    dims_text = _json.dumps(dims, ensure_ascii=False, indent=1)
    try:
        raw = agent._complete(agent.llm_fast,
                              REFINE_PROMPT.format(feedback=feedback[:300]),
                              dims_text)
        cleaned = _re.sub(r"```(?:json)?", "", raw).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        new_dims = _json.loads(cleaned[start:end + 1])
    except Exception:
        return None
    # 只接受合法结构
    merged = {}
    for key, d in dims.items():
        nd = new_dims.get(key, {})
        merged[key] = {
            "label": d["label"],
            "anchor": str(nd.get("anchor", d["anchor"]))[:200],
            "anti_anchor": str(nd.get("anti_anchor", d["anti_anchor"]))[:200],
            "weight": max(0.0, min(1.0, float(nd.get("weight", d["weight"])))),
        }
    save_taste(agent.spine.store, merged)
    agent.spine.append("taste_refined", {"feedback": feedback[:150]})
    return merged
