"""Oracle: run the server's own _transform_edit over JSON cases from stdin."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.textdoc import TextEdit, _transform_edit  # noqa: E402

cases = json.load(sys.stdin)
out = []
for case in cases:
    result = _transform_edit(TextEdit(**case["local"]), TextEdit(**case["remote"]))
    out.append({"start": result.start, "end": result.end, "text": result.text})
json.dump(out, sys.stdout)
