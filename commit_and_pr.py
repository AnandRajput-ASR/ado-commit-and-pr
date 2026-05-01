"""
ADO Commit & PR
---------------
Stage is already done by you. This script:
  1. Reads the current git branch to infer the target branch and work item ID.
  2. Prompts for a short description to build the commit message.
  3. Commits staged changes.
  4. Pushes the branch to origin.
  5. Creates a Pull Request in Azure DevOps targeting the right branch,
     with the work item linked.

Branch naming convention understood:
  feature/<release|develop>/<work-item-id>   e.g. feature/19.0.0/61527
  bug/<release|develop>/<work-item-id>        e.g. bug/20.0.0/61527

  Middle segment → target branch:
    "develop"  → origin/develop
    anything else (e.g. "19.0.0") → origin/release/<segment>
"""

import os
import re
import subprocess
import sys
import argparse
import json
import webbrowser
from pathlib import Path
from datetime import datetime, timezone

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()

ADO_API_VERSION = "7.1"
COMMIT_TYPES = ("feat", "fix", "refactor", "docs", "style", "test", "chore")
COMMIT_SCOPES = ("Requestor", "Supplier")

BRANCH_PATTERN = re.compile(
    r"^(?P<type>feature|bug|bugfix|hotfix)/(?P<target>[^/]+)/(?P<work_item_id>\d+)$",
    re.IGNORECASE,
)

ADO_REMOTE_PATTERNS = [
    re.compile(
        r"^https://(?:[^@/]+@)?dev\.azure\.com/(?P<org>[^/]+)/(?P<project>[^/]+)/_git/(?P<repo>[^/]+?)(?:\.git)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^https://(?P<org>[^./]+)\.visualstudio\.com/(?P<project>[^/]+)/_git/(?P<repo>[^/]+?)(?:\.git)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^git@ssh\.dev\.azure\.com:v3/(?P<org>[^/]+)/(?P<project>[^/]+)/(?P<repo>[^/]+)$",
        re.IGNORECASE,
    ),
]


def load_pat() -> tuple[str, str, str, str, str]:
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        console.print("[bold red]ERROR:[/] .env file not found in this folder.")
        sys.exit(1)
    load_dotenv(env_path)

    pat = os.getenv("AZURE_DEVOPS_PAT", "").strip()
    org = os.getenv("AZURE_DEVOPS_ORG", "").strip()
    project = os.getenv("AZURE_DEVOPS_PROJECT", "").strip()
    repo = os.getenv("AZURE_DEVOPS_REPO", "").strip()
    work_item_project = os.getenv("AZURE_DEVOPS_WORKITEM_PROJECT", "").strip() or project

    missing = [k for k, v in [
        ("AZURE_DEVOPS_PAT", pat),
    ] if not v]
    if missing:
        console.print(f"[bold red]ERROR:[/] Missing in .env: {', '.join(missing)}")
        sys.exit(1)

    return pat, org, project, repo, work_item_project


def parse_ado_remote(origin_url: str) -> tuple[str, str, str] | None:
    clean = origin_url.strip()
    for pattern in ADO_REMOTE_PATTERNS:
        match = pattern.match(clean)
        if match:
            d = match.groupdict()
            return d["org"], d["project"], d["repo"]
    return None


def get_origin_ado_context() -> tuple[str, str, str] | None:
    try:
        origin = git("remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        return None
    return parse_ado_remote(origin)


def get_work_item_details(
    org: str,
    project: str,
    work_item_id: str,
    auth: HTTPBasicAuth,
) -> dict | None:
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/wit/workitems/{work_item_id}"
        f"?api-version={ADO_API_VERSION}"
    )
    try:
        resp = requests.get(url, auth=auth, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        fields = data.get("fields", {})
        return {
            "id": str(data.get("id", work_item_id)),
            "title": fields.get("System.Title"),
            "type": fields.get("System.WorkItemType"),
            "state": fields.get("System.State"),
            "url": data.get("url"),
        }
    except Exception:
        return None


def resolve_prefix(branch_type: str, work_item_type: str | None) -> str:
    if work_item_type:
        item_type = work_item_type.strip().lower()
        if item_type in {"bug"}:
            return "fix"
        if item_type in {"user story", "product backlog item", "feature"}:
            return "feat"
        if item_type in {"task", "spike", "chore"}:
            return "chore"
    return "fix" if branch_type.lower() in ("bug", "bugfix", "hotfix") else "feat"


def resolve_work_item_subject_tag(branch_type: str, work_item_type: str | None) -> str:
    if work_item_type and work_item_type.strip().lower() == "bug":
        return "BUG"
    return "BUG" if branch_type.lower() in ("bug", "bugfix", "hotfix") else "US"


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=check)
    return result.stdout.strip()


