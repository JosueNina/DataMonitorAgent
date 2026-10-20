"""DataMonitorAgent: the one command.

    python run.py                    keep monitoring every dataset (asks for QC credentials the first time)
    python run.py --once             one cycle, then exit
    python run.py --agent            triage even when [agent] enabled is false in config.toml
    python run.py doctor             check Python, QuantConnect credentials, DataFleet access, claude, gh, git
    python run.py triage <case-dir>  triage one case folder now
    python run.py <command> ...      the monitor's own commands: run, check, content, inventory, runs, log, probe
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

COMMANDS = {"run", "check", "content", "inventory", "runs", "log", "probe"}


def main(argv):
    if argv[:1] == ["doctor"]:
        from doctor import main as doctor
        return doctor()
    if argv[:1] == ["triage"]:
        if len(argv) != 2:
            print("usage: python run.py triage <case-dir>", file=sys.stderr)
            return 2
        from agent_backend import triage
        print(triage(argv[1]))
        return 0
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    from doctor import ensure_credentials
    if not ensure_credentials():
        return 1
    if argv[:1] and argv[0] in COMMANDS or argv[:1] == ["--config"]:
        from dsmon.cli import main as cli
        return cli(argv)
    from monitor_datasets import main as service
    return service(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130)
