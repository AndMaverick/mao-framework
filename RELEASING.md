# Releasing

1. Tests, ruff and mypy are clean on the release commit. CI is green on Python 3.9 to 3.13.
2. Update the version in `pyproject.toml` and `mao/__init__.py`, and add the CHANGELOG entry.
3. `python -m pip install build && python -m build`. Install the wheel in a clean environment and run `mao demo`.
4. Tag `vX.Y.Z`, push the tag, create the GitHub release from the CHANGELOG entry.
5. Publish to PyPI only with maintainer approval.
