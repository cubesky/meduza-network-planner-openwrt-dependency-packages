# Meduza NP OpenWrt dependency packages

tinc 和 FRR 分开编译，为 [meduza-network-planner-v2](https://github.com/cubesky/meduza-network-planner-v2) 提供可安装的原生 OpenWrt 包。

## 与主项目同步的构建方式

同步基准为主项目提交 `7b3a4a9ecc551e537c4325ed08a4f00409bc5a4a` 的 `tools/release/src/openwrt.rs`、`tools/openwrt-apk.sh` 及 provider 构建流程：

- 使用同一个固定 digest 的 Alpine 3.24 镜像，通过 Podman / QEMU 在目标平台编译 musl 二进制；允许使用 OpenWrt 自带的 musl 加载器。
- 按同一个 musl 目标到 OpenWrt 架构的映射，一次编译生成多个原生包。完整构建共 6 个任务：2 个软件 × 3 个平台。
- ipk 使用带 `./` 路径的 gzip/tar 外层与 `debian-binary`、`data.tar.gz`、`control.tar.gz`。
- apk 使用同一个固定镜像的 `apk mkpkg` 生成 APK v3 / ADB 格式；打包容器禁用网络。
- 两种格式均命名为 `<package>_<version>_<arch>.ipk|apk`，每个文件附带主项目同结构的 `.json` 产物描述，包括 `openwrt/<arch>`、大小和 SHA256。

构建不下载 OpenWrt SDK。第三方库优先嵌入程序，以避免引入 Alpine 专用共享库；使用系统加载器或 `libc.so` 的包会声明 OpenWrt `libc` 依赖。验收以 OpenWrt 上的实际运行结果为准，不要求程序必须无解释器。源码与校验值记录在 `ci/sources.lock.json`，每次下载均校验；损坏或截断的响应会自动重试，最多 4 次。

当前 FRR 配方使用非 PIE 的静态可执行文件，避免 Alpine 默认 PIE 与 Libtool `-all-static`、`-export-dynamic` 组合导致启动和 TLS 异常；同时移除 `libfrr` 为可执行共享库版本查询嵌入的 `.interp` 段，避免该段通过静态归档进入守护进程、错误启动 musl 动态加载器。编译守护进程前会使用相同链接参数检查主线程和新线程的 TLS，安装后逐个运行二进制；失败日志会标明程序名、退出码及 ELF 信息。

| musl 编译目标 | 构建平台 / 别名 | 输出 OpenWrt 架构 |
| --- | --- | --- |
| x86_64-unknown-linux-musl | linux/amd64、linux-amd64、x64 | x86_64 |
| aarch64-unknown-linux-musl | linux/arm64、arm64 | aarch64_generic、aarch64_cortex-a53、aarch64_cortex-a72、aarch64_cortex-a76 |
| armv7-unknown-linux-musleabihf | linux/arm/v7、arm-v7 | arm_cortex-a7_neon-vfpv4、arm_cortex-a9_vfpv3-d16、arm_cortex-a15_neon-vfpv4 |

平台别名会选择该平台的全部 OpenWrt 架构；输入单个实际架构则只打包该架构。ARMv7 统一采用 VFPv3-D16 hard-float，适用于表中三种架构。与主项目一致，仅提供以上 8 种架构，未提供 32 位 x86 / MIPS / 无 FPU ARM。

## 触发和下载

推送任意 TAG 构建全部目标，所有任务及验收通过后，原生 `.ipk`、`.apk`、对应 `.json` 和 `SHA256SUMS` 直接发布到该 TAG 的 Release。TAG 表示本仓库发布版本；软件版本由源码锁文件决定。

手动运行 **Actions → Build tinc and FRR packages → Run workflow**，可选择 `package`、`format`、`architectures`，例如 `aarch64_generic` 或 `x64,arm64,arm-v7`。手动运行从 Artifacts 下载，不修改 Release。手动入口需要工作流已进入默认分支。

`build_mode` 控制是否重新编译：

- `auto`（默认，也用于 TAG）：编译指纹一致时复用已通过 OpenWrt 验收的二进制，只重新生成原生包并验收；找不到匹配产物时才编译。
- `repackage`：严格只重新打包。匹配产物缺失或校验失败时退出，绝不退回源码编译。
- `rebuild`：忽略已有产物，强制重新编译。

指纹按软件和 musl 平台分别计算，包含所用源码及校验值、构建镜像、编译脚本、Containerfile、ELF 检查规则和该软件补丁。修改 `packaging/`、`ci/openwrt.py`、原生包修订号（例如 FRR `r1` → `r2`）、输出架构标签或文档不会触发重新编译。打包仍使用当前代码、安装脚本和版本，并记录二进制原始构建提交及本次打包提交。

二进制以 `compiled-*` Actions Artifacts 保存 90 天，SHA256 清单与编译指纹会在复用时校验；仅在 OpenWrt 安装、运行和卸载验收通过后保存。它们通过仓库 Artifacts API 跨运行读取，支持后续 TAG 复用；普通依赖缓存受分支/TAG 范围限制，参见 [GitHub 缓存访问规则](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache)。首次采用该流程、产物过期/被删除或编译输入发生变化时，需要运行 `auto` 或 `rebuild` 建立匹配产物。工作流摘要会显示每个平台实际执行了复用还是编译。

## tinc / FRR 分包和验证

tinc 包提供 `/usr/sbin/tinc`、`/usr/sbin/tincd`。FRR 分为主包 `frr` 与 22 个二进制子包，包括 `frr-zebra`、`frr-mgmtd`、`frr-vtysh`、`frr-bgpd`、`frr-bfdd`、`frr-ospfd` 等；子包依赖相同版本的主包。所有进程由 Meduza NP 管理，附带的独立 procd 服务默认不启用。

每个平台检查 ELF 架构、对应的 OpenWrt musl 加载器和共享库依赖，拒绝未打包的第三方共享库。`build-info.json` 如实记录每个程序的解释器、动态依赖及是否完全静态。

三个平台均在真实 OpenWrt userspace 中执行原生包安装、全部二进制运行和卸载：ipk 使用 24.10.8，apk 使用 25.12.6。验收 rootfs 来自 OpenWrt 官方下载站，SHA256 固定在 `ci/targets.json`，通过 Podman 导入对应平台，ARM 经 QEMU 执行。tinc 额外测试 RSA / Ed25519 密钥生成，FRR 额外检查 `vtysh` 配置解析。手动选择某个架构时，发布产物仍按指定架构生成；必要的原生验收包保存在 `out/`，仅供 CI 使用。安装说明见 [INSTALL.md](INSTALL.md)。这些运行检查由 GitHub CI 执行；辅助脚本测试可在 Windows 本地执行。

## 维护

```sh
python -m unittest discover -s tests -v
python ci/sync_upstream.py
# 显式同步主项目最新架构和打包镜像，并检查差异后提交：
python ci/sync_upstream.py --ref master --write
```

CI 会验证固定提交中的架构表、格式与 APK 构建镜像。`--write` 更新固定提交、源码摘要和架构映射，构建配方读取同一镜像值；新增 musl 编译目标会要求补齐对应编译和验收配置。

工作流使用 GitHub 自带的 Token，只有发布任务拥有 `contents: write`。成功 Artifacts 保留 30 天，失败日志保留 7 天。构建需要 Linux x86_64 runner、Podman、QEMU 与 binutils。
