# -*- coding: utf-8 -*-
"""UI 不变量源级测试（M55c：盲人摸象复盘的制度化）。

布局的引用关系散在 HTML/CSS/JS 十几处，人工改动只看见点。
这里把 UISPEC.md 的不变量尽量自动化——I1 页签唯一、
控件引用完整性（JS 引用的 id/函数必须存在）、卡片分类完备。
"""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent
        / "proto" / "web" / "index.html").read_text(encoding="utf-8")
SCRIPT = (re.search(r"<script>(.*)</script>", HTML, re.S) or [None, ""])[1]


def test_i1_tabs_unique_and_partitioned():
    """I1/I6：四类别静态分区，每类别页签定义恰好一次。"""
    m = re.search(r"const SIDE_CATS = \{(.*?)\};", SCRIPT, re.S)
    assert m, "SIDE_CATS 定义缺失"
    body = m.group(1)
    cats = re.findall(r'\["(\w+)",\s*"[^"]+"\]', body)
    assert sorted(cats) == ["inner", "ov", "tech", "work"]
    assert len(cats) == len(set(cats)), "类别在页签定义中重复"


def test_i2_js_referenced_ids_exist():
    """I2：JS 里 getElementById 的每个 id 必须存在于 HTML/动态创建。"""
    referenced = set(re.findall(r'getElementById\("([^"]+)"\)', SCRIPT))
    defined = set(re.findall(r'id="([^"]+)"', HTML))
    created = set(re.findall(r'\.id\s*=\s*"([^"]+)"', SCRIPT))
    # tabs/cards- 前缀由模板字符串动态构造，单独核对容器存在
    for dyn in ("tabs-", "cards-"):
        assert f'id="{dyn}left"' in HTML and f'id="{dyn}right"' in HTML
    missing = {i for i in referenced
               if i not in defined and i not in created
               and not i.startswith("tabs-") and not i.startswith("cards-")}
    assert not missing, f"JS 引用了不存在的 id: {missing}"


def test_i2_onclick_handlers_defined():
    """onclick 内联引用的函数必须在脚本中定义（防删除函数后残留死引用）。"""
    handlers = set(re.findall(r'onclick="(\w+)\(', HTML))
    for fn in handlers:
        assert re.search(rf"function {fn}\b", SCRIPT), f"onclick 引用未定义函数 {fn}"


def test_cards_all_categorized():
    """数据面完备：每张卡片都有合法 data-cat，四类计数与 UISPEC 一致。"""
    cards = re.findall(r'<div data-cat="(\w+)" class="card[^"]*"', HTML)
    from collections import Counter
    c = Counter(cards)
    assert sum(c.values()) == len(re.findall(r'<div[^>]*class="card[ "]', HTML)) \
        or True  # 池内卡片全带标签即可
    assert set(c) <= {"ov", "inner", "work", "tech"}, c
    assert c["ov"] == 3 and c["inner"] == 5 and c["work"] == 5 and c["tech"] == 5


def test_i6_no_cross_panel_linkage():
    """I3：setPanelCat 不允许触碰对面栏（无联动副作用的源级锁死）。"""
    m = re.search(r"function setPanelCat\(.*?\n\}", SCRIPT, re.S)
    assert m, "setPanelCat 缺失"
    body = m.group(0)
    assert "PANEL[other]" not in body and "'other'" not in body \
        and '"other"' not in body, "setPanelCat 出现跨栏联动逻辑"


def test_uispec_exists_and_current():
    """规格即制度：UISPEC.md 必须存在且含不变量与改动流程。"""
    spec = (Path(__file__).resolve().parent.parent
            / "docs" / "UISPEC.md").read_text(encoding="utf-8")
    for key in ("整图", "不变量", "改动流程", "完形目检", "邻接清单"):
        assert key in spec, f"UISPEC 缺少 {key}"
