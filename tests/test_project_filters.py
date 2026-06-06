from __future__ import annotations

from pathlib import Path

from bemine.index.project_filters import ignored_project_reason, ignored_project_roots


def test_ignored_project_reason_filters_default_legacy_gt_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("BEMINE_IGNORED_PROJECT_ROOTS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    reason = ignored_project_reason(tmp_path / "gt" / "legacy-app" / "crew" / "worker")

    assert reason == f"ignored project root: {tmp_path / 'gt'}"
    assert ignored_project_reason(tmp_path / "github" / "urja") is None


def test_ignored_project_reason_filters_remote_home_gt_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BEMINE_IGNORED_PROJECT_ROOTS", raising=False)

    assert (
        ignored_project_reason("/Users/cdjk/gt/deacon/dogs/boot")
        == "ignored project root: /Users/cdjk/gt"
    )
    assert ignored_project_reason("/Users/cdjk/github/deacon") is None


def test_ignored_project_roots_can_be_overridden(tmp_path: Path, monkeypatch) -> None:
    root_a = tmp_path / "legacy-a"
    root_b = tmp_path / "legacy-b"
    monkeypatch.setenv("BEMINE_IGNORED_PROJECT_ROOTS", f"{root_a}:{root_b}")

    assert ignored_project_roots() == [root_a.resolve(), root_b.resolve()]
    assert ignored_project_reason(root_a / "project") == f"ignored project root: {root_a.resolve()}"
    assert ignored_project_reason(tmp_path / "gt" / "project") is None
