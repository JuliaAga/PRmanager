from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .core import Failure, GitHub, conclusion, digest, finding_id
from .reviewer import Reviewer, validate

CONFIG = Path.home() / ".config/pr-manager/config.toml"
DATA = Path.home() / ".local/share/pr-manager"
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def atomic_write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(contents)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(path):
    if not path.exists():
        raise Failure(f"Settings not found: {path}. Run pr-manager init first")
    if path.stat().st_mode & 0o077:
        raise Failure(f"Settings must be owner-only. Run chmod 600 '{path}'")
    try:
        config = tomllib.loads(path.read_text())
    except (ValueError, OSError):
        raise Failure("Could not parse settings TOML") from None
    token = config.get("github_token")
    repos = config.get("repositories")
    if token is not None and (not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token)):
        raise Failure("Optional github_token must be nonempty and contain no whitespace")
    if not isinstance(repos, list) or not repos or not all(isinstance(r, str) and REPO.fullmatch(r) for r in repos):
        raise Failure("Settings require repositories = [\"owner/repository\", ...]")
    return token, list(dict.fromkeys(repos))


def init(path):
    if path.exists():
        raise Failure("Settings already exist; edit them locally instead of overwriting")
    GitHub().check_auth()
    repos = [r.strip() for r in input("Repositories (owner/repo, comma separated): ").split(",")]
    if not repos or not all(REPO.fullmatch(r) for r in repos):
        raise Failure("Provide valid owner/repository names")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(f"repositories = {json.dumps(repos)}\n")
    print(f"Created {path}")


@contextmanager
def scan_lock(data):
    import fcntl
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data / "scan.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure("Another scan is already running") from None
        yield


def discussion_hash(pr):
    return digest({k: pr[k] for k in ("title", "body", "comments", "reviews", "inline", "threads")})


def new_findings(analysis, pr, posted):
    existing = "\n".join(c.get("body") or "" for key in ("comments", "reviews", "inline") for c in pr[key])
    seen = set(posted)
    selected = []
    for f in analysis["findings"]:
        identity = finding_id(f)
        if f["confidence"] != "high" or f["already_discussed"] or identity in seen or f"pr-manager:{identity}" in existing:
            continue
        seen.add(identity)
        selected.append(f)
    return selected


def comment_body(repo, pr, findings):
    parts = ["### PR manager — initial review", "Static review; tests were not executed."]
    for f in findings:
        link = f"https://github.com/{repo}/blob/{pr['head']['sha']}/{quote(f['path'], safe='/')}#L{f['line']}"
        parts.append(f"**{f['severity'].upper()}: {f['title']}** ([{f['path']}:{f['line']}]({link}))\n\n{f['body']}\n\n<!-- pr-manager:{finding_id(f)} -->")
    return "\n\n".join(parts)


def safe_text(text):
    # Keep model/repository text inside the local report as plain Markdown prose.
    return re.sub(r"\s+", " ", str(text)).replace("<", "&lt;").replace(">", "&gt;")


def report_entry(repo, pr, change, analysis, result, posted_url=None, pending=None):
    lines = [f"### [{repo}#{pr['number']}]({pr['html_url']}) — {change}",
             " ".join(safe_text(s) for s in analysis["description"]),
             "**Comments:** " + safe_text(analysis["discussion_summary"] or "No review discussion.")]
    links = list(dict.fromkeys(c.get("html_url") for key in ("comments", "reviews", "inline") for c in pr.get(key, []) if c.get("html_url")))
    if posted_url:
        links.append(posted_url)
    if links:
        lines.append("Discussion: " + ", ".join(f"[comment {i}]({url})" for i, url in enumerate(links[:10], 1)))
        if len(links) > 10:
            lines.append(f"{len(links) - 10} additional comments available on the PR.")
    if analysis["findings"]:
        details = []
        for f in analysis["findings"][:5]:
            source = f"https://github.com/{repo}/blob/{pr['head']['sha']}/{quote(f['path'], safe='/')}#L{f['line']}"
            details.append(f"{safe_text(f['severity'])}: {safe_text(f['title'])} ([source]({source}))")
        lines.append("**Findings:** " + "; ".join(details))
    if pending:
        lines.append("**Would post:** " + "; ".join(safe_text(f["title"]) for f in pending))
    elif posted_url:
        lines.append("**Agent comments:** " + "; ".join(safe_text(f["title"]) for f in analysis["findings"] if f["confidence"] == "high" and not f["already_discussed"]))
    lines.append("**Conclusion:** " + safe_text(result))
    return "\n\n".join(lines)


