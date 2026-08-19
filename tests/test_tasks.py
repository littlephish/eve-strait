from eve_strait.ui.tasks import TaskRegistry


def test_empty_registry_shows_nothing():
    r = TaskRegistry()
    assert r.summary() == ""
    assert not r.active


def test_one_task_shows_its_label():
    r = TaskRegistry()
    r.add(1, "Loading assets…")
    assert r.summary() == "Loading assets…"
    assert r.active


def test_several_tasks_show_the_oldest_plus_a_count():
    # The oldest, not the newest: a long job staying put is less distracting
    # than a label that flickers every time a short job starts.
    r = TaskRegistry()
    r.add(1, "Loading assets…")
    r.add(2, "Loading contacts…")
    r.add(3, "Loading starbases…")
    assert r.summary() == "Loading assets… (+2 more)"


def test_tooltip_lists_every_running_task():
    r = TaskRegistry()
    r.add(1, "Loading assets…")
    r.add(2, "Loading contacts…")
    assert r.tooltip() == "Loading assets…\nLoading contacts…"


def test_progress_updates_replace_the_label():
    r = TaskRegistry()
    r.add(1, "Scanning characters…")
    r.update(1, "Checking MediumPhish…")
    assert r.summary() == "Checking MediumPhish…"


def test_progress_for_an_unknown_task_is_ignored():
    # A worker can emit progress after it has been removed; that must not
    # resurrect a finished task into the status bar.
    r = TaskRegistry()
    r.update(99, "ghost")
    assert r.summary() == ""


def test_removing_a_task_reveals_the_next_one():
    r = TaskRegistry()
    r.add(1, "Loading assets…")
    r.add(2, "Loading contacts…")
    r.remove(1)
    assert r.summary() == "Loading contacts…"


def test_removing_the_last_task_clears_the_display():
    r = TaskRegistry()
    r.add(1, "Loading assets…")
    r.remove(1)
    assert r.summary() == ""
    assert not r.active


def test_removing_twice_is_harmless():
    # finished and destroyed can both fire; double removal must not raise.
    r = TaskRegistry()
    r.add(1, "Loading assets…")
    r.remove(1)
    r.remove(1)
    assert r.summary() == ""


def test_blank_labels_fall_back_to_something_readable():
    r = TaskRegistry()
    r.add(1, "")
    assert r.summary() == "Working…"


def test_counting_tasks():
    r = TaskRegistry()
    r.add(1, "a")
    r.add(2, "b")
    assert len(r) == 2
