"""tools/pr_conflict_label.py labels conflicting pull requests `needs rebase` and unlabels them once they merge.

    python -I tests/test_pr_conflict_label.py

A fake GitHub API stands in for the network, so this checks the calls the tool makes: which pull requests get the
label, that one whose merge GitHub has not computed yet is asked again and otherwise left alone, that the label is
created only when it is first needed, that `--dry-run` writes nothing, and that the workflow runs the tool without
checking out pull request code. Standard library only; no network, no game files.
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import pr_conflict_label as pcl  # noqa: E402

REPO = "owner/name"


class FakeApi:
    """Pull requests as {number: [mergeable answers in order, the last one repeating]} plus their labels."""

    def __init__(self, prs, labeled=(), label_exists=False, closed=()):
        self.answers = {n: list(a) for n, a in prs.items()}
        self.labels = {n: ({pcl.LABEL} if n in labeled else set()) for n in prs}
        self.closed = set(closed)
        self.label_exists = label_exists
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        m = re.fullmatch(rf"/repos/{REPO}/pulls/(\d+)", path)
        if method == "GET" and m:
            n = int(m.group(1))
            answers = self.answers[n]
            mergeable = answers.pop(0) if len(answers) > 1 else answers[0]
            return {"number": n, "state": "closed" if n in self.closed else "open", "mergeable": mergeable,
                    "labels": [{"name": name} for name in sorted(self.labels[n])]}
        if method == "GET" and path.startswith(f"/repos/{REPO}/pulls?"):
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            numbers = sorted(self.answers)
            return [{"number": n} for n in numbers[(page - 1) * 100:page * 100]]
        if method == "POST" and path == f"/repos/{REPO}/labels":
            if self.label_exists:
                raise pcl.ApiError(422, "already_exists")
            self.label_exists = True
            return {}
        m = re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/labels", path)
        if method == "POST" and m:
            self.labels[int(m.group(1))] |= set(body["labels"])
            return []
        m = re.fullmatch(rf"/repos/{REPO}/issues/(\d+)/labels/needs%20rebase", path)
        if method == "DELETE" and m:
            n = int(m.group(1))
            if pcl.LABEL not in self.labels[n]:
                raise pcl.ApiError(404, "Label does not exist")
            self.labels[n].discard(pcl.LABEL)
            return None
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def run(api, numbers, **kw):
    return pcl.run(api, REPO, numbers, sleep=lambda s: None, out=lambda s: None, **kw)


class DecideTest(unittest.TestCase):
    def test_every_case(self):
        self.assertEqual(pcl.decide(False, False), "add")
        self.assertEqual(pcl.decide(True, True), "remove")
        self.assertIsNone(pcl.decide(False, True))     # already labeled
        self.assertIsNone(pcl.decide(True, False))     # clean and unlabeled
        self.assertIsNone(pcl.decide(None, False))     # not computed: never guess
        self.assertIsNone(pcl.decide(None, True))


class RunTest(unittest.TestCase):
    def test_labels_conflicts_and_unlabels_rebased_ones(self):
        api = FakeApi({1: [False], 2: [True], 3: [True], 4: [False]}, labeled={3, 4})
        done = run(api, [1, 2, 3, 4])
        self.assertEqual(done, {"add": [1], "remove": [3], "unknown": []})
        self.assertEqual(api.labels, {1: {pcl.LABEL}, 2: set(), 3: set(), 4: {pcl.LABEL}})

    def test_an_uncomputed_merge_is_asked_again_then_left_alone(self):
        api = FakeApi({1: [None, None, False], 2: [None]}, labeled={2})
        done = run(api, [1, 2], attempts=4)
        self.assertEqual(done, {"add": [1], "remove": [], "unknown": [2]})
        self.assertEqual(api.labels[2], {pcl.LABEL})    # unknown keeps the label it had
        self.assertEqual(sum(1 for c in api.calls if c[1] == f"/repos/{REPO}/pulls/2"), 4)

    def test_the_label_is_created_once_and_only_when_needed(self):
        api = FakeApi({1: [True], 2: [True]})
        run(api, [1, 2])
        self.assertEqual(api.writes(), [])
        api = FakeApi({1: [False], 2: [False]})
        run(api, [1, 2])
        self.assertEqual(sum(1 for c in api.writes() if c[1] == f"/repos/{REPO}/labels"), 1)

    def test_an_existing_label_is_not_an_error(self):
        api = FakeApi({1: [False]}, label_exists=True)
        self.assertEqual(run(api, [1])["add"], [1])

    def test_a_label_removed_in_between_is_not_an_error(self):
        api = FakeApi({1: [True]}, labeled={1})
        api.labels[1].discard(pcl.LABEL)
        orig = api.__call__

        def stale(method, path, body=None):     # the GET still reports the label someone just removed
            result = orig(method, path, body)
            if method == "GET" and path.endswith("/pulls/1"):
                result["labels"] = [{"name": pcl.LABEL}]
            return result
        self.assertEqual(pcl.run(stale, REPO, [1], sleep=lambda s: None, out=lambda s: None)["remove"], [1])

    def test_closed_pull_requests_are_skipped(self):
        api = FakeApi({1: [False]}, closed={1})
        self.assertEqual(run(api, [1]), {"add": [], "remove": [], "unknown": []})
        self.assertEqual(api.writes(), [])

    def test_dry_run_writes_nothing(self):
        api = FakeApi({1: [False], 2: [True]}, labeled={2})
        done = run(api, [1, 2], dry_run=True)
        self.assertEqual((done["add"], done["remove"]), ([1], [2]))
        self.assertEqual(api.writes(), [])

    def test_open_pull_numbers_follows_pages(self):
        api = FakeApi({n: [True] for n in range(1, 251)})
        self.assertEqual(pcl.open_pull_numbers(api, REPO), list(range(1, 251)))


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, ".github", "workflows", "conflict-label.yml"), encoding="utf-8") as f:
            self.text = f.read()

    def test_runs_this_tool(self):
        self.assertIn("python3 -I tools/pr_conflict_label.py", self.text)

    def test_no_run_per_pull_request(self):
        # A run per push to every pull request queued hundreds of runs ahead of the repository's builds and tests.
        # Every run checks all pull requests, so one concurrency group keeps at most one waiting.
        self.assertNotRegex(self.text, r"(?m)^\s+pull_request(_target)?\s*:")
        self.assertRegex(self.text, r"(?m)^\s+group:\s*conflict-label\s*$")
        self.assertNotIn("--pr", self.text)

    def test_never_checks_out_pull_request_code(self):
        self.assertNotRegex(self.text, r"github\.event\.pull_request\.head|github\.head_ref|refs/pull/")
        self.assertRegex(self.text, r"ref:\s*\$\{\{\s*github\.event\.repository\.default_branch\s*\}\}")
        self.assertIn("persist-credentials: false", self.text)

    def test_token_is_limited_to_pull_requests(self):
        self.assertRegex(self.text, r"(?m)^permissions:\s*\{\}\s*$")
        job_perms = re.findall(r"(?m)^\s{6}(\w[\w-]*):\s*(read|write)\s*$", self.text)
        self.assertEqual(dict(job_perms), {"contents": "read", "pull-requests": "write"})


if __name__ == "__main__":
    unittest.main()
