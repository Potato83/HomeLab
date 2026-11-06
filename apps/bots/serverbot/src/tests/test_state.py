import pytest

from serverbot.state import StateError, StateStore, should_notify, threshold_status


@pytest.mark.parametrize(("value", "expected"), [
    (79.9, "ok"), (80, "warning"), (80.1, "warning"),
    (89.9, "warning"), (90, "critical"), (90.1, "critical"),
])
def test_threshold_boundaries(value, expected):
    assert threshold_status(value, 80, 90) == expected


@pytest.mark.parametrize(("previous", "current", "notify"), [
    (None, "ok", False), (None, "warning", True), (None, "error", True),
    ("ok", "ok", False), ("warning", "warning", False), ("error", "error", False),
    ("ok", "warning", True), ("warning", "critical", True), ("critical", "warning", True),
    ("critical", "ok", True), ("error", "ok", True), ("critical", "error", True),
])
def test_transition_rules(previous, current, notify):
    assert should_notify(previous, current) is notify


def test_state_survives_restart(tmp_path):
    path = tmp_path / "var/state.json"
    state = StateStore(path)
    assert state.states == {}
    state.save({"123": {"disk:/": "warning"}})
    assert StateStore(path).states == {"123": {"disk:/": "warning"}}


def test_atomic_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state = StateStore(path)
    old = {"123": {"disk:/": "ok"}}
    state.save(old)
    previous_contents = path.read_bytes()

    def replace(source, target):
        raise OSError("Cannot replace")
    monkeypatch.setattr("serverbot.state.os.replace", replace)
    with pytest.raises(StateError):
        state.save({"123": {"disk:/": "critical"}})
    assert path.read_bytes() == previous_contents
    assert state.states == old
    assert list(tmp_path.glob(".state-*")) == []


@pytest.mark.parametrize("contents", ["broken JSON", "[]", '{"123": {"disk:/": "bad"}}', '{"user": {}}'])
def test_corrupt_state_is_not_silently_reset(tmp_path, contents):
    path = tmp_path / "state.json"
    path.write_text(contents)
    with pytest.raises(StateError):
        StateStore(path)
