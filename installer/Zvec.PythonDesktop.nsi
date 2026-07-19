Unicode True
ManifestDPIAware True
RequestExecutionLevel user
SetCompressor /SOLID lzma

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
!ifndef APP_ICON
  !define APP_ICON "${__FILEDIR__}\..\assets\Zvec.AppIcon.ico"
!endif

!define PRODUCT_NAME "Zvec Desktop"
!define PRODUCT_PUBLISHER "Zvec"
; Keep the primary product identity so the Python release upgrades the former
; legacy desktop client in place instead of creating a second application registration.
!define PRODUCT_ID "{68CDA461-FA06-44DC-A735-2A9C42F46D13}"
!define PRODUCT_MARKER ".zvec-desktop-install.ini"
!ifndef PRODUCT_REG_KEY
  !define PRODUCT_REG_KEY "Software\Zvec\Desktop"
!endif
!ifndef PRODUCT_UNINSTALL_KEY
  !define PRODUCT_UNINSTALL_KEY \
    "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZvecDesktop"
!endif
!ifndef INSTALL_DIR
  !define INSTALL_DIR "$LOCALAPPDATA\Programs\Zvec Desktop"
!endif
!ifndef START_MENU_SHORTCUT
  !define START_MENU_SHORTCUT "$SMPROGRAMS\Zvec Desktop.lnk"
!endif
!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"

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

!macro RequireProcessStopped IMAGE_NAME DISPLAY_NAME
  nsExec::ExecToStack '"$SYSDIR\tasklist.exe" /NH /FI "IMAGENAME eq ${IMAGE_NAME}"'
  Pop $0
  Pop $1
  ${If} $0 != "0"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP|MB_OK \
        "Could not verify whether ${DISPLAY_NAME} is running. No files were changed."
    ${EndIf}
    SetErrorLevel 34
    Quit
  ${EndIf}
  ${StrStr} $2 $1 "${IMAGE_NAME}"
  ${If} $2 != ""
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP|MB_OK \
        "${DISPLAY_NAME} is running. Finish its work and exit it before continuing."
    ${EndIf}
    SetErrorLevel 32
    Quit
  ${EndIf}
!macroend

!macro un.RequireProcessStopped IMAGE_NAME DISPLAY_NAME
  nsExec::ExecToStack '"$SYSDIR\tasklist.exe" /NH /FI "IMAGENAME eq ${IMAGE_NAME}"'
  Pop $0
  Pop $1
  ${If} $0 != "0"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP|MB_OK \
        "Could not verify whether ${DISPLAY_NAME} is running. No files were changed."
    ${EndIf}
    SetErrorLevel 34
    Quit
  ${EndIf}
  ${UnStrStr} $2 $1 "${IMAGE_NAME}"
  ${If} $2 != ""
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP|MB_OK \
        "${DISPLAY_NAME} is running. Finish its work and exit it before continuing."
    ${EndIf}
    SetErrorLevel 32
    Quit
  ${EndIf}
!macroend

; Probe both generations of the native backend without invoking an external shell.
; launch.lock is held by an exclusive file handle during startup/attachment;
; backend.lock is retained on disk and protected by a one-byte OS lock while
; the service is alive.  Unknown access failures fail closed.
!macro RequirePersistentBackendStopped UNINSTALL_PREFIX
  ReadEnvStr $0 "ZVEC_CONFIG_HOME"
  StrCmp $0 "" 0 config_home_selected_${UNINSTALL_PREFIX}
  ReadEnvStr $0 "ZVEC_DOCKER_CONFIG_HOME"
  StrCmp $0 "" 0 config_home_selected_${UNINSTALL_PREFIX}
  StrCpy $0 "$LOCALAPPDATA\zvec-image-search"

config_home_selected_${UNINSTALL_PREFIX}:
  ExpandEnvStrings $0 $0
  StrCpy $1 "$0\backend\launch.lock"
  System::Call \
    'kernel32::CreateFileW(w r1, i 0xC0000000, i 0, p 0, i 3, i 0x80, p 0) p.r2 ? e'
  Pop $3
  IntCmp $2 -1 launch_probe_failed_${UNINSTALL_PREFIX} \
    launch_probe_free_${UNINSTALL_PREFIX} launch_probe_free_${UNINSTALL_PREFIX}

launch_probe_failed_${UNINSTALL_PREFIX}:
  IntCmp $3 2 launch_probe_free_${UNINSTALL_PREFIX}
  IntCmp $3 3 launch_probe_free_${UNINSTALL_PREFIX}
  IntCmp $3 32 backend_active_${UNINSTALL_PREFIX}
  IntCmp $3 33 backend_active_${UNINSTALL_PREFIX}
  Goto backend_probe_unknown_${UNINSTALL_PREFIX}

launch_probe_free_${UNINSTALL_PREFIX}:
  IntCmp $2 -1 +2 +2 0
  System::Call 'kernel32::CloseHandle(p r2)'
  StrCpy $1 "$0\backend\backend.lock"
  System::Call \
    'kernel32::CreateFileW(w r1, i 0xC0000000, i 3, p 0, i 3, i 0x80, p 0) p.r2 ? e'
  Pop $3
  IntCmp $2 -1 backend_open_failed_${UNINSTALL_PREFIX} \
    backend_opened_${UNINSTALL_PREFIX} backend_opened_${UNINSTALL_PREFIX}

