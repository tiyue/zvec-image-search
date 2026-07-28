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
!ifndef DISPLAY_VERSION
  !error "DISPLAY_VERSION is required"
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
!ifndef APP_ICON
  !define APP_ICON "${__FILEDIR__}\..\assets\Zvec.AppIcon.ico"
!endif

; YaoLens keeps the existing ProductId so the former installation identity can
; be migrated in place without creating a second installed product.
!define PRODUCT_NAME "YaoLens"
!define PRODUCT_PUBLISHER "YaoLens"
!define PRODUCT_ID "{7A99D348-73DF-49EA-AF2B-8D86FC6DA2A8}"
!define PRODUCT_MARKER ".zvec-webview-preview-install.ini"
!define PRODUCT_REG_KEY "Software\YaoLens"
!define PRODUCT_UNINSTALL_KEY \
  "Software\Microsoft\Windows\CurrentVersion\Uninstall\YaoLens"
!define INSTALL_DIR "$LOCALAPPDATA\Programs\YaoLens"
!define START_MENU_SHORTCUT "$SMPROGRAMS\YaoLens.lnk"
!define MAIN_EXE "YaoLens.exe"
!define LEGACY_PRODUCT_REG_KEY "Software\Zvec\WebviewPreview"
!define LEGACY_PRODUCT_UNINSTALL_KEY \
  "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZvecWebviewPreview"
!define LEGACY_INSTALL_DIR "$LOCALAPPDATA\Programs\Zvec Webview Preview"
!define LEGACY_START_MENU_SHORTCUT "$SMPROGRAMS\Zvec Webview Preview.lnk"
!define LEGACY_MAIN_EXE "Zvec.WebviewPreview.exe"
!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"

!define PRODUCT_CHANNEL_SUFFIX ""

Name "${PRODUCT_NAME} ${DISPLAY_VERSION}${PRODUCT_CHANNEL_SUFFIX}"
OutFile "${OUTPUT_FILE}"
InstallDir "${INSTALL_DIR}"
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"

VIProductVersion "${FILE_VERSION}"
VIAddVersionKey /LANG=1033 "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey /LANG=1033 "CompanyName" "${PRODUCT_PUBLISHER}"
VIAddVersionKey /LANG=1033 "FileDescription" \
  "${PRODUCT_NAME} ${RID} Installer${PRODUCT_CHANNEL_SUFFIX}"
