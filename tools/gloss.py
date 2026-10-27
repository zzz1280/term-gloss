#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gloss.py —— 把一段 agent 回复渲染成"原文不动 + 术语可悬停"的 HTML 阅读页。

用法：
    python tools/gloss.py samples/示例回复.md
    python tools/gloss.py reply.md -o out.html --all
    python tools/gloss.py --check

设计原则：
  1. 只加包装层，不改一个字。所有原始字符都按顺序出现在输出里。
  2. 词典优先。没命中词库的词不会被猜，宁可漏标，不要瞎标。
  3. 代码块、行内代码、命令一律不注释——注释会破坏复制粘贴。
  4. 产出的页面不依赖 JavaScript：悬停用 CSS attr() 渲染，展开用纯 CSS 复选框。
     这样在任何受限环境（沙箱 iframe、邮件、离线阅读器）里都能用。

依赖：仅 Python 3.8+ 标准库。
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOSSARY = ROOT / "data" / "glossary.json"

LEVEL_NAME = {1: "影响结论", 2: "背景", 3: "可跳过"}


# ---------------------------------------------------------------- 词库

def load_glossary(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    index, order = {}, []
    for entry in data["terms"]:
        keys = [entry["term"]] + entry.get("aliases", [])
        for key in keys:
            if key and key not in index:
                index[key] = entry
                order.append(key)
    order.sort(key=len, reverse=True)  # 长词优先，避免被短词切碎
    return data, index, order


def check_glossary(path):
    data, index, _ = load_glossary(path)
    problems = []
    seen = set()
    for e in data["terms"]:
        t = e["term"]
        if t in seen:
            problems.append(f"重复术语：{t}")
        seen.add(t)
        if e.get("level") not in (1, 2, 3):
            problems.append(f"level 非法：{t} = {e.get('level')}")
        if not e.get("brief"):
            problems.append(f"缺 brief：{t}")
        elif len(e["brief"]) > 60:
            problems.append(f"brief 过长（{len(e['brief'])} 字）：{t}")
        if not e.get("misread"):
            problems.append(f"缺 misread：{t}")
    n = len(data["terms"])
    lv = {i: sum(1 for e in data["terms"] if e["level"] == i) for i in (1, 2, 3)}
    print(f"词库 {path}")
    print(f"  词条 {n} 条 → L1 {lv[1]} / L2 {lv[2]} / L3 {lv[3]}")
    print(f"  索引键 {len(index)} 个（含别名）")
    if problems:
        print(f"  {len(problems)} 处问题：")
        for p in problems:
            print("   -", p)
    else:
        print("  校验通过")
    return not problems


# ---------------------------------------------------------------- 匹配

def find_spans(text, index, order, seen, all_occ):
    """返回 [(start, end, entry)]，保证互不重叠。seen 跨块共享，用于"只标首现"。"""
    spans = []
    for key in order:
        entry = index[key]
        start = 0
        while True:
            i = text.find(key, start)
            if i < 0:
                break
            if not all_occ and entry["term"] in seen:
                break
            spans.append((i, i + len(key), entry))
            if not all_occ:
                seen.add(entry["term"])
                break
            start = i + len(key)
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    chosen = []
    for s in spans:
        if any(s[0] < c[1] and c[0] < s[1] for c in chosen):
            continue
        chosen.append(s)
    chosen.sort(key=lambda s: s[0])
    return chosen


def attr(value):
    """转成可以放进 HTML 属性的值；换行用 &#10; 表示，CSS 的 pre-line 会还原。"""
    return html.escape(value, quote=True).replace("\n", "&#10;")


def annotate(text, index, order, seen, all_occ, stats):
    spans = find_spans(text, index, order, seen, all_occ)
    if not spans:
        return html.escape(text, quote=False)
    out, pos = [], 0
    for start, end, entry in spans:
        out.append(html.escape(text[pos:start], quote=False))
        tip = f"{LEVEL_NAME[entry['level']]}｜{entry['brief']}\n别理解成：{entry['misread']}"
        out.append(
            f'<span class="gloss lv{entry["level"]}"'
            f' data-k="{attr(entry["term"])}"'
            f' data-brief="{attr(entry["brief"])}"'
            f' data-tip="{attr(tip)}"'
            f' tabindex="0">{html.escape(text[start:end], quote=False)}</span>'
        )
        stats["hits"] += 1
        stats["terms"].setdefault(entry["term"], len(stats["terms"]))
        stats["levels"][entry["level"]] += 1
        pos = end
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


# ---------------------------------------------------------------- 渲染

def render_inline(line, index, order, seen, all_occ, stats):
    parts = re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", line)
    out = []
    for p in parts:
        if not p:
            continue
        if len(p) > 4 and p.startswith("**") and p.endswith("**"):
            out.append("<strong>" + annotate(p[2:-2], index, order, seen, all_occ, stats) + "</strong>")
        elif len(p) > 2 and p.startswith("`") and p.endswith("`"):
            out.append("<code>" + html.escape(p[1:-1], quote=False) + "</code>")
        else:
            out.append(annotate(p, index, order, seen, all_occ, stats))
    return "".join(out)


def render_table(rows, index, order, seen, all_occ, stats):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    cells = [r for r in cells if not all(re.match(r"^:?-{2,}:?$", c) for c in r)]
    if not cells:
        return ""
    head, body = cells[0], cells[1:]
    out = ["<table><thead><tr>"]
    out += [f"<th>{render_inline(c, index, order, seen, all_occ, stats)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(
            f"<td>{render_inline(c, index, order, seen, all_occ, stats)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


SPECIAL = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+\.\s|>|\||```)")


def render_markdown(text, index, order, seen, all_occ, stats):
    lines = text.splitlines()
    out, i, n = [], 0, len(lines)

    def ri(s):
        return render_inline(s, index, order, seen, all_occ, stats)

    while i < n:
        s = lines[i].strip()

        if s.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf), quote=False) + "</code></pre>")
            continue

        if not s:
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            tag = "h2" if len(m.group(1)) <= 2 else "h3"
            out.append(f"<{tag}>{ri(m.group(2))}</{tag}>")
            i += 1
            continue

        if s.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            out.append(render_table(rows, index, order, seen, all_occ, stats))
            continue

        if re.match(r"^[-*+]\s+", s):
            items = []
            while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{ri(x)}</li>" for x in items) + "</ul>")
            continue

        if re.match(r"^\d+\.\s+", s):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{ri(x)}</li>" for x in items) + "</ol>")
            continue

        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(ri(x) for x in buf) + "</blockquote>")
            continue

        buf = []
        while i < n and lines[i].strip() and not SPECIAL.match(lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br>".join(ri(x) for x in buf) + "</p>")

    return "\n".join(out)


# ---------------------------------------------------------------- 页面

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--ink:#1f2328;--soft:#57606a;--faint:#8b949e;--line:#e6e2da;--acc:#0e7490;--accs:#e0f2f5;--warn:#b45309;--bgi:#f7f6f3}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bgi);color:var(--ink);font:16px/1.85 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:760px;margin:0 auto;padding:40px 24px 100px}}
.bar{{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;font-size:13px;color:var(--soft);border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:28px}}
.bar b{{color:var(--ink);font-weight:600}}
.btn{{border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 12px;font:inherit;font-size:12.5px;color:var(--soft);cursor:pointer;display:inline-block;user-select:none}}
.btn:hover{{border-color:#d6d0c4;color:var(--ink)}}
.modeinput{{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}}
article{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:30px 32px;box-shadow:0 1px 3px rgba(20,19,17,.03)}}
article p{{margin:0 0 15px;text-align:justify}}
article h2{{font-size:19px;margin:26px 0 12px;line-height:1.4}}
article h3{{font-size:16px;margin:22px 0 10px}}
article ul,article ol{{margin:0 0 15px;padding-left:22px}}
article li{{margin:4px 0}}
article pre{{background:#f6f4ef;border:1px solid var(--line);border-radius:8px;padding:14px 16px;overflow:auto;font-size:13px;line-height:1.6}}
article code{{background:#f1efe8;border-radius:4px;padding:1px 5px;font-size:.9em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
article pre code{{background:none;padding:0}}
article blockquote{{margin:0 0 15px;padding:2px 0 2px 14px;border-left:3px solid var(--line);color:var(--soft)}}
article table{{border-collapse:collapse;width:100%;margin:0 0 16px;font-size:14px}}
article th,article td{{border:1px solid var(--line);padding:7px 10px;text-align:left}}
article th{{background:#faf9f6;font-weight:600}}
article hr{{border:0;border-top:1px solid var(--line);margin:24px 0}}
.gloss{{position:relative;cursor:help;border-bottom:1.5px dotted var(--acc);padding-bottom:.5px;border-radius:2px}}
.gloss:hover{{background:var(--accs)}}
.gloss.lv1{{border-bottom-width:2px}}
.gloss::after{{display:none;content:"（" attr(data-brief) "）";font-size:.85em;color:var(--soft);background:#f5f3ee;border-radius:4px;padding:0 4px;margin-left:2px}}
.gloss::before{{display:none;content:attr(data-tip);white-space:pre-line;position:absolute;left:0;bottom:calc(100% + 9px);width:300px;max-width:72vw;background:#fffdf9;border:1px solid #e0dbd0;border-radius:10px;padding:10px 12px;font-size:13px;line-height:1.65;color:var(--ink);font-weight:400;text-align:left;letter-spacing:0;box-shadow:0 10px 26px -10px rgba(28,25,20,.28);z-index:30}}
.gloss:hover::before,.gloss:focus::before{{display:block}}
#open:checked ~ .wrap .gloss::after{{display:inline}}
#open:checked ~ .wrap .gloss:hover::before{{display:none}}
#open:checked ~ .wrap .btn-off{{display:none}}
#open:not(:checked) ~ .wrap .btn-on{{display:none}}
.glossary{{margin-top:34px;border-top:1px solid var(--line);padding-top:22px}}
.glossary h2{{font-size:13px;letter-spacing:.02em;margin:0 0 4px}}
.glossary .hint{{font-size:11.5px;color:var(--faint);margin:0 0 14px}}
.glossary dl{{margin:0;font-size:13.5px}}
.glossary dt{{font-weight:600;margin-top:11px}}
.glossary dd{{margin:2px 0 0;color:var(--soft);font-size:12.5px;line-height:1.65}}
.glossary .mi{{color:var(--faint)}}
.empty{{color:var(--faint);font-size:12.5px;line-height:1.7}}
.foot{{margin-top:26px;font-size:11.5px;color:var(--faint);line-height:1.8}}
</style>
</head>
<body>
<input class="modeinput" type="checkbox" id="open">
<div class="wrap">
  <div class="bar">
    <span><b>术语注释层</b> · 标注 {hits} 处 / {nterms} 个术语（L1 {l1}）</span>
    <span>
      <label for="open" class="btn btn-off">全部展开注释</label>
      <label for="open" class="btn btn-on">收起注释</label>
    </span>
  </div>
  <article>{body}</article>
  {glossary}
  <p class="foot">原文逐字未改，注释是渲染时外挂的 span；复制正文得到的是干净原文。<br>
  词源 data/glossary.json（{total} 条）。悬停或 Tab 聚焦带下划线的词看解释。</p>
</div>
</body>
</html>
"""


def build_output(body, stats, total, title):
    if stats["terms"]:
        items = sorted(stats["terms"].items(), key=lambda kv: kv[1])
        dl = []
        for term, _ in items:
            e = stats["entries"][term]
            dl.append(f"<dt>{html.escape(term, quote=False)}"
                      f"<span style='font-weight:400;color:#8b949e;font-size:11.5px'>　{LEVEL_NAME[e['level']]}</span></dt>")
            dl.append(f"<dd>{html.escape(e['brief'], quote=False)}<br>"
                      f"<span class='mi'>别理解成：{html.escape(e['misread'], quote=False)}</span></dd>")
        glossary_block = (
            "<div class='glossary'><h2>本文术语</h2>"
            "<p class='hint'>按首次出现顺序排列，标注为影响结论的词需要重点记。</p>"
            "<dl>" + "".join(dl) + "</dl></div>"
        )
    else:
        glossary_block = ("<div class='glossary'><h2>本文术语</h2>"
                          "<p class='empty'>没有命中词库内的术语。可能是这段回复确实不需要解释，"
                          "也可能是词库还没覆盖——把新词补进 data/glossary.json 即可。</p></div>")

    return PAGE.format(
        title=html.escape(title, quote=False),
        hits=stats["hits"],
        nterms=len(stats["terms"]),
        l1=stats["levels"][1],
        total=total,
        body=body,
        glossary=glossary_block,
    )


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(description="给 agent 回复加一层可悬停的术语注释")
    ap.add_argument("input", nargs="?", help="输入的 markdown/txt 文件；省略则读 stdin")
    ap.add_argument("-o", "--out", help="输出 HTML 路径（默认同名 .gloss.html）")
    ap.add_argument("-g", "--glossary", default=str(DEFAULT_GLOSSARY), help="词库 JSON 路径")
    ap.add_argument("--all", action="store_true", help="每一处出现都标注（默认只标首次）")
    ap.add_argument("--check", action="store_true", help="只校验词库，不渲染")
    args = ap.parse_args()

    if args.check:
        sys.exit(0 if check_glossary(args.glossary) else 1)

    if args.input:
        src = Path(args.input)
        text = src.read_text(encoding="utf-8")
        out_path = Path(args.out) if args.out else src.with_suffix(".gloss.html")
        title = src.stem
    else:
        text = sys.stdin.read()
        out_path = Path(args.out) if args.out else Path("stdin.gloss.html")
        title = "stdin"

    data, index, order = load_glossary(args.glossary)
    seen = set()
    stats = {"hits": 0, "terms": {}, "levels": {1: 0, 2: 0, 3: 0}, "entries": {}}
    body = render_markdown(text, index, order, seen, args.all, stats)

    for t in stats["terms"]:
        stats["entries"][t] = index[t]

    page = build_output(body, stats, len(data["terms"]), title)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    print(f"已生成：{out_path}")
    print(f"  标注 {stats['hits']} 处 / {len(stats['terms'])} 个术语"
          f"（L1 {stats['levels'][1]} / L2 {stats['levels'][2]} / L3 {stats['levels'][3]}）")


if __name__ == "__main__":
    main()
