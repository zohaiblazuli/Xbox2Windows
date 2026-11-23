"""Offline C++ lowering for the null-context FidelityFX version-query path."""
import argparse
import hashlib
from pathlib import Path
import re

import pefile
from iced_x86 import (Code as C, Decoder, EncodingKind, FlowControl, MemorySizeExt, Mnemonic as M, OpKind as K,
                      Register as R, RegisterExt, RflagsBits as F)

from cpu_leaf_aot import export_rva
from cpu_ranges import function_ranges, image_bytes
import x87_lower


LOADER = "431bdd2e26c0c8214fd7601140eb9fe6d8a249f5000539453a9a9b6a0b7c0ee8"
UPSCALER = "faff02f294a7e9149ce079dffe3dedbcabe99b1f180ab14222aa8689e00a75fa"
REGS = {getattr(R, n.upper()): n for n in "rax rbx rcx rdx rsi rdi rbp rsp r8 r9 r10 r11 r12 r13 r14 r15".split()}
HIGH_BYTE = {R.AH, R.BH, R.CH, R.DH}
UINT = {1: "uint8_t", 2: "uint16_t", 4: "uint32_t", 8: "uint64_t"}
FRAME_WORDS = 16  # the lowered function's own frame array, at which RSP points on entry
# String instructions by Code: the SSE2 scalar MOVSD and CMPSD share their mnemonics.
STRING_OPS = {
    C.MOVSB_M8_M8: ("movs", 1), C.MOVSW_M16_M16: ("movs", 2), C.MOVSD_M32_M32: ("movs", 4), C.MOVSQ_M64_M64: ("movs", 8),
    C.STOSB_M8_AL: ("stos", 1), C.STOSW_M16_AX: ("stos", 2), C.STOSD_M32_EAX: ("stos", 4), C.STOSQ_M64_RAX: ("stos", 8),
    C.LODSB_AL_M8: ("lods", 1), C.LODSW_AX_M16: ("lods", 2), C.LODSD_EAX_M32: ("lods", 4), C.LODSQ_RAX_M64: ("lods", 8),
    C.SCASB_AL_M8: ("scas", 1), C.SCASW_AX_M16: ("scas", 2), C.SCASD_EAX_M32: ("scas", 4), C.SCASQ_RAX_M64: ("scas", 8),
    C.CMPSB_M8_M8: ("cmps", 1), C.CMPSW_M16_M16: ("cmps", 2), C.CMPSD_M32_M32: ("cmps", 4), C.CMPSQ_M64_M64: ("cmps", 8),
}

# Memory-ordering fences and PAUSE, and the cache hints with one memory operand, by the MSVC intrinsic
# that emits the same instruction.
FENCES = {M.LFENCE: "_mm_lfence();", M.MFENCE: "_mm_mfence();", M.SFENCE: "_mm_sfence();", M.PAUSE: "_mm_pause();"}
CACHE_HINTS = {
    M.PREFETCHT0: "prefetch_t0", M.PREFETCHT1: "prefetch_t1", M.PREFETCHT2: "prefetch_t2",
    M.PREFETCHNTA: "prefetch_nta", M.PREFETCH: "prefetch_amd", M.PREFETCHW: "_m_prefetchw",
    M.CLFLUSH: "_mm_clflush", M.CLFLUSHOPT: "_mm_clflushopt", M.CLWB: "_mm_clwb",
}

INTEGER_HEADER = """#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <intrin.h>
#include <type_traits>
#include <emmintrin.h>
#include <immintrin.h>
using std::memcpy;
using std::memset;
using word = uint64_t;
static_assert(sizeof(void*) == sizeof(word));
// A read of host data, or of the lowered function's own frame array, which cannot fault.
template<class T> T load(word address) {
    T v; memcpy(&v, reinterpret_cast<const void*>(address), sizeof v); return v;
}
static inline void prefetch_t0(const char* p) { _mm_prefetch(p, _MM_HINT_T0); }
static inline void prefetch_t1(const char* p) { _mm_prefetch(p, _MM_HINT_T1); }
static inline void prefetch_t2(const char* p) { _mm_prefetch(p, _MM_HINT_T2); }
static inline void prefetch_nta(const char* p) { _mm_prefetch(p, _MM_HINT_NTA); }
static inline void prefetch_amd(const char* p) { _m_prefetch(const_cast<char*>(p)); }
// Every guest read, once per memory operand: the compiler must perform it even when its value is unused,
// because the original faults on an inaccessible address either way. A scalar is one access at its width;
// a vector operand is read byte by byte, which faults on the same addresses.
template<class T> typename std::enable_if<std::is_arithmetic<T>::value, T>::type load_always(word address) {
    return *reinterpret_cast<const volatile T __unaligned*>(address);
}
template<class T> typename std::enable_if<!std::is_arithmetic<T>::value, T>::type load_always(word address) {
    T v;
    const volatile auto* p = reinterpret_cast<const volatile uint8_t*>(address);
    auto* q = reinterpret_cast<uint8_t*>(&v);
    for (size_t i = 0; i < sizeof v; ++i) q[i] = p[i];
    return v;
}
template<class T> void store(word address, T v) {
    memcpy(reinterpret_cast<void*>(address), &v, sizeof v);
}
static inline uint32_t sha_rol32(uint32_t x, unsigned n) { return (x << n) | (x >> (32u - n)); }
static inline uint32_t sha_ror32(uint32_t x, unsigned n) { return (x >> n) | (x << (32u - n)); }
static inline void sha1_rnds4(uint32_t d[4], const uint32_t s[4], unsigned func) {
    uint32_t a = d[3], b = d[2], c = d[1], dv = d[0], e = 0;
    const uint32_t w[4] = {s[3], s[2], s[1], s[0]};
    const uint32_t k = func == 0 ? 0x5a827999u : (func == 1 ? 0x6ed9eba1u : (func == 2 ? 0x8f1bbcdcu : 0xca62c1d6u));
    for (int i = 0; i < 4; ++i) {
        const uint32_t f = func == 0 ? ((b & c) ^ (~b & dv))
                         : (func == 2 ? ((b & c) ^ (b & dv) ^ (c & dv)) : (b ^ c ^ dv));
        const uint32_t t = f + sha_rol32(a, 5) + w[i] + k + e;
        e = dv; dv = c; c = sha_rol32(b, 30); b = a; a = t;
    }
    d[3] = a; d[2] = b; d[1] = c; d[0] = dv;
}
static inline void sha256_rnds2(uint32_t d[4], const uint32_t s[4], const uint32_t wk[4]) {
    uint32_t a = s[3], b = s[2], c = d[3], dv = d[2], e = s[1], f = s[0], g = d[1], h = d[0];
    for (int i = 0; i < 2; ++i) {
        const uint32_t ch = (e & f) ^ (~e & g);
        const uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        const uint32_t s0 = sha_ror32(a, 2) ^ sha_ror32(a, 13) ^ sha_ror32(a, 22);
        const uint32_t s1 = sha_ror32(e, 6) ^ sha_ror32(e, 11) ^ sha_ror32(e, 25);
        const uint32_t t1 = ch + s1 + wk[i] + h;
        const uint32_t na = t1 + maj + s0, ne = t1 + dv;
        h = g; g = f; f = e; e = ne; dv = c; c = b; b = a; a = na;
    }
    d[3] = a; d[2] = b; d[1] = e; d[0] = f;
}
extern "C" __declspec(dllimport) void __stdcall RaiseException(unsigned long, unsigned long, unsigned long, const uintptr_t*);
[[noreturn]] static void raise_de(bool zero_divisor) {
    // Windows distinguishes a zero divisor from a quotient that does not fit.
    RaiseException(zero_divisor ? 0xc0000094ul : 0xc0000095ul, 1, 0, nullptr);
    __assume(0);
}
[[noreturn]] static void raise_aligned_m128_fault() {
    // #GP(0) for an instruction requiring a 16-byte-aligned operand. Windows reports it as an access
    // violation reading address -1 (measured), so the exception carries the same two parameters.
    const uintptr_t info[2] = {0, ~static_cast<uintptr_t>(0)};
    RaiseException(0xc0000005ul, 0, 2, info);
    __assume(0);
}
static std::array<uint8_t,16> load_legacy_aligned_m128(word address) {
    if (address & 15u) raise_aligned_m128_fault();
    return load_always<std::array<uint8_t,16>>(address);
}
static void cmpxchg_writeback(word address, uint64_t value) {
    // A failed compare writes the old value back. Preserve the access under optimization.
    *reinterpret_cast<volatile uint64_t __unaligned*>(address) = value;
}
static inline uint8_t aes_gf_mul(uint8_t a, uint8_t b) {
    uint8_t p = 0;
    for (int i = 0; i < 8; ++i) {
        if (b & 1u) p ^= a;
        a = static_cast<uint8_t>((a << 1) ^ ((a & 0x80u) ? 0x1bu : 0u));
        b >>= 1;
    }
    return p;
}
static inline uint8_t aes_gf_inv(uint8_t x) {
    uint8_t r = 1, b = x;
    for (int e = 254; e > 0; e >>= 1) {
        if (e & 1) r = aes_gf_mul(r, b);
        b = aes_gf_mul(b, b);
    }
    return x == 0 ? 0 : r;
}
static inline uint8_t aes_rotl8(uint8_t x, unsigned n) {
    return static_cast<uint8_t>((x << n) | (x >> (8u - n)));
}
static inline uint8_t aes_sbox(uint8_t x) {
    const uint8_t s = aes_gf_inv(x);
    return static_cast<uint8_t>(s ^ aes_rotl8(s, 1) ^ aes_rotl8(s, 2) ^ aes_rotl8(s, 3) ^ aes_rotl8(s, 4) ^ 0x63u);
}
static inline uint8_t aes_inv_sbox(uint8_t x) {
    return aes_gf_inv(static_cast<uint8_t>(aes_rotl8(x, 1) ^ aes_rotl8(x, 3) ^ aes_rotl8(x, 6) ^ 0x05u));
}
static inline void aes_mix_columns(uint8_t s[16], bool inv) {
    for (size_t c = 0; c < 16; c += 4) {
        const uint8_t a0 = s[c], a1 = s[c+1], a2 = s[c+2], a3 = s[c+3];
        if (!inv) {
            s[c]   = static_cast<uint8_t>(aes_gf_mul(a0, 2) ^ aes_gf_mul(a1, 3) ^ a2 ^ a3);
            s[c+1] = static_cast<uint8_t>(a0 ^ aes_gf_mul(a1, 2) ^ aes_gf_mul(a2, 3) ^ a3);
            s[c+2] = static_cast<uint8_t>(a0 ^ a1 ^ aes_gf_mul(a2, 2) ^ aes_gf_mul(a3, 3));
            s[c+3] = static_cast<uint8_t>(aes_gf_mul(a0, 3) ^ a1 ^ a2 ^ aes_gf_mul(a3, 2));
        } else {
            s[c]   = static_cast<uint8_t>(aes_gf_mul(a0, 14) ^ aes_gf_mul(a1, 11) ^ aes_gf_mul(a2, 13) ^ aes_gf_mul(a3, 9));
            s[c+1] = static_cast<uint8_t>(aes_gf_mul(a0, 9)  ^ aes_gf_mul(a1, 14) ^ aes_gf_mul(a2, 11) ^ aes_gf_mul(a3, 13));
            s[c+2] = static_cast<uint8_t>(aes_gf_mul(a0, 13) ^ aes_gf_mul(a1, 9)  ^ aes_gf_mul(a2, 14) ^ aes_gf_mul(a3, 11));
            s[c+3] = static_cast<uint8_t>(aes_gf_mul(a0, 11) ^ aes_gf_mul(a1, 13) ^ aes_gf_mul(a2, 9)  ^ aes_gf_mul(a3, 14));
        }
    }
}
static inline void aes_round(uint8_t out[16], const uint8_t state[16], const uint8_t key[16], bool dec, bool last) {
    static constexpr uint8_t sr[16]  = {0,5,10,15, 4,9,14,3, 8,13,2,7, 12,1,6,11};
    static constexpr uint8_t isr[16] = {0,13,10,7, 4,1,14,11, 8,5,2,15, 12,9,6,3};
    for (size_t i = 0; i < 16; ++i) out[i] = dec ? aes_inv_sbox(state[isr[i]]) : aes_sbox(state[sr[i]]);
    if (!last) aes_mix_columns(out, dec);
    for (size_t i = 0; i < 16; ++i) out[i] ^= key[i];
}
static inline void aes_keygen(uint8_t out[16], const uint8_t src[16], uint8_t rcon) {
    auto subw = [](const uint8_t p[4]) -> uint32_t {
        return uint32_t(aes_sbox(p[0])) | (uint32_t(aes_sbox(p[1])) << 8)
             | (uint32_t(aes_sbox(p[2])) << 16) | (uint32_t(aes_sbox(p[3])) << 24);
    };
    const uint32_t w1 = subw(src + 4), w3 = subw(src + 12);
    const uint32_t r[4] = {w1, ((w1 >> 8) | (w1 << 24)) ^ rcon, w3, ((w3 >> 8) | (w3 << 24)) ^ rcon};
    memcpy(out, r, 16);
}
static inline void pclmul_x86(uint8_t out[16], const uint8_t lhs[16], const uint8_t rhs[16], uint8_t imm) {
    uint64_t a, b, lo = 0, hi = 0;
    memcpy(&a, lhs + ((imm & 0x01u) ? 8 : 0), 8);
    memcpy(&b, rhs + ((imm & 0x10u) ? 8 : 0), 8);
    for (unsigned k = 0; k < 64u; ++k) {
        if ((b >> k) & 1ull) { lo ^= (a << k); if (k > 0u) hi ^= (a >> (64u - k)); }
    }
    memcpy(out, &lo, 8); memcpy(out + 8, &hi, 8);
}
template<class F, class U> static F quiet_nan(F x) {
    U bits; memcpy(&bits, &x, sizeof bits);
    bits |= (sizeof(U) == 4) ? static_cast<U>(0x00400000u) : static_cast<U>(0x0008000000000000ull);
    memcpy(&x, &bits, sizeof bits);
    return x;
}
template<class F, class U> static F fma_x86(F s0, F s1, F s2, int order, bool neg_p, bool neg_c) {
    const F a = order == 132 ? s0 : s1;
    const F b = order == 213 ? s0 : s2;
    const F c = order == 231 ? s0 : (order == 132 ? s1 : s2);
    // NaN priority follows the arithmetic operand order, before any sign changes.
    if (std::isnan(a)) return quiet_nan<F, U>(a);
    if (std::isnan(b)) return quiet_nan<F, U>(b);
    if (std::isnan(c)) return quiet_nan<F, U>(c);
    return std::fma(neg_p ? -a : a, b, neg_c ? -c : c);
}
struct PcmpResult {
    uint16_t mask;
    uint32_t index;
    bool carry;
    bool zero;
    bool overflow;
    bool sign;
};
static inline uint32_t pcmp_explicit_len(int32_t raw, uint32_t limit) {
    const uint32_t mag = raw < 0 ? (0u - static_cast<uint32_t>(raw)) : static_cast<uint32_t>(raw);
    return mag < limit ? mag : limit;
}
static inline int32_t pcmp_elem(const uint8_t p[16], uint32_t idx, bool is_word, bool is_signed) {
    if (is_word) {
        uint16_t u;
        memcpy(&u, p + idx * 2u, 2);
        return is_signed ? int32_t(static_cast<int16_t>(u)) : int32_t(u);
    }
    return is_signed ? int32_t(static_cast<int8_t>(p[idx])) : int32_t(p[idx]);
}
static inline PcmpResult pcmp_str(const uint8_t a[16], const uint8_t b[16],
                                  uint8_t imm, bool is_explicit, int32_t raw_la, int32_t raw_lb) {
    const bool is_word = (imm & 1u) != 0;
    const bool is_signed = (imm & 2u) != 0;
    const uint32_t limit = is_word ? 8u : 16u;
    const uint32_t agg = (imm >> 2) & 3u;
    const uint32_t pol = (imm >> 4) & 3u;
    const bool high_sel = (imm & 0x40u) != 0;
    uint32_t len1 = 0, len2 = 0;
    if (is_explicit) {
        len1 = pcmp_explicit_len(raw_la, limit);
        len2 = pcmp_explicit_len(raw_lb, limit);
    } else {
        while (len1 < limit && pcmp_elem(a, len1, is_word, is_signed) != 0) ++len1;
        while (len2 < limit && pcmp_elem(b, len2, is_word, is_signed) != 0) ++len2;
    }
    uint16_t int_res1 = 0;
    for (uint32_t i = 0; i < limit; ++i) {
        bool bit = false;
        if (agg == 0u) {
            if (i < len2) {
                const int32_t bv = pcmp_elem(b, i, is_word, is_signed);
                for (uint32_t j = 0; j < len1; ++j) {
                    if (pcmp_elem(a, j, is_word, is_signed) == bv) { bit = true; break; }
                }
            }
        } else if (agg == 1u) {
            if (i < len2) {
                const int32_t bv = pcmp_elem(b, i, is_word, is_signed);
                for (uint32_t j = 0; j + 1u < len1; j += 2u) {
                    if (bv >= pcmp_elem(a, j, is_word, is_signed) &&
                        bv <= pcmp_elem(a, j + 1u, is_word, is_signed)) { bit = true; break; }
                }
            }
        } else if (agg == 2u) {
            if (i < len1 && i < len2) bit = pcmp_elem(a, i, is_word, is_signed) == pcmp_elem(b, i, is_word, is_signed);
            else if (i >= len1 && i >= len2) bit = true;
        } else {
            bit = true;
            for (uint32_t j = 0; j < len1 && i + j < limit; ++j) {
                if (i + j >= len2 || pcmp_elem(a, j, is_word, is_signed) != pcmp_elem(b, i + j, is_word, is_signed)) {
                    bit = false;
                    break;
                }
            }
        }
        if (bit) int_res1 = static_cast<uint16_t>(int_res1 | (1u << i));
    }
    const uint16_t all_mask = static_cast<uint16_t>((1u << limit) - 1u);
    uint16_t int_res2 = int_res1;
    if (pol == 1u) int_res2 = static_cast<uint16_t>(int_res1 ^ all_mask);
    else if (pol == 3u) int_res2 = static_cast<uint16_t>(int_res1 ^ static_cast<uint16_t>((1u << len2) - 1u));
    uint32_t index = limit;
    if (int_res2 != 0u) {
        if (!high_sel) { index = 0u; while (((int_res2 >> index) & 1u) == 0u) ++index; }
        else { index = limit - 1u; while (((int_res2 >> index) & 1u) == 0u) --index; }
    }
    return {int_res2, index, int_res2 != 0u, len2 < limit, (int_res2 & 1u) != 0u, len1 < limit};
}
static inline void pcmp_write_mask(uint8_t out[16], uint16_t mask, uint8_t imm) {
    memset(out, 0, 16);
    if ((imm & 0x40u) == 0u) {
        memcpy(out, &mask, 2);
    } else if ((imm & 1u) == 0u) {
        for (uint32_t k = 0; k < 16u; ++k) out[k] = ((mask >> k) & 1u) ? 0xffu : 0x00u;
    } else {
        for (uint32_t k = 0; k < 8u; ++k) {
            const uint16_t v = ((mask >> k) & 1u) ? 0xffffu : 0x0000u;
            memcpy(out + 2u * k, &v, 2);
        }
    }
}
"""
HEADER = INTEGER_HEADER + "struct Query { word type, next, effect, device, count, ids, names; };\n"


def reg(number):
    return REGS[RegisterExt.full_register(number)]


def region(pe, start, end):
    ins = list(Decoder(64, image_bytes(pe, start, end-start, executable=True), ip=start))
    if not ins or ins[-1].next_ip != end or any(i.is_invalid for i in ins):
        raise ValueError("query interval does not end on an instruction boundary")
    return ins


def string_op(i):
    """One element per iteration in program order, so overlapping copies replicate as on the hardware."""
    kind, size = STRING_OPS[i.code]
    if any(i.op_kind(n) not in (K.REGISTER, K.MEMORY_ESRDI, K.MEMORY_SEG_RSI) for n in range(i.op_count)):
        # A 67h prefix walks ESI, EDI and ECX instead of the full registers.
        raise ValueError(f"unsupported string address size at {i.ip:#x}: {i}")
    if kind in ("scas", "cmps"):
        repeat = "repe" if i.has_repe_prefix else "repne" if i.has_repne_prefix else None
    elif i.has_repne_prefix:
        raise ValueError(f"unsupported REPNE string prefix at {i.ip:#x}: {i}")
    else:
        repeat = "rep" if i.has_rep_prefix else None
    t = UINT[size]
    # Each element read is a guest read (load_always), performed in each iteration even when its value is unused.
    if kind == "movs":
        step = f"store<{t}>(rdi, load_always<{t}>(rsi)); rsi += step; rdi += step;"
    elif kind == "stos":
        step = f"store<{t}>(rdi, static_cast<{t}>(rax)); rdi += step;"
    elif kind == "lods":
        # AL and AX merge into RAX; a 32-bit load zero-extends.
        value = f"(rax & ~{(1 << (8*size)) - 1}ull) | load_always<{t}>(rsi)" if size < 4 else f"load_always<{t}>(rsi)"
        step = f"rax = {value}; rsi += step;"
    else:
        lhs = f"static_cast<{t}>(rax)" if kind == "scas" else f"load_always<{t}>(rsi)"
        advance = "rdi += step;" if kind == "scas" else "rsi += step; rdi += step;"
        step = (f"{{ const {t} a = {lhs}, b = load_always<{t}>(rdi); zero = a == b; carry = a < b; "
                f"overflow = (((a ^ b) & (a ^ static_cast<{t}>(a - b))) >> {size * 8 - 1}) != 0; "
                f"sign = ((static_cast<{t}>(a - b) >> {size * 8 - 1}) & 1) != 0; }} {advance}")
    stop = {"repe": " if (!zero) break;", "repne": " if (zero) break;"}.get(repeat, "")
    # A zero count performs no iteration and leaves the pointers and flags unchanged.
    body = f"while (rcx != 0) {{ {step} --rcx;{stop} }}" if repeat else step
    return f"{{ const word step = direction ? 0ull - {size}ull : {size}ull; {body} }}"


# The host-MXCSR profile's refusal statuses: a requested word with a reserved bit (16-31), with one of the six
# exception masks (bits 7-12) clear, or with FTZ (bit 15) or DAZ (bit 6), which the first profile leaves out.
MXCSR_STATUS = {"reserved": 1, "unmasked": 2, "unsupported": 3}
# A file that lowers with the host-MXCSR profile observes the floating-point environment.
MXCSR_HEADER = "#pragma fenv_access(on)\n"


class Mxcsr:
    """The host-MXCSR profile's error channel. reject is a format string with a {status} field: C++ that leaves
    the lowered function and hands the status to the caller, as for x87_lower.Checked."""

    def __init__(self, reject):
        self.reject = reject

    def leave(self, name):
        return self.reject.format(status=MXCSR_STATUS[name])


def mxcsr_entry(name, inner):
    """The exported boundary of a function lowered with the host-MXCSR profile. inner(a, b, c, state, &status) is
    the lowered body. The boundary saves the caller's whole MXCSR and restores it on a normal return, after a
    refusal and when a structured exception such as a guest memory fault leaves inner. <name>_status() returns the
    refusal status of the calling thread's last call, 0 when none."""
    return (f"static thread_local int {name}_last_status = 0;\n"
            f'extern "C" __declspec(dllexport) int {name}_status() {{ return {name}_last_status; }}\n'
            f'extern "C" __declspec(dllexport) word {name}(word a, word b, word c, word state) {{\n'
            f"    const unsigned int caller_mxcsr = _mm_getcsr(); word result = 0; int status = 0;\n"
            f"    __try {{ result = {inner}(a, b, c, state, &status); }} __finally {{\n"
            f"        _mm_setcsr(caller_mxcsr);\n"
            f"        {name}_last_status = status;\n"
            f"    }}\n"
            f"    return result;\n}}\n")


# LAHF loads SF, ZF, AF, PF and CF into AH. The lowering keeps AF and PF only where a LAHF reads them, and only
# from the writers that define both from their operands and result.
LAHF_FLAGS = F.SF | F.ZF | F.AF | F.PF | F.CF
LAHF_WRITERS = frozenset({M.ADD, M.SUB, M.CMP})


def lahf_sources(body):
    """The ips of the instructions whose flags a LAHF of body reads. Each LAHF must follow, in the same
    straight-line block, a register ADD, SUB or CMP (register or immediate source) that is the last writer of
    any status flag before it. A function entry, a branch target, a branch, a call or any other flag writer
    between the two is refused, so every flag LAHF reads is defined by that writer at the use."""
    if not any(i.mnemonic == M.LAHF for i in body):
        return set()
    targets = {i.near_branch_target for i in body
               if i.flow_control in (FlowControl.CONDITIONAL_BRANCH, FlowControl.UNCONDITIONAL_BRANCH)}
    indirect = any(i.flow_control == FlowControl.INDIRECT_BRANCH for i in body)
    sources = set()
    for k, i in enumerate(body):
        if i.mnemonic != M.LAHF:
            continue
        where = f"unsupported query operation at {i.ip:#x}: {i}"
        if indirect:
            raise ValueError(f"{where} in a function with an indirect branch, whose joins are not known")
        if i.ip in targets:
            raise ValueError(f"{where} at a branch target, where the flags come from more than one path")
        for j in range(k - 1, -1, -1):
            prev = body[j]
            if prev.next_ip != body[j + 1].ip:
                raise ValueError(f"{where} after a gap in the function body at {prev.ip:#x}")
            if prev.flow_control != FlowControl.NEXT:
                raise ValueError(f"{where} reads flags across {prev} at {prev.ip:#x}")
            if prev.rflags_modified & LAHF_FLAGS:
                if (prev.mnemonic in LAHF_WRITERS and prev.op0_kind == K.REGISTER
                        and prev.op1_kind in (K.REGISTER, K.IMMEDIATE8, K.IMMEDIATE16, K.IMMEDIATE32,
                                              K.IMMEDIATE8TO16, K.IMMEDIATE8TO32, K.IMMEDIATE8TO64,
                                              K.IMMEDIATE32TO64)):
                    sources.add(prev.ip)
                    break
                raise ValueError(f"{where} reads AF and PF after {prev} at {prev.ip:#x}; only a register ADD, "
                                 "SUB or CMP defines them for LAHF")
            if prev.ip in targets:
                raise ValueError(f"{where} reads flags across the branch target {prev.ip:#x}")
        else:
            raise ValueError(f"{where} reads flags that no instruction of its block defines")
    return sources