def current_branch() -> str:
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        console.print("[bold red]ERROR:[/] Not on a named branch (detached HEAD?).")
        sys.exit(1)
    return branch


def has_staged_changes() -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        capture_output=True
    )
    return result.returncode != 0


def parse_branch(branch: str) -> tuple[str, str | None]:
    """
    Returns (target_ref, work_item_id_or_None).
    target_ref is the full ADO target branch name, e.g. 'refs/heads/release/19.0.0'
    """
    match = BRANCH_PATTERN.match(branch)
    if not match:
        return "", None

    target_seg = match.group("target")
    work_item_id = match.group("work_item_id")

    if target_seg.lower() == "develop":
        target_ref = "refs/heads/develop"
    else:
        target_ref = f"refs/heads/release/{target_seg}"

    return target_ref, work_item_id


def get_work_item_title(org: str, project: str, work_item_id: str, auth: HTTPBasicAuth) -> str | None:
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/wit/workitems/{work_item_id}"
        f"?api-version={ADO_API_VERSION}"
    )
    try:
        resp = requests.get(url, auth=auth, timeout=10)
        resp.raise_for_status()
        return resp.json().get("fields", {}).get("System.Title")
    except Exception:
        return None


def format_commit_subject(
    commit_type: str,
    scope: str | None,
    work_item_tag: str | None,
    work_item_id: str | None,
    summary: str,
) -> str:
    segments = [f"{commit_type}"]
    if scope:
        segments.append(f"[{scope}]")
    if work_item_tag and work_item_id:
        segments.append(f"[{work_item_tag} {work_item_id}]")
    return "".join(segments) + f": {summary}"


def build_commit_message(
    branch_type: str,
    work_item_id: str | None,
    work_item_details: dict | None,
    work_item_label: str,
) -> dict:
    console.print()
    work_item_title = work_item_details.get("title") if work_item_details else None
    work_item_type = work_item_details.get("type") if work_item_details else None

    if work_item_title:
        type_part = f" ({work_item_type})" if work_item_type else ""
        console.print(
            f"[dim]Work item:[/] [cyan]#{work_item_id}[/]{type_part} — {work_item_title}"
        )

    default_type = resolve_prefix(branch_type, work_item_type)
    work_item_tag = resolve_work_item_subject_tag(branch_type, work_item_type) if work_item_id else None

    commit_type = Prompt.ask(
        "  Commit type",
        choices=list(COMMIT_TYPES),
        default=default_type,
        console=console,
    ).strip()

    while True:
        scope = Prompt.ask(
            "  Scope (Requestor/Supplier, leave blank to skip)",
            default="",
            console=console,
        ).strip()
        if not scope or scope in COMMIT_SCOPES:
            break
        console.print("[bold red]ERROR:[/] Scope must be Requestor, Supplier, or blank.")

    while True:
        summary_prompt = f"  Short summary for commit message ({work_item_label})"
        if work_item_title:
            summary = Prompt.ask(summary_prompt, default=work_item_title, console=console).strip()
        else:
            summary = Prompt.ask(summary_prompt, console=console).strip()

        if not summary:
            console.print("[bold red]ERROR:[/] Summary cannot be empty.")
            continue

        subject = format_commit_subject(commit_type, scope or None, work_item_tag, work_item_id, summary)
        if len(subject) > 72:
            console.print(
                f"[bold red]ERROR:[/] Subject must be 72 characters or fewer. Current length: {len(subject)}"
            )
            continue
        break

    return {
        "subject": subject,
        "type": commit_type,
        "scope": scope or None,
        "summary": summary,
        "work_item_tag": work_item_tag,
    }


def commit_and_push(branch: str, message: str) -> str:
    console.print(f"\n[dim]Committing:[/] {message.splitlines()[0]}")
    git("commit", "-m", message)
    commit_hash = git("rev-parse", "HEAD")
    console.print(f"[dim]Pushing [cyan]{branch}[/] to origin…[/]")
    git("push", "--set-upstream", "origin", branch)
    console.print("[green]Pushed.[/]")
    return commit_hash


