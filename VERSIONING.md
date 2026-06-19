# Versioning

tmuxbot uses Semantic Versioning for project releases:

- `MAJOR`: incompatible configuration, runtime, or operator workflow changes.
- `MINOR`: new frontend/backend features, commands, lifecycle behavior, or
  supported deployment paths that remain backward compatible.
- `PATCH`: bug fixes, documentation corrections, test coverage, and internal
  maintenance with no intended behavior change.

The current package version is recorded in two places and must stay in sync:

- `pyproject.toml` → `[project].version`
- `tmuxbot/__init__.py` → `__version__`

`tests/test_project_metadata.py` checks this contract.

## Release Branches

- `main`: stable line.
- `productization-prep`: active maintenance and productization line.
- Feature branches should be short lived and merged through pull requests when
  the repository is operated in GitHub-first mode.

## Tagging

Release tags use `vMAJOR.MINOR.PATCH`, for example `v0.2.0`.

## Release Checklist

1. Decide the next version from the change type.
2. Update `pyproject.toml` and `tmuxbot/__init__.py`.
3. Move relevant `CHANGELOG.md` entries from `Unreleased` into a dated release
   section.
4. Run `make check`.
5. Commit with a message such as `Release v0.2.0`.
6. Tag and push:

   ```bash
   git tag -a v0.2.0 -m "v0.2.0"
   git push
   git push origin v0.2.0
   ```

7. Create a GitHub Release from the changelog section.