def scan(token, repos, *, data=DATA, dry_run=False, refresh=False, github=None, reviewer=None):
    gh = github or GitHub(token)
    secrets = {value for value in (token, os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN")) if value}

    def redact(text):
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    reviewer = reviewer or Reviewer()
    gh.api("user")
    with scan_lock(data):
        state_path = data / "state.json"
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
        except (ValueError, OSError):
            raise Failure("Cannot read state; preserve it and repair it before scanning") from None
        if not isinstance(state, dict):
            raise Failure("Invalid review state")
        # Namespace state by repository; settings/token changes never enter it.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        entries = [f"# PR report — {stamp}", "Dry run: no comments posted." if dry_run else "Static review: no PR code was executed."]
        errors = 0
        for repo in repos:
            try:
                prs = gh.prs(repo)
            except (Failure, KeyError, TypeError, ValueError):
                entries.append(f"### {repo}\n\nManual assessment needed — repository unavailable, authentication/rate limit error, or invalid response.")
                errors += 1
                continue
            if not prs:
                entries.append(f"### {repo}\n\nNo open PRs.")
            for listed in prs:
                number = listed["number"]
                key = f"{repo}#{number}"
                try:
                    pr = gh.snapshot(repo, number)
                    if pr["state"] != "open":
                        continue
                    old = state.get(key, {})
                    revision = [pr["head"]["sha"], pr["base"]["sha"]]
                    discussion = discussion_hash(pr)
                    activity = digest({k: pr[k] for k in ("checks", "statuses", "draft")})
                    change = "new" if not old else "updated" if old.get("revision") != revision or old.get("discussion") != discussion or old.get("activity") != activity else "unchanged"
                    if pr["draft"]:
                        analysis = {"description": [pr["title"].rstrip(".") + ".", "This PR is a draft and has not been reviewed."],
                                    "discussion_summary": "Draft; see linked discussion.", "findings": [],
                                    "unresolved_significant": False, "limitations": []}
                    elif not refresh and old.get("revision") == revision and old.get("discussion") == discussion and old.get("analysis") and not old.get("draft"):
                        analysis = old["analysis"]
                    else:
                        context, limitations = gh.context(repo, pr)
                        # Never pass configured credentials to the model, even if echoed in repository data.
                        context = json.loads(redact(json.dumps(context)))
                        analysis = validate(reviewer.review(context), context)
                        analysis["limitations"].extend(limitations)
                    if any(secret in json.dumps(analysis) for secret in secrets):
                        raise Failure("Review output contained a credential; output discarded")
                    candidates = new_findings(analysis, pr, old.get("posted", [])) if not pr["draft"] and not analysis["limitations"] else []
                    posted = list(old.get("posted", []))
                    url = None
                    # Check every reviewed result, including dry runs and cached reviews.
                    fresh = gh.api(f"repos/{repo}/pulls/{number}")
                    if fresh["head"]["sha"] != revision[0] or fresh["base"]["sha"] != revision[1] or fresh["state"] != "open" or fresh["draft"] != pr["draft"]:
                        raise Failure("PR changed during scan; result discarded, rerun scan")
                    if candidates and not dry_run:
                        # Re-fetch discussion immediately before posting to catch concurrent/previous posts.
                        current = gh.snapshot(repo, number)
                        if current["head"]["sha"] != revision[0] or current["base"]["sha"] != revision[1] or current["state"] != "open" or current["draft"]:
                            raise Failure("PR changed before posting; rerun scan")
                        if discussion_hash(current) != discussion:
                            raise Failure("PR discussion changed during review; rerun scan")
                        body = comment_body(repo, pr, candidates)
                        if any(secret in body for secret in secrets):
                            raise Failure("Credential detected in comment; posting cancelled")
                        response = gh.post(repo, number, body)
                        url = response["html_url"]
                        posted.extend(finding_id(f) for f in candidates)
                    state[key] = {"revision": revision, "discussion": discussion, "analysis": analysis,
                                  "posted": posted, "draft": pr["draft"], "activity": activity}
                    # Persist after each PR, including immediately after a successful post.
                    atomic_write(state_path, json.dumps(state, indent=2))
                    entries.append(report_entry(repo, pr, change, analysis, conclusion(pr, analysis), url,
                                                candidates if dry_run else None))
                except (Failure, KeyError, TypeError, ValueError, OSError) as exc:
                    errors += 1
                    reason = str(exc) if isinstance(exc, Failure) else "Incomplete or invalid PR data; review manually"
                    entries.append(f"### [{key}](https://github.com/{repo}/pull/{number})\n\n**Conclusion:** Manual assessment needed — {safe_text(reason)}")
        report = redact("\n\n".join(entries)) + "\n"
        path = data / "reports" / f"{stamp}.md"
        atomic_write(path, report)
        print(report)
        print(f"Report saved: {path}")
        return 1 if errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review open GitHub PRs locally with Codex")
    parser.add_argument("--config", type=Path, default=CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "scan"):
        sub = commands.add_parser(name)
        sub.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if name == "scan":
            sub.add_argument("--repo", action="append", help="Select a configured owner/repository (repeatable)")
            sub.add_argument("--dry-run", action="store_true")
            sub.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            init(args.config.expanduser())
            return 0
        token, repos = load_config(args.config.expanduser())
        if args.repo:
            if any(r not in repos for r in args.repo):
                raise Failure("--repo must select a repository listed in settings")
            repos = list(dict.fromkeys(args.repo))
        missing = [name for name in ("gh", "codex") if not shutil.which(name)]
        if missing:
            raise Failure("Missing prerequisites: " + ", ".join(missing))
        from .core import run
        run(["codex", "login", "status"])
        return scan(token, repos, dry_run=args.dry_run, refresh=args.refresh)
    except (Failure, OSError) as exc:
        print(f"Error: {exc if isinstance(exc, Failure) else 'Local file operation failed'}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled", file=sys.stderr)
        return 130
