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
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()

ADO_API_VERSION = "7.1"

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


def build_commit_message(
    branch_type: str,
    work_item_id: str | None,
    work_item_title: str | None,
    work_item_label: str,
) -> str:
    console.print()
    if work_item_title:
        console.print(f"[dim]Work item:[/] [cyan]#{work_item_id}[/] — {work_item_title}")

    prefix = "fix" if branch_type.lower() in ("bug", "bugfix", "hotfix") else "feat"
    scope = f"#{work_item_id}" if work_item_id else ""

    if work_item_title:
        return f"{prefix}({scope}): {work_item_title}" if scope else f"{prefix}: {work_item_title}"

    description = Prompt.ask(
        f"  Short description for commit message ({work_item_label})",
        console=console,
    ).strip()
    if not description:
        console.print("[bold red]ERROR:[/] Description cannot be empty.")
        sys.exit(1)

    header = f"{prefix}({scope}): {description}" if scope else f"{prefix}: {description}"
    return header


def commit_and_push(branch: str, message: str) -> None:
    console.print(f"\n[dim]Committing:[/] {message.splitlines()[0]}")
    git("commit", "-m", message)
    console.print(f"[dim]Pushing [cyan]{branch}[/] to origin…[/]")
    git("push", "--set-upstream", "origin", branch)
    console.print("[green]Pushed.[/]")


def create_pr(
    org: str,
    project: str,
    repo: str,
    source_branch: str,
    target_ref: str,
    title: str,
    work_item_id: str | None,
    auth: HTTPBasicAuth,
) -> str:
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{repo}/pullRequests?api-version={ADO_API_VERSION}"
    )
    payload: dict = {
        "title": title,
        "sourceRefName": f"refs/heads/{source_branch}",
        "targetRefName": target_ref,
    }
    if work_item_id:
        payload["workItemRefs"] = [{"id": work_item_id}]

    resp = requests.post(url, json=payload, auth=auth, timeout=15)
    if resp.status_code == 409:
        console.print("[yellow]A PR already exists for this branch.[/]")
        data = resp.json()
        existing_url = (
            f"https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/"
            + str(data.get("pullRequestId", ""))
        )
        return existing_url
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
    args = parser.parse_args()

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
    if not staged_changes and not args.dry_run:
        console.print("[bold red]ERROR:[/] No staged changes. Stage your files first (git add …).")
        sys.exit(1)
    if not staged_changes and args.dry_run:
        console.print("[yellow]Dry run:[/] no staged changes found, continuing preview only.")

    target_ref, work_item_id = parse_branch(branch)

    if not target_ref:
        console.print(
            f"[yellow]WARNING:[/] Branch [cyan]{branch}[/] doesn't match expected pattern "
            f"(feature|bug)/<release|develop>/<work-item-id>.\n"
        )
        target_input = Prompt.ask(
            "  Enter target branch (e.g. develop or release/19.0.0)",
            console=console,
        ).strip()
        target_ref = f"refs/heads/{target_input}"

    branch_type = branch.split("/")[0] if "/" in branch else "feature"
    work_item_label = "Bug" if branch_type.lower() in ("bug", "bugfix", "hotfix") else "User Story"

    if not work_item_id:
        work_item_id = Prompt.ask(
            f"  {work_item_label} ID to link (leave blank to skip)",
            default="",
            console=console,
        ).strip() or None

    work_item_title: str | None = None
    if work_item_id:
        with console.status(f"[dim]Fetching work item title from {work_item_project}…[/]"):
            work_item_title = get_work_item_title(org, work_item_project, work_item_id, auth)

    if args.message and args.message.strip():
        commit_message = args.message.strip()
        console.print("[dim]Using provided commit message.[/]")
    else:
        commit_message = build_commit_message(branch_type, work_item_id, work_item_title, work_item_label)

    console.print(f"\n[dim]Commit message preview:[/]\n[bold]{commit_message}[/]\n")
    target_display = target_ref.replace("refs/heads/", "")
    console.print(
        "[dim]Planned PR:[/] "
        f"[cyan]{branch}[/] -> [cyan]{target_display}[/]"
        + (f" | linked item: [cyan]#{work_item_id}[/]" if work_item_id else "")
    )

    if args.dry_run:
        console.print("\n[bold green]Dry run complete.[/] No commit, push, or PR was created.")
        return

    confirm = Prompt.ask("  Proceed? [y/N]", default="N", console=console).strip().lower()
    if confirm != "y":
        console.print("[yellow]Aborted.[/]")
        sys.exit(0)

    commit_and_push(branch, commit_message)

    pr_title = commit_message.splitlines()[0]
    console.print(f"\n[dim]Creating PR:[/] [cyan]{branch}[/] → [cyan]{target_display}[/]")

    try:
        pr_url = create_pr(org, project, repo, branch, target_ref, pr_title, work_item_id, auth)
        console.print(f"\n[bold green]PR created:[/] {pr_url}")
    except requests.HTTPError as exc:
        console.print(f"[bold red]PR creation failed:[/] {exc.response.status_code} {exc.response.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()
