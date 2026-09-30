Unicode true

####
## Please note: Template replacements don't work in this file. They are provided with default defines like
## mentioned underneath.
## If the keyword is not defined, "wails_tools.nsh" will populate them with the values from ProjectInfo.
## If they are defined here, "wails_tools.nsh" will not touch them. This allows to use this project.nsi manually
## from outside of Wails for debugging and development of the installer.
##
## For development first make a wails nsis build to populate the "wails_tools.nsh":
## > wails build --target windows/amd64 --nsis
## Then you can call makensis on this file with specifying the path to your binary:
## For a AMD64 only installer:
## > makensis -DARG_WAILS_AMD64_BINARY=..\..\bin\app.exe
## For a ARM64 only installer:
## > makensis -DARG_WAILS_ARM64_BINARY=..\..\bin\app.exe
## For a installer with both architectures:
## > makensis -DARG_WAILS_AMD64_BINARY=..\..\bin\app-amd64.exe -DARG_WAILS_ARM64_BINARY=..\..\bin\app-arm64.exe
####
## The following information is taken from the ProjectInfo file, but they can be overwritten here.
####
## !define INFO_PROJECTNAME    "MyProject" # Default "{{.Name}}"
## !define INFO_COMPANYNAME    "MyCompany" # Default "{{.Info.CompanyName}}"
## !define INFO_PRODUCTNAME    "MyProduct" # Default "{{.Info.ProductName}}"
## !define INFO_PRODUCTVERSION "1.0.0"     # Default "{{.Info.ProductVersion}}"
## !define INFO_COPYRIGHT      "Copyright" # Default "{{.Info.Copyright}}"
###
## !define PRODUCT_EXECUTABLE  "Application.exe"      # Default "${INFO_PROJECTNAME}.exe"
## !define UNINST_KEY_NAME     "UninstKeyInRegistry"  # Default "${INFO_COMPANYNAME}${INFO_PRODUCTNAME}"
####
## 0.6.1 (T3'): perMachine 安装（用户裁定）——装进 D:\Program Files\localoctop，
## 需要管理员权限，NSIS 以 RequestExecutionLevel admin 启动时自动弹 UAC。
## wails_tools.nsh 的默认即 admin，无需在此覆盖；SetShellVarContext all
## （wails.setShellContext）随之让 $SMPROGRAMS/$DESKTOP 落到全体用户。
## 卸载兼容：旧 0.6.0 currentUser 版（$LOCALAPPDATA\byron\ 下、以旧产品名命名
## 的目录）不做自动清理——用户自行卸载旧版（任务书裁定），本脚本不碰那个目录。
####
## Include the wails tools
####
!include "wails_tools.nsh"

# The version information for this two must consist of 4 parts
VIProductVersion "${INFO_PRODUCTVERSION}.0"
VIFileVersion    "${INFO_PRODUCTVERSION}.0"

VIAddVersionKey "CompanyName"     "${INFO_COMPANYNAME}"
VIAddVersionKey "FileDescription" "${INFO_PRODUCTNAME} Installer"
VIAddVersionKey "ProductVersion"  "${INFO_PRODUCTVERSION}"
VIAddVersionKey "FileVersion"     "${INFO_PRODUCTVERSION}"
VIAddVersionKey "LegalCopyright"  "${INFO_COPYRIGHT}"
VIAddVersionKey "ProductName"     "${INFO_PRODUCTNAME}"

# Enable HiDPI support. https://nsis.sourceforge.io/Reference/ManifestDPIAware
ManifestDPIAware true

!include "MUI.nsh"

!define MUI_ICON "..\icon.ico"
!define MUI_UNICON "..\icon.ico"
# !define MUI_WELCOMEFINISHPAGE_BITMAP "resources\leftimage.bmp" #Include this to add a bitmap on the left side of the Welcome Page. Must be a size of 164x314
!define MUI_FINISHPAGE_NOAUTOCLOSE # Wait on the INSTFILES page so the user can take a look into the details of the installation steps
# 0.6.0 (T2): 安装完成页「立即运行」默认勾上，装完即用（员工零操作）。
# （只赋值、不裸 define：MUI.nsh 已预定义 MUI_FINISHPAGE_RUN，重复 !define 会中止。）
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 localoctop"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${PRODUCT_EXECUTABLE}"
!define MUI_ABORTWARNING # This will warn the user if they exit from the installer.

