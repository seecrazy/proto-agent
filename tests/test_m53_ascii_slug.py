import pytest

from proto.runtime.agent import ProtoAgent
from proto.work.delivery import main_project, project_slug


@pytest.fixture()
def agent(tmp_path):
    return ProtoAgent(data_dir=tmp_path)


@pytest.mark.parametrize(("title", "slug"), [
    ("用 SwiftUI 做日记 App：实现 backup 和 sync", "swiftui-app"),
    ("做一个日记应用", "goal"),
    ("用 ＳｗｉｆｔＵＩ 做 Café App", "swiftui-cafe-app"),
    ("Build a long project name here", "build-a-long-project"),
    ("a_token_that_alone_would_not_fit-with-the_title", "a-token-that-alone-would"),
    ("abcdefghijklmnopqrstuvwxyz 中文 App", "app"),
    ("用 SwiftUI 做 App：SwiftUI App", "swiftui-app"),
])
def test_readable_ascii_slug_at_word_boundaries(title, slug):
    assert project_slug(title) == slug


def test_same_title_goals_get_different_paths_and_children_share_after_restart(agent):
    root, other = [agent.goal_tree.add("用 SwiftUI 做日记 App", "long") for _ in range(2)]
    design = agent.goal_tree.add("设计规格", "mid", parent_id=root.id)
    verify = agent.goal_tree.add("本地验证", "mid", parent_id=root.id)
    directory = main_project(agent, design)
    assert directory == f"projects/swiftui-app-{root.id[:8]}"
    assert main_project(agent, other) == f"projects/swiftui-app-{other.id[:8]}"
    assert main_project(agent, verify) == directory
    agent.code_workspace.write_file(f"{directory}/SPEC.md", "原始规格")
    root.text = "改了标题也不换目录"
    agent.spine.store.put("goaltree", root.id, root.to_dict())
    reopened = ProtoAgent(data_dir=agent.data_dir)
    assert main_project(reopened, reopened.goal_tree.get(verify.id)) == directory
    assert (reopened.code_workspace.root / directory / "SPEC.md").read_text() == "原始规格"


def test_pure_chinese_uses_id_and_does_not_adopt_unbound_legacy_directory(agent):
    goal = agent.goal_tree.add("做一个日记应用", "mid")
    old = f"projects/{goal.text}"
    agent.code_workspace.write_file(f"{old}/original.md", "历史文件")
    assert main_project(agent, goal) == f"projects/goal-{goal.id[:8]}"
    agent.drop_goal(goal.id)
    path = agent.code_workspace.root / old
    assert (path / "original.md").read_text() == "历史文件"
    assert not (path.parent / "_archived").exists()
    assert f"未绑定** — {goal.text}/" in (path.parent / "README.md").read_text()


def test_terminal_binding_reserves_short_name_even_without_folder(agent):
    done = agent.goal_tree.add("已完成的目标", "long")
    current = agent.goal_tree.add("SwiftUI App", "long")
    short = f"projects/SwiftUI-App-{current.id[:8]}"
    agent.spine.store.put("delivery_projects", done.id, {"dir": short})
    agent.goal_tree.complete(done.id)
    assert main_project(agent, current) == f"projects/swiftui-app-{current.id}"
    assert main_project(agent, done) == short


def test_case_insensitive_disk_conflict_and_broken_link_get_a_suffix(agent):
    goal = agent.goal_tree.add("Notes App", "long")
    root = agent.code_workspace.root / "projects"
    root.mkdir()
    existing = root / f"Notes-App-{goal.id[:8]}"
    existing.write_text("留给主人", encoding="utf-8")
    link = root / f"notes-app-{goal.id}"
    link.symlink_to(root / "missing")
    assert main_project(agent, goal) == f"projects/notes-app-{goal.id}-2"
    assert existing.read_text(encoding="utf-8") == "留给主人"
    assert link.is_symlink()


def test_shared_project_is_kept_and_shown_as_dropped_in_index(agent):
    root = agent.goal_tree.add("制作一个日记", "long")
    stage = agent.goal_tree.add("设计阶段", "mid", parent_id=root.id)
    directory = main_project(agent, stage)
    agent.code_workspace.write_file(f"{directory}/SPEC.md", "保留规格")
    agent.drop_goal(root.id)
    folder = agent.code_workspace.root / directory
    assert folder.is_dir()
    assert "制作一个日记（已放弃）" in (folder.parent / "README.md").read_text()


@pytest.mark.parametrize("archive_entry", ["occupied", "symlink"])
def test_dropping_legacy_project_never_writes_through_or_over_existing_archive(agent, tmp_path, archive_entry):
    goal = agent.goal_tree.add("老项目", "mid")
    directory = "projects/old-project"
    agent.spine.store.put("project_dirs", goal.id, {"dir": directory})
    agent.code_workspace.write_file(f"{directory}/new.txt", "new")
    folder = agent.code_workspace.root / directory
    archive = folder.parent / "_archived"
    if archive_entry == "symlink":
        outside = tmp_path / "external"
        outside.mkdir()
        archive.symlink_to(outside)
    else:
        existing = archive / folder.name
        existing.mkdir(parents=True)
        (existing / "old.txt").write_text("old")
    agent.drop_goal(goal.id)
    assert (folder / "new.txt").read_text() == "new"
    assert not (archive / folder.name / "new.txt").exists()
