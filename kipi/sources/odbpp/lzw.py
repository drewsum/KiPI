"""Unix ``compress`` (``.Z``, LZW) decoding, for ODB++ jobs saved with file compression.

Valor / Mentor tools write each ODB++ file as ``features.Z``, ``data.Z`` ... when their
"compress" option is on. Python has no built-in reader for the format, so it is decoded
here. The bit layout follows ncompress: codes are packed least significant bit first,
in groups of ``n_bits`` bytes; when the code width grows, or the table is cleared, the
rest of the current group is skipped.
"""

from __future__ import annotations

MAGIC = b"\x1f\x9d"
BLOCK_MODE = 0x80
CLEAR = 256
INIT_BITS = 9


class LzwError(ValueError):
    pass


def is_compressed(data: bytes) -> bool:
    return data[:2] == MAGIC


def decompress(data: bytes) -> bytes:
    """The contents of a ``.Z`` file."""
    if not is_compressed(data) or len(data) < 3:
        raise LzwError("not a Unix compress (.Z) stream")
    max_bits = data[2] & 0x1F
    block = bool(data[2] & BLOCK_MODE)
    if not INIT_BITS <= max_bits <= 16:
        raise LzwError(f"unsupported .Z code width {max_bits}")
    max_code = 1 << max_bits
    first_free = 257 if block else 256

    prefix = [0] * max_code
    suffix = bytearray(max_code)
    suffix[:256] = bytes(range(256))
    out = bytearray()
    stack = bytearray()

    pos, end = 24, len(data) * 8       # bit positions; the 3-byte header is skipped
    n_bits, free = INIT_BITS, first_free
    group_start = pos
    old = fin = -1

    def next_group(width: int) -> None:
        """Skip to the end of the current group of ``width``-bit codes."""
        nonlocal pos, group_start
        group = width * 8
        used = (pos - group_start) % group
        if used:
            pos += group - used
        group_start = pos

    while pos + n_bits <= end:
        chunk = int.from_bytes(data[pos >> 3:(pos >> 3) + 3], "little")
        code = (chunk >> (pos & 7)) & ((1 << n_bits) - 1)
        pos += n_bits

        if old == -1:                  # the first code after the start or a clear
            if code >= 256:
                raise LzwError("corrupt .Z stream (bad first code)")
            old = fin = code
            out.append(code)
            continue
        if block and code == CLEAR:
            next_group(n_bits)
            n_bits, free, old = INIT_BITS, first_free, -1
            continue

        incode = code
        stack.clear()
        if code >= free:               # the KwKwK case: the code being defined now
            if code > free:
                raise LzwError("corrupt .Z stream (code out of range)")
            stack.append(fin)
            code = old
        while code >= 256:
            stack.append(suffix[code])
            code = prefix[code]
        fin = code
        stack.append(fin)
        stack.reverse()
        out += stack

        if free < max_code:
            prefix[free] = old
            suffix[free] = fin
            free += 1
        old = incode
        if free >= (1 << n_bits) and n_bits < max_bits:
            next_group(n_bits)
            n_bits += 1
    return bytes(out)
