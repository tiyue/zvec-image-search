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
!ifndef SOURCE_DIR
  !error "SOURCE_DIR is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif
!ifndef APP_ICON
  !define APP_ICON "${__FILEDIR__}\..\desktop\Zvec.Desktop\Assets\Zvec.AppIcon.ico"
!endif

!define PRODUCT_NAME "Zvec Desktop Python Preview"
!define PRODUCT_PUBLISHER "Zvec"
!define PRODUCT_ID "{FAFD1275-2CED-4ED8-AD57-A935F269CC22}"
!define PRODUCT_MARKER ".zvec-python-preview-install.ini"
!define PRODUCT_REG_KEY "Software\Zvec\DesktopPythonPreview"
!define PRODUCT_UNINSTALL_KEY \
  "Software\Microsoft\Windows\CurrentVersion\Uninstall\ZvecDesktopPythonPreview"
!define INSTALL_DIR "$LOCALAPPDATA\Programs\Zvec Desktop Python Preview"
!define START_MENU_SHORTCUT "$SMPROGRAMS\Zvec Desktop Python Preview.lnk"
!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"

Name "${PRODUCT_NAME} ${VERSION} (Unsigned)"
OutFile "${OUTPUT_FILE}"
InstallDir "${INSTALL_DIR}"
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"

VIProductVersion "${FILE_VERSION}"
VIAddVersionKey /LANG=1033 "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey /LANG=1033 "CompanyName" "${PRODUCT_PUBLISHER}"
VIAddVersionKey /LANG=1033 "FileDescription" "${PRODUCT_NAME} Installer"
VIAddVersionKey /LANG=1033 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Copyright 2026 Zvec"

!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\Zvec.Desktop.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Run Zvec Desktop Python Preview"

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
    SetErrorLevel 34
    Quit
  ${EndIf}
  ${StrStr} $2 $1 "${IMAGE_NAME}"
  ${If} $2 != ""
    MessageBox MB_ICONSTOP|MB_OK \
      "${DISPLAY_NAME} is running. Finish or stop its work before installing."
    SetErrorLevel 32
    Quit
  ${EndIf}
!macroend

!macro un.RequireProcessStopped IMAGE_NAME DISPLAY_NAME
  nsExec::ExecToStack '"$SYSDIR\tasklist.exe" /NH /FI "IMAGENAME eq ${IMAGE_NAME}"'
  Pop $0
  Pop $1
  ${If} $0 != "0"
    SetErrorLevel 34
    Quit
  ${EndIf}
  ${UnStrStr} $2 $1 "${IMAGE_NAME}"
  ${If} $2 != ""
    MessageBox MB_ICONSTOP|MB_OK \
      "${DISPLAY_NAME} is running. Finish or stop its work before uninstalling."
    SetErrorLevel 32
    Quit
  ${EndIf}
!macroend

Function EnsurePreviewStopped
  !insertmacro RequireProcessStopped "Zvec.Desktop.exe" "Zvec Desktop Python Preview"
  !insertmacro RequireProcessStopped "zvec-backend.exe" "The Zvec background service"
  !insertmacro RequireProcessStopped "zvec.exe" "The Zvec command-line tool"
FunctionEnd

Function un.EnsurePreviewStopped
  !insertmacro un.RequireProcessStopped "Zvec.Desktop.exe" "Zvec Desktop Python Preview"
  !insertmacro un.RequireProcessStopped \
    "zvec-backend.exe" "The Zvec background service"
  !insertmacro un.RequireProcessStopped "zvec.exe" "The Zvec command-line tool"
FunctionEnd

Function .onInit
  SetShellVarContext current
  StrCpy $INSTDIR "${INSTALL_DIR}"
FunctionEnd

Function un.onInit
  SetShellVarContext current
  StrCpy $INSTDIR "${INSTALL_DIR}"
FunctionEnd

Section "Zvec Desktop Python Preview" SEC_APP
  SetShellVarContext current
  Call EnsurePreviewStopped

  IfFileExists "$INSTDIR\${PRODUCT_MARKER}" marker_present check_unowned_dir

marker_present:
  ReadINIStr $0 "$INSTDIR\${PRODUCT_MARKER}" "ZvecPythonPreview" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" owned_install_dir unowned_install_dir

check_unowned_dir:
  IfFileExists "$INSTDIR\*" unowned_install_dir prepare_install_dir

owned_install_dir:
  Call EnsurePreviewStopped
  RMDir /r "$INSTDIR"
  Goto prepare_install_dir

unowned_install_dir:
  MessageBox MB_ICONSTOP|MB_OK \
    "The Python Preview directory is not owned by this installer. No files were removed: $INSTDIR"
  SetErrorLevel 33
  Quit

prepare_install_dir:
  SetOutPath "$INSTDIR"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecPythonPreview" "ProductId" "${PRODUCT_ID}"
  WriteINIStr "$INSTDIR\${PRODUCT_MARKER}" \
    "ZvecPythonPreview" "Version" "${VERSION}"
  File /r "${SOURCE_DIR}\*.*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateShortcut "${START_MENU_SHORTCUT}" "$INSTDIR\Zvec.Desktop.exe"

  WriteRegStr HKCU "${PRODUCT_REG_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_UNINSTALL_KEY}" \
    "DisplayName" "${PRODUCT_NAME} (Unsigned)"
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
  Call un.EnsurePreviewStopped
  ReadINIStr $0 "$INSTDIR\${PRODUCT_MARKER}" "ZvecPythonPreview" "ProductId"
  StrCmp $0 "${PRODUCT_ID}" owned_uninstall_dir unowned_uninstall_dir

unowned_uninstall_dir:
  MessageBox MB_ICONSTOP|MB_OK \
    "The Python Preview ownership marker is invalid. No files were removed: $INSTDIR"
  SetErrorLevel 33
  Quit

owned_uninstall_dir:
  Call un.EnsurePreviewStopped
  Delete "${START_MENU_SHORTCUT}"
  DeleteRegKey HKCU "${PRODUCT_UNINSTALL_KEY}"
  DeleteRegKey HKCU "${PRODUCT_REG_KEY}"
  RMDir /r "$INSTDIR"
SectionEnd
