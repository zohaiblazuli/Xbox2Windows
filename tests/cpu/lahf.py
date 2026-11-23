"""Differential check for LAHF in the shared lowering (tools/ffx_version_query.py).

LAHF loads AH = SF:ZF:0:AF:0:PF:1:CF and leaves the rest of RAX and every flag unchanged. The lowering accepts
it only when its flags come from a register ADD, SUB or CMP earlier in the same straight-line block
(lahf_sources), which then also assigns AF and PF. Every export of lahf.asm is compared with the native DLL and
with a reference model on the whole returned RAX. Refused shapes and mutated translations are checked as
controls.
"""
import ctypes
import re
import sys
from pathlib import Path

from iced_x86 import Decoder
import pefile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from cpu_leaf_aot import export_rva
from cpu_ranges import function_ranges
from ffx_version_query import INTEGER_HEADER, lower, region

WIDTHS = (64, 32, 16, 8)
EXPORTS = tuple(f"lahf_{op}{w}" for op in ("add", "sub", "cmp") for w in WIDTHS) + (
    "lahf_add_imm8", "lahf_sub_imm32", "lahf_cmp_imm8", "lahf_add_rax", "lahf_cmp_ah", "lahf_sub_al",
    "lahf_moves_between", "lahf_consumer",
)

# LAHF/SAHF in 64-bit mode: CPUID.80000001H:ECX bit 0.
CPUID_HELPER = r"""#include <intrin.h>
extern "C" __declspec(dllexport) uint32_t host_has_lahf() {
    int r[4];
    __cpuid(r, static_cast<int>(0x80000000u));
    if (static_cast<unsigned>(r[0]) < 0x80000001u) return 0;
    __cpuid(r, static_cast<int>(0x80000001u));
    return static_cast<uint32_t>(r[2]) & 1u;
}
"""

AUX = re.compile(r"aux = \(\(a \^ b \^ [vd]\) & 0x10u\) != 0;")
MUTATED_CONTROLS = (
    # AF dropped: catches a lowering that never models AF.
    ("lahf_add8", "lahf_no_aux_control", AUX, "aux = false;"),
    # PF inverted: catches odd parity, or parity of more than the low byte if inputs only differ there.
    ("lahf_cmp16", "lahf_odd_parity_control", re.compile(r"& 1u\) == 0;"), "& 1u) != 0;"),
    # LAHF writing the whole of RAX instead of AH.
    ("lahf_sub64", "lahf_whole_rax_control", re.compile(r"\(rax & ~0xff00ull\)"), "(rax & 0ull)"),
    # Reserved bit 1 left clear.
    ("lahf_cmp8", "lahf_reserved_bit_control", re.compile(r" \| 0x200ull"), ""),
    # LAHF changing a flag that a later consumer reads (OF, which LAHF neither reads nor writes).
    ("lahf_consumer", "lahf_writes_overflow_control", re.compile(r"\(word\(carry\) << 8\);"),
     "(word(carry) << 8); overflow = !overflow;"),
)


def _wrap(name, code):
    return f'extern "C" __declspec(dllexport) uint64_t {name}(uint64_t a, uint64_t b, uint64_t c) {{\n{code}\n}}'


def emit(native_dll: Path, output_cpp: Path) -> None:
    parts = [INTEGER_HEADER, CPUID_HELPER]
    lowered = {}
    with pefile.PE(str(native_dll), fast_load=True) as pe:
        for name in EXPORTS:
            ranges = function_ranges(pe, export_rva(pe, name))
            body = [ins for lo, hi in ranges for ins in region(pe, lo, hi)]
            lowered[name] = lower(body, ["rcx = a; rdx = b; r8 = c;"], {}, {}, {})
            parts.append(_wrap(name, lowered[name]))
    for source, control, pattern, replacement in MUTATED_CONTROLS:
        mutated, count = pattern.subn(replacement, lowered[source])
        if count != 1:
            raise AssertionError(f"mutated control {control} changed {count} places, expected 1")
        parts.append(_wrap(control, mutated))
    output_cpp.write_text("\n".join(parts) + "\n", encoding="utf-8")


