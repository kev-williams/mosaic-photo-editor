from datetime import datetime, timedelta, timezone

from mosaic import history


def row(fp="abc", spec="webp|profile|800x1200", session="s1", **kw):
    return dict(action="download", source_name="jane.jpg", source_fp=fp, spec=spec, session=session,
                output_name="jane-profile.webp", **kw)


def test_recent_matches_same_spec_from_other_sessions(tmp_path):
    db = tmp_path / "h.sqlite3"
    history.record([row()], path=db)
    assert "abc" in history.recent(["abc"], "webp|profile|800x1200", exclude_session="s2", path=db)
    assert history.recent(["abc"], "jpeg|profile|800x1200", exclude_session="s2", path=db) == {}
    assert history.recent(["abc"], "webp|profile|800x1200", exclude_session="s1", path=db) == {}


def test_recent_ignores_old_records(tmp_path):
    db = tmp_path / "h.sqlite3"
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    history.record([row(created_at=old)], path=db)
    assert history.recent(["abc"], "webp|profile|800x1200", path=db) == {}
    history.prune(days=7, path=db)
    assert history.stats(path=db)[0] == 0


def test_describe_mentions_date_and_action(tmp_path):
    text = history.describe({"created_at": "2026-10-30T10:00:00+00:00", "action": "save",
                             "output_name": "a.webp"})
    assert "saved" in text and "2026" in text
