# 验证记录

## 0.3.2：第二批 W2 / W4 本地改进（未发布、未部署）

- 保留第一批全部本地修改；继续使用同一未发布的 0.3.2，不生成提交或 release。
- W2：`doctor` 文本/JSON 报告；必要检查错误退出 1、未确认退出 2、正常/警告退出 0。覆盖有效 unit 属性、FD 预算、续期 timer、按 MainPID 归属的实际回环监听、磁盘/在服证书临期及叶指纹一致性、TLS 1.3/h2、独立 HTTP/1.1 HEAD 200、无原始行的日志来源摘要。
- CAA/DNSSEC 查询默认不执行；可选调用固定 HTTPS dns.google，限制响应大小与超时、拒绝重定向，CAA 支持父域继承，DNSSEC 只使用验证解析器 AD 证据。缺少策略/查询未知不阻止启动，报告明确各项状态；不改 DNS。
- W4：默认关闭的独立 service/timer；配置与旧 state 兼容，连续失败阈值、证书临期告警、同问题去重、恢复事件及通知失败 300 秒重试。运行状态私有保存，不参与内容回滚。探针不修复/重启网站，配置失败也不 reload Nginx。
- 默认 journal；仅显式选择的 root 私有通知程序能对外发送，使用单独 argv、JSON stdin、10 秒超时并丢弃 stdout/stderr，检查文件及父目录所有权/写权限/符号链接。不创建真实通知渠道或保存凭据。
- 两个 DeepSeek worker 分别实现独立诊断模块和纯监控状态机，主代理完成 CLI、事务/打包集成、隐私与接口复核。补齐了 ISO/epoch 时间互操作、在服证书告警、HTTP/ACME 日志中性提示和固定 DNS 传输回归；诊断检查拆为独立 helper 而非单一巨型函数。
- 本地 Windows 最终 **215 项：178 通过、37 跳过**；比第一批 126 项增加 89 项。新模块分别为 diagnostics 27、monitor 38、health integration 22（其中 1 项 Linux unit 语法跳过），另增加继承执行的两个真实 Nginx 探针用例（Windows 跳过）。37 项跳过=既有 Linux 30+第一批 APT 解析 1+systemd 3+本批真实 Nginx 2+unit 语法 1，均不计通过。
- Ruff lint/format、Python 编译、Bash/Node 语法、入口 ShellCheck、CI YAML 解析、非变更 plan、`git diff --check` 通过。LSP 覆盖仍不完整（超时/静默服务器），不能宣称全项目类型检查通过。局部 ast-grep 将 `is-active` 字符串误报成 `is` 比较，已核对实际 `returncode == 0` 并标记误报，未修改逻辑绕过检查。
- CI 增加新模块编译和复用隔离 Nginx fixture 的真实 TLS/HTTP/证书指纹探针；另提供仅解析、不激活服务的 `systemd-analyze verify` 用例。**本轮未运行 GitHub CI/真实 Linux，也未执行真实 DNS/通知发送或 CA 续期，不能据 mock 或历史部署声称这些已经通过。**
- 未 SSH/部署 Moecloud，未改生产代理、DNS、订阅、客户端、凭据、防火墙、全局自动更新或 Pi 配置。整机故障、公网 80/443、MLKEM、REALITY 认证及外部渠道真实送达，仍需独立验收。

## 0.3.2：第一批本地改进（未发布、未部署）

- 范围仅 W1（有界自动重启）、W8（可选的软件包运行时刷新）和 W7（REALITY 耦合与维护文档）；不实现第二批 doctor/持续探针/外部告警或 PROXY protocol。
- 新增回归覆盖 unit 节位置、默认关闭与旧 state、显式开启/关闭及重装保留、只读 plan、所有权拒绝、hook/unit 事务恢复、调度失败不回滚已成功安装的网站。
- 运行时用例全部使用模拟服务/包管理器/网络，覆盖 list-only 只筛选本项目、未运行/被 masked 的服务、policy-rc.d 拒绝、无关更新、配置失败、并发停用/PID 变化、APT/dpkg 四个记录锁、等待时不持本站锁、取得本站锁后再次检查并释放、超时、真实 HTTPS 检查路径、探针重试和仍待刷新时不得报成功。
- 本地 Windows：共 **126 项，92 项通过、34 项按平台条件跳过**；相较基线 87 项新增 39 项。跳过包括旧版 30 项 Linux 专用用例、1 项真实 APT 配置解析和 3 项新 systemd 用例，不计作通过。
- Ruff 检查与格式检查、Python 编译、Bash/Node 语法、入口和生成 APT 调度脚本的 ShellCheck、CI YAML 解析、`git diff --check` 均通过。
- LSP 覆盖不完整，部分文件无主语言服务器答复或超时，不据此宣称类型检查全通过；处理了 Windows 对 Linux 专用 SIGKILL 常量的静态平台差异。
- 新增 `TOASTWEB_LIVE_SYSTEMD=1` 的 root 隔离测试：随机名 `toastweb-ci-*.service`，`/run` 临时文件与回环高端口；实际验证 master SIGKILL 后恢复、反复启动失败的限流、原子替换临时 Nginx 二进制后 reload 仍旧而受控重启更新，并执行可信测试证书的 HTTPS HEAD。测试不运行 APT，不修改正式网站/证书或生产 unit。
- CI 已配置在可丢弃的 Ubuntu root runner 执行这些用例；非 root 阶段不会操作 systemd。**本轮未提交或触发 GitHub CI，真实 Linux/systemd/APT 解析及该 needrestart 组合的执行仍待验证。**
- 既有 Debian 13 的 0.3.1 部署记录仅是历史基线，不能证明新代码通过生产验收。未 SSH、未修改 Moecloud，亦未改 DNS、订阅、客户端、凭据、防火墙或共享自动更新策略。