VIAddVersionKey /LANG=1033 "FileVersion" "${DISPLAY_VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${DISPLAY_VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Copyright YaoLens contributors"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${MAIN_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Run YaoLens"

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
        "${DISPLAY_NAME} is running. Exit it before continuing."
    ${EndIf}
    SetErrorLevel 32
    Quit
  ${EndIf}
!macroend

!macro WaitForProcessExit IMAGE_NAME
  ; Poll for at most about ten seconds. A failed query returns immediately so
  ; the authoritative RequireProcessStopped check can fail closed afterwards.
  StrCpy $3 0
  ${Do}
    nsExec::ExecToStack '"$SYSDIR\tasklist.exe" /NH /FI "IMAGENAME eq ${IMAGE_NAME}"'
    Pop $0
    Pop $1
    ${If} $0 != "0"
      ${ExitDo}
    ${EndIf}
    ${StrStr} $2 $1 "${IMAGE_NAME}"
    ${If} $2 == ""
      ${ExitDo}
    ${EndIf}
    IntOp $3 $3 + 1
    ${If} $3 >= 40
      ${ExitDo}
    ${EndIf}
    Sleep 250
  ${Loop}
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
        "${DISPLAY_NAME} is running. Exit it before continuing."
    ${EndIf}
    SetErrorLevel 32
    Quit
  ${EndIf}
!macroend

!macro un.WaitForProcessExit IMAGE_NAME
  StrCpy $3 0
  ${Do}
    nsExec::ExecToStack '"$SYSDIR\tasklist.exe" /NH /FI "IMAGENAME eq ${IMAGE_NAME}"'
    Pop $0
    Pop $1
    ${If} $0 != "0"
      ${ExitDo}
    ${EndIf}
    ${UnStrStr} $2 $1 "${IMAGE_NAME}"
    ${If} $2 == ""
      ${ExitDo}
    ${EndIf}
    IntOp $3 $3 + 1
    ${If} $3 >= 40
      ${ExitDo}
    ${EndIf}
    Sleep 250
  ${Loop}
!macroend

Function EnsureZvecStopped
  !insertmacro RequireProcessStopped "${MAIN_EXE}" "YaoLens"
  !insertmacro RequireProcessStopped "${LEGACY_MAIN_EXE}" "YaoLens"
  !insertmacro RequireProcessStopped "zvec-backend.exe" "The YaoLens background service"
  !insertmacro RequireProcessStopped "zvec.exe" "The YaoLens command-line tool"
FunctionEnd

Function RequestZvecExit
  IfFileExists "$INSTDIR\${MAIN_EXE}" 0 request_exit_done
  nsExec::ExecToStack '"$INSTDIR\${MAIN_EXE}" --exit-running-instance'
  Pop $0
  Pop $1
  ; The process inventory below is authoritative. Older owned versions may not
  ; understand this cooperative command and will remain visible to that check.
  !insertmacro WaitForProcessExit "${MAIN_EXE}"
request_exit_done:
FunctionEnd

Function RequestLegacyZvecExit
  IfFileExists "${LEGACY_INSTALL_DIR}\${LEGACY_MAIN_EXE}" 0 request_exit_done
  nsExec::ExecToStack \
    '"${LEGACY_INSTALL_DIR}\${LEGACY_MAIN_EXE}" --exit-running-instance'
  Pop $0
  Pop $1
  !insertmacro WaitForProcessExit "${LEGACY_MAIN_EXE}"
request_exit_done:
FunctionEnd

Function InstallResidentTask
  nsExec::ExecToStack '"$INSTDIR\${MAIN_EXE}" --install-resident-task'
  Pop $0
  Pop $1
  ${If} $0 != "0"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP|MB_OK \
        "Could not register the current-user YaoLens logon task. The installer cannot continue."
    ${EndIf}
    SetErrorLevel 35
    Quit
  ${EndIf}
FunctionEnd

Function un.EnsureZvecStopped
  !insertmacro un.RequireProcessStopped "${MAIN_EXE}" "YaoLens"
  !insertmacro un.RequireProcessStopped "${LEGACY_MAIN_EXE}" "YaoLens"
  !insertmacro un.RequireProcessStopped "zvec-backend.exe" "The YaoLens background service"
  !insertmacro un.RequireProcessStopped "zvec.exe" "The YaoLens command-line tool"
FunctionEnd

Function un.RequestZvecExit
  IfFileExists "$INSTDIR\${MAIN_EXE}" 0 request_exit_done
  nsExec::ExecToStack '"$INSTDIR\${MAIN_EXE}" --exit-running-instance'
  Pop $0
  Pop $1
  !insertmacro un.WaitForProcessExit "${MAIN_EXE}"
  !insertmacro un.WaitForProcessExit "${LEGACY_MAIN_EXE}"
request_exit_done:
FunctionEnd

Function un.RemoveResidentTask
  nsExec::ExecToStack '"$INSTDIR\${MAIN_EXE}" --remove-resident-task'
  Pop $0
  Pop $1
  ${If} $0 != "0"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP|MB_OK \
        "Could not remove the current-user YaoLens logon task. No installed files were removed."
    ${EndIf}
    SetErrorLevel 35
    Quit
  ${EndIf}
FunctionEnd

Function .onInit
  SetShellVarContext current
  ; Ignore /D overrides: deletion is allowed only after validating our marker.
  StrCpy $INSTDIR "${INSTALL_DIR}"
FunctionEnd

Function un.onInit
  SetShellVarContext current
  StrCpy $INSTDIR "${INSTALL_DIR}"
FunctionEnd

Section "YaoLens" SEC_APP
  SetShellVarContext current

  IfFileExists "$INSTDIR\${PRODUCT_MARKER}" marker_present check_unowned_dir

marker_present:
  ReadINIStr $0 "$INSTDIR\${PRODUCT_MARKER}" "ZvecWebviewPreview" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" owned_install_dir unowned_install_dir

check_unowned_dir:
  IfFileExists "$INSTDIR\*" unowned_install_dir fresh_install_dir

fresh_install_dir:
  StrCpy $5 "0"
  Goto inspect_legacy_install

owned_install_dir:
  StrCpy $5 "1"
  Call RequestZvecExit
  Goto inspect_legacy_install

inspect_legacy_install:
  StrCpy $6 "0"
  ReadRegStr $4 HKCU "${LEGACY_PRODUCT_REG_KEY}" "InstallDir"
  ${If} $4 != ""
    StrCmp $4 "${LEGACY_INSTALL_DIR}" legacy_registry_ok legacy_unowned_dir
  ${EndIf}

legacy_registry_ok:
  IfFileExists "${LEGACY_INSTALL_DIR}\${PRODUCT_MARKER}" \
    legacy_marker_present no_legacy_install

legacy_marker_present:
  ReadINIStr $0 "${LEGACY_INSTALL_DIR}\${PRODUCT_MARKER}" \
    "ZvecWebviewPreview" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" legacy_owned_install legacy_unowned_dir

legacy_owned_install:
  StrCpy $6 "1"
  Call RequestLegacyZvecExit
  Goto stop_owned_processes

no_legacy_install:
  ${If} $4 != ""
    Goto legacy_unowned_dir
  ${EndIf}
  Goto stop_owned_processes

stop_owned_processes:
  Call EnsureZvecStopped
  ${If} $5 == "1"
    RMDir /r "$INSTDIR"
  ${EndIf}
  Goto prepare_install_dir

unowned_install_dir:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "The destination is not owned by YaoLens. No files were removed: $INSTDIR"
  ${EndIf}
  SetErrorLevel 33
  Quit

legacy_unowned_dir:
  ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP|MB_OK \
      "The previous YaoLens installation could not be verified, so it was not changed."
  ${EndIf}
  SetErrorLevel 33
  Quit

prepare_install_dir:
  SetOutPath "$INSTDIR"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecWebviewPreview" "ProductId" "${PRODUCT_ID}"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecWebviewPreview" "Version" "${VERSION}"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecWebviewPreview" "RuntimeIdentifier" "${RID}"
  File /r "${SOURCE_DIR}\*.*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "${START_MENU_SHORTCUT}" "$INSTDIR\${MAIN_EXE}"

  WriteRegStr HKCU "${PRODUCT_REG_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_REG_KEY}" "RuntimeIdentifier" "${RID}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "DisplayName" "${PRODUCT_NAME}${PRODUCT_CHANNEL_SUFFIX}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "DisplayVersion" "${DISPLAY_VERSION}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "DisplayIcon" "$INSTDIR\${MAIN_EXE}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${PRODUCT_UNINSTALL_KEY}" "NoRepair" 1

  ; This call exists only in the installer. The portable ZIP never registers
  ; startup integration.
  Call EnsureZvecStopped
  Call InstallResidentTask

  ${If} $6 == "1"
    Delete "${LEGACY_START_MENU_SHORTCUT}"
    DeleteRegKey HKCU "${LEGACY_PRODUCT_UNINSTALL_KEY}"
    DeleteRegKey HKCU "${LEGACY_PRODUCT_REG_KEY}"
    RMDir /r "${LEGACY_INSTALL_DIR}"
  ${EndIf}
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  ReadINIStr $0 "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecWebviewPreview" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" owned_uninstall_dir unowned_uninstall_dir

unowned_uninstall_dir:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "The ownership marker is invalid. No files were removed: $INSTDIR"
  ${EndIf}
  SetErrorLevel 33
  Quit

owned_uninstall_dir:
  Call un.RequestZvecExit
  Call un.EnsureZvecStopped
  Call un.RemoveResidentTask
  Delete "${START_MENU_SHORTCUT}"
  DeleteRegKey HKCU "${PRODUCT_UNINSTALL_KEY}"
  DeleteRegValue HKCU "${PRODUCT_REG_KEY}" "InstallDir"
  DeleteRegValue HKCU "${PRODUCT_REG_KEY}" "RuntimeIdentifier"
  DeleteRegKey /ifempty HKCU "${PRODUCT_REG_KEY}"
  ReadINIStr $0 "${LEGACY_INSTALL_DIR}\${PRODUCT_MARKER}" \
    "ZvecWebviewPreview" "ProductId"
  ${If} $0 == "${PRODUCT_ID}"
    Delete "${LEGACY_START_MENU_SHORTCUT}"
    DeleteRegKey HKCU "${LEGACY_PRODUCT_UNINSTALL_KEY}"
    DeleteRegKey HKCU "${LEGACY_PRODUCT_REG_KEY}"
    RMDir /r "${LEGACY_INSTALL_DIR}"
  ${EndIf}
  RMDir /r "$INSTDIR"
SectionEnd
