"""The sensor's codec, imported from the cloud source so both sides always agree on the format."""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "sensor" / "watch_core.py"
_spec = importlib.util.spec_from_file_location("watch_core", _PATH)
watch_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch_core)

decode = watch_core.decode
encode = watch_core.encode
FORMAT_VERSION = watch_core.FORMAT_VERSION
