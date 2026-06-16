"""Genere version_info.txt (infos de version Windows pour PyInstaller)
a partir de APP_VERSION defini dans kodak_monitor.py.
"""
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_FILE = os.path.join(SCRIPT_DIR, "kodak_monitor.py")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "version_info.txt")
RELEASE_VERSION_FILE = os.path.join(SCRIPT_DIR, "release", "VERSION.txt")

COMPANY_NAME = "KM SHIVA"
FILE_DESCRIPTION = "Kodak Printer Monitor"
INTERNAL_NAME = "KodakMonitor"
ORIGINAL_FILENAME = "KodakMonitor.exe"
PRODUCT_NAME = "Kodak Printer Monitor"


def _read_app_version():
    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', content)
    if not m:
        raise SystemExit("ERREUR: APP_VERSION introuvable dans kodak_monitor.py")
    return m.group(1)


def _version_tuple(version_str):
    parts = re.findall(r"\d+", version_str)
    nums = [int(p) for p in parts[:4]]
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)


def main():
    version_str = _read_app_version()
    vt = _version_tuple(version_str)
    version_dotted = ".".join(str(n) for n in vt)

    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vt!r},
    prodvers={vt!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040c04b0',
        [
          StringStruct('CompanyName', '{COMPANY_NAME}'),
          StringStruct('FileDescription', '{FILE_DESCRIPTION}'),
          StringStruct('FileVersion', '{version_dotted}'),
          StringStruct('InternalName', '{INTERNAL_NAME}'),
          StringStruct('OriginalFilename', '{ORIGINAL_FILENAME}'),
          StringStruct('ProductName', '{PRODUCT_NAME}'),
          StringStruct('ProductVersion', '{version_dotted}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1036, 1200])])
  ]
)
"""
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    os.makedirs(os.path.dirname(RELEASE_VERSION_FILE), exist_ok=True)
    with open(RELEASE_VERSION_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(version_dotted + "\n")

    print(f"version_info.txt genere - version {version_dotted}")
    print(f"release/VERSION.txt synchronise - version {version_dotted}")


if __name__ == "__main__":
    main()
