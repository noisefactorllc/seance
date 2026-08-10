import json
import subprocess
import sys


def test_stamp_deployment_meta_writes_expected_json(tmp_path):
    output = tmp_path / "deployment-meta.json"
    result = subprocess.run(  # noqa: S603 - exercises the real CLI entrypoint
        [
            sys.executable,
            "bin/stamp_deployment_meta.py",
            "--git-hash",
            "abc123",
            "--date",
            "2026-07-03T00:00:00+00:00",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == {
        "git_hash": "abc123",
        "date": "2026-07-03T00:00:00+00:00",
    }
