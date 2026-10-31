"""生成 exe 的版本资源（VSVersionInfo）。

Windows 的"属性 → 详细信息"面板读的就是这份资源。没有它，exe 显示的是
空白的文件版本，出问题时连"用户装的是哪一版"都问不出来。

版本号来源只有一个：``lbamover.VERSION`` / ``REVISION``。这里不写死任何
数字，避免打包产物和代码里的版本号对不上。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from lbamover import REVISION, VERSION  # noqa: E402


def numeric_version() -> str:
    """Windows 要求四段数字版本；缺的补 0。字母 revision 不能进这里。"""
    parts = [p for p in VERSION.split(".") if p.isdigit()][:3]
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts) + ".0"


def render() -> str:
    v4 = numeric_version()
    display = f"{VERSION}{REVISION}"
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v4.replace('.', ', ')}),
    prodvers=({v4.replace('.', ', ')}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [StringStruct('CompanyName', 'LBAMover'),
         StringStruct('FileDescription', 'LBAMover — LBA 块级流式整理'),
         StringStruct('FileVersion', '{v4}'),
         StringStruct('InternalName', 'LBAMover'),
         StringStruct('LegalCopyright', ''),
         StringStruct('OriginalFilename', 'LBAMover.exe'),
         StringStruct('ProductName', 'LBAMover'),
         StringStruct('ProductVersion', '{display}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def main() -> int:
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "version_info.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render())
    print(f"version -> {out} (file={numeric_version()}, product={VERSION}{REVISION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
