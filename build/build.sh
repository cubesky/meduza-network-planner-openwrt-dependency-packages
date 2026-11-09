#!/bin/sh
# Native musl compilation under the same pinned Alpine image/QEMU method as the main project's providers.
set -eu
lock=$1
out=$2
jobs=${BUILD_JOBS:-2}
deps=/opt/deps
mkdir -p /build "$out/bin" "$out/licenses" "$out/share"
export CFLAGS="-Os -fPIC -std=gnu11"
case "$TARGETPLATFORM" in
  linux/arm/v7) CFLAGS="$CFLAGS -march=armv7-a -mfpu=vfpv3-d16 -mfloat-abi=hard" ;;
  linux/arm64) CFLAGS="$CFLAGS -march=armv8-a" ;;
  linux/amd64) CFLAGS="$CFLAGS -march=x86-64 -mtune=generic" ;;
  *) echo "Unsupported platform: $TARGETPLATFORM" >&2; exit 1 ;;
esac
export CFLAGS
export CXXFLAGS="$(printf '%s' "$CFLAGS" | sed 's/ -std=gnu11//')"
export CPPFLAGS="-I$deps/include"
export LDFLAGS="-L$deps/lib"
export PKG_CONFIG_PATH="$deps/lib/pkgconfig"
export PKG_CONFIG="pkg-config --static"

for name in $(jq -r --arg p "$PACKAGE" '.packages[$p].sources[]' "$lock"); do
  sum=$(jq -r --arg n "$name" '.sources[] | select(.name==$n) | .sha256' "$lock")
  archive="/archives/$name.tar.gz"
  echo "$sum  $archive" | sha256sum -c -
  mkdir -p "/build/$name"
  tar -xzf "$archive" -C "/build/$name" --strip-components=1
  license=$(jq -r --arg n "$name" '.sources[] | select(.name==$n) | .license_file' "$lock")
  install -D -m 0644 "/build/$name/$license" "$out/licenses/$name/$(basename "$license")"
done

if [ "$PACKAGE" = tinc ]; then
  cd /build/lzo
  ./configure --prefix="$deps" --libdir="$deps/lib" --disable-shared --enable-static
  make -j"$jobs"
  make install
  cd /build/tinc
  ./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
    --disable-shared --enable-static --disable-curses --disable-readline \
    --with-lzo-include="$deps/include/lzo" --with-lzo-lib="$deps/lib" \
    LDFLAGS="-L$deps/lib -static" LIBS="-llzo2 -lz -lcrypto -pthread"
  make -j"$jobs"
  install -m 0755 src/tinc src/tincd "$out/bin/"
  # Exercise RSA/Ed25519 generation too; static OpenSSL must work beyond printing a version.
  timeout 120 "$out/bin/tinc" -b -c /tmp/tinc-key-smoke init smoke
  test -s /tmp/tinc-key-smoke/rsa_key.priv
  test -s /tmp/tinc-key-smoke/ed25519_key.priv
else
  # json-c 0.18's apps subdirectory still requests CMake 2.8 policies.
  # CMake 4 removed that compatibility; set the supported policy floor externally.
  cmake -S /build/json-c -B /build/json-c-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    -DCMAKE_INSTALL_PREFIX="$deps" -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DBUILD_SHARED_LIBS=OFF -DBUILD_STATIC_LIBS=ON -DBUILD_TESTING=OFF -DDISABLE_WERROR=ON
  cmake --build /build/json-c-build -j "$jobs"
  cmake --install /build/json-c-build
  cmake -S /build/c-ares -B /build/cares-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$deps" -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DCARES_SHARED=OFF -DCARES_STATIC=ON -DCARES_BUILD_TESTS=OFF -DCARES_BUILD_TOOLS=OFF
  cmake --build /build/cares-build -j "$jobs"
  cmake --install /build/cares-build
  cd /build/protobuf-c
  autoreconf -fi
  ./configure --prefix="$deps" --libdir="$deps/lib" --disable-shared --enable-static --disable-protoc
  make -j"$jobs"
  make install
  cmake -S /build/libyang -B /build/yang-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$deps" -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DBUILD_SHARED_LIBS=OFF -DENABLE_TESTS=OFF -DENABLE_LYD_PRIV=ON
  cmake --build /build/yang-build -j "$jobs"
  cmake --install /build/yang-build
  cd /build/frr
  # Backport upstream's mgmtd archive order before generating Makefile.in.
  patch --batch --fuzz=0 -p1 < /recipe/patches/frr/0001-mgmtd-static-link-order.patch
  ./bootstrap.sh
  # clippy embeds Python and is only a code-generation tool. Build it dynamically
  # first; --enable-static-bin must apply to shipped daemons, not to host Python.
  mkdir -p /build/clippy
  cd /build/clippy
  PYTHON=python3 /build/frr/configure --enable-clippy-only --disable-doc \
    --disable-nhrpd --disable-vtysh --disable-werror
  make -j"$jobs" lib/clippy
  cd /build/frr
  # AC_CHECK_LIB(lyd_find_xpath3) uses global LIBS, not LIBYANG_LIBS.
  # Include libyang's private dependencies so static probes and daemon links work.
  frr_libs="$(pkg-config --static --libs libyang readline) -lm -ldl -pthread -latomic"
  # FRR requires shared-library generation even when its installed daemons are fully static.
  PYTHON=python3 ./configure --prefix=/usr --sbindir=/usr/sbin --libdir=/usr/lib \
    --with-clippy=/build/clippy/lib/clippy \
    --sysconfdir=/etc/frr --localstatedir=/var/run/frr \
    --enable-shared --enable-static --enable-static-bin --enable-vtysh \
    --enable-user=root --enable-group=root --enable-vty-group=root \
    --disable-doc --disable-backtrace --disable-snmp --disable-rpki --disable-werror \
    --disable-sysrepo --disable-grpc --disable-zeromq --disable-fpm \
    LIBS="$frr_libs" || { cat config.log >&2; exit 1; }
  # Libtool's -static only selects static libtool libraries; libc can stay dynamic.
  # Apply -all-static to executables only, including any install-time relinks.
  make -j"$jobs" AC_LDFLAGS_EXEC=-all-static
  make DESTDIR=/stage AC_LDFLAGS_EXEC=-all-static install
  for bin in babeld bfdd bgpd eigrpd fabricd isisd ldpd nhrpd ospfd ospf6d pathd pbrd \
    pimd pim6d ripd ripngd staticd vrrpd watchfrr zebra mgmtd vtysh; do
    path=$(find /stage/usr -type f -name "$bin" | head -n 1)
    test -n "$path" || { echo "Missing required FRR binary: $bin" >&2; exit 1; }
    install -m 0755 "$path" "$out/bin/$bin"
  done
  if [ -d /stage/usr/share ]; then cp -R /stage/usr/share/. "$out/share/"; fi
  if [ -d "$deps/share/yang" ]; then mkdir -p "$out/share/yang"; cp -R "$deps/share/yang/." "$out/share/yang/"; fi
fi

for binary in "$out/bin/"*; do
  strip "$binary"
  if readelf -l "$binary" | grep -q 'Requesting program interpreter' || \
     readelf -d "$binary" 2>/dev/null | grep -q '(NEEDED)'; then
    echo "Not a static ELF: $binary" >&2
    readelf -l "$binary" >&2
    readelf -d "$binary" >&2
    exit 1
  fi
  timeout 30 "$binary" --version
done
cp "$lock" "$out/sources.lock.json"
