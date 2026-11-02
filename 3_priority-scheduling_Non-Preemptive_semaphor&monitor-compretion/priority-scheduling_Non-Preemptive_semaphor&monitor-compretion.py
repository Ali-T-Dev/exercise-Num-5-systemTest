
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
FILE_PREFIX = "semaphore_monitor"

# Task format: (name, C, T, D, P)
# Smaller P means higher static priority.
DEFAULT_CASES = {
    "A": [
        ("A", 1, 4, 4, 1),
        ("B", 2, 5, 5, 2),
        ("C", 3, 10, 10, 3),
    ],
    "B": [
        ("X", 2, 5, 5, 1),
        ("Y", 4, 7, 7, 2),
    ],
}


# ---------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------

@dataclass
class Task:
    name: str
    C: int
    T: int
    D: int
    P: int = 0


@dataclass
class Segment:
    """A critical section that uses a specific synchronization resource."""

    resource: str
    start: int
    length: int


@dataclass
class Job:
    task: Task
    jid: int
    order: int
    release: int
    deadline: int
    remaining: int
    segments: List[Segment]

    executed: int = 0
    seg_idx: int = 0
    granted: bool = False

    state: str = "READY"
    seq: int = 0

    req_time: Optional[int] = None
    woken: bool = False

    first_start: Optional[int] = None
    completion: Optional[int] = None

    resource_wait: int = 0
    block_ticks: int = 0

    timeline: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.task.name}{self.jid}"

    def pending_request(self) -> Optional[Segment]:
        """Return the next segment if a resource request is due."""

        if self.seg_idx >= len(self.segments):
            return None

        segment = self.segments[self.seg_idx]

        if self.executed == segment.start and not self.granted:
            return segment

        return None

    def held_segment(self) -> Optional[Segment]:
        """Return the segment whose resource is currently owned."""

        if self.granted and self.seg_idx < len(self.segments):
            return self.segments[self.seg_idx]

        return None


# ---------------------------------------------------------------------
# Synchronization objects
# ---------------------------------------------------------------------

class BinarySemaphore:
    """
    Binary semaphore with FIFO waiting queue.

    P(S): acquire the semaphore.
    V(S): release it and directly hand ownership to the first waiter.
    """

    HANDOFF = True

    def __init__(self, name: str):
        self.name = name
        self.value = 1
        self.owner: Optional[Job] = None

        self.queue: deque = deque()

        self.p_calls = 0
        self.blocked_p = 0
        self.max_queue = 0

    def acquire(self, job: Job) -> bool:
        """P(S): acquire the semaphore or wait in the FIFO queue."""

        self.p_calls += 1

        if self.value > 0:
            self.value = 0
            self.owner = job
            return True

        self.blocked_p += 1
        self.queue.append(job)

        self.max_queue = max(self.max_queue, len(self.queue))

        return False

    def release(self, job: Job) -> Optional[Job]:
        """V(S): release the semaphore and hand it to the first waiter."""

        if self.value != 0 or self.owner is not job:
            raise RuntimeError(
                f"{job.label} attempted to release {self.name} "
                "without owning it."
            )

        if self.queue:
            self.owner = self.queue.popleft()
            return self.owner

        self.value = 1
        self.owner = None

        return None

    def stats(self) -> dict:
        return {
            "P_calls": self.p_calls,
            "P_blocked": self.blocked_p,
            "max_queue": self.max_queue,
        }


class Monitor:
    """
    Monitor with one condition variable and Mesa signalling semantics.

    acquire():
        while busy:
            wait(free)
        busy = True

    release():
        busy = False
        signal(free)

    A signalled job becomes READY. It must re-check the busy condition
    when it gets CPU time again.
    """

    HANDOFF = False

    def __init__(self, name: str):
        self.name = name

        self.busy = False
        self.owner: Optional[Job] = None

        self.cond: deque = deque()

        self.enter_calls = 0
        self.waits = 0
        self.signals = 0
        self.spurious = 0
        self.max_queue = 0

    def acquire(self, job: Job) -> bool:
        """Enter the monitor or wait on its condition variable."""

        self.enter_calls += 1

        if not self.busy:
            self.busy = True
            self.owner = job
            return True

        if job.woken:
            # The job was signalled, but another job entered first.
            # It must wait again, following Mesa semantics.
            self.spurious += 1
            job.woken = False

        self.waits += 1
        self.cond.append(job)

        self.max_queue = max(self.max_queue, len(self.cond))

        return False

    def release(self, job: Job) -> Optional[Job]:
        """Release the monitor and signal the first waiting job."""

        if not self.busy or self.owner is not job:
            raise RuntimeError(
                f"{job.label} attempted to release {self.name} "
                "without owning it."
            )

        self.busy = False
        self.owner = None

        if self.cond:
            self.signals += 1

            woken = self.cond.popleft()
            woken.woken = True

            return woken

        return None

    def stats(self) -> dict:
        return {
            "enter_calls": self.enter_calls,
            "cond_waits": self.waits,
            "signals": self.signals,
            "spurious_wakeups": self.spurious,
            "max_queue": self.max_queue,
        }


