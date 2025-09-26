#!/usr/bin/env python3
import os, subprocess, re, json, textwrap
from github import Github
import openai
import semver

def run(cmd):
    return subprocess.check_output(cmd, shell=True, text=True).strip()

# get last tag
try:
    run("git fetch --tags")
    last_tag = run('git describe --tags --abbrev=0')
except Exception:
    last_tag = None

if last_tag:
    range_spec = f"{last_tag}..HEAD"
else:
    # no tags exist, get all commits
    range_spec = "HEAD"

# get commit messages since last tag
try:
    commits_text = run(f'git log {range_spec} --pretty=format:%s%n%b---END---')
except Exception as e:
    print("Error retrieving log:", e)
    commits_text = ""

# simple heuristics
lower = commits_text.lower()
bump = None
if "breaking change" in lower or re.search(r'!\)', commits_text):
    bump = "major"
elif re.search(r'(^feat|^feature|^add)', lower, re.M):
    bump = "minor"
elif re.search(r'(^fix|^bug|^patch)', lower, re.M):
    bump = "patch"

# If ambiguous, call AI to suggest
if not bump:
    openai.api_key = os.environ.get("OPENAI_API_KEY")
    if not openai.api_key:
        print("OPENAI_API_KEY missing; defaulting to patch.")
        bump = "patch"
    else:
        prompt = textwrap.dedent(f"""
        Given the following commit messages, recommend the semantic version bump: one of "major","minor","patch".
        Provide just the word and a one-line justification.
        Commits:
        {commits_text[:4000]}
        """)
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role":"system","content":"You are a semver assistant."},
                      {"role":"user","content":prompt}],
            max_tokens=80,
            temperature=0.0
        )
        text = resp["choices"][0]["message"]["content"].strip().lower()
        if "major" in text:
            bump = "major"
        elif "minor" in text:
            bump = "minor"
        else:
            bump = "patch"

# compute next version
if last_tag:
    tag_version = last_tag.lstrip('v')
    try:
        ver = semver.VersionInfo.parse(tag_version)
    except Exception:
        # fallback
        ver = semver.VersionInfo.parse("0.0.0")
else:
    ver = semver.VersionInfo.parse("0.0.0")

if bump == "major":
    new_ver = ver.bump_major()
elif bump == "minor":
    new_ver = ver.bump_minor()
else:
    new_ver = ver.bump_patch()

new_tag = f"v{new_ver}"

# create annotated tag and push
try:
    run(f'git tag -a {new_tag} -m "Release {new_tag}"')
    # push tag using REPO_PAT if provided for permissions else GITHUB_TOKEN
    pat = os.environ.get("REPO_PAT")
    if pat:
        # ensure correct remote auth for pushing tags (use https + token)
        repo_url = os.environ.get("GITHUB_REPOSITORY")
        run(f'git push https://{pat}@github.com/{repo_url}.git {new_tag}')
    else:
        run(f'git push origin {new_tag}')
    print(f"Pushed tag {new_tag}")
except Exception as e:
    print("Failed to push tag:", e)
    # exit non-zero to fail the workflow if desired
    exit(1)

# create GitHub release
gh = Github(os.environ.get("GITHUB_TOKEN"))
repo = gh.get_repo(os.environ.get("GITHUB_REPOSITORY"))
release_body = f"Automated release {new_tag}\n\nCommits since {last_tag}:\n\n{commits_text[:3000]}"
repo.create_git_release(new_tag, new_tag, release_body, draft=False, prerelease=False)
print(f"Created release {new_tag}")
