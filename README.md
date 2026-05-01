# ADO Commit and PR Automation

Automates the final workflow for a feature/bug branch:

1. Use current branch name to infer PR target branch
2. Detect or ask for User Story/Bug ID
3. Build commit message (provided by you or generated)
4. Commit staged changes
5. Push branch
6. Create Azure DevOps PR and link work item

## Branch Convention

Supported formats:

- `feature/<release|develop>/<work-item-id>`
- `bug/<release|develop>/<work-item-id>`
- `bugfix/<release|develop>/<work-item-id>`
- `hotfix/<release|develop>/<work-item-id>`

Examples:

- `feature/19.0.0/61527` -> target `refs/heads/release/19.0.0`
- `feature/develop/61527` -> target `refs/heads/develop`
- `bug/20.0.0/61527` -> target `refs/heads/release/20.0.0`

If branch does not match this pattern, script asks target branch and optional work item ID.

## Requirements

- Python 3.10+
- Git installed and branch checked out
- Azure DevOps PAT with repo + work item read/write permissions as needed

Install dependencies:

```bash
pip install -r requirements.txt
```

## Environment Variables

Create `.env` inside this folder (`./.env`) only.

You can copy from `.env.example`:

```bash
copy .env.example .env
```

Required keys:

```env
AZURE_DEVOPS_PAT=your_pat
# Optional defaults (can be auto-detected from current repo origin remote)
AZURE_DEVOPS_ORG=your_org
AZURE_DEVOPS_PROJECT=your_repo_project
AZURE_DEVOPS_REPO=your_repo

# Optional when work items live in a different project
AZURE_DEVOPS_WORKITEM_PROJECT=your_work_item_project
```

Notes:

- `AZURE_DEVOPS_PAT` is required.
- `ORG/PROJECT/REPO` can be auto-detected from current repo `origin` if it is an Azure DevOps remote.
- Work item project defaults to PR project unless `AZURE_DEVOPS_WORKITEM_PROJECT` is set.

## Usage

Basic interactive run:

```bash
python commit_and_pr.py
```

Provide commit message directly:

```bash
python commit_and_pr.py -m "feat(#61527): add supplier search validation"
```

Dry-run (no commit/push/PR):

```bash
python commit_and_pr.py --dry-run
```

Dry-run with explicit message:

```bash
python commit_and_pr.py --dry-run -m "fix(#61527): handle null payload"
```

Validation-only mode (no prompts, no commit/push/PR):

```bash
python commit_and_pr.py --strict-only
```

Open PR automatically in browser after creation:

```bash
python commit_and_pr.py --open-pr
```

Run for a specific repo/project without changing `.env`:

```bash
python commit_and_pr.py --org your_org --project SupplierHub_22632 --repo your_repo_name --workitem-project SupplierDueDiligence_PR1086
```

## Flags

| Flag | Description |
|---|---|
| `-m`, `--message` | Use provided commit message as-is. If omitted, script generates message interactively. |
| `--dry-run` | Preview commit/PR plan without creating commit, push, or PR. |
| `--org` | Override Azure DevOps org for this run. |
| `--project` | Override Azure DevOps project used for repo and PR creation. |
| `--repo` | Override Azure DevOps repository name for this run. |
| `--workitem-project` | Override Azure DevOps project where work items are fetched from. |
| `--strict` | Run strict preflight checks and fail fast before commit/push/PR. |
| `--strict-only` | Run validations only and exit. No prompts, commit, push, or PR creation. |
| `--open-pr` | Open created PR URL in default browser after successful creation. |

## Multiple Repositories

For multiple repos, you do not need to store many names in one env file.

Preferred flow:

1. Open terminal in the target repository.
2. Keep only PAT in `.env`.
3. Script auto-reads org/project/repo from that repo's `origin` remote.
4. Keep `AZURE_DEVOPS_WORKITEM_PROJECT=SupplierDueDiligence_PR1086` once in `.env`.

If a repo remote is not ADO or you want explicit values, use CLI overrides (`--org --project --repo`).

## Commit Message Behavior

- If `--message` is passed, that value is used directly.
- If message is not passed:
  - Script tries work item type first (`Bug` => `fix`, `User Story/PBI/Feature` => `feat`, `Task/Spike` => `chore`)
  - If work item type is unavailable, it falls back to branch type (`feature` => `feat`, `bug|bugfix|hotfix` => `fix`)
  - If work item title is available from ADO, it uses that title
  - Otherwise asks for a short description and builds header

Examples:

- `feat(#61527): Add invoice filter`
- `fix(#61527): Resolve API timeout`

## End-to-End Workflow

1. Stage your changes (`git add ...`)
2. Run script from branch like `feature/19.0.0/61527`
3. Confirm commit preview
4. Script commits and pushes branch
5. Script creates PR to inferred target
6. Script links work item to PR

## Terminal Output

After successful PR creation, terminal prints the created PR link:

```text
PR created: https://dev.azure.com/<org>/<project>/_git/<repo>/pullrequest/<id>
```

In dry-run, script still prints full commit message preview and planned PR details, but does not create PR.

## Dry-Run Behavior

In `--dry-run` mode, script will:

- Read branch
- Infer/ask target branch
- Infer/ask story or bug ID
- Build commit message preview
- Show planned PR mapping
- Show a structured execution plan including resolved org/project/repo, work item project, commit title/body status, and PR payload preview

In `--dry-run` mode, script will not:

- create commit
- push branch
- call PR creation API

## Preflight Checks

The script performs preflight checks before the main action.

- Repo/project validation with your PAT
- Target branch existence check

When `--strict` is used, it also enforces:

- Branch naming pattern: `(feature|bug|bugfix|hotfix)/<release|develop>/<work-item-id>`
- ADO context must be detected from current repo `origin`

If any check fails, script exits before commit/push/PR.

`--strict-only` means "run validations only".

In this mode, script validates:

- ADO context resolution (org/project/repo)
- PAT access to repo/project
- target branch inference and existence
- strict branch naming and origin-detection rules

Then it exits without prompting, commit, push, or PR creation.

## Existing PR Handling

If a matching PR already exists for the same source and target branch, script reports:

- Existing PR URL
- PR status
- Reviewers
- Last update/creation timestamp available from ADO

No new duplicate PR is created.

## Audit Logs

Each run writes an audit line to:

- `logs/run-log.jsonl`

Includes:

- UTC timestamp
- status (`dry_run`, `success`, `existing_pr`, `preflight_failed`, `strict_only_passed`, `pr_failed`)
- resolved org/project/repo and work item project
- source/target branches
- work item id
- commit title and commit hash (if created)
- PR URL (if available)
- failure/extra details

## Troubleshooting

| Issue | Reason | Fix |
|---|---|---|
| Missing env variables | Required ADO values not found | Set all required keys in `.env` |
| No staged changes | Nothing in index | Run `git add` or use `--dry-run` for preview |
| Branch not matching pattern | Custom branch format | Enter target branch and work item when prompted |
| Work item title not fetched | Invalid ID or permission issue | Check ID, PAT scope, and org/project access |
| PR already exists | ADO already has open PR for source branch | Script reports existing PR URL |
| PR URL not visible | Looking in wrong execution mode | Run normal mode (not `--dry-run`) and check the `PR created:` line in terminal |

## Changelog

### v1.5.0 - Browser Open + Validation-Only Mode

| Feature | Details |
|---|---|
| Browser auto-open | Added `--open-pr` to launch created PR in your default browser. |
| Validation-only mode | Added `--strict-only` to run checks and exit with no side effects. |
| Validation docs | Added explicit explanation of what validations are executed in strict-only mode. |

### v1.4.1 - Output Documentation Update

| Feature | Details |
|---|---|
| PR link visibility docs | Added explicit `Terminal Output` section showing the `PR created: <url>` line. |
| Dry-run output clarification | Documented that dry-run still prints commit and PR preview but does not create PR. |

### v1.4.0 - Validation, Reporting, and Logging

| Feature | Details |
|---|---|
| Work item type-aware commits | Commit prefix now prefers work item type mapping (`Bug`/`User Story`/`Task` etc.) before branch fallback. |
| Strict preflight mode | Added `--strict` with branch-format and origin-context enforcement plus fail-fast validation. |
| Rich duplicate PR output | On duplicate PR conflict, script now reports URL, status, reviewers, and update timestamp details. |
| Structured dry-run plan | Dry-run now prints a full execution summary and PR payload preview. |
| Run audit logging | Added JSONL audit logs under `logs/run-log.jsonl` for traceability. |

### v1.3.0 - Multi-Repo Context Resolution

| Feature | Details |
|---|---|
| Multi-repo friendly | Added auto-detection of org/project/repo from current git `origin` ADO URL. |
| Run-time overrides | Added `--org`, `--project`, `--repo`, and `--workitem-project` flags. |
| Cross-project work items | Added support for work items from a different ADO project than PR/repo project. |

### v1.2.0 - Local Env Standardization

| Feature | Details |
|---|---|
| Local-only env loading | Script now reads `.env` only from the automation folder. |
| Env template | Added `.env.example` with all required ADO keys. |

### v1.1.0 - Dry Run + Message Control

| Feature | Details |
|---|---|
| Optional commit message | Added `-m/--message` to accept user-provided commit message. |
| Story/Bug aware prompts | Prompts User Story or Bug ID based on branch type when ID is missing. |
| Dry run mode | Added `--dry-run` to preview commit and PR actions with zero side effects. |

### v1.0.0 - Initial Release

| Feature | Details |
|---|---|
| Branch parsing | Infers target release/develop branch from naming convention. |
| Auto commit+push+PR | Runs commit, push, and PR creation in one flow. |
| Work item linking | Links ADO work item to created PR. |
