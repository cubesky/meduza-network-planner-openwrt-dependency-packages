# OpenWrt tinc / FRR 自动编译

GitHub Actions 使用官方发行版 SDK 从源码编译 tinc 和 FRR。两者在独立任务中构建，按软件、架构和格式分别输出；FRR 保留官方独立子包，包括 BGP、OSPF、Zebra、watchfrr 等。

## 触发与下载

- 推送任意 TAG：完整构建全部 36 个组合，全部成功后自动将 36 份归档与 `SHA256SUMS` 上传到该 TAG 的 GitHub Release。
- 手动触发：在 **Actions → Build tinc and FRR packages → Run workflow** 中选择软件、格式和架构。在运行页面的 **Artifacts** 下载对应归档；手动运行不会修改 Release。
- 手动入口要求本工作流已提交到仓库的默认分支。TAG 需要指向包含本工作流的提交。

```sh
git tag v1.0.0
git push origin v1.0.0
```

TAG 是本仓库的发布标识；tinc / FRR 的实际版本由对应发行版的官方 packages feed 决定，不会把 TAG 强行写入软件版本。packages feed 固定到官方 `feeds.buildinfo` 中的 commit；基础依赖的 base feed 固定到 SDK 自带的 commit。两者都记录在每份归档的 `build-info.json`。

## 格式和架构

| 格式 | 固定 SDK 发行版 |
| --- | --- |
| ipk | OpenWrt 24.10.8 |
| apk | OpenWrt 25.12.6 |

| 名称 / 可输入别名 | 实际包架构 | SDK target/subtarget |
| --- | --- | --- |
| x86、i386 | i386_pentium4 | x86/generic |
| x64、amd64、linux-amd64 | x86_64 | x86/64 |
| aarch64_cortex-a53 | aarch64_cortex-a53 | bcm27xx/bcm2710 |
| aarch64_cortex-a72 | aarch64_cortex-a72 | bcm27xx/bcm2711 |
| aarch64_cortex-a76 | aarch64_cortex-a76 | bcm27xx/bcm2712 |
| arm64、aarch64、aarch64_generic | aarch64_generic | armsr/armv8 |
| arm-v7、armv7、arm_cortex-a7 | arm_cortex-a7_neon-vfpv4 | sunxi/cortexa7 |
| arm_cortex-a15 | arm_cortex-a15_neon-vfpv4 | ipq806x/generic |
| arm_cortex-a9_vfpv3 | arm_cortex-a9_vfpv3-d16 | mvebu/cortexa9 |

`architectures` 输入 `all` 构建全部架构，也可输入逗号分隔的实际架构或别名，例如 `x64,arm64,arm-v7`。重复的别名会自动去重，未知架构会报错。

`linux-amd64`、`arm-v7`、`arm64` 在这里作为 OpenWrt 架构别名处理。产物是基于 musl 的 OpenWrt 安装包；不能作为通用 Linux 静态二进制使用。特别是 ARMv7 必须匹配设备的 CPU / FPU 架构。x86 选择的是官方 `i386_pentium4`，不是所有旧式 32 位 CPU。

构建所用的 target/subtarget 提供相应用户态架构的工具链，不表示包仅能装到该 target 的设备。实际安装还需匹配固件的发行版、架构与 ABI；TUN 等内核模块从设备自己的软件源安装。

## 归档内容与安装

归档名示例：`frr-openwrt-25.12.6-aarch64_cortex-a53-apk.tar.gz`。

```text
packages/          tinc 或 FRR 的原生 .ipk / .apk 文件
dependencies/      本次编译生成的非内核依赖包
build-info.json    发行版、架构、SDK 校验值、feed commit、文件校验值
sdk.config         实际 SDK 构建配置
package.Makefile   实际编译使用的包配方
SHA256SUMS         归档内文件的 SHA256 校验值
INSTALL.md         安装与依赖说明
```

安装步骤见 [INSTALL.md](INSTALL.md)。FRR 子包可按需安装，tinc 与 FRR 不会放在同一份归档中。应用与依赖均保留原生文件名。

## 维护与验证

- 在 `ci/targets.json` 中更新 SDK 发行版或增加架构；格式必须使用支持该格式的 SDK。
- 准备阶段先运行本地测试，并核对选中组合的官方 SDK 清单、文件校验值与 `profiles.json` 中的实际架构，再启动编译。
- SDK、架构信息和 feed 信息均通过官方 HTTPS 下载；SDK 和元数据按官方 `sha256sums` 验证。
- 使用 GitHub 自带的 `GITHUB_TOKEN`；只有 Release 任务有 `contents: write` 权限，无需额外 Token。仓库或组织策略需允许工作流创建 Release。
- 构建并发上限为 6，每个任务最多使用 2 个编译进程，编译失败会自动以单进程重试。失败日志保留 7 天，成功 Artifacts 保留 30 天，Release 资产长期保留。

本地运行不依赖第三方 Python 模块的测试：

```sh
python -m unittest discover -s tests -v
```

联网检查全部 SDK 的可用性（不下载大型 SDK 归档）：

```sh
python ci/check_sdks.py
```

构建本身需要 Linux x86_64 环境及工作流中列出的依赖。Windows 可运行辅助脚本测试，交叉编译在 GitHub 的 Ubuntu runner 中执行。

官方参考：[OpenWrt 发行版](https://downloads.openwrt.org/releases/)、[tinc 包配方](https://github.com/openwrt/packages/tree/openwrt-25.12/net/tinc)、[FRR 包配方](https://github.com/openwrt/packages/tree/openwrt-25.12/net/frr)。
