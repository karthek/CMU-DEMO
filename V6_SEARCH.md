# V6: parent-preserving, distinct-candidate Beam Search

The `v5` tag and `V5_ORCHESTRATION.md` retain historical behavior. V6 changes only
search selection, not Critic scoring, agents, transport mapping, or public contracts.

## Algorithm and tie-breaking

V5 ranked initial candidates, then replaced each beam with its children. Parents
were not eligible, and duplicate times could occupy multiple slots.

V6 deduplicates and scores the initial pool. Each subsequent round starts with
the current beam, appends earlier/later children, deduplicates by parsed leave
timestamp, scores distinct candidates, and keeps up to beam width.

The first encountered candidate represents a timestamp. Initial input order is
preserved; later pools contain parents in beam order, followed by children in
beam order, earlier before later. Stable descending-score sorting preserves this
order for equal scores. Thus existing parents win duplicate paths, and equal-score
distinct candidates retain encounter order. Histories are real, never merged.
Equivalent ISO spellings such as `16:15` and `16:15:00` deduplicate together.
For identical ordered inputs, repeated runs return identical results.

The existing Critic depends on context and leave time, so duplicate timestamps
have identical scores. With this deterministic Critic, the best score cannot fall
merely because refinement proceeds: every current parent is still eligible.
This is not a guarantee of a global optimum; width and refinement range still
limit exploration.

Width 2 and depth 3 remain configured. Depth counts initial ranking plus two
refinement rounds. Non-positive, non-integer, boolean, or missing width/depth
values raise `ValueError`; an empty starting pool raises `ValueError`. These are
planner argument errors, not agent failures, and use the existing validation
exception convention. No fallback is added. Fewer than width distinct candidates
may return fewer finalists rather than padding with duplicates.

## Verified deterministic trace

Times below are September 11, 2026. Scores come from the unchanged Critic.

| Initial candidate | Time | Score | Decision |
|---|---|---:|---|
| Conservative | 15:45 | 74.00 | Keep |
| Balanced | 16:15 | 76.40 | Keep |
| Efficient | 16:45 | 28.80 | Prune |
| Aggressive | 17:15 | 0.00 | Prune |

| Round 1 pool, in encounter order | Time | Score | Decision |
|---|---|---:|---|
| Balanced parent | 16:15 | 76.40 | Keep, second |
| Conservative parent | 15:45 | 74.00 | Prune |
| Balanced / earlier | 16:05 | 75.60 | Prune |
| Balanced / later | 16:25 | 77.20 | Keep, first |
| Conservative / earlier | 15:35 | 73.20 | Prune |
| Conservative / later | 15:55 | 74.80 | Prune |

There are no duplicates in the initial pool or round 1.

| Round 2 pool, in encounter order | Time | Score | Decision |
|---|---|---:|---|
| Balanced / later parent | 16:25 | 77.20 | Keep, first |
| Balanced parent | 16:15 | 76.40 | Keep, second |
| Balanced / later / earlier | 16:15 | 76.40 | Remove duplicate; retain parent |
| Balanced / later / later | 16:35 | 28.00 | Prune |
| Balanced / earlier | 16:05 | 75.60 | Prune |
| Balanced / later | 16:25 | 77.20 | Remove duplicate; retain parent |

Duplicate scores are shown for explanation; search scores each distinct time
once per round, after deduplication.

## Why the result changed

The tagged V5 search was executed against the same context and existing Critic.
It discovered **16:25 / 77.20** in round 1 but then only considered children:
16:15 / 76.40 (twice), 15:55 / 74.80, and 16:35 / 28.00. The 16:25 parent was
absent, so two 16:15 paths won. V6 keeps the 16:25 parent eligible.

The existing 16:25 score is independently verified: gate readiness is 17:45,
45 minutes before boarding. Airport penalty is zero, early-leaving penalty is
(155 - 120) * 0.08 = 2.80, and the high-priority-meeting penalty is 20.
Therefore 100 - 2.80 - 20 = **77.20**. No scoring rule changed.

| Mode/version | First finalist | Second finalist |
|---|---|---|
| Deterministic V5 | 16:15 / 76.40 | 16:15 / 76.40 |
| Deterministic V6 | 16:25 / 77.20 | 16:15 / 76.40 |
| Host-assisted V5 | 16:20 / 76.80 | 16:10 / 76.00 |
| Host-assisted V6 | 16:20 / 76.80 | 16:10 / 76.00 |

V6 deterministic histories are `Balanced -> later` and the original `Balanced`.
V6 host histories are original `Host buffer` and `Host buffer -> earlier`.
Host times/scores remain unchanged, but parent preservation removes unnecessary
out-and-back histories. Host-provided scores continue to be ignored.

## Verification

`tests/test_beam_search.py` adds focused tests for parent preservation, child
improvement, deduplication, ISO equivalence, width/depth, deterministic ties,
real history selection, repeatability, input immutability, invalid arguments,
and the unchanged Critic's score for the verified winner. Existing regression
expectations were updated only after tracing both algorithms. MCP stdio tests
still launch the actual server and check discovery, both modes, forged scores,
sanitized malformed calls, and recovery.

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -B app.py
.\.venv\Scripts\python.exe -B host_assisted_demo.py
```
