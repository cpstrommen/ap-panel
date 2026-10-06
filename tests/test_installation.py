"""Exercise the installed package without pytest's source-path assistance."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("uv") is None, reason="Installed-package check requires uv")
def test_regular_install_survives_hidden_path_file_and_refreshes_resources(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        shutil.copyfile(root / name, project / name)
    for name in ("src", "electronics", "profiles", "mobiflight", "web"):
        shutil.copytree(root / name, project / name, ignore=shutil.ignore_patterns("__pycache__"))

    environment = os.environ.copy()
    for key in (
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "UV_WORKING_DIR",
        "UV_ENV_FILE",
        "UV_PYTHON",
    ):
        environment.pop(key, None)

    def installed_probe():
        result = subprocess.run(
            [
                shutil.which("uv"),
                "run",
                "--no-editable",
                "--locked",
                "--offline",
                "--no-dev",
                "--python",
                sys.executable,
                "python",
                "-I",
                "-c",
                "import ap_panel, ap_panel.cli, json; "
                "from pathlib import Path; "
                "from ap_panel.config import load_hardware; "
                "print(json.dumps({'module': ap_panel.__file__, "
                "'marker': getattr(ap_panel, '_INSTALLATION_PROBE', None), "
                "'address': load_hardware().lcd.address, "
                "'css': (Path(ap_panel.__file__).parent / 'web/app.css').read_text()}))",
            ],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    initial = installed_probe()
    module = Path(initial["module"])
    assert "site-packages" in module.parts  # Python -I cannot fall back to the source tree.
    assert not list(module.parent.parent.glob("_editable*ap_panel*.pth"))
    assert initial["address"] == 39

    ignored = module.parent.parent / "installation_probe.pth"
    ignored.write_text(str(project / "missing-source") + "\n")
    if hasattr(os, "chflags") and hasattr(stat, "UF_HIDDEN"):
        os.chflags(ignored, stat.UF_HIDDEN)
    repeated = installed_probe()
    assert repeated["module"] == initial["module"]
    assert repeated["address"] == 39

    # Regular installs must not leave code, calibration or dashboard edits cached.
    source = project / "src/ap_panel/__init__.py"
    source.write_text(source.read_text() + "\n_INSTALLATION_PROBE = 'updated'\n")
    pinmap = project / "electronics/pinmap.yaml"
    import yaml

    hardware = yaml.safe_load(pinmap.read_text())
    hardware["lcd"]["address"] = 63
    pinmap.write_text(yaml.safe_dump(hardware))
    css = project / "web/app.css"
    css.write_text(css.read_text() + "\n/* installation-probe-updated */\n")
    updated = installed_probe()
    assert updated["marker"] == "updated"
    assert updated["address"] == 63
    assert "installation-probe-updated" in updated["css"]
