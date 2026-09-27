"""The mount project: where the BMAD project sits inside a checkout of `repo_root`.

A pure leaf (no filesystem, yaml or toml): `bmadconfig.ProjectPaths.rebased`, worktree
provisioning, the pyright-strict `model` core and the `runs` read-model all derive the
mount project through :func:`rebased_project`, so the tree a unit's relative spec
spelling is written against and the tree it is read back from cannot disagree (DW-379).
"""

from __future__ import annotations

from pathlib import Path


def rebased_project(project: Path, repo_root: Path, new_root: Path) -> Path:
    """Where `project` sits in a checkout of `repo_root` mounted at `new_root`.

    - Nested, the default config included: the project's offset inside `repo_root`,
      re-joined onto `new_root`. The default config's offset is ``.``, so the answer
      is `new_root` itself, spelled exactly as passed.
    - Disjoint (the project does not lie inside `repo_root`): `project`, unmoved.
      The checkout carries no copy of it. Worktree isolation is refused for this
      layout (`bmadconfig.worktree_isolation_conflict`), so no mount is ever made
      for it; the unmoved answer is what an in-place caller (`isolation = "none"`,
      rebasing onto `repo_root` itself) needs. A caller reading a RECORDED mount
      (`model.RunState.mount_project`) instead treats a disjoint-looking pair as
      stale spellings of the default config and anchors on the mount itself.

    Lexical: callers pass the spellings they want compared. `load_paths`
    canonicalizes both roots, and `ProjectPaths.rebased` resolves `new_root` first.
    """
    try:
        offset = project.relative_to(repo_root)
    except ValueError:
        return project
    return new_root / offset


def project_offset(project: Path, repo_root: Path) -> str | None:
    """The project's POSIX offset inside `repo_root` (``"."`` when they are the same
    directory), or None for a disjoint layout. Lexical, like :func:`rebased_project`."""
    try:
        return project.relative_to(repo_root).as_posix()
    except ValueError:
        return None
