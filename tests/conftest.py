import sys
from pathlib import Path

# The pipeline lives in scripts/ as plain modules (no package): make them
# importable the same way the workflow runs them (script dir on sys.path).
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
