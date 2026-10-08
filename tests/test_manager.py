import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pr_manager.cli import init, load_config, main, new_findings, scan
from pr_manager.core import Failure, GitHub, clean_env, conclusion, finding_id, run
from pr_manager.reviewer import changed_lines, validate

TOKEN = "test-private-token"


def analysis():
    return {"description": ["Adds a feature.", "Updates its callers."], "discussion_summary": "No concerns.",
            "unresolved_significant": False, "findings": [], "limitations": []}


def finding(**kwargs):
    return {"title": "Null input crashes", "body": "A null value is dereferenced; guard it.", "path": "a.py",
            "line": 2, "severity": "high", "confidence": "high", "already_discussed": False} | kwargs


def pr(number=1):
    return {"number": number, "title": "Feature", "body": "Details", "html_url": f"https://github.com/o/r/pull/{number}",
            "state": "open", "draft": False, "head": {"sha": "abc"}, "base": {"sha": "def"},
            "comments": [], "reviews": [], "inline": [], "threads": [], "checks": [], "statuses": [], "changed_files": 1}


class FakeGitHub:
    def __init__(self):
        self.item = pr()
        self.posts = []
        self.fail = False
        self.stale = False
        self.context_calls = 0

    def api(self, endpoint):
        if endpoint == "user":
            return {"login": "tester"}
        result = copy.deepcopy(self.item)
        if self.stale:
            result["head"]["sha"] = "changed"
        return result

    def prs(self, repo):
        if self.fail:
            raise Failure("GitHub unavailable")
        return [copy.deepcopy(self.item)]

    def snapshot(self, repo, number):
        return copy.deepcopy(self.item)

    def context(self, repo, item):
        self.context_calls += 1
        return {"files": [{"filename": "a.py", "patch": "@@ -1 +1,2 @@\n x\n+bad()"}], "body": item["body"]}, []

    def post(self, repo, number, body):
        self.posts.append(body)
        response = {"body": body, "html_url": "https://github.com/o/r/pull/1#issuecomment-1"}
        self.item["comments"].append(response)
        return response


