import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("release_check", Path(__file__).resolve().parent.parent / "scripts" / "release_check.py")
rc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc)


def test_secret_patterns_catch_real_looking_keys_and_ignore_prose():
    fake_google = "AIza" + "Sy" + "A" * 33
    fake_private = "-----BEGIN " + "RSA PRIVATE KEY-----"
    assert rc.SECRET_PATTERNS["Google API key"].search(f"key={fake_google}")
    assert rc.SECRET_PATTERNS["Private key block"].search(fake_private)
    assert rc.SECRET_PATTERNS["Assigned secret"].search("GOOGLE_API_KEY=abcdefghijklmnop1234")
    assert not rc.SECRET_PATTERNS["Assigned secret"].search("GOOGLE_API_KEY=")
    assert not rc.SECRET_PATTERNS["Google API key"].search("set GOOGLE_API_KEY in .env")


def test_risky_patterns():
    assert rc.RISKY["shell=True"].search("subprocess.run(x, shell=True)")
    assert rc.RISKY["eval()"].search("eval(user)") and not rc.RISKY["eval()"].search("model.evaluate(x)")


def test_repository_passes_its_own_release_gate(capsys):
    assert rc.main() == 0
