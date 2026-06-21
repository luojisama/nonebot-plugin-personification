from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ._loader import load_personification_module


skill_isolation = load_personification_module("plugin.personification.skill_runtime.skill_isolation")


def test_untrusted_isolation_defaults_to_process_without_env_inheritance() -> None:
    config = skill_isolation.normalize_isolation_config({}, trusted=False)
    assert config["mode"] == "process"
    assert config["inherit_env"] is False


def test_trusted_isolation_defaults_to_inprocess_with_env_inheritance() -> None:
    config = skill_isolation.normalize_isolation_config({}, trusted=True)
    assert config["mode"] == "inprocess"
    assert config["inherit_env"] is True


def test_untrusted_process_env_scrubs_parent_secret(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PERSONIFICATION_SECRET_TEST", "should-not-leak")
    skill_dir = tmp_path / "remote_skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "main.py"
    script.write_text(
        "import json\n"
        "import os\n\n"
        "def run():\n"
        "    return json.dumps({\n"
        "        'secret': os.environ.get('PERSONIFICATION_SECRET_TEST'),\n"
        "        'no_user_site': os.environ.get('PYTHONNOUSERSITE'),\n"
        "        'dont_write_bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE'),\n"
        "    }, ensure_ascii=False)\n",
        encoding="utf-8",
    )
    isolation = skill_isolation.normalize_isolation_config({}, trusted=False)

    raw = asyncio.run(
        skill_isolation.run_skill_in_subprocess(
            script_path=script,
            function_name="run",
            kwargs={},
            skill_dir=skill_dir,
            isolation=isolation,
        )
    )

    payload = json.loads(raw)
    assert payload["secret"] is None
    assert payload["no_user_site"] == "1"
    assert payload["dont_write_bytecode"] == "1"
