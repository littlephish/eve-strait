# macOS Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Eve-Strait as a signed, notarized macOS `.app` in a DMG that opens without Gatekeeper complaining.

**Architecture:** Nuitka already produces the Windows program folder; it can also produce a macOS app bundle, so the build script is shared rather than replaced. The in-app updater is not ported — it is a Windows program-folder swap and has no meaning for a signed bundle. macOS users are pointed at the releases page instead.

**Tech Stack:** Nuitka `--macos-create-app-bundle`, `codesign`, `notarytool`, `create-dmg`, GitHub `macos-latest` runners.

**Spec:** none — this plan is the design, resting on the portability audit of 2026-09-17: ~880 of 17,528 LOC are Windows-bound, `config._data_home()` already falls back off Windows, `ai/bridge.py` already branches `AF_PIPE`/`AF_UNIX`, and there is no `subprocess` outside `update.py`.

## Read this before starting

**This port costs money and a machine, and the cost recurs.** Be sure before beginning:

- **Apple Developer Program: $99/year.** Without it you cannot notarize, and without notarization Gatekeeper tells users the app *"is damaged and can't be opened"* — which is worse than not shipping a macOS build at all.
- You need a Mac, or `macos-latest` CI for every build and every test.
- Certificates expire and notarization fails in novel ways, usually at release time.
- Apple Silicon and Intel are separate architectures. Decide early (see Task 3).

If any of that is unwelcome, **do the Flatpak plan instead** — it is mostly deletion plus a manifest, and costs nothing.

## Global Constraints

- **Nothing here can be verified from a Windows machine.** Every build and signing step needs a Mac. "Expected" means "check this", not "this is known".
- The Windows build must keep working unchanged. Every change is additive.
- Do not port the updater.
- Secrets (certificate, password, Apple ID, team ID) live in GitHub Actions secrets and never in the repo.

---

### Task 1: Platform guards

**Files:**
- Modify: `src/eve_strait/update.py`
- Modify: `src/eve_strait/ui/main_window.py`
- Create: `tests/test_platform_guards.py`

**If the Linux Flatpak plan has already been done, this task is complete** —
`updates_supported()` returns False for any non-Windows platform, macOS
included. Verify and move on.

Otherwise implement Task 1 of `2026-09-17-linux-flatpak.md` exactly: the
guard is the same one, and duplicating it with different logic is how the two
ports drift.

- [ ] **Step 1: Check whether the guard exists**

```bash
grep -n "def updates_supported" src/eve_strait/update.py
```

If present, run `.venv/Scripts/python.exe -m pytest tests/test_platform_guards.py -q` and skip to Task 2.

- [ ] **Step 2: If absent, implement Flatpak plan Task 1, then return here.**

---

### Task 2: A macOS icon

**Files:**
- Create: `dist_assets/macos/eve-strait.icns`

macOS needs `.icns`; the repo has `dist_assets/win/eve-strait.svg` and
`src/eve_strait/assets/icon.png`.

- [ ] **Step 1: Generate the iconset (on macOS)**

```bash
mkdir -p /tmp/EveStrait.iconset
for s in 16 32 64 128 256 512; do
  sips -z $s $s src/eve_strait/assets/icon.png \
    --out /tmp/EveStrait.iconset/icon_${s}x${s}.png
  sips -z $((s*2)) $((s*2)) src/eve_strait/assets/icon.png \
    --out /tmp/EveStrait.iconset/icon_${s}x${s}@2x.png
done
iconutil -c icns /tmp/EveStrait.iconset -o dist_assets/macos/eve-strait.icns
```

If `icon.png` is smaller than 1024×1024 the large sizes will be upscaled and
look soft. Render from the SVG instead if so:

```bash
rsvg-convert -w 1024 -h 1024 dist_assets/win/eve-strait.svg -o /tmp/icon1024.png
```

- [ ] **Step 2: Commit**

```bash
git add dist_assets/macos/eve-strait.icns
git commit -m "build(macos): app icon"
```

---

### Task 3: Build an app bundle

**Files:**
- Create: `scripts/build_app.sh`

**Interfaces:**
- Produces: `dist/Eve-Strait.app`

**Architecture decision, to make now rather than discover later.** Nuitka's
`--macos-create-app-bundle` builds for the host architecture. A universal
binary needs building both and `lipo`-ing them, which doubles build time and
is fiddly with bundled Qt. Recommendation: **build arm64 only** initially.
Apple Silicon has been the only Mac sold since 2023, and Rosetta does not help
because it is the *binary* that would need to be x86_64. Revisit if Intel users
actually appear.

