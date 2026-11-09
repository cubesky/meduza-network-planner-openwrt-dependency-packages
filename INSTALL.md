# 安装 tinc / FRR

从 Release 下载与设备架构对应的原生包。架构查询：`. /etc/openwrt_release; echo "$DISTRIB_ARCH"`。OpenWrt 24.10 使用 ipk，25.12+ 使用 APK v3。先下载 `SHA256SUMS`，用 `sha256sum -c --ignore-missing SHA256SUMS` 校验本地包，并确认命令成功。

这些包与主项目采用相同的 musl 平台和 OpenWrt 架构映射，支持完全静态程序及使用 OpenWrt 自带 musl 加载器的程序。需要系统运行时时，原生包会声明 `libc` 依赖，由包管理器检查。文件名统一为 `<package>_<version>_<arch>.<format>`，每个文件的 `.json` 还记录了平台、大小与 SHA256。

## tinc

先从当前固件自己的源安装匹配当前内核的 TUN 模块；tinc 包自身不携带内核模块：

```sh
# OpenWrt 24.10
opkg update
opkg install kmod-tun ip-full
opkg install ./tinc_*_aarch64_generic.ipk

# OpenWrt 25.12+
apk update
apk add kmod-tun ip-full
apk add --allow-untrusted ./tinc_*_aarch64_generic.apk
```

将示例的 `aarch64_generic` 替换为设备实际架构。安装后检查 `tinc --version` 和 `tincd --version`。

## FRR

主项目的 BGP / BFD / OSPF 功能需要以下包；主包与子包使用同一次构建的相同版本：

```sh
# OpenWrt 24.10
opkg install ./frr_*_aarch64_generic.ipk ./frr-zebra_*_aarch64_generic.ipk \
  ./frr-mgmtd_*_aarch64_generic.ipk ./frr-vtysh_*_aarch64_generic.ipk \
  ./frr-bgpd_*_aarch64_generic.ipk ./frr-bfdd_*_aarch64_generic.ipk ./frr-ospfd_*_aarch64_generic.ipk

# OpenWrt 25.12+
apk add --allow-untrusted ./frr_*_aarch64_generic.apk ./frr-zebra_*_aarch64_generic.apk \
  ./frr-mgmtd_*_aarch64_generic.apk ./frr-vtysh_*_aarch64_generic.apk \
  ./frr-bgpd_*_aarch64_generic.apk ./frr-bfdd_*_aarch64_generic.apk ./frr-ospfd_*_aarch64_generic.apk
```

按需安装其他 `frr-*` 协议包。所有守护进程位于 `/usr/sbin`，`vtysh` 位于 `/usr/bin`，符合主项目的系统二进制探测和同目录守护进程查找方式。软件包中的 `build-info.json` 记录每个程序的解释器、共享库依赖、静态链接状态、源码摘要、构建镜像和同步的主项目提交。

## 由 Meduza NP 管理

安装系统二进制后运行 `meduza-np doctor` 检查，并由主项目下发配置、启动和监督进程。附带的 `/etc/init.d/tinc` 与 `/etc/init.d/frr` 默认不启用，供独立使用时手动配置；由 Meduza NP 管理的节点应保持这些独立服务停止，避免两个控制器管理同一服务。

独立使用 tinc 时设置 `/etc/config/tinc` 的 `confdir`、`enabled`，再启用 tinc 服务。独立使用 FRR 时配置 `/etc/frr/frr.conf` 和 `/etc/frr/daemons`，安装所选守护进程后再启用 frr 服务。

OpenWrt apk 使用 `--allow-untrusted` 本地安装，先验证发布校验值。这些是 OpenWrt 架构的原生包；TUN、ip-full 等系统/内核功能仍从当前固件的源提供。
