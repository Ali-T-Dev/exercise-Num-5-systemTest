# Algorithm 2 - Non-Preemptive Priority Scheduling with a Monitor

Non-preemptive priority scheduling with a **monitor**, compared with preemptive RM and EDF.

Exercise 5 - Erfan Barati (student ID 1403176012), Department of Computer Engineering, Eshragh Institute of Higher Education, Bojnord, Iran.
Source repository: https://github.com/THE-ERPHUN/fifo-monitor-rm-edf-comparison

## Contents of this folder

| File | Description |
|---|---|
| `priority-scheduling_Non-Preemptive_monitor-compretion.py` | Python simulator (non-preemptive PRIORITY, RM, EDF). |
| `README.md` | This file. |
| `priority-scheduling_Non-Preemptive_monitor-compretion.docx` | Research report in editable Word format: flowchart, pseudocode, Gantt charts, results, full source code. |

## Scheduling algorithms

- **PRIORITY (non-preemptive priority scheduling)** - every task has a static priority `P` (smaller number = higher priority). When the CPU is free the ready job with the best `P` runs (ties: arrival order) until it finishes or blocks on a lock; it is never preempted.
- **RM (Rate Monotonic)** - fixed priority, shorter period = higher priority, preemptive.
- **EDF (Earliest Deadline First)** - dynamic priority, earliest absolute deadline first, preemptive.

## Synchronization model

The shared resource is encapsulated in a Mesa-style monitor (`busy` flag + condition variable `free`). `acquire()` does `while busy: wait(free)`; `release()` does `busy = False; signal(free)`. A signalled job only becomes READY and re-checks `busy` when it is dispatched again.

Common rules: one CPU, integer time, periodic tasks `(C, T, D)` released synchronously at t = 0; a job that cannot get its lock leaves the CPU (state BLOCKED) and another ready job is dispatched; a lock holder keeps its lock when preempted; no priority inheritance or ceiling is implemented.

## Requirements

Python 3.9 or newer. `matplotlib` is optional (only for the PNG Gantt charts): `pip install matplotlib`. Without it the program still prints a text Gantt chart and writes the CSV files.

## How to run

Open a terminal **inside this folder** and run:

```bash
python priority-scheduling_Non-Preemptive_monitor-compretion.py
```

This runs the two built-in task sets (Case A and Case B) with PRIORITY, RM and EDF.

### Inputs

| Option | Meaning | Example |
|---|---|---|
| *(none)* | built-in Case A `A=(C1,T4,D4,P1) B=(2,5,5,P2) C=(3,10,10,P3)` and Case B `X=(2,5,5,P1) Y=(4,7,7,P2)` | `python priority-scheduling_Non-Preemptive_monitor-compretion.py` |
| `--tasks` | task set `name,C,T[,D[,P]]` separated by `;` - D defaults to T, P = priority (smaller = higher) defaults to 1,2,3... by position | `--tasks "A,1,4,4,1;B,2,5,5,2;C,3,10,10,3"` |
| `--case-name` | label of the task set given with `--tasks` | `--case-name MyCase` |
| `--tasks-file` | JSON or CSV file with one or more task sets | `--tasks-file mytasks.json` |
| `--interactive` | type tasks, policies and critical-section length on the keyboard | `--interactive` |
| `--policies` | subset of `PRIORITY,RM,EDF` | `--policies PRIORITY,EDF` |
| `--cs-len` | critical-section length in time units (default 2) | `--cs-len 1` |
| `--horizon` | release jobs until this time (default: hyperperiod) | `--horizon 40` |
| `--no-drain` | stop exactly at the horizon (unfinished jobs stay unfinished) | `--no-drain` |
| `--outdir` | output directory (default `results`) | `--outdir out` |
| `--no-plots`, `--quiet` | skip PNG charts / skip text Gantt charts | `--no-plots --quiet` |

JSON file example (`mytasks.json`):

```json
{"MyCase": [{"name": "A", "C": 1, "T": 4, "D": 4, "P": 1}, {"name": "B", "C": 2, "T": 5, "P": 2}]}
```

CSV file example: header `name,C,T,D,P` (optionally a `case` column first). Names must be unique; C, T, D must be positive integers; P is an integer.

### Outputs

On the console: for every case and policy a text Gantt chart (`#` running, `C` running inside a critical section, `b` blocked, `.` ready, `_` idle CPU) and a final summary table. In the output directory (`results/` by default):

