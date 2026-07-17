Unicode True
ManifestDPIAware True
RequestExecutionLevel user
SetCompressor /SOLID lzma

!ifndef APP_ICON
  !define APP_ICON "${__FILEDIR__}\..\desktop\Zvec.Desktop\Assets\Zvec.AppIcon.ico"
!endif
!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "StrFunc.nsh"

${StrStr}
${UnStrStr}

!ifndef VERSION
  !error "VERSION is required"
!endif
!ifndef FILE_VERSION
  !error "FILE_VERSION is required"
!endif
!ifndef RID
  !error "RID is required"
!endif
!ifndef SOURCE_DIR
  !error "SOURCE_DIR is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif
!ifndef SIGNING_STATUS
  !define SIGNING_STATUS "unsigned"
!endif
!ifndef BACKEND_GUARD_SCRIPT
  !define BACKEND_GUARD_SCRIPT "${__FILEDIR__}\check-persistent-backend.ps1"
!endif
!ifndef INSTALL_DIR
  !define INSTALL_DIR "$LOCALAPPDATA\Programs\Zvec Desktop"
!endif

!define PRODUCT_NAME "Zvec Desktop"
!define PRODUCT_PUBLISHER "Zvec"
!define PRODUCT_ID "{68CDA461-FA06-44DC-A735-2A9C42F46D13}"
!define PRODUCT_MARKER ".zvec-desktop-install.ini"
!ifndef PRODUCT_REG_KEY
  !define PRODUCT_REG_KEY "Software\Zvec\Desktop"
!endif
!ifndef PRODUCT_UNINSTALL_KEY
  !define PRODUCT_UNINSTALL_KEY \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZvecDesktop"
!endif
!ifndef START_MENU_SHORTCUT
  !define START_MENU_SHORTCUT "$SMPROGRAMS\Zvec Desktop.lnk"
!endif

!if "${SIGNING_STATUS}" == "unsigned"
  !define PRODUCT_CHANNEL_SUFFIX " (Unsigned Preview)"
!else
  !define PRODUCT_CHANNEL_SUFFIX ""
!endif

Name "${PRODUCT_NAME} ${VERSION}${PRODUCT_CHANNEL_SUFFIX}"
OutFile "${OUTPUT_FILE}"
InstallDir "${INSTALL_DIR}"
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"

VIProductVersion "${FILE_VERSION}"
VIAddVersionKey /LANG=1033 "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey /LANG=1033 "CompanyName" "${PRODUCT_PUBLISHER}"
VIAddVersionKey /LANG=1033 "FileDescription" \
  "${PRODUCT_NAME} ${RID} Installer${PRODUCT_CHANNEL_SUFFIX}"
VIAddVersionKey /LANG=1033 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Copyright Zvec contributors"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\Zvec.Desktop.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Run Zvec Desktop"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  SetShellVarContext current
  ; Ignore /D and registry overrides. This directory is exclusively owned by Zvec.
  StrCpy $INSTDIR "${INSTALL_DIR}"
FunctionEnd

Function un.onInit
  SetShellVarContext current
  StrCpy $INSTDIR "${INSTALL_DIR}"
FunctionEnd

Function EnsureDesktopStopped
  nsExec::ExecToStack '"$SYSDIR\tasklist.exe" /NH /FI "IMAGENAME eq Zvec.Desktop.exe"'
  Pop $0
  Pop $1
  StrCmp $0 "0" tasklist_ok tasklist_failed

tasklist_failed:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "Could not verify whether Zvec Desktop is running. Installation was stopped safely."
  ${EndIf}
  SetErrorLevel 34
  Quit

tasklist_ok:
  ${StrStr} $2 $1 "Zvec.Desktop.exe"
  StrCmp $2 "" desktop_not_running
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "Zvec Desktop is running. Exit it from the system tray before installing or upgrading."
  ${EndIf}
  SetErrorLevel 32
  Quit

desktop_not_running:
FunctionEnd

Function un.EnsureDesktopStopped
  nsExec::ExecToStack '"$SYSDIR\tasklist.exe" /NH /FI "IMAGENAME eq Zvec.Desktop.exe"'
  Pop $0
  Pop $1
  StrCmp $0 "0" un_tasklist_ok un_tasklist_failed

un_tasklist_failed:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "Could not verify whether Zvec Desktop is running. Uninstall was stopped safely."
  ${EndIf}
  SetErrorLevel 34
  Quit

un_tasklist_ok:
  ${UnStrStr} $2 $1 "Zvec.Desktop.exe"
  StrCmp $2 "" un_desktop_not_running
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "Zvec Desktop is running. Exit it from the system tray before uninstalling."
  ${EndIf}
  SetErrorLevel 32
  Quit

un_desktop_not_running:
FunctionEnd