def get_branch_ref(branch_name: str) -> str:
    return f"refs/heads/{branch_name.replace('refs/heads/', '', 1)}"


def check_target_branch_exists(
    org: str,
    project: str,
    repo: str,
    target_ref: str,
    auth: HTTPBasicAuth,
) -> bool:
    clean_target = target_ref.replace("refs/heads/", "", 1)
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/refs?filter=heads/{clean_target}&api-version={ADO_API_VERSION}"
    )
    resp = requests.get(url, auth=auth, timeout=10)
    resp.raise_for_status()
    values = resp.json().get("value", [])
    return any(v.get("name") == f"refs/heads/{clean_target}" for v in values)


def run_preflight_checks(
    org: str,
    project: str,
    repo: str,
    target_ref: str,
    branch: str,
    auth: HTTPBasicAuth,
    strict: bool,
    origin_detected: bool,
) -> list[str]:
    failures: list[str] = []

    if strict and not BRANCH_PATTERN.match(branch):
        failures.append(
            "Branch naming is invalid for strict mode. Use (feature|bug|bugfix|hotfix)/<release|develop>/<work-item-id>."
        )

    if strict and not origin_detected:
        failures.append("Could not detect Azure DevOps repo context from git origin in strict mode.")

    if not target_ref:
        failures.append("Unable to infer target branch from current branch name.")
        return failures

    try:
        repo_url = (
            f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
            f"{repo}?api-version={ADO_API_VERSION}"
        )
        repo_resp = requests.get(repo_url, auth=auth, timeout=10)
        if repo_resp.status_code in (401, 403):
            failures.append("PAT validation failed for repo/project access (401/403).")
        else:
            repo_resp.raise_for_status()
    except requests.HTTPError as exc:
        failures.append(
            f"Repo validation failed for {project}/{repo}: {exc.response.status_code}"
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"Repo validation failed: {exc}")

    try:
        if not check_target_branch_exists(org, project, repo, target_ref, auth):
            failures.append(f"Target branch not found: {target_ref}")
    except requests.HTTPError as exc:
        failures.append(
            f"Target branch check failed with {exc.response.status_code} for {target_ref}"
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"Target branch check failed: {exc}")

    return failures


def find_existing_pr(
    org: str,
    project: str,
    repo: str,
    source_branch: str,
    target_ref: str,
    auth: HTTPBasicAuth,
) -> dict | None:
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/pullrequests?searchCriteria.status=active"
        f"&searchCriteria.sourceRefName={get_branch_ref(source_branch)}"
        f"&api-version={ADO_API_VERSION}"
    )
    resp = requests.get(url, auth=auth, timeout=15)
    resp.raise_for_status()
    for pr in resp.json().get("value", []):
        if pr.get("targetRefName") == target_ref:
            return pr
    return None


def build_run_summary(
    org: str,
    project: str,
    repo: str,
    work_item_project: str,
    branch: str,
    target_ref: str,
    commit_message: str,
    work_item_id: str | None,
    pr_description: str,
) -> str:
    pr_title = commit_message.splitlines()[0]
    payload: dict = {
        "title": pr_title,
        "description": pr_description,
        "sourceRefName": f"refs/heads/{branch}",
        "targetRefName": target_ref,
    }
    if work_item_id:
        payload["workItemRefs"] = [{"id": work_item_id}]

    lines = [
        "Run summary:",
        f"- org/project/repo: {org}/{project}/{repo}",
        f"- work item project: {work_item_project}",
        f"- source branch: {branch}",
        f"- target branch: {target_ref}",
        f"- linked work item: {work_item_id or '(none)'}",
        f"- commit title: {pr_title}",
    ]
    if "\n" in commit_message:
        lines.append("- commit body: present")
    lines.append(f"- PR description: {'present' if pr_description.strip() else '(empty)'}")
    lines.append("- PR payload preview:")
    lines.append(json.dumps(payload, indent=2))
    return "\n".join(lines)


