import json
import shutil
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / ".voice-action" / "logs"


def _resolve_dst_conflict(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    parent = dst.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def _move_to_trash(p: Path) -> None:
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    dst = _resolve_dst_conflict(trash / p.name)
    shutil.move(str(p), str(dst))


def execute_plan(actions: list[dict], log_path: Path | None = None) -> list[dict]:
    results = []

    for action in actions:
        act = action.get("action")
        src = Path(action["src"]) if "src" in action else None
        result = {**action}

        try:
            if act == "move":
                dst = Path(action["dst"])
                dst = _resolve_dst_conflict(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                result["status"] = "ok"
                result["dst"] = str(dst)

            elif act == "rename":
                dst = Path(action["dst"])
                src.rename(dst)
                result["status"] = "ok"

            elif act == "delete":
                _move_to_trash(src)
                result["status"] = "ok"

            else:
                result["status"] = "error"
                result["error"] = f"알 수 없는 액션: {act}"

        except FileNotFoundError as e:
            result["status"] = "error"
            result["error"] = str(e)
        except PermissionError as e:
            result["status"] = "error"
            result["error"] = str(e)
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)

        results.append(result)

    _save_log(results, log_path)
    return results


def _save_log(results: list[dict], log_path: Path | None) -> None:
    if log_path is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        log_path = LOG_DIR / f"{timestamp}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
