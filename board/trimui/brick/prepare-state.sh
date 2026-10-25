#!/bin/sh
# Preserve package init scripts behind the persistent-state startup gate.
set -eu
board=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target=$1
init=$target/etc/init.d
lib=$target/usr/libexec/brick-init
share=$target/usr/share/brick
mkdir -p "$lib" "$share"
cp "$board/rootfs-overlay/etc/wpa_supplicant.conf" "$share/wpa-volatile.conf"
sed 's/^update_config=0$/update_config=1/' "$share/wpa-volatile.conf" > "$share/wpa_supplicant.conf"

wrap() {
	old=$1
	new=$2
	wrapper=$lib/.wrapper-new
	printf '#!/bin/sh\nexec /usr/libexec/brick-state-service %s "$@"\n' "$new" > "$wrapper"
	chmod 755 "$wrapper"
	original=$init/$old
	[ -e "$original" ] || original=$init/$new
	if cmp -s "$original" "$wrapper"; then
		# A repeated post-build must not copy our wrapper over the daemon script.
		[ -x "$lib/$new" ] || { echo "Missing original init script: $new" >&2; exit 1; }
	else
		[ -x "$original" ] || { echo "Missing package init script: $old" >&2; exit 1; }
		cp "$original" "$lib/$new"
	fi
	mv "$wrapper" "$init/$new"
	[ "$old" = "$new" ] || rm -f "$init/$old"
}

# Save the seed before data unmounts in reverse shutdown order.
wrap S01seedrng S13seedrng
for service in S30dbus-daemon S40bluetoothd S45wifi S46bluealsa S50dropbear; do
	wrap "$service" "$service"
done