- [ ] **Step 1: Write the build script**

```bash
#!/usr/bin/env bash
# Build Eve-Strait.app for macOS with Nuitka.
#
# Mirrors the Nuitka invocation in .github/workflows/release.yml, minus the
# Windows-only flags and plus the bundle ones. Nuitka is pinned to the same
# version as the Windows build so the two are compiled by the same compiler.
set -euo pipefail

VERSION="${1:-0.1.0}"
python3 -m pip install --upgrade pip
python3 -m pip install PySide6 requests nuitka==4.2.1 zstandard ordered-set
python3 -m pip install .

python3 -m nuitka \
  --standalone \
  --enable-plugin=pyside6 \
  --include-package=eve_strait \
  --include-package-data=eve_strait \
  --assume-yes-for-downloads \
  --macos-create-app-bundle \
  --macos-app-name="Eve-Strait" \
  --macos-app-version="${VERSION}" \
  --macos-app-icon=dist_assets/macos/eve-strait.icns \
  --company-name=Eve-Strait \
  --product-name=Eve-Strait \
  --file-description="EVE Online capital jump route planner" \
  --copyright=Eve-Strait \
  --output-dir=out \
  app.py

rm -rf dist/Eve-Strait.app
mkdir -p dist
mv out/app.app dist/Eve-Strait.app
echo "built dist/Eve-Strait.app"
```

- [ ] **Step 2: Build and launch it (on macOS)**

```bash
chmod +x scripts/build_app.sh
./scripts/build_app.sh 0.1.0
open dist/Eve-Strait.app
```

Expected: the map window opens. Check specifically, since none of it is
exercised by the Windows build:

1. First launch downloads the SDE.
2. Data lands under `~/.local/share/eve-strait` — `_data_home()` has no
   `LOCALAPPDATA` and no `XDG_DATA_HOME` on macOS, so it takes the
   `~/.local/share` branch. **This is not the macOS convention**
   (`~/Library/Application Support`). Decide whether to accept it or add a
   Darwin branch; accepting it is defensible for a cross-platform tool and
   avoids a migration, but note the choice in `config.py` either way.
3. **ESI login** — the browser opens and the `localhost:8635` callback lands.
4. The Help menu shows no update items.

- [ ] **Step 3: Commit**

```bash
git add scripts/build_app.sh
git commit -m "build(macos): app bundle via Nuitka"
```

---

### Task 4: Sign and notarize

**Files:**
- Create: `dist_assets/macos/entitlements.plist`
- Modify: `scripts/build_app.sh`

This is the task that costs money and the one that fails at inconvenient
times. Everything before it produces an app that runs on *your* Mac; only this
produces one that runs on someone else's.

- [ ] **Step 1: Entitlements**

Qt apps need the JIT/unsigned-memory entitlements under the hardened runtime,
which notarization requires.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.allow-jit</key><true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key><true/>
  <key>com.apple.security.cs.disable-library-validation</key><true/>
</dict>
</plist>
```

- [ ] **Step 2: Sign**

```bash
codesign --force --deep --options runtime --timestamp \
  --entitlements dist_assets/macos/entitlements.plist \
  --sign "Developer ID Application: YOUR NAME (TEAMID)" \
  dist/Eve-Strait.app

codesign --verify --deep --strict --verbose=2 dist/Eve-Strait.app
```

`--deep` is deprecated by Apple but still the pragmatic option for a bundle
full of Qt dylibs. If it fails, sign nested binaries inner-to-outer instead.

- [ ] **Step 3: Notarize**

```bash
ditto -c -k --keepParent dist/Eve-Strait.app /tmp/EveStrait.zip
xcrun notarytool submit /tmp/EveStrait.zip \
  --apple-id "$APPLE_ID" --team-id "$TEAM_ID" \
  --password "$APP_SPECIFIC_PASSWORD" --wait
