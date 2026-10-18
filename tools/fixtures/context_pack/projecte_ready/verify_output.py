#!/usr/bin/env python3
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
raise SystemExit(0 if data.get("schema_version") == "decision-result/v1" and data.get("claims") else 1)
