import os
import time
import psutil
import platform
from datetime import datetime


def monitor_cpu():
    # Etelaate kolli system
    print("=" * 60)
    print("Gozaresh vaziyate pardazande va farayandhaye system")
    print("=" * 60)

    print(f"System عامل: {platform.system()} {platform.release()}")
    print(f"Name pardazande: {platform.processor() or 'Namoshakhas'}")
    print(f"Shenase farayande barnameye fa'ali: {os.getpid()}")

    # Tedade hastehaye CPU
    physical_cores = psutil.cpu_count(logical=False)
    logical_cores = psutil.cpu_count(logical=True)

    print("\n--- Etelaate CPU ---")
    print(f"Tedade hastehaye fiziki: {physical_cores or 'Namoshakhas'}")
    print(f"Tedade hastehaye manteghi: {logical_cores or 'Namoshakhas'}")

    # Darsade estefade az CPU dar bazeye yek sanie
    print("\nDar hale andaze-giriye mizane estefade az CPU...")
    cpu_percent = psutil.cpu_percent(interval=1)

    print(f"Darsade estefade kolli az CPU: {cpu_percent:.1f}%")

    # Mizane estefade har haste manteghi
    per_core = psutil.cpu_percent(interval=1, percpu=True)

    print("\n--- Vaziyate har haste manteghi ---")
    for index, usage in enumerate(per_core):
        status = (
            "Dar hale estefade"
            if usage > 0
            else "Bedoone fa'aliyate ghabele andaze-giri"
        )
        print(f"Haste manteghi {index}: {usage:.1f}% - {status}")

    # Shoomareshe farayandha va thread-ha
    process_count = 0
    thread_count = 0
    running_count = 0
    sleeping_count = 0
    other_states_count = 0
    process_data = []

    for proc in psutil.process_iter(
        ["pid", "name", "status", "num_threads", "cpu_percent"]
    ):
        try:
            info = proc.info
            process_count += 1

            num_threads = info["num_threads"] or 0
            thread_count += num_threads

            status = info["status"]

            if status == psutil.STATUS_RUNNING:
                running_count += 1
            elif status == psutil.STATUS_SLEEPING:
                sleeping_count += 1
            else:
                other_states_count += 1

            process_data.append({
                "pid": info["pid"],
                "name": info["name"] or "Namoshakhas",
                "status": status,
                "threads": num_threads,
                "cpu": info["cpu_percent"] or 0.0
            })

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess
        ):
            # Momken ast farayand dar hengame shomaresh khateme yabad
            continue

    # Namayeshe kholase
    print("\n--- Amare farayandha va thread-ha ---")
    print(f"Tedade farayandhaye moshahede-shode: {process_count}")
    print(f"Majmooe threadhaye farayandha: {thread_count}")
    print(f"Farayandha dar vaziyate Running: {running_count}")
    print(f"Farayandha dar vaziyate Sleeping: {sleeping_count}")
    print(f"Farayandha dar sayere vaziyatha: {other_states_count}")

    # Namayeshe farayandhayi ke bishtarin thread ra darand
    process_data.sort(
        key=lambda item: item["threads"],
        reverse=True
    )

    print("\n--- 10 farayand ba bishtarin tedade thread ---")
    print(f"{'PID':<10}{'Threads':<10}{'Status':<15}Name")
    print("-" * 60)

    for item in process_data[:10]:
        print(
            f"{item['pid']:<10}"
            f"{item['threads']:<10}"
            f"{item['status']:<15}"
            f"{item['name']}"
        )

    print(
        "\nZamane gozaresh:",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    print("=" * 60)


if __name__ == "__main__":
    monitor_cpu()
