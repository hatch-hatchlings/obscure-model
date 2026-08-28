#!/usr/bin/env python3
"""
Normalize, score and bucket the fetched MAVSDK issues.

Two independent scores per issue:
  signal_score     - how much the community cared / how load-bearing the issue is
  relevancy_score  - how likely it can be REPRODUCED AND VERIFIED on a headless
                     cloud Linux VM (SITL, build, mavsdk_server, CLI) rather than
                     needing physical flight hardware or a phone.

Then a derived resolution class for closed issues, giving the three buckets:
  1  open   + high signal
  2  closed + high signal + NOT actually resolved
  3  closed + high signal + genuinely resolved (has ground-truth answer)

Every sub-score is emitted alongside the total so the weights below can be
retuned without re-deriving anything.
"""
import json, glob, os, re, csv, math, statistics, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "raw", "issues")
OUT = os.path.join(ROOT, "dataset")
os.makedirs(OUT, exist_ok=True)

MAINTAINER_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}
BOTS = {"dronecodebot", "auterionwrikebot", "github-actions[bot]", "codecov[bot]"}

# ---------------------------------------------------------------- weights ---
W_SIGNAL = {
    "comments": 0.22, "participants": 0.18, "xrefs": 0.20, "reactions": 0.08,
    "maintainer": 0.13, "reopened": 0.09, "tracked": 0.05, "recency": 0.05,
}
# Reactions get a low weight on purpose: 91% of issues in this repo have zero
# reactions, so upvotes cannot stratify the corpus (see README).

# relevancy keyword families -> (weight, regex)
POS = {
    "sitl_sim":   (1.00, r"\b(sitl|jmavsim|gazebo|gz_?sim|px4_sitl|sim_vehicle|simulat\w*|headless|make\s+px4)\b"),
    "build":      (0.85, r"(\bcmake\b|\bmake\s-j|\bcompil\w+|\blinker\b|undefined reference|\bg\+\+\b|\bgcc\b|\bclang\b|find_package|superbuild|\bninja\b|\bvcpkg\b|\bconan\b|cmakelists|\.so\b|\.a\b|pkg-config)"),
    "server_rpc": (0.85, r"(mavsdk_server|\bgrpc\b|protobuf|\bproto\b|50051|udp://|tcp://|serial://|connection[_ ]url|\bmavsdk-server\b)"),
    "docker_ci":  (0.70, r"(\bdocker\b|dockerfile|\bubuntu\b|\bdebian\b|\bapt(-get)?\b|\bcontainer\b|github action|\bci\b|workflow file)"),
    "api_logic":  (0.60, r"(\btelemetry\b|\bmission\b|\boffboard\b|\bparam(eter)?s?\b|mavlink[_ ]passthrough|\bplugin\b|\bcallback\b|\basync\b|\bawait\b|subscribe\w*|\bfuture\b|\bpromise\b)"),
    "lang":       (0.45, r"(\bpython\b|asyncio|\bc\+\+\b|\bcpp\b|\brust\b|\bpip\b|\bnpm\b|\bjava\b)"),
    "crash_repro":(0.90, r"(segfault|segmentation fault|\bcore dump\w*|stack trace|traceback|\bassert\w*|\bexception\b|\bthrow\w*\b|\bdeadlock\b|\bhang\w*\b|memory leak|\bvalgrind\b|\basan\b|sanitizer)"),
    "steps":      (0.75, r"(steps to reproduce|to reproduce|reproduc\w+|minimal example|\bmwe\b|repro\b)"),
    "logic_bug":  (0.70, r"(race condition|data race|thread[- ]safe\w*|\bmutex\b|\bconcurren\w+|"
                         r"off.by.one|\bconversion\b|\bconvert\w*\b|pars(e|ing)|encod\w+|decod\w+|"
                         r"\boverflow\b|\bunderflow\b|round(ing)?|precision|\bunit[s]?\b|"
                         r"wrong (value|result|order|sign)|incorrect\w*|\bnan\b|\bnull ?ptr\b)"),
}
NEG = {
    "flight_hw":  (1.00, r"(\bpixhawk\b|cube ?orange|cubepilot|\bfmuk?\d|\bholybro\b|\bdurandal\b|real (drone|vehicle|hardware|flight)|actual (drone|flight|hardware)|flight test|field test|\boutdoor\b|\bin flight\b|\bflew\b|\bflying\b|\bcrashed\b|\bpropeller\b|\bmotor\b|\besc\b|\bbattery\b|\barm(ed|ing)? the (drone|vehicle)|\brtk\b|gps ?fix|\bcompass\b|\bimu drift\b)"),
    "wire":       (0.70, r"(/dev/tty|\bftdi\b|serial port|\busb\b|\bsik\b|telemetry radio|\bbaud\w*|\buart\b|\brc transmitter\b|\bjoystick\b|\bgamepad\b)"),
    "mobile":     (0.90, r"(\bios\b|\bandroid\b|\bxcode\b|\bswift\b|objective-?c|\.aar\b|cocoapods|\bflutter\b|\bunity\b|\bgradle\b|\bndk\b|\bapk\b)"),
    "nonlinux":   (0.75, r"(\bwindows\b|visual studio|\bmsvc\b|\bvcxproj\b|\bmacos\b|\bmac os\b|\bm1 mac\b|\bhomebrew\b|\bxcode\b)"),
    "av_hw":      (0.55, r"(\brtsp\b|\bgstreamer\b|\bcamera\b|\bgimbal\b|\bsiyi\b|\bgopro\b|video stream|\bh264\b|\bonvif\b)"),
}
LABEL_POS = {"ci": .25, "grpc": .25, "core": .20, "plugins": .15, "bug": .20, "v3": .10}
LABEL_NEG = {"docs": .35, "marketing": .60, "windows": .25, "beginner": .10}

