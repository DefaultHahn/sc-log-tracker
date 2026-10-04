"""Build the standalone Windows executable with PyInstaller.

    pip install pyinstaller
    python tools/build_exe.py

Creates dist/SC-Log-Tracker.exe (no console window; it opens the dashboard in the browser).
Used by the release workflow, works locally too.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "SC-Log-Tracker"


def read_version():
    text = (ROOT / "sc_log_tracker.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def version_file(version, path):
    """Windows version resource, shown in the file's Properties > Details tab."""
    nums = [int(x) for x in re.findall(r"\d+", version)[:3]] + [0]
    nums = (nums + [0, 0, 0, 0])[:4]
    tup = ", ".join(str(n) for n in nums)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'DefaultHahn'),
      StringStruct('FileDescription', 'SC Log Tracker - real-time Star Citizen Game.log viewer'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{NAME}'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', '{NAME}.exe'),
      StringStruct('ProductName', 'SC Log Tracker'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


def main():
    version = read_version()
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
           "--name", NAME, "--icon", str(ROOT / "assets" / "icon.ico"),
           "--distpath", str(ROOT / "dist"), "--workpath", str(build), "--specpath", str(build)]
    if sys.platform == "win32":
        vf = build / "version_info.txt"
        version_file(version, vf)
        cmd += ["--version-file", str(vf)]
    cmd.append(str(ROOT / "sc_log_tracker.py"))
    print("Building", NAME, version)
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
