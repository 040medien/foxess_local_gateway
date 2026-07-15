# Firmware research notes

Internal engineering notes for repeatable analysis of FoxESS M1-family
firmware. Do not turn unknown register candidates into controls: the public
write surface remains restricted to `ActivePowerLimit` (`0xCA5A`).

## Captured 1.84 image

Observed during an M1-1200 FoxCloud relay upgrade on 2026-07-15:

- Cloud filename: `10-300-11100-31_M1_1200_v1.84_BA.bin`
- Image size: 51,941 bytes
- SHA-256: `ada02558b2725d62e6f005fbe86047f589355aa8682d7ae43d7ea89683349587`
- Protocol CRC16/Modbus, little-endian bytes: `c2 86`
- Transfer: one metadata frame plus 51 numbered chunks (1,024 bytes except
  the final chunk)
- The firmware bytes are carried inline in TCP/14431 `7f7f` frames with
  envelope function `0xA2`; no HTTP firmware URL was observed. Requests use
  transfer id `fa 6b 7c 7a 4d` and the metadata CRC field is big-endian.
- After the final image acknowledgement, the inverter reported progress in
  `7f7f` frames: 18, 38, 58, 77, 97, then 100 percent, followed by a reboot.
- An offline replay comparison found 52/52 cloud request frames in the relay
  log. Reassembling chunk indices 0 through 50 reproduced the archived image
  byte-for-byte. Generated metadata for `foxess-7f-func-a2` is byte-for-byte
  identical to the logged 67-byte metadata payload, including CRC field
  `86 c2`, filename length `0x24`, target serial, and timeout `0x012c`.
- A little-endian metadata CRC (`c2 86`) is accepted chunk-by-chunk but returns
  terminal status `03` after the final chunk. The exact big-endian field is
  therefore required; `03` is a validation failure, not flash acceptance.

The captured binary is deliberately gitignored. Each new capture should be
kept with the JSON manifest produced by `FirmwareCapture`; the manifest is the
chain between a FoxCloud-supplied image and a later local upload.

## Recovered 1.66 image and second protocol variant

An offered downgrade on 2026-07-15 exposed a second transfer variant before
the first interceptor supported it. The inverter therefore installed the
image, while the complete relay stream remained available for reconstruction:

- Cloud filename: `110-300-11100-14_M1_1200_v1.66_BA(Only Europe).bin`
- Local filename: `110-300-11100-14_M1_1200_v1.66_BA(Only Europe).bin`
- Image size: 45,769 bytes
- SHA-256: `546776648962e3230c25829a7f735a12c31d14f80c2641aabf65904f2f962220`
- CRC16/Modbus little-endian bytes: `f4 17`; metadata field: `17 f4`
- Transfer: `7f7f`, function `0x99`, magic `01 74 7c 7a 4d`, 45 chunks
- Like the `7f/0xA2` variant, the filename has a one-byte length and is
  NUL-terminated; the metadata CRC field is big-endian. The length bytes
  `0x24` and `0x32` were initially mistaken for filename characters because
  both are printable ASCII.
- Final acknowledgement is magic plus `00`; progress reports use magic plus
  `00` plus percentage.

The reconstructed size, contiguous chunk range, and cloud-supplied CRC all
match. Firmware 1.66 genuinely disables/greys out Active Power Limit in
FoxCloud, while 1.80 exposes it. Firmware 1.77 was not offered for upgrade or
downgrade, so the exact boundary below 1.80 could not be tested directly.

## Recovered 1.64 Brazil image and dynamic transfer id

A 1.64 Brazil-only downgrade exposed that the first two bytes previously
treated as part of the `7f/0x99` magic are image-specific:

- Cloud filename: `110-300-11100-12_M1_1200_v1.64_BA(Only Brazil).bin`
- Local filename: `110-300-11100-12_M1_1200_v1.64_BA(Only Brazil).bin`
- Image size: 45,673 bytes
- SHA-256: `e04b3e3178a709b42ae4275dab7f1e9d119257fb08340de15d391c58b2318a4b`
- CRC16/Modbus little-endian bytes: `45 73`; metadata field: `73 45`
- Transfer id: `ac 76 7c 7a 4d` (1.66 used `01 74 7c 7a 4d`)
- Transfer: 45 contiguous chunks, followed by progress through 100% and reboot

