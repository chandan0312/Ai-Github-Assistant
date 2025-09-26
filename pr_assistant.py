#!/usr/bin/env python3
import os, json, textwrap, re
import openai
from github import Github
import yaml

# --- helpers
def sanitize_text(s, max_len=6000):
    s = re.sub(r'-----BEGIN .*?-----.*?-----END .*?-----', '[REDACTED_PRIVATE_KEY]', s, flags=re.S)
    s = re.sub(r'(?i)(api_key|apikey|token)[\'\"\s:=]+[A-Za-z0-9\-_]{8,}', r'\1=[REDACTED]', s)
    if len(s) > max_len:
        s = s[:max_len] + '\n...[truncated]...'
    return s

event_path = os.environ.get("GITHUB_EVENT_PATH")
if not event_path:
    print("GITHUB_EVENT_PATH missing")
    exit(1)

with open(event_path) as f:
    ev = json.load(f)

pr = ev.get("pull_request")
if not pr:
    print("No pull_request payload.")
    exit(0)

repo_name = os.environ.get("GITHUB_REPOSITORY")
gh = Github(os.environ.get("GITHUB_TOKEN"))
repo = gh.get_repo(repo_name)
pr_number = pr["number"]
pull = repo.get_pull(pr_number)

# collect changed files and small patches
files = pull.get_files()
file_summaries = []
for f in files:
    patch = getattr(f, "patch", None)
    if patch:
        # keep only top N lines per file
        patch = "\n".join(patch.splitlines()[:200])
    else:
        patch = "(binary or large file)"
    file_summaries.append({"filename": f.filename, "patch": sanitize_text(patch, max_len=2000)})

# prompt for AI: request structured JSON for easy parsing
openai.api_key = os.environ.get("OPENAI_API_KEY")
title = pr.get("title","")
body = pr.get("body") or ""
changed_files_text = "\n".join([f"{x['filename']}\n{ x['patch'][:800] }" for x in file_summaries])

prompt = textwrap.dedent(f"""
You are an automated PR assistant. Given the PR title and diff snippets below, return a JSON object with:
- summary: short 3-line summary what changed
- risk: short bullet list of potential risks / things to check
- suggested_reviewers: list of github usernames (strings) (max 3) that would be the best reviewers based on files changed (use heuristics like files under 'frontend', 'backend', 'docs')
- suggested_labels: list of labels (like 'bug', 'feat', 'docs', 'chore', 'security')
- tests: short test instructions to validate this PR

PR Title:
{title}

PR Body:
{body}

Changed files and small diffs:
{changed_files_text}

Return only valid JSON.
""")

resp = openai.ChatCompletion.create(
    model="gpt-4",
    messages=[{"role":"system","content":"You are a helpful PR assistant."},
              {"role":"user","content":prompt}],
    max_tokens=650,
    temperature=0.0
)

content = resp["choices"][0]["message"]["content"].strip()

# try to extract JSON from content
import json, re
m = re.search(r'(\{.*\})', content, re.S)
if not m:
    print("AI did not return JSON, falling back to short summary.")
    summary_text = content
    result = {"summary": summary_text, "risk": [], "suggested_reviewers": [], "suggested_labels": [], "tests": []}
else:
    try:
        result = json.loads(m.group(1))
    except Exception as e:
        print("Failed to parse JSON from AI:", e)
        result = {"summary": content, "risk": [], "suggested_reviewers": [], "suggested_labels": [], "tests": []}

# create or update a PR comment
marker = "<!-- ai-pr-summary-v1 -->"
comment_body = marker + "\n\n" + f"**AI PR Summary**\n\n**Summary:**\n{result.get('summary')}\n\n**Risks / Notes:**\n{result.get('risk')}\n\n**Test instructions:**\n{result.get('tests')}\n\n**Suggested reviewers:** {result.get('suggested_reviewers')}\n\n**Suggested labels:** {result.get('suggested_labels')}\n"

# look for existing AI comment
comments = pull.get_issue_comments()
updated = False
for c in comments:
    if marker in c.body and c.user.login.endswith("github-actions[bot]") or marker in c.body:
        c.edit(comment_body)
        updated = True
        break
if not updated:
    pull.create_issue_comment(comment_body)

# assign reviewers (best-effort)
suggested = result.get("suggested_reviewers", []) or []
# optional mapping file to map roles to usernames (repo/src path mapping). Load .github/ai/reviewers_map.yaml if present
map_path = ".github/ai/reviewers_map.yaml"
try:
    contents = repo.get_contents(map_path)
    import base64
    raw = base64.b64decode(contents.content).decode()
    reviewers_map = yaml.safe_load(raw)
except Exception:
    reviewers_map = {}

# If AI returned logical team names instead of usernames, map them
mapped_reviewers = []
for r in suggested:
    if r in reviewers_map:
        mapped_reviewers.extend(reviewers_map[r])
    else:
        mapped_reviewers.append(r)

# de-duplicate and ensure they are collaborators
final_reviewers = []
for u in mapped_reviewers:
    if u and u not in final_reviewers:
        try:
            repo.get_collaborator(u)
            final_reviewers.append(u)
        except Exception:
            # skip unknown user
            pass

if final_reviewers:
    try:
        pull.create_review_request(reviewers=final_reviewers)
    except Exception as e:
        print("Could not create review request:", e)

# add labels
labels = result.get("suggested_labels", [])
if labels:
    try:
        pull.add_to_labels(*labels)
    except Exception as e:
        print("Failed to add labels (they may not exist):", e)

print("PR assistant finished.")
