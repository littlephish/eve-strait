# Linux Flatpak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Eve-Strait as a Flatpak that installs from Flathub and updates itself through Flatpak, with no Windows assumptions reaching the user.

**Architecture:** No compilation. The Windows build exists as a Nuitka program folder because it has to survive without a Python runtime and because the in-app updater needs a folder it can swap. Inside a Flatpak neither is true: the runtime provides Python, and Flatpak owns updates. So the Flatpak ships plain Python source plus a pip-installed PySide6, which is the idiomatic shape and removes ~880 lines of Windows-bound updater from the delivered product rather than porting it.

**Tech Stack:** `org.freedesktop.Platform` 25.08, `flatpak-builder`, PySide6 wheel, AppStream, `.desktop`.

**Spec:** none — this plan is the design. The portability audit it rests on is in the conversation of 2026-09-17: 17,528 LOC total, ~880 Windows-bound, `config._data_home()` already falls back to `XDG_DATA_HOME`, `ai/bridge.py` already branches `AF_PIPE`/`AF_UNIX`, and no `subprocess` outside `update.py`.

## Runtime branch

**25.08**, verified against freedesktop-sdk's own tags on 2026-09-17.

A new major branch ships each August with a two-year support window, so
**24.08 reached EOL in August 2026** and must not be used -- it was this
plan's original choice. 26.08 exists (`freedesktop-sdk-26.08.1`) but is a
month old; 25.08 is at `.17`, is supported until August 2027, and is what the
PySide6 wheels and Flathub tooling have had time to settle against. Bump to
26.08 once it has mileage.

## Global Constraints

- **Nothing in this plan can be verified from a Windows machine.** Every build step needs running on Linux. Where a step says "expected", treat it as the thing to check, not a thing already known.
- App ID is `io.github.littlephish.EveStrait` everywhere — manifest filename, `.desktop`, AppStream `<id>`, icon filenames. Flathub rejects mismatches.
- The Windows build must keep working unchanged. Every guard added here is additive.
- Do not port the updater. Disable it and let Flatpak update the app.
- Tests stay Qt-free and must pass on Linux unchanged.

---

### Task 1: Make Windows-only behaviour opt-out, not assumed

**Files:**
- Modify: `src/eve_strait/update.py`
- Create: `tests/test_platform_guards.py`

**Interfaces:**
- Produces: `update.updates_supported() -> bool`

The updater is the only part that actively breaks off Windows: it looks for `eve-strait.exe`, shells to `powershell.exe`, and uses `CREATE_BREAKAWAY_FROM_JOB`. None of that should be reachable elsewhere, and under Flatpak the whole feature is wrong rather than merely unavailable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_platform_guards.py
"""The updater is Windows-only and must say so rather than fail oddly.

It swaps a program folder by polling a .exe file lock, shells to
powershell.exe and uses CREATE_BREAKAWAY_FROM_JOB. Inside a Flatpak the
feature is not merely unavailable, it is wrong: Flatpak owns updates.
"""
from eve_strait import update


def test_updates_unsupported_off_windows(monkeypatch):
    monkeypatch.setattr(update.sys, "platform", "linux")
    assert update.updates_supported() is False


def test_updates_unsupported_inside_a_flatpak(monkeypatch):
    """Even on a Linux build that could swap files, Flatpak owns updates."""
    monkeypatch.setattr(update.sys, "platform", "linux")
    monkeypatch.setenv("FLATPAK_ID", "io.github.littlephish.EveStrait")
    assert update.updates_supported() is False


def test_updates_supported_on_windows(monkeypatch):
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    assert update.updates_supported() is True


def test_auto_check_is_off_where_unsupported(monkeypatch):
    monkeypatch.setattr(update.sys, "platform", "linux")
    assert update.auto_check_enabled() is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_platform_guards.py -v`
Expected: FAIL, `AttributeError: module 'eve_strait.update' has no attribute 'updates_supported'`

- [ ] **Step 3: Implement**

Add near the top of `src/eve_strait/update.py`, after the imports:

```python
def updates_supported() -> bool:
    """Whether this build can update itself in place.

    Windows only, and not inside a Flatpak even then. The mechanism is a
    program-folder swap driven by polling a .exe file lock, with a
    powershell.exe fallback -- none of which exists elsewhere. Under Flatpak
    the feature is not missing, it is wrong: the sandbox is read-only and
    Flatpak does the updating.
    """
    if os.environ.get("FLATPAK_ID"):
        return False
    return sys.platform == "win32"
