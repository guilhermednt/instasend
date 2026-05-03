"""Build InstaSend into a platform-native executable using PyInstaller."""

import platform
import plistlib
import subprocess
import sys
from pathlib import Path


def main():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--windowed",
        "--name", "instasend",
        "--noconfirm",
        "main.py",
    ]
    subprocess.run(cmd, check=True)

    if platform.system() == "Darwin":
        _patch_plist()


def _patch_plist():
    """Set LSUIElement so the app never appears in the Dock or Cmd+Tab."""
    plist_path = Path("dist/instasend.app/Contents/Info.plist")
    with open(plist_path, "rb") as f:
        plist = plistlib.load(f)
    plist["LSUIElement"] = True
    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)
    print("Patched Info.plist: LSUIElement = True")


if __name__ == "__main__":
    main()
