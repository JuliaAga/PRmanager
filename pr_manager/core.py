from __future__ import annotations

import hashlib
import json
import os
import subprocess


class Failure(Exception):
    """A safe error suitable for display (never raw subprocess output)."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def clean_env():
    return {k: v for k, v in os.environ.items()
            if not any(word in k.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))}


def run(args, *, env=None, stdin=None, timeout=120):
    try:
        result = subprocess.run(args, input=stdin, text=True, capture_output=True,
                                env=env or clean_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Failure(f"{args[0]} timed out") from None
    except OSError:
        raise Failure(f"Could not start {args[0]}") from None
    if result.returncode:
        raise Failure(f"{args[0]} failed (exit {result.returncode}); check access, authentication, or rate limits")
    return result.stdout


def parse_json(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        raise Failure("Invalid JSON response") from None


class GitHub:
    def __init__(self, token=None):
        self.env = clean_env() | {"GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1"}
        # Let gh use its stored login, or its standard environment overrides.
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            if os.environ.get(key):
                self.env[key] = os.environ[key]
        if token:
            self.env["GH_TOKEN"] = token

    def check_auth(self):
        try:
            self.api("user")
        except Failure:
            raise Failure("GitHub authentication failed. Run gh auth login --hostname github.com and retry") from None

    def api(self, endpoint, fields=None):
        args = ["gh", "api", endpoint]
        for key, value in (fields or {}).items():
            args += ["-f", f"{key}={value}"]
        return parse_json(run(args, env=self.env))

    def pages(self, endpoint):
        result = []
        for page in range(1, 10001):
            sep = "&" if "?" in endpoint else "?"
            rows = self.api(f"{endpoint}{sep}per_page=100&page={page}")
            if not isinstance(rows, list):
                raise Failure("Unexpected GitHub pagination response")
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise Failure("GitHub pagination limit exceeded")

    def prs(self, repo):
        return self.pages(f"repos/{repo}/pulls?state=open")

    def threads(self, repo, number):
        owner, name = repo.split("/")
        query = '''query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) {
              pageInfo { hasNextPage endCursor }
              nodes { id isResolved isOutdated path line comments(first:1) { nodes { databaseId } } }
            }
          } }
        }'''
        cursor = None
        rows = []
        while True:
            args = ["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                    "-f", f"name={name}", "-F", f"number={number}"]
            if cursor:
                args += ["-f", f"cursor={cursor}"]
            data = parse_json(run(args, env=self.env))
            if data.get("errors"):
                raise Failure("Could not read review threads")
            connection = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            rows.extend(connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"]:
                return rows
            cursor = connection["pageInfo"]["endCursor"]

    def snapshot(self, repo, number):
        prefix = f"repos/{repo}"
        pr = self.api(f"{prefix}/pulls/{number}")
        comments = self.pages(f"{prefix}/issues/{number}/comments")
        reviews = self.pages(f"{prefix}/pulls/{number}/reviews")
        inline = self.pages(f"{prefix}/pulls/{number}/comments")
        threads = self.threads(repo, number)
        sha = pr["head"]["sha"]
        checks = []
        for page in range(1, 10001):
            batch = self.api(f"{prefix}/commits/{sha}/check-runs?filter=latest&per_page=100&page={page}")["check_runs"]
            checks.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise Failure("Check pagination limit exceeded")
        statuses = self.pages(f"{prefix}/commits/{sha}/statuses")
        latest = {}
        for status in statuses:
            latest.setdefault(status["context"], status)
        pr.update(comments=comments, reviews=reviews, inline=inline, threads=threads,
                  checks=checks, statuses=list(latest.values()))
        return pr

    def context(self, repo, pr):
        number = pr["number"]
        files = self.pages(f"repos/{repo}/pulls/{number}/files")
        commits = self.pages(f"repos/{repo}/pulls/{number}/commits")
        limitations = []
        if len(files) != pr["changed_files"]:
            limitations.append("GitHub returned an incomplete file list")
        context_files = []
        budget = 350000
        for item in files:
            entry = {k: item.get(k) for k in ("filename", "previous_filename", "status", "patch", "additions", "deletions")}
            if not item.get("patch") and (item["additions"] or item["deletions"]):
                limitations.append(f"Missing diff: {item['filename']}")
            # Immutable blob context avoids checking out or executing repository code.
            if item["status"] != "removed" and item.get("sha"):
                blob = self.api(f"repos/{repo}/git/blobs/{item['sha']}")
                import base64
                try:
                    raw = base64.b64decode(blob.get("content", ""), validate=False)
                    source = raw.decode("utf-8")
                    if len(source) <= 60000 and len(source) <= budget:
                        entry["source"] = source
                        budget -= len(source)
                    else:
                        limitations.append(f"File context too large: {item['filename']}")
                except (ValueError, UnicodeDecodeError):
                    limitations.append(f"Non-text context: {item['filename']}")
            patch = item.get("patch", "")
            if patch:
                added = sum(line.startswith("+") for line in patch.splitlines())
                removed = sum(line.startswith("-") for line in patch.splitlines())
                if added != item["additions"] or removed != item["deletions"]:
                    limitations.append(f"Incomplete diff: {item['filename']}")
            context_files.append(entry)
        context = {"title": pr["title"], "body": pr.get("body"), "files": context_files,
                   "commits": [{"sha": c["sha"], "message": c["commit"]["message"]} for c in commits],
                   "comments": pr["comments"], "reviews": pr["reviews"], "inline": pr["inline"],
                   "threads": pr["threads"]}
        if len(json.dumps(context)) > 650000:
            raise Failure("PR context exceeds safe review size; review manually")
        return context, limitations

    def post(self, repo, number, body):
        return self.api(f"repos/{repo}/issues/{number}/comments", {"body": body})


def finding_id(finding):
    return digest({k: finding[k] for k in ("path", "title", "body")})[:20]


def conclusion(pr, analysis):
    if pr["draft"]:
        return "Wait — draft"
    if analysis.get("limitations"):
        return "Manual assessment needed — " + "; ".join(analysis["limitations"])
    blockers = []
    checks, statuses = pr["checks"], pr["statuses"]
    if any(c["status"] != "completed" for c in checks) or any(s["state"] == "pending" for s in statuses):
        blockers.append("wait for CI completion")
    if any(c["status"] == "completed" and c["conclusion"] not in ("success", "neutral", "skipped") for c in checks) or any(s["state"] in ("failure", "error") for s in statuses):
        blockers.append("CI needs attention")
    latest = {}
    for review in sorted(pr["reviews"], key=lambda r: r.get("submitted_at") or ""):
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(review.get("user") or {}).get("login", "unknown")] = review["state"]
    if "CHANGES_REQUESTED" in latest.values():
        blockers.append("outstanding request for changes")
    if any(f["severity"] in ("high", "medium") for f in analysis["findings"]) or analysis["unresolved_significant"]:
        blockers.append("significant unresolved findings need author changes")
    result = "Wait for changes — " + "; ".join(blockers) if blockers else "Check manually now — no known blockers"
    if not checks and not statuses:
        result += "; CI absent"
    return result
