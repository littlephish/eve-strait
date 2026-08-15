---
name: release-eve-strait
description: Cut and push an Eve-Strait release - verify main is clean and CI is green, tag the next version, and push the tag to trigger the Windows build. Use when the user says "push a release", "release it", "cut a release", "ship this", or asks to tag and publish a new version.
---

# Releasing Eve-Strait

Follow these steps **in order**. Stop and report at the first one that
fails rather than pushing on to the next — a broken step means the release
is not ready, not that the process should route around it.

## Facts this skill was written against

Re-check anything here that looks off before trusting it; repos change.

- Default branch: **`main`**.
- `.github/workflows/release.yml` triggers **only** on `push: tags: ["v*"]`
  (deliberately — see its own header comment). It stamps the version into
  the code from the tag itself during the CI run; nothing needs stamping
  locally before tagging.
- `.github/workflows/ci.yml` triggers on push to `main` and is the real
  answer to "is main green" — byte-compiles the package and imports it.
  It is a fast sanity gate, not a test suite: there is no `tests/` directory
  committed to this repo, so this cannot and does not claim to run one.
  Before this file existed, nothing ran on a push to main at all; the
  "Push on main" entries visible in the Actions tab are GitHub's own
  default CodeQL scan, unrelated to whether the app builds.
- No `gh` CLI is installed on this machine. Check run status through the
  public GitHub API instead (the repo is public, so this needs no token) —
  the exact commands are below.
- Existing tags (`v0.2.0` … `v0.5.1`) are **lightweight**, not annotated —
  match that: `git tag vX.Y.Z`, not `git tag -a`.
- Remote: `origin`. The URL uses an SSH host alias
  (`git@github-littlephish:...`), already resolved by the user's SSH config
  — always push through `origin`, never a literal `github.com` URL.

## Non-negotiables

- Never `git push --force` / `--force-with-lease` to `main`.
- Never amend or rebase existing commits as part of a release.
- Never skip hooks (`--no-verify`) or bypass signing.
- Only ever tag a commit that is actually on `origin/main`, confirmed by
  fetching first — not just "looks right locally".
- If the working tree is dirty, **stop**. This skill releases what is
  already committed; it does not decide what should be in the release.

## Steps

### 1. Confirm main is clean and in sync

```bash
git status --short
git fetch origin main
git log --oneline main..origin/main    # must be empty: local is not behind
git log --oneline origin/main..main    # commits waiting to be pushed, if any
```

- Dirty working tree → stop, report what is uncommitted, ask what to do
  with it. Do not commit unrelated work on the user's behalf.
- `main` behind `origin/main` → stop. Something landed remotely that this
  session does not know about; do not merge blindly as part of a release.
- `main` ahead of `origin/main` → push it: `git push origin main`.

### 2. Confirm CI is green for that exact commit

No `gh` CLI here, so poll the public REST API directly:

```bash
sha=$(git rev-parse origin/main)
curl -s "https://api.github.com/repos/littlephish/eve-strait/actions/runs?branch=main&per_page=5" \
  | python -c "
import json, sys
sha = '$sha'
runs = json.load(sys.stdin)['workflow_runs']
mine = [r for r in runs if r['head_sha'] == sha and r.get('path') == '.github/workflows/ci.yml']
if not mine:
    print('NO CI RUN YET for', sha[:8])
else:
    r = mine[0]
    print(r['status'], r.get('conclusion'), r['html_url'])
"
```

- No run yet for that SHA → CI usually starts within seconds of the push;
  wait a short while and poll again rather than assuming it will never run.
- `status` is `in_progress`/`queued` → wait and poll again.
- `conclusion` is anything other than `success` → **stop**. Report the
  failure and the run URL. Do not tag a commit CI says is broken.
- `success` → continue.

### 3. Work out the next version

```bash
git tag --sort=-v:refname | head -1
```

Default is a **patch** bump: `vX.Y.Z` → `vX.Y.(Z+1)`.

Bump **minor** instead — `vX.Y.Z` → `vX.(Y+1).0` — only for a genuinely
significant change (a new major feature area, a breaking change, a big
architectural shift). This is a judgement call: state which one was chosen
and why in one line before tagging, so it is easy to correct if wrong.
Nothing here jumps to a new **major** version on its own; that is a
deliberate choice the user makes explicitly, not an inferred one.

### 4. Tag and push

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

Lightweight tag, matching every existing tag in this repo — no `-a`, no
`-m`. This push is what triggers `release.yml`.

### 5. Confirm the release build actually succeeded

Tagging only *starts* the build; it does not mean the release exists yet.
Poll the same way as step 2, but for the `Release` workflow this time:

```bash
# Same commit as the tag (a lightweight tag points at it directly), so this
# matches on head_sha exactly like step 2 -- not on display_title, which is
# the commit *message*, not the tag ("Wip" for a real commit here). Checked
# against the live API before trusting it, since that field name looked
# plausible and was wrong.
curl -s "https://api.github.com/repos/littlephish/eve-strait/actions/runs?per_page=5" \
  | python -c "
import json, sys
sha = '$sha'
runs = json.load(sys.stdin)['workflow_runs']
mine = [r for r in runs if r['name'] == 'Release' and r['head_sha'] == sha]
if not mine:
    print('NO RELEASE RUN YET for', sha[:8])
else:
    r = mine[0]
    print(r['status'], r.get('conclusion'), r['html_url'])
"
```

A Nuitka standalone build is several minutes, not seconds — keep polling
rather than declaring victory right after the push. Report the final
status and the run URL either way, success or failure.

## Report back

End with a short, plain summary: the version tagged, the CI run that
gated it, the release-build run and whether it passed, and a link to
the release itself
(`https://github.com/littlephish/eve-strait/releases/tag/vX.Y.Z`) once it
exists.
