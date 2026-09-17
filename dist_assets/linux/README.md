# Building the Flatpak

Everything here needs Linux. None of it can be checked from the Windows
development machine, so treat a first run as discovery rather than
confirmation.

## One-time: generate the offline dependency list

Flathub builds with no network, so every Python dependency must be declared
with a hash. Regenerate after **any** change to `pyproject.toml`:

```bash
pip install flatpak-pip-generator
flatpak-pip-generator \
    --runtime=org.freedesktop.Sdk//25.08 \
    --output python3-requirements \
    PySide6 requests keyring
```

That writes `python3-requirements.json`, which the manifest includes as its
first module. Commit it.

## Build and run

```bash
flatpak install flathub org.freedesktop.Platform//25.08 org.freedesktop.Sdk//25.08
flatpak-builder --user --install --force-clean build-dir \
    io.github.littlephish.EveStrait.yml
flatpak run io.github.littlephish.EveStrait
```

## Validate before submitting

```bash
desktop-file-validate io.github.littlephish.EveStrait.desktop
appstreamcli validate io.github.littlephish.EveStrait.metainfo.xml
flatpak run org.flatpak.Builder --lint manifest io.github.littlephish.EveStrait.yml
```

## What to check on a first run

These are the things the sandbox can break and the Windows build never
exercises:

1. First launch downloads the solar-system dump into the sandboxed data dir.
2. **EVE SSO login** — the browser opens through the portal, and the callback
   on `localhost:8635` is received inside the sandbox. This is the one most
   likely to need a permission change.
3. `config._data_home()` resolves under
   `~/.var/app/io.github.littlephish.EveStrait/`, and the legacy-directory
   migration in `config.py` does not fire spuriously.
4. **Help** contains no update items — Flatpak owns updates, and
   `update_check_supported()` returns False when `FLATPAK_ID` is set.

## Why there is no Nuitka build here

The Windows build is compiled because it has to run without a Python runtime,
and because the in-app updater swaps a program folder. Inside a Flatpak the
runtime provides Python and Flatpak does the updating, so the app ships as
plain source. That also keeps ~880 lines of Windows-only updater out of the
delivered product rather than porting it.
