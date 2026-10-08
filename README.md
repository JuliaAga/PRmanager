# Local PR manager

Scan selected GitHub repositories, review open PRs with your local Codex CLI, post concrete findings, and save a short Markdown report. Runs sequentially, on demand. No server or scheduler.

## Setup

Requires Python 3.11+, GitHub CLI (`gh`), and a current Codex CLI (`codex`) supporting `exec --ignore-user-config` and structured output. Supports macOS and Linux.

```sh
brew install gh  # macOS, if needed
gh auth login --hostname github.com  # skip if already logged in
codex login
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/pr-manager init
```

Alternatively, run `python3 -m pr_manager init` and `python3 -m pr_manager scan` directly from this directory; the runtime has no third-party Python dependencies.

`init` verifies your existing GitHub CLI login and prompts only for comma-separated `owner/repository` names. It creates `~/.config/pr-manager/config.toml` with mode `0600` and refuses to overwrite existing settings. Example structure :

```toml
repositories = ["your-org/service", "your-org/web"]
```

`config.example.toml` lists your selected repository: `JuliaAga/pr-test-python`. It contains no real credential.

The GitHub CLI account needs access to the selected repositories, including read access to contents, pull requests, checks and commit statuses, plus permission to create PR conversation comments. A fine-grained token normally uses Contents: read, Pull requests: read/write, Checks: read, and Commit statuses: read; organization approval or SSO may also be required. Codex authentication is separate. Repository content is sent to Codex for analysis under your Codex account.

Keep this file out of source control. Custom config files must also have owner-only permissions (`chmod 600 /path/to/config.toml`).

## Usage

```sh
pr-manager scan --dry-run                # Review and report without posting
pr-manager scan                          # Automatically post qualifying findings
pr-manager scan --repo your-org/service   # Restrict to a configured repository
pr-manager scan --refresh --dry-run       # Bypass the review cache
pr-manager scan --config /path/to/config.toml
```

`--repo` may be repeated. `--config` works before or after the subcommand. A dry run still saves reports and review cache, so the next normal run can publish cached findings. Initial scans analyze every open non-draft PR, including your own.

Reports and state are saved under `~/.local/share/pr-manager/`. Report files and state use owner-only permissions. A process lock prevents overlapping local scans. Exit code `0` means the scan finished; `1` means configuration, authentication, repository, or PR processing failed; `130` means cancelled. A completed review with limitations appears as manual assessment needed even when the scan exits successfully.

## Report format

Each PR shows its link, new/updated/unchanged status, two-sentence description, discussion summary and links, any new agent comments, and a conclusion:

- **Check manually now:** analysis completed and no known significant findings, requests for changes, or failing/pending CI remain. Missing CI is explicitly flagged.
- **Wait for changes:** concrete unresolved findings, requests for changes, or CI problems; pending CI is identified as waiting for completion. Drafts show **Wait — draft**.
- **Manual assessment needed:** unavailable/incomplete context, oversized PR, invalid model output, stale results, or an API/review failure.

Descriptions are local report content only. Draft entries use the PR title plus a sentence explaining that review was skipped. Discussion links are capped at ten to keep the report compact; the PR has the complete conversation.

## Review and posting behavior

The tool fetches paginated PRs, commits, files, comments, reviews, unresolved thread metadata, checks, and statuses. Codex receives diffs, changed-file source at immutable blob IDs, commit messages, and discussion. No checkout, test execution, build, or dependency installation occurs. Dependencies outside changed files are not loaded automatically; missing context must be reported as a limitation.

Codex runs in a temporary directory with read-only sandboxing, inherited user configuration and rules disabled, and shell, plugins, apps, browser, computer-use, and multi-agent features disabled. GitHub CLI uses its saved login or standard `GH_TOKEN`/`GITHUB_TOKEN` environment overrides. Environment credentials are excluded from Codex subprocesses and redacted from model input. An optional legacy `github_token` setting remains supported and takes precedence; new settings contain only repository names. Raw subprocess output is not exposed in error reports.

Only high-confidence correctness/security findings with validated changed-file line references are posted, in one consolidated conversation comment. Reviews with limitations are not posted. Existing discussion is checked semantically by Codex and exact finding markers are checked by the CLI. State records posted finding IDs; deleting state does not remove markers from GitHub. AI semantic matching is best-effort.

The tool never approves, requests changes, merges, edits descriptions, or resolves threads. It refreshes CI/discussion each scan, reuses analysis only when commits and discussion match, and rechecks head/base and PR state before reporting/posting. GitHub provides no atomic conditional comment creation, so a change in the final interval between checking and posting cannot be completely prevented. Comments link to the reviewed commit.

Large or binary content is flagged for manual assessment. Changed-file source context is capped at 60,000 characters per file and 350,000 total; the complete model input is capped at 650,000 serialized characters. Model calls time out after 15 minutes and GitHub calls after 2 minutes. Failed posts are not retried immediately; the next scan checks GitHub discussion before attempting another post.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use fake GitHub/Codex boundaries and never post real comments. For live validation, configure repositories and run `scan --dry-run`. Test real posting only with a settings file listing a dedicated test repository/PR; normal scans cover all open PRs in each selected repository.
