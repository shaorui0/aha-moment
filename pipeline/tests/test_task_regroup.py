import json
import task_regroup as R


def mkchange(ws, rel, cid, tasks, title="# proposal: T"):
    d = ws / rel / "openspec/changes" / cid
    d.mkdir(parents=True)
    (d / "proposal.md").write_text(title + "\n\nwhy")
    if tasks is not None:
        (d / "tasks.md").write_text(tasks)


def test_scan_counts_from_checkboxes(tmp_path):
    ws = tmp_path / "ws"
    mkchange(ws, ".", "a", "- [x] 1\n- [ ] 2\n  - [X] 3\n")
    mkchange(ws, "projects/p", "b", None)
    mkchange(ws, "node_modules/x", "z", "- [x] no")
    (ws / "openspec/changes/archive/old").mkdir(parents=True)
    got = {c["id"]: c for c in R.scan_changes(ws)}
    assert set(got) == {"a", "b"}
    assert (got["a"]["done"], got["a"]["total"], got["a"]["title"]) == (2, 3, "T")
    assert got["b"]["total"] == 0 and got["b"]["path"] == "projects/p/openspec/changes/b"


def test_pins_win_and_fallback(tmp_path):
    ws, root = tmp_path / "ws", tmp_path / "data"
    for c in "abc":
        mkchange(ws, ".", c, "- [x] 1\n")
    root.mkdir()
    (root / "claude_tasks_pins.json").write_text(json.dumps({"a": {"area": "工作", "group": "PIN"}}))
    seen = []
    def model(prompt):
        seen.append(prompt)
        return '说明\n{"a": {"area": "个人", "group": "X"}, "b": {"area": "个人", "group": "G"}, "c": {"area": "火星", "group": "G"}}'
    tree = R.regroup(root, ws, model)
    flat = {c["id"]: (t["area"], t["group"]) for t in tree for c in t["changes"]}
    assert flat == {"a": ("工作", "PIN"), "b": ("个人", "G"), "c": ("个人", "未分类")}
    assert "- a |" not in seen[0]                       # pinned change is not sent to the model
    assert json.loads((root / "claude_tasks_tree.json").read_text()) == tree


def test_all_pinned_skips_model(tmp_path):
    ws, root = tmp_path / "ws", tmp_path / "data"
    mkchange(ws, ".", "a", None); root.mkdir()
    (root / "claude_tasks_pins.json").write_text(json.dumps({"a": {"area": "个人", "group": "G"}}))
    assert R.regroup(root, ws, lambda p: 1 / 0)[0]["group"] == "G"


def test_bad_model_output_keeps_old_tree(tmp_path, monkeypatch):
    ws, root = tmp_path / "ws", tmp_path / "data"
    mkchange(ws, ".", "a", None); root.mkdir()
    (root / "claude_tasks_tree.json").write_text("[]")
    try:
        R.regroup(root, ws, lambda p: "nope")
        assert False
    except ValueError:
        pass
    assert (root / "claude_tasks_tree.json").read_text() == "[]"
