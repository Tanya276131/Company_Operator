import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_foundation_demo_runs_and_verifies():
    out = subprocess.run([sys.executable, "scripts/foundation_demo.py"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "run status : completed" in out.stdout
    assert "audit chain intact: True" in out.stdout
    assert "status=submitted" in out.stdout
