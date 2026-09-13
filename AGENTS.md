# Development and release workflow

The user requires repaired code to be preserved locally and on GitHub, with the AWS deployment matching the same Git commit.

- Never leave a server-only source fix. Apply changes in this repository, run relevant tests, commit, and push when the session authorizes publication.
- Deploy from `git archive` of a committed revision, not an uncommitted working-directory copy. Follow `deploy/README.md`.
- Before claiming synchronization, verify local HEAD against the GitHub branch, compare source-file SHA-256 values on the server and running container, and check the image revision label.
- Run the Python suite with `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` and the JavaScript runtime checks in `tests/`. Live-model batch tests are opt-in and incur provider usage; keep their budgets explicit and isolated from normal-user quotas.
- The repository is public. Never commit `.env`, private keys, actual allowlists, instance state or live evaluation outputs. Keep instance-specific information in Git-ignored deployment files; examples use reserved example domains and addresses.
- Preserve server secrets and persistent data volumes during updates. Do not run allowlist bootstrap on every release.
- State validation limits clearly: a completed simulated evaluation does not establish audience realism or real-world predictive accuracy.