# Refused shapes. Each is lowered alone (a RET follows) and must raise the query refusal before any C++.
REFUSED = (
    ("4885d29fc3", "after TEST, which leaves AF undefined"),
    ("4821d19fc3", "after AND"),
    ("4809d19fc3", "after OR"),
    ("4831d19fc3", "after XOR"),
    ("48d1e19fc3", "after SHL"),
    ("480fafca9fc3", "after IMUL"),
    ("48f7d99fc3", "after NEG"),
    ("48ffc19fc3", "after INC, whose CF comes from before it"),
    ("48ffc99fc3", "after DEC"),
    ("4811d19fc3", "after ADC"),
    ("4819d19fc3", "after SBB"),
    ("48011a9fc3", "after ADD with a memory destination"),
    ("483b0a9fc3", "after CMP with a memory source"),
    ("9fc3", "at the function entry"),
    ("48c7c0010000009fc3", "after only a MOV, at the function entry"),
    ("4801d1e8000000009fc3", "across a call"),
    ("4801d174009fc3", "at a branch target"),
    ("4801d14889c89f75fac3", "across a branch target between the writer and LAHF"),
    ("4801d174019f90c3", "across a conditional branch"),
)


def check_controls() -> int:
    for encoded, description in REFUSED:
        body = list(Decoder(64, bytes.fromhex(encoded), ip=0x1000))
        try:
            lower(body, [], {}, {}, {})
        except ValueError as error:
            if "unsupported query operation" not in str(error) or "lahf" not in str(error):
                raise AssertionError(f"LAHF {description}: unexpected refusal {error}")
            continue
        raise AssertionError(f"expected LAHF {description} to be refused")
    return len(REFUSED)


def _flags(op, width, a, b):
    """SF, ZF, AF, PF, CF and OF of ADD or SUB/CMP at width, and the result."""
    mask = (1 << width) - 1
    a &= mask
    b &= mask
    r = (a + b) & mask if op == "add" else (a - b) & mask
    carry = a + b > mask if op == "add" else a < b
    sign_bit = 1 << (width - 1)
    if op == "add":
        overflow = bool(~(a ^ b) & (a ^ r) & sign_bit)
    else:
        overflow = bool((a ^ b) & (a ^ r) & sign_bit)
    flags = {
        "SF": bool(r & sign_bit), "ZF": r == 0, "AF": bool((a ^ b ^ r) & 0x10),
        "PF": bin(r & 0xFF).count("1") % 2 == 0, "CF": carry, "OF": overflow,
    }
    return flags, r


def _ah(f):
    return (f["SF"] << 7) | (f["ZF"] << 6) | (f["AF"] << 4) | (f["PF"] << 2) | 0x02 | f["CF"]


def _with_ah(rax, ah):
    return (rax & ~0xFF00) | (ah << 8)


def reference(name, x, y, rax):
    """The whole RAX the export returns, and the flags LAHF read."""
    m = re.fullmatch(r"lahf_(add|sub|cmp)(64|32|16|8)", name)
    if m:
        op, width = m.group(1), int(m.group(2))
        f, _ = _flags("sub" if op == "cmp" else op, width, x, y)
        return _with_ah(rax, _ah(f)), f
    if name == "lahf_add_imm8":
        f, _ = _flags("add", 64, x, 0x0F)
        return _with_ah(rax, _ah(f)), f
    if name == "lahf_sub_imm32":
        f, _ = _flags("sub", 32, x, 0x12345678)
        return _with_ah(rax, _ah(f)), f
    if name == "lahf_cmp_imm8":
        f, _ = _flags("sub", 8, y, 0x80)
        return _with_ah(rax, _ah(f)), f
    if name == "lahf_add_rax":
        f, r = _flags("add", 64, rax, x)
        return _with_ah(r, _ah(f)), f
    if name == "lahf_cmp_ah":
        f, _ = _flags("sub", 8, rax >> 8, x)
        return _with_ah(rax, _ah(f)), f
    if name == "lahf_sub_al":
        f, r = _flags("sub", 8, rax, y)
        return _with_ah((rax & ~0xFF) | r, _ah(f)), f
    if name == "lahf_moves_between":
        f, _ = _flags("sub", 64, x, y)
        return _with_ah(rax, _ah(f)), f
    if name == "lahf_consumer":  # SETL AL, then BSWAP RAX unless JBE is taken
        f, _ = _flags("sub", 64, x, y)
        value = (_with_ah(rax, _ah(f)) & ~0xFF) | int(f["SF"] != f["OF"])
        if not (f["CF"] or f["ZF"]):
            value = int.from_bytes(value.to_bytes(8, "little"), "big")
        return value, f
    raise KeyError(name)