def lower(body, initial, globals_, calls, exits, *, host_ops=None, x87=False, host_entropy=False,
          host_cpu=False, host_mxcsr=None):
    """Emit only query integer operations. All external edges and calls need named bindings."""
    lines = [f"word frame[{FRAME_WORDS}]{{}};", "word " + ", ".join(r + "=0" for r in REGS.values()) + ";",
             "bool zero=false, carry=false, overflow=false, sign=false;", "rsp = reinterpret_cast<word>(frame);", *initial]
    allowed = {i.ip for i in body} | set(exits)
    # The x87 profile checks every x87 instruction of the body before any of it is lowered. x87 is True for
    # the fixed control word, or an x87_lower.Checked for the checked entry, which also accepts FLDCW.
    checked = x87 if isinstance(x87, x87_lower.Checked) else None
    x87_depth = x87_lower.depths(body, exits, checked) if x87 and any(map(x87_lower.is_x87, body)) else None
    # JP, JNP, FCMOVU and FCMOVNU read PF, which only a function the x87 profile has checked models
    # (x87_lower.check_flags); SAHF, the FCOMI family and the 8-bit TEST and AND set it there. Other functions
    # keep their two flags.
    parity = x87_depth is not None and any(i.mnemonic in (M.JP, M.JNP, M.FCMOVU, M.FCMOVNU) for i in body)
    if parity:
        lines[2] = "bool zero=false, carry=false, overflow=false, sign=false, parity=false;"
    # A register ADD, SUB or CMP that a LAHF reads also assigns AF and PF (lahf_sources).
    lahf_from = lahf_sources(body)
    if lahf_from:
        lines[2] = lines[2][:-1] + ("" if parity else ", parity=false") + ", aux=false;"
    # FNSTSW is the only reader of the x87 status word (x87_lower.statement).
    status_word = x87_depth is not None and any(i.mnemonic == M.FNSTSW for i in body)
    xmm = {R.XMM0: "xmm0", R.XMM1: "xmm1", R.XMM2: "xmm2"}
    sse2_int_ops = {
        M.PADDB, M.PADDW, M.PADDD, M.PADDQ, M.PSUBB, M.PSUBW, M.PSUBD, M.PSUBQ,
        M.PAND, M.PANDN, M.POR, M.PXOR,
        M.PCMPEQB, M.PCMPEQW, M.PCMPEQD, M.PCMPGTB, M.PCMPGTW, M.PCMPGTD,
    }
    # Unaligned YMM copies and VEX zeroing in the descriptor builder need
    # only byte storage on the desktop host.
    avx_shuffle_ops = {
        M.VBROADCASTSS, M.VBROADCASTSD, M.VBROADCASTF128,
        M.VEXTRACTF128, M.VINSERTF128, M.VPERM2F128, M.VPERMILPS, M.VPERMILPD,
        M.VSHUFPS, M.VSHUFPD, M.VUNPCKLPS, M.VUNPCKHPS, M.VUNPCKLPD, M.VUNPCKHPD,
        M.VMOVDDUP, M.VMOVSLDUP, M.VMOVSHDUP,
        M.VBLENDPS, M.VBLENDPD, M.VBLENDVPS, M.VBLENDVPD,
        M.VDPPS, M.VDPPD, M.VROUNDPS, M.VROUNDPD, M.VZEROALL,
    }
    avx_existing_xmm_shuffle = {M.VUNPCKLPS, M.VUNPCKHPS, M.VUNPCKLPD, M.VUNPCKHPD,
                                M.VSHUFPS, M.VSHUFPD}
    has_avx_shuffle = any(i.mnemonic in avx_shuffle_ops for i in body)
    wide = {R.YMM0: "wide0", R.YMM1: "wide1", R.YMM2: "wide2"}
    wide_xmm = {R.XMM0: "wide0", R.XMM1: "wide1", R.XMM2: "wide2"}
    sha_ops = {M.SHA1MSG1, M.SHA1MSG2, M.SHA1NEXTE, M.SHA1RNDS4, M.SHA256MSG1, M.SHA256MSG2, M.SHA256RNDS2}
    if has_avx_shuffle:
        wide[R.YMM3] = wide_xmm[R.XMM3] = "wide3"
    wide_any = {**wide, **wide_xmm}
    avx2_wide_ops = (M.VPBROADCASTB, M.VPBROADCASTW, M.VPBROADCASTD, M.VPBROADCASTQ,
                     M.VPSLLVD, M.VPSRLVD, M.VPSRAVD, M.VPSLLVQ, M.VPSRLVQ)
    pmov_ext = {
        getattr(M, f"PMOV{k.upper()}{p.upper()}"): (k == "sx", {"b": 1, "w": 2, "d": 4}[p[0]], {"w": 2, "d": 4, "q": 8}[p[1]])
        for k in ("sx", "zx") for p in ("bw", "bd", "bq", "wd", "wq", "dq")
    }
    sse41_ops = (M.PMINSB, M.PMAXSB, M.PMINUW, M.PMAXUW, M.PMINSD, M.PMAXSD,
                 M.PMINUD, M.PMAXUD, M.PMULLD, M.PMULDQ, M.PCMPEQQ, M.PACKUSDW, M.PBLENDW, M.PBLENDVB)
    sse2_sat_mul_ops = {
        M.PADDSB, M.PADDSW, M.PADDUSB, M.PADDUSW, M.PSUBSB, M.PSUBSW, M.PSUBUSB, M.PSUBUSW,
        M.PMULHW, M.PMULHUW, M.PMULLW, M.PMADDWD, M.PMULUDQ,
        M.PMINUB, M.PMAXUB, M.PMINSW, M.PMAXSW, M.PAVGB, M.PAVGW, M.PSADBW,
    }
    crypto_legacy_ops = {M.AESENC, M.AESENCLAST, M.AESDEC, M.AESDECLAST, M.AESIMC, M.AESKEYGENASSIST, M.PCLMULQDQ}
    crypto_vex_ops = {M.VAESENC, M.VAESENCLAST, M.VAESDEC, M.VAESDECLAST, M.VAESIMC, M.VAESKEYGENASSIST, M.VPCLMULQDQ}
    fma_scalar = {
        getattr(M, f"VF{k.upper()}{o}{s.upper()}"): (k, o, s == "sd")
        for k in ("madd", "msub", "nmadd", "nmsub") for o in ("132", "213", "231") for s in ("ss", "sd")
    }
    has_wide = any(i.mnemonic in (M.VMOVUPS, M.MOVDQU, *avx2_wide_ops, *sse41_ops) or i.mnemonic in sha_ops or i.mnemonic in pmov_ext or i.mnemonic in sse2_sat_mul_ops or i.mnemonic in crypto_legacy_ops or i.mnemonic in crypto_vex_ops or i.mnemonic in fma_scalar for i in body)
    sse41_42_misc_ops = {
        M.PEXTRB, M.PEXTRW, M.PEXTRD, M.PEXTRQ,
        M.PINSRB, M.PINSRW, M.PINSRD, M.PINSRQ,
        M.PMADDUBSW, M.PMULHRSW, M.PHMINPOSUW, M.PTEST, M.MPSADBW, M.PCMPGTQ,
        M.PCMPESTRI, M.PCMPESTRM, M.PCMPISTRI, M.PCMPISTRM,
    }
    has_wide |= any(i.mnemonic in sse41_42_misc_ops for i in body)
    ssse3_ops = {
        M.PABSB, M.PABSW, M.PABSD, M.PSIGNB, M.PSIGNW, M.PSIGND, M.PSHUFB,
        M.PALIGNR, M.PHADDW, M.PHADDD, M.PHSUBW, M.PHSUBD, M.PHADDSW, M.PHSUBSW,
    }
    has_wide |= any(i.mnemonic in ssse3_ops for i in body)
    sse2_shift_pack_ops = {
        M.PSLLW, M.PSLLD, M.PSLLQ, M.PSRLW, M.PSRLD, M.PSRLQ, M.PSRAW, M.PSRAD, M.PSLLDQ, M.PSRLDQ,
        M.PACKSSWB, M.PACKSSDW, M.PACKUSWB,
        M.PUNPCKLBW, M.PUNPCKLWD, M.PUNPCKLDQ, M.PUNPCKLQDQ,
        M.PUNPCKHBW, M.PUNPCKHWD, M.PUNPCKHDQ, M.PUNPCKHQDQ,
        M.PSHUFD, M.PSHUFLW, M.PSHUFHW, M.PMOVMSKB,
    }
    has_wide |= any(i.mnemonic in sse2_shift_pack_ops for i in body)
    fma_packed = {
        getattr(M, f"VF{k.upper()}{o}{s.upper()}"): (k, o, s == "pd")
        for k in ("madd", "msub", "nmadd", "nmsub", "maddsub", "msubadd") for o in ("132", "213", "231") for s in ("ps", "pd")
    }
    has_wide |= any(i.mnemonic in fma_packed for i in body)
    sse34_fp_ops = {
        M.ADDSUBPS, M.ADDSUBPD, M.HADDPS, M.HADDPD, M.HSUBPS, M.HSUBPD,
        M.MOVDDUP, M.MOVSLDUP, M.MOVSHDUP, M.LDDQU,
        M.ROUNDPS, M.ROUNDPD, M.ROUNDSS, M.ROUNDSD,
        M.DPPS, M.DPPD, M.BLENDPS, M.BLENDPD, M.BLENDVPS, M.BLENDVPD,
        M.INSERTPS, M.EXTRACTPS,
    }
    has_wide |= any(i.mnemonic in sse34_fp_ops for i in body)
    shuffle_ops = {
        M.SHUFPS, M.UNPCKHPS, M.UNPCKLPS, M.MOVHLPS, M.MOVLHPS, M.MOVMSKPS, M.MOVUPS, M.MOVAPS,
        M.RCPPS, M.RCPSS, M.RSQRTPS, M.RSQRTSS,
        M.SHUFPD, M.UNPCKHPD, M.UNPCKLPD, M.MOVMSKPD, M.MOVUPD, M.MOVAPD, M.MOVDQA,
        M.VSHUFPS, M.VUNPCKHPS, M.VUNPCKLPS, M.VSHUFPD, M.VUNPCKHPD, M.VUNPCKLPD,
        M.VPSHUFD, M.VPSHUFHW, M.VPSHUFLW, M.VMOVHLPS, M.VMOVLHPS, M.VMOVMSKPS, M.VMOVMSKPD,
        M.VMOVAPS, M.VMOVAPD, M.VMOVDQA, M.VPMOVMSKB, M.VRCPPS, M.VRCPSS, M.VRSQRTPS, M.VRSQRTSS,
        M.VPSLLDQ, M.VPUNPCKHQDQ, M.VPUNPCKLQDQ,
    }
    has_wide |= any(i.mnemonic in shuffle_ops for i in body)
    packed_float_ops = {
        M.ADDPS: ("ps", "_mm_add_ps", False), M.SUBPS: ("ps", "_mm_sub_ps", False), M.MULPS: ("ps", "_mm_mul_ps", False),
        M.DIVPS: ("ps", "_mm_div_ps", False), M.MAXPS: ("ps", "_mm_max_ps", False), M.MINPS: ("ps", "_mm_min_ps", False),
        M.ANDPS: ("ps", "_mm_and_ps", False), M.ANDNPS: ("ps", "_mm_andnot_ps", False), M.ORPS: ("ps", "_mm_or_ps", False),
        M.XORPS: ("ps", "_mm_xor_ps", False), M.SQRTPS: ("sqrt_ps", "_mm_sqrt_ps", False),
        M.MAXSS: ("ss", "_mm_max_ss", False), M.MINSS: ("ss", "_mm_min_ss", False), M.SQRTSS: ("sqrt_ss", "_mm_sqrt_ss", False),
        M.ADDPD: ("pd", "_mm_add_pd", False), M.SUBPD: ("pd", "_mm_sub_pd", False), M.MULPD: ("pd", "_mm_mul_pd", False),
        M.DIVPD: ("pd", "_mm_div_pd", False), M.MAXPD: ("pd", "_mm_max_pd", False), M.MINPD: ("pd", "_mm_min_pd", False),
        M.ANDPD: ("pd", "_mm_and_pd", False), M.ANDNPD: ("pd", "_mm_andnot_pd", False), M.ORPD: ("pd", "_mm_or_pd", False),
        M.XORPD: ("pd", "_mm_xor_pd", False), M.SQRTPD: ("sqrt_pd", "_mm_sqrt_pd", False),
        M.MAXSD: ("sd", "_mm_max_sd", False), M.MINSD: ("sd", "_mm_min_sd", False), M.SQRTSD: ("sqrt_sd", "_mm_sqrt_sd", False),
        M.VADDPS: ("ps", "_mm_add_ps", True), M.VSUBPS: ("ps", "_mm_sub_ps", True), M.VMULPS: ("ps", "_mm_mul_ps", True),
        M.VDIVPS: ("ps", "_mm_div_ps", True), M.VMAXPS: ("ps", "_mm_max_ps", True), M.VMINPS: ("ps", "_mm_min_ps", True),
        M.VANDPS: ("ps", "_mm_and_ps", True), M.VANDNPS: ("ps", "_mm_andnot_ps", True), M.VORPS: ("ps", "_mm_or_ps", True),
        M.VXORPS: ("ps", "_mm_xor_ps", True), M.VSQRTPS: ("sqrt_ps", "_mm_sqrt_ps", True),
        M.VADDPD: ("pd", "_mm_add_pd", True), M.VSUBPD: ("pd", "_mm_sub_pd", True), M.VMULPD: ("pd", "_mm_mul_pd", True),
        M.VDIVPD: ("pd", "_mm_div_pd", True), M.VMAXPD: ("pd", "_mm_max_pd", True), M.VMINPD: ("pd", "_mm_min_pd", True),
        M.VANDPD: ("pd", "_mm_and_pd", True), M.VANDNPD: ("pd", "_mm_andnot_pd", True), M.VORPD: ("pd", "_mm_or_pd", True),
        M.VXORPD: ("pd", "_mm_xor_pd", True), M.VSQRTPD: ("sqrt_pd", "_mm_sqrt_pd", True),
        M.VMAXSS: ("ss", "_mm_max_ss", True), M.VMINSS: ("ss", "_mm_min_ss", True), M.VSQRTSS: ("sqrt_ss", "_mm_sqrt_ss", True),
        M.VMAXSD: ("sd", "_mm_max_sd", True), M.VMINSD: ("sd", "_mm_min_sd", True), M.VSQRTSD: ("sqrt_sd", "_mm_sqrt_sd", True),
    }
    has_wide |= any(i.mnemonic in packed_float_ops for i in body)
    conversion_ops = {
        M.CVTDQ2PS: ("packed", "_mm_cvtepi32_ps", "__m128i", "__m128", False),
        M.VCVTDQ2PS: ("packed", "_mm_cvtepi32_ps", "__m128i", "__m128", True),
        M.CVTDQ2PD: ("packed", "_mm_cvtepi32_pd", "__m128i", "__m128d", False),
        M.VCVTDQ2PD: ("packed", "_mm_cvtepi32_pd", "__m128i", "__m128d", True),
        M.CVTPS2DQ: ("packed", "_mm_cvtps_epi32", "__m128", "__m128i", False),
        M.VCVTPS2DQ: ("packed", "_mm_cvtps_epi32", "__m128", "__m128i", True),
        M.CVTTPS2DQ: ("packed", "_mm_cvttps_epi32", "__m128", "__m128i", False),
        M.VCVTTPS2DQ: ("packed", "_mm_cvttps_epi32", "__m128", "__m128i", True),
        M.CVTPS2PD: ("packed", "_mm_cvtps_pd", "__m128", "__m128d", False),
        M.VCVTPS2PD: ("packed", "_mm_cvtps_pd", "__m128", "__m128d", True),
        M.CVTPD2PS: ("packed", "_mm_cvtpd_ps", "__m128d", "__m128", False),
        M.VCVTPD2PS: ("packed", "_mm_cvtpd_ps", "__m128d", "__m128", True),
        M.CVTPD2DQ: ("packed", "_mm_cvtpd_epi32", "__m128d", "__m128i", False),
        M.VCVTPD2DQ: ("packed", "_mm_cvtpd_epi32", "__m128d", "__m128i", True),
        M.CVTTPD2DQ: ("packed", "_mm_cvttpd_epi32", "__m128d", "__m128i", False),
        M.VCVTTPD2DQ: ("packed", "_mm_cvttpd_epi32", "__m128d", "__m128i", True),
        M.VCVTPH2PS: ("packed", "_mm_cvtph_ps", "__m128i", "__m128", True),
        M.VCVTPS2PH: ("f16c_pack", "_mm_cvtps_ph", True),
        M.CVTSS2SD: ("ss2sd", False), M.VCVTSS2SD: ("ss2sd", True),
        M.CVTSD2SS: ("sd2ss", False), M.VCVTSD2SS: ("sd2ss", True),
        M.CVTSI2SS: ("si2ss", False), M.VCVTSI2SS: ("si2ss", True),
        M.CVTSI2SD: ("si2sd", False), M.VCVTSI2SD: ("si2sd", True),
        M.CVTSS2SI: ("xmm2gpr", False, False, False), M.VCVTSS2SI: ("xmm2gpr", False, False, True),
        M.CVTTSS2SI: ("xmm2gpr", True, False, False), M.VCVTTSS2SI: ("xmm2gpr", True, False, True),
        M.CVTSD2SI: ("xmm2gpr", False, True, False), M.VCVTSD2SI: ("xmm2gpr", False, True, True),
        M.CVTTSD2SI: ("xmm2gpr", True, True, False), M.VCVTTSD2SI: ("xmm2gpr", True, True, True),
    }
    has_wide |= any(i.mnemonic in conversion_ops for i in body)
    has_wide |= any(i.mnemonic == M.VMOVDQU and
                    (i.op0_register in wide or i.op1_register in wide) for i in body)
    avx_int_ops = {
        M.VPADDB, M.VPADDW, M.VPADDQ, M.VPSUBB, M.VPSUBW, M.VPSUBD, M.VPSUBQ,
        M.VPAND, M.VPANDN, M.VPOR,
        M.VPCMPEQB, M.VPCMPEQW, M.VPCMPEQD, M.VPCMPEQQ,
        M.VPCMPGTB, M.VPCMPGTW, M.VPCMPGTD, M.VPCMPGTQ,
        M.VPABSB, M.VPABSW, M.VPABSD, M.VPSIGNB, M.VPSIGNW, M.VPSIGND, M.VPSHUFB, M.VPALIGNR,
    }
    has_wide |= any(i.mnemonic in avx_int_ops for i in body)
    avx_minmax_mul_ops = {
        M.VPADDSB, M.VPADDSW, M.VPADDUSB, M.VPADDUSW, M.VPSUBSB, M.VPSUBSW, M.VPSUBUSB, M.VPSUBUSW,
        M.VPMINSB, M.VPMAXSB, M.VPMINUB, M.VPMAXUB, M.VPMINSW, M.VPMAXSW, M.VPMINUW, M.VPMAXUW,
        M.VPMINSD, M.VPMAXSD, M.VPMINUD, M.VPMAXUD,
        M.VPAVGB, M.VPAVGW, M.VPMULHW, M.VPMULHUW, M.VPMULLW, M.VPMULLD, M.VPMULDQ, M.VPMULUDQ,
        M.VPMULHRSW, M.VPMADDWD, M.VPMADDUBSW, M.VPSADBW, M.VMPSADBW,
    }
    has_wide |= any(i.mnemonic in avx_minmax_mul_ops for i in body)
    avx_extend_specs = {
        M.VPMOVSXBW: (1, 2, True), M.VPMOVSXBD: (1, 4, True), M.VPMOVSXBQ: (1, 8, True),
        M.VPMOVSXWD: (2, 4, True), M.VPMOVSXWQ: (2, 8, True), M.VPMOVSXDQ: (4, 8, True),
        M.VPMOVZXBW: (1, 2, False), M.VPMOVZXBD: (1, 4, False), M.VPMOVZXBQ: (1, 8, False),
        M.VPMOVZXWD: (2, 4, False), M.VPMOVZXWQ: (2, 8, False), M.VPMOVZXDQ: (4, 8, False),
    }
    avx_ext_hadd_ops = avx_extend_specs.keys() | {
        M.VPHADDW, M.VPHADDD, M.VPHADDSW, M.VPHSUBW, M.VPHSUBD, M.VPHSUBSW, M.VPHMINPOSUW,
    }
    has_wide |= any(i.mnemonic in avx_ext_hadd_ops for i in body)
    avx_horiz_fp_ops = {
        M.VHADDPS: ("__m128", "_mm_hadd_ps"),
        M.VHADDPD: ("__m128d", "_mm_hadd_pd"),
        M.VHSUBPS: ("__m128", "_mm_hsub_ps"),
        M.VHSUBPD: ("__m128d", "_mm_hsub_pd"),
        M.VADDSUBPS: ("__m128", "_mm_addsub_ps"),
        M.VADDSUBPD: ("__m128d", "_mm_addsub_pd"),
    }
    has_wide |= any(i.mnemonic in avx_horiz_fp_ops for i in body)

    def lower_avx_horiz_fp(insn, vec_reader):
        """Lower VEX-encoded 128/256-bit SSE3 FP add/sub (horizontal and alternating) using 128-bit intrinsics per lane."""
        dest = insn.op0_register
        valid_regs = wide if dest in wide else wide_xmm
        nbytes = 32 if dest in wide else 16
        if (insn.encoding != EncodingKind.VEX or insn.op_count != 3
                or insn.op0_kind != K.REGISTER or dest not in valid_regs
                or insn.op1_kind != K.REGISTER or insn.op1_register not in valid_regs
                or insn.op2_kind not in (K.REGISTER, K.MEMORY)
                or (insn.op2_kind == K.REGISTER and insn.op2_register not in valid_regs)
                or (insn.op2_kind == K.MEMORY and MemorySizeExt.size(insn.memory_size) != nbytes)):
            raise ValueError(f"unsupported AVX SSE3 FP operation at {insn.ip:#x}: {insn}")
        vtype, fn = avx_horiz_fp_ops[insn.mnemonic]
        dst, src1 = wide_any[dest], wide_any[insn.op1_register]
        rhs = wide_any[insn.op2_register] if insn.op2_kind == K.REGISTER else vec_reader(nbytes)
        zero_hi = f" memset({dst}.data() + 16, 0, 16);" if nbytes == 16 else ""
        return (f"{{ const auto lhs = {src1}; const auto rhs = {rhs}; "
                f"for (size_t l = 0; l < {nbytes}; l += 16) {{ "
                f"{vtype} a, b; memcpy(&a, lhs.data() + l, 16); memcpy(&b, rhs.data() + l, 16); "
                f"const {vtype} r = {fn}(a, b); memcpy({dst}.data() + l, &r, 16); }}{zero_hi} }}")

    avx_shift_pack_ops = {
        M.VPSLLW, M.VPSLLD, M.VPSLLQ, M.VPSRLW, M.VPSRLQ, M.VPSRAW, M.VPSRAD, M.VPSLLDQ,
        M.VPACKSSWB, M.VPACKSSDW, M.VPACKUSWB, M.VPACKUSDW,
        M.VPUNPCKLBW, M.VPUNPCKLWD, M.VPUNPCKLDQ, M.VPUNPCKLQDQ,
        M.VPUNPCKHBW, M.VPUNPCKHWD, M.VPUNPCKHDQ, M.VPUNPCKHQDQ,
        M.VPSHUFD, M.VPSHUFLW, M.VPSHUFHW, M.VPBLENDW, M.VPBLENDVB,
        M.VPMOVMSKB, M.VPEXTRB, M.VPEXTRW, M.VPEXTRD, M.VPEXTRQ,
        M.VPINSRB, M.VPINSRW, M.VPINSRD, M.VPINSRQ,
    }
    has_wide |= any(i.mnemonic in avx_shift_pack_ops for i in body)
    existing_packed_xmm_ops = {
        M.VPMOVMSKB, M.VPSHUFD, M.VPSHUFLW, M.VPSHUFHW, M.VPSLLDQ,
        M.VPUNPCKLQDQ, M.VPUNPCKHQDQ,
    }
    has_wide |= has_avx_shuffle
    avx_compare = {M.VCMPPS: "ps", M.VCMPPD: "pd", M.VCMPSS: "ss", M.VCMPSD: "sd"}
    legacy_packed_compare = {M.CMPPS: ("ps", "__m128"), M.CMPPD: ("pd", "__m128d")}
    legacy_half_move = {M.MOVLPS: 0, M.MOVLPD: 0, M.MOVHPS: 8, M.MOVHPD: 8}
    has_wide |= any(i.mnemonic in legacy_packed_compare or i.mnemonic in legacy_half_move for i in body)
    avx_test = {M.VPTEST: 1, M.VTESTPS: 4, M.VTESTPD: 8}
    avx_wide_conversion = {
        M.VCVTDQ2PS: ("cvtepi32_ps", "__m256i", "__m256", 32, 32),
        M.VCVTDQ2PD: ("cvtepi32_pd", "__m128i", "__m256d", 16, 32),
        M.VCVTPS2DQ: ("cvtps_epi32", "__m256", "__m256i", 32, 32),
        M.VCVTTPS2DQ: ("cvttps_epi32", "__m256", "__m256i", 32, 32),
        M.VCVTPD2DQ: ("cvtpd_epi32", "__m256d", "__m128i", 32, 16),
        M.VCVTTPD2DQ: ("cvttpd_epi32", "__m256d", "__m128i", 32, 16),
        M.VCVTPS2PD: ("cvtps_pd", "__m128", "__m256d", 16, 32),
        M.VCVTPD2PS: ("cvtpd_ps", "__m256d", "__m128", 32, 16),
    }
    has_wide |= any(i.mnemonic in avx_compare or i.mnemonic in avx_test or i.mnemonic in (M.VLDDQU, M.VMOVAPS, M.VMOVAPD, M.VMOVDQA) for i in body)
    avx_scalar_fp = {
        M.VADDSS: ("ss", "_mm_add_ss"), M.VADDSD: ("sd", "_mm_add_sd"),
        M.VSUBSS: ("ss", "_mm_sub_ss"), M.VSUBSD: ("sd", "_mm_sub_sd"),
        M.VMULSS: ("ss", "_mm_mul_ss"), M.VMULSD: ("sd", "_mm_mul_sd"),
        M.VDIVSS: ("ss", "_mm_div_ss"), M.VDIVSD: ("sd", "_mm_div_sd"),
        M.VSQRTSS: ("ss", "_mm_sqrt_ss"), M.VSQRTSD: ("sd", "_mm_sqrt_sd"),
        M.VMINSS: ("ss", "_mm_min_ss"), M.VMINSD: ("sd", "_mm_min_sd"),
        M.VMAXSS: ("ss", "_mm_max_ss"), M.VMAXSD: ("sd", "_mm_max_sd"),
        M.VROUNDSS: ("ss", "_mm_round_ss"), M.VROUNDSD: ("sd", "_mm_round_sd"),
    }
    avx_scalar_ops = {
        M.VADDSS, M.VSUBSS, M.VMULSS, M.VDIVSS, M.VSQRTSS, M.VMINSS, M.VMAXSS, M.VROUNDSS,
        M.VADDSD, M.VSUBSD, M.VMULSD, M.VDIVSD, M.VSQRTSD, M.VMINSD, M.VMAXSD, M.VROUNDSD,
        M.VMOVSS, M.VMOVHLPS, M.VMOVLHPS, M.VMOVLPS, M.VMOVHPS, M.VMOVLPD,
        M.VINSERTPS, M.VEXTRACTPS, M.VCOMISS, M.VUCOMISS, M.VCOMISD, M.VUCOMISD,
    }
    has_wide |= any(i.mnemonic in avx_scalar_ops for i in body)
    if any(i.code in STRING_OPS or i.mnemonic in (M.CLD, M.STD) for i in body):
        # The x64 software conventions expect DF clear on function exit and before calls, so it starts clear.
        lines.append("bool direction=false;")
    avx2_perm_ops = (M.VBROADCASTI128, M.VEXTRACTI128, M.VINSERTI128, M.VPBLENDD,
                     M.VPERM2I128, M.VPERMD, M.VPERMPS, M.VPERMQ, M.VPERMPD)
    has_wide |= any(i.mnemonic in avx2_perm_ops for i in body)
    has_wide |= any(i.mnemonic in sse2_int_ops for i in body)
    needs_xmm_math = any(i.mnemonic == M.VMOVD or
                        (i.mnemonic == M.VPADDD and i.op2_kind == K.REGISTER) for i in body)
    if has_wide and needs_xmm_math:
        raise ValueError("mixed vector arithmetic and wide descriptor copies are unsupported")
    has_xmm_math = not has_wide and (needs_xmm_math or any(
        i.mnemonic in (M.VMOVDQU, M.VPSRLDQ) for i in body
    ))
    if has_wide:
        lines.append("std::array<uint8_t,32> " + ", ".join(f"{name}{{}}" for name in wide.values()) + ";")
    if has_xmm_math:
        lines.append("__m128i xmm0{}, xmm1{}, xmm2{};")
    # RSP points at the private frame array for the whole body when nothing outside the host
    # replacements names RSP as a register (so no instruction moves it) and no binding mentions it.
    rsp_fixed = (not any(i.ip not in (host_ops or {}) and any(
                     i.op_kind(n) == K.REGISTER and RegisterExt.full_register(i.op_register(n)) == R.RSP
                     for n in range(i.op_count)) for i in body)
                 and not any(re.search(r"\brsp\b", text) for text in (*initial, *calls.values(), *exits.values(), *(host_ops or {}).values())))
    for index, i in enumerate(body):
        op = i.mnemonic
        existing_packed_xmm = False
        if op in avx_shift_pack_ops:
            vector_regs = [i.op_register(n) for n in range(i.op_count)
                           if i.op_kind(n) == K.REGISTER and RegisterExt.is_vector_register(i.op_register(n))]
            if i.encoding != EncodingKind.VEX or any(r not in wide_any for r in vector_regs):
                raise ValueError(f"unsupported packed shift, pack or lane operation at {i.ip:#x}: {i}")
            existing_packed_xmm = (op in existing_packed_xmm_ops and all(r in wide_xmm for r in vector_regs)
                                   and not any(i.op_kind(n) == K.MEMORY for n in range(i.op_count)))
        captured = {}  # the declaration of the local that holds the instruction's one guest read

        def in_frame():
            """The memory operand lies inside the function's own frame array, which a read cannot fault on."""
            size = MemorySizeExt.size(i.memory_size)
            return (rsp_fixed and i.memory_base == R.RSP and i.memory_index == R.NONE
                    and i.segment_prefix == R.NONE and 0 <= i.memory_displacement
                    and i.memory_displacement + size <= 8 * FRAME_WORDS)

        def guest(ctype):
            """The instruction's memory operand, read once as ctype into the local mem before the rest of the
            statement (see the end of the loop), through the volatile load: the original faults on an
            inaccessible operand even when the value is never used. A frame read stays an ordinary load."""
            if in_frame():
                return f"load<{ctype}>({address()})"
            declaration = f"const {ctype} mem = load_always<{ctype}>({address()});"
            if captured.setdefault("mem", declaration) != declaration:
                raise ValueError(f"memory operand read at two widths at {i.ip:#x}: {i}")
            return "mem"

        def vector(n):
            """guest() for an n-byte vector operand, as a byte array."""
            return guest(f"std::array<uint8_t,{n}>")

        def address():
            if i.is_ip_rel_memory_operand:
                if i.memory_base == R.EIP:
                    # The hardware adds the low 32 bits of the runtime RIP; an RVA binding cannot model that.
                    raise ValueError(f"unsupported EIP-relative operand at {i.ip:#x}: {i}")
                return globals_[i.ip_rel_memory_address]
            terms = []
            addr32 = False
            if i.memory_base != R.NONE:
                addr32 |= RegisterExt.size(i.memory_base) == 4
                terms.append(reg(i.memory_base))
            if i.memory_index != R.NONE:
                addr32 |= RegisterExt.size(i.memory_index) == 4
                terms.append(f"{reg(i.memory_index)} * {i.memory_index_scale}ull")
            terms.append(f"{i.memory_displacement}ull")
            expr = " + ".join(terms)
            return f"static_cast<uint32_t>({expr})" if addr32 else expr

        def operand(n):
            kind = i.op_kind(n)
            if kind == K.REGISTER:
                register = i.op_register(n)
                if register in HIGH_BYTE:
                    return f"static_cast<uint8_t>({reg(register)} >> 8)"
                return f"static_cast<{UINT[RegisterExt.size(register)]}>({reg(register)})"
            if kind == K.MEMORY:
                return guest(UINT[MemorySizeExt.size(i.memory_size)])
            return f"{i.immediate(n)}ull"

        def write(expr):
            if i.op0_kind == K.MEMORY:
                return f"store<{UINT[MemorySizeExt.size(i.memory_size)]}>({address()}, {expr});"
            return write_register(i.op0_register, expr)

        def write_register(dest, expr):
            if dest in HIGH_BYTE:
                return f"{reg(dest)} = ({reg(dest)} & ~0xff00ull) | (static_cast<word>(static_cast<uint8_t>({expr})) << 8);"
            size = RegisterExt.size(dest)
            value = f"static_cast<{UINT[size]}>({expr})"
            if size < 4:
                value = f"({reg(dest)} & ~{(1 << (8*size))-1}ull) | {value}"
            return f"{reg(dest)} = {value};"

        if host_ops and i.ip in host_ops:
            stmt = host_ops[i.ip]
        elif i.segment_prefix in (R.FS, R.GS) or i.has_lock_prefix:
            # FS/GS select a thread block base and LOCK makes the access atomic;
            # the flat loads and stores below would silently drop both.
            raise ValueError(f"unsupported instruction prefix at {i.ip:#x}: {i}")
        elif i.code in STRING_OPS:
            stmt = string_op(i)
        elif op == M.CLD:
            stmt = "direction = false;"
        elif op == M.STD:
            stmt = "direction = true;"
        elif op in FENCES:
            # The intrinsics also keep the compiler from moving loads and stores across them.
            stmt = FENCES[op]
        elif op in CACHE_HINTS:
            # Prefetches never fault; the flushes fault like a one-byte load of the same address.
            stmt = f"{CACHE_HINTS[op]}(reinterpret_cast<const char*>({address()}));"
        elif (op in (M.JE, M.JNE, M.JAE, M.JA, M.JB, M.JBE, M.JMP) or
              (x87_depth is None and op in (M.JL, M.JGE, M.JLE, M.JG, M.JS, M.JNS, M.JO, M.JNO))):
            target = i.near_branch_target
            if target not in allowed:
                raise ValueError(f"unbound branch at {i.ip:#x}")
            cond = {M.JE: "if (zero) ", M.JNE: "if (!zero) ", M.JAE: "if (!carry) ",
                    M.JA: "if (!carry && !zero) ", M.JB: "if (carry) ", M.JBE: "if (carry || zero) ",
                    M.JL: "if (sign != overflow) ", M.JGE: "if (sign == overflow) ",
                    M.JLE: "if (zero || (sign != overflow)) ", M.JG: "if (!zero && (sign == overflow)) ",
                    M.JS: "if (sign) ", M.JNS: "if (!sign) ",
                    M.JO: "if (overflow) ", M.JNO: "if (!overflow) ", M.JMP: ""}[op]
            stmt = f"{cond}goto block_{target:x};"
        elif op in (M.JP, M.JNP) and parity:
            target = i.near_branch_target
            if target not in allowed:
                raise ValueError(f"unbound branch at {i.ip:#x}")
            stmt = f"if ({'' if op == M.JP else '!'}parity) goto block_{target:x};"
        elif op == M.LAHF:  # AH = SF:ZF:0:AF:0:PF:1:CF; the rest of RAX is unchanged
            stmt = ("rax = (rax & ~0xff00ull) | (word(sign) << 15) | (word(zero) << 14) | (word(aux) << 12) | "
                    "(word(parity) << 10) | 0x200ull | (word(carry) << 8);")
        elif op == M.SAHF and x87_depth is not None:
            # CF, PF and ZF from bits 0, 2 and 6 of AH; SF from bit 7; AF is not modelled.
            stmt = ("{ const uint8_t h = static_cast<uint8_t>(rax >> 8); carry = (h & 1) != 0; zero = (h & 0x40) != 0; sign = (h & 0x80) != 0;"
                    + (" parity = (h & 4) != 0;" if parity else "") + " }")
        elif op in (M.LOOP, M.LOOPE, M.LOOPNE, M.JRCXZ, M.JECXZ):
            # RCX-counted branches. A 67h prefix selects ECX (loopd, jecxz). None of them changes a flag;
            # LOOPE and LOOPNE test ZF after the decrement.
            target = i.near_branch_target
            if target not in allowed:
                raise ValueError(f"unbound branch at {i.ip:#x}")
            ecx = i.code in (C.LOOP_REL8_64_ECX, C.LOOPE_REL8_64_ECX, C.LOOPNE_REL8_64_ECX, C.JECXZ_REL8_64)
            count = "static_cast<uint32_t>(rcx)" if ecx else "rcx"
            if op in (M.JRCXZ, M.JECXZ):
                stmt = f"if ({count} == 0) goto block_{target:x};"
            else:
                decrement = "rcx = static_cast<uint32_t>(rcx - 1);" if ecx else "--rcx;"
                condition = {M.LOOP: "", M.LOOPE: " && zero", M.LOOPNE: " && !zero"}[op]
                stmt = f"{decrement} if ({count} != 0{condition}) goto block_{target:x};"
        elif op in (M.MOV, M.MOVZX):
            stmt = write(operand(1))
        elif op == M.MOVSXD:
            stmt = write(f"int64_t(int32_t({operand(1)}))")
        elif op == M.MOVSX:
            src_bits = 8 * (MemorySizeExt.size(i.memory_size) if i.op1_kind == K.MEMORY else RegisterExt.size(i.op1_register))
            dst_bits = 8 * RegisterExt.size(i.op0_register)
            stmt = write(f"static_cast<int{dst_bits}_t>(static_cast<int{src_bits}_t>({operand(1)}))")
        elif (op in (M.CMOVE, M.CMOVNE, M.CMOVA, M.CMOVAE, M.CMOVB, M.CMOVBE) or
              (x87_depth is None and op in (M.CMOVL, M.CMOVGE, M.CMOVLE, M.CMOVG, M.CMOVS, M.CMOVNS, M.CMOVO, M.CMOVNO))):
            cond = {M.CMOVE: "zero", M.CMOVNE: "!zero", M.CMOVA: "!carry && !zero",
                    M.CMOVAE: "!carry", M.CMOVB: "carry", M.CMOVBE: "carry || zero",
                    M.CMOVL: "sign != overflow", M.CMOVGE: "sign == overflow",
                    M.CMOVLE: "zero || (sign != overflow)", M.CMOVG: "!zero && (sign == overflow)",
                    M.CMOVS: "sign", M.CMOVNS: "!sign",
                    M.CMOVO: "overflow", M.CMOVNO: "!overflow"}[op]
            # A memory source is read whether or not the condition holds: guest() reads it before the
            # condition is evaluated.
            stmt = write(f"({cond}) ? {operand(1)} : {operand(0)}")
        elif op == M.LEA:
            stmt = write(address())
        elif op == M.MOVBE:
            # One operand is memory and the other a register of the same width; the bytes are reversed in
            # either direction, no flag changes, and a 16-bit register destination keeps its upper bits.
            width = UINT[MemorySizeExt.size(i.memory_size)]
            stmt = (f"{{ const {width} a = {operand(1)}; {width} v = 0; "
                    f"for (unsigned k = 0; k < sizeof v; ++k) v = static_cast<{width}>((v << 8) | ((a >> (8 * k)) & 0xffu)); "
                    f"{write('v')} }}")
        elif op == M.CMP:
            # Sign-extended immediates arrive as 64-bit values; compare at the destination width.
            size = MemorySizeExt.size(i.memory_size) if i.op0_kind == K.MEMORY else RegisterExt.size(i.op0_register)
            width = UINT[size]
            stmt = (f"{{ const {width} a = {operand(0)}, b = static_cast<{width}>({operand(1)}), "
                    f"d = static_cast<{width}>(a - b); "
                    f"zero = a == b; carry = a < b; overflow = (((a ^ b) & (a ^ d)) >> {8 * size - 1}) != 0; "
                    f"sign = ((d >> {8 * size - 1}) & 1) != 0;"
                    + (f" aux = ((a ^ b ^ d) & 0x10u) != 0; {x87_lower.parity_statement('d')}" if i.ip in lahf_from else "")
                    + " }")
        elif op == M.TEST and parity and x87_lower.assigns_parity(i):
            stmt = (f"{{ const uint8_t v = static_cast<uint8_t>({operand(0)} & {operand(1)}); zero = v == 0; "
                    f"carry = false; overflow = false; sign = (v & 0x80u) != 0; {x87_lower.parity_statement('v')} }}")
        elif op == M.TEST:
            size = MemorySizeExt.size(i.memory_size) if i.op0_kind == K.MEMORY else RegisterExt.size(i.op0_register)
            stmt = (f"{{ const {UINT[size]} v = static_cast<{UINT[size]}>({operand(0)} & {operand(1)}); "
                    f"zero = v == 0; carry = false; overflow = false; sign = ((v >> {8 * size - 1}) & 1) != 0; }}")
        elif op == M.IMUL and i.op_count in (2, 3) and RegisterExt.size(i.op0_register) == 4:
            lhs, rhs = operand(i.op_count - 2), operand(i.op_count - 1)
            stmt = (f"{{ const int64_t product = int64_t(int32_t({lhs})) * int32_t({rhs}); "
                    f"carry = product != int32_t(product); overflow = carry; {write('product')} }}")
        elif op == M.CWD and x87_depth is None:
            stmt = "rdx = (rdx & ~0xffffull) | (static_cast<int16_t>(rax) < 0 ? 0xffffull : 0ull);"
        elif op == M.CDQ:
            stmt = "rdx = uint32_t(int32_t(rax) >> 31);"
        elif op == M.CBW:
            stmt = "rax = (rax & ~0xffffull) | static_cast<uint16_t>(static_cast<int16_t>(static_cast<int8_t>(rax)));"
        elif op == M.CWDE:
            stmt = "rax = static_cast<uint32_t>(static_cast<int32_t>(static_cast<int16_t>(rax)));"
        elif op == M.CDQE:
            stmt = "rax = static_cast<uint64_t>(static_cast<int64_t>(static_cast<int32_t>(rax)));"
        elif op == M.CQO:
            stmt = "rdx = static_cast<uint64_t>(static_cast<int64_t>(rax) >> 63);"
        elif op == M.RDTSC:
            # The host time-stamp counter, passed through unscaled: EDX:EAX, upper halves zeroed, flags kept.
            # The volatile result keeps the read when the guest overwrites it; without it /O2 removes the read.
            stmt = "{ const volatile uint64_t tsc = __rdtsc(); rax = static_cast<uint32_t>(tsc); rdx = tsc >> 32; }"
        elif op == M.RDTSCP:
            stmt = ("{ unsigned int aux; const volatile uint64_t tsc = __rdtscp(&aux); "
                    "rax = static_cast<uint32_t>(tsc); rdx = tsc >> 32; rcx = aux; }")
        elif op == M.CPUID and host_cpu:
            # The opt-in host-CPU profile: the host's CPUID for the guest EAX leaf and ECX subleaf, all four
            # results zero-extended, flags kept. These are host values; no guest CPU table is applied.
            stmt = ("{ int cpu_regs[4]; __cpuidex(cpu_regs, static_cast<int>(rax), static_cast<int>(rcx)); "
                    "rax = static_cast<uint32_t>(cpu_regs[0]); rbx = static_cast<uint32_t>(cpu_regs[1]); "
                    "rcx = static_cast<uint32_t>(cpu_regs[2]); rdx = static_cast<uint32_t>(cpu_regs[3]); }")
        elif op == M.LDMXCSR and host_mxcsr is not None and i.op0_kind == K.MEMORY:
            # The opt-in host-MXCSR profile: the requested word is checked before it reaches the host register,
            # so a refused word changes nothing and no later guest instruction runs.
            stmt = (f"{{ const uint32_t want = {guest('uint32_t')}; "
                    f"if (want & 0xffff0000u) {host_mxcsr.leave('reserved')} "
                    f"if ((want & 0x1f80u) != 0x1f80u) {host_mxcsr.leave('unmasked')} "
                    f"if (want & 0x8040u) {host_mxcsr.leave('unsupported')} "
                    f"_mm_setcsr(want); }}")
        elif op == M.STMXCSR and host_mxcsr is not None and i.op0_kind == K.MEMORY:
            stmt = f"store<uint32_t>({address()}, _mm_getcsr());"
        elif op == M.XGETBV and host_cpu:
            # The host's extended control register ECX into EDX:EAX, flags kept. Any ECX is passed through, but
            # the supported contract is a caller that checked OSXSAVE and reads XCR0 (ECX = 0); an invalid index
            # faults on the host as it does natively. The volatile result keeps a discarded read; without it /O2
            # removes it.
            stmt = ("{ const volatile uint64_t xcr = _xgetbv(static_cast<uint32_t>(rcx)); "
                    "rax = static_cast<uint32_t>(xcr); rdx = xcr >> 32; }")
        elif op in (M.RDRAND, M.RDSEED) and host_entropy:
            # The opt-in host-entropy profile: one host RDRAND or RDSEED per guest execution. CF is the
            # intrinsic's result; on CF=0 the destination gets zero at its width, and the guest decides whether
            # to retry. ZF and OF are cleared (PF where it is modelled; SF and AF are not). The volatile result
            # keeps the read when the guest discards it; without it /O2 removes the read.
            size = RegisterExt.size(i.op0_register)
            step = f"_{'rdrand' if op == M.RDRAND else 'rdseed'}{8 * size}_step"
            stmt = (f"{{ {UINT[size]} rnd = 0; const volatile int rnd_ok = {step}(&rnd); if (!rnd_ok) rnd = 0; "
                    f"carry = rnd_ok != 0; zero = false; overflow = false; sign = false; {'parity = false; ' if parity else ''}"
                    f"{write_register(i.op0_register, 'rnd')} }}")
        elif op in (M.SHL, M.SHR, M.SAR) and i.op1_kind == K.IMMEDIATE8 and RegisterExt.size(i.op0_register) in (4, 8):
            size = RegisterExt.size(i.op0_register)
            bits = 8 * size
            count = i.immediate(1) & (bits - 1)
            shift, last = ("<<", bits-count) if op == M.SHL else (">>", count-1)
            lhs = f"int{bits}_t(a)" if op == M.SAR else "a"
            of_update = ""
            if count == 1:
                if op == M.SHL:
                    of_update = f" overflow = (((v >> {bits - 1}) & 1) != 0) != carry;"
                elif op == M.SHR:
                    of_update = f" overflow = ((a >> {bits - 1}) & 1) != 0;"
                else:
                    of_update = " overflow = false;"
            stmt = (f"{{ const {UINT[size]} a = {operand(0)}, v = {lhs} {shift} {count}; "
                    f"carry = ((a >> {last}) & 1) != 0;{of_update} zero = v == 0; sign = ((v >> {bits - 1}) & 1) != 0; {write('v')} }}") if count else \
                write(operand(0))  # A masked zero count keeps the flags but still zero-extends a 32-bit destination.
        elif op in (M.RCL, M.RCR):
            # The 9-, 17-, 33- or 65-bit rotation through CF, one bit per step as in the SDM pseudocode.
            # Counts mask to five bits (six for 64-bit). Reducing 8- and 16-bit counts modulo 9 and 17 only
            # bounds the loop, since those rotations repeat with that period.
            # A zero step count keeps CF; ZF is never affected. Only the single-bit rotation defines OF.
            size = MemorySizeExt.size(i.memory_size) if i.op0_kind == K.MEMORY else RegisterExt.size(i.op0_register)
            width, bits = UINT[size], 8 * size
            count = f"(static_cast<unsigned>({operand(1)}) & {0x3f if size == 8 else 0x1f}u)"
            steps = f"(masked % {bits + 1}u)" if size < 4 else "masked"
            if op == M.RCL:
                step = (f"const bool out = ((v >> {bits - 1}) & 1) != 0; "
                        f"v = static_cast<{width}>((v << 1) | {width}(c));")
            else:
                step = (f"const bool out = (v & 1) != 0; "
                        f"v = static_cast<{width}>((v >> 1) | ({width}(c) << {bits - 1}));")
            high = f"((v >> {bits - 1}) & 1u)"
            other = "c" if op == M.RCL else f"((v >> {bits - 2}) & 1u)"
            stmt = (f"{{ {width} v = {operand(0)}; bool c = carry; const unsigned masked = {count}; "
                    f"for (unsigned n = {steps}; n != 0; --n) {{ {step} c = out; }} carry = c; "
                    f"if (masked == 1) overflow = {high} != {other}; {write('v')} }}")
        elif op == M.CLC:
            stmt = "carry = false;"
        elif op == M.STC:
            stmt = "carry = true;"
        elif op == M.CMC:
            stmt = "carry = !carry;"
        elif op == M.NEG and x87_depth is None:
            size = MemorySizeExt.size(i.memory_size) if i.op0_kind == K.MEMORY else RegisterExt.size(i.op0_register)
            width = UINT[size]
            sign_shift = 8 * size - 1
            stmt = (f"{{ const {width} a = {operand(0)}, v = static_cast<{width}>(0 - a); "
                    f"carry = a != 0; overflow = a == (static_cast<{width}>(1) << {sign_shift}); "
                    f"zero = v == 0; sign = ((v >> {sign_shift}) & 1) != 0; {write('v')} }}")
        elif op in (M.ADD, M.ADC, M.AND, M.OR, M.XOR, M.INC, M.DEC, M.SUB, M.SBB):
            operator = {M.ADD: "+", M.ADC: "+", M.INC: "+", M.DEC: "-", M.AND: "&", M.OR: "|", M.XOR: "^", M.SUB: "-", M.SBB: "-"}[op]
            rhs = "1" if op in (M.INC, M.DEC) else operand(1)
            size = MemorySizeExt.size(i.memory_size) if i.op0_kind == K.MEMORY else RegisterExt.size(i.op0_register)
            width = UINT[size]
            sign_shift = 8 * size - 1
            expr = f"a {operator} b" + (" + word(carry)" if op == M.ADC else " - word(carry)" if op == M.SBB else "")
            update = {
                M.ADD: f"carry = v < a; overflow = (((~(a ^ b)) & (a ^ v)) >> {sign_shift}) != 0;",
                M.ADC: f"carry = v < a || (carry && v == a); overflow = (((~(a ^ b)) & (a ^ v)) >> {sign_shift}) != 0;",
                M.SUB: f"carry = a < b; overflow = (((a ^ b) & (a ^ v)) >> {sign_shift}) != 0;",
                M.SBB: f"carry = a < b || (carry && a == b); overflow = (((a ^ b) & (a ^ v)) >> {sign_shift}) != 0;",
                M.INC: f"overflow = (((~(a ^ b)) & (a ^ v)) >> {sign_shift}) != 0;",
                M.DEC: f"overflow = (((a ^ b) & (a ^ v)) >> {sign_shift}) != 0;",
                M.XOR: "carry = false; overflow = false;",
                M.AND: "carry = false; overflow = false;",
                M.OR: "carry = false; overflow = false;",
            }[op]
            update += f" sign = ((v >> {sign_shift}) & 1) != 0;"
            if op == M.AND and parity and x87_lower.assigns_parity(i):
                update += " " + x87_lower.parity_statement("v")
            if i.ip in lahf_from:  # ADD or SUB (lahf_sources)
                update += f" aux = ((a ^ b ^ v) & 0x10u) != 0; {x87_lower.parity_statement('v')}"
            stmt = (f"{{ const {width} a = {operand(0)}, b = static_cast<{width}>({rhs}); "
                    f"const {width} v = static_cast<{width}>({expr}); {update} zero = v == 0; {write('v')} }}")
        elif op in (M.ADCX, M.ADOX) and RegisterExt.size(i.op0_register) in (4, 8):
            width = UINT[RegisterExt.size(i.op0_register)]
            flag = "carry" if op == M.ADCX else "overflow"
            stmt = (f"{{ const {width} a = {operand(0)}, b = {operand(1)}, "
                    f"v = static_cast<{width}>(a + b + static_cast<{width}>({flag})); "
                    f"{flag} = v < a || ({flag} && v == a); {write('v')} }}")
        elif op in (M.ANDN, M.BEXTR, M.BLSI, M.BLSMSK, M.BLSR, M.TZCNT) and RegisterExt.size(i.op0_register) in (4, 8):
            size = RegisterExt.size(i.op0_register)
            width, bits = UINT[size], 8 * size
            if op == M.ANDN:
                stmt = (f"{{ const {width} v = static_cast<{width}>(~static_cast<{width}>({operand(1)}) "
                        f"& static_cast<{width}>({operand(2)})); carry = false; overflow = false; zero = v == 0; "
                        f"sign = ((v >> {bits - 1}) & 1) != 0; {write('v')} }}")
            elif op == M.BEXTR:
                stmt = (f"{{ const {width} a = {operand(1)}, ctrl = {operand(2)}; "
                        f"const unsigned start = static_cast<unsigned>(ctrl & 0xffu), "
                        f"len = static_cast<unsigned>((ctrl >> 8) & 0xffu); "
                        f"{width} v = 0; if (start < {bits}u && len > 0u) {{ "
                        f"const {width} s = a >> start; "
                        f"v = len >= {bits}u ? s : static_cast<{width}>(s & ((static_cast<{width}>(1) << len) - 1)); }} "
                        f"carry = false; overflow = false; zero = v == 0; {write('v')} }}")
            elif op == M.BLSI:
                stmt = (f"{{ const {width} a = {operand(1)}, v = static_cast<{width}>(a & (static_cast<{width}>(0) - a)); "
                        f"carry = a != 0; overflow = false; zero = v == 0; sign = ((v >> {bits - 1}) & 1) != 0; {write('v')} }}")
            elif op == M.BLSMSK:
                stmt = (f"{{ const {width} a = {operand(1)}, v = static_cast<{width}>(a ^ (a - 1)); "
                        f"carry = a == 0; overflow = false; zero = false; sign = ((v >> {bits - 1}) & 1) != 0; {write('v')} }}")
            elif op == M.BLSR:
                stmt = (f"{{ const {width} a = {operand(1)}, v = static_cast<{width}>(a & (a - 1)); "
                        f"carry = a == 0; overflow = false; zero = v == 0; sign = ((v >> {bits - 1}) & 1) != 0; {write('v')} }}")
            else:  # M.TZCNT
                stmt = (f"{{ const {width} a = {operand(1)}; {width} v = 0; "
                        f"if (a == 0) {{ v = {bits}; }} else {{ while (((a >> v) & 1) == 0) ++v; }} "
                        f"carry = a == 0; zero = v == 0; {write('v')} }}")
        elif op in (M.BSF, M.BSR, M.POPCNT, M.LZCNT) and RegisterExt.size(i.op0_register) in (4, 8):
            size = RegisterExt.size(i.op0_register)
            width, bits = UINT[size], 8 * size
            if op == M.BSF:
                stmt = (f"{{ const {width} a = {operand(1)}; zero = a == 0; if (a != 0) {{ "
                        f"{width} v = 0; while (((a >> v) & 1) == 0) ++v; {write('v')} }} }}")
            elif op == M.BSR:
                stmt = (f"{{ const {width} a = {operand(1)}; zero = a == 0; if (a != 0) {{ "
                        f"{width} v = {bits - 1}; while (((a >> v) & 1) == 0) --v; {write('v')} }} }}")
            elif op == M.POPCNT:
                stmt = (f"{{ {width} a = {operand(1)}, v = 0; while (a != 0) {{ a &= a - 1; ++v; }} "
                        f"carry = false; overflow = false; zero = v == 0; sign = false; {write('v')} }}")
            else:  # M.LZCNT
                stmt = (f"{{ const {width} a = {operand(1)}; {width} v = 0; "
                        f"if (a == 0) {{ v = {bits}; }} else {{ while (((a >> ({bits - 1} - v)) & 1) == 0) ++v; }} "
                        f"carry = a == 0; zero = v == 0; {write('v')} }}")
        elif op == M.BSWAP and RegisterExt.size(i.op0_register) in (4, 8):
            size = RegisterExt.size(i.op0_register)
            width = UINT[size]
            stmt = (f"{{ const {width} a = {operand(0)}; {width} v = 0; "
                    f"for (unsigned b = 0; b < {size}u; ++b) {{ v = (v << 8) | ((a >> (b * 8u)) & 0xffu); }} "
                    f"{write('v')} }}")
        elif op in (M.BT, M.BTS, M.BTR, M.BTC) and i.op0_kind == K.REGISTER and RegisterExt.size(i.op0_register) in (4, 8):
            size = RegisterExt.size(i.op0_register)
            width, bits = UINT[size], 8 * size
            bit_expr = f"static_cast<unsigned>({operand(1)}) & {bits - 1}u"
            if op == M.BT:
                stmt = f"{{ const {width} a = {operand(0)}; carry = ((a >> ({bit_expr})) & 1u) != 0; }}"
            else:
                mut = {M.BTS: "a | m", M.BTR: "a & static_cast<" + width + ">(~m)", M.BTC: "a ^ m"}[op]
                stmt = (f"{{ const {width} a = {operand(0)}; const unsigned b = {bit_expr}; "
                        f"const {width} m = static_cast<{width}>(1) << b; carry = (a & m) != 0; "
                        f"const {width} v = static_cast<{width}>({mut}); {write('v')} }}")

        elif (op in (M.ROL, M.ROR) and i.op0_kind == K.REGISTER and RegisterExt.size(i.op0_register) in (4, 8)
              and (i.op1_kind == K.IMMEDIATE8 or i.op1_register == R.CL)):
            size = RegisterExt.size(i.op0_register)
            width, bits = UINT[size], 8 * size
            if i.op1_kind == K.IMMEDIATE8 and (i.immediate(1) & (bits - 1)) == 0:
                stmt = write(operand(0))
            else:
                count_expr = f"{i.immediate(1) & (bits - 1)}u" if i.op1_kind == K.IMMEDIATE8 else f"static_cast<unsigned>(rcx) & {bits - 1}u"
                if op == M.ROL:
                    stmt = (f"{{ const {width} a = {operand(0)}; const unsigned c = {count_expr}; "
                            f"{width} v = a; if (c != 0u) {{ v = static_cast<{width}>((a << c) | (a >> ({bits}u - c))); "
                            f"carry = (v & 1u) != 0; if (c == 1) overflow = (v >> {bits - 1}) != carry; }} {write('v')} }}")
                else:  # M.ROR
                    stmt = (f"{{ const {width} a = {operand(0)}; const unsigned c = {count_expr}; "
                            f"{width} v = a; if (c != 0u) {{ v = static_cast<{width}>((a >> c) | (a << ({bits}u - c))); "
                            f"carry = ((v >> {bits - 1}u) & 1u) != 0; if (c == 1) overflow = carry != ((v >> {bits - 2}) & 1u); }} {write('v')} }}")
        elif (op in (M.SHLD, M.SHRD) and i.op0_kind == K.REGISTER and RegisterExt.size(i.op0_register) in (4, 8)
              and (i.op2_kind == K.IMMEDIATE8 or i.op2_register == R.CL)):
            size = RegisterExt.size(i.op0_register)
            width, bits = UINT[size], 8 * size
            if i.op2_kind == K.IMMEDIATE8 and (i.immediate(2) & (bits - 1)) == 0:
                stmt = write(operand(0))
            else:
                count_expr = f"{i.immediate(2) & (bits - 1)}u" if i.op2_kind == K.IMMEDIATE8 else f"static_cast<unsigned>(rcx) & {bits - 1}u"
                if op == M.SHLD:
                    stmt = (f"{{ const {width} a = {operand(0)}, b = {operand(1)}; const unsigned c = {count_expr}; "
                            f"{width} v = a; if (c != 0u) {{ v = static_cast<{width}>((a << c) | (b >> ({bits}u - c))); "
                            f"carry = ((a >> ({bits}u - c)) & 1u) != 0; zero = v == 0; sign = ((v >> {bits - 1}) & 1) != 0; if (c == 1) overflow = ((a ^ v) >> {bits - 1}) != 0; }} {write('v')} }}")
                else:  # M.SHRD
                    stmt = (f"{{ const {width} a = {operand(0)}, b = {operand(1)}; const unsigned c = {count_expr}; "
                            f"{width} v = a; if (c != 0u) {{ v = static_cast<{width}>((a >> c) | (b << ({bits}u - c))); "
                            f"carry = ((a >> (c - 1u)) & 1u) != 0; zero = v == 0; sign = ((v >> {bits - 1}) & 1) != 0; if (c == 1) overflow = ((a ^ v) >> {bits - 1}) != 0; }} {write('v')} }}")

        elif op in (M.BZHI, M.MULX, M.PDEP, M.PEXT, M.RORX, M.SARX, M.SHLX, M.SHRX) and RegisterExt.size(i.op0_register) in (4, 8):
            size = RegisterExt.size(i.op0_register)
            width, bits = UINT[size], 8 * size
            if op == M.BZHI:
                stmt = (f"{{ const {width} a = {operand(1)}; const unsigned n = static_cast<unsigned>({operand(2)} & 0xffu); "
                        f"const {width} v = n >= {bits}u ? a : static_cast<{width}>(a & ((static_cast<{width}>(1) << n) - 1)); "
                        f"carry = n >= {bits}u; overflow = false; zero = v == 0; sign = ((v >> {bits - 1}) & 1) != 0; {write('v')} }}")
            elif op == M.RORX:
                count = i.immediate(2) & (bits - 1)
                expr = "a" if count == 0 else f"static_cast<{width}>((a >> {count}) | (a << {bits - count}))"
                stmt = f"{{ const {width} a = {operand(1)}, v = {expr}; {write('v')} }}"
            elif op in (M.SHLX, M.SHRX, M.SARX):
                lhs = f"static_cast<int{bits}_t>(a)" if op == M.SARX else "a"
                shift = "<<" if op == M.SHLX else ">>"
                stmt = (f"{{ const {width} a = {operand(1)}; const unsigned c = static_cast<unsigned>({operand(2)}) & {bits - 1}u; "
                        f"const {width} v = static_cast<{width}>({lhs} {shift} c); {write('v')} }}")
            elif op == M.PDEP:
                stmt = (f"{{ const {width} src = {operand(1)}, mask = {operand(2)}; {width} v = 0; unsigned k = 0; "
                        f"for (unsigned b = 0; b < {bits}u; ++b) {{ if ((mask >> b) & 1u) {{ "
                        f"if ((src >> k) & 1u) v |= static_cast<{width}>(1) << b; ++k; }} }} {write('v')} }}")
            elif op == M.PEXT:
                stmt = (f"{{ const {width} src = {operand(1)}, mask = {operand(2)}; {width} v = 0; unsigned k = 0; "
                        f"for (unsigned b = 0; b < {bits}u; ++b) {{ if ((mask >> b) & 1u) {{ "
                        f"if ((src >> b) & 1u) v |= static_cast<{width}>(1) << k; ++k; }} }} {write('v')} }}")
            else:  # M.MULX
                lo_dst = reg(i.op1_register)
                if size == 4:
                    stmt = (f"{{ const uint64_t p = static_cast<uint64_t>(static_cast<uint32_t>(rdx)) * static_cast<uint32_t>({operand(2)}); "
                            f"const uint32_t lo = static_cast<uint32_t>(p), hi = static_cast<uint32_t>(p >> 32); "
                            f"{lo_dst} = lo; {write('hi')} }}")
                else:
                    stmt = (f"{{ const uint64_t a = rdx, b = {operand(2)}; "
                            f"const uint64_t a0 = static_cast<uint32_t>(a), a1 = a >> 32, b0 = static_cast<uint32_t>(b), b1 = b >> 32; "
                            f"const uint64_t p0 = a0 * b0, p1 = a0 * b1, p2 = a1 * b0, p3 = a1 * b1; "
                            f"const uint64_t mid = (p0 >> 32) + static_cast<uint32_t>(p1) + static_cast<uint32_t>(p2); "
                            f"const uint64_t lo = (p0 & 0xffffffffull) | (mid << 32), hi = p3 + (p1 >> 32) + (p2 >> 32) + (mid >> 32); "
                            f"{lo_dst} = lo; {write('hi')} }}")
        elif op in (M.MUL, M.DIV, M.IDIV):
            size = MemorySizeExt.size(i.memory_size) if i.op0_kind == K.MEMORY else RegisterExt.size(i.op0_register)
            if size not in (4, 8):
                raise ValueError(f"unsupported query operation at {i.ip:#x}: {i}")
            if op == M.MUL:
                if size == 4:
                    stmt = (f"{{ const uint64_t p = static_cast<uint64_t>(static_cast<uint32_t>(rax)) * static_cast<uint32_t>({operand(0)}); "
                            f"rax = static_cast<uint32_t>(p); rdx = static_cast<uint32_t>(p >> 32); carry = rdx != 0; overflow = carry; }}")
                else:
                    stmt = (f"{{ const uint64_t a = rax, b = {operand(0)}; "
                            f"const uint64_t a0 = static_cast<uint32_t>(a), a1 = a >> 32, b0 = static_cast<uint32_t>(b), b1 = b >> 32; "
                            f"const uint64_t p0 = a0 * b0, p1 = a0 * b1, p2 = a1 * b0, p3 = a1 * b1; "
                            f"const uint64_t mid = (p0 >> 32) + static_cast<uint32_t>(p1) + static_cast<uint32_t>(p2); "
                            f"rax = (p0 & 0xffffffffull) | (mid << 32); rdx = p3 + (p1 >> 32) + (p2 >> 32) + (mid >> 32); carry = rdx != 0; overflow = carry; }}")
            elif op == M.DIV:
                if size == 4:
                    stmt = (f"{{ const uint32_t hi = static_cast<uint32_t>(rdx), den = {operand(0)}; "
                            f"if (den == 0u || hi >= den) raise_de(den == 0); "
                            f"const uint64_t num = (static_cast<uint64_t>(hi) << 32) | static_cast<uint32_t>(rax); "
                            f"rax = static_cast<uint32_t>(num / den); rdx = static_cast<uint32_t>(num % den); }}")
                else:
                    stmt = (f"{{ uint64_t hi = rdx, lo = rax, den = {operand(0)}, q = 0; "
                            f"if (den == 0ull || hi >= den) raise_de(den == 0); "
                            f"for (int b = 63; b >= 0; --b) {{ const bool top = (hi >> 63) != 0; "
                            f"hi = (hi << 1) | ((lo >> b) & 1u); if (top || hi >= den) {{ hi -= den; q |= 1ull << b; }} }} "
                            f"rax = q; rdx = hi; }}")
            else:  # M.IDIV
                if size == 4:
                    stmt = (f"{{ const int64_t num = static_cast<int64_t>((static_cast<uint64_t>(static_cast<uint32_t>(rdx)) << 32) | static_cast<uint32_t>(rax)); "
                            f"const int32_t den = static_cast<int32_t>({operand(0)}); "
                            f"if (den == 0 || (num == (-9223372036854775807ll - 1) && den == -1)) raise_de(den == 0); "
                            f"const int64_t q = num / den, r = num % den; "
                            f"if (q < -2147483648ll || q > 2147483647ll) raise_de(false); "
                            f"rax = static_cast<uint32_t>(static_cast<int32_t>(q)); rdx = static_cast<uint32_t>(static_cast<int32_t>(r)); }}")
                else:
                    stmt = (f"{{ uint64_t hi = rdx, lo = rax, d = {operand(0)}; "
                            f"if (d == 0ull) raise_de(true); "
                            f"const bool neg_n = (hi >> 63) != 0, neg_d = (d >> 63) != 0; "
                            f"if (neg_n) {{ lo = 0ull - lo; hi = ~hi + (lo == 0ull ? 1ull : 0ull); }} "
                            f"if (neg_d) {{ d = 0ull - d; }} "
                            f"if (hi >= d) raise_de(false); "
                            f"uint64_t q = 0; "
                            f"for (int b = 63; b >= 0; --b) {{ const bool top = (hi >> 63) != 0; "
                            f"hi = (hi << 1) | ((lo >> b) & 1u); if (top || hi >= d) {{ hi -= d; q |= 1ull << b; }} }} "
                            f"const bool neg_q = neg_n != neg_d; "
                            f"if ((!neg_q && q > 0x7fffffffffffffffull) || (neg_q && q > 0x8000000000000000ull)) raise_de(false); "
                            f"rax = neg_q ? (0ull - q) : q; rdx = neg_n ? (0ull - hi) : hi; }}")
        elif op in (M.XADD, M.CMPXCHG):
            size = MemorySizeExt.size(i.memory_size) if i.op0_kind == K.MEMORY else RegisterExt.size(i.op0_register)
            if size not in (4, 8):
                raise ValueError(f"unsupported query operation at {i.ip:#x}: {i}")
            width = UINT[size]
            if op == M.XADD:
                src_dst = reg(i.op1_register)
                if i.op0_kind == K.MEMORY:
                    stmt = (f"{{ const word addr = {address()}; const {width} a = {operand(0)}, b = {operand(1)}, "
                            f"v = static_cast<{width}>(a + b); carry = v < a; zero = v == 0; overflow = ((~(a ^ b) & (a ^ v)) >> {8 * size - 1}) != 0; "
                            f"sign = ((v >> {8 * size - 1}) & 1) != 0; {src_dst} = a; store<{width}>(addr, v); }}")
                else:
                    stmt = (f"{{ const {width} a = {operand(0)}, b = {operand(1)}, v = static_cast<{width}>(a + b); "
                            f"carry = v < a; zero = v == 0; overflow = ((~(a ^ b) & (a ^ v)) >> {8 * size - 1}) != 0; "
                            f"sign = ((v >> {8 * size - 1}) & 1) != 0; {src_dst} = a; {write('v')} }}")
            else:  # M.CMPXCHG
                if i.op0_kind == K.MEMORY:
                    stmt = (f"{{ const word addr = {address()}; const {width} acc = static_cast<{width}>(rax), "
                            f"d = {operand(0)}, s = {operand(1)}, diff = static_cast<{width}>(acc - d); "
                            f"zero = acc == d; carry = acc < d; overflow = (((acc ^ d) & (acc ^ diff)) >> {8 * size - 1}) != 0; "
                            f"sign = ((diff >> {8 * size - 1}) & 1) != 0; if (zero) {{ store<{width}>(addr, s); }} else {{ rax = d; }} }}")
                else:
                    stmt = (f"{{ const {width} acc = static_cast<{width}>(rax), d = {operand(0)}, s = {operand(1)}, "
                            f"diff = static_cast<{width}>(acc - d); "
                            f"zero = acc == d; carry = acc < d; overflow = (((acc ^ d) & (acc ^ diff)) >> {8 * size - 1}) != 0; "
                            f"sign = ((diff >> {8 * size - 1}) & 1) != 0; if (zero) {{ {write('s')} }} else {{ rax = d; }} }}")
        elif op in (M.CMPXCHG8B, M.CMPXCHG16B):
            # Compare EDX:EAX (RDX:RAX) with the memory operand. If equal, set ZF and store ECX:EBX (RCX:RBX);
            # otherwise clear ZF and load the operand into EDX:EAX (RDX:RAX), zero-extending a 32-bit half.
            # The other flags are unchanged. CMPXCHG16B faults on an operand that is not 16-byte aligned.
            if i.op0_kind != K.MEMORY:
                raise ValueError(f"unsupported query operation at {i.ip:#x}: {i}")
            if op == M.CMPXCHG8B:
                stmt = (f"{{ const word addr = {address()}; const uint64_t d = load_always<uint64_t>(addr), "
                        "acc = (static_cast<uint64_t>(static_cast<uint32_t>(rdx)) << 32) | static_cast<uint32_t>(rax); "
                        "zero = acc == d; const uint64_t replacement = "
                        "(static_cast<uint64_t>(static_cast<uint32_t>(rcx)) << 32) | static_cast<uint32_t>(rbx); "
                        "cmpxchg_writeback(addr, zero ? replacement : d); "
                        "if (!zero) { rax = static_cast<uint32_t>(d); rdx = d >> 32; } }")
            else:
                stmt = (f"{{ const word addr = {address()}; if (addr & 15u) raise_aligned_m128_fault(); "
                        "const auto d = load_always<std::array<uint64_t,2>>(addr); const uint64_t lo = d[0], hi = d[1]; "
                        "zero = lo == rax && hi == rdx; "
                        "cmpxchg_writeback(addr, zero ? rbx : lo); "
                        "cmpxchg_writeback(addr + 8, zero ? rcx : hi); "
                        "if (!zero) { rax = lo; rdx = hi; } }")
        elif op in (M.VMOVQ, M.VMOVSD, M.VMOVHPD) and has_wide:
            lanes = {R.XMM0: "wide0", R.XMM1: "wide1", R.XMM2: "wide2"}
            if i.op1_kind == K.MEMORY and op != M.VMOVHPD:
                dst = lanes[i.op0_register]
                stmt = f"{{ const uint64_t v = {guest('uint64_t')}; {dst} = {{}}; memcpy({dst}.data(), &v, 8); }}"
            elif i.op0_kind == K.MEMORY:
                start = 8 if op == M.VMOVHPD else 0
                stmt = f"memcpy(reinterpret_cast<void*>({address()}), {lanes[i.op1_register]}.data()+{start}, 8);"
            else:
                raise ValueError(f"unsupported vector move at {i.ip:#x}")
        elif has_wide and not has_xmm_math and op in (M.VPADDD, M.VPSRLD):
            lanes = {R.XMM0: "wide0", R.XMM1: "wide1", R.XMM2: "wide2"}
            src, dst = lanes[i.op1_register], lanes[i.op0_register]
            operation = (f"+ load<uint32_t>(reinterpret_cast<word>({vector(16)}.data() + n))" if op == M.VPADDD
                         else f">> {i.immediate(2)}")
            if op == M.VPSRLD and i.immediate(2) >= 32:
                raise ValueError("unsupported packed shift count")
            stmt = (f"{{ auto input = {src}; {dst} = {{}}; for (size_t n=0; n<16; n+=4) {{ "
                    f"auto v = load<uint32_t>(reinterpret_cast<word>(input.data()+n)) {operation}; "
                    f"memcpy({dst}.data()+n, &v, 4); }} }}")
        elif op in (M.VMOVAPS, M.VMOVAPD, M.VMOVDQA) and i.encoding == EncodingKind.VEX:
            d, src = i.op0_register, i.op1_register
            nbytes = RegisterExt.size(d if d in wide_any else src)
            valid = wide if nbytes == 32 else wide_xmm
            if d in valid and src in valid:
                tail = f" memset({valid[d]}.data()+16, 0, 16);" if nbytes == 16 else ""
                stmt = f"{{ {valid[d]} = {valid[src]};{tail} }}"
            elif d in valid and i.op1_kind == K.MEMORY:
                stmt = (f"{{ const word source = {address()}; if (source & {nbytes - 1}u) raise_aligned_m128_fault(); "
                        f"const auto bytes = load_always<std::array<uint8_t,{nbytes}>>(source); "
                        f"{valid[d]} = {{}}; memcpy({valid[d]}.data(), bytes.data(), {nbytes}); }}")
            elif i.op0_kind == K.MEMORY and src in valid:
                stmt = (f"{{ const word target = {address()}; if (target & {nbytes - 1}u) raise_aligned_m128_fault(); "
                        f"memcpy(reinterpret_cast<void*>(target), {valid[src]}.data(), {nbytes}); }}")
            else:
                raise ValueError(f"unsupported aligned AVX move at {i.ip:#x}: {i}")
        elif (op in avx_wide_conversion and i.encoding == EncodingKind.VEX
              and (i.op0_register in wide or i.op1_register in wide
                   or (i.op1_kind == K.MEMORY and MemorySizeExt.size(i.memory_size) == 32))):
            convert, source_type, result_type, source_size, result_size = avx_wide_conversion[op]
            source_regs = wide if source_size == 32 else wide_xmm
            result_regs = wide if result_size == 32 else wide_xmm
            if (i.op0_register not in result_regs or i.op1_kind not in (K.REGISTER, K.MEMORY)
                    or (i.op1_kind == K.REGISTER and i.op1_register not in source_regs)
                    or (i.op1_kind == K.MEMORY and MemorySizeExt.size(i.memory_size) != source_size)):
                raise ValueError(f"unsupported wide AVX conversion at {i.ip:#x}: {i}")
            dst = result_regs[i.op0_register]
            src = source_regs[i.op1_register] if i.op1_kind == K.REGISTER else vector(source_size)
            stmt = (f"{{ {source_type} source; memcpy(&source, {src}.data(), {source_size}); "
                    f"const {result_type} result = _mm256_{convert}(source); "
                    f"{dst} = {{}}; memcpy({dst}.data(), &result, {result_size}); }}")
        elif op == M.VMOVNTDQA:
            raise ValueError(f"unsupported VMOVNTDQA memory contract at {i.ip:#x}: {i}")
        elif op == M.VLDDQU:
            if (i.encoding != EncodingKind.VEX or i.op0_kind != K.REGISTER
                    or i.op0_register not in wide_any or i.op1_kind != K.MEMORY):
                raise ValueError(f"unsupported VLDDQU form at {i.ip:#x}: {i}")
            dst = wide_any[i.op0_register]
            nbytes = RegisterExt.size(i.op0_register)
            stmt = (f"{{ const auto loaded = {vector(nbytes)}; {dst} = {{}}; "
                    f"memcpy({dst}.data(), loaded.data(), {nbytes}); }}")
        elif op in avx_test:
            nbytes = RegisterExt.size(i.op0_register)
            valid = wide if nbytes == 32 else wide_xmm
            if (i.encoding != EncodingKind.VEX or i.op0_kind != K.REGISTER or i.op0_register not in valid
                    or i.op1_kind not in (K.REGISTER, K.MEMORY)
                    or (i.op1_kind == K.REGISTER and i.op1_register not in valid)):
                raise ValueError(f"unsupported AVX flag-test form at {i.ip:#x}: {i}")
            lhs = wide_any[i.op0_register]
            rhs = wide_any[i.op1_register] if i.op1_kind == K.REGISTER else vector(nbytes)
            stride = avx_test[op]
            mask = 0xff if op == M.VPTEST else 0x80
            pf = " parity = false;" if parity else ""
            stmt = (f"{{ const auto rhs = {rhs}; unsigned z = 0, c = 0; "
                    f"for (size_t n = {stride - 1}; n < {nbytes}; n += {stride}) {{ "
                    f"const unsigned a = {lhs}[n], b = rhs[n] & {mask}u; z |= a & b; c |= ~a & b; }} "
                    f"zero = z == 0; carry = c == 0; overflow = false; sign = false;{pf} }}")
        elif op in legacy_packed_compare and i.encoding == EncodingKind.LEGACY:
            suffix, fp_vector = legacy_packed_compare[op]
            if i.op0_kind != K.REGISTER or i.op0_register not in wide_xmm:
                raise ValueError(f"unsupported packed SSE compare destination at {i.ip:#x}: {i}")
            dst = wide_xmm[i.op0_register]
            if i.op1_kind == K.MEMORY:
                rhs = f"load_legacy_aligned_m128({address()})"
            elif i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                rhs = wide_xmm[i.op1_register]
            else:
                raise ValueError(f"unsupported packed SSE compare source at {i.ip:#x}: {i}")
            predicate = ("eq", "lt", "le", "unord", "neq", "nlt", "nle", "ord")[i.immediate(2) & 7]
            stmt = (f"{{ {fp_vector} lhs, rhs; memcpy(&lhs, {dst}.data(), 16); "
                    f"memcpy(&rhs, {rhs}.data(), 16); "
                    f"const {fp_vector} result = _mm_cmp{predicate}_{suffix}(lhs, rhs); "
                    f"memcpy({dst}.data(), &result, 16); }}")
        elif op in avx_compare:
            suffix = avx_compare[op]
            scalar = suffix in ("ss", "sd")
            nbytes = RegisterExt.size(i.op0_register)
            valid = wide if nbytes == 32 and not scalar else wide_xmm
            if (i.encoding != EncodingKind.VEX or i.op0_kind != K.REGISTER or i.op0_register not in valid
                    or i.op1_kind != K.REGISTER or i.op1_register not in valid
                    or i.op2_kind not in (K.REGISTER, K.MEMORY)
                    or (i.op2_kind == K.REGISTER and i.op2_register not in valid)):
                raise ValueError(f"unsupported AVX compare form at {i.ip:#x}: {i}")
            dst, lhs = wide_any[i.op0_register], wide_any[i.op1_register]
            mem_size = (4 if suffix == "ss" else 8) if scalar else nbytes
            rhs = wide_any[i.op2_register] if i.op2_kind == K.REGISTER else vector(mem_size)
            intrinsic = "_mm256_cmp" if nbytes == 32 else "_mm_cmp"
            fp_vector = ("__m256" if nbytes == 32 else "__m128") + ("d" if suffix in ("pd", "sd") else "")
            pred = i.immediate(3) & 31
            stmt = (f"{{ {fp_vector} lhs, rhs{{}}; memcpy(&lhs, {lhs}.data(), {nbytes}); "
                    f"memcpy(&rhs, {rhs}.data(), {mem_size}); "
                    f"const {fp_vector} result = {intrinsic}_{suffix}(lhs, rhs, {pred}); "
                    f"{dst} = {{}}; memcpy({dst}.data(), &result, {nbytes}); }}")
        elif op == M.VEXTRACTPS and i.encoding == EncodingKind.VEX and has_wide and i.op1_register in wide_xmm:
            lane = i.immediate(2) & 3
            stmt = f"{{ uint32_t v; memcpy(&v, {wide_xmm[i.op1_register]}.data()+{4 * lane}, 4); {write('v')} }}"
        elif op in (M.VCOMISS, M.VUCOMISS, M.VCOMISD, M.VUCOMISD):
            if (i.encoding != EncodingKind.VEX or i.op0_kind != K.REGISTER
                    or i.op0_register not in wide_xmm or i.op1_kind not in (K.MEMORY, K.REGISTER)
                    or (i.op1_kind == K.REGISTER and i.op1_register not in wide_xmm)):
                raise ValueError(f"unsupported scalar AVX compare at {i.ip:#x}: {i}")
            single = op in (M.VCOMISS, M.VUCOMISS)
            bit_type, vec_type, size, suffix, abs_mask, inf_bits = (
                ("uint32_t", "__m128", 4, "ss", "0x7fffffffu", "0x7f800000u") if single
                else ("uint64_t", "__m128d", 8, "sd", "0x7fffffffffffffffull", "0x7ff0000000000000ull")
            )
            lhs = wide_xmm[i.op0_register]
            rhs = (f"memcpy(&y, {wide_xmm[i.op1_register]}.data(), {size});" if i.op1_kind == K.REGISTER
                   else f"y = {guest(bit_type)};")
            cmp = "_mm_comi" if op in (M.VCOMISS, M.VCOMISD) else "_mm_ucomi"
            pf = " parity = unordered;" if parity else ""
            stmt = (f"{{ {bit_type} x, y; memcpy(&x, {lhs}.data(), {size}); {rhs} "
                    f"{vec_type} a{{}}, b{{}}; memcpy(&a, &x, {size}); memcpy(&b, &y, {size}); "
                    f"const bool eq = {cmp}eq_{suffix}(a, b) != 0, lt = {cmp}lt_{suffix}(a, b) != 0; "
                    f"const bool unordered = (x & {abs_mask}) > {inf_bits} || (y & {abs_mask}) > {inf_bits}; "
                    f"zero = eq | unordered; carry = lt | unordered; overflow = false; sign = false;{pf} }}")
        elif op == M.VMOVSS and i.encoding == EncodingKind.VEX and has_wide:
            if i.op_count == 2 and i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and i.op1_kind == K.MEMORY:
                dst = wide_xmm[i.op0_register]
                stmt = f"{{ const uint32_t s = {guest('uint32_t')}; {dst} = {{}}; memcpy({dst}.data(), &s, 4); }}"
            elif i.op_count == 2 and i.op0_kind == K.MEMORY and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                stmt = f"memcpy(reinterpret_cast<void*>({address()}), {wide_xmm[i.op1_register]}.data(), 4);"
            elif i.op_count == 3 and i.op0_register in wide_xmm and i.op1_register in wide_xmm and i.op2_register in wide_xmm:
                dst, s1, s2 = wide_xmm[i.op0_register], wide_xmm[i.op1_register], wide_xmm[i.op2_register]
                stmt = f"{{ auto t = {s1}; uint8_t lo[4]; memcpy(lo, {s2}.data(), 4); {dst} = {{}}; memcpy({dst}.data(), t.data(), 16); memcpy({dst}.data(), lo, 4); }}"
            else:
                raise ValueError(f"unsupported VMOVSS form at {i.ip:#x}: {i}")
        elif op in (M.VMOVLPS, M.VMOVHPS, M.VMOVLPD) and i.encoding == EncodingKind.VEX and has_wide:
            off = 8 if op == M.VMOVHPS else 0
            if i.op_count == 2 and i.op0_kind == K.MEMORY and i.op1_register in wide_xmm:
                stmt = f"memcpy(reinterpret_cast<void*>({address()}), {wide_xmm[i.op1_register]}.data()+{off}, 8);"
            elif i.op_count == 3 and i.op0_register in wide_xmm and i.op1_register in wide_xmm and i.op2_kind == K.MEMORY:
                dst, s1 = wide_xmm[i.op0_register], wide_xmm[i.op1_register]
                stmt = f"{{ const auto t = {s1}; const uint64_t s = {guest('uint64_t')}; {dst} = {{}}; memcpy({dst}.data(), t.data(), 16); memcpy({dst}.data()+{off}, &s, 8); }}"
            else:
                raise ValueError(f"unsupported {op} form at {i.ip:#x}: {i}")
        elif op == M.VINSERTPS and i.encoding == EncodingKind.VEX and has_wide and i.op0_register in wide_xmm:
            dst, s1 = wide_xmm[i.op0_register], wide_xmm[i.op1_register]
            imm = i.immediate(3)
            cs, cd, zm = (imm >> 6) & 3, (imm >> 4) & 3, imm & 0xf
            s2_val = (f"uint8_t v[4]; memcpy(v, {wide_xmm[i.op2_register]}.data()+{4*cs}, 4);" if i.op2_kind == K.REGISTER
                      else f"uint8_t v[4]; const uint32_t s = {guest('uint32_t')}; memcpy(v, &s, 4);")
            zeros = " ".join(f"memset({dst}.data()+{4*k}, 0, 4);" for k in range(4) if (zm >> k) & 1)
            stmt = f"{{ auto t = {s1}; {s2_val} {dst} = {{}}; memcpy({dst}.data(), t.data(), 16); memcpy({dst}.data()+{4*cd}, v, 4); {zeros} }}"
        elif op in avx_scalar_fp:
            if (i.encoding != EncodingKind.VEX or i.op0_kind != K.REGISTER
                    or i.op0_register not in wide_xmm or i.op1_kind != K.REGISTER
                    or i.op1_register not in wide_xmm
                    or i.op2_kind not in (K.MEMORY, K.REGISTER)
                    or (i.op2_kind == K.REGISTER and i.op2_register not in wide_xmm)):
                raise ValueError(f"unsupported scalar AVX operation at {i.ip:#x}: {i}")
            dst, src = wide_xmm[i.op0_register], wide_xmm[i.op1_register]
            suffix, intrinsic = avx_scalar_fp[op]
            bit_type, vec_type, size = (
                ("uint32_t", "__m128", 4) if suffix == "ss"
                else ("uint64_t", "__m128d", 8)
            )
            rhs = (f"memcpy(&b, {wide_xmm[i.op2_register]}.data(), {size});" if i.op2_kind == K.REGISTER
                   else f"const {bit_type} raw = {guest(bit_type)}; memcpy(&b, &raw, {size});")
            args = "a, b"
            if op in (M.VROUNDSS, M.VROUNDSD):
                imm = i.immediate(3)
                if imm & 4:
                    raise ValueError(f"unsupported MXCSR rounding mode in {op} at {i.ip:#x}: {i}")
                args += f", {imm & 0x0b}"
            elif op == M.VSQRTSS:
                args = "b"
            stmt = (f"{{ {vec_type} a, b{{}}; memcpy(&a, {src}.data(), 16); {rhs} "
                    f"const {vec_type} result = {intrinsic}({args}); {dst} = {{}}; "
                    f"memcpy({dst}.data(), &a, 16); memcpy({dst}.data(), &result, {size}); }}")
        elif op in avx_minmax_mul_ops:
            dest = i.op0_register
            valid_regs = wide if dest in wide else wide_xmm
            if (i.encoding != EncodingKind.VEX or i.op0_kind != K.REGISTER or dest not in valid_regs
                    or i.op1_kind != K.REGISTER or i.op1_register not in valid_regs
                    or i.op2_kind not in (K.REGISTER, K.MEMORY)
                    or (i.op2_kind == K.REGISTER and i.op2_register not in valid_regs)):
                raise ValueError(f"unsupported packed saturating or multiply operation at {i.ip:#x}: {i}")
            dst, src1 = wide_any[i.op0_register], wide_any[i.op1_register]
            nbytes = 32 if i.op0_register in wide else 16
            zero_hi = f" memset({dst}.data()+16, 0, 16);" if nbytes == 16 else ""
            s2_init = (f"auto rhs = {wide_any[i.op2_register]};" if i.op2_kind == K.REGISTER
                       else f"auto rhs = load_always<std::array<uint8_t,{nbytes}>>({address()});")
            if op in (M.VPADDSB, M.VPSUBSB, M.VPADDSW, M.VPSUBSW):
                ls = 1 if op in (M.VPADDSB, M.VPSUBSB) else 2
                st, lo, hi = ("int8_t", -128, 127) if ls == 1 else ("int16_t", -32768, 32767)
                sign = "+" if op in (M.VPADDSB, M.VPADDSW) else "-"
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+={ls}) {{ "
                        f"{st} a, b; memcpy(&a, lhs.data()+n, {ls}); memcpy(&b, rhs.data()+n, {ls}); "
                        f"const int s = int(a) {sign} int(b); const {st} v = static_cast<{st}>(s < {lo} ? {lo} : (s > {hi} ? {hi} : s)); "
                        f"memcpy({dst}.data()+n, &v, {ls}); }}{zero_hi} }}")
            elif op in (M.VPADDUSB, M.VPADDUSW):
                ls = 1 if op == M.VPADDUSB else 2
                ut, hi = ("uint8_t", 255) if ls == 1 else ("uint16_t", 65535)
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+={ls}) {{ "
                        f"{ut} a, b; memcpy(&a, lhs.data()+n, {ls}); memcpy(&b, rhs.data()+n, {ls}); "
                        f"const unsigned s = unsigned(a) + unsigned(b); const {ut} v = static_cast<{ut}>(s > {hi}u ? {hi}u : s); "
                        f"memcpy({dst}.data()+n, &v, {ls}); }}{zero_hi} }}")
            elif op in (M.VPSUBUSB, M.VPSUBUSW):
                ls = 1 if op == M.VPSUBUSB else 2
                ut = "uint8_t" if ls == 1 else "uint16_t"
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+={ls}) {{ "
                        f"{ut} a, b; memcpy(&a, lhs.data()+n, {ls}); memcpy(&b, rhs.data()+n, {ls}); "
                        f"const {ut} v = a > b ? static_cast<{ut}>(a - b) : {ut}(0); memcpy({dst}.data()+n, &v, {ls}); }}{zero_hi} }}")
            elif op in (M.VPMINSB, M.VPMAXSB, M.VPMINUB, M.VPMAXUB, M.VPMINSW, M.VPMAXSW,
                        M.VPMINUW, M.VPMAXUW, M.VPMINSD, M.VPMAXSD, M.VPMINUD, M.VPMAXUD):
                lt, ls = {
                    M.VPMINSB: ("int8_t", 1), M.VPMAXSB: ("int8_t", 1), M.VPMINUB: ("uint8_t", 1), M.VPMAXUB: ("uint8_t", 1),
                    M.VPMINSW: ("int16_t", 2), M.VPMAXSW: ("int16_t", 2), M.VPMINUW: ("uint16_t", 2), M.VPMAXUW: ("uint16_t", 2),
                    M.VPMINSD: ("int32_t", 4), M.VPMAXSD: ("int32_t", 4), M.VPMINUD: ("uint32_t", 4), M.VPMAXUD: ("uint32_t", 4),
                }[op]
                cmp_op = "<" if op in (M.VPMINSB, M.VPMINUB, M.VPMINSW, M.VPMINUW, M.VPMINSD, M.VPMINUD) else ">"
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+={ls}) {{ "
                        f"{lt} a, b; memcpy(&a, lhs.data()+n, {ls}); memcpy(&b, rhs.data()+n, {ls}); "
                        f"const {lt} v = a {cmp_op} b ? a : b; memcpy({dst}.data()+n, &v, {ls}); }}{zero_hi} }}")
            elif op in (M.VPAVGB, M.VPAVGW):
                ls, ut = (1, "uint8_t") if op == M.VPAVGB else (2, "uint16_t")
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+={ls}) {{ "
                        f"{ut} a, b; memcpy(&a, lhs.data()+n, {ls}); memcpy(&b, rhs.data()+n, {ls}); "
                        f"const {ut} v = static_cast<{ut}>((unsigned(a) + unsigned(b) + 1u) >> 1); memcpy({dst}.data()+n, &v, {ls}); }}{zero_hi} }}")
            elif op in (M.VPMULHW, M.VPMULHUW, M.VPMULLW):
                it = "int16_t" if op == M.VPMULHW else "uint16_t"
                prod = ("static_cast<uint32_t>(int32_t(a) * int32_t(b)) >> 16" if op == M.VPMULHW
                        else ("(uint32_t(a) * uint32_t(b)) >> 16" if op == M.VPMULHUW else "uint32_t(a) * uint32_t(b)"))
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+=2) {{ "
                        f"{it} a, b; memcpy(&a, lhs.data()+n, 2); memcpy(&b, rhs.data()+n, 2); "
                        f"const uint16_t v = static_cast<uint16_t>({prod}); memcpy({dst}.data()+n, &v, 2); }}{zero_hi} }}")
            elif op == M.VPMULLD:
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+=4) {{ "
                        f"uint32_t a, b; memcpy(&a, lhs.data()+n, 4); memcpy(&b, rhs.data()+n, 4); "
                        f"const uint32_t v = a * b; memcpy({dst}.data()+n, &v, 4); }}{zero_hi} }}")
            elif op in (M.VPMULDQ, M.VPMULUDQ):
                it, ot = ("int32_t", "int64_t") if op == M.VPMULDQ else ("uint32_t", "uint64_t")
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+=8) {{ "
                        f"{it} a, b; memcpy(&a, lhs.data()+n, 4); memcpy(&b, rhs.data()+n, 4); "
                        f"const {ot} v = {ot}(a) * {ot}(b); memcpy({dst}.data()+n, &v, 8); }}{zero_hi} }}")
            elif op == M.VPMULHRSW:
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+=2) {{ "
                        f"int16_t a, b; memcpy(&a, lhs.data()+n, 2); memcpy(&b, rhs.data()+n, 2); "
                        f"const int32_t t = ((int32_t(a) * int32_t(b)) >> 14) + 1; "
                        f"const uint16_t v = static_cast<uint16_t>(t >> 1); memcpy({dst}.data()+n, &v, 2); }}{zero_hi} }}")
            elif op == M.VPMADDWD:
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+=4) {{ "
                        f"int16_t a0, a1, b0, b1; memcpy(&a0, lhs.data()+n, 2); memcpy(&a1, lhs.data()+n+2, 2); "
                        f"memcpy(&b0, rhs.data()+n, 2); memcpy(&b1, rhs.data()+n+2, 2); "
                        f"const uint32_t v = static_cast<uint32_t>(int32_t(a0) * int32_t(b0)) + static_cast<uint32_t>(int32_t(a1) * int32_t(b1)); "
                        f"memcpy({dst}.data()+n, &v, 4); }}{zero_hi} }}")
            elif op == M.VPMADDUBSW:
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+=2) {{ "
                        f"const int s = int(lhs[n]) * int(static_cast<int8_t>(rhs[n])) + int(lhs[n+1]) * int(static_cast<int8_t>(rhs[n+1])); "
                        f"const int16_t v = static_cast<int16_t>(s < -32768 ? -32768 : (s > 32767 ? 32767 : s)); "
                        f"memcpy({dst}.data()+n, &v, 2); }}{zero_hi} }}")
            elif op == M.VPSADBW:
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t q=0; q<{nbytes}; q+=8) {{ uint64_t s = 0; "
                        f"for (size_t k=0; k<8; ++k) {{ const int d = int(lhs[q+k]) - int(rhs[q+k]); s += static_cast<uint64_t>(d < 0 ? -d : d); }} "
                        f"memcpy({dst}.data()+q, &s, 8); }}{zero_hi} }}")
            else:  # M.VMPSADBW
                imm = i.immediate(3)
                stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t g=0; g<{nbytes // 16}; ++g) {{ "
                        f"const size_t lo = 16 * g, s_off = (({imm}u >> (g ? 3u : 0u)) & 3u) * 4u, d_off = (({imm}u >> (g ? 5u : 2u)) & 1u) * 4u; "
                        f"for (size_t j=0; j<8; ++j) {{ uint16_t s = 0; for (size_t k=0; k<4; ++k) {{ "
                        f"const int d = int(lhs[lo+d_off+j+k]) - int(rhs[lo+s_off+k]); s += static_cast<uint16_t>(d < 0 ? -d : d); }} "
                        f"memcpy({dst}.data()+lo+2*j, &s, 2); }} }}{zero_hi} }}")
        elif op in avx_int_ops:
            unary = op in (M.VPABSB, M.VPABSW, M.VPABSD)
            source_index = 1 if unary else 2
            dest = i.op0_register
            source_kind = i.op_kind(source_index)
            valid_regs = wide if dest in wide else wide_xmm
            if (i.encoding != EncodingKind.VEX or i.op0_kind != K.REGISTER or dest not in valid_regs
                    or (not unary and (i.op1_kind != K.REGISTER or i.op1_register not in valid_regs))
                    or (source_kind not in (K.REGISTER, K.MEMORY))
                    or (source_kind == K.REGISTER and i.op_register(source_index) not in valid_regs)):
                raise ValueError(f"unsupported packed integer operation at {i.ip:#x}: {i}")
            dst = wide_any[i.op0_register]
            nbytes = 32 if i.op0_register in wide else 16
            zero_hi = f" memset({dst}.data()+16, 0, 16);" if nbytes == 16 else ""
            if op in (M.VPABSB, M.VPABSW, M.VPABSD):
                lane_size = {M.VPABSB: 1, M.VPABSW: 2, M.VPABSD: 4}[op]
                stype, utype = {1: ("int8_t", "uint8_t"), 2: ("int16_t", "uint16_t"), 4: ("int32_t", "uint32_t")}[lane_size]
                s_init = (f"auto rhs = {wide_any[i.op1_register]};" if i.op1_kind == K.REGISTER
                          else f"auto rhs = load_always<std::array<uint8_t,{nbytes}>>({address()});")
                stmt = (f"{{ {s_init} for (size_t n=0; n<{nbytes}; n+={lane_size}) {{ "
                        f"{stype} a; memcpy(&a, rhs.data()+n, {lane_size}); "
                        f"const {utype} u = static_cast<{utype}>(a); const {utype} v = a < 0 ? static_cast<{utype}>({utype}(0) - u) : u; "
                        f"memcpy({dst}.data()+n, &v, {lane_size}); }}{zero_hi} }}")
            else:
                src1 = wide_any[i.op1_register]
                s2_init = (f"auto rhs = {wide_any[i.op2_register]};" if i.op2_kind == K.REGISTER
                           else f"auto rhs = load_always<std::array<uint8_t,{nbytes}>>({address()});")
                if op in (M.VPADDB, M.VPADDW, M.VPADDQ, M.VPSUBB, M.VPSUBW, M.VPSUBD, M.VPSUBQ):
                    lane_size = {M.VPADDB: 1, M.VPSUBB: 1, M.VPADDW: 2, M.VPSUBW: 2,
                                 M.VPSUBD: 4, M.VPADDQ: 8, M.VPSUBQ: 8}[op]
                    utype = UINT[lane_size]
                    sign = "+" if op in (M.VPADDB, M.VPADDW, M.VPADDQ) else "-"
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+={lane_size}) {{ "
                            f"{utype} a, b; memcpy(&a, lhs.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                            f"const {utype} v = static_cast<{utype}>(a {sign} b); memcpy({dst}.data()+n, &v, {lane_size}); }}{zero_hi} }}")
                elif op in (M.VPAND, M.VPANDN, M.VPOR):
                    expr = {M.VPAND: "a & b", M.VPANDN: "(~a) & b", M.VPOR: "a | b"}[op]
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; ++n) {{ "
                            f"const uint8_t a = lhs[n], b = rhs[n]; {dst}[n] = static_cast<uint8_t>({expr}); }}{zero_hi} }}")
                elif op in (M.VPCMPEQB, M.VPCMPEQW, M.VPCMPEQD, M.VPCMPEQQ):
                    lane_size = {M.VPCMPEQB: 1, M.VPCMPEQW: 2, M.VPCMPEQD: 4, M.VPCMPEQQ: 8}[op]
                    utype = UINT[lane_size]
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+={lane_size}) {{ "
                            f"{utype} a, b; memcpy(&a, lhs.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                            f"const {utype} v = a == b ? static_cast<{utype}>(~{utype}(0)) : {utype}(0); "
                            f"memcpy({dst}.data()+n, &v, {lane_size}); }}{zero_hi} }}")
                elif op in (M.VPCMPGTB, M.VPCMPGTW, M.VPCMPGTD, M.VPCMPGTQ):
                    lane_size = {M.VPCMPGTB: 1, M.VPCMPGTW: 2, M.VPCMPGTD: 4, M.VPCMPGTQ: 8}[op]
                    stype, utype = {1: ("int8_t", "uint8_t"), 2: ("int16_t", "uint16_t"),
                                    4: ("int32_t", "uint32_t"), 8: ("int64_t", "uint64_t")}[lane_size]
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+={lane_size}) {{ "
                            f"{stype} a, b; memcpy(&a, lhs.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                            f"const {utype} v = a > b ? static_cast<{utype}>(~{utype}(0)) : {utype}(0); "
                            f"memcpy({dst}.data()+n, &v, {lane_size}); }}{zero_hi} }}")
                elif op in (M.VPSIGNB, M.VPSIGNW, M.VPSIGND):
                    lane_size = {M.VPSIGNB: 1, M.VPSIGNW: 2, M.VPSIGND: 4}[op]
                    stype, utype = {1: ("int8_t", "uint8_t"), 2: ("int16_t", "uint16_t"), 4: ("int32_t", "uint32_t")}[lane_size]
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+={lane_size}) {{ "
                            f"{stype} a, b; memcpy(&a, lhs.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                            f"const {utype} u = static_cast<{utype}>(a); "
                            f"const {utype} v = b < 0 ? static_cast<{utype}>({utype}(0) - u) : (b == 0 ? {utype}(0) : u); "
                            f"memcpy({dst}.data()+n, &v, {lane_size}); }}{zero_hi} }}")
                elif op == M.VPSHUFB:
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<16; ++k) {{ const uint8_t m = rhs[l+k]; "
                            f"{dst}[l+k] = (m & 0x80u) ? 0u : lhs[l+(m & 0xfu)]; }} }}{zero_hi} }}")
                else:  # M.VPALIGNR
                    imm = i.immediate(3)
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"uint8_t cat[32]; memcpy(cat, rhs.data()+l, 16); memcpy(cat+16, lhs.data()+l, 16); "
                            f"for (size_t k=0; k<16; ++k) {{ const size_t idx = {imm}u + k; "
                            f"{dst}[l+k] = idx < 32u ? cat[idx] : 0u; }} }}{zero_hi} }}")
        elif op == M.MOVDQU and i.encoding == EncodingKind.LEGACY and has_wide:
            if i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and i.op1_kind == K.MEMORY:
                stmt = f"memcpy({wide_xmm[i.op0_register]}.data(), {vector(16)}.data(), 16);"
            elif i.op0_kind == K.MEMORY and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                stmt = f"memcpy(reinterpret_cast<void*>({address()}), {wide_xmm[i.op1_register]}.data(), 16);"
            elif (i.op0_kind == K.REGISTER and i.op0_register in wide_xmm
                  and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm):
                stmt = f"memcpy({wide_xmm[i.op0_register]}.data(), {wide_xmm[i.op1_register]}.data(), 16);"
            else:
                raise ValueError(f"unsupported MOVDQU form at {i.ip:#x}: {i}")
        elif op in sha_ops and i.encoding == EncodingKind.LEGACY and i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and has_wide:
            dst = wide_xmm[i.op0_register]
            if i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                src_init = f"uint32_t s[4]; memcpy(s, {wide_xmm[i.op1_register]}.data(), 16);"
            elif i.op1_kind == K.MEMORY:
                src_init = f"uint32_t s[4]; memcpy(s, {vector(16)}.data(), 16);"
            else:
                raise ValueError(f"unsupported SHA source at {i.ip:#x}: {i}")
            if op == M.SHA1MSG1:
                stmt = (f"{{ uint32_t d[4]; memcpy(d, {dst}.data(), 16); {src_init} "
                        f"const uint32_t r[4] = {{d[0] ^ s[2], d[1] ^ s[3], d[2] ^ d[0], d[3] ^ d[1]}}; "
                        f"memcpy({dst}.data(), r, 16); }}")
            elif op == M.SHA1MSG2:
                stmt = (f"{{ uint32_t d[4]; memcpy(d, {dst}.data(), 16); {src_init} "
                        f"const uint32_t w16 = sha_rol32(d[3] ^ s[2], 1), w17 = sha_rol32(d[2] ^ s[1], 1), "
                        f"w18 = sha_rol32(d[1] ^ s[0], 1), w19 = sha_rol32(d[0] ^ w16, 1); "
                        f"const uint32_t r[4] = {{w19, w18, w17, w16}}; memcpy({dst}.data(), r, 16); }}")
            elif op == M.SHA1NEXTE:
                stmt = (f"{{ uint32_t d[4]; memcpy(d, {dst}.data(), 16); {src_init} "
                        f"const uint32_t r[4] = {{s[0], s[1], s[2], s[3] + sha_rol32(d[3], 30)}}; "
                        f"memcpy({dst}.data(), r, 16); }}")
            elif op == M.SHA1RNDS4:
                func = i.immediate(2) & 3
                stmt = f"{{ uint32_t d[4]; memcpy(d, {dst}.data(), 16); {src_init} sha1_rnds4(d, s, {func}u); memcpy({dst}.data(), d, 16); }}"
            elif op == M.SHA256MSG1:
                stmt = (f"{{ uint32_t d[4]; memcpy(d, {dst}.data(), 16); {src_init} "
                        f"auto sig0 = [](uint32_t x) {{ return sha_ror32(x, 7) ^ sha_ror32(x, 18) ^ (x >> 3); }}; "
                        f"const uint32_t r[4] = {{d[0] + sig0(d[1]), d[1] + sig0(d[2]), d[2] + sig0(d[3]), d[3] + sig0(s[0])}}; "
                        f"memcpy({dst}.data(), r, 16); }}")
            elif op == M.SHA256MSG2:
                stmt = (f"{{ uint32_t d[4]; memcpy(d, {dst}.data(), 16); {src_init} "
                        f"auto sig1 = [](uint32_t x) {{ return sha_ror32(x, 17) ^ sha_ror32(x, 19) ^ (x >> 10); }}; "
                        f"const uint32_t w16 = d[0] + sig1(s[2]), w17 = d[1] + sig1(s[3]), "
                        f"w18 = d[2] + sig1(w16), w19 = d[3] + sig1(w17); "
                        f"const uint32_t r[4] = {{w16, w17, w18, w19}}; memcpy({dst}.data(), r, 16); }}")
            else:  # M.SHA256RNDS2
                stmt = (f"{{ uint32_t d[4], wk[4]; memcpy(d, {dst}.data(), 16); {src_init} "
                        f"memcpy(wk, wide0.data(), 16); sha256_rnds2(d, s, wk); memcpy({dst}.data(), d, 16); }}")
        elif op in avx2_wide_ops and any(i.op_kind(n) == K.REGISTER and i.op_register(n) not in wide_any
                                          for n in range(i.op_count)):
            raise ValueError(f"unsupported query operation at {i.ip:#x}: {i}")
        elif has_wide and i.encoding == EncodingKind.VEX and op in (M.VPBROADCASTB, M.VPBROADCASTW, M.VPBROADCASTD, M.VPBROADCASTQ):
            elem_size = {M.VPBROADCASTB: 1, M.VPBROADCASTW: 2, M.VPBROADCASTD: 4, M.VPBROADCASTQ: 8}[op]
            vec_size = RegisterExt.size(i.op0_register)
            w = UINT[elem_size]
            dst = wide_any[i.op0_register]
            src_expr = (guest(w) if i.op1_kind == K.MEMORY
                        else f"load<{w}>(reinterpret_cast<word>({wide_any[i.op1_register]}.data()))")
            stmt = (f"{{ const {w} elem = {src_expr}; {dst} = {{}}; "
                    f"for (size_t n = 0; n < {vec_size}; n += {elem_size}) memcpy({dst}.data() + n, &elem, {elem_size}); }}")
        elif has_wide and i.encoding == EncodingKind.VEX and op in (M.VPSLLVD, M.VPSRLVD, M.VPSRAVD, M.VPSLLVQ, M.VPSRLVQ):
            elem_size = 8 if op in (M.VPSLLVQ, M.VPSRLVQ) else 4
            bits, w = 8 * elem_size, UINT[elem_size]
            vec_size = RegisterExt.size(i.op0_register)
            dst, src1 = wide_any[i.op0_register], wide_any[i.op1_register]
            if i.op2_kind == K.MEMORY:
                cnt_init = f"const auto in2 = {vector(vec_size)};"
                cnt_expr = f"load<{w}>(reinterpret_cast<word>(in2.data() + n))"
            else:
                cnt_init = f"const auto in2 = {wide_any[i.op2_register]};"
                cnt_expr = f"load<{w}>(reinterpret_cast<word>(in2.data() + n))"
            if op in (M.VPSLLVD, M.VPSLLVQ):
                op_expr = f"c >= {bits}u ? static_cast<{w}>(0) : static_cast<{w}>(a << c)"
            elif op in (M.VPSRLVD, M.VPSRLVQ):
                op_expr = f"c >= {bits}u ? static_cast<{w}>(0) : static_cast<{w}>(a >> c)"
            else:
                op_expr = "static_cast<uint32_t>(static_cast<int32_t>(a) >> (c >= 32u ? 31u : c))"
            stmt = (f"{{ const auto in1 = {src1}; {cnt_init} {dst} = {{}}; "
                    f"for (size_t n = 0; n < {vec_size}; n += {elem_size}) {{ "
                    f"const {w} a = load<{w}>(reinterpret_cast<word>(in1.data() + n)), c = {cnt_expr}; "
                    f"const {w} v = {op_expr}; memcpy({dst}.data() + n, &v, {elem_size}); }} }}")
        elif i.encoding == EncodingKind.VEX and op in avx2_perm_ops and any(
            i.op_kind(n) == K.REGISTER and i.op_register(n) not in wide_any for n in range(i.op_count)
        ):
            raise ValueError(f"unsupported AVX2 permute register at {i.ip:#x}: {i}")
        elif has_wide and i.encoding == EncodingKind.VEX and op == M.VBROADCASTI128 and i.op1_kind == K.MEMORY:
            dst = wide[i.op0_register]
            stmt = (f"{{ const auto lane = {vector(16)}; "
                    f"memcpy({dst}.data(), lane.data(), 16); memcpy({dst}.data() + 16, lane.data(), 16); }}")
        elif has_wide and i.encoding == EncodingKind.VEX and op == M.VEXTRACTI128:
            src = wide[i.op1_register]
            off = 16 if (i.immediate(2) & 1) else 0
            if i.op0_kind == K.MEMORY:
                stmt = f"memcpy(reinterpret_cast<void*>({address()}), {src}.data() + {off}, 16);"
            else:
                dst = wide_any[i.op0_register]
                stmt = f"{{ const auto in1 = {src}; {dst} = {{}}; memcpy({dst}.data(), in1.data() + {off}, 16); }}"
        elif has_wide and i.encoding == EncodingKind.VEX and op == M.VINSERTI128:
            dst, src1 = wide[i.op0_register], wide[i.op1_register]
            off = 16 if (i.immediate(3) & 1) else 0
            src2_expr = (vector(16) if i.op2_kind == K.MEMORY
                         else f"load<std::array<uint8_t,16>>(reinterpret_cast<word>({wide_any[i.op2_register]}.data()))")
            stmt = f"{{ auto out = {src1}; const auto lane = {src2_expr}; memcpy(out.data() + {off}, lane.data(), 16); {dst} = out; }}"
        elif has_wide and i.encoding == EncodingKind.VEX and op == M.VPBLENDD:
            vec_size = RegisterExt.size(i.op0_register)
            dst, src1 = wide_any[i.op0_register], wide_any[i.op1_register]
            imm = i.immediate(3)
            if i.op2_kind == K.MEMORY:
                s2_init = f"const auto in2 = {vector(vec_size)};"
            else:
                s2_init = f"const auto in2 = {wide_any[i.op2_register]};"
            s2_ptr = "load<uint32_t>(reinterpret_cast<word>(in2.data() + n))"
            stmt = (f"{{ const auto in1 = {src1}; {s2_init} {dst} = {{}}; "
                    f"for (size_t lane = 0; lane < {vec_size // 4}; ++lane) {{ const size_t n = lane * 4; "
                    f"const uint32_t v = (({imm}u >> lane) & 1u) ? {s2_ptr} : load<uint32_t>(reinterpret_cast<word>(in1.data() + n)); "
                    f"memcpy({dst}.data() + n, &v, 4); }} }}")
        elif has_wide and i.encoding == EncodingKind.VEX and op == M.VPERM2I128:
            dst, src1 = wide[i.op0_register], wide[i.op1_register]
            imm = i.immediate(3)
            s2_init = (f"const auto in2 = {vector(32)};" if i.op2_kind == K.MEMORY
                       else f"const auto in2 = {wide[i.op2_register]};")
            def _lane_copy(sel, out_off):
                if sel & 8:
                    return f"memset({dst}.data() + {out_off}, 0, 16);"
                src_arr, src_off = ("in1", 0) if (sel & 3) == 0 else ("in1", 16) if (sel & 3) == 1 else ("in2", 0) if (sel & 3) == 2 else ("in2", 16)
                return f"memcpy({dst}.data() + {out_off}, {src_arr}.data() + {src_off}, 16);"
            stmt = f"{{ const auto in1 = {src1}; {s2_init} {_lane_copy(imm & 0xf, 0)} {_lane_copy((imm >> 4) & 0xf, 16)} }}"
        elif has_wide and i.encoding == EncodingKind.VEX and op in (M.VPERMD, M.VPERMPS):
            dst, idx_reg = wide[i.op0_register], wide[i.op1_register]
            s2_init = (f"const auto src = {vector(32)};" if i.op2_kind == K.MEMORY
                       else f"const auto src = {wide[i.op2_register]};")
            stmt = (f"{{ const auto idx = {idx_reg}; {s2_init} {dst} = {{}}; "
                    f"for (size_t lane = 0; lane < 8; ++lane) {{ "
                    f"const size_t pick = load<uint32_t>(reinterpret_cast<word>(idx.data() + lane * 4)) & 7u; "
                    f"memcpy({dst}.data() + lane * 4, src.data() + pick * 4, 4); }} }}")
        elif has_wide and i.encoding == EncodingKind.VEX and op in (M.VPERMQ, M.VPERMPD):
            dst = wide[i.op0_register]
            imm = i.immediate(2)
            s1_init = (f"const auto src = {vector(32)};" if i.op1_kind == K.MEMORY
                       else f"const auto src = {wide[i.op1_register]};")
            stmt = (f"{{ {s1_init} {dst} = {{}}; "
                    f"for (size_t lane = 0; lane < 4; ++lane) {{ "
                    f"const size_t pick = ({imm}u >> (lane * 2u)) & 3u; "
                    f"memcpy({dst}.data() + lane * 8, src.data() + pick * 8, 8); }} }}")
        elif has_wide and i.encoding == EncodingKind.LEGACY and i.op0_register in wide_xmm and op in (
                M.PMINSB, M.PMAXSB, M.PMINUW, M.PMAXUW, M.PMINSD, M.PMAXSD, M.PMINUD, M.PMAXUD,
                M.PMULLD, M.PMULDQ, M.PCMPEQQ, M.PACKUSDW, M.PBLENDW, M.PBLENDVB):
            if i.op1_kind != K.MEMORY and (i.op1_kind != K.REGISTER or i.op1_register not in wide_xmm):
                raise ValueError(f"unsupported query operation at {i.ip:#x}: {i}")
            dst = wide_xmm[i.op0_register]
            s2_init = (f"const auto in2 = {vector(16)};" if i.op1_kind == K.MEMORY
                       else f"const auto in2 = load<std::array<uint8_t,16>>(reinterpret_cast<word>({wide_xmm[i.op1_register]}.data()));")
            minmax = {
                M.PMINSB: ("int8_t", 1, "<"), M.PMAXSB: ("int8_t", 1, ">"),
                M.PMINUW: ("uint16_t", 2, "<"), M.PMAXUW: ("uint16_t", 2, ">"),
                M.PMINSD: ("int32_t", 4, "<"), M.PMAXSD: ("int32_t", 4, ">"),
                M.PMINUD: ("uint32_t", 4, "<"), M.PMAXUD: ("uint32_t", 4, ">"),
            }
            if op in minmax:
                ct, sz, cmp_op = minmax[op]
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} for (size_t n = 0; n < 16; n += {sz}) {{ "
                        f"const {ct} a = load<{ct}>(reinterpret_cast<word>(in1.data() + n)), "
                        f"b = load<{ct}>(reinterpret_cast<word>(in2.data() + n)); "
                        f"const {ct} v = (a {cmp_op} b) ? a : b; memcpy({dst}.data() + n, &v, {sz}); }} }}")
            elif op == M.PMULLD:
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} for (size_t n = 0; n < 16; n += 4) {{ "
                        f"const uint32_t v = load<uint32_t>(reinterpret_cast<word>(in1.data() + n)) * "
                        f"load<uint32_t>(reinterpret_cast<word>(in2.data() + n)); memcpy({dst}.data() + n, &v, 4); }} }}")
            elif op == M.PMULDQ:
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} for (size_t n = 0; n < 16; n += 8) {{ "
                        f"const int64_t v = static_cast<int64_t>(load<int32_t>(reinterpret_cast<word>(in1.data() + n))) * "
                        f"static_cast<int64_t>(load<int32_t>(reinterpret_cast<word>(in2.data() + n))); "
                        f"memcpy({dst}.data() + n, &v, 8); }} }}")
            elif op == M.PCMPEQQ:
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} for (size_t n = 0; n < 16; n += 8) {{ "
                        f"const uint64_t v = load<uint64_t>(reinterpret_cast<word>(in1.data() + n)) == "
                        f"load<uint64_t>(reinterpret_cast<word>(in2.data() + n)) ? ~0ull : 0ull; "
                        f"memcpy({dst}.data() + n, &v, 8); }} }}")
            elif op == M.PACKUSDW:
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} for (size_t k = 0; k < 8; ++k) {{ "
                        f"const uint8_t* src = k < 4 ? in1.data() : in2.data(); "
                        f"const int32_t s = load<int32_t>(reinterpret_cast<word>(src + (k % 4) * 4)); "
                        f"const uint16_t v = s < 0 ? 0u : (s > 65535 ? 65535u : static_cast<uint16_t>(s)); "
                        f"memcpy({dst}.data() + k * 2, &v, 2); }} }}")
            elif op == M.PBLENDW:
                imm = i.immediate(2)
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} for (size_t k = 0; k < 8; ++k) {{ "
                        f"const uint8_t* src = (({imm}u >> k) & 1u) ? in2.data() : in1.data(); "
                        f"memcpy({dst}.data() + k * 2, src + k * 2, 2); }} }}")
            else:  # M.PBLENDVB (implicit XMM0 mask)
                stmt = (f"{{ const auto in1 = {dst}, m = wide0; {s2_init} for (size_t n = 0; n < 16; ++n) {{ "
                        f"{dst}[n] = (m[n] & 0x80u) ? in2[n] : in1[n]; }} }}")
        elif has_wide and i.encoding == EncodingKind.LEGACY and i.op0_register in wide_xmm and op in pmov_ext:
            signed, src_sz, dst_sz = pmov_ext[op]
            lanes = 16 // dst_sz
            src_t = f"int{8 * src_sz}_t" if signed else UINT[src_sz]
            dst_t = f"int{8 * dst_sz}_t" if signed else UINT[dst_sz]
            dst = wide_xmm[i.op0_register]
            if i.op1_kind == K.MEMORY:
                src_init = f"const auto in1 = {vector(lanes * src_sz)};"
            elif i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                src_init = f"const auto in1 = {wide_xmm[i.op1_register]};"
            else:
                raise ValueError(f"unsupported query operation at {i.ip:#x}: {i}")
            stmt = (f"{{ {src_init} for (size_t k = 0; k < {lanes}; ++k) {{ "
                    f"const {dst_t} v = static_cast<{dst_t}>(load<{src_t}>(reinterpret_cast<word>(in1.data() + k * {src_sz}))); "
                    f"memcpy({dst}.data() + k * {dst_sz}, &v, {dst_sz}); }} }}")
        elif op in sse2_sat_mul_ops and i.encoding == EncodingKind.LEGACY and i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and has_wide:
            dst = wide_xmm[i.op0_register]
            if i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                src_init = f"std::array<uint8_t,16> rhs; memcpy(rhs.data(), {wide_xmm[i.op1_register]}.data(), 16);"
            elif i.op1_kind == K.MEMORY:
                src_init = f"std::array<uint8_t,16> rhs = {vector(16)};"
            else:
                raise ValueError(f"unsupported SSE2 integer source at {i.ip:#x}: {i}")
            if op in (M.PADDSB, M.PSUBSB, M.PADDSW, M.PSUBSW):
                lane_size = 1 if op in (M.PADDSB, M.PSUBSB) else 2
                stype, lo, hi = ("int8_t", -128, 127) if lane_size == 1 else ("int16_t", -32768, 32767)
                sign = "+" if op in (M.PADDSB, M.PADDSW) else "-"
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+={lane_size}) {{ "
                        f"{stype} a, b; memcpy(&a, {dst}.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                        f"const int s = int(a) {sign} int(b); "
                        f"const {stype} v = static_cast<{stype}>(s < {lo} ? {lo} : (s > {hi} ? {hi} : s)); "
                        f"memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
            elif op in (M.PADDUSB, M.PADDUSW):
                lane_size = 1 if op == M.PADDUSB else 2
                utype, hi = ("uint8_t", 255) if lane_size == 1 else ("uint16_t", 65535)
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+={lane_size}) {{ "
                        f"{utype} a, b; memcpy(&a, {dst}.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                        f"const unsigned s = unsigned(a) + unsigned(b); "
                        f"const {utype} v = static_cast<{utype}>(s > {hi}u ? {hi}u : s); "
                        f"memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
            elif op in (M.PSUBUSB, M.PSUBUSW):
                lane_size = 1 if op == M.PSUBUSB else 2
                utype = "uint8_t" if lane_size == 1 else "uint16_t"
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+={lane_size}) {{ "
                        f"{utype} a, b; memcpy(&a, {dst}.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                        f"const {utype} v = a > b ? static_cast<{utype}>(a - b) : {utype}(0); "
                        f"memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
            elif op in (M.PMULHW, M.PMULHUW, M.PMULLW):
                itype = "int16_t" if op == M.PMULHW else "uint16_t"
                prod = ("static_cast<uint32_t>(int32_t(a) * int32_t(b)) >> 16" if op == M.PMULHW
                        else ("(uint32_t(a) * uint32_t(b)) >> 16" if op == M.PMULHUW
                              else "uint32_t(a) * uint32_t(b)"))
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+=2) {{ "
                        f"{itype} a, b; memcpy(&a, {dst}.data()+n, 2); memcpy(&b, rhs.data()+n, 2); "
                        f"const uint16_t v = static_cast<uint16_t>({prod}); memcpy({dst}.data()+n, &v, 2); }} }}")
            elif op == M.PMADDWD:
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+=4) {{ "
                        f"int16_t a0, a1, b0, b1; memcpy(&a0, {dst}.data()+n, 2); memcpy(&a1, {dst}.data()+n+2, 2); "
                        f"memcpy(&b0, rhs.data()+n, 2); memcpy(&b1, rhs.data()+n+2, 2); "
                        f"const uint32_t v = static_cast<uint32_t>(int32_t(a0) * int32_t(b0)) "
                        f"+ static_cast<uint32_t>(int32_t(a1) * int32_t(b1)); "
                        f"memcpy({dst}.data()+n, &v, 4); }} }}")
            elif op == M.PMULUDQ:
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+=8) {{ "
                        f"uint32_t a, b; memcpy(&a, {dst}.data()+n, 4); memcpy(&b, rhs.data()+n, 4); "
                        f"const uint64_t v = uint64_t(a) * uint64_t(b); memcpy({dst}.data()+n, &v, 8); }} }}")
            elif op in (M.PMINUB, M.PMAXUB, M.PMINSW, M.PMAXSW):
                lane_size = 1 if op in (M.PMINUB, M.PMAXUB) else 2
                ltype = "uint8_t" if lane_size == 1 else "int16_t"
                cmp_op = "<" if op in (M.PMINUB, M.PMINSW) else ">"
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+={lane_size}) {{ "
                        f"{ltype} a, b; memcpy(&a, {dst}.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                        f"const {ltype} v = a {cmp_op} b ? a : b; memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
            elif op in (M.PAVGB, M.PAVGW):
                lane_size = 1 if op == M.PAVGB else 2
                utype = "uint8_t" if lane_size == 1 else "uint16_t"
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+={lane_size}) {{ "
                        f"{utype} a, b; memcpy(&a, {dst}.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                        f"const {utype} v = static_cast<{utype}>((unsigned(a) + unsigned(b) + 1u) >> 1); "
                        f"memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
            else:  # M.PSADBW
                stmt = (f"{{ {src_init} for (size_t q=0; q<16; q+=8) {{ uint64_t s = 0; "
                        f"for (size_t k=0; k<8; ++k) {{ const int d = int({dst}[q+k]) - int(rhs[q+k]); "
                        f"s += static_cast<uint64_t>(d < 0 ? -d : d); }} memcpy({dst}.data()+q, &s, 8); }} }}")
        elif ((op in crypto_legacy_ops and i.encoding == EncodingKind.LEGACY)
              or (op in crypto_vex_ops and i.encoding == EncodingKind.VEX)) and has_wide:
            if i.op0_kind != K.REGISTER or i.op0_register not in wide_xmm:
                raise ValueError(f"unsupported crypto destination at {i.ip:#x}: {i}")
            dst = wide_xmm[i.op0_register]
            is_vex = op in crypto_vex_ops
            zero_hi = f" memset({dst}.data()+16, 0, 16);" if is_vex else ""
            if op in (M.AESIMC, M.VAESIMC, M.AESKEYGENASSIST, M.VAESKEYGENASSIST):
                if i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                    s_init = f"uint8_t s[16]; memcpy(s, {wide_xmm[i.op1_register]}.data(), 16);"
                elif i.op1_kind == K.MEMORY:
                    s_init = f"uint8_t s[16]; memcpy(s, {vector(16)}.data(), 16);"
                else:
                    raise ValueError(f"unsupported crypto source at {i.ip:#x}: {i}")
                if op in (M.AESIMC, M.VAESIMC):
                    stmt = f"{{ {s_init} aes_mix_columns(s, true); memcpy({dst}.data(), s, 16);{zero_hi} }}"
                else:
                    rcon = i.immediate(2) & 0xff
                    stmt = f"{{ {s_init} uint8_t o[16]; aes_keygen(o, s, {rcon}u); memcpy({dst}.data(), o, 16);{zero_hi} }}"
            else:
                if is_vex and (i.op1_kind != K.REGISTER or i.op1_register not in wide_xmm):
                    raise ValueError(f"unsupported crypto source at {i.ip:#x}: {i}")
                src1 = wide_xmm[i.op1_register] if is_vex else dst
                src2_idx = 2 if is_vex else 1
                if i.op_kind(src2_idx) == K.REGISTER and i.op_register(src2_idx) in wide_xmm:
                    s2_init = f"uint8_t s2[16]; memcpy(s2, {wide_xmm[i.op_register(src2_idx)]}.data(), 16);"
                elif i.op_kind(src2_idx) == K.MEMORY:
                    s2_init = f"uint8_t s2[16]; memcpy(s2, {vector(16)}.data(), 16);"
                else:
                    raise ValueError(f"unsupported crypto source2 at {i.ip:#x}: {i}")
                if op in (M.PCLMULQDQ, M.VPCLMULQDQ):
                    imm = i.immediate(3 if is_vex else 2) & 0xff
                    stmt = (f"{{ uint8_t s1[16], o[16]; memcpy(s1, {src1}.data(), 16); {s2_init} "
                            f"pclmul_x86(o, s1, s2, {imm}u); memcpy({dst}.data(), o, 16);{zero_hi} }}")
                else:
                    dec = "true" if op in (M.AESDEC, M.AESDECLAST, M.VAESDEC, M.VAESDECLAST) else "false"
                    last = "true" if op in (M.AESENCLAST, M.AESDECLAST, M.VAESENCLAST, M.VAESDECLAST) else "false"
                    stmt = (f"{{ uint8_t s1[16], o[16]; memcpy(s1, {src1}.data(), 16); {s2_init} "
                            f"aes_round(o, s1, s2, {dec}, {last}); memcpy({dst}.data(), o, 16);{zero_hi} }}")
        elif has_wide and i.encoding == EncodingKind.VEX and op in fma_scalar:
            if (i.op0_kind != K.REGISTER or i.op0_register not in wide_xmm
                    or i.op1_kind != K.REGISTER or i.op1_register not in wide_xmm
                    or (i.op2_kind != K.MEMORY and
                        (i.op2_kind != K.REGISTER or i.op2_register not in wide_xmm))):
                raise ValueError(f"unsupported query operation at {i.ip:#x}: {i}")
            kind, order, is_f64 = fma_scalar[op]
            ftype, utype, sz = ("double", "uint64_t", 8) if is_f64 else ("float", "uint32_t", 4)
            dst, src1 = wide_xmm[i.op0_register], wide_xmm[i.op1_register]
            s0 = f"load<{ftype}>(reinterpret_cast<word>({dst}.data()))"
            s1 = f"load<{ftype}>(reinterpret_cast<word>({src1}.data()))"
            s2 = (guest(ftype) if i.op2_kind == K.MEMORY
                  else f"load<{ftype}>(reinterpret_cast<word>({wide_xmm[i.op2_register]}.data()))")
            neg_p = "true" if kind in ("nmadd", "nmsub") else "false"
            neg_c = "true" if kind in ("msub", "nmsub") else "false"
            stmt = (f"{{ const {ftype} r = fma_x86<{ftype}, {utype}>({s0}, {s1}, {s2}, {order}, {neg_p}, {neg_c}); "
                    f"auto keep = {dst}; {dst} = {{}}; memcpy({dst}.data(), keep.data(), 16); memcpy({dst}.data(), &r, {sz}); }}")
        elif op in sse2_int_ops and i.encoding == EncodingKind.LEGACY and i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and has_wide:
            dst = wide_xmm[i.op0_register]
            if i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                src_init = f"std::array<uint8_t,16> rhs; memcpy(rhs.data(), {wide_xmm[i.op1_register]}.data(), 16);"
            else:
                raise ValueError(f"unsupported SSE2 integer source at {i.ip:#x}: {i}")
            if op in (M.PADDB, M.PADDW, M.PADDD, M.PADDQ, M.PSUBB, M.PSUBW, M.PSUBD, M.PSUBQ):
                lane_size = {M.PADDB: 1, M.PSUBB: 1, M.PADDW: 2, M.PSUBW: 2,
                             M.PADDD: 4, M.PSUBD: 4, M.PADDQ: 8, M.PSUBQ: 8}[op]
                utype = UINT[lane_size]
                sign = "+" if op in (M.PADDB, M.PADDW, M.PADDD, M.PADDQ) else "-"
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+={lane_size}) {{ "
                        f"{utype} a, b; memcpy(&a, {dst}.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                        f"const {utype} v = static_cast<{utype}>(a {sign} b); memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
            elif op in (M.PAND, M.PANDN, M.POR, M.PXOR):
                expr = {M.PAND: "a & b", M.PANDN: "(~a) & b", M.POR: "a | b", M.PXOR: "a ^ b"}[op]
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; ++n) {{ "
                        f"const uint8_t a = {dst}[n], b = rhs[n]; {dst}[n] = static_cast<uint8_t>({expr}); }} }}")
            elif op in (M.PCMPEQB, M.PCMPEQW, M.PCMPEQD):
                lane_size = {M.PCMPEQB: 1, M.PCMPEQW: 2, M.PCMPEQD: 4}[op]
                utype = UINT[lane_size]
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+={lane_size}) {{ "
                        f"{utype} a, b; memcpy(&a, {dst}.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                        f"const {utype} v = a == b ? static_cast<{utype}>(~{utype}(0)) : {utype}(0); "
                        f"memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
            else:  # M.PCMPGTB, M.PCMPGTW, M.PCMPGTD
                lane_size = {M.PCMPGTB: 1, M.PCMPGTW: 2, M.PCMPGTD: 4}[op]
                stype = {1: "int8_t", 2: "int16_t", 4: "int32_t"}[lane_size]
                utype = UINT[lane_size]
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+={lane_size}) {{ "
                        f"{stype} a, b; memcpy(&a, {dst}.data()+n, {lane_size}); memcpy(&b, rhs.data()+n, {lane_size}); "
                        f"const {utype} v = a > b ? static_cast<{utype}>(~{utype}(0)) : {utype}(0); "
                        f"memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
        elif op == M.CRC32 and i.op0_kind == K.REGISTER and RegisterExt.size(i.op0_register) in (4, 8):
            src_size = MemorySizeExt.size(i.memory_size) if i.op1_kind == K.MEMORY else RegisterExt.size(i.op1_register)
            stmt = (f"{{ uint32_t crc = static_cast<uint32_t>({operand(0)}); uint64_t data = {operand(1)}; "
                    f"for (size_t k=0; k<{src_size}; ++k) {{ crc ^= static_cast<uint8_t>(data >> (8u * k)); "
                    f"for (int b=0; b<8; ++b) crc = (crc & 1u) ? ((crc >> 1) ^ 0x82f63b78u) : (crc >> 1); }} "
                    f"{write('crc')} }}")
        elif op == M.MOVNTDQA:
            raise ValueError(f"unsupported MOVNTDQA memory contract at {i.ip:#x}: {i}")
        elif op == M.EXTRACTPS and i.encoding == EncodingKind.LEGACY and has_wide and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
            if i.op0_kind == K.MEMORY:
                if MemorySizeExt.size(i.memory_size) != 4:
                    raise ValueError(f"unsupported EXTRACTPS destination at {i.ip:#x}: {i}")
            elif i.op0_kind == K.REGISTER:
                if RegisterExt.size(i.op0_register) not in (4, 8):
                    raise ValueError(f"unsupported EXTRACTPS destination at {i.ip:#x}: {i}")
            else:
                raise ValueError(f"unsupported EXTRACTPS destination at {i.ip:#x}: {i}")
            lane = i.immediate(2) & 3
            src = wide_xmm[i.op1_register]
            stmt = f"{{ uint32_t v; memcpy(&v, {src}.data() + {lane * 4}, 4); {write('v')} }}"
        elif op in sse34_fp_ops and op != M.EXTRACTPS and i.encoding == EncodingKind.LEGACY and i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and has_wide:
            dst = wide_xmm[i.op0_register]
            if op == M.LDDQU:
                if i.op1_kind != K.MEMORY:
                    raise ValueError(f"unsupported LDDQU source at {i.ip:#x}: {i}")
                stmt = f"memcpy({dst}.data(), reinterpret_cast<const void*>({address()}), 16);"
            elif op == M.MOVDDUP:
                if i.op1_kind == K.MEMORY:
                    s_init = f"uint8_t s[8]; memcpy(s, reinterpret_cast<const void*>({address()}), 8);"
                elif i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                    s_init = f"uint8_t s[8]; memcpy(s, {wide_xmm[i.op1_register]}.data(), 8);"
                else:
                    raise ValueError(f"unsupported SSE3/4.1 FP source at {i.ip:#x}: {i}")
                stmt = f"{{ {s_init} memcpy({dst}.data(), s, 8); memcpy({dst}.data() + 8, s, 8); }}"
            elif op == M.INSERTPS:
                imm = i.immediate(2) & 0xff
                count_s = (imm >> 6) & 3
                count_d = (imm >> 4) & 3
                zmask = imm & 0x0f
                if i.op1_kind == K.MEMORY:
                    s_init = f"const uint32_t s = load<uint32_t>({address()});"
                elif i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                    s_init = f"uint32_t s; memcpy(&s, {wide_xmm[i.op1_register]}.data() + {count_s * 4}, 4);"
                else:
                    raise ValueError(f"unsupported SSE3/4.1 FP source at {i.ip:#x}: {i}")
                stmt = (f"{{ {s_init} memcpy({dst}.data() + {count_d * 4}, &s, 4); "
                        f"for (size_t k = 0; k < 4; ++k) {{ if (({zmask}u >> k) & 1u) memset({dst}.data() + k * 4, 0, 4); }} }}")
            elif op in (M.ROUNDSS, M.ROUNDSD):
                is_sd = op == M.ROUNDSD
                ftype, elem_sz, count, load_fn, store_fn, round_fn = (
                    ("double", 8, 2, "_mm_loadu_pd", "_mm_storeu_pd", "_mm_round_sd") if is_sd
                    else ("float", 4, 4, "_mm_loadu_ps", "_mm_storeu_ps", "_mm_round_ss")
                )
                imm = i.immediate(2) & 0x0f
                if i.op1_kind == K.MEMORY:
                    b_init = f"{ftype} b[{count}]{{}}; memcpy(b, reinterpret_cast<const void*>({address()}), {elem_sz});"
                elif i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                    b_init = f"{ftype} b[{count}]; memcpy(b, {wide_xmm[i.op1_register]}.data(), 16);"
                else:
                    raise ValueError(f"unsupported SSE3/4.1 FP source at {i.ip:#x}: {i}")
                stmt = (f"{{ {ftype} a[{count}], r[{count}]; memcpy(a, {dst}.data(), 16); {b_init} "
                        f"{store_fn}(r, {round_fn}({load_fn}(a), {load_fn}(b), {imm})); memcpy({dst}.data(), r, 16); }}")
            else:
                if i.op1_kind == K.MEMORY:
                    s2_bytes = f"const auto in2 = load_legacy_aligned_m128({address()});"
                elif i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                    s2_bytes = f"const auto in2 = load<std::array<uint8_t,16>>(reinterpret_cast<word>({wide_xmm[i.op1_register]}.data()));"
                else:
                    raise ValueError(f"unsupported SSE3/4.1 FP source at {i.ip:#x}: {i}")
                if op == M.MOVSLDUP:
                    stmt = (f"{{ {s2_bytes} memcpy({dst}.data(), in2.data(), 4); memcpy({dst}.data() + 4, in2.data(), 4); "
                            f"memcpy({dst}.data() + 8, in2.data() + 8, 4); memcpy({dst}.data() + 12, in2.data() + 8, 4); }}")
                elif op == M.MOVSHDUP:
                    stmt = (f"{{ {s2_bytes} memcpy({dst}.data(), in2.data() + 4, 4); memcpy({dst}.data() + 4, in2.data() + 4, 4); "
                            f"memcpy({dst}.data() + 8, in2.data() + 12, 4); memcpy({dst}.data() + 12, in2.data() + 12, 4); }}")
                elif op in (M.BLENDPS, M.BLENDPD):
                    lanes, sz = (4, 4) if op == M.BLENDPS else (2, 8)
                    imm = i.immediate(2) & ((1 << lanes) - 1)
                    stmt = (f"{{ const auto in1 = {dst}; {s2_bytes} for (size_t k = 0; k < {lanes}; ++k) {{ "
                            f"const uint8_t* src = (({imm}u >> k) & 1u) ? in2.data() : in1.data(); "
                            f"memcpy({dst}.data() + k * {sz}, src + k * {sz}, {sz}); }} }}")
                elif op in (M.BLENDVPS, M.BLENDVPD):
                    lanes, sz = (4, 4) if op == M.BLENDVPS else (2, 8)
                    stmt = (f"{{ const auto in1 = {dst}, m = wide0; {s2_bytes} for (size_t k = 0; k < {lanes}; ++k) {{ "
                            f"const uint8_t* src = (m[k * {sz} + {sz - 1}] & 0x80u) ? in2.data() : in1.data(); "
                            f"memcpy({dst}.data() + k * {sz}, src + k * {sz}, {sz}); }} }}")
                elif op in (M.ROUNDPS, M.ROUNDPD):
                    is_pd = op == M.ROUNDPD
                    ftype, count, load_fn, store_fn, round_fn = (
                        ("double", 2, "_mm_loadu_pd", "_mm_storeu_pd", "_mm_round_pd") if is_pd
                        else ("float", 4, "_mm_loadu_ps", "_mm_storeu_ps", "_mm_round_ps")
                    )
                    imm = i.immediate(2) & 0x0f
                    stmt = (f"{{ {s2_bytes} {ftype} b[{count}], r[{count}]; memcpy(b, in2.data(), 16); "
                            f"{store_fn}(r, {round_fn}({load_fn}(b), {imm})); memcpy({dst}.data(), r, 16); }}")
                elif op in (M.DPPS, M.DPPD):
                    is_pd = op == M.DPPD
                    ftype, count, load_fn, store_fn, dp_fn = (
                        ("double", 2, "_mm_loadu_pd", "_mm_storeu_pd", "_mm_dp_pd") if is_pd
                        else ("float", 4, "_mm_loadu_ps", "_mm_storeu_ps", "_mm_dp_ps")
                    )
                    imm = i.immediate(2) & 0xff
                    stmt = (f"{{ {s2_bytes} {ftype} a[{count}], b[{count}], r[{count}]; "
                            f"memcpy(a, {dst}.data(), 16); memcpy(b, in2.data(), 16); "
                            f"{store_fn}(r, {dp_fn}({load_fn}(a), {load_fn}(b), {imm})); memcpy({dst}.data(), r, 16); }}")
                else:
                    intr = {
                        M.ADDSUBPS: ("float", 4, "_mm_loadu_ps", "_mm_storeu_ps", "_mm_addsub_ps"),
                        M.ADDSUBPD: ("double", 2, "_mm_loadu_pd", "_mm_storeu_pd", "_mm_addsub_pd"),
                        M.HADDPS: ("float", 4, "_mm_loadu_ps", "_mm_storeu_ps", "_mm_hadd_ps"),
                        M.HADDPD: ("double", 2, "_mm_loadu_pd", "_mm_storeu_pd", "_mm_hadd_pd"),
                        M.HSUBPS: ("float", 4, "_mm_loadu_ps", "_mm_storeu_ps", "_mm_hsub_ps"),
                        M.HSUBPD: ("double", 2, "_mm_loadu_pd", "_mm_storeu_pd", "_mm_hsub_pd"),
                    }[op]
                    ftype, count, load_fn, store_fn, op_fn = intr
                    stmt = (f"{{ {s2_bytes} {ftype} a[{count}], b[{count}], r[{count}]; "
                            f"memcpy(a, {dst}.data(), 16); memcpy(b, in2.data(), 16); "
                            f"{store_fn}(r, {op_fn}({load_fn}(a), {load_fn}(b))); memcpy({dst}.data(), r, 16); }}")
        elif op in (M.PEXTRB, M.PEXTRW, M.PEXTRD, M.PEXTRQ) and i.encoding == EncodingKind.LEGACY and has_wide and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
            lane_size = {M.PEXTRB: 1, M.PEXTRW: 2, M.PEXTRD: 4, M.PEXTRQ: 8}[op]
            lane = i.immediate(2) & ((16 // lane_size) - 1)
            utype = UINT[lane_size]
            src = wide_xmm[i.op1_register]
            stmt = f"{{ {utype} v; memcpy(&v, {src}.data()+{lane * lane_size}, {lane_size}); {write('v')} }}"
        elif op in (M.PINSRB, M.PINSRW, M.PINSRD, M.PINSRQ) and i.encoding == EncodingKind.LEGACY and has_wide and i.op0_kind == K.REGISTER and i.op0_register in wide_xmm:
            lane_size = {M.PINSRB: 1, M.PINSRW: 2, M.PINSRD: 4, M.PINSRQ: 8}[op]
            lane = i.immediate(2) & ((16 // lane_size) - 1)
            utype = UINT[lane_size]
            dst = wide_xmm[i.op0_register]
            stmt = f"{{ const {utype} v = static_cast<{utype}>({operand(1)}); memcpy({dst}.data()+{lane * lane_size}, &v, {lane_size}); }}"
        elif (op in (M.PMADDUBSW, M.PMULHRSW, M.PHMINPOSUW, M.PTEST, M.MPSADBW, M.PCMPGTQ)
              and i.encoding == EncodingKind.LEGACY and i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and has_wide):
            dst = wide_xmm[i.op0_register]
            if i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                src_init = f"std::array<uint8_t,16> rhs; memcpy(rhs.data(), {wide_xmm[i.op1_register]}.data(), 16);"
            else:
                raise ValueError(f"unsupported SSE4.1/4.2 source at {i.ip:#x}: {i}")
            if op == M.PMADDUBSW:
                stmt = (f"{{ {src_init} for (size_t k=0; k<8; ++k) {{ "
                        f"const int s = int({dst}[2*k]) * int(static_cast<int8_t>(rhs[2*k])) "
                        f"+ int({dst}[2*k+1]) * int(static_cast<int8_t>(rhs[2*k+1])); "
                        f"const int16_t v = static_cast<int16_t>(s < -32768 ? -32768 : (s > 32767 ? 32767 : s)); "
                        f"memcpy({dst}.data()+2*k, &v, 2); }} }}")
            elif op == M.PMULHRSW:
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+=2) {{ "
                        f"int16_t a, b; memcpy(&a, {dst}.data()+n, 2); memcpy(&b, rhs.data()+n, 2); "
                        f"const int32_t t = ((int32_t(a) * int32_t(b)) >> 14) + 1; "
                        f"const int16_t v = static_cast<int16_t>(t >> 1); memcpy({dst}.data()+n, &v, 2); }} }}")
            elif op == M.PHMINPOSUW:
                stmt = (f"{{ {src_init} uint16_t best = 0xffffu, idx = 0u; "
                        f"for (uint16_t k=0; k<8; ++k) {{ uint16_t v; memcpy(&v, rhs.data()+2*k, 2); "
                        f"if (k == 0 || v < best) {{ best = v; idx = k; }} }} "
                        f"memset({dst}.data(), 0, 16); memcpy({dst}.data(), &best, 2); memcpy({dst}.data()+2, &idx, 2); }}")
            elif op == M.PTEST:
                stmt = (f"{{ {src_init} uint8_t z = 0, c = 0; "
                        f"for (size_t n=0; n<16; ++n) {{ z |= ({dst}[n] & rhs[n]); c |= (static_cast<uint8_t>(~{dst}[n]) & rhs[n]); }} "
                        f"zero = z == 0; carry = c == 0; overflow = false; sign = false;"
                        + (" parity = false;" if parity else "") + " }")
            elif op == M.MPSADBW:
                imm = i.immediate(2)
                src_off = (imm & 3) * 4
                dst_off = ((imm >> 2) & 1) * 4
                stmt = (f"{{ std::array<uint8_t,16> lhs; memcpy(lhs.data(), {dst}.data(), 16); {src_init} "
                        f"for (size_t j=0; j<8; ++j) {{ uint16_t s = 0; "
                        f"for (size_t k=0; k<4; ++k) {{ const int d = int(lhs[{dst_off}+j+k]) - int(rhs[{src_off}+k]); "
                        f"s += static_cast<uint16_t>(d < 0 ? -d : d); }} memcpy({dst}.data()+2*j, &s, 2); }} }}")
            else:  # M.PCMPGTQ
                stmt = (f"{{ {src_init} for (size_t n=0; n<16; n+=8) {{ "
                        f"int64_t a, b; memcpy(&a, {dst}.data()+n, 8); memcpy(&b, rhs.data()+n, 8); "
                        f"const uint64_t v = a > b ? ~0ull : 0ull; memcpy({dst}.data()+n, &v, 8); }} }}")
        elif op in (M.PCMPESTRI, M.PCMPESTRM, M.PCMPISTRI, M.PCMPISTRM) and has_wide:
            if (i.encoding != EncodingKind.LEGACY or i.op_count != 3
                    or i.op0_kind != K.REGISTER or i.op0_register not in wide_xmm
                    or i.op1_kind != K.REGISTER or i.op1_register not in wide_xmm
                    or i.op2_kind != K.IMMEDIATE8):
                raise ValueError(f"unsupported packed string compare form at {i.ip:#x}: {i}")
            src1, src2 = wide_xmm[i.op0_register], wide_xmm[i.op1_register]
            imm = i.immediate(2) & 0xff
            is_explicit = "true" if op in (M.PCMPESTRI, M.PCMPESTRM) else "false"
            is_mask = op in (M.PCMPESTRM, M.PCMPISTRM)
            out_stmt = (f"pcmp_write_mask(wide0.data(), r.mask, {imm}u);" if is_mask
                        else "rcx = static_cast<uint64_t>(r.index);")
            parity_stmt = " parity = false;" if parity else ""
            stmt = (f"{{ const PcmpResult r = pcmp_str({src1}.data(), {src2}.data(), {imm}u, {is_explicit}, "
                    f"static_cast<int32_t>(rax), static_cast<int32_t>(rdx)); "
                    f"{out_stmt} carry = r.carry; zero = r.zero; overflow = r.overflow; sign = r.sign;{parity_stmt} }}")
        elif i.encoding == EncodingKind.LEGACY and i.op0_register in wide_xmm and op in ssse3_ops:
            if i.op1_kind != K.REGISTER or i.op1_register not in wide_xmm:
                raise ValueError(f"unsupported SSSE3 source at {i.ip:#x}: {i}")
            dst = wide_xmm[i.op0_register]
            src = wide_xmm[i.op1_register]
            s2_init = f"const auto in2 = {src};"
            if op in (M.PABSB, M.PABSW, M.PABSD):
                sz = {M.PABSB: 1, M.PABSW: 2, M.PABSD: 4}[op]
                ut, st = UINT[sz], f"int{8 * sz}_t"
                stmt = (f"{{ {s2_init} for (size_t n = 0; n < 16; n += {sz}) {{ "
                        f"const {st} s = load<{st}>(reinterpret_cast<word>(in2.data() + n)); "
                        f"const {ut} v = s < 0 ? static_cast<{ut}>(0u - static_cast<{ut}>(s)) : static_cast<{ut}>(s); "
                        f"memcpy({dst}.data() + n, &v, {sz}); }} }}")
            elif op in (M.PSIGNB, M.PSIGNW, M.PSIGND):
                sz = {M.PSIGNB: 1, M.PSIGNW: 2, M.PSIGND: 4}[op]
                ut, st = UINT[sz], f"int{8 * sz}_t"
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} for (size_t n = 0; n < 16; n += {sz}) {{ "
                        f"const {ut} a = load<{ut}>(reinterpret_cast<word>(in1.data() + n)); "
                        f"const {st} b = load<{st}>(reinterpret_cast<word>(in2.data() + n)); "
                        f"const {ut} v = b < 0 ? static_cast<{ut}>(0u - a) : (b == 0 ? static_cast<{ut}>(0) : a); "
                        f"memcpy({dst}.data() + n, &v, {sz}); }} }}")
            elif op == M.PSHUFB:
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} for (size_t n = 0; n < 16; ++n) {{ "
                        f"const uint8_t c = in2[n]; {dst}[n] = (c & 0x80u) ? 0u : in1[c & 0x0fu]; }} }}")
            elif op == M.PALIGNR:
                imm = i.immediate(2)
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} std::array<uint8_t,32> cat{{}}; "
                        f"memcpy(cat.data(), in2.data(), 16); memcpy(cat.data() + 16, in1.data(), 16); "
                        f"for (size_t n = 0; n < 16; ++n) {{ {dst}[n] = (n + {imm}u < 32u) ? cat[n + {imm}u] : 0u; }} }}")
            else:  # PHADDW/D, PHSUBW/D, PHADDSW, PHSUBSW
                sz = 4 if op in (M.PHADDD, M.PHSUBD) else 2
                lanes = 16 // sz
                half = lanes // 2
                ut = UINT[sz]
                sub = op in (M.PHSUBW, M.PHSUBD, M.PHSUBSW)
                sat = op in (M.PHADDSW, M.PHSUBSW)
                if sat:
                    calc = (f"int32_t s = static_cast<int32_t>(static_cast<int16_t>(a)) {'-' if sub else '+'} "
                            f"static_cast<int32_t>(static_cast<int16_t>(b)); "
                            f"const uint16_t v = static_cast<uint16_t>(static_cast<int16_t>(s > 32767 ? 32767 : (s < -32768 ? -32768 : s)));")
                else:
                    calc = f"const {ut} v = static_cast<{ut}>(a {'-' if sub else '+'} b);"
                stmt = (f"{{ const auto in1 = {dst}; {s2_init} for (size_t k = 0; k < {lanes}; ++k) {{ "
                        f"const uint8_t* src = k < {half} ? in1.data() : in2.data(); "
                        f"const size_t base = (k % {half}) * {2 * sz}; "
                        f"const {ut} a = load<{ut}>(reinterpret_cast<word>(src + base)), "
                        f"b = load<{ut}>(reinterpret_cast<word>(src + base + {sz})); "
                        f"{calc} memcpy({dst}.data() + k * {sz}, &v, {sz}); }} }}")
        elif (op == M.PMOVMSKB and i.encoding == EncodingKind.LEGACY and has_wide
              and i.op0_kind == K.REGISTER and RegisterExt.size(i.op0_register) in (4, 8)
              and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm):
            src = wide_xmm[i.op1_register]
            stmt = (f"{{ uint32_t m = 0; for (size_t k=0; k<16; ++k) {{ "
                    f"if ({src}[k] & 0x80u) m |= (1u << k); }} {write('m')} }}")
        elif (op in sse2_shift_pack_ops and op != M.PMOVMSKB and i.encoding == EncodingKind.LEGACY
              and i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and has_wide):
            dst = wide_xmm[i.op0_register]
            if op in (M.PSLLDQ, M.PSRLDQ):
                if i.op1_kind != K.IMMEDIATE8:
                    raise ValueError(f"unsupported byte shift form at {i.ip:#x}: {i}")
                cnt = i.immediate(1)
                if cnt >= 16:
                    stmt = f"memset({dst}.data(), 0, 16);"
                elif op == M.PSLLDQ:
                    stmt = (f"{{ std::array<uint8_t,16> tmp; memcpy(tmp.data(), {dst}.data(), 16); "
                            f"memset({dst}.data(), 0, 16); memcpy({dst}.data()+{cnt}, tmp.data(), {16 - cnt}); }}")
                else:
                    stmt = (f"{{ std::array<uint8_t,16> tmp; memcpy(tmp.data(), {dst}.data(), 16); "
                            f"memset({dst}.data(), 0, 16); memcpy({dst}.data(), tmp.data()+{cnt}, {16 - cnt}); }}")
            elif op in (M.PSLLW, M.PSLLD, M.PSLLQ, M.PSRLW, M.PSRLD, M.PSRLQ, M.PSRAW, M.PSRAD):
                if i.op1_kind == K.IMMEDIATE8:
                    cnt_init = f"const uint64_t cnt = {i.immediate(1)}ull;"
                elif i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                    cnt_init = f"uint64_t cnt; memcpy(&cnt, {wide_xmm[i.op1_register]}.data(), 8);"
                else:
                    raise ValueError(f"unsupported SSE2 shift source at {i.ip:#x}: {i}")
                lane_size = {M.PSLLW: 2, M.PSRLW: 2, M.PSRAW: 2,
                             M.PSLLD: 4, M.PSRLD: 4, M.PSRAD: 4,
                             M.PSLLQ: 8, M.PSRLQ: 8}[op]
                bits = 8 * lane_size
                if op in (M.PSRAW, M.PSRAD):
                    stype = "int16_t" if lane_size == 2 else "int32_t"
                    stmt = (f"{{ {cnt_init} const unsigned c = cnt >= {bits - 1}ull ? {bits - 1}u : static_cast<unsigned>(cnt); "
                            f"for (size_t n=0; n<16; n+={lane_size}) {{ {stype} a; memcpy(&a, {dst}.data()+n, {lane_size}); "
                            f"const {stype} v = static_cast<{stype}>(a >> c); memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
                else:
                    utype = UINT[lane_size]
                    shift_op = "<<" if op in (M.PSLLW, M.PSLLD, M.PSLLQ) else ">>"
                    stmt = (f"{{ {cnt_init} for (size_t n=0; n<16; n+={lane_size}) {{ "
                            f"{utype} a; memcpy(&a, {dst}.data()+n, {lane_size}); "
                            f"const {utype} v = cnt >= {bits}ull ? {utype}(0) : static_cast<{utype}>(a {shift_op} cnt); "
                            f"memcpy({dst}.data()+n, &v, {lane_size}); }} }}")
            else:
                if i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                    src_init = f"std::array<uint8_t,16> rhs; memcpy(rhs.data(), {wide_xmm[i.op1_register]}.data(), 16);"
                else:
                    raise ValueError(f"unsupported SSE2 source operand at {i.ip:#x}: {i}")
                if op in (M.PACKSSWB, M.PACKUSWB):
                    out_type, lo, hi = ("int8_t", -128, 127) if op == M.PACKSSWB else ("uint8_t", 0, 255)
                    stmt = (f"{{ std::array<uint8_t,16> lhs; memcpy(lhs.data(), {dst}.data(), 16); {src_init} "
                            f"for (size_t k=0; k<8; ++k) {{ int16_t a, b; memcpy(&a, lhs.data()+2*k, 2); memcpy(&b, rhs.data()+2*k, 2); "
                            f"const {out_type} va = static_cast<{out_type}>(a < {lo} ? {lo} : (a > {hi} ? {hi} : a)); "
                            f"const {out_type} vb = static_cast<{out_type}>(b < {lo} ? {lo} : (b > {hi} ? {hi} : b)); "
                            f"memcpy({dst}.data()+k, &va, 1); memcpy({dst}.data()+8+k, &vb, 1); }} }}")
                elif op == M.PACKSSDW:
                    stmt = (f"{{ std::array<uint8_t,16> lhs; memcpy(lhs.data(), {dst}.data(), 16); {src_init} "
                            f"for (size_t k=0; k<4; ++k) {{ int32_t a, b; memcpy(&a, lhs.data()+4*k, 4); memcpy(&b, rhs.data()+4*k, 4); "
                            f"const int16_t va = static_cast<int16_t>(a < -32768 ? -32768 : (a > 32767 ? 32767 : a)); "
                            f"const int16_t vb = static_cast<int16_t>(b < -32768 ? -32768 : (b > 32767 ? 32767 : b)); "
                            f"memcpy({dst}.data()+2*k, &va, 2); memcpy({dst}.data()+8+2*k, &vb, 2); }} }}")
                elif op in (M.PUNPCKLBW, M.PUNPCKLWD, M.PUNPCKLDQ, M.PUNPCKLQDQ,
                            M.PUNPCKHBW, M.PUNPCKHWD, M.PUNPCKHDQ, M.PUNPCKHQDQ):
                    lane_size = {M.PUNPCKLBW: 1, M.PUNPCKHBW: 1, M.PUNPCKLWD: 2, M.PUNPCKHWD: 2,
                                 M.PUNPCKLDQ: 4, M.PUNPCKHDQ: 4, M.PUNPCKLQDQ: 8, M.PUNPCKHQDQ: 8}[op]
                    base = 0 if op in (M.PUNPCKLBW, M.PUNPCKLWD, M.PUNPCKLDQ, M.PUNPCKLQDQ) else 8
                    pairs = 8 // lane_size
                    stmt = (f"{{ std::array<uint8_t,16> lhs; memcpy(lhs.data(), {dst}.data(), 16); {src_init} "
                            f"for (size_t k=0; k<{pairs}; ++k) {{ "
                            f"memcpy({dst}.data()+2*k*{lane_size}, lhs.data()+{base}+k*{lane_size}, {lane_size}); "
                            f"memcpy({dst}.data()+(2*k+1)*{lane_size}, rhs.data()+{base}+k*{lane_size}, {lane_size}); }} }}")
                elif op == M.PSHUFD:
                    imm = i.immediate(2)
                    copies = " ".join(f"memcpy({dst}.data()+{4*k}, rhs.data()+{4*((imm >> (2*k)) & 3)}, 4);" for k in range(4))
                    stmt = f"{{ {src_init} {copies} }}"
                elif op == M.PSHUFLW:
                    imm = i.immediate(2)
                    copies = " ".join(f"memcpy({dst}.data()+{2*k}, rhs.data()+{2*((imm >> (2*k)) & 3)}, 2);" for k in range(4))
                    stmt = f"{{ {src_init} memcpy({dst}.data()+8, rhs.data()+8, 8); {copies} }}"
                else:  # M.PSHUFHW
                    imm = i.immediate(2)
                    copies = " ".join(f"memcpy({dst}.data()+{8+2*k}, rhs.data()+{8+2*((imm >> (2*k)) & 3)}, 2);" for k in range(4))
                    stmt = f"{{ {src_init} memcpy({dst}.data(), rhs.data(), 8); {copies} }}"
        elif has_wide and i.encoding == EncodingKind.VEX and op in fma_packed:
            kind, order, is_f64 = fma_packed[op]
            ftype, utype, sz = ("double", "uint64_t", 8) if is_f64 else ("float", "uint32_t", 4)
            vec_size = RegisterExt.size(i.op0_register)
            if any(i.op_kind(n) == K.REGISTER and i.op_register(n) not in wide_any
                   for n in range(i.op_count)):
                raise ValueError(f"unsupported packed FMA register at {i.ip:#x}: {i}")
            dst, src1 = wide_any[i.op0_register], wide_any[i.op1_register]
            if i.op2_kind == K.MEMORY:
                memory_src = f"const auto in2 = {vector(vec_size)}; "
                src2 = "in2"
            else:
                memory_src = ""
                src2 = wide_any[i.op2_register]
            neg_p = "true" if kind in ("nmadd", "nmsub") else "false"
            if kind in ("msub", "nmsub"):
                neg_c = "true"
            elif kind == "maddsub":
                neg_c = "(lane & 1u) == 0u"
            elif kind == "msubadd":
                neg_c = "(lane & 1u) != 0u"
            else:
                neg_c = "false"
            # Read the old register, including when a source aliases it, before committing the VEX write.
            stmt = (f"{{ {memory_src}std::array<uint8_t,32> result{{}}; "
                    f"for (size_t lane = 0; lane < {vec_size // sz}; ++lane) {{ const size_t n = lane * {sz}; "
                    f"{ftype} s0, s1, s2; memcpy(&s0, {dst}.data() + n, {sz}); "
                    f"memcpy(&s1, {src1}.data() + n, {sz}); memcpy(&s2, {src2}.data() + n, {sz}); "
                    f"const {ftype} r = fma_x86<{ftype}, {utype}>(s0, s1, s2, {order}, {neg_p}, {neg_c}); "
                    f"memcpy(result.data() + n, &r, {sz}); }} {dst} = result; }}")
        elif op in (M.MOVMSKPS, M.VMOVMSKPS) and has_wide:
            src = wide_xmm[i.op1_register]
            stmt = write(f"((({src}[3] >> 7) & 1) | ((({src}[7] >> 7) & 1) << 1) | ((({src}[11] >> 7) & 1) << 2) | ((({src}[15] >> 7) & 1) << 3))")
        elif op in (M.MOVMSKPD, M.VMOVMSKPD) and has_wide:
            src = wide_xmm[i.op1_register]
            stmt = write(f"((({src}[7] >> 7) & 1) | ((({src}[15] >> 7) & 1) << 1))")
        elif (op == M.VPMOVMSKB and i.encoding == EncodingKind.VEX and has_wide
              and i.op0_kind == K.REGISTER and RegisterExt.size(i.op0_register) in (4, 8)
              and i.op1_kind == K.REGISTER and i.op1_register in wide):
            src = wide_any[i.op1_register]
            nbytes = 32 if i.op1_register in wide else 16
            stmt = (f"{{ uint32_t m = 0; for (size_t k=0; k<{nbytes}; ++k) {{ "
                    f"if ({src}[k] & 0x80u) m |= (1u << k); }} {write('m')} }}")
        elif op in (M.VPEXTRB, M.VPEXTRW, M.VPEXTRD, M.VPEXTRQ) and i.encoding == EncodingKind.VEX and has_wide and i.op1_register in wide_xmm:
            ls = {M.VPEXTRB: 1, M.VPEXTRW: 2, M.VPEXTRD: 4, M.VPEXTRQ: 8}[op]
            lane = i.immediate(2) & ((16 // ls) - 1)
            ut = UINT[ls]
            stmt = f"{{ {ut} v; memcpy(&v, {wide_xmm[i.op1_register]}.data()+{lane * ls}, {ls}); {write('v')} }}"
        elif op in (M.VPINSRB, M.VPINSRW, M.VPINSRD, M.VPINSRQ) and i.encoding == EncodingKind.VEX and has_wide and i.op0_register in wide_xmm:
            ls = {M.VPINSRB: 1, M.VPINSRW: 2, M.VPINSRD: 4, M.VPINSRQ: 8}[op]
            lane = i.immediate(3) & ((16 // ls) - 1)
            ut = UINT[ls]
            dst, src1 = wide_xmm[i.op0_register], wide_xmm[i.op1_register]
            stmt = (f"{{ auto t = {src1}; const {ut} v = static_cast<{ut}>({operand(2)}); "
                    f"memcpy(t.data()+{lane * ls}, &v, {ls}); {dst} = {{}}; memcpy({dst}.data(), t.data(), 16); }}")
        elif (op in avx_shift_pack_ops and op not in (M.VPMOVMSKB, M.VPEXTRB, M.VPEXTRW, M.VPEXTRD, M.VPEXTRQ,
                                                      M.VPINSRB, M.VPINSRW, M.VPINSRD, M.VPINSRQ)
              and not existing_packed_xmm and i.op0_register in wide_any):
            dst = wide_any[i.op0_register]
            nbytes = 32 if i.op0_register in wide else 16
            zero_hi = f" memset({dst}.data()+16, 0, 16);" if nbytes == 16 else ""
            if op == M.VPSLLDQ:
                src1, cnt = wide_any[i.op1_register], i.immediate(2)
                if cnt >= 16:
                    stmt = f"{dst} = {{}};"
                else:
                    stmt = (f"{{ auto lhs = {src1}; {dst} = {{}}; for (size_t l=0; l<{nbytes}; l+=16) "
                            f"memcpy({dst}.data()+l+{cnt}, lhs.data()+l, {16 - cnt}); }}")
            elif op in (M.VPSLLW, M.VPSLLD, M.VPSLLQ, M.VPSRLW, M.VPSRLQ, M.VPSRAW, M.VPSRAD):
                src1 = wide_any[i.op1_register]
                if i.op2_kind == K.IMMEDIATE8:
                    cnt_init = f"const uint64_t cnt = {i.immediate(2)}ull;"
                elif i.op2_kind == K.REGISTER and i.op2_register in wide_xmm:
                    cnt_init = f"uint64_t cnt; memcpy(&cnt, {wide_xmm[i.op2_register]}.data(), 8);"
                elif i.op2_kind == K.MEMORY:
                    cnt_init = f"uint64_t cnt; memcpy(&cnt, {vector(16)}.data(), 8);"
                else:
                    raise ValueError(f"unsupported AVX shift count at {i.ip:#x}: {i}")
                ls = {M.VPSLLW: 2, M.VPSRLW: 2, M.VPSRAW: 2, M.VPSLLD: 4, M.VPSRAD: 4, M.VPSLLQ: 8, M.VPSRLQ: 8}[op]
                bits = 8 * ls
                if op in (M.VPSRAW, M.VPSRAD):
                    st = "int16_t" if ls == 2 else "int32_t"
                    stmt = (f"{{ auto lhs = {src1}; {cnt_init} const unsigned c = cnt >= {bits - 1}ull ? {bits - 1}u : static_cast<unsigned>(cnt); "
                            f"for (size_t n=0; n<{nbytes}; n+={ls}) {{ {st} a; memcpy(&a, lhs.data()+n, {ls}); "
                            f"const {st} v = static_cast<{st}>(a >> c); memcpy({dst}.data()+n, &v, {ls}); }}{zero_hi} }}")
                else:
                    ut = UINT[ls]
                    sh = "<<" if op in (M.VPSLLW, M.VPSLLD, M.VPSLLQ) else ">>"
                    stmt = (f"{{ auto lhs = {src1}; {cnt_init} for (size_t n=0; n<{nbytes}; n+={ls}) {{ "
                            f"{ut} a; memcpy(&a, lhs.data()+n, {ls}); "
                            f"const {ut} v = cnt >= {bits}ull ? {ut}(0) : static_cast<{ut}>(a {sh} cnt); "
                            f"memcpy({dst}.data()+n, &v, {ls}); }}{zero_hi} }}")
            elif op in (M.VPSHUFD, M.VPSHUFLW, M.VPSHUFHW):
                imm = i.immediate(2)
                s_init = (f"auto rhs = {wide_any[i.op1_register]};" if i.op1_kind == K.REGISTER
                          else f"const auto rhs = {vector(nbytes)};")
                if op == M.VPSHUFD:
                    copies = " ".join(f"memcpy({dst}.data()+l+{4*k}, rhs.data()+l+{4*((imm >> (2*k)) & 3)}, 4);" for k in range(4))
                    stmt = f"{{ {s_init} for (size_t l=0; l<{nbytes}; l+=16) {{ {copies} }}{zero_hi} }}"
                elif op == M.VPSHUFLW:
                    copies = " ".join(f"memcpy({dst}.data()+l+{2*k}, rhs.data()+l+{2*((imm >> (2*k)) & 3)}, 2);" for k in range(4))
                    stmt = f"{{ {s_init} for (size_t l=0; l<{nbytes}; l+=16) {{ memcpy({dst}.data()+l+8, rhs.data()+l+8, 8); {copies} }}{zero_hi} }}"
                else:
                    copies = " ".join(f"memcpy({dst}.data()+l+{8+2*k}, rhs.data()+l+{8+2*((imm >> (2*k)) & 3)}, 2);" for k in range(4))
                    stmt = f"{{ {s_init} for (size_t l=0; l<{nbytes}; l+=16) {{ memcpy({dst}.data()+l, rhs.data()+l, 8); {copies} }}{zero_hi} }}"
            else:
                src1 = wide_any[i.op1_register]
                s2_init = (f"auto rhs = {wide_any[i.op2_register]};" if i.op2_kind == K.REGISTER
                           else f"const auto rhs = {vector(nbytes)};")
                if op in (M.VPACKSSWB, M.VPACKUSWB):
                    ot, lo, hi = ("int8_t", -128, 127) if op == M.VPACKSSWB else ("uint8_t", 0, 255)
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<8; ++k) {{ int16_t a, b; memcpy(&a, lhs.data()+l+2*k, 2); memcpy(&b, rhs.data()+l+2*k, 2); "
                            f"const {ot} va = static_cast<{ot}>(a < {lo} ? {lo} : (a > {hi} ? {hi} : a)); "
                            f"const {ot} vb = static_cast<{ot}>(b < {lo} ? {lo} : (b > {hi} ? {hi} : b)); "
                            f"memcpy({dst}.data()+l+k, &va, 1); memcpy({dst}.data()+l+8+k, &vb, 1); }} }}{zero_hi} }}")
                elif op in (M.VPACKSSDW, M.VPACKUSDW):
                    ot, lo, hi = ("int16_t", -32768, 32767) if op == M.VPACKSSDW else ("uint16_t", 0, 65535)
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<4; ++k) {{ int32_t a, b; memcpy(&a, lhs.data()+l+4*k, 4); memcpy(&b, rhs.data()+l+4*k, 4); "
                            f"const {ot} va = static_cast<{ot}>(a < {lo} ? {lo} : (a > {hi} ? {hi} : a)); "
                            f"const {ot} vb = static_cast<{ot}>(b < {lo} ? {lo} : (b > {hi} ? {hi} : b)); "
                            f"memcpy({dst}.data()+l+2*k, &va, 2); memcpy({dst}.data()+l+8+2*k, &vb, 2); }} }}{zero_hi} }}")
                elif op in (M.VPUNPCKLBW, M.VPUNPCKLWD, M.VPUNPCKLDQ, M.VPUNPCKLQDQ,
                            M.VPUNPCKHBW, M.VPUNPCKHWD, M.VPUNPCKHDQ, M.VPUNPCKHQDQ):
                    ls = {M.VPUNPCKLBW: 1, M.VPUNPCKHBW: 1, M.VPUNPCKLWD: 2, M.VPUNPCKHWD: 2,
                          M.VPUNPCKLDQ: 4, M.VPUNPCKHDQ: 4, M.VPUNPCKLQDQ: 8, M.VPUNPCKHQDQ: 8}[op]
                    base = 0 if op in (M.VPUNPCKLBW, M.VPUNPCKLWD, M.VPUNPCKLDQ, M.VPUNPCKLQDQ) else 8
                    pairs = 8 // ls
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<{pairs}; ++k) {{ "
                            f"memcpy({dst}.data()+l+2*k*{ls}, lhs.data()+l+{base}+k*{ls}, {ls}); "
                            f"memcpy({dst}.data()+l+(2*k+1)*{ls}, rhs.data()+l+{base}+k*{ls}, {ls}); }} }}{zero_hi} }}")
                elif op == M.VPBLENDW:
                    imm = i.immediate(3)
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<8; ++k) {{ const auto& p = (({imm}u >> k) & 1u) ? rhs : lhs; "
                            f"memcpy({dst}.data()+l+2*k, p.data()+l+2*k, 2); }} }}{zero_hi} }}")
                else:  # M.VPBLENDVB
                    mreg = wide_any[i.op3_register]
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} auto m = {mreg}; for (size_t n=0; n<{nbytes}; ++n) "
                            f"{dst}[n] = (m[n] & 0x80u) ? rhs[n] : lhs[n];{zero_hi} }}")
        elif op in (M.PMOVMSKB, M.VPMOVMSKB) and has_wide:
            src = wide_xmm[i.op1_register]
            stmt = write(f"([](const std::array<uint8_t,32>& s) -> uint32_t {{ uint32_t m=0; for(size_t k=0; k<16; ++k) if(s[k] & 0x80) m |= (1u<<k); return m; }})({src})")
        elif (op in (M.MOVUPS, M.MOVUPD, M.MOVAPD, M.MOVAPS, M.MOVDQA, M.VMOVAPS, M.VMOVAPD, M.VMOVDQA)
              and has_wide and i.op0_kind == K.REGISTER and i.op1_kind == K.REGISTER
              and i.op0_register in wide_xmm and i.op1_register in wide_xmm):
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src = wide_xmm[i.op1_register]
            if dst == src:
                stmt = f"memset({dst}.data()+16, 0, 16);" if is_vex else ";"
            else:
                stmt = f"{{ memcpy({dst}.data(), {src}.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in legacy_half_move and i.encoding == EncodingKind.LEGACY:
            offset = legacy_half_move[op]
            if i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and i.op1_kind == K.MEMORY:
                stmt = (f"{{ const uint64_t value = {guest('uint64_t')}; "
                        f"memcpy({wide_xmm[i.op0_register]}.data() + {offset}, &value, 8); }}")
            elif i.op0_kind == K.MEMORY and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                stmt = (f"{{ uint64_t value; memcpy(&value, {wide_xmm[i.op1_register]}.data() + {offset}, 8); "
                        f"store<uint64_t>({address()}, value); }}")
            else:
                raise ValueError(f"unsupported SSE half move form at {i.ip:#x}: {i}")
        elif op in (M.MOVAPS, M.MOVAPD, M.MOVDQA) and i.encoding == EncodingKind.LEGACY and has_wide:
            if i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and i.op1_kind == K.MEMORY:
                stmt = (f"{{ const auto bytes = load_legacy_aligned_m128({address()}); "
                        f"memcpy({wide_xmm[i.op0_register]}.data(), bytes.data(), 16); }}")
            elif i.op0_kind == K.MEMORY and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                stmt = (f"{{ const word target = {address()}; if (target & 15u) raise_aligned_m128_fault(); "
                        f"memcpy(reinterpret_cast<void*>(target), {wide_xmm[i.op1_register]}.data(), 16); }}")
            else:
                raise ValueError(f"unsupported aligned SSE move form at {i.ip:#x}: {i}")
        elif op in (M.MOVUPS, M.MOVUPD) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            if i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and i.op1_kind == K.MEMORY:
                dst = wide_xmm[i.op0_register]
                stmt = f"{{ memcpy({dst}.data(), {vector(16)}.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
            elif i.op0_kind == K.MEMORY and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                src = wide_xmm[i.op1_register]
                stmt = f"memcpy(reinterpret_cast<void*>({address()}), {src}.data(), 16);"
            else:
                raise ValueError(f"unsupported unaligned vector move at {i.ip:#x}")
        elif op == M.VMOVDQU and has_wide and not has_xmm_math:
            if i.op0_kind == K.REGISTER and i.op0_register in wide and i.op1_kind == K.MEMORY:
                stmt = f"memcpy({wide[i.op0_register]}.data(), {vector(32)}.data(), 32);"
            elif i.op0_kind == K.MEMORY and i.op1_kind == K.REGISTER and i.op1_register in wide:
                stmt = f"memcpy(reinterpret_cast<void*>({address()}), {wide[i.op1_register]}.data(), 32);"
            elif i.op0_kind == K.REGISTER and i.op0_register in wide_xmm and i.op1_kind == K.MEMORY:
                stmt = f"{{ memcpy({wide_xmm[i.op0_register]}.data(), {vector(16)}.data(), 16); memset({wide_xmm[i.op0_register]}.data()+16, 0, 16); }}"
            elif i.op0_kind == K.MEMORY and i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                stmt = f"memcpy(reinterpret_cast<void*>({address()}), {wide_xmm[i.op1_register]}.data(), 16);"
            else:
                raise ValueError(f"unsupported VMOVDQU form at {i.ip:#x}")
        elif op in (M.MOVHLPS, M.VMOVHLPS) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src1 = wide_xmm[i.op1_register] if is_vex else dst
            src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
            stmt = f"{{ std::array<uint8_t, 16> r; memcpy(r.data(), {src2}.data() + 8, 8); memcpy(r.data() + 8, {src1}.data() + 8, 8); memcpy({dst}.data(), r.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.MOVLHPS, M.VMOVLHPS) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src1 = wide_xmm[i.op1_register] if is_vex else dst
            src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
            stmt = f"{{ std::array<uint8_t, 16> r; memcpy(r.data(), {src1}.data(), 8); memcpy(r.data() + 8, {src2}.data(), 8); memcpy({dst}.data(), r.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif (op in avx_shuffle_ops and (op not in avx_existing_xmm_shuffle
                                          or RegisterExt.size(i.op0_register) != 16
                                          or i.op2_kind != K.REGISTER)):
            vector_regs = [i.op_register(n) for n in range(i.op_count)
                           if i.op_kind(n) == K.REGISTER and RegisterExt.is_vector_register(i.op_register(n))]
            if i.encoding != EncodingKind.VEX or any(r not in wide_any for r in vector_regs):
                raise ValueError(f"unsupported AVX shuffle input at {i.ip:#x}: {i}")
            if op == M.VZEROALL:
                stmt = " ".join(f"{v} = {{}};" for v in wide.values())
            elif op == M.VEXTRACTF128:
                src = wide[i.op1_register]
                lane = (i.immediate(2) & 1) * 16
                if i.op0_kind == K.REGISTER and i.op0_register in wide_xmm:
                    dst = wide_xmm[i.op0_register]
                    stmt = f"{{ auto s = {src}; {dst} = {{}}; memcpy({dst}.data(), s.data()+{lane}, 16); }}"
                elif i.op0_kind == K.MEMORY:
                    stmt = f"memcpy(reinterpret_cast<void*>({address()}), {src}.data()+{lane}, 16);"
                else:
                    raise ValueError(f"unsupported VEXTRACTF128 dest at {i.ip:#x}: {i}")
            elif op == M.VINSERTF128:
                dst, src1 = wide[i.op0_register], wide[i.op1_register]
                lane = (i.immediate(3) & 1) * 16
                if i.op2_kind == K.REGISTER and i.op2_register in wide_xmm:
                    s2 = f"uint8_t s2[16]; memcpy(s2, {wide_xmm[i.op2_register]}.data(), 16);"
                elif i.op2_kind == K.MEMORY:
                    s2 = f"uint8_t s2[16]; memcpy(s2, reinterpret_cast<const void*>({address()}), 16);"
                else:
                    raise ValueError(f"unsupported VINSERTF128 src2 at {i.ip:#x}: {i}")
                stmt = f"{{ auto t = {src1}; {s2} memcpy(t.data()+{lane}, s2, 16); {dst} = t; }}"
            elif op in (M.VBROADCASTSS, M.VBROADCASTSD, M.VBROADCASTF128):
                dst = wide_any[i.op0_register]
                nbytes = 32 if i.op0_register in wide else 16
                elem = {M.VBROADCASTSS: 4, M.VBROADCASTSD: 8, M.VBROADCASTF128: 16}[op]
                zero_hi = f" memset({dst}.data()+16, 0, 16);" if nbytes == 16 else ""
                if i.op1_kind == K.MEMORY:
                    s_init = f"uint8_t e[{elem}]; memcpy(e, reinterpret_cast<const void*>({address()}), {elem});"
                elif i.op1_kind == K.REGISTER and i.op1_register in wide_xmm:
                    s_init = f"uint8_t e[{elem}]; memcpy(e, {wide_xmm[i.op1_register]}.data(), {elem});"
                else:
                    raise ValueError(f"unsupported broadcast source at {i.ip:#x}: {i}")
                stmt = f"{{ {s_init} for (size_t n=0; n<{nbytes}; n+={elem}) memcpy({dst}.data()+n, e, {elem});{zero_hi} }}"
            elif op == M.VPERM2F128:
                dst, src1 = wide[i.op0_register], wide[i.op1_register]
                imm = i.immediate(3)
                s2 = (f"auto s2 = {wide[i.op2_register]};" if i.op2_kind == K.REGISTER
                      else f"auto s2 = load<std::array<uint8_t,32>>({address()});")
                def p2_lane(sel, out_off):
                    if sel & 8:
                        return f"memset({dst}.data()+{out_off}, 0, 16);"
                    src_name = "s1" if (sel & 2) == 0 else "s2"
                    src_off = 16 if (sel & 1) else 0
                    return f"memcpy({dst}.data()+{out_off}, {src_name}.data()+{src_off}, 16);"
                stmt = f"{{ auto s1 = {src1}; {s2} {p2_lane(imm & 0xf, 0)} {p2_lane((imm >> 4) & 0xf, 16)} }}"
            elif op in (M.VMOVDDUP, M.VMOVSLDUP, M.VMOVSHDUP, M.VROUNDPS, M.VROUNDPD) or (op in (M.VPERMILPS, M.VPERMILPD) and i.op2_kind == K.IMMEDIATE8):
                dst = wide_any[i.op0_register]
                nbytes = 32 if i.op0_register in wide else 16
                zero_hi = f" memset({dst}.data()+16, 0, 16);" if nbytes == 16 else ""
                s_init = (f"std::array<uint8_t,32> rhs = {wide_any[i.op1_register]};" if i.op1_kind == K.REGISTER
                          else f"std::array<uint8_t,32> rhs{{}}; memcpy(rhs.data(), reinterpret_cast<const void*>({address()}), {MemorySizeExt.size(i.memory_size)});")
                if op == M.VMOVDDUP:
                    stmt = (f"{{ {s_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"memcpy({dst}.data()+l, rhs.data()+l, 8); memcpy({dst}.data()+l+8, rhs.data()+l, 8); }}{zero_hi} }}")
                elif op == M.VMOVSLDUP:
                    stmt = (f"{{ {s_init} for (size_t l=0; l<{nbytes}; l+=8) {{ "
                            f"memcpy({dst}.data()+l, rhs.data()+l, 4); memcpy({dst}.data()+l+4, rhs.data()+l, 4); }}{zero_hi} }}")
                elif op == M.VMOVSHDUP:
                    stmt = (f"{{ {s_init} for (size_t l=0; l<{nbytes}; l+=8) {{ "
                            f"memcpy({dst}.data()+l, rhs.data()+l+4, 4); memcpy({dst}.data()+l+4, rhs.data()+l+4, 4); }}{zero_hi} }}")
                elif op == M.VPERMILPS:
                    imm = i.immediate(2)
                    copies = " ".join(f"memcpy({dst}.data()+l+{4*k}, rhs.data()+l+{4*((imm >> (2*k)) & 3)}, 4);" for k in range(4))
                    stmt = f"{{ {s_init} for (size_t l=0; l<{nbytes}; l+=16) {{ {copies} }}{zero_hi} }}"
                elif op == M.VPERMILPD:
                    imm = i.immediate(2)
                    stmt = (f"{{ {s_init} for (size_t g=0; g<{nbytes // 16}; ++g) {{ "
                            f"const unsigned s0 = ({imm}u >> (2u * g)) & 1u, s1 = ({imm}u >> (2u * g + 1u)) & 1u; "
                            f"memcpy({dst}.data()+16*g, rhs.data()+16*g+8*s0, 8); "
                            f"memcpy({dst}.data()+16*g+8, rhs.data()+16*g+8*s1, 8); }}{zero_hi} }}")
                else:  # VROUNDPS, VROUNDPD
                    imm = i.immediate(2)
                    if imm & 4:
                        raise ValueError(f"unsupported MXCSR rounding mode in {op} at {i.ip:#x}: {i}")
                    ftype, count, load_fn, store_fn, round_fn = (
                        ("double", 2, "_mm_loadu_pd", "_mm_storeu_pd", "_mm_round_pd") if op == M.VROUNDPD
                        else ("float", 4, "_mm_loadu_ps", "_mm_storeu_ps", "_mm_round_ps")
                    )
                    stmt = (f"{{ {s_init} for (size_t n=0; n<{nbytes}; n+=16) {{ "
                            f"{ftype} b[{count}], r[{count}]; memcpy(b, rhs.data()+n, 16); "
                            f"{store_fn}(r, {round_fn}({load_fn}(b), {imm & 0x0b})); "
                            f"memcpy({dst}.data()+n, r, 16); }}{zero_hi} }}")
            else:
                dst, src1 = wide_any[i.op0_register], wide_any[i.op1_register]
                nbytes = 32 if i.op0_register in wide else 16
                zero_hi = f" memset({dst}.data()+16, 0, 16);" if nbytes == 16 else ""
                s2_init = (f"std::array<uint8_t,32> rhs = {wide_any[i.op2_register]};" if i.op2_kind == K.REGISTER
                           else f"std::array<uint8_t,32> rhs{{}}; memcpy(rhs.data(), reinterpret_cast<const void*>({address()}), {MemorySizeExt.size(i.memory_size)});")
                if op == M.VPERMILPS:
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<4; ++k) {{ uint32_t c; memcpy(&c, rhs.data()+l+4*k, 4); "
                            f"memcpy({dst}.data()+l+4*k, lhs.data()+l+4*(c & 3u), 4); }} }}{zero_hi} }}")
                elif op == M.VPERMILPD:
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<2; ++k) {{ uint64_t c; memcpy(&c, rhs.data()+l+8*k, 8); "
                            f"memcpy({dst}.data()+l+8*k, lhs.data()+l+8*((c >> 1) & 1u), 8); }} }}{zero_hi} }}")
                elif op == M.VSHUFPS:
                    imm = i.immediate(3)
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"memcpy({dst}.data()+l+0, lhs.data()+l+{4*(imm & 3)}, 4); "
                            f"memcpy({dst}.data()+l+4, lhs.data()+l+{4*((imm >> 2) & 3)}, 4); "
                            f"memcpy({dst}.data()+l+8, rhs.data()+l+{4*((imm >> 4) & 3)}, 4); "
                            f"memcpy({dst}.data()+l+12, rhs.data()+l+{4*((imm >> 6) & 3)}, 4); }}{zero_hi} }}")
                elif op == M.VSHUFPD:
                    imm = i.immediate(3)
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t g=0; g<{nbytes // 16}; ++g) {{ "
                            f"const unsigned s0 = ({imm}u >> (2u * g)) & 1u, s1 = ({imm}u >> (2u * g + 1u)) & 1u; "
                            f"memcpy({dst}.data()+16*g, lhs.data()+16*g+8*s0, 8); "
                            f"memcpy({dst}.data()+16*g+8, rhs.data()+16*g+8*s1, 8); }}{zero_hi} }}")
                elif op in (M.VUNPCKLPS, M.VUNPCKHPS, M.VUNPCKLPD, M.VUNPCKHPD):
                    lane_size = 4 if op in (M.VUNPCKLPS, M.VUNPCKHPS) else 8
                    base = 0 if op in (M.VUNPCKLPS, M.VUNPCKLPD) else 8
                    pairs = 8 // lane_size
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<{pairs}; ++k) {{ "
                            f"memcpy({dst}.data()+l+2*k*{lane_size}, lhs.data()+l+{base}+k*{lane_size}, {lane_size}); "
                            f"memcpy({dst}.data()+l+(2*k+1)*{lane_size}, rhs.data()+l+{base}+k*{lane_size}, {lane_size}); }} }}{zero_hi} }}")
                elif op in (M.VBLENDPS, M.VBLENDPD):
                    imm = i.immediate(3)
                    lane_size = 4 if op == M.VBLENDPS else 8
                    lanes = nbytes // lane_size
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t k=0; k<{lanes}; ++k) {{ "
                            f"const auto& p = (({imm}u >> k) & 1u) ? rhs : lhs; "
                            f"memcpy({dst}.data()+k*{lane_size}, p.data()+k*{lane_size}, {lane_size}); }}{zero_hi} }}")
                elif op in (M.VBLENDVPS, M.VBLENDVPD):
                    mask_reg = wide_any[i.op3_register]
                    lane_size = 4 if op == M.VBLENDVPS else 8
                    lanes = nbytes // lane_size
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} auto m = {mask_reg}; for (size_t k=0; k<{lanes}; ++k) {{ "
                            f"const auto& p = (m[(k + 1) * {lane_size} - 1] & 0x80u) ? rhs : lhs; "
                            f"memcpy({dst}.data()+k*{lane_size}, p.data()+k*{lane_size}, {lane_size}); }}{zero_hi} }}")
                else:  # VDPPS, VDPPD
                    imm = i.immediate(3)
                    ftype, count, load_fn, store_fn, dp_fn = (
                        ("double", 2, "_mm_loadu_pd", "_mm_storeu_pd", "_mm_dp_pd") if op == M.VDPPD
                        else ("float", 4, "_mm_loadu_ps", "_mm_storeu_ps", "_mm_dp_ps")
                    )
                    stmt = (f"{{ auto lhs = {src1}; {s2_init} for (size_t n=0; n<{nbytes}; n+=16) {{ "
                            f"{ftype} a[{count}], b[{count}], r[{count}]; "
                            f"memcpy(a, lhs.data()+n, 16); memcpy(b, rhs.data()+n, 16); "
                            f"{store_fn}(r, {dp_fn}({load_fn}(a), {load_fn}(b), {imm})); "
                            f"memcpy({dst}.data()+n, r, 16); }}{zero_hi} }}")
        elif op in (M.UNPCKLPS, M.VUNPCKLPS) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
            stmt = f"{{ uint32_t a[4], b[4], r[4]; memcpy(a, {src1}.data(), 16); memcpy(b, {src2}.data(), 16); r[0] = a[0]; r[1] = b[0]; r[2] = a[1]; r[3] = b[1]; memcpy({dst}.data(), r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.UNPCKHPS, M.VUNPCKHPS) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
            stmt = f"{{ uint32_t a[4], b[4], r[4]; memcpy(a, {src1}.data(), 16); memcpy(b, {src2}.data(), 16); r[0] = a[2]; r[1] = b[2]; r[2] = a[3]; r[3] = b[3]; memcpy({dst}.data(), r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.UNPCKLPD, M.VUNPCKLPD, M.PUNPCKLQDQ, M.VPUNPCKLQDQ) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
            stmt = f"{{ uint64_t a[2], b[2], r[2]; memcpy(a, {src1}.data(), 16); memcpy(b, {src2}.data(), 16); r[0] = a[0]; r[1] = b[0]; memcpy({dst}.data(), r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.UNPCKHPD, M.VUNPCKHPD, M.PUNPCKHQDQ, M.VPUNPCKHQDQ) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
            stmt = f"{{ uint64_t a[2], b[2], r[2]; memcpy(a, {src1}.data(), 16); memcpy(b, {src2}.data(), 16); r[0] = a[1]; r[1] = b[1]; memcpy({dst}.data(), r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.SHUFPS, M.VSHUFPS) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
            imm = i.immediate(3 if is_vex else 2)
            i0, i1, i2, i3 = imm & 3, (imm >> 2) & 3, (imm >> 4) & 3, (imm >> 6) & 3
            stmt = f"{{ uint32_t a[4], b[4], r[4]; memcpy(a, {src1}.data(), 16); memcpy(b, {src2}.data(), 16); r[0] = a[{i0}]; r[1] = a[{i1}]; r[2] = b[{i2}]; r[3] = b[{i3}]; memcpy({dst}.data(), r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.SHUFPD, M.VSHUFPD) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
            imm = i.immediate(3 if is_vex else 2)
            i0, i1 = imm & 1, (imm >> 1) & 1
            stmt = f"{{ uint64_t a[2], b[2], r[2]; memcpy(a, {src1}.data(), 16); memcpy(b, {src2}.data(), 16); r[0] = a[{i0}]; r[1] = b[{i1}]; memcpy({dst}.data(), r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.PSHUFD, M.VPSHUFD) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            imm = i.immediate(2)
            i0, i1, i2, i3 = imm & 3, (imm >> 2) & 3, (imm >> 4) & 3, (imm >> 6) & 3
            stmt = f"{{ uint32_t s[4], r[4]; memcpy(s, {src}.data(), 16); r[0] = s[{i0}]; r[1] = s[{i1}]; r[2] = s[{i2}]; r[3] = s[{i3}]; memcpy({dst}.data(), r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.PSHUFHW, M.VPSHUFHW) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            imm = i.immediate(2)
            i0, i1, i2, i3 = (imm & 3) + 4, ((imm >> 2) & 3) + 4, ((imm >> 4) & 3) + 4, ((imm >> 6) & 3) + 4
            stmt = f"{{ uint16_t s[8], r[8]; memcpy(s, {src}.data(), 16); memcpy(r, s, 8); r[4] = s[{i0}]; r[5] = s[{i1}]; r[6] = s[{i2}]; r[7] = s[{i3}]; memcpy({dst}.data(), r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.PSHUFLW, M.VPSHUFLW) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            imm = i.immediate(2)
            i0, i1, i2, i3 = imm & 3, (imm >> 2) & 3, (imm >> 4) & 3, (imm >> 6) & 3
            stmt = f"{{ uint16_t s[8], r[8]; memcpy(s, {src}.data(), 16); r[0] = s[{i0}]; r[1] = s[{i1}]; r[2] = s[{i2}]; r[3] = s[{i3}]; memcpy(r + 4, s + 4, 8); memcpy({dst}.data(), r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.PSLLDQ, M.VPSLLDQ) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            imm = i.immediate(2 if is_vex else 1)
            stmt = f"{{ std::array<uint8_t, 16> r{{}}; const unsigned count = {imm}; for (size_t k = count; k < 16; ++k) r[k] = {src}[k - count]; memcpy({dst}.data(), r.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.PSRLDQ, M.VPSRLDQ) and has_wide and not has_xmm_math:
            is_vex = i.encoding == EncodingKind.VEX
            dst = wide_xmm[i.op0_register]
            src = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            imm = i.immediate(2 if is_vex else 1)
            stmt = f"{{ std::array<uint8_t, 16> r{{}}; const unsigned count = {imm}; for (size_t k = 0; k + count < 16; ++k) r[k] = {src}[k + count]; memcpy({dst}.data(), r.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}"
        elif op in (M.RCPPS, M.VRCPPS, M.RSQRTPS, M.VRSQRTPS) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            is_rsqrt = op in (M.RSQRTPS, M.VRSQRTPS)
            dst = wide_xmm[i.op0_register]
            src = wide_xmm[i.op1_register]
            fn = "_mm_rsqrt_ps" if is_rsqrt else "_mm_rcp_ps"
            stmt = (f"{{ __m128 s; memcpy(&s, {src}.data(), sizeof s); const __m128 r = {fn}(s); "
                    f"memcpy({dst}.data(), &r, sizeof r); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
        elif op in (M.RCPSS, M.VRCPSS, M.RSQRTSS, M.VRSQRTSS) and has_wide:
            is_vex = i.encoding == EncodingKind.VEX
            is_rsqrt = op in (M.RSQRTSS, M.VRSQRTSS)
            dst = wide_xmm[i.op0_register]
            src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
            src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
            fn = "_mm_rsqrt_ss" if is_rsqrt else "_mm_rcp_ss"
            stmt = (f"{{ __m128 s; memcpy(&s, {src2}.data(), sizeof s); const __m128 r = {fn}(s); "
                    f"std::array<uint8_t, 16> res; memcpy(res.data(), {src1}.data(), res.size()); "
                    f"memcpy(res.data(), &r, sizeof(float)); memcpy({dst}.data(), res.data(), res.size()); "
                    f"{' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
        elif op in (M.VMINPS, M.VMAXPS) and i.op0_register in wide:
            if (i.encoding != EncodingKind.VEX or i.op_count != 3 or
                any(i.op_kind(n) != K.REGISTER or i.op_register(n) not in wide for n in range(3))):
                raise ValueError(f"unsupported 256-bit packed float min/max at {i.ip:#x}: {i}")
            _, fn, _ = packed_float_ops[op]
            dst, src1, src2 = (wide[i.op_register(n)] for n in range(3))
            # The packed operation has independent lanes. Keep instructions in
            # source order so MINPS/MAXPS return the correct second operand
            # under DAZ, including signed zero and NaN payload selection.
            stmt = (f"{{ for (size_t n=0; n<32; n+=16) {{ "
                    f"__m128 a, b; memcpy(&a, {src1}.data()+n, 16); memcpy(&b, {src2}.data()+n, 16); "
                    f"const __m128 r = {fn}(a, b); memcpy({dst}.data()+n, &r, 16); }} }}")
        elif op in packed_float_ops:
            kind, fn, is_vex = packed_float_ops[op]
            expected_encoding = EncodingKind.VEX if is_vex else EncodingKind.LEGACY
            expected_ops = 2 if (kind in ("sqrt_ps", "sqrt_pd") or not is_vex) else 3
            if (i.encoding != expected_encoding or
                i.op_count != expected_ops or
                any(i.op_kind(n) != K.REGISTER or i.op_register(n) not in wide_xmm
                    for n in range(i.op_count))):
                raise ValueError(f"unsupported float packed operation at {i.ip:#x}: {i}")
            dst = wide_xmm[i.op0_register]
            if kind == "sqrt_ps":
                src = wide_xmm[i.op1_register]
                stmt = (f"{{ __m128 s; memcpy(&s, {src}.data(), sizeof s); const __m128 r = {fn}(s); "
                        f"memcpy({dst}.data(), &r, sizeof r); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            elif kind == "sqrt_pd":
                src = wide_xmm[i.op1_register]
                stmt = (f"{{ __m128d s; memcpy(&s, {src}.data(), sizeof s); const __m128d r = {fn}(s); "
                        f"memcpy({dst}.data(), &r, sizeof r); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            elif kind == "sqrt_ss":
                src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
                src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
                stmt = (f"{{ __m128 s; memcpy(&s, {src2}.data(), sizeof s); const __m128 r = {fn}(s); "
                        f"std::array<uint8_t, 16> res; memcpy(res.data(), {src1}.data(), res.size()); "
                        f"memcpy(res.data(), &r, sizeof(float)); memcpy({dst}.data(), res.data(), res.size()); "
                        f"{' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            elif kind == "sqrt_sd":
                src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
                src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
                stmt = (f"{{ __m128d s; memcpy(&s, {src2}.data(), sizeof s); const __m128d r = {fn}(s, s); "
                        f"std::array<uint8_t, 16> res; memcpy(res.data(), {src1}.data(), res.size()); "
                        f"memcpy(res.data(), &r, sizeof(double)); memcpy({dst}.data(), res.data(), res.size()); "
                        f"{' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            elif kind in ("ps", "ss"):
                src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
                src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
                stmt = (f"{{ __m128 a, b; memcpy(&a, {src1}.data(), sizeof a); memcpy(&b, {src2}.data(), sizeof b); "
                        f"const __m128 r = {fn}(a, b); memcpy({dst}.data(), &r, sizeof r); "
                        f"{' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            else:
                src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
                src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
                stmt = (f"{{ __m128d a, b; memcpy(&a, {src1}.data(), sizeof a); memcpy(&b, {src2}.data(), sizeof b); "
                        f"const __m128d r = {fn}(a, b); memcpy({dst}.data(), &r, sizeof r); "
                        f"{' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
        elif op in conversion_ops:
            entry = conversion_ops[op]
            kind = entry[0]
            is_vex = entry[-1]
            expected_encoding = EncodingKind.VEX if is_vex else EncodingKind.LEGACY
            if i.encoding != expected_encoding:
                raise ValueError(f"unsupported conversion encoding at {i.ip:#x}: {i}")
            if kind == "packed":
                if (i.op_count != 2 or
                    i.op0_kind != K.REGISTER or i.op1_kind != K.REGISTER or
                    i.op0_register not in wide_xmm or i.op1_register not in wide_xmm):
                    raise ValueError(f"unsupported packed conversion at {i.ip:#x}: {i}")
                _, fn, in_type, out_type, _ = entry
                dst, src = wide_xmm[i.op0_register], wide_xmm[i.op1_register]
                stmt = (f"{{ {in_type} s; memcpy(&s, {src}.data(), 16); const {out_type} r = {fn}(s); "
                        f"memcpy({dst}.data(), &r, 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            elif kind == "f16c_pack":
                if (i.op_count != 3 or
                    i.op0_kind != K.REGISTER or i.op1_kind != K.REGISTER or
                    i.op0_register not in wide_xmm or i.op1_register not in wide_xmm or
                    i.op2_kind != K.IMMEDIATE8 or i.immediate8 != 0):
                    raise ValueError(f"unsupported f16c pack at {i.ip:#x}: {i}")
                dst, src = wide_xmm[i.op0_register], wide_xmm[i.op1_register]
                stmt = (f"{{ __m128 s; memcpy(&s, {src}.data(), 16); const __m128i r = _mm_cvtps_ph(s, 0); "
                        f"std::array<uint8_t, 16> res{{}}; memcpy(res.data(), &r, 8); memcpy({dst}.data(), res.data(), 16); "
                        f"memset({dst}.data()+16, 0, 16); }}")
            elif kind == "ss2sd":
                expected_ops = 3 if is_vex else 2
                if (i.op_count != expected_ops or
                    any(i.op_kind(n) != K.REGISTER or i.op_register(n) not in wide_xmm for n in range(i.op_count))):
                    raise ValueError(f"unsupported ss2sd at {i.ip:#x}: {i}")
                dst = wide_xmm[i.op0_register]
                src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
                src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
                stmt = (f"{{ __m128 s; memcpy(&s, {src2}.data(), sizeof s); const __m128d r = _mm_cvtss_sd(_mm_setzero_pd(), s); "
                        f"std::array<uint8_t, 16> res; memcpy(res.data(), {src1}.data(), 16); memcpy(res.data(), &r, 8); "
                        f"memcpy({dst}.data(), res.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            elif kind == "sd2ss":
                expected_ops = 3 if is_vex else 2
                if (i.op_count != expected_ops or
                    any(i.op_kind(n) != K.REGISTER or i.op_register(n) not in wide_xmm for n in range(i.op_count))):
                    raise ValueError(f"unsupported sd2ss at {i.ip:#x}: {i}")
                dst = wide_xmm[i.op0_register]
                src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
                src2 = wide_xmm[i.op2_register] if is_vex else wide_xmm[i.op1_register]
                stmt = (f"{{ __m128d s; memcpy(&s, {src2}.data(), sizeof s); const __m128 r = _mm_cvtsd_ss(_mm_setzero_ps(), s); "
                        f"std::array<uint8_t, 16> res; memcpy(res.data(), {src1}.data(), 16); memcpy(res.data(), &r, 4); "
                        f"memcpy({dst}.data(), res.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            elif kind == "si2ss":
                expected_ops = 3 if is_vex else 2
                gpr_op = 2 if is_vex else 1
                if (i.op_count != expected_ops or
                    i.op0_kind != K.REGISTER or i.op0_register not in wide_xmm or
                    (is_vex and (i.op1_kind != K.REGISTER or i.op1_register not in wide_xmm)) or
                    i.op_kind(gpr_op) != K.REGISTER or
                    RegisterExt.full_register(i.op_register(gpr_op)) not in REGS or
                    RegisterExt.size(i.op_register(gpr_op)) not in (4, 8)):
                    raise ValueError(f"unsupported si2ss at {i.ip:#x}: {i}")
                gpr_reg = reg(i.op_register(gpr_op))
                gpr_size = RegisterExt.size(i.op_register(gpr_op))
                fn = "_mm_cvtsi64_ss" if gpr_size == 8 else "_mm_cvtsi32_ss"
                cast_type = "int64_t" if gpr_size == 8 else "int32_t"
                dst = wide_xmm[i.op0_register]
                src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
                stmt = (f"{{ const __m128 r = {fn}(_mm_setzero_ps(), static_cast<{cast_type}>({gpr_reg})); "
                        f"std::array<uint8_t, 16> res; memcpy(res.data(), {src1}.data(), 16); memcpy(res.data(), &r, 4); "
                        f"memcpy({dst}.data(), res.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            elif kind == "si2sd":
                expected_ops = 3 if is_vex else 2
                gpr_op = 2 if is_vex else 1
                if (i.op_count != expected_ops or
                    i.op0_kind != K.REGISTER or i.op0_register not in wide_xmm or
                    (is_vex and (i.op1_kind != K.REGISTER or i.op1_register not in wide_xmm)) or
                    i.op_kind(gpr_op) != K.REGISTER or
                    RegisterExt.full_register(i.op_register(gpr_op)) not in REGS or
                    RegisterExt.size(i.op_register(gpr_op)) not in (4, 8)):
                    raise ValueError(f"unsupported si2sd at {i.ip:#x}: {i}")
                gpr_reg = reg(i.op_register(gpr_op))
                gpr_size = RegisterExt.size(i.op_register(gpr_op))
                fn = "_mm_cvtsi64_sd" if gpr_size == 8 else "_mm_cvtsi32_sd"
                cast_type = "int64_t" if gpr_size == 8 else "int32_t"
                dst = wide_xmm[i.op0_register]
                src1 = wide_xmm[i.op1_register] if is_vex else wide_xmm[i.op0_register]
                stmt = (f"{{ const __m128d r = {fn}(_mm_setzero_pd(), static_cast<{cast_type}>({gpr_reg})); "
                        f"std::array<uint8_t, 16> res; memcpy(res.data(), {src1}.data(), 16); memcpy(res.data(), &r, 8); "
                        f"memcpy({dst}.data(), res.data(), 16); {' memset(' + dst + '.data()+16, 0, 16);' if is_vex else ''} }}")
            else:
                if (i.op_count != 2 or
                    i.op0_kind != K.REGISTER or
                    RegisterExt.full_register(i.op0_register) not in REGS or
                    RegisterExt.size(i.op0_register) not in (4, 8) or
                    i.op1_kind != K.REGISTER or i.op1_register not in wide_xmm):
                    raise ValueError(f"unsupported xmm2gpr at {i.ip:#x}: {i}")
                _, is_trunc, is_sd, _ = entry
                gpr_size = RegisterExt.size(i.op0_register)
                in_type = "__m128d" if is_sd else "__m128"
                fn_base = "_mm_cvtt" if is_trunc else "_mm_cvt"
                fn_type = "sd_si" if is_sd else "ss_si"
                fn_bits = "64" if gpr_size == 8 else "32"
                fn = f"{fn_base}{fn_type}{fn_bits}"
                src = wide_xmm[i.op1_register]
                stmt = f"{{ {in_type} s; memcpy(&s, {src}.data(), sizeof s); {write(f'{fn}(s)')} }}"
        elif op in avx_ext_hadd_ops:
            dest = i.op0_register
            valid_regs = wide if dest in wide else wide_xmm
            unary = op in avx_extend_specs or op == M.VPHMINPOSUW
            src_index = 1 if unary else 2
            src_regs = wide_xmm if unary else valid_regs
            src_kind = i.op_kind(src_index)
            src = i.op_register(src_index)
            if (i.encoding != EncodingKind.VEX or i.op0_kind != K.REGISTER or dest not in valid_regs
                    or (op == M.VPHMINPOSUW and dest not in wide_xmm)
                    or (not unary and (i.op1_kind != K.REGISTER or i.op1_register not in valid_regs))
                    or src_kind not in (K.REGISTER, K.MEMORY)
                    or (src_kind == K.REGISTER and src not in src_regs)):
                raise ValueError(f"unsupported packed extension or horizontal operation at {i.ip:#x}: {i}")
            dst = wide_any[dest]
            nbytes = 32 if dest in wide else 16
            zero_hi = f" memset({dst}.data()+16, 0, 16);" if nbytes == 16 else ""
            if op in avx_extend_specs:
                src_sz, dst_sz, signed = avx_extend_specs[op]
                lanes = nbytes // dst_sz
                in_bytes = lanes * src_sz
                in_t = {1: "int8_t", 2: "int16_t", 4: "int32_t"}[src_sz] if signed else UINT[src_sz]
                out_t = {2: "int16_t", 4: "int32_t", 8: "int64_t"}[dst_sz] if signed else UINT[dst_sz]
                rhs = wide_any[src] if src_kind == K.REGISTER else vector(in_bytes)
                stmt = (f"{{ const auto rhs = {rhs}; for (size_t k=0; k<{lanes}; ++k) {{ "
                        f"{in_t} a; memcpy(&a, rhs.data()+k*{src_sz}, {src_sz}); "
                        f"const {out_t} v = static_cast<{out_t}>(a); memcpy({dst}.data()+k*{dst_sz}, &v, {dst_sz}); }}{zero_hi} }}")
            elif op == M.VPHMINPOSUW:
                rhs = wide_any[src] if src_kind == K.REGISTER else vector(16)
                stmt = (f"{{ const auto rhs = {rhs}; uint16_t best = 0xffffu, idx = 0u; "
                        f"for (uint16_t k=0; k<8; ++k) {{ uint16_t v; memcpy(&v, rhs.data()+2*k, 2); "
                        f"if (k == 0 || v < best) {{ best = v; idx = k; }} }} "
                        f"{dst} = {{}}; memcpy({dst}.data(), &best, 2); memcpy({dst}.data()+2, &idx, 2); }}")
            else:  # VPHADDW, VPHADDD, VPHADDSW, VPHSUBW, VPHSUBD, VPHSUBSW
                src1 = wide_any[i.op1_register]
                rhs = wide_any[src] if src_kind == K.REGISTER else vector(nbytes)
                sign = "+" if op in (M.VPHADDW, M.VPHADDD, M.VPHADDSW) else "-"
                if op in (M.VPHADDSW, M.VPHSUBSW):
                    stmt = (f"{{ auto lhs = {src1}; const auto rhs = {rhs}; for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<4; ++k) {{ int16_t a0, a1, b0, b1; "
                            f"memcpy(&a0, lhs.data()+l+4*k, 2); memcpy(&a1, lhs.data()+l+4*k+2, 2); "
                            f"memcpy(&b0, rhs.data()+l+4*k, 2); memcpy(&b1, rhs.data()+l+4*k+2, 2); "
                            f"const int sa = int(a0) {sign} int(a1), sb = int(b0) {sign} int(b1); "
                            f"const int16_t va = static_cast<int16_t>(sa < -32768 ? -32768 : (sa > 32767 ? 32767 : sa)); "
                            f"const int16_t vb = static_cast<int16_t>(sb < -32768 ? -32768 : (sb > 32767 ? 32767 : sb)); "
                            f"memcpy({dst}.data()+l+2*k, &va, 2); memcpy({dst}.data()+l+8+2*k, &vb, 2); }} }}{zero_hi} }}")
                else:
                    ls = 2 if op in (M.VPHADDW, M.VPHSUBW) else 4
                    ut = UINT[ls]
                    pairs = 8 // ls
                    stmt = (f"{{ auto lhs = {src1}; const auto rhs = {rhs}; for (size_t l=0; l<{nbytes}; l+=16) {{ "
                            f"for (size_t k=0; k<{pairs}; ++k) {{ {ut} a0, a1, b0, b1; "
                            f"memcpy(&a0, lhs.data()+l+2*k*{ls}, {ls}); memcpy(&a1, lhs.data()+l+(2*k+1)*{ls}, {ls}); "
                            f"memcpy(&b0, rhs.data()+l+2*k*{ls}, {ls}); memcpy(&b1, rhs.data()+l+(2*k+1)*{ls}, {ls}); "
                            f"const {ut} va = static_cast<{ut}>(a0 {sign} a1), vb = static_cast<{ut}>(b0 {sign} b1); "
                            f"memcpy({dst}.data()+l+k*{ls}, &va, {ls}); memcpy({dst}.data()+l+8+k*{ls}, &vb, {ls}); }} }}{zero_hi} }}")
        elif op in avx_horiz_fp_ops:
            stmt = lower_avx_horiz_fp(i, vector)
        elif op == M.VMOVUPS and i.op1_kind == K.MEMORY:
            stmt = f"{wide[i.op0_register]} = {vector(32)};"
        elif op == M.VMOVUPS and i.op0_kind == K.MEMORY:
            if i.op1_register in wide:
                stmt = f"store({address()}, {wide[i.op1_register]});"
            else:
                source = {R.XMM0: "wide0", R.XMM1: "wide1", R.XMM2: "wide2"}[i.op1_register]
                stmt = f"memcpy(reinterpret_cast<void*>({address()}), {source}.data(), 16);"
        elif op == M.VPXOR and i.op0_register == i.op1_register == i.op2_register and has_wide:
            stmt = {R.XMM0: "wide0", R.XMM1: "wide1", R.XMM2: "wide2"}[i.op0_register] + " = {};"
        elif op == M.VZEROUPPER:
            stmt = " ".join(f"memset({v}.data()+16, 0, 16);" for v in wide.values()) if has_wide else ";"
        elif has_xmm_math and op == M.VMOVDQU and i.op1_kind == K.MEMORY and i.op0_register in xmm:
            stmt = f"{xmm[i.op0_register]} = {guest('__m128i')};"
        elif has_xmm_math and op == M.VPSRLDQ and i.op0_register in xmm and i.op1_register in xmm:
            stmt = f"{xmm[i.op0_register]} = _mm_srli_si128({xmm[i.op1_register]}, {i.immediate(2)});"
        elif (has_xmm_math and op == M.VPADDD
              and all(reg in xmm for reg in (i.op0_register, i.op1_register, i.op2_register))):
            stmt = f"{xmm[i.op0_register]} = _mm_add_epi32({xmm[i.op1_register]}, {xmm[i.op2_register]});"
        elif has_xmm_math and op == M.VMOVD and i.op1_register in xmm:
            stmt = write(f"_mm_cvtsi128_si32({xmm[i.op1_register]})")
        elif op == M.XCHG and i.op_count == 2 and i.op0_kind == i.op1_kind == K.REGISTER:
            # Both values are read before either write; 32-bit forms zero-extend both registers, so
            # 87 C0 (xchg eax, eax) clears the upper half of RAX where the one-byte 90 is a NOP.
            stmt = (f"{{ const word x = {operand(0)}, y = {operand(1)}; "
                    f"{write_register(i.op0_register, 'y')} {write_register(i.op1_register, 'x')} }}")
        elif op == M.XCHG and i.op_count == 2:
            # XCHG with memory is atomic without a LOCK prefix, so it lowers to the host's interlocked
            # exchange instead of a flat load and store. Ordering of the surrounding flat accesses is
            # not modelled.
            n = 1 if i.op0_kind == K.MEMORY else 0
            register = i.op_register(n)
            size = RegisterExt.size(register)
            suffix, host = {1: ("8", "char"), 2: ("16", "short"), 4: ("", "long"), 8: ("64", "__int64")}[size]
            stmt = write_register(register, f"_InterlockedExchange{suffix}(reinterpret_cast<{host} volatile*>({address()}), "
                                            f"static_cast<{host}>({operand(n)}))")
        elif op == M.NOP or (op == M.XCHG and i.op0_register == i.op1_register == R.AX):
            stmt = ";"
        elif (op in (M.SETE, M.SETNE, M.SETA, M.SETAE, M.SETB, M.SETBE) or
              (x87_depth is None and op in (M.SETL, M.SETGE, M.SETLE, M.SETG, M.SETS, M.SETNS, M.SETO, M.SETNO))):
            cond = {M.SETE: "zero", M.SETNE: "!zero", M.SETA: "!carry && !zero",
                    M.SETAE: "!carry", M.SETB: "carry", M.SETBE: "carry || zero",
                    M.SETL: "sign != overflow", M.SETGE: "sign == overflow",
                    M.SETLE: "zero || (sign != overflow)", M.SETG: "!zero && (sign == overflow)",
                    M.SETS: "sign", M.SETNS: "!sign",
                    M.SETO: "overflow", M.SETNO: "!overflow"}[op]
            stmt = write(f"({cond})")
        elif op == M.SETO:
            stmt = write("overflow")
        elif op == M.CALL:
            stmt = calls[i.ip]
        elif op == M.XLATB:
            # AL = [RBX + AL]: iced names AL as the index, and address() would add all of RAX. The sum
            # wraps at 32 bits under 67h (base EBX). The flags and bits 8 to 63 of RAX are unchanged.
            table = f"{reg(i.memory_base)} + static_cast<uint8_t>(rax)"
            if RegisterExt.size(i.memory_base) == 4:
                table = f"static_cast<uint32_t>({table})"
            stmt = f"rax = (rax & ~0xffull) | load_always<uint8_t>({table});"  # a guest read, used once
        elif op == M.RET:
            stmt = "return rax;" if checked is None or x87_depth is None else x87_lower.return_statement(checked)
        elif x87_depth is not None and op in x87_lower.X87_OPS:
            stmt = x87_lower.statement(i, x87_depth[i.ip], address, checked, parity,
                                       "load" if i.op_count == 1 and i.op0_kind == K.MEMORY and in_frame()
                                       else "load_always", status_word)
        else:
            raise ValueError(f"unsupported query operation at {i.ip:#x}: {i}")
        if captured:
            # The guest read comes first, before any write of the instruction. A read whose value nothing
            # uses (CMP or TEST without a flag consumer) is still performed.
            parts = [captured["mem"], stmt] if re.search(r"\bmem\b", stmt) else [captured["mem"], "static_cast<void>(mem);", stmt]
            stmt = "{ " + " ".join(parts) + ("\n}" if "//" in stmt else " }")
        lines.append(f"block_{i.ip:x}: {stmt}")
        if (op not in (M.RET, M.JMP) and
                (index + 1 == len(body) or body[index + 1].ip != i.next_ip)):
            if i.next_ip not in allowed:
                raise ValueError(f"unbound fallthrough at {i.ip:#x}")
            lines.append(f"goto block_{i.next_ip:x};")
    if x87_depth is not None:
        lines.insert(3, x87_lower.declaration(body, checked))
    lines.extend(f"block_{target:x}: {statement}" for target, statement in exits.items())
    return "\n".join("    " + line for line in lines)


def leaf(pe, address):
    for i in Decoder(64, image_bytes(pe, address, 32, executable=True), ip=address):
        if i.mnemonic == M.RET:
            return region(pe, address, i.next_ip)
    raise ValueError("unsupported provider predicate")


def checked_image(path, digest):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"unrecognized {path.name} version-query profile")
    return pefile.PE(data=data, fast_load=True)


def export_body(pe, name, slices):
    owned = function_ranges(pe, export_rva(pe, name))
    if not all(any(lo <= a < b <= hi for lo, hi in owned) for a, b in slices):
        raise ValueError(f"slice outside exported function {name}")
    return [ins for a, b in slices for ins in region(pe, a, b)]


def generate(loader, upscaler):
    # These two build-specific profiles specialize the entry guards to (nullptr, type 4).
    # Recover the record from initializer stores; the on-disk registry is zero-filled.
    with checked_image(upscaler, UPSCALER) as pe:
        init = {i.ip: i for i in region(pe, 0x10bb, 0x1144)}
        record = init[0x1130].ip_rel_memory_address
        fields = {init[addr].ip_rel_memory_address-record: value for addr, value in [
            (0x10d0, init[0x10bb].immediate(1)), (0x10e5, init[0x10e5].immediate(1)),
            (0x10f0, init[0x10de].ip_rel_memory_address)]}
        name = pe.get_string_at_rva(fields[32], 128)
        if not name or not all(32 <= c < 127 for c in name):
            raise ValueError("invalid provider version name")
        vtable = init[0x1105].ip_rel_memory_address
        predicates = []
        for slot, symbol in ((1, "supports_effect"), (2, "supports_device")):
            addr = pe.get_qword_at_rva(vtable + 8*slot) - pe.OPTIONAL_HEADER.ImageBase
            code = lower(leaf(pe, addr), ["rcx = self; rdx = input;"], {}, {}, {})
            predicates.append(f"static word {symbol}(word self, word input) {{\n{code}\n}}")
        globals_ = {init[0x1137].ip_rel_memory_address: "reinterpret_cast<word>(registry)",
                    record: "reinterpret_cast<word>(registry + 1)"}
        # The actual registry occupies one pointer between the decoded start and end.
        if record - init[0x1137].ip_rel_memory_address != 8:
            raise ValueError("unexpected provider registry size")
        calls = {site: f"rax = {fn}(rcx, rdx);" for site, fn in [
            (0x1595, "supports_effect"), (0x15a5, "supports_device"),
            (0x161a, "supports_effect"), (0x162a, "supports_device")]}
        provider = lower(export_body(pe, "ffxQuery", [(0x152b, 0x164a)]),
                         ["r15 = reinterpret_cast<word>(query); rdi = 0;"], globals_, calls,
                         {0x1999: "return static_cast<uint32_t>(rax);", 0x164a: "return 6;"})
    with checked_image(loader, LOADER) as pe:
        # The desktop binding supplies the translated upscaler; frame generation is absent.
        slots = {0x4088: "reinterpret_cast<word>(&present)", 0x40c0: "reinterpret_cast<word>(&absent)",
                 0x40a8: "reinterpret_cast<word>(&present)", 0x40e0: "reinterpret_cast<word>(&absent)"}
        caller = lower(export_body(pe, "ffxQuery", [(0x144b, 0x14b2), (0x14c2, 0x14d5)]),
                       ["rbx = reinterpret_cast<word>(query); rdi = reinterpret_cast<word>(&query->effect);"],
                       slots, {0x1456: "/* selected modules are bound offline */",
                               0x14ac: "rax = provider_versions(reinterpret_cast<Query*>(rdx));"},
                       {0x14b2: "return static_cast<uint32_t>(rax);", 0x150f: "return static_cast<uint32_t>(rax);",
                        0x14e5: "return 6;", 0x14d5: "return static_cast<uint32_t>(rax);"})
    chars = ", ".join(str(b) for b in name + b"\0")
    return HEADER + "\n".join(predicates) + f"""
static const char version_name[] = {{{chars}}};
static const word present = 1, absent = 0;
static const word entry[] = {{0, 0, {fields[16]}ull, {fields[24]}ull, reinterpret_cast<word>(version_name)}};
static const word registry[] = {{reinterpret_cast<word>(entry)}};
static uint32_t provider_versions(Query* query) {{
{provider}
}}
extern "C" __declspec(dllexport) uint32_t query_versions(Query* query) {{
    if (!query || query->type != 4) return 6;
{caller}
}}
"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("loader", "upscaler", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    if args.output.suffix.lower() != ".cpp" or args.output.resolve() in (args.loader.resolve(), args.upscaler.resolve()):
        parser.error("choose a distinct .cpp output")
    args.output.write_text(generate(args.loader, args.upscaler), encoding="ascii")
