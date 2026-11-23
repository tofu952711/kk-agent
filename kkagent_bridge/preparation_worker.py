"""Private media preparation process; accepts paths only, never credentials."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys


def main():
    package = Path(__file__).resolve().parent
    sys.path.insert(0, str(package.parent))
    output = Path(sys.argv[1])
    model = Path(sys.argv[2])
    chain = sys.argv[3]
    module = importlib.import_module(package.name + (
        ".b_video_preparation" if chain == "B" else ".video_preparation"))
    prepare = module.prepare_b_video if chain == "B" else module.prepare_video
    result = prepare(output, model)
    (output / "preparation-result.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