xcrun stapler staple dist/Eve-Strait.app
xcrun stapler validate dist/Eve-Strait.app
```

`--wait` blocks until Apple answers, typically minutes. On rejection,
`xcrun notarytool log <submission-id>` gives the reason — usually an unsigned
nested binary.

- [ ] **Step 4: Verify as a user would**

On a **different** Mac that has never seen the app:

```bash
spctl -a -vvv -t install dist/Eve-Strait.app
```

Expected: `accepted, source=Notarized Developer ID`. Testing on the build
machine proves nothing — it already trusts your certificate.

- [ ] **Step 5: Commit (entitlements only, never secrets)**

```bash
git add dist_assets/macos/entitlements.plist scripts/build_app.sh
git commit -m "build(macos): hardened-runtime signing and notarization"
```

---

### Task 5: DMG

**Files:**
- Modify: `scripts/build_app.sh`

- [ ] **Step 1: Produce a DMG**

```bash
brew install create-dmg
create-dmg \
  --volname "Eve-Strait" \
  --icon "Eve-Strait.app" 175 190 \
  --app-drop-link 425 190 \
  --window-size 600 400 \
  "dist/Eve-Strait-${VERSION}-macos-arm64.dmg" \
  "dist/Eve-Strait.app"
```

- [ ] **Step 2: Sign and staple the DMG too**

A notarized app inside an unsigned DMG still warns on the DMG itself.

```bash
codesign --sign "Developer ID Application: YOUR NAME (TEAMID)" \
  "dist/Eve-Strait-${VERSION}-macos-arm64.dmg"
xcrun stapler staple "dist/Eve-Strait-${VERSION}-macos-arm64.dmg"
```

- [ ] **Step 3: Commit**

```bash
git add scripts/build_app.sh
git commit -m "build(macos): DMG packaging"
```

---

### Task 6: CI

**Files:**
- Create: `.github/workflows/release-macos.yml`

A separate workflow from `release.yml`, deliberately: a notarization failure
must not block the Windows release, which is the one users have today.

- [ ] **Step 1: Store the secrets**

In repository settings, add: `MACOS_CERT_P12` (base64 of the exported
Developer ID certificate), `MACOS_CERT_PASSWORD`, `APPLE_ID`, `TEAM_ID`,
`APP_SPECIFIC_PASSWORD`.

- [ ] **Step 2: Add the workflow**

```yaml
name: Release (macOS)
on:
  push:
    tags: ['v*']

