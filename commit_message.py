#!/usr/bin/env python3
import os, json, subprocess, re, tempfile, textwrap
import openai

# --- helpers
def run(cmd):
    return subprocess.check_output(cmd, shell=True, text=True)

def sanitize_text(s, max_len=4000):
    # redact obvious private key blocks
    s = re.sub(r'-----BEGIN .*?-----.*?-----END .*?-----', '[REDACTED_PRIVATE_KEY]', s, flags=re.S)
    # redact likely API keys like "apikey=XYZ" or "OPENAI_API_KEY=..."
    s = re.sub(r'(?i)(api_key|apikey|token)[\'\"\s:=]+[A-Za-z0-9\-_]{8,}', r'\1=[REDACTED]', s)
    if len(s) > max_len:
        s = s[:max_len] + '\n...[truncated]...'
    return s

# --- load push event
event_path = os.environ.get("GITHUB_EVENT_PATH")
if not event_path or not os.path.exists(event_path):
    print("No GITHUB_EVENT_PATH; exiting.")
    exit(1)

with open(event_path) as f:
    ev = json.load(f)

commits = ev.get("commits", [])
if len(commits) != 1:
    print("Multiple or zero commits in push; this action only processes single commits (safer). Skipping.")
    exit(0)

head = ev.get("head_commit", {})
sha = head.get("id")
orig_msg = head.get("message", "")

if "[ai-generated]" in orig_msg:
    print("Commit already AI-generated. Skipping.")
    exit(0)

# get diff of the last commit
try:
    raw_diff = run(f"git show {sha} --unified=3")
except Exception as e:
    print("Failed to get git diff:", e)
    raw_diff = ""

diff = sanitize_text(raw_diff)

openai.api_key = os.environ.get("OPENAI_API_KEY")
if not openai.api_key:
    print("OPENAI_API_KEY not set. Exiting.")
    exit(1)

# build prompt
prompt = textwrap.dedent(f"""
You are an assistant that writes concise, useful git commit messages.
Rules:
- Output a one-line subject (conventional commit style: type(scope): subject) and optionally a short body (<= 3 lines).
- Use present tense.
- Be precise and descriptive.
- Do NOT invent features.
- Append the token " [ai-generated]" to the commit subject (so it is identifiable).
Here is the diff (already sanitized for secrets). If diff is large, focus on file names and key hunks.