def build_pr_description(
    *,
    commit_message: str,
    branch: str,
    target_ref: str,
    work_item_id: str | None,
    work_item_details: dict | None,
    commit_scope: str | None,
    commit_summary: str,
    work_item_tag: str | None,
) -> str:
    lines: list[str] = []
    work_item_title = work_item_details.get("title") if work_item_details else None
    work_item_type = work_item_details.get("type") if work_item_details else None
    target_display = target_ref.replace("refs/heads/", "", 1)

    if work_item_id:
        item_ref = f"{work_item_tag or 'US'} {work_item_id}"
        if work_item_title:
            lines.append(f"This PR covers {item_ref}: {work_item_title}.")
        else:
            lines.append(f"This PR covers {item_ref}.")
    else:
        lines.append("This PR contains the requested changes.")

    if commit_scope:
        lines.append(f"It is focused on the {commit_scope} flow.")

    lines.append(f"It is being raised from {branch} to {target_display}.")

    if commit_summary:
        lines.append("")
        lines.append(f"Summary: {commit_summary}.")

    if work_item_type:
        lines.append("")
        lines.append(f"Work item type: {work_item_type}.")

    return "\n".join(lines).strip()


def append_audit_log(
    *,
    status: str,
    org: str,
    project: str,
    repo: str,
    work_item_project: str,
    source_branch: str,
    target_ref: str,
    commit_message: str,
    work_item_id: str | None,
    commit_hash: str | None,
    pr_url: str | None,
    details: str | None = None,
) -> None:
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "run-log.jsonl"

    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "org": org,
        "project": project,
        "repo": repo,
        "work_item_project": work_item_project,
        "source_branch": source_branch,
        "target_ref": target_ref,
        "work_item_id": work_item_id,
        "commit_title": commit_message.splitlines()[0],
        "commit_hash": commit_hash,
        "pr_url": pr_url,
        "details": details,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=True) + "\n")


def open_pr_in_browser(pr_url: str) -> None:
    try:
        if webbrowser.open(pr_url):
            console.print("[green]Opened PR in browser.[/]")
        else:
            console.print(f"[yellow]Could not auto-open browser. Open manually:[/] {pr_url}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Browser open failed:[/] {exc}")


def create_pr(
    org: str,
    project: str,
    repo: str,
    source_branch: str,
    target_ref: str,
    title: str,
    description: str,
    work_item_id: str | None,
    auth: HTTPBasicAuth,
) -> str:
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/pullRequests?api-version={ADO_API_VERSION}"
    )
    payload: dict = {
        "title": title,
        "description": description,
        "sourceRefName": f"refs/heads/{source_branch}",
        "targetRefName": target_ref,
    }
    if work_item_id:
        payload["workItemRefs"] = [{"id": work_item_id}]

    resp = requests.post(url, json=payload, auth=auth, timeout=15)
    resp.raise_for_status()
    pr_id = resp.json()["pullRequestId"]
    return f"https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{pr_id}"