For `7f/0x99`, matching must therefore use the stable trailing signature
`7c 7a 4d`, learn the complete five-byte transfer id from the metadata frame,
and require subsequent chunks to use that same id. The interceptor now does
this dynamically and its tests use an id different from the original 1.66
capture to prevent another hard-coded regression.

## Recovered 1.80 Europe image and dynamic function

The 1.80 Europe upgrade showed that the envelope function is also selected
per transfer, rather than being fixed at `0x99`:

- Cloud filename: `10-300-11100-27_M1_1200_v1.80_BA.bin`
- Local filename: `10-300-11100-27_M1_1200_v1.80_BA.bin`
- Image size: 49,009 bytes
- SHA-256: `7d8d37463d0461bae975a3ef72f3f0e9031b16a0f6fbe3bb03617c6dc7a0eb59`
- CRC16/Modbus little-endian bytes: `d5 cd`; metadata field: `cd d5`
- Transfer id: `f9 77 7c 7a 4d`; envelope function: `0x5d`
- Transfer: 48 contiguous chunks, progress through 100%, reboot, and a clean
  reconnect reporting firmware 1.80

The matcher must identify cloud-to-device `7f` firmware frames by the device
class and stable `7c 7a 4d` payload signature, then learn both the complete
transfer id and envelope function from metadata. The historical manifest name
`foxess-7f-func-99` remains accepted as an alias; new captures use
`foxess-7f-dynamic`.

## Local replay and mesh behavior

The exact `foxess-7f-func-a2` replay of the verified 1.84 image was accepted:
all 51 chunks were acknowledged, progress matched the original
18/38/58/77/97/100 sequence, and the inverter rebooted reporting 1.84 with
fresh telemetry. Request frames and acknowledgements are both `7f`; matching
also accepts a `7f` acknowledgement for the legacy `7e` request definition so
an envelope-family mismatch cannot silently discard an otherwise correlated
reply.

A second inverter in the same mesh behaved differently. Sending the metadata
to its own connected session produced no standard metadata acknowledgement,
so the local command timed out before sending chunks. The inverter nevertheless
rebooted and later reported 1.84 with fresh telemetry. The most plausible
explanation is an internal mesh handoff using the image already staged by the
first upgrade, but the transport was not visible on TCP/14431 and this remains
an inference. Operationally, never retry a timed-out mesh firmware request
until the full metadata timeout has elapsed and product info plus telemetry
have been checked; timeout alone does not prove failure.

## Compatibility clues

Word-swapped strings include `SIW100G` in every captured image,
`FHE-MASTER` in 1.66 and later, and `M10300` in 1.83 and 1.84. The first two
appear to be rebrand/product-family identifiers. Together with matching public
power variants, this is good evidence that WEG SIW100G M006/M008/M010/M012 W00
and FHE-MASTER 600/800/1000/1200 use the same firmware platform. It is not
proof of wire compatibility; neither family has yet been tested against the
gateway.

## Cross-version comparison

The five captured images were compared byte-for-byte after verifying their
manifests. None has a container signature recognized by `file` or Binwalk.
Their moderate entropy, repeated code/data blocks, readable word-swapped
strings, and regular descriptor tables are evidence that these are raw,
uncompressed images rather than encrypted or compressed containers.

| Version | Region label | Size | Shannon entropy | Descriptor format |
| --- | --- | ---: | ---: | --- |
| 1.64 | Brazil only | 45,673 | 6.9031 bits/byte | 153 x 20 bytes |
| 1.66 | Europe only | 45,769 | 6.9062 bits/byte | 153 x 20 bytes |
| 1.80 | none | 49,009 | 6.8910 bits/byte | 153 x 20 bytes |
| 1.83 | none | 51,941 | 6.9363 bits/byte | 328 x 12 bytes |
| 1.84 | none | 51,941 | 6.9360 bits/byte | 328 x 12 bytes |

