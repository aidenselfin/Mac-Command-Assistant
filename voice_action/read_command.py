"""local__run_read_command: the model combines find·grep·mdfind and other read programs itself.

The arguments are an argv list run without a shell, so `| ; > $()` and backticks are plain text.
Only fixed read programs run, with their write/exec/link-following options refused. Every path
argument must resolve inside an allowed folder, and every path the output names is checked
again, so a link or a broad search never shows what lies outside. A misused option is told
back to the model to fix; a path outside the folders is a safety boundary.
"""

import os
import subprocess
import threading
import unicodedata
from pathlib import Path

from voice_action.mcp_client import ToolDefinition, ToolRejectedError, ToolResult

READ_COMMAND_TOOL = "local__run_read_command"
PROGRAMS = {
    "find": "/usr/bin/find",
    "grep": "/usr/bin/grep",
    "mdfind": "/usr/bin/mdfind",
    "ls": "/bin/ls",
    "stat": "/usr/bin/stat",
    "wc": "/usr/bin/wc",
    "head": "/usr/bin/head",
    "file": "/usr/bin/file",
    "mdls": "/usr/bin/mdls",
}
# Options that run, write, follow links out or read a list of other paths.
FORBIDDEN = {
    "find": {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0",
             "-fprintf", "-fls", "-L", "-follow", "-H"},
    "grep": {"-R", "-S", "-O", "--dereference-recursive"},
    "mdfind": {"-live"},
    "ls": {"-L"},
    "file": {"-f", "-m", "-M", "-C", "--files-from"},
}
# Options whose next argument is a value, not a path (find's are told apart by position), and
# the options whose value is a path.
VALUES = {
    "grep": {"-e", "-m", "-A", "-B", "-C", "--include", "--exclude", "--exclude-dir",
             "--regexp", "--max-count", "--context", "--label", "--color"},
    "mdfind": {"-name", "-attr", "-s"},
    "head": {"-n", "-c"},
    "stat": {"-f", "-t"},
    "mdls": {"-name", "-n", "-nullMarker"},
}
PATH_VALUES = {
    "find": {"-newer", "-anewer", "-cnewer", "-samefile"},
    "grep": {"-f", "--file"},
    "mdfind": {"-onlyin"},
}
TIMEOUT = 5.0
MAX_LINES = 200
MAX_BYTES = 64 * 1024
SANDBOX = "/usr/bin/sandbox-exec"
SANDBOX_PROFILE = "(version 1)(allow default)(deny file-write*)(deny network*)"

READ_COMMAND_DEFINITION = ToolDefinition(
    READ_COMMAND_TOOL,
    "허용 폴더 안에서 읽기 명령 하나를 셸 없이 실행해 파일을 찾거나 정보를 본다. argv는 "
    "[프로그램, 인자...] 배열이다(파이프·리다이렉트 없음). 프로그램: find(이름·크기·날짜), "
    "grep -rl(내용), mdfind -onlyin 폴더(Spotlight 색인 보조), ls, stat, wc, head, file, mdls. "
    "경로 인자는 허용 폴더의 절대경로를 쓴다. -exec·-delete 같은 실행·쓰기 옵션은 거부된다. "
    f"출력은 {MAX_LINES}줄까지이며 넘으면 조건을 좁혀 다시 실행한다.",
    {
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": '예: ["find", "/Users/me/Documents", "-iname", "*회의록*"]',
            }
        },
        "required": ["argv"],
        "additionalProperties": False,
    },
)


class CommandError(ToolRejectedError):
    """A command the model can fix: another program, a refused option, a missing argument."""


class OutsideError(ToolRejectedError):
    """A path outside the allowed folders: a safety boundary, not something to retry."""


def _inside(raw: str, roots: tuple[Path, ...]) -> str:
    resolved = os.path.realpath(Path(roots[0], raw))  # relative paths start at the first root
    if not any(Path(resolved).is_relative_to(root) for root in roots):
        raise OutsideError(f"허용 폴더 밖 경로는 읽을 수 없습니다: {raw}")
    return resolved