!insertmacro MUI_PAGE_WELCOME # Welcome to the installer page.
# !insertmacro MUI_PAGE_LICENSE "resources\eula.txt" # Adds a EULA page to the installer
!insertmacro MUI_PAGE_DIRECTORY # In which folder install page.
!insertmacro MUI_PAGE_INSTFILES # Installing page.
!insertmacro MUI_PAGE_FINISH # Finished installation page.

!insertmacro MUI_UNPAGE_INSTFILES # Uinstalling page

!insertmacro MUI_LANGUAGE "English" # Set the Language of the installer

## The following two statements can be used to sign the installer and the uninstaller. The path to the binaries are provided in %1
#!uninstfinalize 'signtool --file "%1"'
#!finalize 'signtool --file "%1"'

Name "${INFO_PRODUCTNAME}"
OutFile "..\..\bin\${INFO_PROJECTNAME}-${ARCH}-installer.exe" # Name of the installer's file.
# 0.6.1 (T3'): perMachine 安装目录（用户裁定）——Program Files 下 localoctop
# 子文件夹；D 盘是员工机约定的数据盘。InstallDirRegKey 让升级安装记住上次位置。
InstallDir "D:\Program Files\localoctop"
ShowInstDetails show # This will always show the installation details.

Function .onInit
   !insertmacro wails.checkArchitecture
FunctionEnd

Section
    !insertmacro wails.setShellContext

    !insertmacro wails.webview2runtime

    SetOutPath $INSTDIR

    !insertmacro wails.files

    CreateShortcut "$SMPROGRAMS\${INFO_PRODUCTNAME}.lnk" "$INSTDIR\${PRODUCT_EXECUTABLE}"
    CreateShortCut "$DESKTOP\${INFO_PRODUCTNAME}.lnk" "$INSTDIR\${PRODUCT_EXECUTABLE}"

    !insertmacro wails.associateFiles
    !insertmacro wails.associateCustomProtocols

    !insertmacro wails.writeUninstaller

    # 0.6.0 (T2): 注册 HKCU Run 开机自启（与桌面端 autostart_windows.go 同一键）。
    # 初次装机默认开——员工零操作；之后设置页勾选框可关（两端删同一值）。
    # 值带 --minimized：开机直接进托盘不弹窗。（0.6.1 perMachine 下保持 HKCU：
    # 自启是当前用户的登录动作，per-user 语义正确，且升级/重装不互踩。）
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "localoctop-desktop" '"$INSTDIR\${PRODUCT_EXECUTABLE}" --minimized'
SectionEnd

Section "uninstall"
    !insertmacro wails.setShellContext

    RMDir /r "$AppData\${PRODUCT_EXECUTABLE}" # Remove the WebView2 DataPath

    RMDir /r $INSTDIR

    Delete "$SMPROGRAMS\${INFO_PRODUCTNAME}.lnk"
    Delete "$DESKTOP\${INFO_PRODUCTNAME}.lnk"

    !insertmacro wails.unassociateFiles
    !insertmacro wails.unassociateCustomProtocols

    !insertmacro wails.deleteUninstaller

    # 0.6.0 (T2): 清开机自启（HKCU Run，安装与卸载两端删同一值）。
    # 卸载注册键由 wails.deleteUninstaller 清（perMachine admin 下在 HKLM，
    # SetRegView 64 已由该宏设置）。
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "localoctop-desktop"

    # 0.6.0 (T2): 用户数据不删 —— $APPDATA\localoctop（config.json 含令牌与
    # 白名单、audit.jsonl 审计记录）整体保留，重装即恢复原配置。
    # （此注释即「保留」的实现：明确不写任何 RMDir/Delete 指向它。）

    # 0.6.1 (T3'): 旧 0.6.0 currentUser 版装在 $LOCALAPPDATA\byron\ 下（旧产品名
    # 目录），不做自动清理——用户自行卸载旧版（任务书裁定）；本脚本不写任何指令指向它。
SectionEnd
