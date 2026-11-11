# Contributing to KiPI

Bug reports, fixes, example boards and features are welcome. This page says what KiPI is
for, what fits in it, and what a pull request needs before it can be merged.

## What KiPI is

KiPI is a power integrity simulator for a PCB layout: DC voltage drop, AC PDN impedance
and coupled noise, solved from the board's copper and a setup the user enters in the
window. It aims to be something an engineer can open, set up in a few minutes and trust.

That sets the scope:

* **In scope:** correct and faster solves, reading more board formats, clearer results and
  reports, fixes for boards that fail to load or mesh, and analysis that a typical user
  would set up by hand in the window.
* **Out of scope unless agreed first:** support for one company's or one tool's own data
  format or workflow, features that only work with data from such a file, and features
  that add fields, columns or modes most users will never fill in. These are better kept
  in a fork or a separate tool that calls KiPI.

When a feature could go either way, the question is whether KiPI stays simple for someone
who does not use it.

## Before you write code

* **A bug fix** needs no discussion first. Open the pull request.
* **A feature, a new setting, a change to a default or a change to what users see** needs
  an issue first, with the maintainer's agreement on the approach. A pull request for
  work that was not discussed may be closed, however good the code is.

The maintainer decides what goes into KiPI and may take part of a pull request and leave
the rest.

## Pull requests

* **One change per pull request.** A bug fix, a feature and a refactor are three pull
  requests. Do not stack pull requests on each other, and do not bundle fixes into a
  feature.
* **Keep it reviewable.** As a guide, a pull request over about 1000 changed lines
  (tests aside) should have been split or discussed first.
* **Target the open release branch** (for example `v1.0.1`), or `main` if there is none.
* **Say what changes for the user**, including for users who never touch the new feature:
  results, defaults, wording, columns, file formats. If nothing changes for them, say so,
  and make sure it is true.
* **Describe what the code does, not what it is meant to do.** A claim in the description
  (a limit raised, a case fixed, results unchanged) must be something the diff does and a
  test shows.
* **Add a line to `docs/CHANGELOG.md`** under the version in development.

## Tests

* **A bug fix comes with a test that fails without the fix.** Run the new test against
  the base branch to check. If the bug cannot be reproduced in a test, say why and attach
  what does reproduce it.
* **A feature comes with tests of its behaviour**, and of the default path staying as it
  was.
* Do not weaken an existing test to make a change pass. If an expected value has to
  change, the description says which and why.
* Run before pushing:

  ```
  python -m pytest tests
  ruff check .
  ```

  CI runs these on Windows, Linux and macOS. The accuracy tests only run in CI for pull
  requests into `main`, so run them yourself for anything that touches a solver or the
  mesher:

  ```
  python -m pytest tests/accuracy
  ```

## Changes to the solvers and the mesher

These decide whether KiPI's numbers can be trusted, so they are held to more:

* **Results for existing setups do not change** unless changing them is the purpose of the
  pull request, and then the description gives the before and after on a board.
* **New physics is opt-in**, set per item (a VRM, a load, a link) where that is where it
  applies, and off by default.
* **Show it is right.** A hand calculation in `tests/accuracy`, or agreement with an
  independent solve. Agreement with another tool is worth stating but is not proof: say
  which settings and assumptions each tool used.
* **State the assumption.** A model that idealizes something (a short where there is a
  resistance, an efficiency, an equal share) says so in the tooltip and the user guide,
  and says which way it moves the result.
* **Check the cost on a large board.** Anything that runs once per pad, per via or per
  solve iteration should be measured on a board with thousands of them.

## Importers and file formats

* A reader for a board format (KiCad, ODB++, HyperLynx and others) is in scope. Attach or
  point to a board that exercises it; boards that can be shared become tests.
* An importer fills in the setup the user could have entered by hand. It does not change
  solver defaults, add solver behaviour or switch on settings the user cannot see and
  change in the window.
* Support for a format that one party defines and maintains needs agreement first (see
  [Before you write code](#before-you-write-code)), and its code stays within its own
  module.

## Style

* Follow the code around your change: naming, comment style, module layout. `ruff` settles
  formatting questions.
* Comments say why, for the reader of this repository. Leave out notes about a fork, a
  customer or a private board.
* Every control has a tooltip, and anything a user sees is covered in `docs/help.html`.

## Licence and copyright

KiPI is licensed under GPL-3.0-or-later. By contributing you agree that your contribution
is licensed the same way, and that you have the right to submit it. You keep the copyright
in what you write; the git history records who wrote what. A copyright line may be added
to a new file you wrote in full. Please do not add one to an existing file.

## Reporting a bug

Open an [issue](https://github.com/drewsum/KiPI/issues) with the KiPI version, how it was
started (plugin or standalone), the log, and if you can share it, the board file or a
snapshot (**Save snapshot** in the toolbar).
