#!/usr/bin/env python3
"""
Fold the subagent audit verdicts back into the dataset.

Reads scratchpad/verify/out/batch_*.json (one verdict per audited issue), reports
heuristic-vs-audit agreement, and writes a corrected dataset where the audit
overrides the regex label. Nothing is silently overwritten: every corrected row
keeps the original label in `heuristic_*` fields plus a `verified` block.
"""
import json, glob, os, sys, collections, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dataset")
VER = os.environ["VERIFY_DIR"]

# verdicts live in <VERIFY_DIR>/verdicts (committed layout); the scratchpad run
# used <VERIFY_DIR>/out, so accept either.
paths = sorted(glob.glob(os.path.join(VER, "verdicts", "batch_*.json"))) \
     or sorted(glob.glob(os.path.join(VER, "out", "batch_*.json")))
if not paths:
    sys.exit(f"no verdict files under {VER}/verdicts or {VER}/out")

verdicts, dupes = {}, []
for p in paths:
    try:
        data = json.load(open(p))
    except Exception as e:
        print(f"!! {os.path.basename(p)} unreadable: {e}", file=sys.stderr)
        continue
    for v in data:
        n = v.get("number")
        if n in verdicts:
            dupes.append(n)
        verdicts[n] = v
print(f"verdicts loaded: {len(verdicts)} (from {len(paths)} batches)"
      + (f", {len(dupes)} duplicates" if dupes else ""))

rows = [json.loads(l) for l in open(os.path.join(OUT, "issues.jsonl"))]
audited = [r for r in rows if r["number"] in verdicts]
missing = [r["number"] for r in rows
           if r["number"] not in verdicts and (r["bucket"] in (1, 2, 3) or r["resolution_class"] == "unclear")]
print(f"audited {len(audited)} rows; {len(missing)} expected-but-missing"
      + (f": {missing[:20]}" if missing else ""))

# ------------------------------------------------------------- agreement ---
def norm(v):
    return v if v in (True, False, None) else None

solved_agree = [r for r in audited if norm(verdicts[r["number"]].get("solved_verdict")) == r["solved"]]
rel_deltas = [abs(verdicts[r["number"]].get("relevancy_verdict", 0) - r["relevancy_score"]) for r in audited]
rel_agree = [d for d in rel_deltas if d <= 0.20]

print(f"\n=== AGREEMENT ===")
print(f"solved:     {len(solved_agree)}/{len(audited)} = {100*len(solved_agree)/max(len(audited),1):.1f}%")
print(f"relevancy:  {len(rel_agree)}/{len(audited)} = {100*len(rel_agree)/max(len(audited),1):.1f}% within 0.20"
      f"  | mean |delta| {statistics.mean(rel_deltas):.3f}, median {statistics.median(rel_deltas):.3f}")

print("\nper-bucket solved agreement:")
for b in (1, 2, 3, 0):
    sub = [r for r in audited if r["bucket"] == b]
    if not sub: continue
    ok = [r for r in sub if norm(verdicts[r["number"]].get("solved_verdict")) == r["solved"]]
    lbl = {1: "b1 open", 2: "b2 unsolved", 3: "b3 solved", 0: "needs_review"}[b]
    print(f"  {lbl:<14} {len(ok):>3}/{len(sub):<3} = {100*len(ok)/len(sub):5.1f}%")

print("\ndisagreement directions (heuristic -> audit):")
for k, c in collections.Counter(
        (str(r["solved"]), str(norm(verdicts[r["number"]].get("solved_verdict"))))
        for r in audited if norm(verdicts[r["number"]].get("solved_verdict")) != r["solved"]).most_common():
    print(f"  {k[0]:>5} -> {k[1]:<5} {c}")

print("\nconfidence on disagreements:",
      dict(collections.Counter(verdicts[r["number"]].get("confidence")
           for r in audited if norm(verdicts[r["number"]].get("solved_verdict")) != r["solved"])))

# ------------------------------------------------------------- corrected ---
for r in rows:
    v = verdicts.get(r["number"])
    if not v:
        r["verified"] = None
        continue
    sv, rv = norm(v.get("solved_verdict")), v.get("relevancy_verdict")
    r["verified"] = {
        "solved_audit": sv, "relevancy_audit": rv,
        "cloud_verifiable": v.get("cloud_verifiable"),
        "confidence": v.get("confidence"), "note": v.get("note") or "",
        "solved_changed": sv != r["solved"],
        "relevancy_delta": round((rv - r["relevancy_score"]), 3) if isinstance(rv, (int, float)) else None,
    }
    r["heuristic_solved"] = r["solved"]
    r["heuristic_relevancy_score"] = r["relevancy_score"]
    r["solved"] = sv
    if isinstance(rv, (int, float)):
        # average the two so a single audit call cannot swing the ranking wildly
        r["relevancy_score"] = round((r["relevancy_score"] + rv) / 2, 4)
    r["combined_score"] = round(0.5 * r["signal_score"] + 0.5 * r["relevancy_score"], 4)

# ------------------------------------------------------------- re-bucket ---
open_rows = [r for r in rows if r["state"] == "open"]
closed = [r for r in rows if r["state"] == "closed"]
solved_rows = [r for r in closed if r["solved"] is True]
unsolved_rows = [r for r in closed if r["solved"] is False]
review_rows = [r for r in closed if r["solved"] is None]

def cut(rs, q):
    vals = sorted(x["signal_score"] for x in rs)
    return vals[int(q * (len(vals) - 1))] if vals else 0.0

for r in rows: r["bucket"] = 0
b1 = [r for r in open_rows if r["signal_score"] >= cut(open_rows, 0.33)]
b2 = [r for r in unsolved_rows if r["signal_score"] >= cut(unsolved_rows, 0.66)]
b3 = [r for r in solved_rows if r["signal_score"] >= cut(solved_rows, 0.66)]
for r in b1: r["bucket"] = 1
for r in b2: r["bucket"] = 2
for r in b3: r["bucket"] = 3

rows.sort(key=lambda r: -r["combined_score"])
with open(os.path.join(OUT, "issues.verified.jsonl"), "w") as fh:
    for r in rows: fh.write(json.dumps(r) + "\n")
for name, bucket in (("bucket1_open_high_signal", b1),
                     ("bucket2_closed_high_signal_unsolved", b2),
                     ("bucket3_closed_high_signal_solved", b3),
                     ("needs_review", review_rows)):
    bucket.sort(key=lambda r: -r["combined_score"])
    with open(os.path.join(OUT, name + ".verified.jsonl"), "w") as fh:
        for r in bucket: fh.write(json.dumps(r) + "\n")

print(f"\n=== AFTER AUDIT ===")
print(f"bucket1 open      {len(b1):>4}")
print(f"bucket2 unsolved  {len(b2):>4}")
print(f"bucket3 solved    {len(b3):>4}")
print(f"needs_review      {len(review_rows):>4}")
moved = [r for r in rows if r.get("verified") and r["verified"]["solved_changed"]]
print(f"\nrows whose solved label the audit changed: {len(moved)}")