# resolution language
RE_ABANDON = re.compile(r"(no (more )?(answer|answers|response|responses|reply|feedback|follow ?up)|"
    r"there was no (more )?follow|no further (info|information|response)|"
    r"closing (this )?(due to|for|because of)? ?(inactivity|staleness|no response|lack of)|"
    r"\bstale\b|didn'?t hear back|haven'?t heard back|please reopen if|feel free to reopen|"
    r"i might as well close|closing for now|closing as inactive|going to close|gonna close|"
    r"if (this|it)'?s? (is )?still (an issue|a problem|relevant)|"
    r"if you have (more|any) (comments|info|information|questions)|"
    r"(steps|example) (for me )?to reproduce|can'?t reproduce|cannot reproduce|unable to reproduce)", re.I)
RE_WONTFIX = re.compile(r"(won'?t fix|wontfix|not going to (fix|implement|support)|out of scope|"
    r"by design|not a (bug|mavsdk)|belongs in|has to go into|should go (in)?to|not something we|"
    r"we don'?t (plan|intend)|not planned|upstream (issue|problem)|report (this )?(to|in) px4)", re.I)
RE_DUP = re.compile(r"(duplicate of|dupe of|closing in favou?r of|superseded by|same as #\d+)", re.I)
RE_MOVED = re.compile(r"(mov(ed|ing|e)? (to|into)|transferr?(ed|ing)? to|re-?open(ed)? (in|at)|"
    r"opened (this )?(in|at)|tracked in|continuing in|see instead) ?(https?://|#\d)", re.I)
RE_INVALID = re.compile(r"(wrong (repo|repository|place|project)|not (the )?right (repo|place)|"
    r"posted (this )?in the wrong)", re.I)

# Strongest soft evidence: an explicit pointer to the thing that fixed it.
RE_FIXREF = re.compile(r"\b(added|implemented|fixed|resolved|addressed|merged|landed|done|supported)\b"
    r"[^.\n]{0,30}?\b(in|by|via|with|through)\b[^.\n]{0,30}?"
    r"(#\d+|https://github\.com/[\w.-]+/[\w.-]+/(pull|commit)/)", re.I)
# Weaker: a maintainer declaring completion, matched only in the CLOSING region
# of the thread so an offhand "done" early on cannot mark the issue solved.
RE_FIXED = re.compile(r"(fixed (in|by|with|now)|addressed (in|by)|resolved (in|by)|"
    r"implemented (in|by|now)|released in|available in v?\d|"
    r"(this|that|it)'?s? (is |has been )?(now )?(fixed|done|implemented|merged|released)|"
    r"that'?s done|is done now|should (now )?be fixed|has been merged|landed in|"
    r"i (just |now )?(fixed|merged|added|implemented|pushed))", re.I)
