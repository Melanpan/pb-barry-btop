#!/bin/sh
# Build the app archive for `barry-app install`: dist/btop-VERSION.zip.
#
#   ./package.sh                  the app alone; it runs the system's btop
#   ./package.sh --with-btop      with btop's static build for aarch64 (the
#                                 AYN Thor) in bin/, for systems without btop
#                                 (SteamOS has none, and /usr is read-only)
#   ./package.sh --with-btop=x86_64   the same for an x86_64 handheld
#
# BTOP_VERSION picks btop's release (default below).
set -eu

BTOP_VERSION=${BTOP_VERSION:-1.4.7}
here=$(cd "$(dirname "$0")" && pwd)
arch=
for a in "$@"; do
    case $a in
        --with-btop) arch=aarch64 ;;
        --with-btop=*) arch=${a#--with-btop=} ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "package.sh: unknown option $a" >&2; exit 2 ;;
    esac
done

version=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$here/barry-app.json")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
app="$work/btop"
mkdir -p "$app" "$here/dist"
cp "$here/barry-app.json" "$here/main.qml" "$here/service.py" "$here/btop.conf" "$here/icon.svg" \
   "$here/LICENSE" "$app/"

name="btop-$version"
if [ -n "$arch" ]; then
    url="https://github.com/aristocratos/btop/releases/download/v$BTOP_VERSION/btop-$arch-unknown-linux-musl.tar.gz"
    echo "Fetching btop $BTOP_VERSION for $arch"
    curl -fL --retry 3 -o "$work/btop.tar.gz" "$url"
    mkdir "$work/unpacked"
    tar xzf "$work/btop.tar.gz" -C "$work/unpacked"
    mkdir -p "$app/bin"
    cp "$work/unpacked/btop/bin/btop" "$app/bin/btop"
    cp "$work/unpacked/btop/LICENSE" "$app/bin/btop-LICENSE"
    name="$name-with-btop-$BTOP_VERSION-$arch"
fi

rm -f "$here/dist/$name.zip"
if command -v zip >/dev/null; then
    (cd "$work" && zip -qr "$here/dist/$name.zip" btop)
else
    (cd "$work" && python3 -m zipfile -c "$here/dist/$name.zip" btop)
fi
echo "dist/$name.zip"