def main() -> None:
    console.print(Panel("[bold cyan]ADO Commit & PR[/]", expand=False))

    parser = argparse.ArgumentParser(description="Commit staged changes and auto-create ADO PR.")
    parser.add_argument(
        "--message",
        "-m",
        help="Commit message to use as-is. If omitted, message is generated interactively.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview commit/push/PR actions without creating commit, push, or PR.",
    )
    parser.add_argument("--org", help="Override Azure DevOps org for this run.")
    parser.add_argument("--project", help="Override Azure DevOps repo/PR project for this run.")
    parser.add_argument("--repo", help="Override Azure DevOps repository name for this run.")
    parser.add_argument(
        "--workitem-project",
        help="Override Azure DevOps project where work items are stored for this run.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Run strict preflight checks and fail fast before commit/push/PR.",
    )
    parser.add_argument(
        "--strict-only",
        action="store_true",
        help="Run validations only and exit. No prompts, commit, push, or PR creation.",
    )
    parser.add_argument(
        "--open-pr",
        action="store_true",
        help="Open the PR URL in your default browser after successful creation.",
    )
    args = parser.parse_args()

    if args.strict_only:
        args.strict = True

    pat, env_org, env_project, env_repo, env_work_item_project = load_pat()
    origin_ctx = get_origin_ado_context()

    org = args.org or (origin_ctx[0] if origin_ctx else "") or env_org
    project = args.project or (origin_ctx[1] if origin_ctx else "") or env_project
    repo = args.repo or (origin_ctx[2] if origin_ctx else "") or env_repo
    work_item_project = args.workitem_project or env_work_item_project or project

    missing_runtime = [k for k, v in [
        ("ORG", org),
        ("PROJECT", project),
        ("REPO", repo),
    ] if not v]
    if missing_runtime:
        console.print(
            "[bold red]ERROR:[/] Unable to resolve ADO context: "
            + ", ".join(missing_runtime)
            + "\nProvide via --org/--project/--repo, set origin remote to ADO URL, or set AZURE_DEVOPS_ORG/AZURE_DEVOPS_PROJECT/AZURE_DEVOPS_REPO in .env."
        )
        sys.exit(1)

    console.print(
        f"[dim]ADO context:[/] org=[cyan]{org}[/], project=[cyan]{project}[/], repo=[cyan]{repo}[/], workitems=[cyan]{work_item_project}[/]"
    )
    auth = HTTPBasicAuth("", pat)

    branch = current_branch()
    console.print(f"[dim]Branch:[/] [cyan]{branch}[/]")

    staged_changes = has_staged_changes()
    if not staged_changes and not args.dry_run and not args.strict_only:
        console.print("[bold red]ERROR:[/] No staged changes. Stage your files first (git add …).")
        sys.exit(1)
    if not staged_changes and (args.dry_run or args.strict_only):
        console.print("[yellow]Dry run:[/] no staged changes found, continuing preview only.")

    target_ref, work_item_id = parse_branch(branch)

    if not target_ref and not args.strict and not args.strict_only:
        console.print(
            f"[yellow]WARNING:[/] Branch [cyan]{branch}[/] doesn't match expected pattern "
            f"(feature|bug)/<release|develop>/<work-item-id>.\n"
        )
        target_input = Prompt.ask(
            "  Enter target branch (e.g. develop or release/19.0.0)",
            console=console,
        ).strip()
        target_ref = f"refs/heads/{target_input}"

    preflight_failures = run_preflight_checks(
        org=org,
        project=project,
        repo=repo,
        target_ref=target_ref,
        branch=branch,
        auth=auth,
        strict=args.strict,
        origin_detected=origin_ctx is not None,
    )
    if preflight_failures:
        console.print("\n[bold red]Preflight checks failed:[/]")
        for item in preflight_failures:
            console.print(f"  - {item}")
        append_audit_log(
            status="preflight_failed",
            org=org,
            project=project,
            repo=repo,
            work_item_project=work_item_project,
            source_branch=branch,
            target_ref=target_ref,
            commit_message=args.message.strip() if args.message else "(not-built)",
            work_item_id=None,
            commit_hash=None,
            pr_url=None,
            details="; ".join(preflight_failures),
        )
        sys.exit(1)

    if args.strict_only:
        append_audit_log(
            status="strict_only_passed",
            org=org,
            project=project,
            repo=repo,
            work_item_project=work_item_project,
            source_branch=branch,
            target_ref=target_ref,
            commit_message=args.message.strip() if args.message else "(validation-only)",
            work_item_id=None,
            commit_hash=None,
            pr_url=None,
        )
        console.print("[bold green]Strict validation passed.[/] No commit, push, or PR was performed.")
        return

    branch_type = branch.split("/")[0] if "/" in branch else "feature"
    work_item_label = "Bug" if branch_type.lower() in ("bug", "bugfix", "hotfix") else "User Story"

    if not work_item_id:
        work_item_id = Prompt.ask(
            f"  {work_item_label} ID to link (leave blank to skip)",
            default="",
            console=console,
        ).strip() or None

    work_item_details: dict | None = None
    if work_item_id:
        with console.status(f"[dim]Fetching work item title from {work_item_project}…[/]"):
            work_item_details = get_work_item_details(org, work_item_project, work_item_id, auth)

    if args.message and args.message.strip():
        commit_message = args.message.strip()
        console.print("[dim]Using provided commit message.[/]")
        work_item_tag = resolve_work_item_subject_tag(branch_type, work_item_details.get("type") if work_item_details else None) if work_item_id else None
        commit_scope = "Requestor" if "[Requestor]" in commit_message else "Supplier" if "[Supplier]" in commit_message else None
        commit_summary = commit_message.split(":", 1)[1].strip() if ":" in commit_message else commit_message.strip()
    else:
        commit_details = build_commit_message(branch_type, work_item_id, work_item_details, work_item_label)
        commit_message = commit_details["subject"]
        commit_scope = commit_details["scope"]
        commit_summary = commit_details["summary"]
        work_item_tag = commit_details["work_item_tag"]

    pr_description = build_pr_description(
        commit_message=commit_message,
        branch=branch,
        target_ref=target_ref,
        work_item_id=work_item_id,
        work_item_details=work_item_details,
        commit_scope=commit_scope,
        commit_summary=commit_summary,
        work_item_tag=work_item_tag,
    )

    console.print(f"\n[dim]Commit message preview:[/]\n[bold]{commit_message}[/]\n")
    console.print(f"[dim]PR description preview:[/]\n{pr_description}\n")
    target_display = target_ref.replace("refs/heads/", "")
    console.print(
        "[dim]Planned PR:[/] "
        f"[cyan]{branch}[/] -> [cyan]{target_display}[/]"
        + (f" | linked item: [cyan]#{work_item_id}[/]" if work_item_id else "")
    )
    console.print(
        Panel(
            build_run_summary(
                org=org,
                project=project,
                repo=repo,
                work_item_project=work_item_project,
                branch=branch,
                target_ref=target_ref,
                commit_message=commit_message,
                work_item_id=work_item_id,
                pr_description=pr_description,
            ),
            title="Execution Plan",
            expand=False,
        )
    )

    if args.dry_run:
        append_audit_log(
            status="dry_run",
            org=org,
            project=project,
            repo=repo,
            work_item_project=work_item_project,
            source_branch=branch,
            target_ref=target_ref,
            commit_message=commit_message,
            work_item_id=work_item_id,
            commit_hash=None,
            pr_url=None,
        )
        console.print("\n[bold green]Dry run complete.[/] No commit, push, or PR was created.")
        return

    confirm = Prompt.ask("  Proceed? [y/N]", default="N", console=console).strip().lower()
    if confirm != "y":
        console.print("[yellow]Aborted.[/]")
        sys.exit(0)

    commit_hash = commit_and_push(branch, commit_message)

    pr_title = commit_message.splitlines()[0]
    console.print(f"\n[dim]Creating PR:[/] [cyan]{branch}[/] → [cyan]{target_display}[/]")

    try:
        pr_url = create_pr(org, project, repo, branch, target_ref, pr_title, pr_description, work_item_id, auth)
        console.print(f"\n[bold green]PR created:[/] {pr_url}")
        if args.open_pr:
            open_pr_in_browser(pr_url)
        append_audit_log(
            status="success",
            org=org,
            project=project,
            repo=repo,
            work_item_project=work_item_project,
            source_branch=branch,
            target_ref=target_ref,
            commit_message=commit_message,
            work_item_id=work_item_id,
            commit_hash=commit_hash,
            pr_url=pr_url,
        )
    except requests.HTTPError as exc:
        if exc.response.status_code == 409:
            existing = find_existing_pr(org, project, repo, branch, target_ref, auth)
            if existing:
                existing_url = (
                    f"https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/"
                    f"{existing.get('pullRequestId')}"
                )
                reviewers = existing.get("reviewers", [])
                reviewer_names = ", ".join(r.get("displayName", "") for r in reviewers) or "(none)"
                updated = existing.get("creationDate", "unknown")
                status = existing.get("status", "unknown")
                console.print("[yellow]A matching PR already exists.[/]")
                console.print(f"  URL: {existing_url}")
                console.print(f"  Status: {status}")
                console.print(f"  Reviewers: {reviewer_names}")
                console.print(f"  Last update: {updated}")
                append_audit_log(
                    status="existing_pr",
                    org=org,
                    project=project,
                    repo=repo,
                    work_item_project=work_item_project,
                    source_branch=branch,
                    target_ref=target_ref,
                    commit_message=commit_message,
                    work_item_id=work_item_id,
                    commit_hash=commit_hash,
                    pr_url=existing_url,
                    details=f"status={status}; reviewers={reviewer_names}; updated={updated}",
                )
                return
        append_audit_log(
            status="pr_failed",
            org=org,
            project=project,
            repo=repo,
            work_item_project=work_item_project,
            source_branch=branch,
            target_ref=target_ref,
            commit_message=commit_message,
            work_item_id=work_item_id,
            commit_hash=commit_hash,
            pr_url=None,
            details=f"{exc.response.status_code} {exc.response.text}",
        )
        console.print(f"[bold red]PR creation failed:[/] {exc.response.status_code} {exc.response.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