RE_SELFRES = re.compile(r"(i (got|found|figured|solved|fixed|managed|resolved)|my (bad|mistake|fault)|"
    r"nevermind|never mind|turns out|it (was|turned out to be) (my|a) |sorry to bother|"
    r"works? (for me )?now|that (fixed|solved|did) it|thanks[,!]? (that|it) (worked|works|fixed)|"
    r"i'?ve solved|solved my problem|problem (is )?solved)", re.I)
RE_CODEBLOCK = re.compile(r"```")
RE_SHELL = re.compile(r"(^|\n)\s*[$#>]\s+\S|```(bash|sh|shell|console|cmake|txt)")
RE_VERSION = re.compile(r"\b(v?\d+\.\d+\.\d+|version[: ]+\d|ubuntu \d\d\.\d\d|px4 v?\d)", re.I)
# Roadmap / tracking / release-checklist issues: high signal but not a solvable
# task, so they are flagged and heavily demoted rather than trained on.
RE_META_TITLE = re.compile(r"(\broadmap\b|\bplan\b|\btracking\b|\bmeta\b|\brfc\b|\[discussion\]|"
    r"\btodo\b|out the door|\brelease\b|\bmilestone\b|\bbrainstorm\w*|\bwish ?list\b|"
    r"\bumbrella\b|what'?s next|ideas? for)", re.I)
RE_CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\]", re.M)


def is_meta(iss):
    title = iss.get("title") or ""
    body = iss.get("body") or ""
    if len(RE_CHECKBOX.findall(body)) >= 4:
        return True
    return bool(RE_META_TITLE.search(title))


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def norm_log(v, ceiling):
    return clamp(math.log1p(max(v, 0)) / math.log1p(ceiling))


def is_bot(login):
    return bool(login) and (login.lower() in BOTS or login.lower().endswith("[bot]"))


def load():
    out = []
    for p in sorted(glob.glob(os.path.join(RAW, "*.json")), key=lambda p: int(os.path.basename(p)[:-5])):
        out.append(json.load(open(p)))
    return out


# --------------------------------------------------------------- features ---
def features(rec):
    iss, comments, tl = rec["issue"], rec["comments"], rec["timeline"]
    author = (iss.get("user") or {}).get("login")

    human_comments = [c for c in comments if not is_bot((c.get("user") or {}).get("login"))]
    participants = {author} | {(c.get("user") or {}).get("login") for c in human_comments}
    participants.discard(None)
    maint = [c for c in human_comments if c.get("author_association") in MAINTAINER_ASSOC]
    maint_logins = {(c.get("user") or {}).get("login") for c in maint}

    xref_events = [t for t in tl if t.get("event") == "cross-referenced"]
    xref_prs, xref_issues = [], []
    for t in xref_events:
        src = (t.get("source") or {}).get("issue") or {}
        entry = {"number": src.get("number"), "title": src.get("title"),
                 "url": src.get("html_url"), "state": src.get("state")}
        if src.get("pull_request"):
            entry["merged_at"] = (src.get("pull_request") or {}).get("merged_at")
            xref_prs.append(entry)
        else:
            xref_issues.append(entry)
    merged_prs = [p for p in xref_prs if p.get("merged_at")]
    close_commits = [t.get("commit_id") for t in tl if t.get("event") == "closed" and t.get("commit_id")]

    rx = iss.get("reactions") or {}
    reactions = sum(rx.get(k, 0) for k in ("+1", "laugh", "hooray", "heart", "rocket", "eyes"))

    return dict(
        author=author, author_assoc=iss.get("author_association"),
        n_comments=len(human_comments), n_participants=len(participants),
        n_maintainer_comments=len(maint), maintainers=sorted(x for x in maint_logins if x),
        n_xref_prs=len(xref_prs), n_xref_merged_prs=len(merged_prs), n_xref_issues=len(xref_issues),
        xref_prs=xref_prs, xref_issues=xref_issues, close_commits=close_commits,
        reactions_total=reactions, reactions_plus1=rx.get("+1", 0),
        reopened=sum(1 for t in tl if t.get("event") == "reopened"),
        assigned=bool(iss.get("assignees")), milestoned=bool(iss.get("milestone")),
        marked_duplicate=any(t.get("event") == "marked_as_duplicate" for t in tl),
        human_comments=human_comments, maint_comments=maint,
    )


