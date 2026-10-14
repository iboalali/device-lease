"""Stands in for `claude -p` in tests. Appends each call to FAKE_CLAUDE_LOG and answers like a
headless run that called SendMessage, or fails when FAKE_CLAUDE_RESULT is "fail"."""

import json
import os
import re
import sys

prompt = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt}) + "\n")
if os.environ.get("FAKE_CLAUDE_RESULT") == "fail":
    print("API Error: rate limited", file=sys.stderr)
    sys.exit(1)
to = re.search(r"set to exactly (\S+) and", prompt).group(1)
events = [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "SendMessage",
         "input": {"to": to, "message": "..."}}]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [
            {"type": "text", "text": json.dumps({"success": True, "message": "in inbox"})}]}]}},
    {"type": "result", "subtype": "success"},
]
for event in events:
    print(json.dumps(event))