def check(argv: object, roots: tuple[Path, ...]) -> list[str]:
    """The argv to run, with the program's fixed absolute path; raises on anything refused."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise CommandError("argv는 문자열 배열이어야 합니다. 예: [\"find\", \"<폴더>\", ...]")
    name = argv[0]
    program = PROGRAMS.get(name) or (name if name in PROGRAMS.values() else None)
    if program is None:
        raise CommandError(f"읽기 명령만 쓸 수 있습니다: {', '.join(PROGRAMS)}")
    name = Path(program).name
    forbidden, values, path_values = (
        FORBIDDEN.get(name, set()), VALUES.get(name, set()), PATH_VALUES.get(name, set())
    )
    positional: list[str] = []
    onlyin = False
    args = iter(argv[1:])
    for arg in args:
        option = arg.split("=", 1)[0]
        short = name == "grep" and arg.startswith("-") and not arg.startswith("--")
        if option in forbidden or (short and any(f"-{c}" in forbidden for c in arg[1:])):
            hint = " 내용 검색은 grep -r를 쓰세요." if name == "grep" else ""
            raise CommandError(f"{name}의 {option} 옵션은 쓸 수 없습니다.{hint}")
        if arg in path_values:
            value = next(args, None)
            if value is None:
                raise CommandError(f"{arg} 뒤에 경로가 필요합니다.")
            _inside(value, roots)
            onlyin = onlyin or arg == "-onlyin"
        elif arg in values:
            next(args, None)
        elif arg == "--" and name != "find":
            positional += list(args)
        elif not arg.startswith("-") and arg not in {"(", ")", "!"}:
            positional.append(arg)
    if name == "mdfind":
        if not onlyin:
            raise CommandError("mdfind에는 -onlyin <허용 폴더>가 필요합니다.")
        positional = []  # the rest is the query
    elif name == "grep" and not any(a in {"-e", "--regexp", "-f", "--file"} for a in argv):
        positional = positional[1:]  # the first one is the pattern
    elif name == "find":
        # Paths come before the expression; later bare words are values of tests.
        first = next((i for i, a in enumerate(argv[1:]) if a.startswith("-") or a in "()!"),
                     len(argv) - 1)
        positional = [a for a in argv[1:first + 1] if not a.startswith("-")]
    for raw in positional:
        _inside(raw, roots)
    return [program, *argv[1:]]


def _output_path(line: str, name: str) -> str | None:
    """The path an output line names; None for a line that names none."""
    if not line.startswith("/"):
        return None
    return line.split(":", 1)[0] if name in {"grep", "file"} else line


def run_read_command(arguments: dict, roots: tuple[Path, ...]) -> ToolResult:
    roots = tuple(Path(os.path.realpath(root)) for root in roots)
    if not roots:
        raise OutsideError("허용 폴더가 없습니다.")
    argv = check(arguments.get("argv"), roots)
    name, cwd = Path(argv[0]).name, str(roots[0])
    if os.path.exists(SANDBOX):  # no writes and no network, under the checks above
        argv = [SANDBOX, "-p", SANDBOX_PROFILE, *argv]
    process = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "LC_ALL": "en_US.UTF-8"},
    )
    fired = threading.Event()
    timer = threading.Timer(TIMEOUT, lambda: (fired.set(), process.kill()))
    timer.start()
    try:
        data = process.stdout.read(MAX_BYTES + 1)
    finally:
        timer.cancel()
        process.kill()
        process.wait()
        process.stdout.close()
    timed_out = fired.is_set()
    lines = data[:MAX_BYTES].decode("utf-8", errors="replace").splitlines()
    if len(data) > MAX_BYTES:
        lines = lines[:-1]  # the last line may be cut in the middle
    truncated = len(data) > MAX_BYTES or len(lines) > MAX_LINES or timed_out
    kept, paths = [], []
    for line in lines[:MAX_LINES]:
        line = unicodedata.normalize("NFC", line)
        if line.startswith("./"):  # relative to the first root, where the command runs
            line = cwd + line[1:]
        path = _output_path(line, name)
        if path is not None:
            resolved = os.path.realpath(path)
            if not any(Path(resolved).is_relative_to(root) for root in roots):
                continue
            paths.append(path)
        kept.append(line)
    text = "\n".join(kept) or "(결과 없음)"
    if timed_out:
        text += f"\n[{TIMEOUT:g}초 시간 초과로 잘림, 조건을 좁히세요]"
    elif truncated:
        text += f"\n[{MAX_LINES}줄·64KB 넘어 잘림, 조건을 좁히세요]"
    return ToolResult(
        True, text, structured={"paths": list(dict.fromkeys(paths))}, truncated=truncated
    )
