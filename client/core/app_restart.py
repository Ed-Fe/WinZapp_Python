"""Restart WinZapp from itself: a small batch waits for this process to end,
then starts the same program again with the same arguments. Used when a
change (the connection engine chosen in the pairing dialog) is only picked up
by a fresh start. Pure builders here; the caller launches the script."""

import os
import subprocess
import sys

#: Arguments of this run that must not follow it into the next one.
TRANSIENT_ARGUMENTS = ("--post-update", "--background")

#: How many one-second checks the script makes before giving up (it never
#: starts a second copy over a process that refuses to end).
WAIT_PASSES = 120


def restart_command(executable: str, argv, frozen: bool) -> list:
    """The command line of the next run: this program with this run's
    arguments, less the transient ones."""
    kept = [a for a in list(argv)[1:] if a not in TRANSIENT_ARGUMENTS]
    if frozen:
        return [executable] + kept
    return [executable, os.path.abspath(argv[0])] + kept


def build_restart_script(pid: int, command: list, log_path: str = "",
                         passes: int = WAIT_PASSES) -> str:
    """The batch text: wait until ``pid`` is gone, then start ``command``."""
    line = subprocess.list2cmdline(command)
    log = f'>> "{log_path}" echo ' if log_path else "rem "
    return (
        "@echo off\n"
        "setlocal EnableDelayedExpansion\n"
        "set /a WAITED=0\n"
        ":WAIT\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\n'
        "if errorlevel 1 goto RESTART\n"
        "set /a WAITED+=1\n"
        f"if !WAITED! GEQ {passes} goto GAVE_UP\n"
        "timeout /t 1 /nobreak >NUL\n"
        "goto WAIT\n"
        ":RESTART\n"
        f"{log}restarting WinZapp\n"
        f'start "" {line}\n'
        'del "%~f0"\n'
        "goto :EOF\n"
        ":GAVE_UP\n"
        f"{log}the previous run did not end - not restarting\n"
        'del "%~f0"\n'
        "exit /b 1\n"
    )


def start_restart_waiter(script_path: str, pid: int, command: list, log_path: str = "",
                         environment=None, popen=subprocess.Popen) -> bool:
    """Write and launch the script, detached and without a window. True when it
    was started."""
    from update_relaunch import relaunch_environment

    try:
        with open(script_path, "w", encoding="mbcs", newline="\r\n") as stream:
            stream.write(build_restart_script(pid, command, log_path))
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        popen(["cmd.exe", "/c", script_path],
              env=relaunch_environment(os.environ if environment is None else environment),
              creationflags=flags, close_fds=True,
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def restart_this_program(script_path: str, log_path: str = "") -> bool:
    """Arrange the restart of the running program (see the module docstring)."""
    return start_restart_waiter(
        script_path, os.getpid(),
        restart_command(sys.executable, sys.argv, bool(getattr(sys, "frozen", False))), log_path)
