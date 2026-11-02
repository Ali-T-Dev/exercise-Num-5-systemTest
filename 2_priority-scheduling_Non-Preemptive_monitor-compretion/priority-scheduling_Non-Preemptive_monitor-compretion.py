from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import deque
from dataclasses import dataclass, field
from math import lcm
from typing import Dict, List, Optional

POLICIES = ("PRIORITY", "RM", "EDF")
FILE_PREFIX = "monitor"

# Built-in task sets (name, C, T, D, P). P = static priority, a SMALLER number means a HIGHER priority.
DEFAULT_CASES = {
    "A": [("A", 1, 4, 4, 1), ("B", 2, 5, 5, 2), ("C", 3, 10, 10, 3)],
    "B": [("X", 2, 5, 5, 1), ("Y", 4, 7, 7, 2)],
}


# ---------------------------------------------------------------- data model
@dataclass
class Task:
    name: str
    C: int          # execution time
    T: int          # period
    D: int          # relative deadline
    P: int = 0      # static priority used by the PRIORITY policy (smaller number = higher priority)


@dataclass
class Segment:
    """A critical section: `length` CPU units on `resource`, starting after `start` executed units."""
    resource: str
    start: int
    length: int


@dataclass
class Job:
    task: Task
    jid: int
    order: int                      # task position in the input list (tie-breaker)
    release: int
    deadline: int
    remaining: int
    segments: List[Segment]
    executed: int = 0
    seg_idx: int = 0                # index of the current/next critical section
    granted: bool = False           # True while the job owns the resource of its current segment
    state: str = "READY"            # READY / RUNNING / BLOCKED / DONE
    seq: int = 0                    # ready-queue arrival number (tie-breaker)
    req_time: Optional[int] = None  # time of the first request for the current segment
    woken: bool = False             # monitor: signalled but has not re-checked its condition yet
    first_start: Optional[int] = None
    completion: Optional[int] = None
    resource_wait: int = 0
    block_ticks: int = 0
    timeline: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.task.name}{self.jid}"

    def pending_request(self) -> Optional[Segment]:
        """Segment whose lock must be requested right now (None if no request is due)."""
        if self.seg_idx < len(self.segments):
            seg = self.segments[self.seg_idx]
            if self.executed == seg.start and not self.granted:
                return seg
        return None

    def held_segment(self) -> Optional[Segment]:
        return self.segments[self.seg_idx] if self.granted else None


# ----------------------------------------------------- synchronization objects
class Monitor:
    """Monitor with one condition variable `free` and Mesa signalling semantics.

        monitor SharedResource:
            busy = False;  condition free
            procedure acquire():  while busy: wait(free);  busy = True
            procedure release():  busy = False;  signal(free)

    signal() only moves the first waiter to the ready queue. The woken job must re-check `busy`
    when the CPU is given to it again, so a running job may "barge in" first (spurious wake-up)."""
    HANDOFF = False

    def __init__(self, name: str):
        self.name, self.busy, self.owner = name, False, None
        self.cond: deque = deque()
        self.enter_calls = self.waits = self.signals = self.spurious = self.max_queue = 0

    def acquire(self, job: Job) -> bool:
        self.enter_calls += 1
        if not self.busy:
            self.busy, self.owner = True, job
            return True
        if job.woken:                      # was signalled, but the monitor is busy again
            self.spurious += 1
            job.woken = False
        self.waits += 1
        self.cond.append(job)              # wait(free): join the condition queue
        self.max_queue = max(self.max_queue, len(self.cond))
        return False

    def release(self, job: Job) -> Optional[Job]:
        self.busy, self.owner = False, None
        if self.cond:                      # signal(free)
            self.signals += 1
            woken = self.cond.popleft()
            woken.woken = True
            return woken
        return None

    def stats(self) -> dict:
        return {"enter_calls": self.enter_calls, "cond_waits": self.waits, "signals": self.signals,
                "spurious_wakeups": self.spurious, "max_queue": self.max_queue}


# -------------------------------------------------------------- critical sections
RESOURCE_TITLE = "monitor"


def make_resources() -> dict:
    return {"MON": Monitor("MON")}


def build_segments(task: Task, cs_len: int) -> List[Segment]:
    """The final min(cs_len, C) units of the job are executed inside the monitor."""
    length = min(cs_len, task.C)
    return [Segment("MON", task.C - length, length)] if length > 0 else []


