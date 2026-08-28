import subprocess
import time
from pathlib import Path


class ScreenshotMaker:
    def __init__(self, *, temp_dir: str | Path | None = None):
        if temp_dir is None:
            base_dir = Path(__file__).resolve().parent
            temp_dir = base_dir / "temp"
        self.temp_dir = Path(temp_dir)

    def capture_png(self, *, serial: str | None = None, filename: str | None = None) -> Path:
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        if filename is None:
            ts = time.strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{ts}.png"
        if not filename.endswith(".png"):
            filename = f"{filename}.png"

        out_path = self.temp_dir / filename

        cmd = ["adb"]
        if serial:
            cmd += ["-s", serial]
        cmd += ["exec-out", "screencap", "-p"]

        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
            raise RuntimeError(f"adb screencap failed: {stderr}")

        png_bytes = proc.stdout or b""
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("adb screencap did not return PNG bytes")

        out_path.write_bytes(png_bytes)
        return out_path

