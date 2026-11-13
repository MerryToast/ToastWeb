# 验证记录

## 0.2：docs / status / api 三套模板

- 本地 Ruff、Bash 与 Node 开发工具语法检查通过。
- 本地 Windows 当前 48 项中 35 项通过，13 项 Linux 专用测试跳过。
- 使用全新 Edge 无头配置实际渲染三套模板，并检查 1440 × 1100、390 × 844 两种精确视口；六次渲染的文档宽度均未超过视口宽度。
- 使用 CDP 视口模拟，避免 Windows 无头浏览器最小窗口宽度造成错误的窄屏截图。
- 桌面/窄屏截图保存在 `.github/previews/`，仅使用示例域名，不含生产状态。
- 新增前缀匹配、手动覆盖、旧 notes 迁移、主题切换/回滚/失败恢复、自定义内容保护、本地资源/锚点检查和 API 元数据检查。
- Linux 集成测试新增三套实际站点的 HTML/CSS/SVG 与 `/meta.json` GET/HEAD 验证；结果以[当前提交的 Actions](https://github.com/MerryToast/ToastWeb/actions)为准。

## 0.1：初版历史记录

日期：2026-11-12。

### 已完成

#### 本地 Windows

- Ruff 0.14.0：通过。
- `bash -n install.sh`：通过。
- Python unittest：当前 32 项中 23 项通过，9 项 Linux 专用测试按设计跳过。
- `plan`：输出无变更计划，不调用部署命令。

#### GitHub Actions / Ubuntu 24.04

初次完整验证对应提交 `9397252`：

[CI 运行记录](https://github.com/MerryToast/ToastWeb/actions/runs/43179614677)

- Ruff、Bash 语法、ShellCheck：通过。
- **31 项测试全部通过，无跳过。**
- 测试中实际启动临时 Nginx 进程，验证 HTTP-01 文件服务、TLS 1.3、h2、静态页面、404、POST 拒绝、未知 HTTP Host 和未知 SNI 拒绝。
- 安装/内容更新/回滚测试使用临时根目录、模拟 systemd/Certbot，验证幂等重装及失败后的项目文件和内容指针恢复。
- 域名/输入校验、端口冲突、管理员策略保留、旧 ACME 检查、HTML 转义、隐藏文件/符号链接拒绝等测试通过。

后续补充了严格 `umask 077` 下公开静态目录权限的回归测试，当前共 32 项。后续提交以仓库 Actions 的对应运行结果为准。

## 尚未完成，不能由 CI 结果替代

- Debian 13 上的真实 APT 安装及 systemd 服务全流程。
- 真实域名的 Let's Encrypt 正式证书签发。
- CA 续期 dry-run、真实到期续签和部署 hook 的端到端验证。
- 外部网络对公网 TCP 80 的可达性。
- 与现有 x-ui/Xray 的正式 Target/SNI 切换及客户端验收。
- Nginx/OpenSSL 在目标 VPS 上的 MLKEM 实际协商。
- 人为断电/SIGKILL、APT 中断后的人工恢复演练。

**未为测试修改 ProWee 或其他生产 VPS，没有修改 Cloudflare DNS、生产订阅或防火墙。**

首次 VPS 试点前，应保留原代理配置和独立管理通道，检查端口及旧 ACME 任务，并按 README 分阶段验收。