class FakeReviewer:
    def __init__(self):
        self.result = analysis()
        self.calls = 0
        self.contexts = []

    def review(self, context):
        self.calls += 1
        self.contexts.append(context)
        return copy.deepcopy(self.result)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.gh = FakeGitHub()
        self.reviewer = FakeReviewer()

    def scan(self, **kwargs):
        output = io.StringIO()
        with redirect_stdout(output):
            code = scan(TOKEN, ["o/r"], data=self.data, github=self.gh, reviewer=self.reviewer, **kwargs)
        return code, output.getvalue()

    def test_token_free_scan_and_environment_redaction(self):
        self.gh.item["body"] = TOKEN
        self.reviewer.result["findings"] = [finding()]
        with patch.dict(os.environ, {"GH_TOKEN": TOKEN}), redirect_stdout(io.StringIO()) as output:
            code = scan(None, ["o/r"], data=self.data, github=self.gh, reviewer=self.reviewer)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.gh.posts), 1)
        self.assertNotIn(TOKEN, json.dumps(self.reviewer.contexts))
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertNotIn(TOKEN, (self.data / "state.json").read_text())

    def test_cache_refresh_commits_and_discussion(self):
        self.assertIn("— new", self.scan()[1])
        self.assertIn("— unchanged", self.scan()[1])
        self.assertEqual(self.reviewer.calls, 1)
        self.gh.item["checks"] = [{"status": "in_progress", "conclusion": None}]
        self.assertIn("wait for CI completion", self.scan()[1])
        self.assertEqual(self.reviewer.calls, 1)
        self.gh.item["head"]["sha"] = "xyz"
        self.assertIn("— updated", self.scan()[1])
        self.assertEqual(self.reviewer.calls, 2)
        self.gh.item["comments"].append({"body": "question", "html_url": "https://github.com/o/r/pull/1#x"})
        self.scan()
        self.assertEqual(self.reviewer.calls, 3)
        self.scan(refresh=True)
        self.assertEqual(self.reviewer.calls, 4)

    def test_posts_once_and_keeps_links(self):
        self.reviewer.result["findings"] = [finding()]
        code, output = self.scan()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.gh.posts), 1)
        self.assertIn("issuecomment-1", output)
        self.assertIn("Wait for changes", output)
        self.scan()
        self.assertEqual(len(self.gh.posts), 1)
        # A lost local state must not cause a duplicate post.
        (self.data / "state.json").unlink()
        self.scan()
        self.assertEqual(len(self.gh.posts), 1)

    def test_dry_run_then_publish(self):
        self.reviewer.result["findings"] = [finding()]
        self.assertIn("Would post", self.scan(dry_run=True)[1])
        self.assertEqual(self.gh.posts, [])
        self.scan()
        self.assertEqual(len(self.gh.posts), 1)

    def test_stale_results_not_posted_or_cached(self):
        self.reviewer.result["findings"] = [finding()]
        self.gh.stale = True
        code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("discarded", output)
        self.assertEqual(self.gh.posts, [])
        self.assertFalse((self.data / "state.json").exists())

    def test_draft_then_ready(self):
        self.gh.item["draft"] = True
        self.assertIn("Wait — draft", self.scan()[1])
        self.assertEqual(self.reviewer.calls, 0)
        self.gh.item["draft"] = False
        self.assertIn("Check manually now", self.scan()[1])
        self.assertEqual(self.reviewer.calls, 1)

    def test_limitations_prevent_posting(self):
        self.reviewer.result["limitations"] = ["Missing dependency context"]
        self.reviewer.result["findings"] = [finding()]
        self.assertIn("Manual assessment needed", self.scan()[1])
        self.assertEqual(self.gh.posts, [])

    def test_unavailable_repo_report(self):
        self.gh.fail = True
        code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("repository unavailable", output)

    def test_credentials_redacted(self):
        self.gh.item["body"] = TOKEN
        code, output = self.scan()
        self.assertNotIn(TOKEN, output)
        self.assertNotIn(TOKEN, json.dumps(self.reviewer.contexts))
        self.assertNotIn(TOKEN, (self.data / "state.json").read_text())
        self.reviewer.result["description"][0] = TOKEN
        self.assertEqual(self.scan(refresh=True)[0], 1)

    def test_partial_failure_continues(self):
        self.gh.prs = lambda repo: [pr(1), pr(2)]
        def snapshot(repo, number):
            if number == 1:
                raise Failure("Rate limited")
            return pr(2)
        self.gh.snapshot = snapshot
        self.gh.api = lambda endpoint: {"login": "me"} if endpoint == "user" else pr(2)
        code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("Rate limited", output)
        self.assertIn("o/r#2", output)
        self.assertIn("Check manually now", output)

    def test_discussion_race_prevents_post(self):
        self.reviewer.result["findings"] = [finding()]
        calls = 0
        def snapshot(repo, number):
            nonlocal calls
            calls += 1
            result = copy.deepcopy(self.gh.item)
            if calls > 1:
                result["comments"].append({"body": "New discussion", "html_url": "https://github.com/o/r/pull/1#new"})
            return result
        self.gh.snapshot = snapshot
        self.assertEqual(self.scan()[0], 1)
        self.assertEqual(self.gh.posts, [])

    def test_failed_post_is_reported(self):
        self.reviewer.result["findings"] = [finding()]
        def failed_post(*args):
            raise Failure("Posting failed")
        self.gh.post = failed_post
        code, output = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("Posting failed", output)
        self.assertFalse((self.data / "state.json").exists())

    def test_invalid_credentials_stop_before_scanning(self):
        def rejected(endpoint):
            raise Failure("Authentication failed")
        self.gh.api = rejected
        with self.assertRaises(Failure):
            self.scan()
        self.assertEqual(self.reviewer.calls, 0)

    def test_malformed_review_continues(self):
        self.reviewer.review = lambda context: {"bad": True}
        self.assertEqual(self.scan()[0], 1)
        self.assertEqual(self.gh.posts, [])


class DecisionTests(unittest.TestCase):
    def test_ci_states(self):
        p = pr()
        self.assertIn("CI absent", conclusion(p, analysis()))
        for result in ("failure", "cancelled", "timed_out", "action_required", None):
            p["checks"] = [{"status": "completed", "conclusion": result}]
            self.assertIn("CI needs attention", conclusion(p, analysis()))
        p["checks"] = [{"status": "completed", "conclusion": "success"}]
        self.assertIn("Check manually now", conclusion(p, analysis()))
        p["statuses"] = [{"state": "pending"}]
        self.assertIn("wait for CI completion", conclusion(p, analysis()))

    def test_review_latest_decisive_state(self):
        p = pr()
        p["reviews"] = [{"state": "CHANGES_REQUESTED", "user": {"login": "x"}, "submitted_at": "1"},
                        {"state": "COMMENTED", "user": {"login": "x"}, "submitted_at": "2"}]
        self.assertIn("outstanding request", conclusion(p, analysis()))
        p["reviews"].append({"state": "APPROVED", "user": {"login": "x"}, "submitted_at": "3"})
        self.assertIn("Check manually now", conclusion(p, analysis()))

    def test_dedup_and_confidence(self):
        a = analysis()
        a["findings"] = [finding(), finding(), finding(title="Different", confidence="low"), finding(title="Discussed", already_discussed=True)]
        self.assertEqual(len(new_findings(a, pr(), [])), 1)
        self.assertEqual(new_findings(a, pr(), [finding_id(finding())]), [])

    def test_validation(self):
        context = {"files": [{"filename": "a.py", "patch": "@@ -1 +1,2 @@\n x\n+bad()"}]}
        a = analysis()
        a["findings"] = [finding()]
        self.assertEqual(validate(a, context), a)
        a["findings"][0]["line"] = 900
        with self.assertRaises(Failure):
            validate(a, context)
        with self.assertRaises(Failure):
            validate({}, context)
        self.assertEqual(changed_lines("@@ -1,2 +1,2 @@\n-old\n+new\n x"), {1})


