from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.hermes_agent.storyboard_split import detect_preview_cells


def main() -> int:
    parser = argparse.ArgumentParser(description="Split a storyboard on detected row-specific gutters.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ffmpeg", default="/opt/apps/bin/ffmpeg")
    parser.add_argument("--manifest")
    args = parser.parse_args()

    source = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cells, layout = detect_preview_cells(source, count=args.count)
    outputs: list[dict[str, object]] = []
    for index, (x, y, width, height) in enumerate(cells, 1):
        target = output_dir / f"panel-{index}.png"
        subprocess.run(
            [
                args.ffmpeg,
                "-y",
                "-i",
                str(source),
                "-vf",
                f"crop={width}:{height}:{x}:{y}",
                "-frames:v",
                "1",
                str(target),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        outputs.append(
            {
                "index": index,
                "path": str(target),
                "crop": {"x": x, "y": y, "width": width, "height": height},
            }
        )
    manifest = {"source": str(source), "count": args.count, "layout": layout, "outputs": outputs}
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.manifest:
        Path(args.manifest).write_text(encoded, encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