Function EnsurePersistentBackendStopped
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=zvec-check-persistent-backend.ps1 "${BACKEND_GUARD_SCRIPT}"
  nsExec::ExecToStack \
    '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\zvec-check-persistent-backend.ps1"'
  Pop $0
  Pop $1
  StrCmp $0 "0" persistent_backend_not_running
  StrCmp $0 "35" persistent_backend_running persistent_backend_check_failed

persistent_backend_running:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "Zvec is still starting or its background service is active. No files were changed. Open Zvec Desktop, wait for all tasks to finish, then choose Exit on the page or from the system tray."
  ${EndIf}
  SetErrorLevel 35
  Quit

persistent_backend_check_failed:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "Could not safely verify the Zvec background service. No files were changed. Open Zvec Desktop once, then choose Exit on the page or from the system tray. If the warning remains, restart Windows and retry."
  ${EndIf}
  SetErrorLevel 36
  Quit

persistent_backend_not_running:
FunctionEnd

Function un.EnsurePersistentBackendStopped
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=zvec-check-persistent-backend.ps1 "${BACKEND_GUARD_SCRIPT}"
  nsExec::ExecToStack \
    '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\zvec-check-persistent-backend.ps1"'
  Pop $0
  Pop $1
  StrCmp $0 "0" un_persistent_backend_not_running
  StrCmp $0 "35" un_persistent_backend_running un_persistent_backend_check_failed

un_persistent_backend_running:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "Zvec is still starting or its background service is active. No files were changed. Open Zvec Desktop, wait for all tasks to finish, then choose Exit on the page or from the system tray."
  ${EndIf}
  SetErrorLevel 35
  Quit

un_persistent_backend_check_failed:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "Could not safely verify the Zvec background service. No files were changed. Open Zvec Desktop once, then choose Exit on the page or from the system tray. If the warning remains, restart Windows and retry."
  ${EndIf}
  SetErrorLevel 36
  Quit

un_persistent_backend_not_running:
FunctionEnd

Section "Zvec Desktop" SEC_APP
  SetShellVarContext current
  Call EnsureDesktopStopped
  Call EnsurePersistentBackendStopped

  ; Only clean a directory carrying our validated ownership marker. An existing
  ; unowned directory is never recursively removed.
  IfFileExists "$INSTDIR\${PRODUCT_MARKER}" marker_present check_unowned_dir

marker_present:
  ReadINIStr $0 "$INSTDIR\${PRODUCT_MARKER}" "ZvecDesktop" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" owned_install_dir unowned_install_dir

check_unowned_dir:
  IfFileExists "$INSTDIR\*" unowned_install_dir prepare_install_dir

owned_install_dir:
  ; Re-check immediately before recursive replacement. This closes the ordinary race
  ; where the user starts Zvec while an upgrade is validating the owned directory.
  Call EnsureDesktopStopped
  Call EnsurePersistentBackendStopped
  RMDir /r "$INSTDIR"
  Goto prepare_install_dir

unowned_install_dir:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "The fixed Zvec install directory exists but is not owned by Zvec. No files were removed: $INSTDIR"
  ${EndIf}
  SetErrorLevel 33
  Quit

prepare_install_dir:
  SetOutPath "$INSTDIR"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecDesktop" "ProductId" "${PRODUCT_ID}"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecDesktop" "RuntimeIdentifier" "${RID}"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecDesktop" "Version" "${VERSION}"
  File /r "${SOURCE_DIR}\*.*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "${START_MENU_SHORTCUT}" "$INSTDIR\Zvec.Desktop.exe"

  WriteRegStr HKCU "${PRODUCT_REG_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_REG_KEY}" "RuntimeIdentifier" "${RID}"

  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "DisplayName" "${PRODUCT_NAME}${PRODUCT_CHANNEL_SUFFIX}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "DisplayIcon" "$INSTDIR\Zvec.Desktop.exe"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Call un.EnsureDesktopStopped
  Call un.EnsurePersistentBackendStopped

  ReadINIStr $0 "$INSTDIR\${PRODUCT_MARKER}" "ZvecDesktop" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" owned_uninstall_dir unowned_uninstall_dir

unowned_uninstall_dir:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "The Zvec ownership marker is missing or invalid. No files were removed: $INSTDIR"
  ${EndIf}
  SetErrorLevel 33
  Quit

owned_uninstall_dir:
  ; Repeat both guards at the destructive boundary for the same reason as upgrade.
  Call un.EnsureDesktopStopped
  Call un.EnsurePersistentBackendStopped
  Delete "${START_MENU_SHORTCUT}"
  DeleteRegKey HKCU "${PRODUCT_UNINSTALL_KEY}"
  DeleteRegKey HKCU "${PRODUCT_REG_KEY}"
  RMDir /r "$INSTDIR"
SectionEnd
