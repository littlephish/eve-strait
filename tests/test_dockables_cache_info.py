"""What the character manager is told about each character's asset cache.

The manager shows a location count and an "assets read" age per character,
and both come from dockables_cache_info(). The distinction it has to keep is
between a character whose assets were read and came back empty, and one whose
assets were never read at all: the first is an answer, the second is a
character contributing nothing to routing until somebody reloads it.
"""
import os

from eve_strait.esi import client as client_mod


def dock(name):
    return client_mod.Dockable(1, name, 2, "station")


def use_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(client_mod.config, "CACHE_DIR", tmp_path)
    return tmp_path


def test_never_loaded_is_not_zero(monkeypatch, tmp_path):
    use_cache_dir(monkeypatch, tmp_path)
    assert client_mod.dockables_cache_info(42) == (None, None)


def test_counts_what_save_dockables_wrote(monkeypatch, tmp_path):
    use_cache_dir(monkeypatch, tmp_path)
    client_mod.save_dockables(42, [dock("Jita 4-4"), dock("Amarr VIII")])
    count, mtime = client_mod.dockables_cache_info(42)
    assert count == 2
    assert mtime == (tmp_path / "dockables_42.json").stat().st_mtime


def test_read_and_empty_is_zero_not_never(monkeypatch, tmp_path):
    """Owning nothing is an answer; it must not look like 'never loaded'."""
    use_cache_dir(monkeypatch, tmp_path)
    client_mod.save_dockables(42, [])
    count, mtime = client_mod.dockables_cache_info(42)
    assert count == 0
    assert mtime is not None


def test_age_is_when_the_file_was_written(monkeypatch, tmp_path):
    use_cache_dir(monkeypatch, tmp_path)
    client_mod.save_dockables(42, [dock("Jita 4-4")])
    os.utime(tmp_path / "dockables_42.json", (1_700_000_000, 1_700_000_000))
    assert client_mod.dockables_cache_info(42) == (1, 1_700_000_000)


def test_each_character_has_its_own_cache(monkeypatch, tmp_path):
    use_cache_dir(monkeypatch, tmp_path)
    client_mod.save_dockables(1, [dock("Jita 4-4")])
    assert client_mod.dockables_cache_info(1)[0] == 1
    assert client_mod.dockables_cache_info(2) == (None, None)


def test_unreadable_file_is_no_cache(monkeypatch, tmp_path):
    use_cache_dir(monkeypatch, tmp_path)
    (tmp_path / "dockables_42.json").write_text("{not json", encoding="utf-8")
    assert client_mod.dockables_cache_info(42) == (None, None)


def test_a_file_that_is_not_a_list_is_no_cache(monkeypatch, tmp_path):
    """load_dockables() reads these as empty, so they must not report a count.

    A dict would otherwise report its number of keys, and a bare number would
    raise out of the manager's refresh.
    """
    use_cache_dir(monkeypatch, tmp_path)
    path = tmp_path / "dockables_42.json"
    for body in ('{"a": 1, "b": 2}', "5", "null"):
        path.write_text(body, encoding="utf-8")
        assert client_mod.dockables_cache_info(42) == (None, None), body
        assert client_mod.load_dockables(42) == []
