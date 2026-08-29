# Contributing

## Workflow

`main` is protected: no direct pushes, and merging requires 1 approving review. All changes go through a branch and a pull request.

1. Create a branch off `main` using the naming convention below.
2. Commit using Conventional Commits (enforced locally by a git hook, see below).
3. Open a PR into `main`. The PR title must also follow Conventional Commits — it's linted in CI and becomes the squash-merge commit message.
4. Get it reviewed and merged (squash merge).

**Note for coding agents working in this repo:** open the PR, but do not merge it — leave it for @arnaud18o5 to review. GitHub doesn't allow a PR author to approve their own PR, and PRs opened via `gh pr create` under the maintainer's authenticated account count as authored by them, so a required review can only come from an actual human look at the diff. `enforce_admins` is intentionally off so the maintainer can merge via the admin bypass after reviewing, without needing a second collaborator account — but that bypass is for them to use, not the agent.

## Branch naming

`<type>/<short-description>`, e.g. `feat/impact-analysis-hops`, `fix/connect-resolution`.

## Commit / PR title types (Conventional Commits)

| Type       | Use for |
|------------|---------|
| `feat`     | a new feature |
| `fix`      | a bug fix |
| `docs`     | documentation only |
| `style`    | formatting, no code behavior change |
| `refactor` | code change that neither fixes a bug nor adds a feature |
| `perf`     | performance improvement |
| `test`     | adding or fixing tests |
| `build`    | build system or dependencies |
| `ci`       | CI configuration |
| `chore`    | anything else (tooling, maintenance) |
| `revert`   | reverts a previous commit |

Format: `type(scope): short description`, e.g. `feat(parser): resolve connect through feature typing`. The scope is optional.

## Commit lint setup

This repo uses [pre-commit](https://pre-commit.com/) with [conventional-pre-commit](https://github.com/compilerla/conventional-pre-commit) to validate commit messages locally.

```bash
pip install pre-commit
pre-commit install --hook-type commit-msg
```

After this, any commit with a message that doesn't follow Conventional Commits will be rejected locally. PR titles are additionally checked in CI (`.github/workflows/pr-title-lint.yml`), since `main` only receives squash-merge commits built from the PR title.

## Local setup

See [README.md](README.md) for environment setup and running the pipeline.
