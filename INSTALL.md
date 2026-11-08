# 安装编译包

1. 解压对应归档，进入解压出的目录；查看 `build-info.json` 的 OpenWrt 版本、架构及包清单。
2. 核对固件版本与架构。ipk 使用 `ubus call system board` 和 `opkg print-architecture`；apk 使用 `ubus call system board` 和 `apk --print-arch`。
3. 执行 `sha256sum -c SHA256SUMS`，应全部通过。

只安装与固件的发行版、架构、ABI 相符的包。不要在 OpenWrt 24.10 上装 25.12 的包，或将 OpenWrt apk 用于 Alpine Linux、Android。ARMv7 各个 CPU / FPU 架构不能随意混装；`arm-v7` 在此表示 `arm_cortex-a7_neon-vfpv4`。

## tinc

tinc 需要 TUN 内核模块。先从当前固件自己的软件源安装它，保证与正在运行的内核匹配：

```sh
# ipk / OpenWrt 24.10
opkg update
opkg install kmod-tun
opkg install ./packages/tinc_*.ipk

# apk / OpenWrt 25.12
apk update
apk add kmod-tun
apk add --allow-untrusted ./packages/tinc-*.apk
```

## FRR

`packages/` 内保留独立的 `frr`、`frr-zebra`、`frr-watchfrr`、`frr-bgpd`、`frr-ospfd` 等包。主包包含共享库、vtysh、配置与服务脚本；按需选择协议包。例如安装 BGP 和 OSPFv2：

```sh
# ipk / OpenWrt 24.10
opkg update
opkg install ./packages/frr_*.ipk ./packages/frr-zebra_*.ipk \
  ./packages/frr-watchfrr_*.ipk ./packages/frr-bgpd_*.ipk ./packages/frr-ospfd_*.ipk

# apk / OpenWrt 25.12
apk update
apk add --allow-untrusted ./packages/frr-[0-9]*.apk ./packages/frr-zebra-*.apk \
  ./packages/frr-watchfrr-*.apk ./packages/frr-bgpd-*.apk ./packages/frr-ospfd-*.apk
```

请同时使用同一归档的 FRR 主包和协议包。安装后按需修改 `/etc/frr/daemons`、`/etc/frr/frr.conf`；tinc 则按官方包格式配置 `/etc/config/tinc`、`/etc/tinc/`。随后启用并启动所需服务：

```sh
/etc/init.d/tinc enable
/etc/init.d/tinc start
# 或
/etc/init.d/frr enable
/etc/init.d/frr start
```

## 依赖

包管理器会从当前固件的软件源解决常规依赖。`dependencies/` 另外提供本次构建生成的依赖包；如果源中没有所需版本，在确认与当前固件兼容后，把所需的依赖文件与应用包一起传给同一条 `opkg install` 或 `apk add --allow-untrusted` 命令。

不要批量覆盖固件中的 libc 等基础组件，也不要使用强制忽略依赖或架构的参数。内核模块始终使用当前固件自己的源。自编译 apk 未使用 OpenWrt 官方密钥签名，因此本地安装时需要 `--allow-untrusted`；先检查归档和包的 SHA256。
