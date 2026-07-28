"""WebView PyInstaller hook for the zvec Windows wheel.

The native extension is discovered as a binary, while the tokenizer dictionaries
and distribution metadata are data files that static analysis cannot infer.
"""

from PyInstaller.utils.hooks import (  # type: ignore[import-not-found]
    collect_data_files,
    collect_dynamic_libs,
    copy_metadata,
)

binaries = collect_dynamic_libs("zvec")
datas = collect_data_files(
    "zvec",
    includes=["data/jieba_dict/*"],
) + copy_metadata("zvec")
hiddenimports = ["zvec._zvec"]