```

Ensure `import os` and `import sys` are present. Then make `auto_check_enabled()` return `False` when `updates_supported()` is False, before it reads any setting.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_platform_guards.py -v`
Expected: PASS

- [ ] **Step 5: Hide the menu items where unsupported**

In `src/eve_strait/ui/main_window.py`, where the Help menu adds *Check for updates...* and *Check for updates at startup*, wrap both in:

```python
        if _update.updates_supported():
            ...existing two actions...
```

`_update` is already imported in that block.

- [ ] **Step 6: Run the full suite and commit**

```bash
.venv/Scripts/python.exe -m pytest -q
git add src/eve_strait/update.py src/eve_strait/ui/main_window.py tests/test_platform_guards.py
git commit -m "feat(update): in-place updates are Windows-only, and never inside a Flatpak"
```

---

### Task 2: Desktop entry, icons and AppStream metadata

**Files:**
- Create: `dist_assets/linux/io.github.littlephish.EveStrait.desktop`
- Create: `dist_assets/linux/io.github.littlephish.EveStrait.metainfo.xml`
- Copy: `dist_assets/win/eve-strait.svg` → `dist_assets/linux/io.github.littlephish.EveStrait.svg`

Flathub requires all three and validates them in CI. An SVG already exists, which is the awkward asset to produce.

- [ ] **Step 1: Create the desktop entry**

```ini
[Desktop Entry]
Type=Application
Name=Eve-Strait
GenericName=EVE Online Route Planner
Comment=Plan capital and jump freighter routes in EVE Online
Exec=eve-strait
Icon=io.github.littlephish.EveStrait
Terminal=false
Categories=Game;StrategyGame;
Keywords=EVE;Online;jump;capital;freighter;route;wormhole;
StartupWMClass=eve-strait
```

- [ ] **Step 2: Create the AppStream metainfo**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>io.github.littlephish.EveStrait</id>
  <name>Eve-Strait</name>
  <summary>Plan capital and jump freighter routes in EVE Online</summary>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>GPL-3.0-or-later</project_license>
  <developer id="io.github.littlephish">
    <name>littlephish</name>
  </developer>
  <description>
    <p>
      A route planner for EVE Online capitals and jump freighters. It draws a
      pannable map of New Eden, shows jump range, and plans routes across
      stargates, jump drives, Ansiblex jump gates and scouted wormholes,
      computing fuel and jump-fatigue timing for every leg.
    </p>
    <p>Optionally signs in to ESI to use your own dockable structures.</p>
  </description>
  <launchable type="desktop-id">io.github.littlephish.EveStrait.desktop</launchable>
  <url type="homepage">https://github.com/littlephish/eve-strait</url>
  <url type="bugtracker">https://github.com/littlephish/eve-strait/issues</url>
  <content_rating type="oars-1.1"/>
  <screenshots>
    <screenshot type="default">
      <image>https://raw.githubusercontent.com/littlephish/eve-strait/main/docs/screenshot.png</image>
      <caption>Planning a jump freighter route out of Jita</caption>
    </screenshot>
  </screenshots>
  <releases>
    <release version="0.1.0" date="2026-09-17"/>
  </releases>
</component>
```

Confirm `project_license` matches the actual `LICENSE` file before submitting; the value above is a placeholder that must be checked, not assumed.

- [ ] **Step 3: Validate (on Linux)**

```bash
desktop-file-validate dist_assets/linux/io.github.littlethish.EveStrait.desktop
appstreamcli validate dist_assets/linux/io.github.littlephish.EveStrait.metainfo.xml
```

Expected: both clean. Fix anything reported — Flathub runs the same checks.

- [ ] **Step 4: Commit**

```bash
git add dist_assets/linux
git commit -m "build(linux): desktop entry, icon and AppStream metadata"
```

---

### Task 3: The Flatpak manifest

**Files:**
- Create: `dist_assets/linux/io.github.littlephish.EveStrait.yml`
- Create: `dist_assets/linux/README.md`

**Interfaces:**
- Produces: a manifest `flatpak-builder` can build

Runtime choice, and the reasoning to record: `org.kde.Platform` provides Qt6 but expects PySide6 built against exactly that Qt. The PySide6 PyPI wheel bundles its own Qt, which then fights the runtime's. Using `org.freedesktop.Platform` with the wheel avoids the version-matching problem entirely at the cost of ~200 MB of bundled Qt. Take the simpler, larger option first; revisit only if Flathub objects to the size.

- [ ] **Step 1: Write the manifest**

```yaml
app-id: io.github.littlephish.EveStrait
runtime: org.freedesktop.Platform
runtime-version: '25.08'
sdk: org.freedesktop.Sdk
command: eve-strait

