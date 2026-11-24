"""Label the open pull requests that conflict with their base branch `needs rebase`, and unlabel them once they merge.

    python -I tools/pr_conflict_label.py --repo OWNER/NAME [--pr N] [--dry-run]

`.github/workflows/conflict-label.yml` runs this on every push to `main` (a merge can make other pull requests
conflict) and hourly (a rebase can clear a conflict, and GitHub may not have finished computing a merge). It has no
per-pull-request trigger: one run per push to every pull request flooded the repository's runners. Every pull request gets reviewed by hand; the label lets a reviewer skip
the ones that cannot be merged as they stand and tells their authors to rebase.

GitHub computes `mergeable` in the background and answers `null` until it has, so a pull request whose answer is
still unknown is asked again a few times and otherwise left as it is for the next run. Only the GitHub REST API is
read and written: no pull request code is checked out or run. The token comes from `GITHUB_TOKEN` (or `GH_TOKEN`); `--dry-run` prints the changes and writes nothing.
Standard library only; no game files.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LABEL = "needs rebase"
LABEL_COLOR = "d93f0b"
LABEL_DESCRIPTION = "Conflicts with the base branch: rebase it on main"
API = "https://api.github.com"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def github_api(token):
    """A function (method, path, body=None) -> parsed JSON (or None) that calls the GitHub REST API with `token`."""
    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "pr-conflict-label",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise ApiError(e.code, e.read().decode("utf-8", "replace")[:300]) from None
        return json.loads(raw) if raw else None
    return call


def decide(mergeable, labeled):
    """'add', 'remove' or None for a pull request whose `mergeable` is True, False or None (not computed yet)."""
    if mergeable is False and not labeled:
        return "add"
    if mergeable is True and labeled:
        return "remove"
    return None


def open_pull_numbers(api, repo):
    numbers, page = [], 1
    while True:
        batch = api("GET", f"/repos/{repo}/pulls?state=open&per_page=100&page={page}")
        numbers += [p["number"] for p in batch]
        if len(batch) < 100:
            return numbers
        page += 1


def ensure_label(api, repo):
    try:
        api("POST", f"/repos/{repo}/labels",
            {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION})
    except ApiError as e:
        if e.status != 422:         # 422: the label exists already
            raise


def apply(api, repo, number, action):
    if action == "add":
        api("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": [LABEL]})
    else:
        try:
            api("DELETE", f"/repos/{repo}/issues/{number}/labels/{urllib.parse.quote(LABEL, safe='')}")
        except ApiError as e:
            if e.status != 404:     # 404: someone removed it in between
                raise


def run(api, repo, numbers, dry_run=False, attempts=4, wait=10.0, sleep=time.sleep, out=print):
    """Label or unlabel each pull request of `numbers`. Returns {'add': [...], 'remove': [...], 'unknown': [...]}."""
    done = {"add": [], "remove": [], "unknown": []}
    pending = list(numbers)
    label_ready = dry_run
    for attempt in range(attempts):
        if attempt:
            sleep(wait)     # asking for a pull request starts GitHub's merge computation; give it time
        unknown = []
        for number in pending:
            pr = api("GET", f"/repos/{repo}/pulls/{number}")
            if pr.get("state") != "open":
                continue
            labeled = any(label.get("name") == LABEL for label in pr.get("labels", []))
            if pr.get("mergeable") is None:
                unknown.append(number)
                continue
            action = decide(pr["mergeable"], labeled)
            if action is None:
                continue
            out(f"#{number}: {action} '{LABEL}'" + (" (dry run)" if dry_run else ""))
            if not dry_run:
                if action == "add" and not label_ready:
                    ensure_label(api, repo)
                    label_ready = True
                apply(api, repo, number, action)
            done[action].append(number)
        pending = unknown
        if not pending:
            break
    done["unknown"] = pending
    if pending:
        out("mergeable not computed yet, left for the next run: " + " ".join(f"#{n}" for n in pending))
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="OWNER/NAME")
    ap.add_argument("--pr", type=int, action="append", help="only this pull request (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    args = ap.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not args.repo or not token:
        ap.error("needs --repo (or GITHUB_REPOSITORY) and GITHUB_TOKEN (or GH_TOKEN)")
    api = github_api(token)
    numbers = args.pr or open_pull_numbers(api, args.repo)
    done = run(api, args.repo, numbers, dry_run=args.dry_run)
    print(f"{len(numbers)} checked: {len(done['add'])} labeled, {len(done['remove'])} unlabeled, "
          f"{len(done['unknown'])} not computed yet")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
