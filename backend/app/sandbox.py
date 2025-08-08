import importlib.util
import multiprocessing as mp
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

# Only available on Unix-like systems; on Windows the resource module is missing.
try:
    import resource  # type: ignore

    RLIMIT_SUPPORTED = True
except ImportError:
    RLIMIT_SUPPORTED = False

TIMEOUT_SECONDS = 0.1  # 100 ms per decision
MAX_CPU_SECONDS = 1  # wall CPU seconds for safety
MAX_MEMORY_BYTES = 256 * 1024 * 1024  # 256 MB

# Use a safer multiprocessing context to avoid excessive memory duplication and thread issues
try:
    MP_CTX = mp.get_context("fork")
except ValueError:
    try:
        MP_CTX = mp.get_context("forkserver")
    except ValueError:
        MP_CTX = mp.get_context("spawn")


@dataclass
class BotProxy:
    decide: types.MethodType  # function(state_dict) -> dict


@dataclass
class TeamController:
    team_id: str
    bots: Dict[str, BotProxy]  # bot_id -> BotProxy


# ---------------- Internal helpers ----------------

def _load_module_from_path(path: Path) -> types.ModuleType:
    # Ensure the directory is importable so multiprocessing children can unpickle classes
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load module from path")
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)  # type: ignore[arg-type]
    return module


def _ensure_classes(module: types.ModuleType):
    order = ["Sniper", "Tank", "Bomber", "Scout", "Teleporter"]
    classes = {name: cls for name, cls in module.__dict__.items() if isinstance(cls, type)}
    missing = set(order) - classes.keys()
    if missing:
        raise ValueError(f"Missing bot classes: {', '.join(missing)}")
    return [classes[name] for name in order]


# ---------------- Public API ----------------


def load_team(team_id: str, file_path: str, bot_ids: List[str]) -> TeamController:
    """Load bot classes and create instances bound to bot_ids order ROLE_ORDER."""
    module = _load_module_from_path(Path(file_path))
    class_list = _ensure_classes(module)

    bots: Dict[str, BotProxy] = {}
    for bot_id, cls in zip(bot_ids, class_list):
        instance = cls()
        if not hasattr(instance, "decide"):
            raise ValueError("Bot class must implement decide() method")
        bots[bot_id] = BotProxy(decide=instance.decide)  # type: ignore[arg-type]
    return TeamController(team_id=team_id, bots=bots)


# ---------------- Decision wrapper with timeout ----------------

def _call_decide(method, observation, conn):
    # Apply resource limits inside child before executing user code
    if RLIMIT_SUPPORTED:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
            # Avoid strict CPU limit for persistent/pooled scenarios; rely on TIMEOUT_SECONDS
            # resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS))
        except Exception:
            pass

    try:
        action = method(observation)
    except Exception as exc:
        try:
            conn.send(exc)
        except Exception:
            pass
        finally:
            conn.close()
        return
    try:
        conn.send(action)
    finally:
        conn.close()


def safe_decide(bot_proxy: BotProxy, observation: dict) -> dict:
    """Run bot decide with timeout using a forked child and a Pipe to avoid
    background feeder threads. Ensures full cleanup to prevent leaks.
    """
    parent_conn, child_conn = MP_CTX.Pipe(duplex=False)
    p = MP_CTX.Process(target=_call_decide, args=(bot_proxy.decide, observation, child_conn))
    p.daemon = True
    p.start()
    # Parent no longer needs child's end
    child_conn.close()

    result: dict = {"type": "idle"}

    p.join(TIMEOUT_SECONDS)

    if p.is_alive():
        try:
            p.terminate()
        finally:
            p.join(0.1)
    else:
        try:
            if parent_conn.poll(0):
                item = parent_conn.recv()
                if not isinstance(item, Exception) and isinstance(item, dict):
                    result = item
        except EOFError:
            pass

    try:
        parent_conn.close()
    except Exception:
        pass

    return result  # should be dict 