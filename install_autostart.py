"""Add (or with --remove, delete) a Startup-folder shortcut that launches the widget at login."""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINK = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup\Deye Widget.lnk"

def main():
    if "--remove" in sys.argv:
        LINK.unlink(missing_ok=True)
        print("Removed", LINK)
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        sys.exit(f"pythonw.exe not found next to {sys.executable}")
    q = lambda v: str(v).replace("'", "''")  # PowerShell single-quote escaping, for paths like C:\Users\O'Brien
    ps = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(LINK)}');"
        f"$s.TargetPath='{q(pythonw)}';$s.Arguments='\"{q(HERE / 'app.py')}\"';"
        f"$s.WorkingDirectory='{q(HERE)}';$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print("Created", LINK)


if __name__ == "__main__":
    main()