# ------------------------------------------------------------ signal score ---
def signal(f, iss):
    c = {
        "comments": norm_log(f["n_comments"], 15),
        "participants": clamp((f["n_participants"] - 1) / 5.0),
        "xrefs": norm_log(f["n_xref_prs"] + f["n_xref_issues"], 6),
        "reactions": clamp(f["reactions_total"] / 4.0),
        "maintainer": norm_log(f["n_maintainer_comments"], 5),
        "reopened": clamp(f["reopened"] / 1.5),
        "tracked": 1.0 if (f["assigned"] or f["milestoned"]) else 0.0,
        "recency": clamp((int(iss["created_at"][:4]) - 2017) / 9.0),
    }
    return round(sum(W_SIGNAL[k] * v for k, v in c.items()), 4), {k: round(v, 3) for k, v in c.items()}


# --------------------------------------------------------- relevancy score ---
def relevancy(iss, f, meta=False):
    title = iss.get("title") or ""
    body = iss.get("body") or ""
    head = f"{title}\n{body}".lower()
    thread = "\n".join((c.get("body") or "") for c in f["human_comments"]).lower()
    labels = {l["name"] for l in iss.get("labels", [])}

    pos_hits, neg_hits = {}, {}
    pos = neg = 0.0
    for name, (w, pat) in POS.items():
        h = len(re.findall(pat, head)) + 0.35 * len(re.findall(pat, thread))
        if h:
            pos_hits[name] = round(h, 1)
            pos += w * clamp(h / 2.0)
    for name, (w, pat) in NEG.items():
        h = len(re.findall(pat, head)) + 0.35 * len(re.findall(pat, thread))
        if h:
            neg_hits[name] = round(h, 1)
            neg += w * clamp(h / 2.0)

    pos = clamp(pos / 3.0)
    neg = clamp(neg / 2.2)

    struct = 0.0
    full = head + "\n" + thread
    if RE_CODEBLOCK.search(body or ""): struct += .40
    elif RE_CODEBLOCK.search(full):     struct += .20
    if RE_SHELL.search(full):           struct += .25
    if RE_VERSION.search(head):         struct += .20
    if len(body) > 400:                 struct += .15
    struct = clamp(struct)

    lab = sum(LABEL_POS.get(l, 0) for l in labels) - sum(LABEL_NEG.get(l, 0) for l in labels)

    raw = 0.62 * pos + 0.28 * struct + 0.10 * clamp(lab + .5)
    score = clamp(raw * (1.0 - 0.78 * neg))
    if meta:
        score *= 0.40
    return round(score, 4), {
        "pos": round(pos, 3), "neg": round(neg, 3), "struct": round(struct, 3),
        "label_adj": round(lab, 3), "is_meta": meta,
        "pos_hits": pos_hits, "neg_hits": neg_hits,
    }