# Operands near every carry, sign and nibble boundary at each width, and a few without structure.
VALUES = (
    0, 1, 0x0F, 0x10, 0x11, 0x7F, 0x80, 0xFF, 0x100, 0x7FFF, 0x8000, 0xFFFF, 0x7FFFFFFF, 0x80000000,
    0xFFFFFFFF, 0x12345678, 0x7FFFFFFFFFFFFFFF, 0x8000000000000000, 0xFFFFFFFFFFFFFFFF,
    0x0123456789ABCDEF, 0xFEDCBA9876543210, 0x00000000F0F0F00F,
)
# RAX before the flag writer: all clear, all set (reserved AH bits 5, 3 and 1 visible), and a distinct pattern.
PRELOADS = (0, 0xFFFFFFFFFFFFFFFF, 0x0123456789ABCDEF, 0x5A5A5A5A5A5AD5A5)


def check(native_dll: Path, lifted_dll: Path) -> None:
    refused = check_controls()
    lifted = ctypes.CDLL(str(lifted_dll))
    lifted.host_has_lahf.argtypes, lifted.host_has_lahf.restype = [], ctypes.c_uint32
    if not lifted.host_has_lahf():
        print("host CPU lacks LAHF/SAHF in 64-bit mode; skipping the native differential check")
        raise SystemExit(77)
    native = ctypes.CDLL(str(native_dll))

    def function(dll, name):
        fn = getattr(dll, name)
        fn.argtypes = [ctypes.c_uint64] * 3
        fn.restype = ctypes.c_uint64
        return fn

    cases = 0
    seen = {flag: set() for flag in ("SF", "ZF", "AF", "PF", "CF")}
    for name in EXPORTS:
        n, t = function(native, name), function(lifted, name)
        for x in VALUES:
            for y in VALUES:
                for rax in PRELOADS:
                    want, f = reference(name, x, y, rax)
                    got_native, got_lifted = n(x, y, rax), t(x, y, rax)
                    if got_native != want or got_lifted != want:
                        raise AssertionError(f"{name}(x={x:#x}, y={y:#x}, rax={rax:#x}): expected {want:#018x} "
                                             f"native {got_native:#018x} lifted {got_lifted:#018x}")
                    for flag in seen:
                        seen[flag].add(f[flag])
                    cases += 1
    for flag, values in seen.items():
        if values != {False, True}:
            raise AssertionError(f"the inputs never produce {flag} = {not next(iter(values))}")

    for source, control, _pattern, _replacement in MUTATED_CONTROLS:
        fn = function(lifted, control)
        if all(fn(x, y, rax) == reference(source, x, y, rax)[0]
               for x in VALUES for y in VALUES for rax in PRELOADS):
            raise AssertionError(f"mutated control {control} matched {source} on every input")

    print(f"{cases} LAHF cases across {len(EXPORTS)} exports matched the native DLL and the reference on the whole "
          f"RAX, with SF, ZF, AF, PF and CF each seen both ways; {refused} refused shapes; "
          f"{len(MUTATED_CONTROLS)} mutated controls mismatched")


if __name__ == "__main__":
    mode, native_path, other = sys.argv[1:4]
    if mode == "emit":
        emit(Path(native_path), Path(other))
    elif mode == "check":
        check(Path(native_path), Path(other))
    else:
        raise SystemExit(f"unknown mode {mode}")
