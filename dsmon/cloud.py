"""Run the DatasetWatch sensor as a QuantConnect cloud backtest and read its facts back.

The sensor source lives next to this package (sensor/); every run uploads it, so the cloud project is a
disposable copy and never the source of truth.
"""
import json
import time
from pathlib import Path

from .api import ApiError

SENSOR_DIR = Path(__file__).resolve().parent.parent / "sensor"
SENSOR_FILES = ("main.py", "watch_core.py")


class CloudError(RuntimeError):
    pass


def ensure_project(api, name):
    """Project id by name, creating the Python project when it does not exist yet."""
    projects = api.post_json("/projects/read", {}).get("projects", [])
    for project in projects:
        if project.get("name") == name:
            return project["projectId"]
    payload = {"name": name, "language": "Py"}
    if api.organization_id:
        payload["organizationId"] = api.organization_id
    created = api.post_json("/projects/create", payload)
    return created["projects"][0]["projectId"]


def upload_sensor(api, project_id):
    existing = {f["name"] for f in api.post_json("/files/read", {"projectId": project_id}).get("files", [])}
    for name in SENSOR_FILES:
        content = (SENSOR_DIR / name).read_text(encoding="utf-8")
        route = "/files/update" if name in existing else "/files/create"
        api.post_json(route, {"projectId": project_id, "name": name, "content": content})


def set_parameters(api, project_id, parameters):
    if parameters:
        api.post_json("/projects/update", {"projectId": project_id,
                                            "parameters": [{"key": k, "value": v} for k, v in parameters.items()]})


def compile_project(api, project_id, timeout=300):
    compile_id = api.post_json("/compile/create", {"projectId": project_id})["compileId"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = api.post_json("/compile/read", {"projectId": project_id, "compileId": compile_id})
        if state.get("state") == "BuildSuccess":
            return compile_id
        if state.get("state") == "BuildError":
            raise CloudError(f"compile failed: {json.dumps(state.get('logs', state))[:800]}")
        time.sleep(3)
    raise CloudError("compile timed out")


def run_backtest(api, project_id, compile_id, name, timeout=1800, poll=10):
    created = api.post_json("/backtests/create", {"projectId": project_id, "compileId": compile_id,
                                                   "backtestName": name})
    backtest_id = created["backtest"]["backtestId"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = api.post_json("/backtests/read", {"projectId": project_id, "backtestId": backtest_id})["backtest"]
        if state.get("error") or state.get("stacktrace"):
            raise CloudError(f"backtest {backtest_id} failed: {state.get('error')} {state.get('stacktrace')}"[:1500])
        if state.get("completed") or state.get("status") in ("Completed.", "Completed"):
            return backtest_id
        time.sleep(poll)
    raise CloudError(f"backtest {backtest_id} did not finish in {timeout}s")


def read_log(api, project_id, backtest_id, page=200, max_lines=20000):
    lines, start = [], 0
    while start < max_lines:
        try:
            chunk = api.post_json("/backtests/read/log", {"projectId": project_id, "backtestId": backtest_id,
                                                          "query": "", "start": start, "end": start + page})
        except ApiError as error:
            if lines:
                break
            raise
        batch = chunk.get("logs") or []
        lines.extend(batch)
        if len(batch) < page:
            break
        start += page
    return lines


def run_sensor(api, project_name="DatasetWatchSensor", parameters=None, log=print):
    """Upload, compile and run the sensor; returns (payload, backtest id, raw log lines)."""
    from .sensor import decode  # the sensor's own codec, shared with the cloud code
    project_id = ensure_project(api, project_name)
    upload_sensor(api, project_id)
    set_parameters(api, project_id, parameters or {})
    log(f"project {project_id}: compiling")
    compile_id = compile_project(api, project_id)
    log(f"project {project_id}: backtest running")
    backtest_id = run_backtest(api, project_id, compile_id, f"sensor {time.strftime('%Y-%m-%d %H:%M')}")
    lines = read_log(api, project_id, backtest_id)
    return decode(lines), {"projectId": project_id, "backtestId": backtest_id}, lines