## 0.3.1：首次安装运行目录

- 真机首次启动暴露检查顺序问题：独立的 `nginx -t` 早于 systemd 创建 `/run/toastweb-nginx`，无法打开 PID 路径。
- 由服务 `ExecStartPre` 在 RuntimeDirectory 创建后执行首次配置校验，启动后、reload 前再执行手动校验；不移除启动前检查，也不放宽私钥权限。
- 安装事务测试模拟“服务尚未启动时 PID 目录不存在”，并断言首次手动校验在 systemd 启动之后，防止用已有目录掩盖问题。现共 87 项测试；[修复 CI（7ff6878）](https://github.com/MerryToast/ToastWeb/actions/runs/43422617901) 的 Ubuntu 24.04 root/非 root 各 87 项全部通过，无跳过。
- 受授权的独立 Debian 13 试点已完成真实 APT/systemd、正式 CA 签发、带 deploy hook 的 staging 续期演练及普通续期 service 执行。生产证书未被 dry-run 替换；公网 HTTP-01 文件与通过临时 SSH 隧道的可信 HTTPS 页面均通过外部验证。
- 该试点 Nginx 1.26.3/OpenSSL 3.5.7 协商 TLS 1.3/h2 和 X25519MLKEM768；错误 HTTP/1.1 Host 被断开，错误 HTTP/2 authority 返回标准 421。集成测试接受明确的 421 或无响应重置，仍拒绝成功响应和默认欢迎页。没有据此宣称 REALITY/客户端已经切换。

## 0.3：审查修复与测试边界

- 回归覆盖相对目录跳转、正确 SNI 配错误 HTTP/1.1 Host / HTTP/2 authority、未知 SNI 拒绝。
- 同一真实 Nginx 测试分别覆盖默认 HTTP 404 和主动启用的 HTTPS 跳转，ACME 路径始终直接服务。
- CI 分别以非 root、root 运行完整测试；root 场景检查 worker 实际为 www-data。仅放开临时公开父目录的遍历权限，证书目录 0700、私钥 0600 不放宽。
- 单元测试覆盖 NAT 连接失败例外的严格边界、token/HTTP 错误、跳转确认和失败恢复、所有权及保留备份引用、清理预览/异常/符号链接保护、FD 预算和非变更式 plan。
- HTTP/2 的 h2 包仅为开发测试依赖，不引入 VPS Python 第三方依赖。
- 连接参数的渲染与 FD 预算检查不是并发/性能压力测试。CI 不申请真实证书、不操作生产 systemd、DNS 或 REALITY。
- 本地 Windows：86 项中 57 项通过、29 项 Linux 专用测试按设计跳过；Ruff、Python/Bash/Node 语法检查通过。Edge 重新渲染 6 张桌面/窄屏截图，1440/390 视口无横向溢出。
- [首轮修复 CI（5343abd）](https://github.com/MerryToast/ToastWeb/actions/runs/43395232837)：Ubuntu 24.04 非 root 与 root **各 86 项全部通过，无跳过**；Ruff 0.14.0、ShellCheck 及语法检查通过。后续提交以 [Actions](https://github.com/MerryToast/ToastWeb/actions) 为准；本地跳过 Linux 测试不代表通过。

## 0.2：docs / status / api 三套模板

- 本地 Ruff、Bash 与 Node 开发工具语法检查通过。
- 本地 Windows 当前 48 项中 35 项通过，13 项 Linux 专用测试跳过。
- 使用全新 Edge 无头配置实际渲染三套模板，并检查 1440 × 1100、390 × 844 两种精确视口；六次渲染的文档宽度均未超过视口宽度。
- 使用 CDP 视口模拟，避免 Windows 无头浏览器最小窗口宽度造成错误的窄屏截图。
- 桌面/窄屏截图保存在 `.github/previews/`，仅使用示例域名，不含生产状态。
- 新增前缀匹配、手动覆盖、旧 notes 迁移、主题切换/回滚/失败恢复、自定义内容保护、本地资源/锚点检查和 API 元数据检查。
- Linux 集成测试新增三套实际站点的 HTML/CSS/SVG 与 `/meta.json` GET/HEAD 验证。
- [三模板首轮 CI](https://github.com/MerryToast/ToastWeb/actions/runs/43356164343) 的 48 项测试全部通过；包含旧版升级、主题切换/回滚和模拟失败恢复。后续变更以[当前提交的 Actions](https://github.com/MerryToast/ToastWeb/actions)为准。

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

## 不能由 CI 结果替代的真机验收

已完成项仅代表上述受控 Debian 13 独立网站试点，不推广为所有主机均已通过：

- [x] Debian 13 上的真实 APT 安装及 systemd 独立网站服务流程。
- [x] 真实域名的 Let's Encrypt 正式证书签发。
- [x] CA 续期 dry-run 与部署 hook 的端到端验证，未替换正式证书。
- [x] 外部网络对公网 TCP 80 的可达性。
- [x] 该后端 Nginx/OpenSSL 的 X25519MLKEM768 实际协商。
- [ ] 真实到期后的自动续签与长期运行观察。
- [ ] 现有 x-ui/Xray 的正式 Target/SNI 切换及客户端验收。
- [ ] 重启持久化、人为断电/SIGKILL、APT 中断后的恢复演练。

**CI 始终使用隔离环境。试点未修改 ProWee、Cloudflare DNS 或生产代理/订阅；仅在独立授权后为试点主机放行 IPv4 TCP 80，防火墙变更不是安装器的自动功能。**

首次 VPS 试点前，应保留原代理配置和独立管理通道，检查端口及旧 ACME 任务，并按 README 分阶段验收。
