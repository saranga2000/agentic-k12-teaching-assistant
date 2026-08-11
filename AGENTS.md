# Agent working agreement

This file is the single source of truth for any coding assistant working in this repo
(Claude Code, Codex, Gemini CLI, Cursor, Continue, aider). It is intentionally
tool-agnostic. `CLAUDE.md` and `.cursorrules` are pointers to this file.

## Prime directive

The human reviewing your output can read Python but will not rewrite it. Optimise every
choice for **cheap review**: small files, obvious names, no cleverness, no frameworks
introduced without being asked.

## Hard rules

1. **Tests first.** Write the failing test, show it fail, then implement. A PR with
   implementation and no test is rejected.
2. **One module, one job.** If a file passes ~150 lines, split it or explain why in the
   module docstring.
3. **Type hints everywhere.** `mypy --strict` on `src/` must pass.
4. **No new dependency without a line in `docs/ARCHITECTURE.md` justifying it.** Standard
   library first. `pydantic`, `httpx`, `pytest`, `ruff`, `mypy`, `fastapi`, `jinja2` are
   pre-approved.
5. **No hardcoded product name.** The student names the coach at setup. Use
   `settings.coach_name` with the placeholder `"Coach"`. Grep for a hardcoded name before
   you commit.
6. **No hardcoded student, grade, state, subject, or content source.** All of these are
   configuration or database rows.
7. **Never write a prompt string inline in Python.** Prompts live in `prompts/*.md` and
   are loaded by id. They are versioned and eval'd like code.
8. **Multi-user schemas from day one.** Every persisted row carries `student_id`. No
   singleton assumptions. Do not build authentication.
9. **Model calls go through an adapter.** Provider SDK or raw HTTP calls live only in
   `src/alc/llm/`. Every other package, including `alc.transcribe` and `alc.diagnose`,
   calls a model through that adapter. Swapping providers is a new file, not a refactor.
10. **Fail loud on unreadable input.** A confident wrong grade is the worst outcome in
    this system. When confidence is below threshold, return `NEEDS_HUMAN`, never a guess.

## Definition of done for any task

- [ ] Test written first and now passing
- [ ] `make check` clean (ruff, mypy strict, pytest)
- [ ] Public functions have docstrings stating the contract, not restating the name
- [ ] No secret, student name, or photo committed
- [ ] If behaviour is user-visible, `docs/ROADMAP.md` milestone table updated

## Repo conventions

- Package root `src/alc/`, imports are absolute (`from alc.domain.models import ...`)
- Dataclasses for domain objects, pydantic only at I/O boundaries
- Dates are `datetime.date`; timestamps are timezone-aware UTC
- Money and token cost tracked in `Decimal`, never float
- Test files are flat in `tests/` and named after the module under test
  (`src/alc/mastery/model.py` -> `tests/test_mastery.py`). Move to mirrored
  subdirectories only once a package has three or more test files. Do not create empty
  test directories in advance.
- Hidden files and directories are part of this repo, search them
## What to ask about instead of guessing

Grading semantics, feedback policy, anything touching a child's data, anything that
changes what the student sees after a wrong answer. Guessing here is expensive.
