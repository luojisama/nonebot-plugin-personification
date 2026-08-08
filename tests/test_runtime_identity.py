from __future__ import annotations

from ._loader import load_personification_module


runtime_identity = load_personification_module("plugin.personification.core.runtime_identity")


def test_runtime_identity_reads_linked_worktree_common_ref(tmp_path) -> None:  # noqa: ANN001
    repo_root = tmp_path / "plugin"
    worktree_git = tmp_path / "repo.git" / "worktrees" / "plugin"
    common_git = tmp_path / "repo.git"
    repo_root.mkdir()
    worktree_git.mkdir(parents=True)
    (common_git / "refs" / "heads").mkdir(parents=True)
    (repo_root / ".git").write_text(
        f"gitdir: {worktree_git.as_posix()}\n",
        encoding="utf-8",
    )
    (worktree_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
    expected = "1234567890abcdef1234567890abcdef12345678"
    (common_git / "refs" / "heads" / "main").write_text(expected, encoding="utf-8")

    assert runtime_identity._read_git_commit(repo_root) == expected


def test_runtime_identity_ignores_generic_host_commit_env(monkeypatch) -> None:  # noqa: ANN001
    plugin_commit = "abcdef1234567890abcdef1234567890abcdef12"
    monkeypatch.delenv("PERSONIFICATION_BUILD_COMMIT", raising=False)
    monkeypatch.setenv("GIT_COMMIT", "9999999999999999999999999999999999999999")
    monkeypatch.setattr(runtime_identity, "_read_git_commit", lambda _root: plugin_commit)

    assert runtime_identity._resolve_build_commit() == plugin_commit


def test_runtime_identity_accepts_explicit_plugin_build_commit(monkeypatch) -> None:  # noqa: ANN001
    explicit = "fedcba9876543210fedcba9876543210fedcba98"
    monkeypatch.setenv("PERSONIFICATION_BUILD_COMMIT", explicit)
    monkeypatch.setattr(
        runtime_identity,
        "_read_git_commit",
        lambda _root: "abcdef1234567890abcdef1234567890abcdef12",
    )

    assert runtime_identity._resolve_build_commit() == explicit
