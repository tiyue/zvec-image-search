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

; The WebView preview intentionally has its own installation identity.  It can
; be tested beside the existing desktop client without replacing user files.
!define PRODUCT_NAME "Zvec Webview Preview"
!define PRODUCT_PUBLISHER "Zvec"
!define PRODUCT_ID "{7A99D348-73DF-49EA-AF2B-8D86FC6DA2A8}"
!define PRODUCT_MARKER ".zvec-webview-preview-install.ini"
!define PRODUCT_REG_KEY "Software\Zvec\WebviewPreview"
!define PRODUCT_UNINSTALL_KEY \
  "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZvecWebviewPreview"
!define INSTALL_DIR "$LOCALAPPDATA\Programs\Zvec Webview Preview"
!define START_MENU_SHORTCUT "$SMPROGRAMS\Zvec Webview Preview.lnk"
!define MAIN_EXE "Zvec.WebviewPreview.exe"
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
!define MUI_FINISHPAGE_RUN "$INSTDIR\${MAIN_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "Run Zvec Webview Preview"

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

Function EnsureZvecStopped
  !insertmacro RequireProcessStopped "${MAIN_EXE}" "Zvec Webview Preview"
  !insertmacro RequireProcessStopped "zvec-backend.exe" "The Zvec background service"
  !insertmacro RequireProcessStopped "zvec.exe" "The Zvec command-line tool"
FunctionEnd

Function un.EnsureZvecStopped
  !insertmacro un.RequireProcessStopped "${MAIN_EXE}" "Zvec Webview Preview"
  !insertmacro un.RequireProcessStopped "zvec-backend.exe" "The Zvec background service"
  !insertmacro un.RequireProcessStopped "zvec.exe" "The Zvec command-line tool"
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

Section "Zvec Webview Preview" SEC_APP
  SetShellVarContext current
  Call EnsureZvecStopped

  IfFileExists "$INSTDIR\${PRODUCT_MARKER}" marker_present check_unowned_dir

marker_present:
  ReadINIStr $0 "$INSTDIR\${PRODUCT_MARKER}" "ZvecWebviewPreview" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" owned_install_dir unowned_install_dir

check_unowned_dir:
  IfFileExists "$INSTDIR\*" unowned_install_dir prepare_install_dir

owned_install_dir:
  ; Recheck immediately before removing an older owned payload.
  Call EnsureZvecStopped
  RMDir /r "$INSTDIR"
  Goto prepare_install_dir

unowned_install_dir:
  ${IfNot} ${Silent}
    MessageBox MB_ICONSTOP|MB_OK \
      "The destination is not owned by Zvec Webview Preview. No files were removed: $INSTDIR"
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
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecWebviewPreview" "SigningStatus" "${SIGNING_STATUS}"
  File /r "${SOURCE_DIR}\*.*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "${START_MENU_SHORTCUT}" "$INSTDIR\${MAIN_EXE}"

  WriteRegStr HKCU "${PRODUCT_REG_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_REG_KEY}" "RuntimeIdentifier" "${RID}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "DisplayName" "${PRODUCT_NAME}${PRODUCT_CHANNEL_SUFFIX}"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
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
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Call un.EnsureZvecStopped
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
  Call un.EnsureZvecStopped
  Delete "${START_MENU_SHORTCUT}"
  DeleteRegKey HKCU "${PRODUCT_UNINSTALL_KEY}"
  DeleteRegKey HKCU "${PRODUCT_REG_KEY}"
  RMDir /r "$INSTDIR"
SectionEnd
