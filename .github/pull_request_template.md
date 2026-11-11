## What this changes

<!-- One change per pull request. Link the issue where a feature or a change of default was agreed. -->

## What changes for users

<!-- Results, defaults, wording, columns, file formats, including for users who do not use
     the new feature. "Nothing" is a fine answer if it is true. -->

## How it was tested

<!-- For a bug fix: the test that fails without the fix. For a solver or mesher change:
     the hand calculation or independent solve, and the result of tests/accuracy. -->

## Checklist

- [ ] One change, discussed in an issue first if it is a feature or changes a default
- [ ] `python -m pytest tests` and `ruff check .` pass
- [ ] `python -m pytest tests/accuracy` passes (solver or mesher changes)
- [ ] A bug fix has a test that fails on the base branch
- [ ] `docs/CHANGELOG.md` has a line for it
- [ ] I have read [CONTRIBUTING.md](https://github.com/drewsum/KiPI/blob/main/CONTRIBUTING.md)
