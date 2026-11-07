# Repository boundaries

The Brick product is assembled from three independently versioned repositories.
Keep their responsibilities and release provenance separate.

## Kernel: `trimui-brick-kernel`

Owns Linux device drivers, device trees, kernel configuration and kernel
self-tests. Kernel changes reach a firmware release only through a published,
checksum-pinned kernel archive. The firmware release must not depend on a local
kernel checkout or carry driver source patches in a Buildroot external.

## Base firmware: `trimui-brick-firmware`

Owns the board Buildroot configuration, boot artifacts, hardware services,
Wi-Fi authority, supplicant/DHCP integration and the captive-browser runtime.
It exposes stable product-facing service APIs, but contains no shell screens or
game UX. Its kernel input is the pinned archive named in `brick_defconfig`.

## Product firmware: `trimui-brick-custom-firmware`

Owns the Brick shell, games, Settings and in-game UX. It consumes base service
APIs and composes the final image as a second Buildroot external. It contains no
kernel or board-driver implementation and does not access wpa_supplicant
directly.

## Release rule

A product release records clean commits for both Buildroot externals and the
exact pinned kernel archive revision. Local kernel overrides are development
builds and cannot pass the release gate. Changes crossing a boundary are
released in dependency order: kernel archive, base firmware, then product
firmware.
