# Zvec 项目规则

## Spec 索引

- 项目架构、模块职责与发布约束：`docs/spec.md`
- Sony A7M4 ARW 选片模块需求规格：`docs/raw-image-support-requirements.md`
- 软件开发全流程、需求约束、原型、测试与发布实践：`docs/development-workflow-guide.md`

## 发布矩阵

- GitHub Release 仅允许发布 Zvec-Webview Windows 版本、Zvec Android
  版本，以及对应的校验和、验证报告和发布策略元数据。
- 禁止构建、上传或附加 Zvec-Desktop（Tkinter/pystray）安装包和便携包。
- 禁止将 Python wheel 作为 GitHub Release 附件；wheel 仅可用于 CI
  安装验证。
- `zvec.exe` 与 `zvec-backend.exe` 可以作为 Zvec-Webview 包的内部运行组件，
  不视为独立发布版本。
- 修改发布矩阵必须取得用户明确批准，并同步更新 `docs/spec.md`、
  `RELEASE.md` 和 `SUPPLY_CHAIN.md`。
