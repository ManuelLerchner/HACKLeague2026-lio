"""Encode the captured synthetic workflow as a local, captioned MP4."""

import subprocess
from pathlib import Path


def main() -> None:
    output = Path("artifacts")
    frames = [output / f"video-{index}.png" for index in range(1, 8)]
    if not all(frame.is_file() for frame in frames):
        raise SystemExit(
            "Capture the fixture walkthrough with node scripts/capture.mjs video first"
        )
    # End on the resolved case; the historical closing slide contains stale integration status.
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            "1/12",
            "-start_number",
            "1",
            "-i",
            str(output / "video-%d.png"),
            "-t",
            "84",
            "-vf",
            "fps=24",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output / "supplier-chaser-demo.mp4"),
        ],
        check=True,
    )
    print(output / "supplier-chaser-demo.mp4")


if __name__ == "__main__":
    main()
