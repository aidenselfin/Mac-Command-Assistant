import json
import re

import anthropic

SYSTEM_PROMPT = """You are a macOS file organization assistant. Given a snapshot of the user's file system and a voice command, return a JSON array of file actions.

Output format (JSON array only, no extra text):
[
  {"action": "move",   "src": "<absolute_path>", "dst": "<absolute_path>", "reason": "<one line>"},
  {"action": "rename", "src": "<absolute_path>", "dst": "<absolute_path>", "reason": "<one line>"},
  {"action": "delete", "src": "<absolute_path>",                           "reason": "<one line>"}
]

Rules:
- Only use actions: move, rename, delete
- All paths must be absolute
- If nothing should be done, return []
- Do not invent files that are not in the snapshot

Example 1:
Snapshot shows: report_final.pdf and report_copy.pdf in ~/Downloads (same sha8)
Command: "다운로드 폴더에서 중복 PDF 삭제해줘"
Response: [{"action": "delete", "src": "/Users/x/Downloads/report_copy.pdf", "reason": "sha8 동일한 중복 파일"}]

Example 2:
Snapshot shows: 2024_tax.pdf in ~/Downloads
Command: "세금 관련 파일 Documents로 옮겨줘"
Response: [{"action": "move", "src": "/Users/x/Downloads/2024_tax.pdf", "dst": "/Users/x/Documents/2024_tax.pdf", "reason": "세금 관련 파일"}]"""

ALLOWED_ACTIONS = {"move", "rename", "delete"}


def call_llm(voice_text: str, fs_snap: str) -> list[dict]:
    client = anthropic.Anthropic()

    user_message = f"<snapshot>\n{fs_snap}\n</snapshot>\n\n<command>{voice_text}</command>"

    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    raw = response.content[0].text.strip()

    try:
        actions = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            actions = json.loads(match.group())
        else:
            raise ValueError("LLM 응답을 파싱할 수 없습니다.")

    return [a for a in actions if isinstance(a, dict) and a.get("action") in ALLOWED_ACTIONS]
