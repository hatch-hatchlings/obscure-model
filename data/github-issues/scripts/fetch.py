#!/usr/bin/env python3
"""
Fetch every issue in mavlink/MAVSDK (excluding PRs) with its full comment thread
and timeline, and cache one JSON file per issue on disk.

Resumable: an issue is re-fetched only if its `updated_at` changed since the
cached copy. Re-running after the first pass costs ~10 list calls.
"""
import json, os, sys, time, threading
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO = os.environ.get("REPO", "mavlink/MAVSDK")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "raw")
ISSUE_DIR = os.path.join(RAW, "issues")
os.makedirs(ISSUE_DIR, exist_ok=True)

TOKEN = os.popen("gh auth token").read().strip()
if not TOKEN:
    sys.exit("no github token; run `gh auth login`")

API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "mavsdk-issue-harvest",
}

_lock = threading.Lock()
_stats = {"calls": 0, "retries": 0}


def api(path, params=None):
    """GET one page. Returns (parsed_json, link_header). Retries on 403/429/5xx."""
    url = path if path.startswith("http") else API + path
    if params:
        sep = "&" if "?" in url else "?"
        url += sep + urllib.parse.urlencode(params)
    delay = 2.0
    for attempt in range(8):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                with _lock:
                    _stats["calls"] += 1
                return json.loads(r.read().decode()), r.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            # secondary rate limit / abuse detection / server hiccup
            if e.code in (403, 429, 500, 502, 503, 504):
                reset = e.headers.get("X-RateLimit-Reset")
                remaining = e.headers.get("X-RateLimit-Remaining")
                wait = delay
                if remaining == "0" and reset:
                    wait = max(delay, int(reset) - time.time() + 5)
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    wait = max(wait, float(retry_after))
                with _lock:
                    _stats["retries"] += 1
                sys.stderr.write(f"  [{e.code}] backoff {wait:.0f}s :: {url[-60:]} {body[:80]}\n")
                time.sleep(min(wait, 300))
                delay = min(delay * 2, 120)
                continue
            if e.code == 404:
                return None, ""
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            with _lock:
                _stats["retries"] += 1
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"giving up on {url}")


def paginate(path, params=None, cap=None):
    """Follow Link: rel=next until exhausted."""
    out = []
    params = dict(params or {})
    params.setdefault("per_page", 100)
    url, first = path, True
    while url:
        data, link = api(url, params if first else None)
        first = False
        if not data:
            break
        out.extend(data)
        if cap and len(out) >= cap:
            break
        url = ""
        for part in link.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                break
    return out


def list_all_issues():
    """Every issue+PR stub, ascending by number. PRs filtered out by caller."""
    print("listing issues (state=all)...", flush=True)
    items = paginate(f"/repos/{REPO}/issues",
                     {"state": "all", "sort": "created", "direction": "asc"})
    issues = [i for i in items if "pull_request" not in i]
    prs = len(items) - len(issues)
    print(f"  {len(items)} items -> {len(issues)} issues, {prs} PRs skipped", flush=True)
    return issues


def enrich(stub):
    """Fetch comments + timeline for one issue, unless cache is current."""
    n = stub["number"]
    path = os.path.join(ISSUE_DIR, f"{n}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                cached = json.load(f)
            if cached.get("issue", {}).get("updated_at") == stub["updated_at"]:
                return n, "cached"
        except Exception:
            pass
    comments = paginate(f"/repos/{REPO}/issues/{n}/comments")
    timeline = paginate(f"/repos/{REPO}/issues/{n}/timeline")
    rec = {"issue": stub, "comments": comments, "timeline": timeline,
           "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)
    return n, "fetched"


def main():
    t0 = time.time()
    issues = list_all_issues()
    with open(os.path.join(RAW, "issues_index.json"), "w") as f:
        json.dump(issues, f)

    done = {"cached": 0, "fetched": 0}
    errors = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(enrich, s): s["number"] for s in issues}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                _, status = fut.result()
                done[status] += 1
            except Exception as e:
                errors.append((futs[fut], repr(e)))
            if i % 50 == 0 or i == len(futs):
                print(f"  {i}/{len(futs)}  fetched={done['fetched']} cached={done['cached']} "
                      f"errors={len(errors)} calls={_stats['calls']} retries={_stats['retries']} "
                      f"({time.time()-t0:.0f}s)", flush=True)

    if errors:
        print(f"\n{len(errors)} failures:", flush=True)
        for n, e in errors[:20]:
            print(f"  #{n}: {e}", flush=True)
    print(f"\ndone in {time.time()-t0:.0f}s :: {done['fetched']} fetched, "
          f"{done['cached']} cached, {_stats['calls']} api calls", flush=True)

    rl, _ = api("/rate_limit")
    core = rl["resources"]["core"]
    print(f"rate limit: {core['remaining']}/{core['limit']} remaining", flush=True)


if __name__ == "__main__":
    main()
