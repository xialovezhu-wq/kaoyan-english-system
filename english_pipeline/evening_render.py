"""Self-contained, offline English feedback. All untrusted strings are escaped."""

import re
from html import escape
from urllib.parse import quote


def h(value):
    return escape(str(value), quote=True)


STYLE = """
:root{color-scheme:light;--paper:#f8f6ef;--ink:#202f31;--green:#1f5548;--muted:#4e6460;--rule:#c8d6ca;--cream:#fffdf8;--rust:#94422c}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:1rem}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.65 "Hiragino Sans GB","PingFang SC",system-ui,sans-serif}
main{max-width:1080px;margin:auto;padding:30px clamp(18px,5vw,62px) 60px}
header{border-top:5px solid var(--green);border-bottom:1px solid var(--rule);padding:24px 0 18px}
time,.eyebrow,.meta{color:var(--muted);font-size:.86rem}.eyebrow{font-weight:700;letter-spacing:.08em}h1,h2,h3,h4,p{margin:0 0 .55em}
h1{font-family:Georgia,"Songti SC",serif;font-size:clamp(2rem,5vw,3.45rem);line-height:1.16;letter-spacing:-.035em}
h2{font-size:clamp(1.35rem,3vw,2rem);margin-top:.9em}h3{font-size:1.16rem;color:var(--green);margin:1.4em 0 .55em}
a{color:var(--green);text-underline-offset:.22em}a:focus-visible,summary:focus-visible,input:focus-visible{outline:3px solid #965229;outline-offset:3px}
nav,.links{display:flex;flex-wrap:wrap;gap:.45em 1em;margin:10px 0}nav a{font-size:.9rem}section{margin-top:1.5em}article{border-top:1px solid var(--rule);padding:15px 0}
details{border-top:1px solid var(--rule);padding:.48em 0}summary{cursor:pointer;padding:.35em 0;font-weight:650}summary::marker{color:var(--green)}
details[open]>summary{color:var(--green)}.sentence,.example{font-family:Georgia,"Times New Roman",serif;font-size:1.19rem;line-height:1.5;overflow-wrap:anywhere}
.card,.panel{background:var(--cream);border:1px solid var(--rule);border-radius:10px;padding:clamp(13px,2vw,22px);margin:.7em 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,16.5em),1fr));gap:.8em}.grid>.card{margin:0}
.callout{border-left:3px solid var(--rust);background:#f6ede2;padding:10px 14px;margin:12px 0}.label{font-weight:700;color:var(--green)}
.termname{font-family:Georgia,serif;font-size:1.24em}.frequency{display:block;color:var(--muted);font-size:.84em;font-weight:400}.role{display:inline-block;border:1px solid var(--rule);background:#edf3ec;border-radius:5px;padding:0 .35em;margin-right:.4em;font-size:.88em}
blockquote{margin:8px 0;padding-left:12px;border-left:2px solid var(--rule);white-space:pre-line}small{font-size:.82em;color:var(--muted)}.src{overflow-wrap:anywhere}.pills{display:flex;gap:.45em;flex-wrap:wrap}
.pills a{border:1px solid var(--rule);border-radius:20px;padding:2px 10px;text-decoration:none;color:var(--green)}.pills a:hover{background:#e9efe8}
footer{border-top:1px solid var(--rule);margin-top:30px;padding:14px 0;color:var(--muted)}[hidden]{display:none!important}
@media(max-width:540px){main{padding:16px 14px 36px}.card,.panel{border-radius:6px}.grid{grid-template-columns:1fr}.sentence{font-size:1.12rem}}
.review-day header{padding:12px 0}.review-day h1{font-size:clamp(1.55rem,3.6vw,2.25rem)}.review-day h2{font-size:1.2rem}.review-day summary{font-weight:500}.review-day .reading-focus{font-weight:750;color:#174939;background:#e7f0e9;border-radius:.12em}.reading-cue{color:#294e43;border-left:3px solid #609078;padding-left:.65em}.review-day .card{border-radius:6px}.review-day h4{margin-top:.65em}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""


def html_document(title, body, extra=""):
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
        + h(title)
        + "</title><style>"
        + STYLE
        + "</style><main>"
        + body
        + "</main>"
        + extra
        + "</html>"
    )


def example(row):
    return (
        '<p class="meta">'
        + h(row["locator"])
        + '</p><p class="example" lang="en">'
        + h(row["quote"])
        + "</p><p>"
        + h(row["translation"])
        + "</p>"
    )


def focused_sentence(row):
    """Emphasize the reviewed spans without changing the original wording or order."""
    text = row["en"]
    spans = row.get("focus")
    if spans is None:
        # Older authors named the key words directly in the Chinese reading cue.
        words = re.findall(r"[A-Za-z]+(?:[-’'][A-Za-z]+)*(?: [A-Za-z]+(?:[-’'][A-Za-z]+)*)*", row["action"])
        spans = []
        for word in words:
            if len(word) <= 1:
                continue
            matches = list(re.finditer(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", text, re.I))
            if len(matches) == 1 and text.count(matches[0].group()) == 1:
                spans.append(matches[0].group())
        if not spans and row["nodes"]:
            spans = [row["nodes"][0]["en"]]
    result, cursor = [], 0
    for span in sorted(set(spans), key=text.index):
        start = text.index(span)
        if start < cursor or span == text:
            continue
        result.extend((h(text[cursor:start]), '<strong class="reading-focus">' + h(span) + '</strong>'))
        cursor = start + len(span)
    return "".join(result) + h(text[cursor:])


def card(row, anchor):
    translation = h(row["zh"])
    if row.get("translation_focus"):
        before, span, after = row["zh"].partition(row["translation_focus"])
        translation = h(before) + '<strong class="reading-focus">' + h(span) + '</strong>' + h(after)
    parts = [
        f'<article class="card" id="{anchor}" data-card><p class="sentence" lang="en">{focused_sentence(row)}</p>',
        '<p data-translation>' + translation + '</p>',
    ]
    if row["action"] and row["action"] != row["zh"]:
        parts.append('<p class="reading-cue">' + h(row["action"]) + '</p>')
    obs = row.get("observation")
    if obs:
        parts.append('<details data-observation><summary>对照我的首译</summary><blockquote>' + h(obs["quote"]) + '</blockquote>')
        if obs["feedback"] not in (row["action"],row["zh"],obs["quote"]):
            parts.append('<p>' + h(obs["feedback"]) + '</p>')
        if obs.get("later"):
            parts.append('<p>' + h(obs["later"]) + '</p>')
        parts.append('</details>')
    if row["nodes"]:
        parts.append('<details data-structure><summary>拆句</summary>')
        for node in row["nodes"]:
            parts.append(
                '<details data-node><summary><span lang="en">' + h(node["en"]) + '</span></summary>'
                '<p><span class="role">' + h(node["role"]) + '</span>' + h(node["zh"]) + '</p>'
            )
            if node["relation"] not in (node["zh"],row["action"]):
                parts.append('<p>' + h(node["relation"]) + '</p>')
            parts.append('</details>')
        parts.append('</details>')
    return "".join(parts) + '</article>'


def term(row, source_anchor):
    c = row["counts"]
    counts = " · ".join(
        f"英{n} {c[k]['papers']}/17 套，{c[k]['hits']} 次" for k, n in (("I", "一"), ("II", "二"))
    )
    parts = [
        '<details class="panel" data-term><summary><span class="termname" lang="en">' + h(row["item"])
        + '</span>　' + h(row["meaning"]) + '</summary>',
        '<p><span class="role">' + h(row["pos"]) + '</span>' + h(row["collocation"]) + '</p>',
    ]
    if source_anchor:
        parts.append('<a href="#' + source_anchor + '">回看本文原句</a>')
    else:
        parts.append('<p class="sentence" lang="en">' + h(row["en"]) + '</p><p>' + h(row["translation"]) + '</p>')
    parts.append('<details><summary>真题词频</summary><p class="frequency">' + h(counts) + '（2010–2026，词条全部语境）</p></details>')
    if row.get("exam_example"):
        parts.append(
            "<details><summary>往年真题用例</summary>" + example(row["exam_example"]) + "</details>"
        )
    if row.get("other_uses"):
        parts.append(
            "<details><summary>换个语境怎样理解</summary>"
            + "".join(
                "<article><b>"
                + h(s["meaning"])
                + '</b><p class="example" lang="en">'
                + h(s["example"])
                + "</p><p>"
                + h(s["translation"])
                + "</p></article>"
                for s in row["other_uses"]
            )
            + "</details>"
        )
    if row.get("writing"):
        parts.append(
            "<details data-transfer><summary>写作文时怎么用</summary><p>"
            + h(row["writing"])
            + '</p><p class="example" lang="en">'
            + h(row["example"])
            + "</p><p>"
            + h(row["example_zh"])
            + "</p><small>教学例句</small></details>"
        )
    return "".join(parts) + "</details>"


def pattern(row, source_anchor):
    info = "已核例句覆盖 " + str(row["papers"]) + " 套卷"
    if row.get("counts"):
        counts = row["counts"]
        info = " · ".join(f"英{label} {counts[series]['papers']}/17 套，{counts[series]['hits']} 次"
                          for series,label in (("I","一"),("II","二"))) + "（2010–2026，阅读 A 正文）"
    source = ('<a href="#' + source_anchor + '">回看本文原句</a>' if source_anchor else
              '<p class="sentence" lang="en">' + h(row["en"]) + '</p>')
    return ('<details class="panel" data-pattern><summary>' + h(row["title"]) + '</summary>'
            '<p>' + h(row["relation"]) + '</p>' + source +
            '<details><summary>往年用例与覆盖范围</summary><p class="frequency">' + h(info) + '</p>'
            + ''.join('<article>' + example(e) + '</article>' for e in row["examples"]) + '</details></details>')


def render(day, groups, scope):
    parts = ['<div class="review-day"><header><time>' + h(day) + '</time><h1>英语晚间复盘</h1></header>']
    if len(groups) > 1:
        parts.append('<nav aria-label="当日学习目录">' + "".join(
            '<a href="#g' + h(g["id"]) + '">' + h(g["title"]) + '</a>' for g in groups) + '</nav>')
    for g in groups:
        gid = h(g["id"])
        parts.append('<section id="g' + gid + '"><h2>' + h(g["title"]) + '</h2>')
        if g["mode"] == "protected":
            parts.append('<p>指定题号后可回看。</p></section>')
            continue
        anchors = {}
        cards = []
        for i, row in enumerate(g["cards"]):
            anchor = gid + "-s" + str(i)
            anchors.setdefault(row["unit"], anchor)
            cards.append(card(row, anchor))
        if cards:
            parts.append(cards[0])
            if len(cards) > 1:
                parts.append(f'<details data-more-cards><summary>其余 {len(cards)-1} 句</summary>'
                             + ''.join(cards[1:]) + '</details>')
        if g["terms"]:
            parts.append('<details data-terms><summary>单词与搭配</summary>')
            for phrase, label in ((False, "单词"), (True, "词组")):
                subset = [t for t in g["terms"] if (" " in t["item"]) == phrase]
                if subset:
                    parts.append('<h4>' + label + '</h4><div class="grid">' + ''.join(
                        term(t, anchors.get(t["unit"])) for t in subset) + '</div>')
            parts.append('</details>')
        if g["patterns"]:
            parts.append('<details data-patterns><summary>句型与真题用例</summary>' + ''.join(
                pattern(p, anchors.get(p["unit"])) for p in g["patterns"]) + '</details>')
        for row in g["writing"]:
            parts.append('<article><p>' + h(row["feedback"]) + '</p><details><summary>'
                         + h(row["stage"]) + '</summary><blockquote lang="en">' + h(row["quote"])
                         + '</blockquote></details></article>')
        parts.append('</section>')
    if scope["pending"]:
        parts.append('<p class="meta">另有 ' + str(len(scope["pending"])) + ' 个学习包待核。</p>')
    parts.append('<footer><a href="../../../learning/writing-course-2027/'
                 + quote("写作模板流程.html") + '">大小作文模板流程</a>'
                 + ('　<a href="../history/index.html">历史诊断与错题检索</a>' if scope.get("history_available") else "")
                 + '</footer></div>')
    return html_document(day + " 英语晚间复盘", "".join(parts))
