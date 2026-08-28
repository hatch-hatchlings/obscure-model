# MAVSDK issue corpus

Every issue from [`mavlink/MAVSDK`](https://github.com/mavlink/MAVSDK) (971 issues; the
2,070 PRs sharing the same number space are excluded), scored and split into the three
buckets for fine-tuning data prep.

## Layout

```
scripts/fetch.py              GitHub REST -> raw/issues/<number>.json   (resumable, cached)
scripts/score.py              raw/ -> dataset/*.jsonl                   (pure function of raw/)
scripts/make_batches.py       dataset/ -> audit/inputs/                 (subagent work packets)
scripts/apply_verification.py audit/verdicts/ -> dataset/*.verified.jsonl

raw/issues/*.json     971 files: issue + full comment thread + full timeline
raw/issues_index.json
audit/inputs/         16 batch files handed to the audit subagents
audit/verdicts/       17 files, 467 verdicts  <- NOT REGENERABLE, see below
dataset/
  issues.jsonl / issues.verified.jsonl        all 971, scored, ranked
  bucket1_open_high_signal[.verified].jsonl        28
  bucket2_closed_high_signal_unsolved[.verified].jsonl   38 -> 55
  bucket3_closed_high_signal_solved[.verified].jsonl    252 -> 242
  needs_review[.verified].jsonl                     85 -> 66
  ranked_summary.csv                          all 971, flat columns
```

### Reproducing

```bash
python3 scripts/fetch.py                      # ~200s cold, ~10 API calls warm
python3 scripts/score.py                      # seconds, no network
VERIFY_DIR=audit python3 scripts/apply_verification.py
```

`fetch.py` caches on `updated_at`, so re-running costs almost nothing unless issues
actually changed. `score.py` is deterministic and re-derives everything from `raw/`.

**`audit/verdicts/` cannot be regenerated.** It is the output of 16 LLM subagents judging
threads; re-running them produces *different* labels, and it encodes 152 `solved`
corrections. Everything else here is reproducible from `raw/` plus these scripts — this
directory is not. Treat it as source data, not build output. `audit/inputs/` is
regenerable via `make_batches.py` and is kept only so the exact prompt payloads are
auditable.

## Two things about this repo that changed the design

**1. Upvotes do not exist here.** Only 83 of 971 issues have *any* reaction, and the
maximum is 8. Ranking by 👍 would produce noise. Signal is therefore carried by
comment volume, distinct participants, cross-references from other issues/PRs, maintainer
engagement, and reopens — with reactions kept at a low weight (0.08) and still reported
per-issue as `reactions_total` in case you want them back.

**2. GitHub's `state_reason` is useless for solved-vs-unsolved.** It reports 929
"completed" and 2 "not planned", because GitHub backfilled every pre-2022 closure as
"completed" regardless of what happened. Resolution is therefore *derived* — see below.

## Scores

Each is 0–1, and every sub-component is emitted next to the total so you can retune
without re-deriving. Weights live at the top of `score.py`.

`signal_score` — comments, participants, cross-refs, maintainer comments, reopens,
assigned/milestoned, recency.

`relevancy_score` — **your definition: can this be reproduced and verified on a headless
cloud Linux VM?** Positive families: SITL/sim, build/CMake/linker, `mavsdk_server`/gRPC,
Docker/CI, API semantics, crash-repro (segfault/traceback), explicit repro steps. Negative
families: flight hardware (Pixhawk, real flight, RTK, motors), wiring (`/dev/tty`, USB,
serial), mobile (iOS/Android/Xcode), non-Linux (Windows/macOS), and A/V hardware
(camera/gimbal/RTSP). Plus a structural bonus for fenced code blocks, shell commands and
version strings. Hardware evidence discounts the score by up to 78% rather than zeroing it,
since e.g. a gimbal *plugin API* bug may still be SITL-testable.

`combined_score` = mean of the two. This is the sort order.