| File | Content |
|---|---|
| `monitor_summary.csv` | one row per case and policy: average waiting / turnaround / response / resource-wait / block time, deadline misses, idle time, preemptions, lock statistics |
| `monitor_job_details.csv` | one row per job: release, deadline, first start, completion, waiting, turnaround, response, resource wait, block time, deadline-miss flag |
| `monitor_gantt.csv` | merged Gantt intervals with the resource held in each slice |
| `gantt_<case>.png` | Gantt chart of all policies of a case (one row per job, hatching = lock held, pink = blocked, grey = idle, `v` = deadline) |

Example with your own task set:

```bash
python priority-scheduling_Non-Preemptive_monitor-compretion.py --tasks "P1,2,6,6,3;P2,3,9,9,2;P3,1,4,4,1" --case-name Mine --outdir my_results
```

## Pseudocode

```text
MONITOR SharedResource
    busy <- FALSE;   CONDITION free
    PROCEDURE acquire()                          PROCEDURE release()
        WHILE busy DO wait(free)                     busy <- FALSE
        busy <- TRUE                                 signal(free)      // Mesa: waiter only becomes READY

ALGORITHM Simulate(tasks, policy, cs_len, horizon)
 1  H <- LCM(T1, ..., Tn);  horizon <- H if not given;  now <- 0
 2  M <- new SharedResource;  running <- NIL;  ready <- empty;  blocked <- empty
 3  WHILE now < horizon OR some released job is unfinished DO
 4      IF now < horizon THEN FOR EACH task t WITH now MOD t.T = 0 DO
 5          create job (release = now, deadline = now + t.D);  ready.enqueue(job)
 6      LOOP                                   // dispatch phase
 7          job <- SELECT(policy)
 8          IF job = NIL THEN BREAK
 9          IF job is at the start of its monitor section AND not inside the monitor THEN
10              IF M.busy = FALSE THEN M.busy <- TRUE;  BREAK     // entered the monitor
11              ELSE wait(free): job joins M.free queue;  job.state <- BLOCKED;  running <- NIL
12          ELSE BREAK
13      FOR EACH unfinished job j: record state (run / run in monitor / blocked / ready)
14      IF running = NIL THEN log IDLE ELSE
15          execute one unit;  log Gantt slice
16          IF last unit of the monitor section THEN M.release()   // busy <- FALSE; signal(free)
17          IF running.remaining = 0 THEN completion <- now + 1;  running <- NIL
18      now <- now + 1
19  compute metrics (waiting, turnaround, response, resource-wait, block, misses);  write CSV and Gantt

FUNCTION SELECT(policy)                         // identical to the semaphore version
    PRIORITY: best static priority P if CPU free (no preemption);  RM: shortest period;  EDF: earliest deadline
    RM and EDF may preempt the running job; a preempted job keeps the monitor if it is inside it

NOTE (Mesa semantics): a job taken from the condition queue is READY, not the owner.
      When it is dispatched it executes acquire() again; if busy = TRUE it waits again (spurious wake-up).
```

## Results with the built-in task sets

Averages over completed jobs (time units), produced by running the program with default options:

| Case | Policy | Avg wait | Avg turnaround | Avg resource wait | Avg block | Misses | Idle |
|---|---|---|---|---|---|---|---|
| A | PRIORITY | 1.273 | 3.000 | 0.000 | 0.000 | 0 | 1 |
| A | RM | 1.182 | 2.909 | 0.273 | 0.182 | 0 | 1 |
| A | EDF | 1.273 | 3.000 | 0.000 | 0.000 | 0 | 1 |
| B | PRIORITY | 1.083 | 3.917 | 0.000 | 0.000 | 0 | 1 |
| B | RM | 1.000 | 3.833 | 0.167 | 0.167 | 0 | 1 |
| B | EDF | 1.000 | 3.833 | 0.000 | 0.000 | 0 | 1 |

See the Word report for the flowchart, Gantt charts, preemption / lock statistics and discussion.

## Metrics

- Turnaround = completion - release; waiting = turnaround - C; response = first dispatch - release.
- Resource waiting time = first lock request until the lock is actually obtained.
- Block time = ticks spent in the BLOCKED state.
- Deadline misses, idle time, preemptions and lock-holder preemptions are also reported.

## Limitations

The results are specific to the two task sets, the critical-section placement (end of each job), its length, and the lock semantics implemented here. They are not a universal ranking of PRIORITY, RM and EDF. No context-switch cost, priority inheritance/ceiling, multi-core, I/O or stochastic execution times are modelled. A randomized self-test (10,800 simulations) confirmed mutual exclusion, work conservation and job completion, but the model remains an educational simplification.