jobs:
  macos:
    runs-on: macos-latest      # arm64
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }

      - name: Stamp the version
        run: python scripts/version.py "${GITHUB_REF_NAME#v}"

      - name: Import signing certificate
        env:
          CERT: ${{ secrets.MACOS_CERT_P12 }}
          CERT_PW: ${{ secrets.MACOS_CERT_PASSWORD }}
        run: |
          echo "$CERT" | base64 --decode > /tmp/cert.p12
          security create-keychain -p actions build.keychain
          security default-keychain -s build.keychain
          security unlock-keychain -p actions build.keychain
          security import /tmp/cert.p12 -k build.keychain \
            -P "$CERT_PW" -T /usr/bin/codesign
          security set-key-partition-list -S apple-tool:,apple:,codesign: \
            -s -k actions build.keychain
          rm /tmp/cert.p12

      - name: Build, sign, notarize, package
        env:
          APPLE_ID: ${{ secrets.APPLE_ID }}
          TEAM_ID: ${{ secrets.TEAM_ID }}
          APP_SPECIFIC_PASSWORD: ${{ secrets.APP_SPECIFIC_PASSWORD }}
        run: ./scripts/build_app.sh "${GITHUB_REF_NAME#v}"

      - uses: softprops/action-gh-release@v2
        with:
          draft: true
          files: dist/*.dmg
```

`scripts/version.py` already exists and is what the Windows workflow uses;
skipping the stamp is how a release once reported the wrong version.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release-macos.yml
git commit -m "ci(macos): signed and notarized DMG on tags"
```

---

---

### Task 7: Decide how updates reach users

**Files:**
- Modify: `src/eve_strait/update.py`
- Modify: `src/eve_strait/ui/main_window.py`
- Create: `dist_assets/macos/eve-strait.rb` (Homebrew cask)
- Modify: `README.md`

A DMG on a releases page does not update. Without this task a macOS user
installs 0.2.0 and stays on it silently — worse than the Windows build, which
at least checks GitHub.

Four options, and one of them is a trap:

| | How users update | Verdict |
|---|---|---|
| **Homebrew Cask** | `brew upgrade --cask eve-strait` | **Recommended.** The macOS equivalent of Flathub. Zero in-app code, no signing beyond what Task 4 already does |
| **Notify only** | App says "0.3.0 is out", opens the releases page | **Also do this.** Nearly free — see below |
| **Sparkle** | In-app download and replace | The standard macOS framework, but Objective-C embedded in the bundle and driven from Python. A third update mechanism to maintain. Not worth it |
| **Port the folder swap** | — | **Trap.** Modifying a signed `.app` in place invalidates its signature, and Gatekeeper then refuses to launch it. Do not |

**The notify path is nearly free because the check already exists.**
`update.py:35` queries `api.github.com/repos/{repo}/releases/latest` and
`check()` already compares versions. Only the *install* half is Windows-bound.

- [ ] **Step 1: Split the guard, which conflates two different questions**

Task 1 gave one predicate for "can this build update itself". That is really
two questions, and macOS answers them differently:

```python
def update_check_supported() -> bool:
    """Whether to look for a newer release at all.

    Anywhere except a Flatpak, where the software centre already tells the
    user and a second notice is noise.
    """
    return not os.environ.get("FLATPAK_ID")


def update_install_supported() -> bool:
    """Whether this build can install an update over itself.

    Windows only. The mechanism swaps a program folder by polling a .exe
    file lock -- there is no equivalent elsewhere, and on macOS modifying a
    signed .app in place invalidates its signature, after which Gatekeeper
    refuses to launch it at all.
    """
    return updates_supported()
```

Keep `updates_supported()` as the Windows-only install predicate so Task 1's
tests stay valid, and add a test that macOS checks but does not install.

- [ ] **Step 2: Make the menu action match the platform**

*Check for updates…* stays visible wherever `update_check_supported()`. What
it *does* on a newer version differs:

* Windows — the existing download-and-swap dialog.
* macOS — a dialog naming the new version, with **Open releases page**, and a
  line reading `brew upgrade --cask eve-strait` for cask installs.

Never offer an in-place install where `update_install_supported()` is False.

- [ ] **Step 3: Write the cask**

```ruby
cask "eve-strait" do
  version "0.2.0"
  sha256 "<sha256 of the DMG>"

  url "https://github.com/littlephish/eve-strait/releases/download/v#{version}/Eve-Strait-#{version}-macos-arm64.dmg"
  name "Eve-Strait"
  desc "EVE Online capital and jump freighter route planner"
  homepage "https://github.com/littlephish/eve-strait"

  depends_on arch: :arm64
  app "Eve-Strait.app"

  zap trash: [
    "~/.local/share/eve-strait",
  ]
end
```

The `zap` path must match whatever Task 3 step 2 decided about the data
directory. If that decision was to add a Darwin branch pointing at
`~/Library/Application Support/eve-strait`, change it here too — a `zap` that
misses is a cask that litters.

- [ ] **Step 4: Publish the cask**

Start with **your own tap** (`littlephish/homebrew-tap`) rather than
`homebrew-cask`: no review queue, and you can iterate while the DMG is still
settling. Users run:

```bash
brew tap littlephish/tap
brew install --cask eve-strait
```

Submit to `homebrew-cask` proper once versions have been stable for a few
releases. It requires a notarized, signed artifact — which Task 4 already
produces — and it brings `brew upgrade` without a tap.

- [ ] **Step 5: Automate the cask bump**

Add a release-workflow step that computes the DMG's sha256 and opens a PR
against the tap. Otherwise the cask silently drifts behind the releases and
`brew upgrade` stops meaning anything.

- [ ] **Step 6: Update the README and commit**

```bash
git add src/eve_strait/update.py src/eve_strait/ui/main_window.py \
        dist_assets/macos/eve-strait.rb README.md
git commit -m "feat(macos): notify-only update check, and a Homebrew cask"
```

---

## Deferred

- **Universal binary.** arm64 only until Intel users ask.
- **Sparkle.** See Task 7 — a third update mechanism alongside the Windows
  swap and Homebrew, for a gain Homebrew already delivers.
- **Mac App Store.** A different sandbox, a different review, and the ESI
  localhost callback would need re-examining under App Sandbox.

## Verify before release

- [ ] `spctl -a -vvv -t install` reports `accepted, source=Notarized Developer ID`
      **on a Mac that has never seen the certificate**.
- [ ] ESI login completes, including the `localhost:8635` callback.
- [ ] The data directory choice from Task 3 step 2 is deliberate and recorded.
- [ ] The Help menu shows no update items.
- [ ] The Windows build and installer are unaffected.