# ---------------------------------------------------------------------
# Critical sections and resources
# ---------------------------------------------------------------------

RESOURCE_TITLE = "Monitor + Binary Semaphore"


def make_resources() -> dict:
    return {
        "MON": Monitor("MON"),
        "SEM": BinarySemaphore("SEM"),
    }


def build_segments(task: Task, cs_len: int) -> List[Segment]:
    """
    Split the final critical-section units into two sequential parts.

    First part: Monitor
    Second part: Binary Semaphore
    """

    length = min(cs_len, task.C)

    in_monitor = (length + 1) // 2
    in_semaphore = length - in_monitor

    first = task.C - length

    segments = []

    if in_monitor > 0:
        segments.append(
            Segment(
                resource="MON",
                start=first,
                length=in_monitor,
            )
        )

    if in_semaphore > 0:
        segments.append(
            Segment(
                resource="SEM",
                start=first + in_monitor,
                length=in_semaphore,
            )
        )

    return segments


# ---------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------

class Simulator:
    """
    Discrete-time, single-CPU scheduling simulator.

    Policies:
        1. Non-Preemptive Priority Scheduling
        2. Rate Monotonic (RM)
        3. Earliest Deadline First (EDF)

    Critical sections use a combination of:
        - Monitor
        - Binary Semaphore
    """

    def __init__(
        self,
        tasks: List[Task],
        policy: str,
        cs_len: int = 2,
        horizon: Optional[int] = None,
        drain: bool = True,
    ):

        if not tasks:
            raise ValueError("At least one task is required.")

        if policy not in POLICIES:
            raise ValueError(
                f"Unknown policy {policy!r}; choose from {POLICIES}"
            )

        if cs_len < 2:
            raise ValueError(
                "cs_len must be at least 2 to use both Monitor "
                "and Binary Semaphore."
            )

        if horizon is not None and horizon <= 0:
            raise ValueError("horizon must be a positive integer.")

        self.tasks = tasks
        self.policy = policy
        self.cs_len = cs_len
        self.drain = drain

        self.hyperperiod = lcm(*(task.T for task in tasks))
        self.horizon = (
            horizon if horizon is not None else self.hyperperiod
        )

        self.resources = make_resources()

        self.jobs: List[Job] = []
        self.ready: List[Job] = []
        self.blocked: List[Job] = []

        self.running: Optional[Job] = None

        self.slices = []

        self.counters = {
            "idle": 0,
            "preemptions": 0,
            "lock_holder_preemptions": 0,
        }

        self.jid_count = {
            task.name: 0 for task in tasks
        }

        self.seq = 0
        self.end_time = 0

    # -----------------------------------------------------------------
    # Scheduler helpers
    # -----------------------------------------------------------------

    def enqueue_ready(self, job: Job) -> None:
        self.seq += 1

        job.seq = self.seq
        job.state = "READY"

        self.ready.append(job)

    def priority(self, job: Job):
        if self.policy == "RM":
            # Shorter period means higher priority.
            return (
                job.task.T,
                job.release,
                job.order,
                job.jid,
            )

        # EDF: earlier absolute deadline means higher priority.
        return (
            job.deadline,
            job.release,
            job.order,
            job.jid,
        )

    # -----------------------------------------------------------------
    # Phase 1: periodic job releases
    # -----------------------------------------------------------------

    def release_jobs(self, now: int) -> None:

        for order, task in enumerate(self.tasks):

            if now % task.T != 0:
                continue

            self.jid_count[task.name] += 1

            job = Job(
                task=task,
                jid=self.jid_count[task.name],
                order=order,
                release=now,
                deadline=now + task.D,
                remaining=task.C,
                segments=build_segments(task, self.cs_len),
            )

            self.jobs.append(job)
            self.enqueue_ready(job)

    # -----------------------------------------------------------------
    # Phase 2: scheduling and resource requests
    # -----------------------------------------------------------------

    def pick(self) -> Optional[Job]:

        if self.policy == "PRIORITY":
            # Non-preemptive static priority.
            if self.running is None and self.ready:

                self.running = min(
                    self.ready,
                    key=lambda job: (job.task.P, job.seq),
                )

                self.ready.remove(self.running)

            return self.running

        # RM and EDF are preemptive.
        candidates = self.ready.copy()

        if self.running is not None:
            candidates.append(self.running)

        if not candidates:
            return None

        best = min(candidates, key=self.priority)

        if best is not self.running:

            if self.running is not None:

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

            segment = job.pending_request()

            if segment is None:
                return

            resource = self.resources[segment.resource]

            if job.req_time is None:
                job.req_time = now

            if resource.acquire(job):

                job.granted = True
                job.woken = False

                job.resource_wait += now - job.req_time
                job.req_time = None

                return

            # Resource is busy: the job must leave the CPU.
            job.state = "BLOCKED"
            self.blocked.append(job)

            self.running = None

    # -----------------------------------------------------------------
    # Phase 3: timeline bookkeeping
    # -----------------------------------------------------------------

    def record_states(self, now: int) -> None:

        for job in self.jobs:

            if job.completion is not None:
                continue

            if job is self.running:

                job.timeline.append(
                    "C" if job.granted else "#"
                )

            elif job.state == "BLOCKED":

                job.timeline.append("b")
                job.block_ticks += 1

            else:
                job.timeline.append(".")

    # -----------------------------------------------------------------
    # Phase 4: execute one time unit
    # -----------------------------------------------------------------

    def execute(self, now: int) -> None:

        job = self.running

        if job is None:

            self.slices.append(
                (now, now + 1, "IDLE", "")
            )

            self.counters["idle"] += 1

            return

        if job.first_start is None:
            job.first_start = now

        segment = job.held_segment()

        resource_name = segment.resource if segment else ""

        self.slices.append(
            (now, now + 1, job.label, resource_name)
        )

        job.remaining -= 1
        job.executed += 1

        # Check whether the current critical section has ended.
        if (
            segment is not None
            and job.executed == segment.start + segment.length
        ):

            resource = self.resources[segment.resource]

            woken = resource.release(job)

            job.granted = False
            job.seg_idx += 1

            if woken is not None:

                self.blocked.remove(woken)
                self.enqueue_ready(woken)

                if resource.HANDOFF:
                    # Semaphore directly transfers ownership.
                    woken.granted = True

                    woken.resource_wait += (
                        now + 1 - woken.req_time
                    )

                    woken.req_time = None

        # Job completion.
        if job.remaining == 0:

            job.completion = now + 1
            job.state = "DONE"

            self.running = None

    # -----------------------------------------------------------------
    # Run simulation
    # -----------------------------------------------------------------

    def run(self) -> "Simulator":

        now = 0

        while (
            now < self.horizon
            or (
                self.drain
                and any(
                    job.completion is None
                    for job in self.jobs
                )
            )
        ):

            if now < self.horizon:
                self.release_jobs(now)

            self.dispatch(now)
            self.record_states(now)
            self.execute(now)

            now += 1

        self.end_time = now

        return self

    # -----------------------------------------------------------------
    # Results
    # -----------------------------------------------------------------

    def job_rows(self) -> List[dict]:

        rows = []

        for job in self.jobs:

            done = job.completion is not None

            turnaround = (
                job.completion - job.release
                if done else None
            )

            if done:
                missed = job.completion > job.deadline
            else:
                missed = self.end_time >= job.deadline

            rows.append({
                "policy": self.policy,
                "job": job.label,
                "task": job.task.name,
                "release": job.release,
                "deadline": job.deadline,
                "C": job.task.C,
                "first_start": job.first_start,
                "completion": job.completion,

                "waiting_time": (
                    turnaround - job.task.C
                    if done else None
                ),

                "turnaround_time": turnaround,

                "response_time": (
                    job.first_start - job.release
                    if job.first_start is not None else None
                ),

                "resource_waiting_time": job.resource_wait,
                "block_time": job.block_ticks,
                "deadline_missed": missed,
            })

        return rows

    def summary(self) -> dict:

        def avg(values):
            return (
                round(sum(values) / len(values), 3)
                if values else None
            )

        done = [
            job for job in self.jobs
            if job.completion is not None
        ]

        rows = self.job_rows()

        summary = {
            "policy": self.policy,
            "hyperperiod": self.hyperperiod,
            "end_time": self.end_time,

            "jobs_released": len(self.jobs),
            "jobs_completed": len(done),

            "avg_waiting_time": avg([
                job.completion - job.release - job.task.C
                for job in done
            ]),

            "avg_turnaround_time": avg([
                job.completion - job.release
                for job in done
            ]),

            "avg_response_time": avg([
                job.first_start - job.release
                for job in done
                if job.first_start is not None
            ]),

            "avg_resource_waiting_time": avg([
                job.resource_wait
                for job in done
            ]),

            "avg_block_time": avg([
                job.block_ticks
                for job in done
            ]),

            "deadline_misses": sum(
                1 for row in rows
                if row["deadline_missed"]
            ),

            "idle_time": self.counters["idle"],
            "preemptions": self.counters["preemptions"],

            "lock_holder_preemptions": (
                self.counters["lock_holder_preemptions"]
            ),
        }

        for resource_name, resource in self.resources.items():

            for key, value in resource.stats().items():
                summary[f"{resource_name}_{key}"] = value

        return summary

    def gantt(self) -> List[tuple]:

        merged = []

        for start, end, label, resource in self.slices:

            if (
                merged
                and merged[-1][2] == label
                and merged[-1][3] == resource
                and merged[-1][1] == start
            ):

                merged[-1] = (
                    merged[-1][0],
                    end,
                    label,
                    resource,
                )

            else:
                merged.append(
                    (start, end, label, resource)
                )

        return merged