finish-args:
  # Qt needs a display; both are listed so Wayland is preferred and X11 works.
  - --socket=wayland
  - --socket=fallback-x11
  - --share=ipc
  - --device=dri
  # ESI, the SDE download from Fuzzwork, EVE-Scout, and the OAuth callback
  # this app serves on localhost:8635.
  - --share=network
  # Opening the SSO page in the user's browser goes through the portal.
  - --talk-name=org.freedesktop.portal.OpenURI
  # Credential storage for mapper logins (keyring -> SecretService).
  - --talk-name=org.freedesktop.secrets

modules:
  - name: python-deps
    buildsystem: simple
    build-commands:
      - pip3 install --no-index --find-links="file://${PWD}" --prefix=${FLATPAK_DEST} PySide6 requests keyring
    sources:
      # Generated by flatpak-pip-generator; see dist_assets/linux/README.md.
      - type: file
        path: pypi-deps.json

  - name: eve-strait
    buildsystem: simple
    build-commands:
      - pip3 install --no-deps --prefix=${FLATPAK_DEST} .
      - install -Dm644 dist_assets/linux/io.github.littlephish.EveStrait.desktop
        ${FLATPAK_DEST}/share/applications/io.github.littlephish.EveStrait.desktop
      - install -Dm644 dist_assets/linux/io.github.littlephish.EveStrait.metainfo.xml
        ${FLATPAK_DEST}/share/metainfo/io.github.littlephish.EveStrait.metainfo.xml
      - install -Dm644 dist_assets/linux/io.github.littlephish.EveStrait.svg
        ${FLATPAK_DEST}/share/icons/hicolor/scalable/apps/io.github.littlephish.EveStrait.svg
    sources:
      - type: dir
        path: ../..
```

- [ ] **Step 2: Document how the dependency list is generated**

Create `dist_assets/linux/README.md`:

```markdown
# Flatpak build

Flathub builds offline, so every Python dependency must be declared with a
hash rather than resolved at build time. Regenerate after any dependency
change in `pyproject.toml`:

    pip install flatpak-pip-generator
    flatpak-pip-generator --runtime=org.freedesktop.Sdk//25.08 \
        PySide6 requests keyring --output pypi-deps

Build and run locally:

    flatpak-builder --user --install --force-clean build-dir \
        io.github.littlephish.EveStrait.yml
    flatpak run io.github.littlephish.EveStrait

Validate before submitting:

    flatpak run org.flatpak.Builder --lint manifest io.github.littlephish.EveStrait.yml
```

- [ ] **Step 3: Build it (on Linux)**

Run the `flatpak-builder` command above.
Expected: a build that completes and an app that launches to the map.

Three things to check specifically, because they are the ones the sandbox
breaks and the Windows build never exercises:

1. First launch downloads the SDE into the sandboxed data dir.
2. **ESI login** — the browser opens via the portal and the callback on
   `localhost:8635` is received inside the sandbox.
3. `config._data_home()` resolves under `~/.var/app/io.github.littlephish.EveStrait/`.

- [ ] **Step 4: Commit**

```bash
git add dist_assets/linux
git commit -m "build(linux): Flatpak manifest"
```

---

### Task 4: Run the test suite on Linux in CI

**Files:**
- Modify: `.github/workflows/ci.yml`

CI is `windows-latest` only, so nothing would catch a Linux regression. The suite is Qt-free and has no network, so this is close to free.

- [ ] **Step 1: Add a matrix**

Change the job to:

```yaml
    strategy:
      fail-fast: false
      matrix:
        os: [windows-latest, ubuntu-latest]
    runs-on: ${{ matrix.os }}
```

- [ ] **Step 2: Verify the byte-compile and import steps are shell-agnostic**

The existing steps use `python -m compileall` and an import check. If either
is written in PowerShell syntax, give it `shell: bash` or rewrite it.

- [ ] **Step 3: Push and confirm both legs pass**

Expected: 238 tests green on both.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run the test suite on Linux as well as Windows"
```

---

### Task 5: Build the Flatpak in CI

**Files:**
- Create: `.github/workflows/flatpak.yml`

