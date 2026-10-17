# CI workflows

Copy these two files into `.github/workflows/` in your repository:

```
mkdir -p .github/workflows
cp docs/ci/ci.yml docs/ci/security.yml .github/workflows/
```

* `ci.yml` – tests on Linux and Windows, offline demo, offline evaluation (checker self-test + runtime-guard drill), benchmark, release gate, and an optional live evaluation when a `GOOGLE_API_KEY` repository secret exists. Evaluation and benchmark reports are uploaded as build artifacts.
* `security.yml` – `pip-audit`, `bandit` and `gitleaks`, weekly and on every push.
