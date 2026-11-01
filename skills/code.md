# Code / app

Use this checklist for scripts, CLIs, libraries, services and small apps.

## Brief and acceptance criteria
- [ ] Restate the required behaviour, inputs, outputs, constraints, supported
      runtime and interface (CLI, API, library or UI).
- [ ] Turn the brief into numbered acceptance criteria (`R1...Rn`) with a
      measurable pass/fail test and an evidence command or file for each.
- [ ] List edge cases and error paths before coding: empty, malformed and huge
      input, missing files, network failure, permission denied, cancellation
      and concurrent use.
- [ ] Choose the simplest design that meets the brief; no unrequested
      frameworks, services or dependencies.
- [ ] Record assumptions where the brief is ambiguous and confirm anything that
      would change the interface.

## Interface and implementation
- [ ] Code runs on the stated runtime and has a clear entry point plus working
      `--help` or equivalent usage text.
- [ ] Define the interface contract: flags and arguments, stdin/stdout,
      stderr for diagnostics, exit codes, API request/response schema and file
      formats. State backward-compatibility expectations.
- [ ] Inputs are validated at the boundary; errors are specific, actionable
      and do not leak secrets, credentials or internal paths.
- [ ] Handle failures deliberately: catch specific exceptions, set timeouts and
      bounded retries, release files/sockets/connections, and avoid partial
      writes or inconsistent state.
- [ ] Make repeated runs safe (idempotent where the task implies it) and make
      destructive operations explicit.
- [ ] No hard-coded credentials, personal paths or machine-specific values;
      read configuration and secrets from the environment or a documented
      config file.
- [ ] Limit file and network access to what the task needs; validate paths and
      never pass untrusted input to a shell, SQL string or deserialiser.
- [ ] Public functions and non-obvious logic are documented; comments explain
      why, not what.

## Dependencies and reproducibility
- [ ] Declare and pin dependencies in the project's lockfile; install from a
      clean environment with no undeclared global packages.
- [ ] Record the tested runtime and operating-system assumptions; avoid
      platform-specific behaviour unless the brief requires it.
- [ ] No build artefacts, caches, virtual environments or generated files are
      part of the deliverable.

## Verification
- [ ] Build/install succeeds from a clean checkout using the documented
      commands.
- [ ] Automated tests cover the core behaviour, edge cases and failure paths;
      all pass. Tests use fakes or fixtures for network, time and randomness so
      they are deterministic; no tests are skipped without a recorded reason.
- [ ] At least one end-to-end or integration path exercises the real entry
      point and output format.
- [ ] Lint and format checks pass, and type checking passes where the language
      supports it. Treat new warnings as failures or document why not.
- [ ] Manual smoke test of the main command succeeds and matches the required
      output exactly, including exit codes and stderr behaviour.
- [ ] Run a dependency audit and check the obvious security cases: injection,
      path traversal, unsafe deserialisation and secret exposure.
- [ ] Check runtime and memory on a representative large input; document any
      limit, timeout or retry policy.

## Delivery
- [ ] README or usage text explains install, run, test and configuration,
      including a minimal example and the exact commands used.
- [ ] Sample input and expected output are included where useful; the licence
      is present if the code is redistributed.
- [ ] No secrets, personal data or build artefacts are left in the deliverable;
      `.gitignore` covers generated files.
- [ ] The final answer lists files changed and the exact verification commands
      with their results.

## Review and quality gate
- [ ] Compare at least two independent designs or implementations, and record
      why the chosen one is simpler or more correct.
- [ ] Use a reviewer that did not write the code. It reads the diff, runs the
      documented commands and checks the numbered requirements; where a UI is
      part of the deliverable, it inspects a screenshot with `read_image`. It
      scores the result 0-10 against this checklist.
- [ ] Blocking defects include a failing build, test or lint step; a leaked
      secret or personal path; data loss or corruption; an unhandled crash on a
      documented input; and any mismatch with the required output contract.
- [ ] Fix every blocking defect; revise until the score is at least 8.5 with
      zero blocking defects, or three rounds have been used. If the quality
      loop is disabled, run the same checks yourself and record that
      independence was not possible.
- [ ] When the quality loop is enabled, save `QUALITY_REPORT.md` with the
      numbered checklist, per-round scores, defects found and fixed, evidence
      paths (command output, test log, diff) and the final score; otherwise
      include the same checklist, score and evidence in the final answer. In
      both cases end the answer with `Final quality score: X/10`.

## Lessons learned
<!-- Add only verified, reusable lessons as symptom -> cause -> check to add.
     Keep them short and free of names, emails, tokens and personal paths. -->
