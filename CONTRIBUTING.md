# Contributing

The standard library only, Python 3.9+. No runtime dependencies.

Before a pull request:

```
python -m unittest discover -s tests -t .
ruff check .
mypy
```

A bug fix needs a test that fails without the fix. A new control needs a test that tries to defeat it. Describe what the change does not protect against, in `docs/THREAT-MODEL.md` if it changes the model.

Do not add benchmark numbers, customer claims or adoption statistics to the docs unless a reader can reproduce them from the repository.
