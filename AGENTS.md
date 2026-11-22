# PROTO 项目协作规范

## 本地提交

- 任务修改完成且相关验证通过后，核对设计文档、进展和 README 与实现一致，再用 `git commit` 本地提交本次任务的改动，不纳入无关改动。
- 不自动推送或发布；用户本轮明确指定其他交付方式时，以本轮要求为准。

## 控制台同步维护

- Web 和 Electron 共用 `proto/web/index.html` 及 Python API；增加或修改控制台功能在共享实现中完成，不在 `desktop/` 复制 UI 或业务逻辑。
- 控制台交付检查 Web 回归和 `npm --prefix desktop test`；改动启动、窗口权限或主交互时，用 `npm --prefix desktop run smoke` 在临时舰队验收，不动真实任务。