# ------------------------------------------------------ resolution class ----
def resolution(iss, f):
    """Return (class, solved: bool|None, evidence list). GitHub's own
    state_reason is useless here - it backfills every old close as 'completed'."""
    if iss["state"] == "open":
        return "open", None, []

    ev = []
    closer = (iss.get("closed_by") or {}).get("login")
    closer_is_maint = closer in set(f["maintainers"]) or closer in {"julianoes", "JonasVautherin", "hamishwillee"}
    tail = f["human_comments"][-3:]
    tail_txt = "\n".join((c.get("body") or "") for c in tail)
    all_txt = "\n".join((c.get("body") or "") for c in f["human_comments"])
    maint_txt = "\n".join((c.get("body") or "") for c in f["maint_comments"])
    author_tail = "\n".join((c.get("body") or "") for c in tail
                            if (c.get("user") or {}).get("login") == f["author"])

    # --- hard evidence of a real fix
    if f["n_xref_merged_prs"]:
        ev.append(f"merged PR xref: {[p['number'] for p in f['xref_prs'] if p.get('merged_at')][:4]}")
        return "fixed_merged_pr", True, ev
    if f["close_commits"]:
        ev.append(f"closed by commit {f['close_commits'][0][:8]}")
        return "fixed_commit", True, ev

    if RE_INVALID.search(tail_txt):
        ev.append("misfiled issue (wrong repo/project)")
        return "invalid_misfiled", False, ev

    # --- explicit pointer to a fix outranks everything soft: "Added in #957"
    maint_fixref = RE_FIXREF.search(maint_txt)
    if maint_fixref:
        ev.append(f"maintainer points at the fix: {maint_fixref.group(0)[:70]!r}")
        return "fixed_stated", True, ev

    # --- explicit dispositions, read from the CLOSING region only
    if f["marked_duplicate"] or RE_DUP.search(tail_txt):
        ev.append("duplicate language/event")
        return "duplicate", False, ev
    if RE_MOVED.search(tail_txt):
        ev.append("moved/tracked elsewhere")
        return "moved_elsewhere", False, ev
    if RE_ABANDON.search(tail_txt):
        ev.append("abandonment language in closing comments")
        return "abandoned_no_response", False, ev
    if RE_WONTFIX.search(tail_txt):
        ev.append("wontfix / out-of-scope language")
        return "wontfix_out_of_scope", False, ev

    # --- soft resolutions
    if author_tail and RE_SELFRES.search(author_tail):
        ev.append("author states own resolution")
        return "self_resolved", True, ev
    closing_maint = "\n".join((c.get("body") or "") for c in f["maint_comments"][-3:])
    m = RE_FIXED.search(closing_maint)
    if m:
        ev.append(f"maintainer declares completion: {m.group(0)[:60]!r}")
        return "fixed_stated", True, ev
    if f["n_maintainer_comments"] >= 1 and RE_SELFRES.search(all_txt):
        ev.append("thread converges on a working answer")
        return "answered", True, ev
    if f["n_maintainer_comments"] >= 1 and len(maint_txt) > 180:
        ev.append(f"substantive maintainer answer ({len(maint_txt)} chars)")
        return "answered", True, ev

    if not f["n_comments"]:
        ev.append("closed with no discussion")
        return "closed_silently", False, ev
    if not f["n_maintainer_comments"]:
        ev.append("no maintainer ever replied")
        return "no_maintainer_reply", False, ev
    # Genuinely ambiguous: a maintainer engaged and closed it, but nothing in the
    # thread says whether it was resolved. solved=None keeps these out of BOTH
    # the gold set and the unsolved set rather than guessing wrong in either
    # direction; they are written to needs_review.jsonl for manual triage.
    ev.append("closed by maintainer, no clear resolution marker")
    return "unclear", None, ev


def code_blocks(text, limit=6):
    return [b.strip()[:4000] for b in re.findall(r"```[\w+-]*\n(.*?)```", text or "", re.S)][:limit]


