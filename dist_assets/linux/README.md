# Building the Flatpak

Everything here needs Linux. None of it can be checked from the Windows
development machine, so treat a first run as discovery rather than
confirmation.

The app is compiled with **Nuitka**, the same as the Windows build — one build
tool across platforms, and a faster start than interpreted source.

## One-time: generate the offline dependency list

Flathub builds with no network, so every Python dependency must be declared
with a hash. **Nuitka is one of them**: it is a build-time dependency of this
manifest, not just a developer tool. Regenerate after **any** change to
`pyproject.toml`:

```bash
pip install flatpak-pip-generator
flatpak-pip-generator \
    --runtime=org.freedesktop.Sdk//25.08 \
    --output python3-requirements \
    PySide6 requests keyring nuitka==4.2.1 zstandard ordered-set
```

That writes `python3-requirements.json`, which the manifest includes as its
first module. Commit it.

Keep the Nuitka pin in step with `.github/workflows/release.yml` and
`scripts/build_exe.ps1`, so a Flatpak and a Windows release are compiled by
the same compiler.

## Build and run

```bash
flatpak install flathub org.freedesktop.Platform//25.08 org.freedesktop.Sdk//25.08
flatpak-builder --user --install --force-clean build-dir \
    io.github.littlephish.EveStrait.yml
flatpak run io.github.littlephish.EveStrait
```

Expect **several minutes**. A Nuitka standalone build with PySide6 is not
quick, and Flathub's builders are not fast.

## Validate before submitting

```bash
desktop-file-validate io.github.littlephish.EveStrait.desktop
appstreamcli validate io.github.littlephish.EveStrait.metainfo.xml
flatpak run org.flatpak.Builder --lint manifest io.github.littlephish.EveStrait.yml
```

## What to check on a first run

These are the things the sandbox can break and the Windows build never
exercises:

1. **The Nuitka build completes without reaching for the network.** The
   manifest deliberately omits `--assume-yes-for-downloads`, which would let
   Nuitka fetch ccache; `--disable-cache=ccache` stops it wanting to. If the
   build fails asking to download something, that is the thing to fix — not by
   adding the flag, which cannot work offline anyway, but by providing the
   dependency through the SDK.
2. First launch downloads the solar-system dump into the sandboxed data dir.
3. **EVE SSO login** — the browser opens through the portal, and the callback
   on `localhost:8635` is received inside the sandbox. This is the one most
   likely to need a permission change.
4. `config._data_home()` resolves under
   `~/.var/app/io.github.littlephish.EveStrait/`, and the legacy-directory
   migration in `config.py` does not fire spuriously.
5. **Help** contains no update items — Flatpak owns updates, and
   `update_check_supported()` returns False when `FLATPAK_ID` is set.

## Layout inside the sandbox

Nuitka produces a program folder, not a single binary, so:

```
/app/lib/eve-strait/     the compiled program folder (app.dist)
/app/bin/eve-strait      symlink to the launcher above, which is what
                         `command:` in the manifest resolves to
```

## Why the in-app updater is absent

It is not ported, and should not be. On Windows it swaps a program folder by
polling a `.exe` file lock, with a `powershell.exe` fallback. Inside a Flatpak
the sandbox is read-only and Flatpak does the updating, so `update.py`'s
guards hide the feature entirely when `FLATPAK_ID` is set. That is ~880 lines
of Windows-only code that never travels, regardless of how the app is built.