# ----------------------------------------------------------------- simulator
class Simulator:
    """Discrete-time (1 tick = 1 time unit) single-CPU simulator for non-preemptive PRIORITY, RM and EDF."""

    def __init__(self, tasks: List[Task], policy: str, cs_len: int = 2,
                 horizon: Optional[int] = None, drain: bool = True):
        if policy not in POLICIES:
            raise ValueError(f"unknown policy {policy!r}; choose from {POLICIES}")
        self.tasks, self.policy, self.cs_len, self.drain = tasks, policy, cs_len, drain
        self.hyperperiod = lcm(*(t.T for t in tasks))
        self.horizon = horizon or self.hyperperiod
        self.resources = make_resources()
        self.jobs: List[Job] = []
        self.ready: List[Job] = []
        self.blocked: List[Job] = []
        self.running: Optional[Job] = None
        self.slices = []                 # (start, end, label, resource held)
        self.counters = {"idle": 0, "preemptions": 0, "lock_holder_preemptions": 0}
        self.jid_count = {t.name: 0 for t in tasks}
        self.seq = 0
        self.end_time = 0

    # -- helpers ----------------------------------------------------------
    def enqueue_ready(self, job: Job) -> None:
        self.seq += 1
        job.seq, job.state = self.seq, "READY"
        self.ready.append(job)

    def priority(self, job: Job):
        if self.policy == "RM":          # shorter period = higher priority
            return (job.task.T, job.release, job.order, job.jid)
        return (job.deadline, job.release, job.order, job.jid)   # EDF

    # -- one tick, phase 1: periodic releases ------------------------------
    def release_jobs(self, now: int) -> None:
        for order, t in enumerate(self.tasks):
            if now % t.T == 0:
                self.jid_count[t.name] += 1
                job = Job(t, self.jid_count[t.name], order, now, now + t.D, t.C,
                          build_segments(t, self.cs_len))
                self.jobs.append(job)
                self.enqueue_ready(job)

    # -- one tick, phase 2: scheduler decision + lock requests -------------
    def pick(self) -> Optional[Job]:
        if self.policy == "PRIORITY":                 # non-preemptive static priority
            if self.running is None and self.ready:
                self.running = min(self.ready, key=lambda j: (j.task.P, j.seq))
                self.ready.remove(self.running)
            return self.running
        candidates = self.ready + ([self.running] if self.running else [])   # RM / EDF: preemptive
        if not candidates:
            return None
        best = min(candidates, key=self.priority)
        if best is not self.running:
            if self.running is not None:              # preemption
                self.counters["preemptions"] += 1
                if self.running.granted:
                    self.counters["lock_holder_preemptions"] += 1
                self.enqueue_ready(self.running)
            self.ready.remove(best)
            self.running = best
        return self.running

    def dispatch(self, now: int) -> None:
        while True:
            job = self.pick()
            if job is None:
                return
            job.state = "RUNNING"
            seg = job.pending_request()
            if seg is None:                           # no lock needed in this tick
                return
            res = self.resources[seg.resource]
            if job.req_time is None:
                job.req_time = now
            if res.acquire(job):                      # P(S) / monitor entry succeeded
                job.granted, job.woken = True, False
                job.resource_wait += now - job.req_time
                job.req_time = None
                return
            job.state = "BLOCKED"                     # lock busy: leave the CPU, pick another job
            self.blocked.append(job)
            self.running = None

    # -- one tick, phase 3: bookkeeping ------------------------------------
    def record_states(self, now: int) -> None:
        for job in self.jobs:
            if job.completion is not None:
                continue
            if job is self.running:
                job.timeline.append("C" if job.granted else "#")
            elif job.state == "BLOCKED":
                job.timeline.append("b")
                job.block_ticks += 1
            else:
                job.timeline.append(".")

    # -- one tick, phase 4: execute one time unit --------------------------
    def execute(self, now: int) -> None:
        job = self.running
        if job is None:
            self.slices.append((now, now + 1, "IDLE", ""))
            self.counters["idle"] += 1
            return
        if job.first_start is None:
            job.first_start = now
        seg = job.held_segment()
        self.slices.append((now, now + 1, job.label, seg.resource if seg else ""))
        job.remaining -= 1
        job.executed += 1
        if seg and job.executed == seg.start + seg.length:       # leave the critical section
            woken = self.resources[seg.resource].release(job)     # V(S) / release + signal
            job.granted = False
            job.seg_idx += 1
            if woken is not None:
                self.blocked.remove(woken)
                self.enqueue_ready(woken)
                if self.resources[seg.resource].HANDOFF:          # semaphore: ownership passed on
                    woken.granted = True
                    woken.resource_wait += now + 1 - woken.req_time
                    woken.req_time = None
        if job.remaining == 0:
            job.completion, job.state = now + 1, "DONE"
            self.running = None

    def run(self) -> "Simulator":
        now = 0
        limit = self.horizon * 10 + max(t.D for t in self.tasks)
        while now < limit:
            all_done = all(j.completion is not None for j in self.jobs)
            if now >= self.horizon and (not self.drain or all_done):
                break
            if now < self.horizon:
                self.release_jobs(now)
            self.dispatch(now)
            self.record_states(now)
            self.execute(now)
            now += 1
        self.end_time = now
        return self

    # -- results -------------------------------------------------------------
    def job_rows(self) -> List[dict]:
        rows = []
        for j in self.jobs:
            done = j.completion is not None
            turnaround = j.completion - j.release if done else None
            missed = (j.completion > j.deadline) if done else (self.end_time >= j.deadline)
            rows.append({
                "policy": self.policy, "job": j.label, "task": j.task.name,
                "release": j.release, "deadline": j.deadline, "C": j.task.C,
                "first_start": j.first_start, "completion": j.completion,
                "waiting_time": turnaround - j.task.C if done else None,
                "turnaround_time": turnaround,
                "response_time": j.first_start - j.release if j.first_start is not None else None,
                "resource_waiting_time": j.resource_wait, "block_time": j.block_ticks,
                "deadline_missed": missed})
        return rows

    def summary(self) -> dict:
        def avg(xs):
            return round(sum(xs) / len(xs), 3) if xs else None
        done = [j for j in self.jobs if j.completion is not None]
        rows = self.job_rows()
        s = {"policy": self.policy, "hyperperiod": self.hyperperiod, "end_time": self.end_time,
             "jobs_released": len(self.jobs), "jobs_completed": len(done),
             "avg_waiting_time": avg([j.completion - j.release - j.task.C for j in done]),
             "avg_turnaround_time": avg([j.completion - j.release for j in done]),
             "avg_response_time": avg([j.first_start - j.release for j in done]),
             "avg_resource_waiting_time": avg([j.resource_wait for j in done]),
             "avg_block_time": avg([j.block_ticks for j in done]),
             "deadline_misses": sum(1 for r in rows if r["deadline_missed"]),
             "idle_time": self.counters["idle"],
             "preemptions": self.counters["preemptions"],
             "lock_holder_preemptions": self.counters["lock_holder_preemptions"]}
        for rname, res in self.resources.items():
            for k, v in res.stats().items():
                s[f"{rname}_{k}"] = v
        return s

    def gantt(self) -> List[tuple]:
        merged = []
        for a, b, label, res in self.slices:
            if merged and merged[-1][2] == label and merged[-1][3] == res and merged[-1][1] == a:
                merged[-1] = (merged[-1][0], b, label, res)
            else:
                merged.append((a, b, label, res))
        return merged


