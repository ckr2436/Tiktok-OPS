#!/usr/bin/env python3
import json, sys, pathlib, re

root = pathlib.Path(__file__).resolve().parents[1] / "docs" / "tiktok_api"
idx  = json.loads((root / "index.json").read_text(encoding="utf-8"))

q = " ".join(sys.argv[1:]).strip().lower()
if not q:
    print("Usage: python tools/search_api_docs.py <keywords>")
    sys.exit(1)

tokens = [t for t in re.split(r"\s+", q) if t]

def hay(x):
    return " ".join([
        x.get("id",""), x.get("title",""), x.get("endpoint",""),
        x.get("method",""), " ".join(x.get("category_path",[]))
    ]).lower()

hits = [x for x in idx if all(t in hay(x) for t in tokens)]
for h in hits[:50]:
    print(f"{h['id']:>4}  {h.get('method',''):<6}  {h.get('endpoint',''):<60}  |  {h.get('title','')}  ->  {h['page_file']}")