# ------------------------------------------------------------------- main ---
def main():
    recs = load()
    rows = []
    for rec in recs:
        iss = rec["issue"]
        f = features(rec)
        sig, sig_parts = signal(f, iss)
        meta = is_meta(iss)
        rel, rel_parts = relevancy(iss, f, meta)
        klass, solved, ev = resolution(iss, f)

        body = iss.get("body") or ""
        answer_comments = []
        if solved:
            for c in f["maint_comments"][-4:]:
                b = (c.get("body") or "").strip()
                if len(b) > 60:
                    answer_comments.append({"user": (c.get("user") or {}).get("login"),
                                            "assoc": c.get("author_association"),
                                            "created_at": c.get("created_at"), "body": b})

        rows.append({
            "number": iss["number"], "url": iss["html_url"], "title": iss["title"],
            "state": iss["state"], "state_reason_github": iss.get("state_reason"),
            "created_at": iss["created_at"], "closed_at": iss.get("closed_at"),
            "updated_at": iss["updated_at"],
            "labels": [l["name"] for l in iss.get("labels", [])],
            "author": f["author"], "author_assoc": f["author_assoc"],
            "closed_by": (iss.get("closed_by") or {}).get("login"),
            "body": body,
            "body_code_blocks": code_blocks(body),

            "is_meta": meta,
            "signal_score": sig, "signal_parts": sig_parts,
            "relevancy_score": rel, "relevancy_parts": rel_parts,
            "combined_score": round(0.5 * sig + 0.5 * rel, 4),

            "resolution_class": klass, "solved": solved, "resolution_evidence": ev,
            "answer_comments": answer_comments,

            "n_comments": f["n_comments"], "n_participants": f["n_participants"],
            "n_maintainer_comments": f["n_maintainer_comments"],
            "reactions_total": f["reactions_total"], "reactions_plus1": f["reactions_plus1"],
            "n_xref_prs": f["n_xref_prs"], "n_xref_merged_prs": f["n_xref_merged_prs"],
            "n_xref_issues": f["n_xref_issues"], "reopened": f["reopened"],
            "linked_prs": f["xref_prs"], "linked_issues": f["xref_issues"],
            "close_commits": f["close_commits"],
            "comments": [{"user": (c.get("user") or {}).get("login"),
                          "assoc": c.get("author_association"),
                          "created_at": c.get("created_at"),
                          "body": c.get("body") or ""} for c in f["human_comments"]],
        })

    rows.sort(key=lambda r: -r["combined_score"])
    for r in rows:
        r["bucket"] = 0          # 0 = in the corpus but below the signal cut

    # ---- buckets. "high signal" = top tercile of signal within its own state,
    #      because closed issues have had years to accumulate discussion.
    open_rows = [r for r in rows if r["state"] == "open"]
    closed_rows = [r for r in rows if r["state"] == "closed"]
    solved_rows = [r for r in closed_rows if r["solved"] is True]
    unsolved_rows = [r for r in closed_rows if r["solved"] is False]
    review_rows = [r for r in closed_rows if r["solved"] is None]

    def cut(rows, q):
        """Signal threshold at quantile q within this population."""
        vals = sorted(r["signal_score"] for r in rows)
        return vals[int(q * (len(vals) - 1))] if vals else 0.0

    # Each population is thresholded against ITSELF. Unsolved issues are
    # structurally quieter (an abandoned thread stops accruing comments), so a
    # global cut would leave bucket 2 nearly empty.
    open_cut = cut(open_rows, 0.33)
    solved_cut = cut(solved_rows, 0.66)
    unsolved_cut = cut(unsolved_rows, 0.66)

    b1 = [r for r in open_rows if r["signal_score"] >= open_cut]
    b2 = [r for r in unsolved_rows if r["signal_score"] >= unsolved_cut]
    b3 = [r for r in solved_rows if r["signal_score"] >= solved_cut]
    for r in b1: r["bucket"] = 1
    for r in b2: r["bucket"] = 2
    for r in b3: r["bucket"] = 3

    review_rows.sort(key=lambda r: -r["combined_score"])
    with open(os.path.join(OUT, "needs_review.jsonl"), "w") as fh:
        for r in review_rows:
            fh.write(json.dumps(r) + "\n")
    print(f"needs_review (ambiguous resolution): {len(review_rows)}")

    for name, bucket in (("bucket1_open_high_signal", b1),
                         ("bucket2_closed_high_signal_unsolved", b2),
                         ("bucket3_closed_high_signal_solved", b3)):
        bucket.sort(key=lambda r: -r["combined_score"])
        with open(os.path.join(OUT, name + ".jsonl"), "w") as fh:
            for r in bucket:
                fh.write(json.dumps(r) + "\n")
        print(f"{name}: {len(bucket)}")

    with open(os.path.join(OUT, "issues.jsonl"), "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    cols = ["bucket", "number", "state", "resolution_class", "solved", "is_meta", "signal_score",
            "relevancy_score", "combined_score", "n_comments", "n_participants",
            "n_xref_merged_prs", "reactions_total", "reopened", "labels", "created_at",
            "title", "url"]
    with open(os.path.join(OUT, "ranked_summary.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "labels": "|".join(r["labels"])})

    print(f"\ntotal {len(rows)} | cuts: open {open_cut:.3f} solved {solved_cut:.3f} unsolved {unsolved_cut:.3f}")
    print(f"meta/roadmap issues flagged: {sum(1 for r in rows if r['is_meta'])}")
    print("resolution classes:", dict(collections.Counter(r["resolution_class"] for r in rows).most_common()))
    print("solved:", collections.Counter(r["solved"] for r in rows))


if __name__ == "__main__":
    main()