# ---------------------------------------------------------------------
# ASCII Gantt chart
# ---------------------------------------------------------------------

def ascii_gantt(sim: Simulator) -> str:
    """
    Symbols:
        # = running outside critical section
        C = running inside critical section
        b = blocked
        . = ready
        _ = CPU idle
    """

    end = sim.end_time

    lines = [
        f"  {sim.policy}   "
        "(# run, C critical section, b blocked, . ready, _ idle)",

        "  " + "time  " + "".join(
            str(t % 10) for t in range(end)
        ),
    ]

    for job in sim.jobs:

        row = [" "] * end

        for index, char in enumerate(job.timeline):

            position = job.release + index

            if position < end:
                row[position] = char

        lines.append(
            f"  {job.label:<6}" + "".join(row)
        )

    idle = [" "] * end

    for start, finish, label, _ in sim.slices:

        if label == "IDLE":

            for time in range(start, finish):
                idle[time] = "_"

    lines.append(
        f"  {'CPU':<6}" + "".join(idle)
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Plot Gantt chart
# ---------------------------------------------------------------------

def plot_case(
    case: str,
    sims: List[Simulator],
    outdir: str,
) -> Optional[str]:

    try:
        import matplotlib

        matplotlib.use("Agg")

        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch

    except ImportError:

        print(
            "  (matplotlib not installed - "
            "PNG Gantt chart skipped; text Gantt is shown instead)"
        )

        return None

    tasks = sims[0].tasks

    palette = plt.get_cmap("tab10")

    color = {
        task.name: palette(index % 10)
        for index, task in enumerate(tasks)
    }

    rows_per = [
        len(sim.jobs) + 1
        for sim in sims
    ]

    fig, axes = plt.subplots(
        len(sims),
        1,
        figsize=(
            13,
            0.27 * sum(rows_per) + 1.7 * len(sims),
        ),
        gridspec_kw={
            "height_ratios": rows_per
        },
        squeeze=False,
    )

    for ax, sim in zip(axes[:, 0], sims):

        order = [
            job.label for job in sim.jobs
        ]

        ypos = {
            label: len(order) - index
            for index, label in enumerate(order)
        }

        ypos["IDLE"] = 0

        job_task = {
            job.label: job.task.name
            for job in sim.jobs
        }

        for job in sim.jobs:

            y = ypos[job.label]

            for index, char in enumerate(job.timeline):

                if char == "b":

                    ax.broken_barh(
                        [(job.release + index, 1)],
                        (y - 0.35, 0.7),
                        facecolors="#f6b8b8",
                        edgecolor="#c0504d",
                        linewidth=0.5,
                    )

            ax.plot(
                [job.deadline],
                [y],
                marker="v",
                color=(
                    "red"
                    if (
                        job.completion is None
                        or job.completion > job.deadline
                    )
                    else "black"
                ),
                markersize=5,
            )

        for start, finish, label, resource in sim.slices:

            y = ypos[label]

            if label == "IDLE":

                ax.broken_barh(
                    [(start, finish - start)],
                    (y - 0.35, 0.7),
                    facecolors="#d9d9d9",
                    edgecolor="grey",
                    linewidth=0.5,
                )

                continue

            task_name = job_task[label]

            ax.broken_barh(
                [(start, finish - start)],
                (y - 0.35, 0.7),
                facecolors=color[task_name],
                edgecolor="black",
                linewidth=1.2 if resource else 0.4,
                hatch="///" if resource else None,
            )

        ax.set_yticks(list(ypos.values()))
        ax.set_yticklabels(
            list(ypos.keys()),
            fontsize=8,
        )

        ax.set_xlim(
            -0.2,
            sim.end_time + 0.4,
        )

        ax.set_xticks(
            range(
                0,
                sim.end_time + 1,
                1 if sim.end_time <= 40 else 5,
            )
        )

        ax.tick_params(
            axis="x",
            labelsize=8,
        )

        ax.grid(
            axis="x",
            color="#e5e5e5",
        )

        summary = sim.summary()

        ax.set_title(
            f"{sim.policy} - "
            f"deadline misses: {summary['deadline_misses']}, "
            f"avg block time: {summary['avg_block_time']}",
            loc="left",
            fontsize=10,
        )

    handles = [
        Patch(
            facecolor=color[task.name],
            edgecolor="black",
            label=f"task {task.name}",
        )
        for task in tasks
    ]

    handles += [
        Patch(
            facecolor="white",
            edgecolor="black",
            hatch="///",
            label="holding monitor/semaphore",
        ),

        Patch(
            facecolor="#f6b8b8",
            edgecolor="#c0504d",
            label="blocked",
        ),

        Patch(
            facecolor="#d9d9d9",
            edgecolor="grey",
            label="CPU idle",
        ),
    ]

    fig.legend(
        handles=handles,
        loc="lower center",
        ncol=len(handles),
        fontsize=8,
        frameon=False,
    )

    fig.suptitle(
        f"Case {case}: Non-Preemptive Priority Scheduling "
        f"with Combined Monitor and Binary Semaphore "
        f"vs RM and EDF (v = deadline, red = missed)",
        fontsize=12,
    )

    fig.tight_layout(
        rect=(0, 0.04, 1, 0.97)
    )

    path = os.path.join(
        outdir,
        f"gantt_{case}.png",
    )

    fig.savefig(
        path,
        dpi=130,
    )

    plt.close(fig)

    return path


# ---------------------------------------------------------------------
# CSV reporting
# ---------------------------------------------------------------------

def write_csv(path: str, rows: List[dict]) -> None:

    if not rows:
        return

    fields = list(rows[0].keys())

    for row in rows:

        for key in row:

            if key not in fields:
                fields.append(key)

    with open(
        path,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields,
        )

        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------
# Input parsing and validation
# ---------------------------------------------------------------------

def make_task_list(raw) -> List[Task]:

    tasks = []

    for item in raw:

        if len(item) < 3:
            raise ValueError(
                "Each task needs at least: name C T"
            )

        name = str(item[0]).strip()

        if not name:
            raise ValueError("Task name cannot be empty.")

        C = int(item[1])
        T = int(item[2])

        D = (
            int(item[3])
            if len(item) > 3 and item[3] not in (None, "")
            else T
        )

        P = (
            int(item[4])
            if len(item) > 4 and item[4] not in (None, "")
            else len(tasks) + 1
        )

        if C <= 0 or T <= 0 or D <= 0:

            raise ValueError(
                f"Task {name}: C, T and D must be positive integers."
            )

        if C > D:

            print(
                f"Warning: task {name} has C > D "
                "and cannot meet its deadline."
            )

        tasks.append(
            Task(
                name=name,
                C=C,
                T=T,
                D=D,
                P=P,
            )
        )

    if not tasks:
        raise ValueError("At least one task is required.")

    if len({task.name for task in tasks}) != len(tasks):
        raise ValueError("Task names must be unique.")

    return tasks


def parse_task_string(text: str) -> List[Task]:
    """
    Example:
        A,1,4,4,1;B,2,5,5,2

    Format:
        name,C,T[,D[,P]]

    D defaults to T.
    P defaults to task position.
    """

    raw = [
        [part.strip() for part in chunk.split(",")]
        for chunk in text.split(";")
        if chunk.strip()
    ]

    return make_task_list(raw)


def load_task_file(
    path: str,
    default_case: str,
) -> Dict[str, List[Task]]:

    cases: Dict[str, List[Task]] = {}

    if path.lower().endswith(".json"):

        with open(path, encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, list):
            data = {
                default_case: data
            }

        for case, items in data.items():

            cases[case] = make_task_list([
                [
                    item["name"],
                    item["C"],
                    item["T"],
                    item.get("D"),
                    item.get("P"),
                ]
                for item in items
            ])

    else:

        grouped: Dict[str, list] = {}

        with open(
            path,
            newline="",
            encoding="utf-8",
        ) as file:

            for row in csv.DictReader(file):

                grouped.setdefault(
                    row.get("case") or default_case,
                    [],
                ).append([
                    row["name"],
                    row["C"],
                    row["T"],
                    row.get("D"),
                    row.get("P"),
                ])

        cases = {
            case: make_task_list(items)
            for case, items in grouped.items()
        }

    if not cases:
        raise ValueError("No task sets were found in the input file.")

    return cases


def interactive_input():

    print(
        "Interactive mode - enter one task per line:\n"
        "name C T [D [P]]\n"
        "P: smaller number means higher priority\n"
        "Press Enter on an empty line to finish."
    )

    raw = []

    while True:

        line = input(
            f"task {len(raw) + 1}> "
        ).strip()

        if not line:
            break

        raw.append(
            line.replace(",", " ").split()
        )

    case = (
        input("case name [custom]> ").strip()
        or "custom"
    )

    policies_input = (
        input("policies [PRIORITY,RM,EDF]> ").strip()
        or "PRIORITY,RM,EDF"
    )

    cs_input = (
        input("critical-section length [2]> ").strip()
        or "2"
    )

    policies = [
        policy.strip().upper()
        for policy in policies_input.split(",")
        if policy.strip()
    ]

    return (
        {
            case: make_task_list(raw)
        },
        policies,
        int(cs_input),
    )


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main(argv=None) -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Non-Preemptive Priority Scheduling using combined "
            "Monitor and Binary Semaphore, compared with RM and EDF"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "--tasks",
        help=(
            'Task set: "name,C,T[,D[,P]];..." '
            "(P: smaller number = higher priority)"
        ),
    )

    parser.add_argument(
        "--case-name",
        default="custom",
        help="Label for the task set given with --tasks",
    )

    parser.add_argument(
        "--tasks-file",
        help="JSON or CSV file containing one or more task sets",
    )

    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Enter the task set using the keyboard",
    )

    parser.add_argument(
        "--policies",
        default="PRIORITY,RM,EDF",
        help="Comma-separated list: PRIORITY,RM,EDF",
    )

    parser.add_argument(
        "--cs-len",
        type=int,
        default=2,
        help="Critical-section length in time units (minimum 2)",
    )

    parser.add_argument(
        "--horizon",
        type=int,
        help="Release jobs until this time (default: hyperperiod)",
    )

    parser.add_argument(
        "--no-drain",
        action="store_true",
        help="Stop at the horizon; unfinished jobs remain unfinished",
    )

    parser.add_argument(
        "--outdir",
        default="results",
        help="Output directory (default: results)",
    )

    parser.add_argument(
        "--no-plots",
        action="store_true",
        help="Do not generate PNG Gantt charts",
    )

    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print ASCII Gantt charts",
    )

    args = parser.parse_args(argv)

    # -------------------------------------------------------------
    # Read and validate inputs
    # -------------------------------------------------------------

    try:

        policies = [
            policy.strip().upper()
            for policy in args.policies.split(",")
            if policy.strip()
        ]

        cs_len = args.cs_len

        if args.interactive:

            cases, policies, cs_len = interactive_input()

        elif args.tasks:

            cases = {
                args.case_name: parse_task_string(args.tasks)
            }

        elif args.tasks_file:

            cases = load_task_file(
                args.tasks_file,
                args.case_name,
            )

        else:

            cases = {
                case: make_task_list(items)
                for case, items in DEFAULT_CASES.items()
            }

        if cs_len < 2:

            raise ValueError(
                "Critical-section length must be at least 2 "
                "to use both synchronization mechanisms."
            )

        if args.horizon is not None and args.horizon <= 0:

            raise ValueError(
                "--horizon must be a positive integer."
            )

        if not policies:

            raise ValueError(
                "At least one scheduling policy must be selected."
            )

        for policy in policies:

            if policy not in POLICIES:

                raise ValueError(
                    f"Unknown policy {policy!r}; "
                    f"choose from {POLICIES}"
                )

        # Warn about tasks that are too short to use both resources.
        for case, tasks in cases.items():

            for task in tasks:

                if task.C < 2:

                    print(
                        f"Warning: task {task.name} in case {case} "
                        "has C < 2, so it cannot use both "
                        "Monitor and Binary Semaphore."
                    )

    except (
        ValueError,
        KeyError,
        OSError,
        TypeError,
    ) as exc:

        print(
            f"Input error: {exc}",
            file=sys.stderr,
        )

        return 2

    # -------------------------------------------------------------
    # Run simulations
    # -------------------------------------------------------------

    os.makedirs(
        args.outdir,
        exist_ok=True,
    )

    summaries = []
    details = []
    gantts = []

    for case, tasks in cases.items():

        utilization = sum(
            task.C / task.T
            for task in tasks
        )

        task_description = ", ".join(
            (
                f"{task.name}"
                f"(C={task.C},T={task.T},"
                f"D={task.D},P={task.P})"
            )
            for task in tasks
        )

        print(
            f"\n=== Case {case}: {task_description}"
            f" | utilization = {utilization:.4f}"
        )

        sims = []

        for policy in policies:

            sim = Simulator(
                tasks=tasks,
                policy=policy,
                cs_len=cs_len,
                horizon=args.horizon,
                drain=not args.no_drain,
            ).run()

            sims.append(sim)

            summary = sim.summary()
            summary["case"] = case

            summaries.append(summary)

            for row in sim.job_rows():

                row["case"] = case
                details.append(row)

            for start, end, label, resource in sim.gantt():

                gantts.append({
                    "case": case,
                    "policy": policy,
                    "start": start,
                    "end": end,
                    "slot": label,
                    "resource_held": resource,
                })

            if not args.quiet:

                print(
                    ascii_gantt(sim)
                )

        # Generate Gantt chart.
        if not args.no_plots:

            path = plot_case(
                case,
                sims,
                args.outdir,
            )

            if path:
                print(
                    f"  Gantt chart saved: {path}"
                )

    # -------------------------------------------------------------
    # Export CSV files
    # -------------------------------------------------------------

    write_csv(
        os.path.join(
            args.outdir,
            f"{FILE_PREFIX}_summary.csv",
        ),
        summaries,
    )

    write_csv(
        os.path.join(
            args.outdir,
            f"{FILE_PREFIX}_job_details.csv",
        ),
        details,
    )

    write_csv(
        os.path.join(
            args.outdir,
            f"{FILE_PREFIX}_gantt.csv",
        ),
        gantts,
    )

    # -------------------------------------------------------------
    # Print comparison summary
    # -------------------------------------------------------------

    print(
        "\ncase,policy,avg_wait,avg_turnaround,"
        "avg_response,avg_resource_wait,avg_block,"
        "misses,idle,preempt,lock_preempt"
    )

    for summary in summaries:

        values = (
            summary["case"],
            summary["policy"],
            summary["avg_waiting_time"],
            summary["avg_turnaround_time"],
            summary["avg_response_time"],
            summary["avg_resource_waiting_time"],
            summary["avg_block_time"],
            summary["deadline_misses"],
            summary["idle_time"],
            summary["preemptions"],
            summary["lock_holder_preemptions"],
        )

        print(
            ",".join(
                str(value)
                for value in values
            )
        )

    print(
        f"\nCSV files written to: "
        f"{os.path.abspath(args.outdir)}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
