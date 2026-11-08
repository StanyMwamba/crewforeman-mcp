# Run Status Vocabulary (v3.11.0)

`status.py` defines one flat vocabulary for terminal run status. A status is
always a lowercase string; there is no enum and no `None`.

| Status | Meaning |
| --- | --- |
| `ok` | Builder succeeded and review accepted at/above `target` with no blocking defects. |
| `ok_rescued` | Builder phase failed but the run was marked rescued and the review gate passed. |
| `ok_unreviewed` | Builder output exists, but the independent review itself errored (`review_status == "error"`); a disabled/unknown review yields `ok`, and a stopped review yields `ok_below_gate`. |
| `ok_below_gate` | Review ran but rejected, scored below `target`, or found blocking defects. |
| `error` | Builder failed and no rescued output passed review. |
| `no_balance` | The provider account has no credit; top up and retry. |
| `cancelled` | Cancelled by the operator or request. |
| `deadline_exceeded` | Stopped because the wall-clock deadline was reached. |
| `budget_exceeded` | Stopped because the cost budget was exhausted. |
| `gate_failed` | The deterministic gate (F1) failed after its revision cap; the paid reviewer was never called. `review_status` is `skipped` and `quality_score` is `None`. |

`SUCCESS_STATUSES` contains `ok`, `ok_rescued`, `ok_unreviewed`,
`ok_below_gate` (the run produced usable output, even if not fully gated).
`FAILURE_STATUSES` contains `error`, `no_balance`, `cancelled`,
`deadline_exceeded`, `budget_exceeded`, `gate_failed`.

## Mapping and precedence

`final_status(builder_status, review_status, score, blocking_defects,
target=8.5, rescued=False, cancelled=False, stop_reason=None)` returns
`(status, detail, review_passed)`. Precedence, highest first:

1. `cancelled=True` or a cancellation stop reason -> `cancelled`
2. deadline stop reason -> `deadline_exceeded`
3. budget stop reason -> `budget_exceeded`
4. builder status `no_balance` -> `no_balance`
5. builder status other than `ok`/`ok_rescued` -> `error`
6. `gate_status="gate_failed"` (gate exhausted its revision cap) -> `gate_failed`
7. review status `error` -> `ok_unreviewed`
8. a decisive review that did not pass -> `ok_below_gate`
9. rescued output -> `ok_rescued`
10. otherwise -> `ok`

A gate *crash* is not `gate_failed`: it stays `error` with
`stop_reason="gate_error"` because the gate could not produce a verdict.

`review_passed` is `True` only for an accepted review at/above the gate with
zero blocking defects, `False` for a stopped/failed review, and `None` when no
independent review ran or when the run stopped before the review gate
(deadline, budget, cancellation or builder failure). Review status strings are
compared literally
(`accepted`, `stopped`, `error`, `disabled`); an unknown string is treated like
a disabled review, so a successful builder still yields `ok`. A missing score
never counts as passing the gate.

## Backward compatibility

The v3.8.0 report payload keeps the fields existing consumers rely on:

* `status` remains a plain string and is always one of `STATUSES`.
* `quality_score` is still emitted exactly as before (number or `None`).
* `review_status` is still emitted as the raw reviewer string.
* `detail` (human-readable explanation) and `review_passed` (boolean) are
  additive fields. Consumers must not parse `detail`; it is informational.

Dashboards should compare against the documented strings or
`SUCCESS_STATUSES`, and should treat an unrecognised future status as
non-successful while still rendering the string. This guarantees that adding a
status in a later release does not break older readers.

## Pipeline install detail

A pipelined job keeps its own worktree, patch and reviewer. The result header
adds `| install: conflict (…)` when the combined `git apply --check` refuses
the queue: the canonical tree is left untouched and the job is reported as
`error` with `stop_reason="install_conflict"`. `install: manual` means the
patch is retained for an operator. A normal (non-pipelined) job keeps
`install_status="n/a"`. `deepseek_job_status` mirrors `write_set`,
`pipeline_slot`, `base_commit` and `install_status`, so a conflict can be
diagnosed without reading patch files.
