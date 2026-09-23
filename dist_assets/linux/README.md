# Building the Flatpak

Everything here needs Linux. None of it can be checked from the Windows
development machine, so treat a first run as discovery rather than
confirmation.

The app is compiled with **Nuitka**, the same as the Windows build — one build
tool across platforms, and a faster start than interpreted source.

## Regenerating the offline dependency list

Flathub builds with no network, so every Python dependency must be declared
with a hash. **Nuitka is one of them**: it is a build-time dependency of this
manifest, not just a developer tool. Regenerate after **any** change to
`pyproject.toml`.

The tool is **req2flatpak**, not `flatpak-pip-generator`. The latter downloads
by running pip inside the Flatpak SDK, so it needs a machine with flatpak on
it; req2flatpak is pure Python, resolves against PyPI, and runs anywhere
including the Windows development box.

```bash
pip install req2flatpak
req2flatpak -r requirements.in -t 313-x86_64 -o python3-requirements.json
```

### `-t 313-x86_64` is the part that breaks the build

**The target Python version must match the runtime's, exactly.** It is not a
default and it is not checked for you.

`org.freedesktop.Platform//25.08` ships **Python 3.13**, so the target is
`313`. Confirm it rather than trusting this line, because it changes with the
runtime branch:

```bash
flatpak run --command=python3 org.freedesktop.Sdk//25.08 --version
```

Get this wrong and the build fails deep into the pip step with a message that
does not mention Python versions at all:

```
ERROR: Could not find a version that satisfies the requirement cffi
       (from versions: none)
```

That is a `cp312`-tagged wheel being invisible to a 3.13 interpreter. Most of
the list survives a mismatch — pure-Python wheels are tagged `py3-none-any`
and PySide6, shiboken6 and cryptography ship `abi3` wheels good for any later
3.x — so only the few packages that build a version-specific C extension fail.
Today that is **cffi, charset-normalizer and zstandard**. A mismatch therefore
looks like one broken package rather than a systematically wrong file.

### Three more things that cost time

- **`requirements.in` is fully pinned**, because req2flatpak rejects anything
  that is not exactly one version. Update pins there, not in the generated
  JSON.
- **Nuitka is appended by hand.** It ships as an sdist with no wheel and
  req2flatpak handles wheels only, so after regenerating you must re-add its
  `sources` entry and append `nuitka` to the end of the `pip3 install` command
  in `build-commands`. Keep the pin in step with
  `.github/workflows/release.yml` and `scripts/build_exe.ps1`, so a Flatpak and
  a Windows release are compiled by the same compiler.
- PySide6 6.11 publishes no `manylinux2014` wheel, so the resolve reports
  "unsatisfiable" unless the platform is `manylinux_2_34`. `-t 313-x86_64`
  already picks that.

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