class IntegrationBoundaryTests(unittest.TestCase):
    def test_rest_pagination(self):
        gh = GitHub(TOKEN)
        calls = []
        def api(endpoint):
            calls.append(endpoint)
            return [1] * 100 if endpoint.endswith("&page=1") else [2]
        gh.api = api
        self.assertEqual(len(gh.pages("repos/o/r/pulls?state=open")), 101)
        self.assertTrue(calls[-1].endswith("page=2"))

    def test_graphql_pagination(self):
        gh = GitHub(TOKEN)
        def response(more):
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": [{"id": str(more)}], "pageInfo": {"hasNextPage": more, "endCursor": "next"}}}}}})
        with patch("pr_manager.core.run", side_effect=[response(True), response(False)]) as runner:
            self.assertEqual(len(gh.threads("o/r", 1)), 2)
            self.assertIn("cursor=next", runner.call_args_list[1].args[0])

    def test_settings_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            with patch.object(GitHub, "check_auth"), patch("builtins.input", return_value="o/r"), redirect_stdout(io.StringIO()):
                init(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_config(path), (None, ["o/r"]))
            self.assertNotIn("github_token", path.read_text())
            with self.assertRaises(Failure):
                init(path)
            path.chmod(0o644)
            with self.assertRaises(Failure):
                load_config(path)

    def test_saved_gh_login_and_environment_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            gh = GitHub()
            self.assertNotIn("GH_TOKEN", gh.env)
            with patch("pr_manager.core.run", return_value='{"login": "tester"}') as runner:
                gh.check_auth()
                self.assertEqual(runner.call_args.args[0], ["gh", "api", "user"])
        with patch.dict(os.environ, {"GH_TOKEN": TOKEN}):
            self.assertEqual(GitHub().env["GH_TOKEN"], TOKEN)
            self.assertEqual(GitHub("legacy-token").env["GH_TOKEN"], "legacy-token")

    def test_init_auth_failure_does_not_create_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            with patch("pr_manager.core.run", side_effect=Failure("gh failed")):
                with self.assertRaisesRegex(Failure, "gh auth login"):
                    init(path)
            self.assertFalse(path.exists())

    def test_legacy_token_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('github_token = "legacy-token"\nrepositories = ["o/r"]\n')
            path.chmod(0o600)
            self.assertEqual(load_config(path), ("legacy-token", ["o/r"]))

    def test_cli_selection(self):
        with patch("pr_manager.cli.load_config", return_value=(TOKEN, ["o/r", "o/s"])), patch("shutil.which", return_value="/bin/x"), patch("pr_manager.core.run"), patch("pr_manager.cli.scan", return_value=0) as scanner:
            self.assertEqual(main(["scan", "--repo", "o/s", "--dry-run"]), 0)
            self.assertEqual(scanner.call_args.args[1], ["o/s"])

    def test_subprocess_error_does_not_leak(self):
        result = type("Result", (), {"returncode": 1, "stdout": TOKEN, "stderr": TOKEN})()
        with patch("subprocess.run", return_value=result):
            with self.assertRaises(Failure) as raised:
                run(["gh", "api", "user"])
            self.assertNotIn(TOKEN, str(raised.exception))

    def test_incomplete_diff_is_flagged(self):
        gh = GitHub(TOKEN)
        gh.pages = lambda endpoint: ([{"filename": "a.py", "status": "removed", "patch": "@@ -1 +0,0 @@\n-old", "additions": 0, "deletions": 2}]
                                     if endpoint.endswith("/files") else [])
        context, limitations = gh.context("o/r", pr())
        self.assertIn("Incomplete diff: a.py", limitations)

    def test_environment_separation(self):
        with patch.dict(os.environ, {"GH_TOKEN": TOKEN, "GITHUB_TOKEN": TOKEN, "OPENAI_API_KEY": TOKEN}):
            self.assertNotIn(TOKEN, clean_env().values())
            self.assertEqual(GitHub(TOKEN).env["GH_TOKEN"], TOKEN)


if __name__ == "__main__":
    unittest.main()
