#!/bin/bash
# Build the RPM in a clean Fedora container. Needs podman, nothing else.
# The package lands in dist/. FEDORA=45 packaging/build-rpm.sh for another release.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
fedora=${FEDORA:-44}
version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$root/pyproject.toml")
spec_version=$(sed -n 's/^Version: *//p' "$root/packaging/audiorouter.spec")
if [ "$version" != "$spec_version" ]; then
    echo "pyproject.toml says $version but the spec says $spec_version" >&2
    exit 1
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$root/dist"

# The working tree as it is, committed or not, minus what git ignores.
git -C "$root" ls-files -z --cached --others --exclude-standard \
    | (cd "$root" && tar --null -T - --ignore-failed-read \
        --transform "s,^,audiorouter-$version/," -czf "$work/audiorouter-$version.tar.gz")
cp "$root/packaging/audiorouter.spec" "$work/"
# rpmbuild runs as an unprivileged user inside the container.
chmod -R go+rX "$work"

podman run --rm -v "$work:/src:Z" -v "$root/dist:/out:Z" \
    "registry.fedoraproject.org/fedora:$fedora" bash -euc '
        dnf -y -q install rpm-build dnf-plugins-core &>/dev/null
        dnf -y -q builddep /src/audiorouter.spec &>/dev/null
        useradd -m builder
        su builder -c "rpmbuild -bb --define \"_sourcedir /src\" --define \"_rpmdir /tmp/rpms\" /src/audiorouter.spec"
        cp $(ls /tmp/rpms/*/*.rpm | grep -v debug) /out/
    '
ls -1 "$root/dist/audiorouter-$version"-*.rpm
