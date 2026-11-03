# -*- coding: utf-8 -*-
import sys
import re

sys.stdout.reconfigure(encoding="utf-8")

for path in ["docs/PROPOSAL-BearRun-提交版.md", "docs/PRD-BearRun-MVP-提交版.md"]:
    raw = open(path, encoding="utf-8").read()
    lines = raw.replace("\r\n", "\n").split("\n")
    print("=" * 76)
    print(path)
    print("  文件 %d 字符 / %d 行" % (len(raw), len(lines)))
    cur = "(前言)"
    stats = {}

    def touch(k):
        if k not in stats:
            stats[k] = {"chars": 0, "tblrows": 0, "tables": 0, "imgs": 0, "para": 0, "list": 0}

    touch(cur)
    prev_tbl = False
    for ln in lines:
        s = ln.strip()
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        if m and len(m.group(1)) <= 2:
            cur = m.group(2).strip()
            touch(cur)
            prev_tbl = False
            continue
        stats[cur]["chars"] += len(s)
        if s.startswith("|"):
            stats[cur]["tblrows"] += 1
            if not prev_tbl:
                stats[cur]["tables"] += 1
            prev_tbl = True
        else:
            prev_tbl = False
            if re.match(r"^[-*]\s+", s) or re.match(r"^\d+\.\s+", s):
                stats[cur]["list"] += 1
            elif s:
                stats[cur]["para"] += 1
        if "docs/images/" in s:
            stats[cur]["imgs"] += len(re.findall(r"docs/images/", s))

    for k, v in stats.items():
        print("  %-44s chars=%5d tables=%2d tblrows=%3d para=%3d list=%3d imgs=%d"
              % (k[:44], v["chars"], v["tables"], v["tblrows"], v["para"], v["list"], v["imgs"]))