- [ ] **Step 1: Add the workflow**

```yaml
name: Flatpak
on:
  push:
    tags: ['v*']
  pull_request:
    paths: ['dist_assets/linux/**', 'pyproject.toml', 'src/**']

jobs:
  flatpak:
    runs-on: ubuntu-latest
    container:
      image: bilelmoussaoui/flatpak-github-actions:freedesktop-25.08
      options: --privileged
    steps:
      - uses: actions/checkout@v4
      - uses: flatpak/flatpak-github-actions/flatpak-builder@v6
        with:
          bundle: eve-strait.flatpak
          manifest-path: dist_assets/linux/io.github.littlephish.EveStrait.yml
          cache-key: flatpak-builder-${{ github.sha }}
```

- [ ] **Step 2: Attach the bundle to tagged releases**

In `release.yml`, add the produced `eve-strait.flatpak` to the release
artifacts. Keep it a separate job from the Windows build so a Flatpak
failure cannot block the Windows release, which is the one users have today.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows
git commit -m "ci: build a Flatpak bundle on tags"
```

---

---

### Task 6: Decide how updates reach users

**Files:**
- Modify: `README.md` (install and update instructions)
- Create: `dist_assets/linux/FLATHUB.md` (submission notes)

**This is the task that makes the Flatpak worth doing**, and the one most
easily missed: the bundle Task 5 produces **does not update**. A `.flatpak`
file is a one-shot install. A user who installs it that way is frozen on that
version forever and will never be told otherwise — strictly worse than the
Windows build, which at least checks GitHub.

Three ways to actually deliver updates:

| | How users get updates | Cost |
|---|---|---|
| **Flathub** | `flatpak update`, or silently via GNOME Software / KDE Discover | Free hosting, one-time review, each release is a PR to the flathub repo |
| **Self-hosted OSTree repo** | `flatpak remote-add` once, then updates like any remote | You host and sign it; GPG key management is yours forever |
| **Bundle only** | **They don't** | Nothing, and it shows |

**Recommendation: Flathub.** It *is* the update mechanism, it is free, and it
puts the app in the software centre where Linux users look. The review queue
is the only real cost, and it is one-time. Keep producing the bundle from
Task 5 as a convenience for people who will not add a remote, but do not treat
it as distribution.

- [ ] **Step 1: Submit to Flathub**

Fork `flathub/flathub`, branch named exactly `io.github.littlephish.EveStrait`,
add the manifest, open a PR. Expect review comments on two things, so have the
answers ready:

* `--share=network` — ESI, the Fuzzwork SDE download, EVE-Scout, and the
  OAuth callback this app serves on `localhost:8635`.
* `--talk-name=org.freedesktop.secrets` — mapper credential storage, so a
  Tripwire password is never written to disk in plaintext.

- [ ] **Step 2: Point the manifest at tags, not at the working tree**

The manifest in Task 3 uses `type: dir` for local iteration. Flathub builds
from a pinned source, so the published manifest uses:

```yaml
    sources:
      - type: git
        url: https://github.com/littlephish/eve-strait.git
        tag: v0.2.0
        commit: <full sha of that tag>
        x-checker-data:
          type: git
          tag-pattern: "^v([\\d.]+)$"
```

`x-checker-data` is what makes Flathub's bot open an automatic PR when a new
tag appears, so releasing stays "push a tag" as it is today.

- [ ] **Step 3: Make the release workflow reflect two channels**

A tag now produces a Windows installer *and* a Flathub release, on different
timelines — Flathub builds after its bot's PR merges, which is not instant.
Note this in the release workflow so nobody waits for a Flatpak that is
queued behind a review.

- [ ] **Step 4: Update the README**

Add a Linux section: install from Flathub, one line to install, and state
plainly that updates arrive through Flatpak rather than the in-app updater
(which is hidden on Linux by Task 1).

- [ ] **Step 5: Commit**

```bash
git add README.md dist_assets/linux/FLATHUB.md
git commit -m "docs(linux): install and update via Flathub"
```

## Verify before release

- [ ] ESI login completes inside the sandbox, including the `localhost:8635`
      callback.
- [ ] Data lands under `~/.var/app/io.github.littlephish.EveStrait/` and the
      legacy-directory migration in `config.py` does not fire spuriously.
- [ ] The Help menu shows no update items.
- [ ] `flatpak run org.flatpak.Builder --lint` is clean.
- [ ] The Windows build and installer are byte-for-byte unaffected.