Verified SHA-256 identities used for the comparison:

- 1.64: `e04b3e3178a709b42ae4275dab7f1e9d119257fb08340de15d391c58b2318a4b`
- 1.66: `546776648962e3230c25829a7f735a12c31d14f80c2641aabf65904f2f962220`
- 1.80: `7d8d37463d0461bae975a3ef72f3f0e9031b16a0f6fbe3bb03617c6dc7a0eb59`
- 1.83: `aba247328acf80c6ec8acdefe4b2ea9ed6e2428bf6fbbd4acf99410de83876b6`
- 1.84: `ada02558b2725d62e6f005fbe86047f589355aa8682d7ae43d7ea89683349587`

An order-preserving binary comparison gives the following exact-match ratios.
This is a structural similarity measure, not a count of changed source lines:
inserted code moves later blocks even when their contents remain identical.

| Pair | Size delta | Exact-match ratio | Main observation |
| --- | ---: | ---: | --- |
| 1.64 -> 1.66 | +96 bytes | 93.17% | very closely related builds; region and version both differ |
| 1.66 -> 1.80 | +3,240 bytes | 70.09% | substantial code growth; power-limit descriptor changes |
| 1.80 -> 1.83 | +2,932 bytes | 80.85% | descriptor map expands and changes representation |
| 1.83 -> 1.84 | 0 bytes | 99.969% | only 16 bytes differ in 13 short runs |

The 1.83 and 1.84 descriptor tables are byte-identical. Their 16 changed bytes
are confined to seven one-byte changes between offsets `0x6142` and `0x6B06`,
six bytes in three repeated data/check fields between `0xCA24` and `0xCA5E`,
and three bytes around the trailing version string at `0xCADC..0xCAE0`. The
first `0x6142` bytes are identical, as is the large block from `0x6B07` to the
data/check fields. This makes 1.84 look like a small maintenance release, with
no register-map change, rather than a new platform build. The meaning of the
seven code-area changes cannot be established until the instruction set and
load map are known.

The descriptor evolution gives the clearest power-limit result:

- 1.64 and 1.66 do not contain the little-endian literal `0xCA5A`. They instead
  contain one descriptor for `0xCA59`, internal id `0x012C`, bounds 0..100, and
  metadata 0. Its similar shape makes it a possible predecessor, but it is not
  identified and must not be written.
- 1.80 replaces that one address with `0xCA5A`, internal id `0x0132`, bounds
  0..100, and metadata 1. The descriptor count remains 153. This is direct
  firmware evidence for a feature boundary at 1.80 and agrees with FoxCloud
  exposing Active Power Limit on 1.80 but not 1.66.
- 1.83 and 1.84 retain `0xCA5A` with the same bounds and metadata. Its internal
  id moves to `0x0147` as the table grows to 328 records. Internal ids are
  therefore build artifacts, not stable protocol identifiers.

No human-readable `ActivePowerLimit` label is embedded in any captured image.
The descriptor and the independently observed Modbus traffic are what identify
`0xCA5A`. Firmware 1.77 is still unavailable, so these images do not establish
whether an intermediate release below 1.80 used `0xCA59`, omitted local
control, or used another implementation.

## Register descriptor table

Every image contains a sorted little-endian descriptor table. The pre-1.83
images use 153 20-byte records; 1.83 and 1.84 use 328 12-byte records:

| Version | File range (end exclusive) | Working record layout |
| --- | --- | --- |
| 1.64 | `0x9BD8..0xA7CC` | `<u16, u16, u32, i32, u32, u32>` |
| 1.66 | `0x9C24..0xA818` | `<u16, u16, u32, i32, u32, u32>` |
| 1.80 | `0xA254..0xAE48` | `<u16, u16, u32, i32, u32, u32>` |
| 1.83/1.84 | `0xA862..0xB7C2` | `<u16, u16, u16, i16, u16, u16>` |

