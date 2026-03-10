from __future__ import annotations

from pathlib import Path

from feiqiao_guard.identity_memory import IdentityMemoryStore


def test_identity_memory_rolls_window_and_updates_summaries(tmp_path: Path) -> None:
    store = IdentityMemoryStore(root_dir=tmp_path, window_size=3, refresh_stride=2)

    store.append_turn(identity_id="id-a", role="user", text="u1", metadata={"trace_id": "t1"})
    store.append_turn(identity_id="id-a", role="assistant", text="a1", metadata={"trace_id": "t1"})
    store.append_turn(identity_id="id-a", role="user", text="u2", metadata={"trace_id": "t2"})
    store.append_turn(identity_id="id-a", role="assistant", text="a2", metadata={"trace_id": "t2"})

    payload = store.read("id-a")
    assert payload is not None
    turns = payload.get("turns")
    assert isinstance(turns, list)
    assert len(turns) == 3
    assert turns[0]["text"] == "a1"
    assert turns[-1]["text"] == "a2"
    assert str(payload.get("rolling_summary", "")).strip()
    assert str(payload.get("stable_summary", "")).strip()
    archive = payload.get("archive_summary")
    assert isinstance(archive, list)
    assert len(archive) >= 1


def test_identity_memory_dedupes_same_turn_signature(tmp_path: Path) -> None:
    store = IdentityMemoryStore(root_dir=tmp_path, window_size=30, refresh_stride=5)

    store.append_turn(
        identity_id="id-a",
        role="assistant",
        text="same reply",
        metadata={"trace_id": "trace-x", "task_id": "task-y"},
    )
    store.append_turn(
        identity_id="id-a",
        role="assistant",
        text="same reply",
        metadata={"trace_id": "trace-x", "task_id": "task-y"},
    )

    payload = store.read("id-a")
    assert payload is not None
    turns = payload.get("turns")
    assert isinstance(turns, list)
    assert len(turns) == 1


def test_identity_memory_60_turn_three_tier_rolls_forward(tmp_path: Path) -> None:
    store = IdentityMemoryStore(
        root_dir=tmp_path,
        window_size=60,
        fresh_size=20,
        stable_size=20,
        archive_size=20,
        refresh_stride=5,
    )

    for idx in range(1, 71):
        store.append_turn(identity_id="id-roll", role="user", text=f"m{idx}", metadata={"trace_id": f"t{idx}"})

    payload = store.read("id-roll")
    assert payload is not None
    turns = payload.get("turns")
    assert isinstance(turns, list)
    assert len(turns) == 60
    assert turns[0]["text"] == "m11"
    assert turns[-1]["text"] == "m70"
    tier_counts = payload.get("tier_counts")
    assert isinstance(tier_counts, dict)
    assert tier_counts == {"fresh": 20, "stable": 20, "archive": 20}
    tier_turn_ids = payload.get("tier_turn_ids")
    assert isinstance(tier_turn_ids, dict)
    fresh_ids = tier_turn_ids.get("fresh")
    stable_ids = tier_turn_ids.get("stable")
    archive_ids = tier_turn_ids.get("archive")
    assert isinstance(fresh_ids, list)
    assert isinstance(stable_ids, list)
    assert isinstance(archive_ids, list)
    assert fresh_ids[0] == "T000051"
    assert fresh_ids[-1] == "T000070"
    assert stable_ids[0] == "T000031"
    assert stable_ids[-1] == "T000050"
    assert archive_ids[0] == "T000011"
    assert archive_ids[-1] == "T000030"

    for idx in range(71, 76):
        store.append_turn(identity_id="id-roll", role="assistant", text=f"m{idx}", metadata={"trace_id": f"t{idx}"})

    rolled = store.read("id-roll")
    assert rolled is not None
    rolled_turns = rolled.get("turns")
    assert isinstance(rolled_turns, list)
    assert len(rolled_turns) == 60
    assert rolled_turns[0]["text"] == "m16"
    assert rolled_turns[-1]["text"] == "m75"
    rolled_ids = rolled.get("tier_turn_ids")
    assert isinstance(rolled_ids, dict)
    rolled_fresh = rolled_ids.get("fresh")
    rolled_stable = rolled_ids.get("stable")
    rolled_archive = rolled_ids.get("archive")
    assert isinstance(rolled_fresh, list)
    assert isinstance(rolled_stable, list)
    assert isinstance(rolled_archive, list)
    assert rolled_fresh[0] == "T000056"
    assert rolled_fresh[-1] == "T000075"
    assert rolled_stable[0] == "T000036"
    assert rolled_stable[-1] == "T000055"
    assert rolled_archive[0] == "T000016"
    assert rolled_archive[-1] == "T000035"
