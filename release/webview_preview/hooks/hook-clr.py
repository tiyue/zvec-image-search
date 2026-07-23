"""PyInstaller hook for pythonnet + clr_loader .NET runtime files.

pythonnet 3.1.0 ships Python.Runtime.dll alongside 80+ .NET Standard 2.0
facade assemblies (netstandard.dll, System.Runtime.dll, etc.) in its
``runtime/`` directory.  When clr_loader loads Python.Runtime.dll via the
.NET Framework CLR custom AppDomain, these facades must be co-located so
that type resolution succeeds.  Without them the CLR returns NULL from
``pyclr_get_function`` and the user sees:

    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
    ...\\_internal\\pythonnet\\runtime\\Python.Runtime.dll

This hook explicitly collects every file under ``pythonnet/runtime/`` as
data files so PyInstaller places them at ``_internal/pythonnet/runtime/``
in the frozen payload.  It also ensures ``ClrLoader.dll`` (the native
interop shim used by clr_loader's .NET Framework backend) is bundled.
"""

from __future__ import annotations

from PyInstaller.utils.hooks import (  # type: ignore[import-not-found]
    collect_data_files,
    collect_dynamic_libs,
)

# pythonnet: collect ALL runtime files (Python.Runtime.dll + facade assemblies)
datas = collect_data_files("pythonnet", includes=["runtime/**"])
binaries = collect_dynamic_libs("pythonnet")

# clr_loader: ensure ClrLoader.dll native interop is collected
datas += collect_data_files("clr_loader", includes=["ffi/dlls/**"])
