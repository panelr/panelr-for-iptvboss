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


def test_layout_removal_only_touches_that_layout(tmp_path):
    s = make(tmp_path)
    s.record_categories("live", [{"category_id": "1", "category_name": "A"}, {"category_id": "2", "category_name": "B"},
                                 {"category_id": "3", "category_name": "C"}])
    s.record_layout_categories("live", 2, ["1", "2"])
    s.record_layout_categories("live", 3, ["3"])
    s.record_layout_categories("live", 2, ["1"], mark_removed=True)
    active = {c["id"] for c in s.categories("live")}
    assert active == {"1", "3"}
    s.record_categories("live", [{"category_id": "2", "category_name": "B"}])
    assert {c["id"] for c in s.categories("live")} == {"1", "2", "3"}


def test_rename_and_clear(tmp_path):
    s = make(tmp_path)
    s.save_picks("old", "live", "selected", ["1"], [], "default")
    s.rename_user("old", "new")
    assert s.picks("new")["live"].filters and not s.picks("old")["live"].filters
    s.clear_picks("new")
    assert not s.has_filtering("new")
