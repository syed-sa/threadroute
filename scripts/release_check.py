"""Pre-push release gate:  python scripts/release_check.py [--tests]

Fails (exit 1) if the tree is not safe to publish:
  * a real-looking secret or private key is present in any file that git would include;
  * .env exists but is not git-ignored;
  * .env.example contains a value for a secret variable;
  * risky Python constructs appear (eval/exec, shell=True, verify=False, pickle, unsafe yaml.load);
  * personal-data patterns (cards, Aadhaar, PAN, non-example e-mail addresses, phone numbers) appear outside the
    deliberately synthetic fixtures;
  * required project files are missing, or a Python file does not compile.
`--tests` also runs the test suite and the offline evaluation."""

from __future__ import annotations

import compileall
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".threadroute", "reports", "node_modules"}
TEXT_EXT = {".py", ".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".sh", ".ps1", ".html", ".css",
            ".js", ".example", ""}
SECRET_PATTERNS = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "Assigned secret": re.compile(r"""(?im)^\s*(?:GOOGLE_API_KEY|GEMINI_API_KEY|GOOGLE_MAPS_API_KEY|THREADROUTE_AUTH_TOKEN|THREADROUTE_A2A_TOKEN)\s*=\s*['"]?[A-Za-z0-9_\-]{16,}"""),
}
RISKY = {
    "eval()": re.compile(r"(?<![\w.])eval\("), "exec()": re.compile(r"(?<![\w.])exec\("),
    "shell=True": re.compile(r"shell\s*=\s*True"), "TLS verify disabled": re.compile(r"verify\s*=\s*False"),
    "pickle": re.compile(r"pickle\.loads?\("), "unsafe yaml.load": re.compile(r"yaml\.load\((?!.*Loader)"),
}
# Files that deliberately contain synthetic personal data or deliberately risky-looking strings
FIXTURE_PATHS = ("threadroute/data/seed.py", "tests/", "evals/", "samples/", "scripts/make_samples.py",
                 "scripts/release_check.py", "threadroute/guardrails/", "threadroute/evaluation/", "docs/", "README.md",
                 "SECURITY.md")
REQUIRED = ["README.md", "SECURITY.md", "Dockerfile", "docker-compose.yml", "requirements.txt", ".env.example",
            ".gitignore", "docs/ARCHITECTURE.md", "evals/golden.yaml"]


def files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not set(p.relative_to(ROOT).parts) & SKIP_DIRS and p.suffix.lower() in TEXT_EXT \
                and p.name != ".env" and p.stat().st_size < 2_000_000:
            yield p


def is_fixture(rel: str) -> bool:
    return rel.startswith(FIXTURE_PATHS) or rel in FIXTURE_PATHS


def main() -> int:
    problems: list[str] = []
    warnings: list[str] = []
    from threadroute.guardrails import pii

    env = ROOT / ".env"
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore") if (ROOT / ".gitignore").exists() else ""
    if env.exists() and not re.search(r"(?m)^\.env\s*$", ignore):
        problems.append(".env exists and is NOT in .gitignore - your keys would be pushed")
    elif env.exists():
        warnings.append(".env exists locally (fine - it is git-ignored). Do not use `git add -f`.")

    example = ROOT / ".env.example"
    if example.exists():
        for line in example.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*(GOOGLE_API_KEY|GEMINI_API_KEY|GOOGLE_MAPS_API_KEY|THREADROUTE_AUTH_TOKEN|THREADROUTE_A2A_TOKEN)\s*=\s*(\S+)", line)
            if m:
                problems.append(f".env.example sets {m.group(1)} to a value")

    for rel in REQUIRED:
        if not (ROOT / rel).exists():
            problems.append(f"missing required file: {rel}")

    for p in files():
        rel = p.relative_to(ROOT).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, rx in SECRET_PATTERNS.items():
            for m in rx.finditer(text):
                if rel == "scripts/release_check.py" or (rel == ".env.example"):
                    continue
                problems.append(f"{rel}: looks like a {name}")
        if p.suffix == ".py" and not is_fixture(rel):
            for name, rx in RISKY.items():
                if rx.search(text):
                    problems.append(f"{rel}: risky construct {name}")
        if not is_fixture(rel):
            for f in pii.scan(text):
                if f.kind in ("CARD", "AADHAAR", "PAN"):
                    problems.append(f"{rel}: personal-data pattern {f.kind}")
                elif f.kind == "EMAIL" and ".example" not in f.text and not f.text.endswith("@localhost"):
                    problems.append(f"{rel}: e-mail address that is not an example domain")
                elif f.kind == "PHONE":
                    warnings.append(f"{rel}: phone-like number - check it is not real")

    if not compileall.compile_dir(str(ROOT / "threadroute"), quiet=2, force=False):
        problems.append("a Python file in threadroute/ does not compile")

    if "--tests" in sys.argv:
        for label, cmd in (("pytest", [sys.executable, "-m", "pytest", "-q"]),
                           ("evaluate --offline", [sys.executable, "-m", "threadroute", "evaluate", "--offline"])):
            print(f"== running {label} ==")
            if subprocess.run(cmd, cwd=ROOT).returncode != 0:
                problems.append(f"{label} failed")

    for w in warnings:
        print("warning:", w)
    if problems:
        print("\nRELEASE CHECK FAILED")
        for pr in problems:
            print(" -", pr)
        return 1
    print("\nRELEASE CHECK PASSED: no secrets, no real-looking personal data, required files present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