# ------------------------------------------------------------------ reporting
def ascii_gantt(sim: Simulator) -> str:
    """One line per job: # running, C running inside a critical section, b blocked, . ready, _ idle CPU."""
    end = sim.end_time
    lines = [f"  {sim.policy}   (# run, C run in critical section, b blocked, . ready, _ idle)",
             "  " + "time  " + "".join(str(t % 10) for t in range(end))]
    for j in sim.jobs:
        row = [" "] * end
        for i, ch in enumerate(j.timeline):
            row[j.release + i] = ch
        lines.append(f"  {j.label:<6}" + "".join(row))
    idle = [" "] * end
    for a, b, label, _ in sim.slices:
        if label == "IDLE":
            for t in range(a, b):
                idle[t] = "_"
    lines.append(f"  {'CPU':<6}" + "".join(idle))
    return "\n".join(lines)


def plot_case(case: str, sims: List[Simulator], outdir: str) -> Optional[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except ImportError:
        print("  (matplotlib not installed - PNG Gantt chart skipped; text Gantt is shown instead)")
        return None
    tasks = sims[0].tasks
    palette = plt.get_cmap("tab10")
    color = {t.name: palette(i % 10) for i, t in enumerate(tasks)}
    rows_per = [len(s.jobs) + 1 for s in sims]
    fig, axes = plt.subplots(len(sims), 1, figsize=(13, 0.27 * sum(rows_per) + 1.7 * len(sims)),
                             gridspec_kw={"height_ratios": rows_per}, squeeze=False)
    for ax, sim in zip(axes[:, 0], sims):
        order = [j.label for j in sim.jobs]
        ypos = {lab: len(order) - i for i, lab in enumerate(order)}
        ypos["IDLE"] = 0
        for j in sim.jobs:
            y = ypos[j.label]
            for i, ch in enumerate(j.timeline):
                if ch == "b":
                    ax.broken_barh([(j.release + i, 1)], (y - 0.35, 0.7), facecolors="#f6b8b8",
                                   edgecolor="#c0504d", linewidth=0.5)
            ax.plot([j.deadline], [y], marker="v", color="red" if j.completion is None or
                    j.completion > j.deadline else "black", markersize=5)
        for a, b, label, res in sim.slices:
            y = ypos[label]
            if label == "IDLE":
                ax.broken_barh([(a, b - a)], (y - 0.35, 0.7), facecolors="#d9d9d9", edgecolor="grey",
                               linewidth=0.5)
                continue
            tname = label.rstrip("0123456789")
            ax.broken_barh([(a, b - a)], (y - 0.35, 0.7), facecolors=color[tname],
                           edgecolor="black", linewidth=1.2 if res else 0.4, hatch="///" if res else None)
        ax.set_yticks(list(ypos.values()))
        ax.set_yticklabels(list(ypos.keys()), fontsize=8)
        ax.set_xlim(-0.2, sim.end_time + 0.4)
        ax.set_xticks(range(0, sim.end_time + 1, 1 if sim.end_time <= 40 else 5))
        ax.tick_params(axis="x", labelsize=8)
        ax.grid(axis="x", color="#e5e5e5")
        s = sim.summary()
        ax.set_title(f"{sim.policy}  -  deadline misses: {s['deadline_misses']},  "
                     f"avg block time: {s['avg_block_time']}", loc="left", fontsize=10)
    handles = [Patch(facecolor=color[t.name], edgecolor="black", label=f"task {t.name}") for t in tasks]
    handles += [Patch(facecolor="white", edgecolor="black", hatch="///", label="holding monitor"),
                Patch(facecolor="#f6b8b8", edgecolor="#c0504d", label="blocked"),
                Patch(facecolor="#d9d9d9", edgecolor="grey", label="CPU idle")]
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), fontsize=8, frameon=False)
    fig.suptitle(f"Case {case}: non-preemptive PRIORITY vs RM vs EDF with a monitor (v = deadline, red = missed)",
                 fontsize=12)
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    path = os.path.join(outdir, f"gantt_{case}.png")
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def write_csv(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    fields = list(rows[0].keys())
    for r in rows:                      # make sure every column that appears is declared
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- input parsing
def make_task_list(raw) -> List[Task]:
    tasks = []
    for item in raw:
        name, C, T = str(item[0]).strip(), int(item[1]), int(item[2])
        D = int(item[3]) if len(item) > 3 and item[3] not in (None, "") else T
        P = int(item[4]) if len(item) > 4 and item[4] not in (None, "") else len(tasks) + 1
        if C <= 0 or T <= 0 or D <= 0:
            raise ValueError(f"task {name}: C, T and D must be positive integers")
        if C > D:
            print(f"warning: task {name} has C > D and can never meet its deadline")
        tasks.append(Task(name, C, T, D, P))
    if not tasks:
        raise ValueError("at least one task is required")
    if len({t.name for t in tasks}) != len(tasks):
        raise ValueError("task names must be unique")
    return tasks


def parse_task_string(text: str) -> List[Task]:
    """'A,1,4,4,1;B,2,5,5,2' -> tasks (name,C,T[,D[,P]]). D defaults to T, P defaults to the task position (1,2,3...)."""
    return make_task_list([[p.strip() for p in chunk.split(",")] for chunk in text.split(";") if chunk.strip()])


def load_task_file(path: str, default_case: str) -> Dict[str, List[Task]]:
    cases: Dict[str, List[Task]] = {}
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            data = {default_case: data}
        for case, items in data.items():
            cases[case] = make_task_list([[i["name"], i["C"], i["T"], i.get("D"), i.get("P")] for i in items])
    else:                                              # CSV with header: [case,]name,C,T[,D]
        grouped: Dict[str, list] = {}
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                grouped.setdefault(r.get("case") or default_case, []).append(
                    [r["name"], r["C"], r["T"], r.get("D"), r.get("P")])
        cases = {c: make_task_list(v) for c, v in grouped.items()}
    return cases


def interactive_input():
    print("Interactive mode - enter one task per line as:  name C T [D [P]]   (P: smaller = higher priority)   (empty line to finish)")
    raw = []
    while True:
        line = input(f"task {len(raw) + 1}> ").strip()
        if not line:
            break
        raw.append(line.replace(",", " ").split())
    case = input("case name [custom]> ").strip() or "custom"
    pol = input("policies [PRIORITY,RM,EDF]> ").strip() or "PRIORITY,RM,EDF"
    cs = input("critical-section length [2]> ").strip() or "2"
    return {case: make_task_list(raw)}, [p.strip().upper() for p in pol.split(",")], int(cs)


# ----------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
    description="Non-Preemptive Priority Scheduling with Monitor compared with RM and EDF",
    formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--tasks", help='task set "name,C,T[,D[,P]];..."  (P: priority, smaller number = higher priority)')
    ap.add_argument("--case-name", default="custom", help="label for the task set given with --tasks")
    ap.add_argument("--tasks-file", help="JSON or CSV file with one or more task sets")
    ap.add_argument("--interactive", action="store_true", help="type the task set on the keyboard")
    ap.add_argument("--policies", default="PRIORITY,RM,EDF", help="comma list from PRIORITY,RM,EDF (default: all)")
    ap.add_argument("--cs-len", type=int, default=2, help="critical-section length in time units (default 2)")
    ap.add_argument("--horizon", type=int, help="release jobs until this time (default: hyperperiod)")
    ap.add_argument("--no-drain", action="store_true", help="stop exactly at the horizon (unfinished jobs stay unfinished)")
    ap.add_argument("--outdir", default="results", help="output directory (default: results)")
    ap.add_argument("--no-plots", action="store_true", help="do not draw PNG Gantt charts")
    ap.add_argument("--quiet", action="store_true", help="do not print the text Gantt charts")
    args = ap.parse_args(argv)

    try:
        policies = [p.strip().upper() for p in args.policies.split(",") if p.strip()]
        cs_len = args.cs_len
        if args.interactive:
            cases, policies, cs_len = interactive_input()
        elif args.tasks:
            cases = {args.case_name: parse_task_string(args.tasks)}
        elif args.tasks_file:
            cases = load_task_file(args.tasks_file, args.case_name)
        else:
            cases = {c: make_task_list(v) for c, v in DEFAULT_CASES.items()}
        if cs_len < 0:
            raise ValueError("--cs-len must be >= 0")
        for p in policies:
            if p not in POLICIES:
                raise ValueError(f"unknown policy {p!r}; choose from {POLICIES}")
    except (ValueError, KeyError, OSError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2

    os.makedirs(args.outdir, exist_ok=True)
    summaries, details, gantts = [], [], []
    for case, tasks in cases.items():
        util = sum(t.C / t.T for t in tasks)
        print(f"\n=== Case {case}: " + ", ".join(f"{t.name}(C={t.C},T={t.T},D={t.D},P={t.P})" for t in tasks)
              + f"  | utilization = {util:.4f}")
        sims = []
        for policy in policies:
            sim = Simulator(tasks, policy, cs_len, args.horizon, drain=not args.no_drain).run()
            sims.append(sim)
            s = sim.summary()
            s["case"] = case
            summaries.append(s)
            for r in sim.job_rows():
                r["case"] = case
                details.append(r)
            for a, b, label, res in sim.gantt():
                gantts.append({"case": case, "policy": policy, "start": a, "end": b, "slot": label,
                               "resource_held": res})
            if not args.quiet:
                print(ascii_gantt(sim))
        if not args.no_plots:
            path = plot_case(case, sims, args.outdir)
            if path:
                print(f"  Gantt chart saved: {path}")

    write_csv(os.path.join(args.outdir, f"{FILE_PREFIX}_summary.csv"), summaries)
    write_csv(os.path.join(args.outdir, f"{FILE_PREFIX}_job_details.csv"), details)
    write_csv(os.path.join(args.outdir, f"{FILE_PREFIX}_gantt.csv"), gantts)

    print("\ncase,policy,avg_wait,avg_turnaround,avg_response,avg_resource_wait,avg_block,misses,idle,preempt,lock_preempt")
    for s in summaries:
        print(",".join(str(x) for x in (s["case"], s["policy"], s["avg_waiting_time"], s["avg_turnaround_time"],
              s["avg_response_time"], s["avg_resource_waiting_time"], s["avg_block_time"], s["deadline_misses"],
              s["idle_time"], s["preemptions"], s["lock_holder_preemptions"])))
    print(f"\nCSV files written to: {os.path.abspath(args.outdir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