The fields currently fit the working interpretation:

```text
<register address, internal id, flags, minimum, maximum, metadata>
```

Signed minima such as -100 and -1000 survive the 32-to-16-bit representation
change, which supports the widths shown above. The field meanings are still
inferred rather than confirmed by code references. The expanded 1.83/1.84
table contains these notable ranges:

| Register range | Count | Working note |
| --- | ---: | --- |
| `0x2724..0x2746` | 35 | unknown |
| `0x277E..0x2799` | 28 | unknown; `0x277E` shares internal id `0x05B1` with `0xC419` |
| `0x320A..0x3247` | 62 | likely read-only diagnostic/snapshot bank |
| `0xC419..0xC41A`, `0xC41C`, `0xC41F..0xC421` | 6 | unknown settings/status |
| `0xC5A8..0xC5DC` | 53 | unknown settings/status |
| `0xC670..0xC67B` | 12 | unknown |
| `0xC6D4..0xC6E1` | 14 | unknown |
| `0xC79C..0xC7A4` | 9 | unknown |
| `0xC864..0xC86B` | 8 | unknown |
| `0xC8C8..0xC8CF` | 8 | unknown |
| `0xC92C..0xC946` | 27 | likely installer/grid configuration; do not expose |
| `0xC990..0xC9A4` | 21 | likely installer/grid configuration; do not expose |
| `0xCA5A` | 1 | confirmed `ActivePowerLimit`, id `0x0147`, min 0, max 100, metadata 1 |
| `0xCABC..0xCAC5` | 10 | unknown |
| `0xD2F0..0xD30D` | 30 | unknown |
| `0xD313..0xD314` | 2 | unknown |
| `0xD319..0xD31A` | 2 | unknown |

The table supports the existing `0xCA5A` mapping and provides candidates for
read-only investigation. It does not establish that a register is safe to
write. In particular, grid voltage/frequency protection, reactive power, and
safety-country settings remain out of scope.

## Analysis status and next steps

Stock Ghidra loaders did not identify the processor architecture from the raw
image, and treating it as dsPIC code did not produce credible disassembly. The
image may have a vendor header, word/byte transform, split sections, or target
an architecture not covered by the attempted loader.

Next investigation steps, in order:

1. Capture 1.79 or the nearest available pre-1.80 Europe image. A same-region
   adjacent-version diff remains the best route to the power-limit
   implementation and avoids the region/version ambiguity in the 1.64/1.66
   pair.
2. Export every version's descriptors to CSV with file offsets and normalized
   signed minima. Look for sorted secondary indexes and code references to the
   table lookup machinery; direct searches for `0xCA5A` only find its one
   descriptor record.
3. Identify the MCU from PCB markings, update metadata, boot vectors, and
   instruction patterns. Build a Ghidra loader or preprocessor only after the
   byte/word transform and memory map are evidenced.
4. Compare images offered for FoxESS, WEG SIW100G, and FHE-MASTER devices. A
   byte-identical or minimally branded image would materially strengthen the
   compatibility claim.
5. On live hardware, issue only one-shot reads of candidate ranges and
   correlate them with installer/app values and operating states. Record
   firmware, model, mesh role, time, and raw responses. Do not poll expecting
   fresh telemetry: the known Modbus readable window is a boot-time snapshot.
6. Never write an unknown candidate register. Promote a field only after it is
   identified in firmware and independently correlated read-only on hardware;
   regulatory settings must remain unexposed even if decoded.

## Implementation map

- `foxess_local_cloud/firmware.py`: metadata/chunk protocol, interception,
  manifests, simulated progress, and local uploader state machine.
- `firmware_capture` config: opt-in; usable only while relay traffic is present.
- `tools/foxess-firmware-upgrade`: separate hash-gated maintenance client over
  a root/local Unix socket.
- `tests/test_firmware.py`: synthetic protocol coverage; contains no production
  serials or copyrighted firmware bytes.