`is_meta` — roadmap/tracking/release-checklist issues ("v4 plan", "Work to get v2.0 out the
door"). High signal but not solvable tasks, so relevancy is cut to 40%. 26 flagged.

## Resolution classes

`solved` is a **three-state** field: `true`, `false`, or `null` for genuinely ambiguous.

| solved | classes | n |
|---|---|---|
| `true` | `fixed_merged_pr` 329, `answered` 299, `fixed_stated` 71, `self_resolved` 37, `fixed_commit` 2 | 738 |
| `false` | `abandoned_no_response` 59, `no_maintainer_reply` 14, `closed_silently` 13, `duplicate` 8, `wontfix_out_of_scope` 7, `moved_elsewhere` 4, `invalid_misfiled` 3 | 108 |
| `null` | `unclear` 85, `open` 40 | 125 |

The ladder, strongest evidence first: cross-referenced **merged** PR → closing commit →
misfiled → an explicit pointer to the fix (`"Added in #957"`) → duplicate/moved/abandoned/
wontfix language *in the closing comments only* → author declaring self-resolution →
maintainer declaring completion in the last 3 maintainer comments → substantive maintainer
answer. `resolution_evidence` records which rule fired and the matched text, so every call
is auditable.

`unclear` means a maintainer engaged and closed it but nothing states the outcome. Those
85 go to `needs_review.jsonl` and are kept out of **both** bucket 2 and bucket 3 — guessing
would either poison the gold set or fake an unsolved problem. Triage them separately if you
want the extra volume.

### Measured accuracy: 16-agent audit

Every bucketed issue plus the review queue — 467 in total, **100% of the rows where an LLM
judgement affects the output** — was independently re-judged by 16 parallel subagents (~31
each), blind to each other. Bucket-0 rows were skipped: bucket membership is gated on
`signal_score`, which is arithmetic over hard counts.

That skip needed two follow-up rounds. Correcting 152 `solved` labels changed the solved/
unsolved populations, which shifted the self-relative percentile cuts, which pulled
previously-unbucketed rows into bucket 3 unaudited: 57 after round 1, 6 after round 2, 1
after round 3 (adjudicated directly). Auditing changes the thing the buckets are computed
from, so coverage has to be re-checked after each fold, not assumed.

**Agreement on `solved`: 314/466 = 67.4%.** By class (round 1, n=403):

| resolution_class | n | agree | rate |
|---|---:|---:|---|
| `open` | 28 | 28 | **100%** |
| `fixed_merged_pr` | 123 | 107 | **87.0%** |
| `abandoned_no_response` | 27 | 23 | 85.2% |
| `fixed_stated` | 26 | 19 | 73.1% |
| `self_resolved` | 6 | 4 | 66.7% |
| `answered` | 97 | 53 | **54.6%** |
| `unclear` | 85 | 22 | 25.9% |

1. **`fixed_merged_pr` holds at 87%** — a merged-PR cross-reference is the strongest
   evidence available. It fails the way you would expect (#2620 was marked fixed because a
   merged PR mentioned it, but that PR fixed a different bug and the thread ends "No more
   responses… closing"), so: good evidence, not proof.
2. **`answered` at 54.6% is the weak link, and it is the second-largest class (299 repo-wide).**
   Its rule — "a maintainer wrote >180 characters" — cannot separate an answer that resolved
   the problem from an explanation of why it is hard. Disagreements run `True -> None` (42)
   and `True -> False` (38): it systematically over-claims resolution. **This is the main
   reason to prefer the audited labels.**
3. **`unclear` at 25.9% is not failure** — that class means "cannot tell", and the auditors
   resolved most of it into real verdicts.

Four independent auditors converged on the same two mechanisms: regex misses resolutions
phrased non-formulaically ("SOLVED!!!!", "Implemented, see example"), and over-trusts a
substantive answer or a merged-PR cross-reference without checking the PR matches the ask.

The disagreement rate is not uniform. The 57 gap rows — which sit just under the original
cut and skew toward `fixed_merged_pr` — disagreed at 18%, versus 35% in the main batches.

**Agreement on `relevancy`: 73.2% within +/-0.20**, mean |delta| 0.136, mean *signed* delta
+0.08 (the keyword scorer reads slightly low). Three defects it exposed, all fixed in
`score.py`:

- **`hitl` sat in the positive sim family.** Hardware-in-the-loop needs a real flight
  controller — a term on the wrong side of the ledger, not a weight to tune.
- **Pure code-level defects scored near zero** — race conditions, unit conversions, parser
  bugs are trivially cloud-reproducible but carry no SITL/build/gRPC vocabulary. Added a
  `logic_bug` family. This is the structural limit of keyword matching and the single best
  argument for having run the audit.
- **Windows/macOS-only build issues scored too high.** They cannot run on a Linux VM at
  all; `nonlinux` went 0.45 -> 0.75.

### How much to trust this

The audit is Sonnet judging threads, not ground truth. Where it disagrees with the regex it
is usually right — it reads "SOLVED!!!!" and "that's a PX4 issue" correctly where regex
cannot — but of 152 corrections, 82 were logged at *medium* confidence and 9 at *low*, and
no auditor-vs-auditor adjudication was run. What this dataset contains is **two independent
noisy labelers agreeing 67% of the time, with disagreements concentrated in one class whose
rule is demonstrably broken**. Better than one labeler; not verified truth.

Both labels survive per row, so a human pass buys the most on the medium-confidence
corrections sitting in bucket 3. Filter them with:

```
jq 'select(.bucket==3 and .verified.solved_changed and .verified.confidence=="medium")' \
   dataset/issues.verified.jsonl
```

### Which files to use

`*.verified.jsonl` are the audited set and are what you should train on. Each corrected row
keeps `heuristic_solved` and `heuristic_relevancy_score` next to a `verified` block
(`solved_audit`, `relevancy_audit`, `cloud_verifiable`, `confidence`, `note`,
`solved_changed`, `relevancy_delta`), so every change is inspectable and reversible.
`solved` is replaced by the audit verdict; `relevancy_score` is the **mean** of the two
scores, so one auditor call cannot swing the ranking on its own. Buckets are recomputed
from the audited labels.

The unaudited `*.jsonl` files are kept for comparison. After the audit:
bucket 1 = 28, bucket 2 = 51 (up from 38), bucket 3 = 246 (down from 252),
needs_review = 64 (down from 85). 141 rows changed `solved`.

## Bucketing

"High signal" is a threshold **within each population, not globally**: open issues at the
33rd percentile, solved and unsolved closed issues each at their own 66th. Unsolved issues
are structurally quieter — an abandoned thread stops accruing comments — so a single global
cut left bucket 2 with 14 issues. Self-relative cuts give 38. Adjust in `main()`.

Every row in `issues.jsonl` carries a `bucket` field (0 = in the corpus but below the
signal cut, 1/2/3 = the buckets), so the master file alone is enough — the per-bucket
files are just pre-filtered views of it. 250 of the 252 bucket-3 rows carry a linked PR
or answer text; the other 2 have no extractable ground truth.

## Per-issue fields

Identity (`number`, `url`, `title`, `state`, `created_at`, `closed_at`, `labels`, `author`,
`closed_by`), content (`body`, `body_code_blocks`, full `comments[]` with author +
association, bot comments stripped), scores (`signal_score`/`signal_parts`,
`relevancy_score`/`relevancy_parts` incl. the exact keyword hit counts, `combined_score`,
`is_meta`), resolution (`resolution_class`, `solved`, `resolution_evidence`,
`answer_comments` — the maintainer comments constituting the answer, for solved issues),
and links (`linked_prs` with `merged_at`, `linked_issues`, `close_commits`).

For bucket 3, `answer_comments` + `linked_prs` are the ground truth. `linked_prs[].url`
gives you the PR; append `.diff` or `.patch` to fetch the actual change if you want the
diff as the target.
