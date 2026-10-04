#!/bin/bash
# Build the Arch package in a clean Arch container. Needs podman, nothing else.
# Builds the working tree as it is (committed or not), not the release tag the
# PKGBUILD downloads. The package lands in dist/.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$root/pyproject.toml")
pkgbuild_version=$(sed -n 's/^pkgver=//p' "$root/packaging/arch/PKGBUILD")
if [ "$version" != "$pkgbuild_version" ]; then
    echo "pyproject.toml says $version but the PKGBUILD says $pkgbuild_version" >&2
    exit 1
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$root/dist"

# Named and laid out like GitHub's tag archive, so makepkg uses it instead of
# downloading.
git -C "$root" ls-files -z --cached --others --exclude-standard \
    | (cd "$root" && tar --null -T - --ignore-failed-read \
        --transform "s,^,AudioRouter-$version/," -czf "$work/audiorouter-$version.tar.gz")
cp "$root/packaging/arch/PKGBUILD" "$work/"
# makepkg refuses to run as root. It builds in the container's own folder:
# files a rootless container's user writes into a mount cannot be deleted here.
podman run --rm -v "$work:/src:Z" -v "$root/dist:/out:Z" docker.io/archlinux:latest bash -euc '
    pacman -Syu --noconfirm --needed base-devel &>/dev/null
    useradd -m builder
    echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/builder
    cp -r /src /home/builder/build && chown -R builder /home/builder/build
    cd /home/builder/build
    su builder -c "makepkg --syncdeps --noconfirm --cleanbuild --skipchecksums"
    cp $(ls *.pkg.tar.zst | grep -v -- -debug-) /out/
'
ls -1 "$root/dist/audiorouter-$version"-*.pkg.tar.zst
