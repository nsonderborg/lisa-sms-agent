"""CONFIG_PATH env override for the per-school config file.

One deploy serves one school. Onboarding a second school is a second Railway
service pointed at its own config file via CONFIG_PATH — so this override is
what makes multi-school possible at all, and a silent regression here would
serve one school another school's prices and theory times.

NB: `app.config` reads the filesystem at IMPORT time, so every test here has to
reload the module. The autouse fixture restores the default afterwards; without
it, later tests in the same session would read whichever school was loaded last.
"""
import importlib
import os
from pathlib import Path

# Must be set before app.config is imported anywhere: module-level validation
# calls sys.exit() otherwise.
os.environ["LISA_SKIP_VALIDATION"] = "1"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000000")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_auth_token_placeholder")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("OWNER_PHONE", "+4500000000")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type":"service_account"}')

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_OTHER_SCHOOL = """\
school_name: Anden Køreskole ApS
address: Testvej 1
city: Testby
pricing: "Lovpakke 9.999 kr."
"""


def _reload_config():
    import app.config
    return importlib.reload(app.config)


@pytest.fixture(autouse=True)
def _restore_default_config():
    """Put app.config back on the repo-root config.yaml after every test."""
    original = os.environ.get("CONFIG_PATH")
    yield
    if original is None:
        os.environ.pop("CONFIG_PATH", None)
    else:
        os.environ["CONFIG_PATH"] = original
    _reload_config()


def test_absolute_config_path_is_honoured(tmp_path):
    """THE regression test: with CONFIG_PATH set, the named file is loaded —
    not the repo-root config.yaml. Fails against the hardcoded path."""
    other = tmp_path / "config.other.yaml"
    other.write_text(_OTHER_SCHOOL, encoding="utf-8")

    os.environ["CONFIG_PATH"] = str(other)
    cfg = _reload_config()

    assert cfg.SCHOOL_NAME == "Anden Køreskole ApS"
    assert cfg.SCHOOL_CITY == "Testby"


def test_relative_config_path_resolves_against_repo_root(tmp_path, monkeypatch):
    """A relative CONFIG_PATH must resolve against the repo root, not the
    process CWD — Railway's working directory is not a local shell's, and
    `CONFIG_PATH=config.oemer.yaml` has to mean the same file in both."""
    target = REPO_ROOT / "config.__pytest_tmp__.yaml"
    target.write_text(_OTHER_SCHOOL, encoding="utf-8")
    try:
        # Run from somewhere else entirely; the override must still resolve.
        monkeypatch.chdir(tmp_path)
        os.environ["CONFIG_PATH"] = "config.__pytest_tmp__.yaml"
        cfg = _reload_config()
        assert cfg.SCHOOL_NAME == "Anden Køreskole ApS"
    finally:
        target.unlink(missing_ok=True)


def test_unset_config_path_falls_back_to_repo_root_config():
    """Existing deploys and the whole test suite must keep working unchanged."""
    os.environ.pop("CONFIG_PATH", None)
    cfg = _reload_config()

    assert cfg._config_path == REPO_ROOT / "config.yaml"
    assert cfg.SCHOOL_NAME  # the committed config has a school name


def test_empty_config_path_is_treated_as_unset():
    """A Railway variable set to "" must not resolve to the repo root directory
    itself — that would raise IsADirectoryError, not the FileNotFoundError the
    startup message is written for."""
    os.environ["CONFIG_PATH"] = "   "
    cfg = _reload_config()

    assert cfg._config_path == REPO_ROOT / "config.yaml"


def test_missing_config_path_exits_with_a_diagnosable_message(tmp_path, capsys):
    """Read at import time, so a typo kills the process at boot with only this
    output to debug from. It must name CONFIG_PATH explicitly."""
    os.environ["CONFIG_PATH"] = str(tmp_path / "does-not-exist.yaml")

    with pytest.raises(SystemExit):
        _reload_config()

    out = capsys.readouterr().out
    assert "CONFIG_PATH" in out
    assert "does-not-exist.yaml" in out
