#!/usr/bin/env python3
"""Check that a screen change really reached the app, without a person looking.

python3 scripts/ui_check.py status        # is the installed app built from the latest code?
python3 scripts/ui_check.py run [이름...]  # replay scripts/ui_scenarios/*.json on a new build

`run` builds this checkout's app and starts it with a scripted backend instead of Python's real one.
The script sends the same events the backend would, presses keys and clicks like the user, then
asks the app what is on screen (chat window, type box, note, chips, file box) and compares.
Windows flash on screen for a few seconds; the user's keys are not caught (voice key is off).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = Path(__file__).resolve().parent / "ui_scenarios"
MAIN = Path(
    subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    ).stdout.strip()
).parent
APPS = [
    Path.home() / "Applications/Symbio.app",
    MAIN / "native-preview/.build/FinderChatPreview.app",
]


def git(*args):
    return subprocess.run(
        ["git", "-C", str(MAIN), *args], capture_output=True, text=True
    ).stdout.strip()


def status():
    latest = git("rev-parse", "main:native-preview")
    running = subprocess.run(["ps", "-axwwo", "command="], capture_output=True, text=True).stdout
    stale = False
    for app in APPS:
        runtime = app / "Contents/Resources/runtime.json"
        if not runtime.exists():
            print(f"없음   {app}")
            stale |= app == APPS[0]  # the installed app is the one the user opens
            continue
        built = json.loads(runtime.read_text()).get("app_tree")
        ok = built == latest
        stale |= not ok
        state = "최신" if ok else ("옛 빌드(버전 기록 없음)" if built is None else "옛 빌드")
        on = " · 실행 중" if str(app / "Contents/MacOS") in running else ""
        print(f"{state:<6} {app}{on}")
    if stale:
        print("→ native-preview/install-app.sh 를 실행하면 최신으로 바뀝니다.")
    return 1 if stale else 0


# ---- scripted backend (runs inside the app as its "Python backend") ----


def serve(scenario_path, result_path):
    scenario = json.loads(Path(scenario_path).read_text())
    # Not the system temp folder: the app sees /private/var as /var and would refuse every file.
    folder = Path(tempfile.mkdtemp(prefix="ui-check-", dir=ROOT / "native-preview/.build"))
    out, failures, sent = sys.stdout, [], []

    def emit(value):
        text = json.dumps(value, ensure_ascii=False).replace("{dir}", str(folder))
        out.write(text + "\n")
        out.flush()

    def read_until(action):
        while line := sys.stdin.readline():
            value = json.loads(line)
            if value.get("action") == action:
                return value
            sent.append(value)
        return None

    emit({"type": "config", "folders": [str(folder)], "finder_reveal": False})
    emit({"type": "state", "state": "IDLE"})
    time.sleep(1)  # let launch focus settle: a type box that loses focus closes itself
    for number, step in enumerate(scenario["steps"], 1):
        if "touch" in step:
            for name in step["touch"]:
                (folder / name).write_text(name)
        if "emit" in step:
            emit(step["emit"])
        if "do" in step:
            emit({"type": "_test", **step})
            done = read_until("_done")
            if not done or not done.get("ok"):
                failures.append(f"{number}단계 {step['do']}: 앱이 동작을 하지 못함")
        time.sleep(step.get("wait", 0.3))
        if "expect" in step:
            emit({"type": "_test", "do": "snapshot"})
            reply = read_until("_snapshot")
            shown = reply["state"] if reply else {}
            actions = [value.get("action") for value in sent]
            sent.clear()
            for key, want in step["expect"].items():
                got = actions if key == "sent" else shown.get(key)
                good = all(a in got for a in want) if key == "sent" else got == want
                if not good:
                    failures.append(f"{number}단계 {key}: 기대 {want!r}, 실제 {got!r}")
    shutil.rmtree(folder, ignore_errors=True)
    Path(result_path).write_text(json.dumps({"failures": failures}, ensure_ascii=False))
    emit({"type": "_test", "do": "quit"})
    read_until("shutdown")


def run(names):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import heavy_lock

    heavy_lock.acquire()
    package = ROOT / "native-preview"
    build = subprocess.run(
        ["swift", "build", "--package-path", str(package)], capture_output=True, text=True
    )
    if build.returncode:
        print(build.stdout[-2000:] + build.stderr[-2000:])
        return 1
    binary = (
        Path(
            subprocess.run(
                ["swift", "build", "--package-path", str(package), "--show-bin-path"],
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        / "FinderChatPreview"
    )
    paths = [SCENARIOS / f"{name}.json" for name in names] or sorted(SCENARIOS.glob("*.json"))
    failed = 0
    with tempfile.TemporaryDirectory() as work:
        for path in paths:
            result = Path(work) / f"{path.stem}.result.json"
            runtime = Path(work) / "runtime.json"
            runtime.write_text(
                json.dumps(
                    {
                        "project": str(ROOT),
                        "python": sys.executable,
                        "args": ["-u", __file__, "serve", str(path), str(result)],
                    }
                )
            )
            app = subprocess.Popen(
                [str(binary)],
                env={**os.environ, "SYMBIO_UI_TEST_RUNTIME": str(runtime)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                code = app.wait(timeout=60)
            except subprocess.TimeoutExpired:
                app.kill()
                code = "시간 초과"
            problems = (
                json.loads(result.read_text())["failures"]
                if result.exists()
                else ["시나리오를 끝까지 돌지 못함"]
            )
            if code != 0:
                problems.append(f"앱이 정상 종료하지 못함({code})")
            failed += bool(problems)
            print(("FAIL " if problems else "PASS ") + path.stem)
            for problem in problems:
                print("     " + problem)
    return 1 if failed else 0


if __name__ == "__main__":
    command, *rest = sys.argv[1:] or ["status"]
    if command == "serve":
        serve(*rest)
    else:
        raise SystemExit(status() if command == "status" else run(rest))
