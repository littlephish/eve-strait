# Submitting to Flathub

Flathub is the distribution channel: it hosts the app, serves updates to
`flatpak update` and to GNOME Software / KDE Discover, and its x-checker bot
opens an automatic PR in the flathub repo whenever a new tag appears in this
repo — so releasing stays "push a tag".

The `.flatpak` bundle that `release.yml` attaches to each GitHub release is
not a channel. It is a one-shot install that never updates; it exists for
people who will not add a remote.

## One-time submission

1. Fork <https://github.com/flathub/flathub>.
2. Create a branch named exactly `io.github.littlephish.EveStrait`.
3. Copy **two** files from this directory into the fork at
   `apps/io.github.littlephish.EveStrait/`:
   - `io.github.littlephish.EveStrait.flathub.yml` →
     `io.github.littlephish.EveStrait.yml` — the filename must match the
     app-id; Flathub rejects mismatches. It differs from the local manifest
     only in the source block (a pinned git tag instead of `type: dir`).
   - `python3-requirements.json` — verbatim. A bare-filename module entry is
     resolved relative to the manifest, so it must sit beside it.
4. Open the PR.

## Expect these review questions

- `--share=network` — ESI, the Fuzzwork SDE download on first launch,
  EVE-Scout, and the OAuth callback this app serves on `localhost:8635`.
- `--talk-name=org.freedesktop.secrets` — mapper credential storage, so a
  Tripwire password is never written to disk in plaintext.

## After it merges

- Check the first Flathub build log for the version stamp. The eve-strait
  module's first build command runs `git describe --tags --exact-match` and
  stamps the tag into the source. If the build compiles as `0.1.0` instead,
  the tag is not visible inside the sandbox: add an explicit `commit:` (the
  full SHA the tag points at) alongside the `tag:` in the source block.
- Every `vX.Y.Z` tag afterwards triggers an x-checker PR in the flathub
  repo. Merging it builds and publishes the new version. Note the two
  timelines: a tag ships the Windows installer immediately, but the Flatpak
  only once that PR is merged — do not wait for a Flatpak that is queued
  behind a bot.
- Users install with `flatpak install flathub io.github.littlephish.EveStrait`.

## Keeping the two manifests in sync

`io.github.littlephish.EveStrait.flathub.yml` here is a verbatim copy of
`io.github.littlephish.EveStrait.yml` except the source block. When you
change the local manifest, change the published one too, and re-copy
`python3-requirements.json` if the dependency list was regenerated.
