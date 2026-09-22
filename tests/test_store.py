from middleware.store import Store


def make(tmp_path, rule="show"):
    return Store(str(tmp_path / "db.sqlite"), rule)


def test_everything_visible_without_picks(tmp_path):
    s = make(tmp_path)
    assert not s.has_filtering("bob")
    assert s.picks("bob")["live"].allows("5", "hide")


def test_included_excluded_and_undecided(tmp_path):
    s = make(tmp_path)
    s.save_picks("bob", "live", "selected", ["1", "2"], ["3"], "default")
    p = s.picks("bob")["live"]
    assert p.allows("1", "hide") and not p.allows("3", "show")
    assert p.allows("9", "show") and not p.allows("9", "hide")
    s.save_picks("bob", "live", "selected", ["1"], ["3"], "hide")
    assert not s.picks("bob")["live"].allows("9", "show")


def test_included_wins_over_excluded(tmp_path):
    s = make(tmp_path)
    s.save_picks("bob", "vod", "selected", ["1"], ["1", "2"], "default")
    p = s.picks("bob")["vod"]
    assert p.included == {"1"} and p.excluded == {"2"}


def test_rule_setting_persists(tmp_path):
    s = make(tmp_path)
    assert s.new_categories_rule() == "show"
    s.set_new_categories_rule("hide")
    assert make(tmp_path).new_categories_rule() == "hide"


def test_unknown_rows_are_claimed_by_a_layout_and_never_overwrite_it(tmp_path):
    s = make(tmp_path)
    s.record_categories("live", [{"category_id": "1", "category_name": "A"}, {"category_id": "2", "category_name": "B"},
                                 {"category_id": "3", "category_name": "C"}])          # layout not known yet
    assert {(c["id"], c["layout"]) for c in s.categories("live")} == {("1", None), ("2", None), ("3", None)}
    s.record_layout_categories("live", 2, ["1", "2"])
    s.record_layout_categories("live", 3, ["3"])
    assert {(c["id"], c["layout"]) for c in s.categories("live")} == {("1", 2), ("2", 2), ("3", 3)}
    s.record_categories("live", [{"category_id": "1", "category_name": "A on 3"}], layout=3)
    s.record_categories("live", [{"category_id": "1", "category_name": "A again"}])     # unknown again
    s.record_layout_categories("live", 3, ["1"])
    names = {(c["layout"], c["id"]): c["name"] for c in s.categories("live", layout=3)}
    assert names[(3, "1")] == "A on 3"                                                   # the real row won


def test_removal_needs_a_grace_period(tmp_path):
    never = make(tmp_path)
    never.record_categories("live", [{"category_id": "1", "category_name": "A"}, {"category_id": "2", "category_name": "B"}], 2)
    never.record_layout_categories("live", 2, ["1"], mark_removed=True)
    assert {c["id"] for c in never.categories("live", layout=2)} == {"1", "2"}
    graced = Store(str(tmp_path / "graced.sqlite"), "show", grace_days=1)
    graced.record_categories("live", [{"category_id": "1", "category_name": "A"}, {"category_id": "2", "category_name": "B"}], 2)
    graced.record_layout_categories("live", 2, ["1"], mark_removed=True)
    assert {c["id"] for c in graced.categories("live", layout=2)} == {"1", "2"}         # missing, within grace
    graced._db.execute("UPDATE categories SET last_seen = last_seen - 100000 WHERE id = '2'")
    graced.record_layout_categories("live", 2, ["1"], mark_removed=True)
    assert {c["id"] for c in graced.categories("live", layout=2)} == {"1"}
    graced.record_categories("live", [{"category_id": "2", "category_name": "B"}], 2)   # it came back
    assert {c["id"] for c in graced.categories("live", layout=2)} == {"1", "2"}


def test_a_1_0_database_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE categories (type TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
            layout INTEGER, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, removed_at INTEGER, PRIMARY KEY (type, id));
        INSERT INTO categories VALUES ('live', '1', 'A', 0, 2, 1, 1, NULL), ('live', '2', 'B', 1, NULL, 1, 1, NULL);
    """)
    db.close()
    s = Store(path)
    rows = {(c["layout"], c["id"]) for c in s.categories("live")}
    assert rows == {(2, "1"), (None, "2")}
    s.record_categories("live", [{"category_id": "1", "category_name": "A"}], 3)          # same id, other layout
    assert len(s.categories("live")) == 3


def test_catalogue_changes_bump_the_version(tmp_path):
    s = make(tmp_path)
    v = s.version
    s.record_categories("live", [{"category_id": "1", "category_name": "A"}], 2)
    assert s.version == v + 1
    s.record_categories("live", [{"category_id": "1", "category_name": "A"}], 2)
    assert s.version == v + 1                                                            # nothing changed
    s.record_categories("live", [{"category_id": "1", "category_name": "A renamed"}], 2)
    assert s.version == v + 2


def test_rename_and_clear(tmp_path):
    s = make(tmp_path)
    s.save_picks("old", "live", "selected", ["1"], [], "default")
    s.rename_user("old", "new")
    assert s.picks("new")["live"].filters and not s.picks("old")["live"].filters
    s.clear_picks("new")
    assert not s.has_filtering("new")
