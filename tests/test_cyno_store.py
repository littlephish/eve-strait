"""Cyno alts survive a restart.

Where your alts are parked is the answer to "do I have a way home", and
re-scanning to find out costs a full asset read per character against the
ESI budget. It should be remembered.

What it must NOT do is present a remembered position as a live one. The
module's own docstring is clear that location is near-live while assets
lag; once the app has closed, even the location is only a snapshot.
"""
import json

from eve_strait.data import cyno


def _alt(**over):
    base = dict(character_id=95465499, character_name="Scout Alt",
                system_id=30000142, ship_type_id=11957, ship_name="Falcon",
                module_type_id=28646, module_name="Covert Cynosural Field "
                                                 "Generator I", docked=False)
    base.update(over)
    return cyno.CynoAlt(**base)


def test_round_trip_preserves_every_field(tmp_path):
    path = tmp_path / "cyno_alts.json"
    alts = [_alt(), _alt(character_id=2, character_name="Other", docked=True)]
    cyno.save_alts(alts, ["Third: missing scope"], path=path, now=1000.0)
    got, notes, fetched = cyno.load_alts(path=path)
    assert got == alts
    assert notes == ["Third: missing scope"]
    assert fetched == 1000.0


def test_properties_still_work_after_a_round_trip(tmp_path):
    """Restored entries must behave like scanned ones, not like dicts."""
    path = tmp_path / "c.json"
    cyno.save_alts([_alt()], [], path=path)
    (got,), _n, _f = cyno.load_alts(path=path)
    assert got.covert is True
    assert "covert cyno" in got.summary()


def test_missing_file_is_empty(tmp_path):
    assert cyno.load_alts(path=tmp_path / "nope.json") == ([], [], None)


def test_corrupt_file_is_empty_not_an_error(tmp_path):
    """A half-written cache must cost the feature, never the launch."""
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert cyno.load_alts(path=path) == ([], [], None)


def test_unknown_fields_are_ignored(tmp_path):
    """A cache written by a newer build must not crash an older one."""
    path = tmp_path / "future.json"
    path.write_text(json.dumps({
        "fetched": 5.0, "notes": [],
        "alts": [{"character_id": 1, "character_name": "A", "system_id": 2,
                  "ship_type_id": 3, "ship_name": "S", "module_type_id": 4,
                  "module_name": "Cynosural Field Generator I",
                  "docked": False, "invented_later": "boom"}],
    }), encoding="utf-8")
    alts, _n, fetched = cyno.load_alts(path=path)
    assert len(alts) == 1 and alts[0].character_name == "A"
    assert fetched == 5.0


def test_entries_missing_required_fields_are_dropped(tmp_path):
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"fetched": 1.0, "notes": [],
                                "alts": [{"character_id": 1}]}),
                    encoding="utf-8")
    assert cyno.load_alts(path=path)[0] == []


def test_age_describes_a_remembered_snapshot():
    assert cyno.describe_age(None) == "never scanned"
    assert "just now" in cyno.describe_age(1000.0, now=1000.0)
    assert "12 minutes" in cyno.describe_age(1000.0, now=1000.0 + 12 * 60)
    assert "3.0 hours" in cyno.describe_age(1000.0, now=1000.0 + 3 * 3600)
