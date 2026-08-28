#!/usr/bin/env python3
"""
Build the self-contained batch files handed to the audit subagents.

Each batch is a JSON array of issues carrying enough content to judge them
(title, body, trimmed comment thread, linked PRs) plus the heuristic labels
under `ASSIGNED`. Agents never touch the network or the main dataset.

  python3 make_batches.py --out DIR [--batches 13] [--numbers a,b,c]

With no --numbers, selects every issue where an LLM judgement can change the
output: buckets 1-3 plus the `unclear` review queue. Bucket-0 rows are excluded
because bucket membership is gated on signal_score, which is arithmetic over hard
counts -- but note that correcting `solved` labels shifts the self-relative
percentile cuts, so re-check coverage after folding an audit back in.
"""
import json, os, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def trim(s, n):
    s = s or ""
    return s[:n] + ("\n...[truncated]" if len(s) > n else "")


def pack(r, bucket=None):
    cs = r["comments"]
    keep = cs[:2] + cs[-6:] if len(cs) > 8 else cs   # opening context + resolution
    return {
        "number": r["number"], "url": r["url"], "title": r["title"],
        "state": r["state"], "labels": r["labels"], "created_at": r["created_at"][:10],
        "body": trim(r["body"], 3000), "n_comments_total": len(cs),
        "comments": [{"user": c["user"], "assoc": c["assoc"], "body": trim(c["body"], 1500)} for c in keep],
        "linked_prs": [{"number": p["number"], "merged": bool(p.get("merged_at")),
                        "title": (p.get("title") or "")[:90]} for p in r["linked_prs"]],
        "close_commits": r["close_commits"],
        "ASSIGNED": {
            "bucket": bucket if bucket is not None else r["bucket"],
            "resolution_class": r["resolution_class"], "solved": r["solved"],
            "resolution_evidence": r["resolution_evidence"],
            "relevancy_score": r["relevancy_score"],
            "relevancy_hits": {"pos": r["relevancy_parts"]["pos_hits"],
                               "neg": r["relevancy_parts"]["neg_hits"]},
            "is_meta": r["is_meta"], "signal_score": r["signal_score"],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--batches", type=int, default=13)
    ap.add_argument("--start", type=int, default=1, help="first batch number")
    ap.add_argument("--numbers", default="", help="comma-separated issue numbers")
    ap.add_argument("--src", default=os.path.join(ROOT, "dataset", "issues.jsonl"))
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.src)]
    if a.numbers:
        want = {int(x) for x in a.numbers.split(",")}
        target = [r for r in rows if r["number"] in want]
    else:
        target = [r for r in rows if r["bucket"] in (1, 2, 3) or r["resolution_class"] == "unclear"]

    os.makedirs(a.out, exist_ok=True)
    n = max(1, a.batches)
    batches = [[] for _ in range(n)]
    # round-robin, so no agent gets a homogeneous slice of one resolution_class
    for i, r in enumerate(sorted(target, key=lambda x: x["number"])):
        batches[i % n].append(pack(r))

    for i, b in enumerate(batches, a.start):
        if not b:
            continue
        p = os.path.join(a.out, f"batch_{i:02d}.json")
        json.dump(b, open(p, "w"), indent=1)
        print(f"batch_{i:02d}: {len(b)} issues")
    print(f"{len(target)} issues across {sum(1 for b in batches if b)} batches")


if __name__ == "__main__":
    main()
