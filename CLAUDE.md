# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A coding model that solves niche / obscure engineering problems in firmware, hard tech,
and research. The repo has two parts today: `data/github-issues/` (a data-prep pipeline
turning GitHub issue threads into fine-tuning data) and `training/` (scripts for pulling
and running the base model on scratch storage).

## Model and storage

The base model is `Qwen/Qwen3-30B-A3B-Base`, kept off-repo on scratch storage at
`/mnt/gs21/scratch/moham147/obscure-model/models/Qwen3-30B-A3B-Base` (too large for git).

- `training/download_qwen_model.py` — pulls the model via `huggingface_hub.snapshot_download`
  into that scratch path (`--model-id`/`--out-dir` to override).
- `training/run_sample_prompt.py` — loads it and runs a single completion. It's a **base**
  model, not instruct-tuned: pass raw completion-style prompt text (e.g. a `//` comment plus
  an open function signature), not chat-formatted messages.

## data/github-issues/ pipeline

Turns every issue in `mavlink/MAVSDK` (971 issues, PRs excluded) into scored, bucketed
fine-tuning data. Full design rationale is in `data/github-issues/README.md` — read it
before touching scoring or bucketing logic, it explains *why* the scoring works this way,
not just what it does. Key things not to relearn the hard way:

**Pipeline stages, each a pure function of the previous stage's output:**
```
scripts/fetch.py              GitHub REST -> raw/issues/<number>.json   (resumable, cached on updated_at)
scripts/score.py              raw/ -> dataset/*.jsonl                   (deterministic, no network)
scripts/make_batches.py       dataset/ -> audit/inputs/                 (packets for audit subagents)
scripts/apply_verification.py audit/verdicts/ -> dataset/*.verified.jsonl
```

Run them with:
```bash
python3 data/github-issues/scripts/fetch.py                      # ~200s cold, ~10 API calls warm
python3 data/github-issues/scripts/score.py                      # seconds, no network
VERIFY_DIR=data/github-issues/audit python3 data/github-issues/scripts/apply_verification.py
```
`fetch.py` needs a GitHub token via `gh auth token` (reads it with `os.popen`, so `gh` must
be authenticated). `REPO` env var overrides the target repo (default `mavlink/MAVSDK`).

**`audit/verdicts/` is the one non-regenerable directory in this repo.** It's the output of
16 parallel LLM subagents judging issue threads; every other dataset file can be rebuilt
from `raw/` + the scripts, but re-running the audit produces different labels and would
silently overwrite 152 manually-adjudicated `solved` corrections. Treat it as source data.
`audit/inputs/` is regenerable via `make_batches.py` and is kept only so the exact prompts
handed to those subagents stay auditable.

**Use the `*.verified.jsonl` files, not the unaudited `*.jsonl` ones**, for anything
downstream (training data, evaluation). The audit exists because the regex-based
`resolution_class` labeler is only 54.6% accurate on its largest class (`answered`) —
see the README's agreement tables before trusting `solved` on an unaudited row.

**Two scores per issue, `signal_score` and `relevancy_score`**, both 0–1 with
sub-components emitted alongside the total (weights at the top of `score.py`).
`relevancy_score` encodes "can this be reproduced and verified on a headless cloud Linux
VM" — SITL/build/gRPC/CI evidence scores positive, flight-hardware/wiring/mobile/non-Linux
evidence discounts (not zeroes) it. If you touch this scoring, the README documents three
concrete defects the audit already found and fixed (`hitl` mis-classified as positive,
pure code-level bugs scoring near zero, Windows/macOS builds scoring too high) — don't
reintroduce them.

**Bucketing cuts are self-relative, not global**: open/solved/unsolved issues each get
their own percentile threshold (open @ 33rd, closed populations @ 66th each), because
unsolved threads are structurally quieter and a single global cut starved bucket 2.
Every row in `issues.jsonl` carries a `bucket` field (0 = below cut, 1/2/3 = the buckets);
the per-bucket files are just pre-filtered views of the same master file.
