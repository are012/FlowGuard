# AGENTS.md

## Documentation Guide

- `README.md`: Defines the product vision, user experience, and MVP scope.
- `ARCHITECTURE.md`: Defines the system architecture, component responsibilities, and execution flows.
- `SPECIFICATION.md`: Defines the implementation requirements, constraints, and authoritative scope.
- `DEVELOPMENT.md`: Defines local setup, validation commands, and operational safeguards.
- `docs/BACKLOG.md`: Tracks verified improvement items and resolved history.
- `docs/plans/`: Holds in-progress design documents. Not authoritative until implemented.

If documents conflict, follow this order of precedence: `SPECIFICATION.md` → `ARCHITECTURE.md` → `README.md`.

## General Principles

- Review the relevant code and documentation before making changes.
- Do not assume unfamiliar or incomplete code is incorrect.
- Do not revert or overwrite another contributor's changes without an explicit request.
- Do not include unrelated refactoring, formatting, file cleanup, or dependency updates in the same task.
- Preserve existing behavior and compatibility unless the task explicitly allows a breaking change.
- When requirements are ambiguous, report the ambiguity before implementing a broad interpretation.
- Never claim that a command or test was run when it was not.
- Clearly report failed validation, incomplete work, and known risks.

## Before Implementation

Before implementation:

1. Inspect the current repository state.
2. Review the relevant code and documentation.
3. Check for existing branches, worktrees, and uncommitted changes that may conflict.
4. Create a dedicated branch and worktree for the task.

Planning and investigation may happen in the current worktree and branch. Implementation must happen in the dedicated task worktree.

## Task Scope and Ownership

- Each task must use one primary branch and one primary worktree.
- Do not implement the same task in multiple worktrees unless the work is explicitly coordinated.
- When multiple tasks affect the same files or shared interfaces, coordinate the order of work before implementation.
- Prefer small, reviewable changes over broad rewrites.
- Do not change APIs, data schemas, configuration formats, or shared types without communicating the change.
- When changing a shared interface, update all affected callers and tests in the same task.
- Record important assumptions and limitations in the task summary or pull request.

## Git Worktree Workflow

- Planning and investigation may happen in the current worktree and branch.
- Before implementation, create a dedicated Git branch and worktree for the task.
- Do not implement directly on `main`.
- Create task worktrees under `<project-root>/.worktrees/`.
- Do not create task worktrees as siblings of the project root.
- Name the worktree directory from the corresponding branch name after sanitizing it for filesystem use.
- Replace characters unsuitable for file or path names, including `/`, whitespace, `:`, `*`, `?`, `"`, `<`, `>`, and `|`, with `_`.
- Collapse repeated `_` characters.
- Avoid leading or trailing `_` characters where practical.
- Keep implementation changes isolated in the task worktree.
- Treat `.worktrees/` as local workspace storage.
- Do not commit `.worktrees/` contents unless the user explicitly requests changes to worktree-management files.

Example:

```text
branch: feature/dashboard-summary
worktree: .worktrees/feature_dashboard_summary
```

## Branch and Commit Rules

- Use a branch name that clearly reflects the task.
- Keep commits focused and logically grouped.
- Do not include unrelated changes in the same commit.
- Do not commit secrets, credentials, generated local state, or machine-specific configuration.
- Do not commit local `.env` files unless the repository intentionally tracks a safe example file such as `.env.example`.
- Review `git status` and the staged diff before committing.
- Include required tests and documentation updates with the implementation.
- Do not rewrite or force-push shared branch history without explicit approval.

## Parallel Work Rules

- Do not modify or delete changes that belong to another worktree.
- Report possible file or interface conflicts before implementation.
- When multiple tasks depend on a shared type or interface, establish the shared contract before dependent work proceeds.
- Do not treat a temporary interface from another task as a finalized contract.
- During handoff, distinguish confirmed decisions from assumptions and unresolved items.

## Validation

Before requesting review:

- Run the smallest relevant validation first.
- Run affected unit and integration tests.
- Run linting, type checks, formatting checks, and build checks when applicable.
- Confirm that unrelated existing behavior was not unintentionally changed.
- Record the exact commands that were run.
- Record failed checks, skipped checks, environment limitations, and known risks.
- Do not weaken tests merely to make them pass.
- Do not replace meaningful validation with superficial checks.

## Review Handoff

When handing work to another developer or agent, provide:

- the task goal;
- the branch name;
- the worktree path;
- a concise summary of changes;
- the main files changed;
- the tests and validation commands run;
- known limitations and unresolved questions;
- local configuration required to continue the work;
- Docker resources and sharing details, when Docker was used.

The reviewer must inspect the diff and validation results before approving a merge.

## Applying Changes to `main`

- Merge changes into `main` only after explicit user approval.
- Phrases such as `apply to main`, `main 브랜치에 적용`, or equivalent count as explicit approval.
- When applying changes to `main`:
  1. inspect the task worktree status;
  2. exclude local-only files;
  3. commit the intended changes on the task branch;
  4. switch to the main worktree;
  5. merge the task branch into `main`;
  6. report the merge result and any conflicts.
- Do not commit local `.env` files, `.worktrees/`, temporary outputs, or machine-specific files.
- Do not delete the task worktree or branch after merging unless cleanup is requested or approved.

## Conflict Resolution

- Do not resolve merge conflicts by blindly choosing one side.
- Understand the intent of both changes before editing the conflicted file.
- Preserve unrelated work from both branches where possible.
- Re-run affected tests after resolving conflicts.
- If the correct resolution depends on a product or architecture decision, request clarification instead of deciding silently.
- Record manual conflict resolutions in the handoff summary.

## Docker Safety

These rules apply only when Docker or Docker Compose is actually used for the task.

- Do not introduce Docker configuration into a task that does not require Docker.
- Inspect the repository's current Docker configuration and running resources before starting containers.
- Do not stop or delete existing containers, networks, images, or volumes without approval.
- Do not delete volumes or data when their ownership or sharing status is unclear.
- Do not run broad cleanup commands such as `docker system prune` or `docker volume prune`.
- Do not commit local `.env` files or machine-specific Docker configuration.
- Before starting a new Compose stack, check for project-name, port, and volume conflicts.
- Clean up Docker resources only when the user explicitly requests or approves cleanup.

## Documentation

- Update relevant documentation when behavior, configuration, interfaces, or workflow changes.
- Do not duplicate the same rule across multiple documents unless necessary.
- Keep project-specific requirements and implementation details in separate project documents.
- When code and documentation disagree, report the inconsistency instead of silently choosing one.

## Cleanup

- Treat cleanup as a separate action from implementation and merging.
- Do not delete worktrees, branches, containers, networks, volumes, caches, or generated data unless cleanup is requested or approved.
- Identify private and shared resources before cleanup.
- Report which resources were removed and which were intentionally retained.

## Definition of Done

A task is complete when:

- the requested change is implemented in its dedicated worktree;
- unrelated changes are excluded;
- relevant validation has been run;
- failures and limitations are disclosed;
- required documentation is updated;
- the work is ready for review;
- the task branch has not been merged into `main` without explicit approval;
- local-only files and shared resources remain protected.
