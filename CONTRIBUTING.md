# Contributing to Seance

Thanks for your interest in contributing.

Contributions follow the Noise Factor
[contributing policy](https://github.com/noisefactorllc/.github/blob/main/CONTRIBUTING.md) and
[Code of Conduct](https://github.com/noisefactorllc/.github/blob/main/CODE_OF_CONDUCT.md). The policy covers which pull requests we
accept and what LLM-assisted pull requests need to include. This page adds
what's specific to Seance.

## Getting Set Up

```bash
git clone https://github.com/noisefactorllc/seance.git
cd seance
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

Generate a local secret and start the server:

```bash
SEANCE_SECRET=$(.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
SEANCE_DB=./seance.db \
SEANCE_ALLOWED_ORIGINS=http://localhost:3000 \
.venv/bin/python bin/app.py
```

Then `curl -s http://127.0.0.1:8000/up` should return a JSON health response.

## Running Tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
npm run test:sdk
```

The default pytest command excludes tests marked `slow`; run
`.venv/bin/python -m pytest -m slow -q` when you need the load/performance
smoke test.

## Code Style

- Python targets 3.14 and is linted with Ruff.
- JavaScript SDK files are plain browser ESM with no runtime dependencies.
- Keep public APIs documented in `docs/protocol.md` or `sdk/README.md`.
- New dependencies should be justified in the pull request.

## Submitting Changes

1. Pick an issue labelled `help wanted` and comment on it to say you're working on it.
2. Fork the repo and create a branch from `main`.
3. Make focused changes with tests where behavior changes.
4. Run the Python and SDK checks above.
5. Open a pull request and fill in the template.

## Reporting Issues

Open an issue on GitHub. Include the Seance version or commit, Python version,
browser/runtime details for SDK issues, and steps to reproduce.

Please report suspected security vulnerabilities privately using the process in
[SECURITY.md](SECURITY.md), not in public issues.