backend_open_failed_${UNINSTALL_PREFIX}:
  IntCmp $3 2 backend_probe_free_${UNINSTALL_PREFIX}
  IntCmp $3 3 backend_probe_free_${UNINSTALL_PREFIX}
  IntCmp $3 32 backend_active_${UNINSTALL_PREFIX}
  IntCmp $3 33 backend_active_${UNINSTALL_PREFIX}
  Goto backend_probe_unknown_${UNINSTALL_PREFIX}

backend_opened_${UNINSTALL_PREFIX}:
  System::Call 'kernel32::LockFile(p r2, i 0, i 0, i 1, i 0) i.r3 ? e'
  Pop $4
  IntCmp $3 0 backend_lock_failed_${UNINSTALL_PREFIX} \
    backend_lock_acquired_${UNINSTALL_PREFIX} backend_lock_acquired_${UNINSTALL_PREFIX}

backend_lock_failed_${UNINSTALL_PREFIX}:
  System::Call 'kernel32::CloseHandle(p r2)'
  IntCmp $4 32 backend_active_${UNINSTALL_PREFIX}
  IntCmp $4 33 backend_active_${UNINSTALL_PREFIX}
  Goto backend_probe_unknown_${UNINSTALL_PREFIX}

backend_lock_acquired_${UNINSTALL_PREFIX}:
  System::Call 'kernel32::UnlockFile(p r2, i 0, i 0, i 1, i 0)'
  System::Call 'kernel32::CloseHandle(p r2)'

backend_probe_free_${UNINSTALL_PREFIX}:
  Goto backend_probe_done_${UNINSTALL_PREFIX}

backend_active_${UNINSTALL_PREFIX}:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "The Zvec background service is active. Finish all tasks and exit Zvec before continuing."
  ${EndIf}
  SetErrorLevel 35
  Quit

backend_probe_unknown_${UNINSTALL_PREFIX}:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "Zvec could not safely verify the background service. No files were changed."
  ${EndIf}
  SetErrorLevel 36
  Quit

backend_probe_done_${UNINSTALL_PREFIX}:
!macroend

Function EnsureZvecStopped
  !ifndef SKIP_PROCESS_GUARDS
    !insertmacro RequireProcessStopped "Zvec.Desktop.exe" "Zvec Desktop"
    !insertmacro RequireProcessStopped \
      "zvec-backend.exe" "The Zvec background service"
    !insertmacro RequireProcessStopped "zvec.exe" "The Zvec command-line tool"
  !endif
  !insertmacro RequirePersistentBackendStopped "install"
FunctionEnd

Function un.EnsureZvecStopped
  !ifndef SKIP_PROCESS_GUARDS
    !insertmacro un.RequireProcessStopped "Zvec.Desktop.exe" "Zvec Desktop"
    !insertmacro un.RequireProcessStopped \
      "zvec-backend.exe" "The Zvec background service"
    !insertmacro un.RequireProcessStopped "zvec.exe" "The Zvec command-line tool"
  !endif
  !insertmacro RequirePersistentBackendStopped "uninstall"
FunctionEnd

Function .onInit
  SetShellVarContext current
  ; Ignore /D overrides. This directory is owned only after marker validation.
  StrCpy $INSTDIR "${INSTALL_DIR}"
FunctionEnd

Function un.onInit
  SetShellVarContext current
  StrCpy $INSTDIR "${INSTALL_DIR}"
FunctionEnd

Section "Zvec Desktop" SEC_APP
  SetShellVarContext current
  Call EnsureZvecStopped

  IfFileExists "$INSTDIR\${PRODUCT_MARKER}" marker_present check_unowned_dir

marker_present:
  ReadINIStr $0 "$INSTDIR\${PRODUCT_MARKER}" "ZvecDesktop" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" owned_install_dir unowned_install_dir

check_unowned_dir:
  IfFileExists "$INSTDIR\*" unowned_install_dir prepare_install_dir

owned_install_dir:
  ; Repeat the guards immediately before the destructive upgrade boundary.
  Call EnsureZvecStopped
  RMDir /r "$INSTDIR"
  Goto prepare_install_dir

unowned_install_dir:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "The destination is not owned by Zvec. No files were removed: $INSTDIR"
  ${EndIf}
  SetErrorLevel 33
  Quit

prepare_install_dir:
  SetOutPath "$INSTDIR"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecDesktop" "ProductId" "${PRODUCT_ID}"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecDesktop" "Version" "${VERSION}"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecDesktop" "RuntimeIdentifier" "${RID}"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecDesktop" "SigningStatus" "${SIGNING_STATUS}"
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
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Call un.EnsureZvecStopped
  ReadINIStr $0 "$INSTDIR\${PRODUCT_MARKER}" "ZvecDesktop" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" owned_uninstall_dir unowned_uninstall_dir

unowned_uninstall_dir:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "The Zvec ownership marker is invalid. No files were removed: $INSTDIR"
  ${EndIf}
  SetErrorLevel 33
  Quit

owned_uninstall_dir:
  Call un.EnsureZvecStopped
  Delete "${START_MENU_SHORTCUT}"
  DeleteRegKey HKCU "${PRODUCT_UNINSTALL_KEY}"
  DeleteRegKey HKCU "${PRODUCT_REG_KEY}"
  RMDir /r "$INSTDIR"
SectionEnd
