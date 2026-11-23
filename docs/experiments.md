# Experiment notes and reproduction commands

Run commands from the repository root. Start with the [project overview](../README.md) and [contribution guide](../CONTRIBUTING.md). Package-specific observations below used local inputs. Community regression tests use fixtures built from this repository.

## Local tools

For optional static dependency inspection, build Microsoft's pinned version of xbdepends from PowerShell with x64 Visual Studio Build Tools, CMake and the Windows SDK.

```powershell
.\tools\setup-xbdepends.ps1
```

Load the toolchain in each new development shell, then configure, build and test.

```powershell
.\tools\dev.ps1
cmake --preset dev
cmake --build --preset dev
ctest --preset dev
```

The tests exercise desktop D3D12 root signature serialization and deserialization. Use the resulting test binary to try the static inspection tools.

```powershell
xbdepends -v .\out\build\dev\d3d12_sdk_check.exe
dumpbin /imports .\out\build\dev\d3d12_sdk_check.exe
```

Build outputs and downloaded tool sources live under `out/`. [Microsoft xbdepends documentation](https://learn.microsoft.com/en-us/samples/microsoft/xbox-gdk-samples/xbdepends/)

[`pe_run_package.py`](../tools/pe_run_package.py) starts a source-built x64 EXE with acyclic bundled DLL dependencies. Pass the path to `pe_map.dll`, a package directory and an EXE path relative to that directory. Use `--system kernel32.dll` or `--shim DLL PATH` for explicit external providers. `--preflight-only` resolves those external exports without mapping any guest image. In execution mode the runner attaches DLLs in dependency order, starts the EXE, and releases the images in reverse. A returning fixture must join any workers before it returns. The runner rejects cycles and self imports. It now resolves bundled ordinal imports,
including ordinal-only (`NONAME`) DLL exports and forwarder chains ending in an
ordinal, using `pe_map_export_ordinal` on the mapped provider. `cpu_native_startup` checks the same two-DLL fixture under this runner and the Windows loader.

External providers with process-lifetime workers can use `--pin-shim DLL` alongside `--shim DLL PATH`. This pins the provider immediately before the first guest image loads, and only in execution mode. Run packages in a child process when using pinned providers. The guest must still join its own workers and quiesce any host calls into guest code before returning. `cpu_package_shim_lifetime` verifies that the selected provider remains loaded after the entry returns and the ordinary provider unloads.

### Scoped content root on Windows

`startup_content_root` creates generated archive bytes in a package directory and different bytes
in a save/config directory. The source-built EXE selects a free drive letter and resolves its
root-level synthetic archive names through a process-local mapping. It opens the package archive
read-only and checks the exact bytes. A file that exists only in save/config returns
`ERROR_FILE_NOT_FOUND` through the content mapping, and a write to the content handle is refused.

An occupied host letter returns `ERROR_ALREADY_ASSIGNED`. After both the successful open and the
missing-file case, the scope releases the mapping and the same drive path returns
`ERROR_INVALID_DRIVE`. The EXE checks that the Windows drive mask is unchanged and removes its
generated files. A host with all drive letters occupied returns skip code 77. This fixture is
independent of the package loader. [Windows drive mask](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getlogicaldrives) · [File open behavior](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)

```powershell
cmake --build --preset dev --target guest_content_root_check
ctest --preset dev --no-tests=error -R '^startup_content_root$' -V
```

## Two-record archive reader from standard startup

The [archive reader](../tests/cpu/archive/reader.zig) uses Zig 0.13.0's standard single-threaded Windows console startup. From the linked entry the translator follows the call into the record loop and the final call to RtlExitUserProcess. It recognizes six-byte linker jump thunks for explicitly bound IAT slots, models termination without fallthrough and propagates a narrow return value through a wider register copy. A control changes the startup to consume EAX instead of AL and must fail translation.

Each record contains a one-byte compressed length and a PKWare DCL stream. The [native codec DLL](../tests/cpu/archive/codec.cpp) builds the unmodified blast.c and blast.h from [Mark Adler's blast 1.3 in zlib v1.3.1](https://github.com/madler/zlib/tree/v1.3.1/contrib/blast), retaining their license notices. The callback stages up to 6144 decoded bytes per record. The translated reader drives allocation, decode, publication, cleanup and the completed count. All three processes share the native codec.

Two distinct eight-byte streams produced 26 identical bytes, two completed records, two allocations and frees, and exit 0 across Windows, pe_map and AOT. With only four bytes of the second payload the decoder requested more input, then delivered one tentative output byte. Only the first record's 13 bytes reached stdout. All three completed one record, freed both allocations and exited 2. The [comparison](../tests/cpu/archive_check.py) checks the full event trace against fixed expectations.

Install requirements-dev.txt before building. On x64 Windows this includes the pinned Zig compiler (about 80 MB download).

```powershell
cmake --build --preset dev --target archive_reader_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_archive_reader$' -V
```

### Publishing a texture from each complete archive record

The [texture reader](../tests/cpu/archive/texture_reader.zig) selects a different publisher in the same Zig record loop. Each successful decode supplies one tightly packed 4×4 RGBA8 image to [consume_archive_texture](../tests/cpu/archive/texture.hpp). At pixel `(x, y)` the red channel cycles through the decoded bytes in row-major order, green is `85*x`, blue is `85*y`, and alpha is 255. Rows occupy 16 bytes. The consumer can use or copy the pixels during the call and returns zero on success. The fixture consumer writes those bytes to stdout.

The [comparison](../tests/cpu/archive_texture_check.py) checks native Windows, the mapped reader and decoder, and the AOT reader and decoder against the known red samples and coordinate ramps. Two valid records produce 128 bytes. Reusing the first texture for the second would change nine red samples. With the second input truncated, blast emits one tentative byte internally. Only the first 64-byte texture reaches the consumer, both records are freed, and all three processes exit 2. [Drawing each record through D3D12](#drawing-each-archive-record-through-d3d12) adds GPU upload and frame readback.

```powershell
cmake --build --preset release --target archive_texture_lifted pe_map
ctest --preset release --no-tests=error -R '^cpu_archive_texture_publication$' -V
```

### Drawing each archive record through D3D12

The same reader image also runs against a [GPU consumer](../tests/cpu/archive/texture_gpu.cpp) built as `archive_codec.dll` in `archive_gpu/`, next to copies of the reader and the blast DLL. The AOT reader links a GPU build of the codec with the translated decoder. On the first record the consumer creates one device, queue and pipeline. For each completed record it uploads the 4×4 texture, draws a full-target quad with a point-clamp sampler onto a 16×16 RGBA8 target, signals the next fence value, waits and writes the readback to stdout. After the last record, `completed()` calls `finish_archive_textures()`, which releases every object and reports the device's remaining references. The stdout consumer's version is empty, so the publication output above is unchanged. [`QuadFrames`](../tests/cpu/archive/quad_frames.hpp) holds the reusable device, pipeline and frame state. It accepts any copyable texture size and format and checks the source size against the copy footprint.

The [comparison](../tests/cpu/archive_texture_gpu_check.py) runs native Windows, `pe_map` and AOT on WARP and on a physical adapter. Each pixel `(x, y)` must equal texel `(x/4, y/4)` of the independent texture reference. The stderr trace must contain the adapter and driver, which every run must report identically, the FNV-1a 64 hash of each staged texture, the completed fence values, the device references left after release, and the exit status. The three `X2W_ARCHIVE_TEXTURE_CASE` controls change only the second record.

| Case | Frames | Trace and exit |
|---|---|---|
| Two valid records | Both match their references | Uploads of both textures, fences 1 and 2, `gpu release 0`, exit 0 |
| Second record truncated | Only the first frame | One upload and fence 1. Both records are freed, the device is released once and the process exits 2 |
| First record truncated | None | No device is created and nothing is released. The process exits 2 |
| `stale`: the second upload is skipped | The second frame repeats the first and differs from its reference in 144 bytes (9 texels × 16 pixels) | `upload skipped`, fence 2, `gpu release 0`, exit 0 |
| `fail`: the second texture is one byte short | Only the first frame | The copy-footprint check refuses the upload before any GPU work. The device is still released with no references left, and the consumer status reaches the exit as 4 |
| `leak`: one more device object is never released | Both match their references | `gpu release 1`, which shows that the release count detects a leaked device object |

The consumer and `QuadFrames` return errors instead of throwing C++ exceptions. In the mapped run, the codec DLL is mapped by `pe_map`. There, a C++ exception thrown and caught inside that DLL escaped the `catch` as `0xE06D7363`, while the same DLL loaded by Windows caught it. Structured exceptions are dispatched through the mapped image, as `cpu_native_seh` shows.

On Windows 10 Pro 22H2 (build 19045) with MSVC 19.52, all 18 runs passed on the in-box WARP (driver 10.0.19041.7811) and on an NVIDIA GeForce RTX 3080 Ti (driver 32.0.16.1047), in the `dev` and `release` builds. The hardware test exits 77 without a physical D3D12 adapter.

```powershell
cmake --build --preset dev --target archive_texture_gpu_lifted archive_gpu_reader pe_map
ctest --preset dev --no-tests=error -R '^cpu_archive_texture_gpu_(warp|hardware)$' -V
```

### Recompiling the blast decoder

The `cpu_native_archive_blast` test starts from the same archive reader and builds the unmodified blast 1.3 C file into a separate scalar x64 DLL with MSVC /Od. The [fixture translator](../tests/cpu/archive/translate_blast.py) follows direct calls from its PE export through the bit reader, Huffman table construction, symbol decoder and decompression loop. It reuses the x64 instruction lifter and frame analysis. Only generated C++ is compiled into the translated decoder. Its initialized data, including relocated table pointers, is separate from the native module. This profile uses per-thread tables, a bounded invocation stack, the six-argument blast ABI and two typed callback targets. The host supplies setjmp/longjmp semantics for a single active recovery point. The file I/O and allocation callbacks remain native.

On the MSVC 19.51 build, tracing the generated callback sites for the two streams reached the input call at RVA `0x11e0` and the final output call at `0x115d`. With the second stream truncated, the input calls returned 4 then 0 bytes, the call at `0x1206` took the longjmp path, and the final output callback received one tentative byte. This byte remained unpublished. The two short streams exercise the final partial-window callback.

The window cases retain a 13-byte first record and use explicit DCL length/distance pairs in the second. Eight 512-byte overlapping copies at distance three span the 4096-byte decoder window. The decoder then reads a distinct final literal. On Windows x64, the native, mapped and recompiled programs all delivered callbacks of 4096 and 4 bytes for this record, published all 4113 archive bytes in order, completed two records and exited 0.

With sixteen copies, the adapter stages the first 4096 bytes and refuses the next 4096 because they exceed its 6144-byte per-record limit. The second record publishes zero bytes, including the previously staged window. All three runners retained the first record's 13 bytes, completed one record, and exited 2. Both cases check the full callback trace and one allocation and release per record.

Windows loads the original decoder for the native run. The mapped child separately maps the original decoder, codec adapter and reader, binding each import to the preceding mapping's export. The AOT reader imports an adapter linked to the translated decoder; PE import checks guard against falling back to the original. Both archive outcomes match the byte and event traces above. A direct callback control on both decoder DLLs refuses output and checks both context pointers, the delivered bytes, and blast's error return of 1.

```powershell
cmake --build --preset dev --target archive_all_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_archive_blast$' -V
```

## CRC-32 console program from the PE entry

`cpu_crc_line` reads the first text line from stdin and writes eight uppercase hexadecimal digits and a newline. Its [MASM entry](../tests/cpu/crc_line/entry.asm) handles input, formatting and exit status. MSVC compiles the [CRC update](../tests/cpu/crc_line/crc_step.cpp) as a separate optimized object. The linker imports the real Windows Universal CRT stdio functions through `ucrt.lib`. The source PE uses the shared [native startup wrapper](../tests/cpu/exe_startup.asm) and exports the returning body as `native_entry`. The wrapper calls `ExitProcess` when Windows launches the EXE and returns the body status when a host maps the image.

The [console translator](../tools/cpu_stdio_exe_aot.py) lifts `native_entry` with no defined argument registers. Its generated EXE embeds the SHA-256 of the input PE, which the comparison checks against the image it launches and maps. On MSVC 19.51 the first refusal before enabling imports was at RVA `0x103d`, the call through the IAT to `getchar`. The translator binds near RIP-relative calls by their IAT slots, DLL and symbol, with the argument widths and defined return bytes of `getchar`, `putchar` and `fflush`. It follows the direct call to the compiled CRC routine and emits both functions from the linked instructions. Only the generated C++ is compiled into the AOT executable. The host supplies the UCRT calls. The test rejects a copy that names a different provider.

The [Windows comparison](../tests/cpu/crc_line_check.py) launches the native EXE, a child running the same PE with `pe_map_run`, and the AOT EXE. The mapped child restores normal UCRT text-mode pipes before entry. All three produced `CBF43926\r\n` for `123456789` and exited 0. They also agreed on CRLF, a final line without a newline, an empty line and a 13,000-byte line. EOF before any input produced no output and exit 1. A read-only stdout handle left the sink unchanged and produced exit 2. A separately linked and retranslated Castagnoli polynomial control produced `E3069283\r\n` on the same standard vector. With a worker held on an event at the startup boundary, the piped cases check the native process handle, EOF on both pipes, output bytes and exit code. The read-only stdout case also checks exit 2 with the worker held. A marker emitted after `pe_map_run` returns confirms that the mapped host regains control.

```powershell
cmake --build --preset dev --target cpu_crc_line_lifted cpu_crc_castagnoli_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_crc_line$' -V
```

### Converting an EXE with one command

[`tools/exe_aot.py`](../tools/exe_aot.py) converts one PE32+ x64 EXE into a recompiled Windows EXE, or stops and reports why it cannot. It runs four stages in order. **header** requires an AMD64 PE32+ executable image. **imports** picks the profile that leaves the fewest imports unbound and lists every unbound import. **translate** runs that profile's translator and records the first refused RVA from the lifter message. **build** compiles the generated C++ with `cl /O2 /MT`. The only profile so far is `console-stdio`, which is the [console translator](../tools/cpu_stdio_exe_aot.py) with its `getchar`, `putchar` and `fflush` bindings. A program outside that profile is refused at a named stage, not partly converted. Each run writes `report.json` with the input SHA-256, the import list, the profile and either the generated paths or the refusal. Before each run the tool removes stale `<stem>_aot.*` outputs, so a refusal never leaves an EXE from an earlier conversion.

```powershell
.\tools\dev.ps1
.\out\pe-env\Scripts\python.exe -B tools\exe_aot.py out\build\dev\cpu_crc_line.exe out\convert --entry-export native_entry
```

`cpu_exe_aot_convert` runs the tool at test time on both CRC programs. The generated C++ must equal the console translator's output byte for byte. The EXE must embed the input SHA-256 and must not import `cpu_exe_io.dll`. It must match the native EXE's stdout and exit status for a line, a final line without a newline, an empty line and EOF. The standard and Castagnoli programs print `CBF43926\r\n` and `E3069283\r\n` respectively. Six negative controls must exit 1, name the expected stage and leave no `_aot` EXE:

| Input | Stage | Reported |
|---|---|---|
| `cpu_exe_original` with its fixture trace imports | imports | `cpu_exe_io.dll!emit_trace`, `read_selector` and `cpu_import_provider.dll!mix_args` |
| `cpu_crc_line` lifted from the PE entry instead of `native_entry` | imports | `cpu_exe_io.dll!finish_exe` from the startup wrapper |
| `cpu_exe_io.dll` | header | not an executable image |
| A text file named `.exe` | header | not a PE image |
| `cpu_crc_line` with `xor esi, esi` patched to `cpuid` | translate | the patched RVA, `0x103b` with MSVC 19.51. It also removes the EXE left by an earlier successful conversion in the same directory |
| A compiler path that does not exist | build | The C++ is kept and no EXE is written |

On Windows 11 build 26300 with MSVC 19.51 (Visual Studio 18), the test passed in the `dev` build. A passing conversion means the program fits the `console-stdio` profile. It says nothing about programs with other imports, TLS, delay imports or unsupported instructions.

```powershell
cmake --build --preset dev --target cpu_crc_line cpu_crc_castagnoli cpu_exe_original cpu_exe_io
ctest --preset dev --no-tests=error -R '^cpu_exe_aot_convert$' -V
```

## EXE startup through the desktop runtime shims

The source-built `cpu_shim_startup` EXE calls a companion DLL that links
against the desktop `xmem.dll` and `d3d12_xs.dll`. The DLL checks that its
process attach ran once before the call. It allocates, writes, queries and frees
a page-backed XMem block, then checks that the freed range is no longer committed
or registered as graphics memory under the desktop policy. It also checks the
unsupported working-set reply for a selector the callers do not pass
(`0x20000002`) and round-trips a tagged root signature. The
resulting descriptor retains its root constants and the desktop flag.

`cpu_native_shim_startup` runs the same EXE and companion under Windows and
under `pe_map`. The mapped host loads both desktop shims from the build and
maps the companion before resolving its entry export for the EXE. The test
checks all six shim requests, the ordered output, exit status 42 and the
mapped host's return marker. Withholding either provider identifies the first
unbound import from that DLL before guest entry.

```powershell
cmake --build --preset dev --target cpu_shim_startup pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_shim_startup$' -V
```

The same EXE drives a compute dispatch when the test supplies a size. The
shared x64 translator starts at the PE entry and follows the internal call
through allocation, the null check, dispatch and release. The AOT runner
records the input image SHA-256. All three runners call the same native
providers. The compute provider submits the repository's HLSL on a D3D12
compute queue, waits for a fence, then copies the GPU readback into the XMem
allocation. The test checks both the bytes and the sequence of import calls.

| Compute request | Readback (hex, in memory order) | Compute frees | Exit |
| --- | --- | ---: | ---: |
| 4,096 bytes | `594634123d463412e546341209463412` | 1 | 42 |
| 8,192 bytes | `597634123d763412e576341209763412` | 1 | 42 |
| `SIZE_MAX` | empty, allocation returned null before dispatch | 0 | 17 |

The verifier checks the size, attributes (`0xa8004000`), seed and word
count against an independent expectation. Virtual addresses may differ per
process. Within each process the returned, 256-byte-aligned pointer must reach
dispatch and release unchanged, and its pages must be free afterward. The
counts above cover the compute allocation. The companion also performs its
existing startup allocation and release in every case.

```powershell
cmake --build --preset dev --target cpu_shim_startup_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_shim_compute_(warp|hardware)$' -V
```

The same EXE drives a raster draw when the test requests it. The draw branch
calls the new `cpu_shim_draw` provider, which renders the repository's 64x64
test triangle offscreen on a D3D12 direct queue, waits for a fence, copies the
render target to a readback buffer and publishes the raw RGBA bytes. The
guest exits 42 only when every one of the 16,384 bytes matches the CPU pixel
rule (`x >= 8 && y >= 8 && x + y <= 63` green, black otherwise, opaque). The
check compares the native EXE, the `pe_map` mapping and the AOT build byte for
byte. A mirrored-X shader variant must exit 20 with a different frame that
still agrees across runners; an unknown draw case must exit 21 with no frame.
The non-mirrored shader object is unchanged, so the existing triangle draws are
unaffected.

For the two-frame case, the guest calls the same draw import twice, with
arguments 6 and 7. The provider keeps one device, direct queue, graphics
pipeline, render target and readback resource. It waits for the first fence
before resetting the command allocator and rebinding the pixel shader's root
constants. All three runners must publish the green triangle followed by a
red triangle. The check compares all 16,384 RGBA bytes per frame with an
independent raster reference and requires completed fence values 1 and 2.
The stale-input control changes the second import argument to 8 and must
return two green frames and exit 20. Argument 9 fails before the second
submission. That control must publish just the first frame, report just fence
1 and exit 24. The mapped host must regain control in all three cases.

`cpu_shim_texture_reuse` extends the same EXE with three fenced draws. One
compute dispatch fills a 2×2 `R32_UINT` texture with packed RGBA8 colors.
The second dispatch changes only texel (1, 0), from green to magenta. The
other three texels serve as guards. Both draws load from the same texture
SRV. The second dispatch transitions that resource from pixel-shader-resource
to UAV, then back before rasterization. The third draw leaves the texture in
pixel-shader-resource state and uses a second pipeline to swap red and blue.

Each runner checks the full 64×64 readback after fence values 1, 2 and 3.
Pixels (12, 12), (30, 12), (12, 30) and (30, 30) observe the four texels.
The stale control skips the partial dispatch. Comparing it against the
updated reference must report the first mismatch at stage 2, pixel (24, 8),
with expected magenta and actual green. The control also checks the full
unchanged second frame and the channel-swapped third frame.

```powershell
cmake --build --preset dev --target cpu_shim_startup cpu_shim_startup_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_shim_(draw|texture_reuse)_(warp|hardware)$' -V
```

The same draw contract also runs the translated compute, mesh and pixel chain
from the [compute-to-mesh texture fixture](#single-group-mesh-entry-specialization).
The draw cases `chain0`, `chain1` and `chain-stale` send the frame to
[`shim_chain_gpu.cpp`](../tests/cpu/shim_chain_gpu.cpp). On one direct queue it
dispatches translated gfx1013 producers that write two packed RGBA8 rows to a
raw UAV, copies them into a 2x2 texture, and draws with the translated indexed
mesh and `image_sample_lz` pixel stages into a 32x32 target. The EXE, the AOT
import table and the `draw_request`/`draw_frame` signatures are unchanged. The
provider publishes all 4,096 RGBA bytes and the 80-byte mesh export trace, and
the guest exits 42 only when both match the case reference.
[The check](../tests/cpu/shim_chain_check.py) builds the shaders and references
from the generators that `compute_mesh_texture_check.py` uses. It requires the
native EXE, `pe_map` and the AOT build to agree on the shim bindings, the exit
status and all 4,176 published bytes, and compares them with the Python
reference.

| Case | Producers dispatched | Bound texture | Exit | Published bytes |
|---|---|---|---|---|
| `chain0` | palette A | A | 42 | match A's reference |
| `chain1` | A, then B | B | 42 | match B's reference |
| `chain-stale` | A, then B | A | 20 | equal to A's frame; against B's reference, 276 covered pixels differ, while all 748 clear pixels and the trace match |

Mesh shader tier 1 is required. Without it the test exits 77, reported as a
skip. `XBOX2WINDOWS_ADAPTER` selects the hardware adapter by part of its name.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_shim_chain_(warp|hardware)$' -V
```

The same provider also renders a textured quad from the same EXE entry. The
8x8 RGBA8 table is an original asymmetric rule (64/64 distinct texels); the
8x8 BC7 texture reuses the repository's own mode-6 block builder and decoded
reference, and the fixture proves the 4 KiB standard-swizzle tiled-to-linear
conversion on the way in. The textured path uses a real vertex buffer, a D3D12
texture with an SRV, a point-clamp static sampler, upload footprints, resource
transitions and the same fence-waited readback. The `tex` and `bc7` cases exit
42 with frames that match the CPU rules byte for byte (`8f9271e5`, `b2d756a4`);
a half-texel UV shift (`texuv`), a corrupted upload (`texbad`) and a two-draw
RGBA8+BC7 scene (`scene`, `d54d504f`) exercise the negative and multi-draw
controls. Passing `tex`, `bc7` and `scene` frames are saved as BMPs (see below).

Reproduce with:

```powershell
ctest --preset dev --no-tests=error -R '^cpu_shim_upload_texture_(warp|hardware)$' -V
```

The `_hardware` variant returns 77 (capability skip) when no physical adapter exists. Set `X2W_SHIM_DRAW_OUT` to choose where the BMPs go.

## Source-built EXE entry to a DLL import

MSVC assembles a small CRT-free [EXE entry body](../tests/cpu/exe_entry.asm) and links it with the existing `call_mix` fixture. The [translator](../tools/cpu_exe_aot.py) takes the fixture's `native_entry` export through `--entry-export`, reads its unwind-bounded instruction range, follows the relative call to `call_mix`, and resolves its IAT slot against `cpu_import_provider.dll!mix_args`. It emits both functions as C++. The translated EXE uses ordinary Windows C++ startup to enter the generated `main`. Both versions use the same native provider and the same I/O DLL.

The [Windows test](../tests/cpu/exe_entry_check.py) sends one byte to each child. The provider records its two incoming integers and writes the computed word through its third argument. The I/O DLL sends the recorded integers to stderr and the eight output bytes to stdout. The test reads both pipes and the process exit code.

| Input byte | Integers received by the DLL | Output bytes in write order | Exit code |
| --- | --- | --- | --- |
| `00` | `0x100000303`, `0xfffffffd` | `11 03 00 00 00 00 00 00` | `0x60d` |
| `01` | `0x30`, `0x06` | `35 00 00 00 00 00 00 00` | `0x57` |

A second build flips the selector's conditional jump. The same translator compiles that EXE, and both inverted programs must exchange the two rows in all three observations. Empty input returns 2 with both pipes empty in all four programs. The ordinary process launch and the first exit code follow the contributor's [mapped-process experiment](https://github.com/R0Y00/Xbox2Windows/issues/165). That experiment covers TLS startup separately.

```powershell
cmake --build --preset dev --target cpu_exe_original_lifted cpu_exe_inverted_lifted cpu_exe_pair_lifted cpu_exe_swapped_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_exe_(entry|call_targets)$' -V
```

The two-target fixture gives each branch its own direct call site. One caller adds seven to the first import argument and the other adds nine. Swapping the targets while keeping the selector and incoming arguments fixed changes the imported arguments, output bytes and process status for each input. Both native and translated variants are checked against these expected observations.

The [nested-call fixture](../tests/cpu/exe_nested.asm) sends the same inputs through an extra internal caller before reaching `call_mix` and its DLL import. The translator follows direct dependencies by RVA and emits each function before its callers. Internal functions use the same two integer arguments, trace pointer and return value as the original fixture. Recursive calls are rejected. The control bypasses `call_mix` and calls the import directly, changing the import arguments, output bytes and exit status for both inputs.

```powershell
cmake --build --preset dev --target cpu_exe_nested_lifted cpu_exe_nested_bypass_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_exe_nested_calls$' -V
```

### Running the same EXE under pe_map

`cpu_native_exe_chain` gives the original EXE and its AOT build the same selector byte as above. A third child maps that exact original EXE, binds its ordinary imports to the two source-built Windows DLLs and calls `pe_map_run`. The child passes the guest's returned DWORD back as its process exit code. All three paths produce the same 16 argument bytes, eight output bytes and exit codes in the table.

`cpu_native_exe_frame_chain` adds a direct call inside the EXE before the selector branch. The existing `call_bare` fixture saves RBX, reserves home space, calls the unexported leaf `bare_mix`, writes its result through the trace pointer and restores its frame. The linker map supplies the leaf bounds for this EXE's SHA-256. The AOT build compiles the switch translator's generated caller and leaf alongside the translated entry. With `y = 5`, the leaf returns `5 * selector - 5`. On Windows x64, selectors 0 and 1 therefore took the opposite paths to the original fixture on all three runners, producing exit codes `0x57` and `0x60d` respectively. Bypassing the framed call changes the import arguments, eight output bytes and exit code for both inputs.

```powershell
cmake --build --preset dev --target cpu_exe_framed_lifted cpu_exe_original_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_exe_(frame_)?chain$' -V
```

`cpu_native_exe_image_chain` extends this same call path into writable image data. The no-unwind leaf reads a RIP-relative bias, stores `5 * selector - 5 + bias` in the image, and reloads the result to select the DLL call. The AOT EXE uses a live mapping of the source EXE away from its preferred base. It checks the complete 64-bit value in that mapping before release. Only the generated code executes the entry and leaf in this runner.

The Windows process, the mapped original and the AOT EXE match the two DLL arguments, eight output bytes and process exit. With the linked bias of 5, selectors 0 and 1 return `0x60d` and `0x57`. Changing just the writable bias to 0 in a copy of the original reverses those results with the same AOT binary. Both the range manifest and generated sources come from the unmodified build.

```powershell
cmake --build --preset dev --target cpu_exe_image_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_exe_image_chain$' -V
```

The first host dependency appears during IAT binding, before any guest instruction runs. Before the startup wrapper was added, the tested MSVC build with an empty binding table stopped at `cpu_exe_io.dll!emit_trace`. Supplying just the I/O DLL stops at `cpu_import_provider.dll!mix_args`. The first import invoked at runtime is `read_selector`.

The PE entry points to a [startup wrapper](../tests/cpu/exe_startup.asm). It calls the same return-based body in both cases. After it returns, the I/O DLL compares the fixture's mapped base with the main process module. A native process exits with the body's DWORD via `ExitProcess`. A host mapping returns that DWORD to `pe_map_run`. The regression starts a worker that signals readiness and stays alive. It checks process termination, both pipe EOFs and exact output and exit status for both selectors and empty input. A separate marker proves that the host gets control back after running the exact same image. A temporary copy with its startup RVA set directly to the body is the negative control, whose live worker and open pipes are sampled before cleanup. [Issue #281](https://github.com/R0Y00/Xbox2Windows/issues/281)

`cpu_exe_start_original` and `cpu_exe_start_no_tls` use the same wrapper. Their PE entry is the unchanged [`exe_startup.asm`](../tests/cpu/exe_startup.asm), which calls the body (`native_entry`) and passes its DWORD to `cpu_exe_io.dll!finish_exe`. The body also stores that DWORD in the exported `body_status` (sentinel `0xFEEDFACE` until it has returned), so a check can read the body's result from a process that never ends. Besides `cpu_import_provider.dll!mix_args` the fixtures import `finish_exe`; `cpu_native_exe_start` compares the sorted import requests of both mapped EXEs and of the lifetime control's mapped run, so the wrapper's binding is checked while the `mix_args` request stays checked. TLS callback order and the no-TLS control are unchanged.

The lifetime control copies each EXE next to `cpu_exe_io.dll` and a `cpu_import_live` provider installed under the imported name `cpu_import_provider.dll`. Inside `mix_args` that provider starts a worker. The worker signals readiness, outlives the body's return by about 2 s and then ends by itself with code 0. The test observes three things separately: the body's return, the native process handle becoming signaled with its exit code (`0x60d` with TLS digits 1, 2 for the original, `0x20d` with digit 2 without TLS, because `finish_exe` ends the process while the worker is alive; in the run on 22 November 2026 the original exited after 42.3 ms and the no-TLS EXE after 35.5 ms, so the worker's 2 s lifetime is not waited for), and the TLS digits decoded from that code. A successful native run also requires both pipes to reach EOF without output. It also checks that the mapped host regains control with the same value from `pe_map_run`. If the process does not end, `exe_process.launch(..., on_unfinished=...)` calls a hook while the process is still alive. The hook locates the EXE among the process's modules by file path and polls `body_status` with `ReadProcessMemory` for up to 10 s until it leaves the sentinel, so the message names the body's return or reports a lingering process (with the entry thread's state and the live threads) next to it. A process that ends with a different code is reported with that code, the elapsed time and the decoded TLS digits next to the expected ones. `finish_exe` runs on the main thread until `ExitProcess`, and a worker that ends does not set the exit code while that thread is alive, so the worker's 2 s lifetime does not make a correct build fail.

The unwrapped control accepts two diagnostics, `process exited 0x0` and `body returned 0x60d` with a lingering process. The second relies on the loader not calling `ExitProcess` after the entry point returns, which was measured only on Windows 11 build 26200. The simple reference starts (original, no TLS) go through the same observation without a worker, and a fifth control (synthetic) feeds the late-exit report a launch row that ended after the hook, and a sixth runs the hang build of `finish_exe` without a worker (`body returned 0x60d`, lingering process). If the process ends between the deadline and the diagnostic hook, the message reports the late exit code instead of a lingering process.

Four negative controls run with the same live worker. In each, the check must fail with the named diagnostic: a temporary copy of the EXE whose PE entry point is the `native_entry` export, so nothing ends the process (`process exited 0x0` after about 2 s, the worker's code, or in 1 of 25 repeated runs a real lingering process: `body returned 0x60d`, entry thread ended, process handle not signaled after 10010 ms with two live threads); a `cpu_exe_io_hang` build of `exe_io.cpp` whose `finish_exe` sleeps forever, installed as `cpu_exe_io.dll` (`body returned 0x60d` and a lingering process); the same process with `body_status` read at an unmapped address (`body state unknown` and a lingering process); and `CPU_EXE_TEST_BODY_HANG=1`, which makes the live provider's `mix_args` block (`body did not return` and a lingering process). The last three simulate failures that were not seen in a real fixture; the unreadable-address control tests the diagnostic branch rather than a natural read failure.

Measured on 22 November 2026, Windows 11 build 26200, MSVC 19.51, CPU-only test, one `ctest --preset dev --no-tests=error -R '^cpu_native_exe_start$' -V` process per run. Unpatched `5889876` (clean detached worktree, no local change): 78 of 80 runs passed and 2 failed. Both failures were in the native launch before any mapped run, with `native launch: original 0x0, without TLS 0x0` (run 9, 60.2 s) and `native launch: original 0x0, without TLS 0x20d` (run 68, 30.2 s), followed by `native launch does not run the TLS callback (null reserved) before the entry point`; a passing run takes about 1.2 s. With this change, `ctest --preset dev --no-tests=error -R '^cpu_native_exe_start$' --repeat until-fail:40` passed 40 of 40, about 15 s per run. That bounds the failure rate only to roughly 7 % at 95 % confidence and does not show it is gone. Control without the fix: the same checker against fixtures that keep `body_status` but start at the body (`/ENTRY:native_entry`, no wrapper) in a scratch worktree failed with `original: process exited 0x0 after 2061.6 ms, expected 0x60d (TLS/entry digits [], expected [1, 2])`, the reported symptom of an EXE that ends with exit code 0 instead of the entry result. The committed controls run on every pass, for example `unwrapped entry control: rejected as expected: original: process exited 0x0 after 2041.8 ms, expected 0x60d (TLS/entry digits [], expected [1, 2])` and `hanging finish_exe control: rejected as expected: original: body returned 0x60d (TLS/entry digits [1, 2]); native process handle not signaled after 3001.9 ms (lingering process; ...)`.

The cause of the exit code 0 on the unpatched fixtures was examined with a throw-away loop of `exe_process.launch()` over both unmodified EXEs (4626 launches in three runs, 2 failures, both in the no-TLS EXE). In one failure, with the launch deadline at 10 s, the process was still alive, its entry thread had ended with `0x20d` (the correct entry result) and exactly one other thread was alive. That thread started at the same ntdll address as an ordinary Windows thread-pool worker and had been created 3.6 ms after the process, before user code. In the other failure the process ended by itself with exit code 0 after 30.03 s. This fits an idle loader or thread-pool worker that keeps the process alive after the entry point returns without `ExitProcess`, with the worker's code 0 becoming the exit status after its idle time of about 30 s. It is a hypothesis: the worker was identified only by its start address, the two observations come from different failures, and the earlier 60.2 s failure was not reproduced.

`cpu_native_exe_two_providers` exercises two ordinary named imports from the same internal caller. The caller feeds the result from `cpu_import_provider.dll!mix_args` into `cpu_exe_second_provider.dll!accumulate_args`. The translator accepts the second provider through an explicit `--provider DLL SYMBOL` binding and verifies each used import against the corresponding DLL. Both calls use two integer arguments and the same three-word trace ABI. Windows, `pe_map`, and AOT are compared against independent expected arguments, output bytes, and exit status for both selectors. Removing the second mapping binding stops before entry, and the translator reports the missing import when its binding is omitted or names the wrong DLL.

```powershell
cmake --build --preset dev --target cpu_exe_two_providers_lifted pe_map
ctest --preset dev --no-tests=error -R '^cpu_native_exe_two_providers$' -V
```

```powershell
cmake --build --preset dev --target pe_map cpu_exe_original_lifted
ctest --preset dev --no-tests=error -R '^cpu_native_exe_(chain|termination)$' -V
```

## Pipeline readiness from shader manifests

`tools/pso_readiness.py` joins the native SHA-256 and stage in the shader extraction manifest with explicit pipeline references. It counts archive records, records with a native hash and unique stage/hash pairs separately. Multiple archive records can refer to the same native code. A hash from one stage does not establish coverage in another stage.

Export the archive manifest with `extract_shaders.py --format native` or `--format both`. Supply a separate JSON file containing pipeline IDs, their effective root signature status and shader references. Here's a synthetic example. The zero hash stands for a missing archive entry.

```json
{
  "pipelines": [
    {
      "id": "example-pixel-pipeline",
      "effective_root_signature": "unknown",
      "shaders": [
        {
          "stage": "pixel",
          "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
        }
      ]
    }
  ]
}
```

Set `effective_root_signature` to `present`, `missing` or `unknown` after accounting for both the embedded signature and any signature provided by the caller. The report accepts normalized references from an inspection tool or a trace and assigns no references from archive proximity.

```powershell
python -B tools/pso_readiness.py out/shaders/manifest.json out/pipelines.json
cmake --build --preset dev --target pso_root_check
ctest --preset dev --no-tests=error -R '^pso_(readiness_check|root_signature_warp)$' -V
```

Add `--llvm <AMDGPU LLVM library>` to disassemble referenced native pixel code and run the existing pixel scanner and lowering probe. Each unique stage/hash pair is assessed once. A known missing root signature takes precedence over shader diagnostics. Missing references, unassessed stages and unknown signature status require more evidence. Passing the pixel probe leaves the pipeline awaiting PSO validation, since the scanner uses synthetic bindings.

The WARP control compiles the source-built compute shader with its root signature stripped. D3D12 rejects creation without a root signature. The same shader creates a PSO when given the matching signature from the embedded build. The control measures PSO creation on the source-built fixture.

### Shader census tiers and what each percentage divides by

Three counts describe different populations, so each one is named here with its source and its key. A percentage is only comparable with another that divides by the same one.

| Count | Population | Source | Key |
| --- | --- | --- | --- |
| 26,503 accepted archive records | Named records in the plaintext archive, before the PSO cache | Independent byte-by-byte enumeration of named records with validated DEFLATE and DXBC framing; `tools/shader_profile_audit.py` reproduces the accepted count through `resource_inventory.scene_records` | End offset plus the 16-byte DXBC digest at container bytes 4..20 |
| 25,889 distinct DXBC header digests | The same 26,503 records, counted by container header | Same enumeration | Container bytes 4..20 alone |
| 21,522 cache index entries | Programs in the console PSO cache, a separate population behind the archive | The 8-byte entries that follow the 0x80 header and the 25,471 × 0x70, 2,693 × 0x18 and 54,576 × 8 tables on cache page 0 | The entry itself: a 4-byte offset from a code-area base followed by a 4-byte allocation span |

The profile tables in this document divide by the 26,503 accepted archive records, and each per-stage row divides by that stage's own count (PS 12,101, CS 2,689, MS 9,282, VS 2,236, GS 195). `tools/shader_profile_audit.py` counts those records and caches classification by the full-container SHA-256. Enumerating a start at every byte yields 26,721 accepted candidates; 218 of them are re-detections, leaving 26,503 records and 25,889 digests. Swapping 26,503 records for 25,889 digests would move a denominator by 614 without measuring anything new, so the two keys stay apart: one selects records, the other selects container bytes.

The 21,522 cache entries sit on the other side of the archive. The last named record ends 170,812 B before the cache begins, and the two disk ranges do not overlap. 21,044 was the earlier extractor result. It was revised because that scan assumed a fixed 40-byte `S_CODE_END` tail and a terminating `S_ENDPGM`, and 478 of the entries fell outside that scan's assumptions: 419 carry the standard 8-byte gate and a normal `S_ENDPGM` yet 407 of them end with a 20-byte tail and 12 carry no `S_CODE_END` fill at all, 58 begin directly with the program body without the gate, and one has no terminator. A further 4,485 programs are not aligned to any old record at all: each starts in the gap between two old records and its tail runs on into the next old record, so the record that follows it is bounded wrongly at both ends. Boundaries now come from the index, which gives 21,522 programs. 26,091 is the number of `S_ENDPGM` instructions in the code area and is not a count of records.

The 218 re-detections are suffix aliases. Each one starts inside the name of the previous record, at a byte whose value equals its distance to the name's terminating NUL, and shares that record's terminator, header and payload. The count does not change either way, because de-duplication keeps the earlier start whenever the end offset and the digest match.

### Reporting shader census populations

`tools/shader_census.py` reports accepted record occurrences, distinct header
digests and full-container SHA-256 counts without running LLVM or changing
`shader_profile_audit.py` output. It reuses the same `scene_records` acceptance
and scan order as the profile audit. With an already decoded cache from the
same build, it also reports index entries, distinct `(code_offset,
allocation_size)` spans and alias counts. The supported cache profile starts
with bytes `50 53 00 09`; the four u32 counts at byte 80 describe 0x70-byte,
0x18-byte, 8-byte and index-entry tables following a 128-byte header. Each index entry is
a little-endian offset and size pair, relative to a caller-established code-area
base. The tool does not guess that base or decompress cache streams.

```powershell
python -B tools/shader_census.py local.archive
python -B tools/shader_census.py local.archive --cache-blob out/decoded.cache --code-base 0x2100000
ctest --preset dev --no-tests=error -R 'shader_metadata|dxcontainer_part_offsets' -V
```

The base in the example is illustrative: replace it with the independently
established offset for that cache. Missing cache input reports `null`, not zero.
The parser checks table extents, a limit of 1,048,576 index entries, nonzero
in-range allocations, 16-byte relative offset alignment and distinct-span
overlaps. Exact aliases remain separate index entries. It does not validate the
programs inside allocations or prove that two supplied inputs share a build.

Run `python -B tests/shader_census_check.py` without local archives or a GPU.
Its source-built framing fixture has four accepted records, two deliberately
colliding header digests, three complete-container hashes and five cache index
entries. It is not executable DXIL. Rejection controls cover malformed cache
tables, allocation bounds and overlaps, invalid archive framing and CLI input.
Separate controls verify alias accounting, missing-cache output and that the
unchanged profile audit still counts all four occurrences while classifying
only the three full-container hashes.

Observed with Python 3.14.4 on Linux: all seven census test methods and all
seven DXContainer controls passed. On Windows 11 build 26300.9974, MSVC
19.44.35230 and Python 3.12.8, the CTest filter above passed 4/4 tests, including
the existing native metadata probe, with no skips. No archive totals were
remeasured for this change.

### Fixed-profile archive rescan

The fixed profiles previously verified 861 records. The Windows readback run at `1aafb0f` revisited all 26,503 records and added a packed RGB PS. Reclassification at `f94c254` reproduced the same 862 passes. A final full scan at `26544c6` reproduced every previous accepted source byte for byte and added one texture-sampling PS. The table incorporates its separate Windows readback.

| Stage | Records | Desktop DXIL | D3D12 pipeline | Output matched on both adapters | Change in each verified column |
| --- | ---: | ---: | ---: | ---: | ---: |
| PS | 12,101 | 849 | 849 | 849 | +2 |
| MS | 9,282 | 2 | 2 | 2 | 0 |
| CS | 2,689 | 12 | 12 | 12 | 0 |
| VS | 2,236 | 0 | 0 | 0 | 0 |
| GS | 195 | 0 | 0 | 0 | 0 |
| Total | 26,503 | 863 | 863 | 863 | +2 |

Each archive record counts once across profiles, inputs and adapters. Generated variants and repeated copies of the same native program can serve multiple records. The graphics checks use controlled partner stages, descriptors and draw inputs. The compute checks use a fixed raw UAV with an independent expected word array.

- **PS.** The accepted sets were disjoint in this input and covered 336 general color, 35 interpolated attribute, 354 two-table depth and 124 sampled coverage. Of the remaining records, 837 stopped at the native program boundary or its control-flow checks and 10,415 reached lowering without matching a binding profile. For those 10,415 records, the first general configuration alone (table s8, interpolation s9, fixed sampler, no CB) first rejected 2,927 for an unresolved constant buffer and 2,007 at `s_setreg_imm32_b32`. A different binding can expose a different next instruction.
- **CS.** The first rejection was unsupported thread-group dimensions for 1,054 records, an unverified multi-exit program for 1,002 and the fixed store lowering for 621. The 12 successes were tested with 320-word readbacks in 14 dispatches on each adapter.
- **MS.** All 9,282 programs supplied a bounded span. As in the preceding survey, the point profile targeted the shortest program (832 bytes) and the indexed profile examined the 531 programs at most 1,200 bytes long. The length-first survey counted 8,751 longer programs. The audit CLI checks the entry before applying the size filter. It rejected 309 of those longer programs for multiple exits. The other 8,442 reached the size filter. All 531 shorter entries passed entry validation. Within the 531, 427 records (24 native streams) first read entry register `s23` without an established binding. One indexed record reached a visible triangle. The shortest, degenerate case was checked using exported positions and indices as well as raster pixels.
- **VS and GS.** Neither stage has a package translator in these profiles. Their first stop is stage dispatch.

Windows 11 build 26200.9974, D3D12 debug layer enabled, NVIDIA GeForce RTX 4060 Laptop GPU 32.0.16.1656 and WARP 10.0.26100.9818. Both adapters completed every accepted record. The sampled coverage group used two separate selector bindings. The mesh and depth checks retained their indexed-export and swapped-table controls.

To repeat the inventory with a local archive and an LLVM 15 library built with AMDGPU support, run the following from the repository root. Use a fresh output directory for each revision. The optional source directory contains local HLSL indexed by profile and decoded container hash. The JSON counts HLSL acceptance; DXC, pipeline construction and readback are separate measurements.

```sh
python3 -B tools/shader_profile_audit.py "$ARCHIVE" --llvm libLLVM-15.so.1 --source-dir out/profile-sources > out/profile-counts.json
```

The first additional PS reads a packed word from the first constant buffer. Its low three bytes select RGB and each byte goes through the piecewise sRGB-to-linear conversion before a round-toward-zero binary16 export. The new readback reference evaluates the scalar transfer function independently. Inputs include byte values 10 and 11 on opposite sides of the threshold, distinct channel values and a change to the unused high byte. All six positive draws across the two adapters matched. All six rotated-channel references failed at pixel comparison.

With the archive scan complete and `offscreen_check` configured, run the following in Developer PowerShell. The source selector identifies the three-byte conversion in this measured corpus and rejects a missing or ambiguous match. The other profiles use the draw and dispatch fixtures documented in their experiment sections.

```powershell
$packed = @(Get-ChildItem out/profile-sources/general/*.hlsl | Where-Object {
    (Get-Content -Raw $_.FullName).Contains('(asuint(params[0].x) >> 16u) & 0xffu')
})
if ($packed.Count -ne 1) { throw "Expected one packed-color PS for this input" }
dxc -Gis -T ps_6_0 -E main -Fo out/packed-rgb.dxil $packed[0].FullName
if ($LASTEXITCODE -ne 0) { throw "DXC failed" }
cmake --build --preset dev --target offscreen_check
python -B tests/packed_rgb_case.py out/build/dev/offscreen_check.exe out/packed-rgb.dxil hardware
python -B tests/packed_rgb_case.py out/build/dev/offscreen_check.exe out/packed-rgb.dxil warp
```

The second additional PS samples RGB and supplies a constant 1 for alpha. The color ramp check covered 4,096 pixels, and the transparent black input produced opaque black for all 1,024 pixels. Both adapters caught red/blue exchange on the ramp and rejected a reference that copied the input alpha. To repeat on the final scan, select the local source with that three-channel sample export and run the existing draw modes:

```powershell
$opaque = @(Get-ChildItem out/profile-sources/general/*.hlsl | Where-Object {
    (Get-Content -Raw $_.FullName) -match 'return float4\(export16\(sample\d+\.x\), export16\(sample\d+\.y\), export16\(sample\d+\.z\), export16\(alu\d+\)\);'
})
if ($opaque.Count -ne 1) { throw "Expected one sampled RGB / constant alpha export" }
dxc -Gis -T ps_6_0 -E main -Fo out/opaque.dxil $opaque[0].FullName
if ($LASTEXITCODE -ne 0) { throw "DXC failed" }
python -c "import struct,pathlib; p=pathlib.Path('out'); (p/'opaque.case').write_bytes(bytes(256)+struct.pack('<4f',0,0,0,1)*1024); (p/'copied-alpha.case').write_bytes(bytes(256+16*1024))"
foreach ($adapter in 'hardware','warp') {
    out/build/dev/offscreen_check.exe $adapter --check-pixel out/opaque.dxil rgba
    out/build/dev/offscreen_check.exe $adapter --check-pixel-attribute out/opaque.dxil out/opaque.case
    # Each control below should exit 1 with pixel_mismatch.
    out/build/dev/offscreen_check.exe $adapter --check-pixel out/opaque.dxil bgra
    out/build/dev/offscreen_check.exe $adapter --check-pixel-attribute out/opaque.dxil out/copied-alpha.case
}
```

### First unresolved constant-buffer read

At `26544c6` the first general profile stopped at an unresolved constant buffer for 2,927 PS records after all profiles had been tried. These records contained 1,925 distinct native streams. All first stops were 16-byte scalar descriptor loads. Tracking scalar moves and the zero high word back to entry gave 1,896 records using s8, 1,011 using s9 and 20 using s10.

| First descriptor location (entry pointer + bytes) | Records |
| --- | ---: |
| s9 + 0 | 565 |
| s8 + 0xa80 | 430 |
| s8 + 0xac0 | 379 |
| s8 + 0x6e0 | 268 |
| s9 + 0x20 | 229 |
| Other locations | 1,056 |

Across the original cohort, the straight-line trace established a completed first consumer for 1,765 records. Immediate-offset reads comprised 508 one-word, 69 two-word, 604 four-word and four eight-word reads. Another 580 read one word with an SGPR offset. For 1,162 records the trace stopped before it could establish a completed first consumer.

In the largest group, **529 of 565** records load the descriptor at byte zero of the table supplied by s9, wait for it, then read four DWORDs from that buffer at bytes 16, 20, 24 and 28. The Windows probe binds that slot to root CBV b0 and a distinct buffer to b1. These are explicit desktop bindings. The PSV0 declarations vary across the group. They do not establish one original HLSL register and space for the traced native slot.

The diagnostic PS exposes the four traced words as repeating depth columns. Its diagnostic export makes each word observable independently of the remaining shader. All other words contain a sentinel. Changing b0 changes the expected picture, changing b1 alone preserves it, and swapping the GPU addresses produces 1,024 mismatches against the old picture and zero against the swapped reference. Both RTX 4060 Laptop (32.0.16.1656) and WARP (10.0.26100.9818) passed the 32×32 D32 readbacks on Windows 11 build 26200.9974 with the D3D12 debug layer enabled.

The full-record scan at `c849f7a` used the original bindings. It had 2,928 records in the first-profile unresolved-CB group. Binding s9 and descriptor byte zero in the existing unsampled profile yields zero complete PS from the largest group. Its most frequent next stop is `v_cvt_u32_f32_e32`. The diagnostic is counted separately.

| Stage | Records | DXIL | D3D12 pipelines | Matched output on both adapters | Delta from `26544c6` |
| --- | ---: | ---: | ---: | ---: | ---: |
| PS | 12,101 | 851 | 851 | 851 | +2 |
| CS | 2,689 | 12 | 12 | 12 | 0 |
| MS | 9,282 | 2 | 2 | 2 | 0 |
| VS | 2,236 | 0 | 0 | 0 | 0 |
| GS | 195 | 0 | 0 | 0 | 0 |
| Total | 26,503 | 865 | 865 | 865 | +2 |

Both additional PS already translated on the new base, using explicit level-zero texture sampling. Each passed the color ramp and rejected a red/blue exchange on each adapter. All previous accepted identities remained present. Across the previous 986 distinct profile/container source files (including selector variants), 616 generated sources were byte-identical and the remaining 370 produced byte-identical desktop DXIL with the same DXC flags. Their existing readbacks therefore cover the same executables.

At `4c3b573` the first-profile unresolved-CB group held 4,411 records in the same input, because the scratch-base register writes no longer stop these programs earlier. A local linear trace of scalar moves and loads found that 81 of them read one 16-byte descriptor through `s_buffer_load_*`, 140 read two, 90 read three and 4,099 read four or more (at most 32), and that 3,711 take their descriptors from three entry registers, 606 from two and 93 from one. With one bound descriptor, whichever table and offset is chosen, 3,565 records next stop at a descriptor load from another entry register and 144 at another offset of the same table.

Binding every entry-table descriptor that the trace saw read through `s_buffer_load_*`, with the `extra_constants` contract described under scalar buffer loads below, takes 3,463 of the 4,411 records past every constant-descriptor stop. Two complete, both with a single bound descriptor at an offset outside the fixed audit profiles. The others next stop at a read of an unbound entry SGPR (1,553), a `v_mov_b32` whose source is not a tracked numeric value (692), an unbound pixel input at `v_cvt_u32_f32_e32` (624), `v_interp_p1_f32_e32` outside the accepted contexts (162), a register-offset `s_buffer_load_dword` (97), an unbound texture descriptor offset (96) and smaller groups. Of the remaining 948, 582 stop at a 16-byte descriptor that no `s_buffer_load_*` reads (a candidate vector-buffer descriptor), 293 at a descriptor whose table address the tracker cannot follow to an entry register, 72 at an immediate read beyond byte 256 and one has no traced descriptor. The trace is linear, 4,250 of these programs branch, and the trace only proposes bindings: `lower` still verifies each load. The fixed audit profiles are unchanged, so the audit totals do not move. The counts say how far lowering proceeds under explicit bindings. They do not establish what a title binds to any of these slots.

Run the fixed scan with LLVM 15, then pass the emitted diagnostic to the Windows readback runner. The CTest exercises the same access using a source-built fixture.

```sh
python3 -B tools/shader_profile_audit.py "$ARCHIVE" --llvm libLLVM-15.so.1 --cb-reads --source-dir out/cb-scan > out/cb-counts.json
```

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_cb_read_' -V
foreach ($adapter in 'hardware','warp') {
    python -B tests/pixel_cb_read_check.py dxc out/build/dev/offscreen_check.exe $adapter out/cb-scan/s9-slot0-read-probe.hlsl
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
```

### Unsigned pixel conversion and entry inputs

The plain gfx1013 `v_cvt_u32_f32_e32` path uses the binary32-to-uint helper from the FFX translator. It truncates positive fractions toward zero, maps NaN and negative inputs to zero, and saturates at `0xffffffff` for inputs at or above 2^32. Sources are a defined VGPR, a ready SGPR or a hexadecimal literal word. Whole-quad execution requires a source defined for helper lanes. A varying conversion cannot establish an affine texture coordinate. [AMD RDNA 2 ISA](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf) · [Shared conversion implementation](../src/ffx_float_to_int.hlsli)

On Windows 11 build 26200.9974, RTX 4060 Laptop 32.0.16.1656 and WARP 10.0.26100.9818 each passed 27 input words in both the entry and whole-quad cases. The fixture extracts four bytes from the result and renders one byte per color channel. All 256 byte values are exactly representable even through the compressed color export. The renderer compares the readback bits with four independently expected bytes at all 4,096 pixels. Inputs include signed zero, subnormals, negative fractions, quiet and signaling NaNs, infinities and both sides of the upper limit. The binary32 immediately below 2^32 yielded `0xffffff00`. Replacing truncation with nearest-even rounding made both 1.875 and 3.75 mismatch on both adapters. LLVM 15 also assembles and disassembles both fixtures in the binary regression.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_aot_u32_from_f32_' -V
```

With the previous fixed bindings and the `--cb-reads` command above, all 530 records (459 distinct native streams) reached the conversion with an unbound source on the `92e80ed` base plus this change. The first conversion reads v3 in 447 records and v2 in 83. **Zero advanced past that instruction and zero completed.** All 530 input signatures report reads of both X and Y of `SV_Position`. The signature identifies logical inputs; assigning either component to a native VGPR still requires entry-layout evidence. In a separate diagnostic, supplying two distinct quad-defined fixture values let all 530 cross both conversions. Their next missing scalar inputs were s17 (505), s15 (23) and s14 (2).

The fixed-profile full scan covered 26,503 records. Output-verified totals stayed at 851 PS, 12 CS, 2 MS, 0 VS and 0 GS (865 overall). All 988 previous profile/container files, including selector variants, recurred. Of those, 616 had identical generated source; recompiling the remaining 372 old/new pairs with the same DXC produced identical desktop DXIL. The existing pipeline and readback results apply to the same binaries. The new conversion draws are counted as source-built fixtures.

### Exec-masked pixel regions

`tools/pixel_aot.py` lowers a single forward masked-if from a known entry mask, the narrow form described in issue #338. A supported `v_cmp` (or integer comparison) records a lane condition, `s_and_saveexec_b64` saves the entry exec mask into an even SGPR pair while intersecting execution with that condition, and the matching `s_or_b64 exec, exec, s[a:b]` closes the region. The region can only contain supported per-lane moves and basic binary32 add, subtract, multiply, min and max: anything that writes scalar state, waits, samples, interpolates, accesses memory, exports, rewrites EXEC or produces scalar reads is rejected while the region is open. On restore every rewritten register merges with a per-lane select between the inner value and the value it held at the save, so outer values are preserved exactly on lanes where the condition is false. A register first defined inside the region is marked conditional; reading it after the merge is refused until an unconditional write defines it on every path.

The optional whole-wave empty-mask skip `s_cbranch_execz` is accepted only as `(offset, text)` pair input: the signed displacement from the next instruction must land exactly on the matching restore, and it must sit immediately after the save. The translator skips no instruction: the skip is elided and the region body lowers unconditionally, which is valid because every allowed body operation is safe to speculate on every lane. `scc` is untracked after both the save and the restore, since the saved mask's emptiness is a wave property a per-lane model cannot compute; `s_cselect`-style readers of it reject. Nesting, additional skips, wrong targets, pending-load overlap with the saved pair, scalar or mask writes inside the region, and programs ending inside an open region all reject.

The source-built fixtures render an 8x8 ramp through three programs: a masked region rewriting two channels, a second region on the complementary predicate covering every lane, and the first program with the skip encoded. Threshold constants 0.5, 10 and -1 exercise mixed, full and empty masks per draw. On Windows 11 build 26300.10075 with the D3D12 debug layer enabled and zero debug errors, `pixel_aot_exec_regions_warp` matched the expected ramp at all 64 pixels in each of the nine cases, and `pixel_aot_exec_regions_hardware` matched identically on a GeForce RTX 5070 (driver 32.0.16.2135). Each adapter also caught a reversed predicate mutant and a mask-free mutant on the mixed threshold. LLVM 15 assembles and disassembles all three fixture binaries in the binary regression, including the labelled branch form.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_aot_exec_regions_(warp|hardware)$' -V
```

The same region can also open with `s_mov_b64 s[a:b], exec` immediately followed by a VOPC `_e32` `v_cmpx` on binary32 or 32-bit integer operands (issue #932): all 16 binary32 and 8 signed and 8 unsigned integer predicates, 32 forms. The [AMD RDNA 2 ISA guide](https://gpuopen.com/download/rdna2-shader-instruction-set-architecture.pdf) (§6.2.2, §12.9) defines `v_cmpx` as writing its comparison to EXEC, and a thread writes only while its EXEC bit is set. From the known entry mask the translator therefore takes the new mask as the entry mask intersected with the comparison, the same lane condition as `v_cmp` plus `s_and_saveexec_b64`. That intersection rule comes from the ISA text and from LLVM's gfx10.3 rewrite of `v_cmp` plus `s_and_saveexec` into this pattern; no gfx1013 hardware run checks it. The save must hold the entry mask (not a whole-quad mask), must directly precede the `v_cmpx` and must not overlap a pending scalar load, and the region closes with `s_mov_b64 exec, s[a:b]` or `s_or_b64 exec, exec, s[a:b]` on the saved pair; the optional `s_cbranch_execz` must sit right after the `v_cmpx` and target that restore. `v_cmpx` writes neither VCC nor SCC, so both keep their tracked values through the region; `s_mov_b64` leaves SCC, while `s_or_b64` writes it from the restored mask, which is untracked, so an SCC reader after the `s_or_b64` close rejects. The region body follows the rules above. `_e64`, SDWA, binary16, binary64, 16- and 64-bit integer and class forms, a VSRC1 that is not a VGPR, source modifiers, chained or nested `v_cmpx`, wave32 saves and else (`s_andn2_b64 exec, s[a:b], exec`) or kill shapes all reject.

The fixtures draw the ramp region with the `s_mov_b64` restore, the `s_or_b64` restore and the skip on mixed (0.5), full (10) and empty (-1) masks, and each must match all 64 pixels; a reversed predicate and a mask-free copy of the body must mismatch on the mixed mask. A second program sets VCC and SCC before the region and reads both after the `s_mov_b64` close; references that take VCC from the `v_cmpx` comparison or invert SCC must mismatch. Eight programs of four regions each draw every accepted predicate against the truth tables of the existing `v_cmp` draws (less, equal, greater and, for binary32, unordered inputs), alternating the two restores; the signed shader against the unsigned truth table must mismatch. Every fixture is also assembled and disassembled with LLVM 15 for gfx1013 and must lower identically. The translation controls cover the rejections listed above, wrong skip targets, an overwritten save pair and unsupported body instructions.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_aot_cmpx_regions_(warp|hardware)$' -V
```

The region body also admits, under either opener, the per-lane binary32 forms the translator already lowers outside regions, in the encodings measured in real regions (stage 2 of issue #932): `v_fmac_f32_e32`, `v_mac_f32_e32`, `v_mul_legacy_f32_e32` and `_e64`, `v_cvt_f32_ubyte0` to `3` `_e32`, `v_rcp_f32_e32`, `v_rsq_f32_e32`, `v_sqrt_f32_e32`, `v_fma_f32`, `v_mad_f32` and `v_med3_f32`. Each lowers exactly as outside a region and still passes the per-instruction check that only VGPRs change; a false lane's speculated value, including an out-of-domain infinity or NaN, is discarded by the restore-time select. The accumulator forms read their destination, so a destination still conditional from an earlier region rejects. Other encodings of these operations (`_e64` of the VOP1/VOP2 forms), other operations and scalar instructions stay refused. Each form has its own draw (`pixel_aot_region_body_warp` and `_hardware`): it rewrites the green channel inside a `v_cmpx` region on mixed, full and empty masks, with inputs whose results are exact, and a reference that applies the body to every lane must mismatch. D3D allows `rcp`, `rsqrt` and `sqrt` an error, so their integer results (8, 4 and 4) and the outer value are rounded after the region with +0.5 and floor. The translation controls lower every form under both openers and check the refusals and the conditional accumulator read; the LLVM 15 round trip covers the fourteen programs.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_aot_region_body_(warp|hardware)$' -V
```

### SPIR-V physical addresses in a D3D12 buffer window

An optional offline experiment accepts validated Vulkan 1.2 SPIR-V assembly with the named user-data and physical DWORD pointer types emitted by [Leonida at `f85582e`](https://github.com/ItsNotRexed/Leonida/blob/f85582e23d4ab9e19acbf694358f137d6b8531b1/tools/rdna2spirv.py). [SPIRV-Cross at `57e4efc`](https://github.com/KhronosGroup/SPIRV-Cross/tree/57e4efcc84112abcd426dc5ec248b5339bee04f2) rejects the physical addressing model on its HLSL path. The [window adapter](../tools/spirv_address_window.py) converts direct DWORD loads, stores and array element addresses to logical storage-buffer accesses. It validates the input and the result before publishing the output module.

The host contract supplies a read-only raw SRV at `t0, space1` containing the original packed user-data bytes and a raw UAV at `u0, space2` covering the address window. Physical addresses supplied to the adapter are byte offsets relative to this view; an upstream heap base added to these offsets must be zero. The descriptor must have at most 1,073,741,823 DWORDs. The final byte offset `0xfffffffc` lies outside the view. An address above the window, an unaligned access, or an overflowing array-index addition selects this offset. D3D12 descriptor bounds then provide a zero read or discarded write. A production runtime still needs to locate each guest allocation, establish its lifetime and supply its real descriptors. This profile makes no claim about arbitrary GPU virtual addresses or atomics.

The [source-built compute shader](../tests/spirv_window/subtract.spvasm) reads two distinct addresses, subtracts their words and writes to a third. Windows checks all 16 words of the arena, including guards, for eight cases. Swapping the two reads reverses the result; moving the store changes which guard is replaced. High addresses, misalignment, 64-bit index overflow and a write past the actual descriptor exercise the window bounds.

The GPU fixture targets `cs_6_0` and requires `Int64ShaderOps`. On Windows 10
22H2 build 19045.6456, the GTX 1060 (32.0.15.6094), in-box WARP (10.0.19041.5794)
and app-local WARP (1.0.22) matched all 128 words across the eight cases in
Debug and Release with zero D3D12 warnings and errors. The original `cs_6_6`
fixture skipped on both in-box adapters and passed the same cases on app-local
WARP.

The SPIR-V bridge GPU tests (`spirv_{address_window,sampler_views,ballot_extract,compaction,compare_sampler}_{hardware,warp}`)
need `spirv-as`, `spirv-val` and `spirv-cross`. `tools/setup-spirv-tools.ps1` builds them from pinned
SPIRV-Tools, SPIRV-Headers and SPIRV-Cross revisions into `out/tools/spirv/bin`, where CMake looks
first. It also searches `PATH` and `$env:VULKAN_SDK\Bin`. Without the tools, the tests are still
registered and report a skip. Both Windows CI jobs build the pinned tools, cached by revision and MSVC
toolset, and set `X2W_REQUIRE_SPIRV_TOOLS=1`, which turns a missing tool or a skip into a failure.
A separate step then requires all five `_warp` tests to have passed. Physical-GPU runs stay manual.

```powershell
.\tools\setup-spirv-tools.ps1
cmake --preset dev
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^spirv_address_window_' -V
```

With SPIRV-Tools on Linux, run `python3 -B tests/spirv_window/check.py` to assemble and validate the fixture and exercise the rejection paths. This also validates a combined fixture where a lowered ballot bit controls the value stored through the address window. The adapter reserves `%x2w_` identifiers in the input and gives address and ballot temporaries separate prefixes. To use WSL tools for the GPU check, generate a logical module with `python3 tools/spirv_address_window.py tests/spirv_window/subtract.spvasm out/window.spv`, then generate HLSL with `spirv-cross out/window.spv --hlsl --shader-model 60 --output out/window.hlsl`. Compile it on Windows with `dxc -T cs_6_0 -E main -Fo out/window.dxil out/window.hlsl` and run `out/build/dev/offscreen_check.exe hardware --spirv-address-window out/window.dxil`, then repeat with `warp`.

### SPIR-V subgroup compaction across lane widths

An optional experiment starts from the optional bridge fixture and compares a source-built subgroup compaction dispatch on a physical adapter and on WARP. The [SPIR-V module](../tests/spirv_window/compaction.spvasm) is one 64-thread group. It computes a sparse predicate from a 64-bit mask, ballots it with `OpGroupNonUniformBallot`, derives the first and last active lane and each lane's compaction rank, and writes the lane index, the observed lane width, the four ballot words, the active count, the selected lane's rank and the compacted payloads into separate output words. Compacted writes go to per-wave disjoint ranges keyed by rank, so the readback does not depend on workgroup or wave scheduling order; a repeated dispatch must be word-identical. The [direct desktop-HLSL control](../tests/spirv_window/compaction.hlsl) implements the same contract independently, so the bridge path and the direct path can be compared word for word. The CPU reference in `compaction.py` is recomputed with the lane width the shader observed, from an independent Python model of the ballot, first/last lanes, ranks and compaction.

Two deterministic masks are injected into the fixture constants. Every 4-bit nibble of both masks has one bit set, so every wave of every supported width (4, 8, 16, 32 and 64) has an active lane. The negative controls flip the predicate bit selection and shift the compaction write index by one; the first divergent word must be the wave-0 ballot word (65) and the first compacted payload (257) respectively, otherwise the test fails.

The original SM 6.6 fixture was measured with SPIRV-Tools 2025.5 (`262bdab`), SPIRV-Cross `57e4efc`, DXC 1.9.2609.5 and MSVC 19.50 on Windows 11 build 26300, debug layer unavailable: the RTX 4060 (driver 32.0.16.2124) reports a 32-lane wave and WARP 10.0.26100.10068 reports a 4-lane wave (`D3D12_OPTIONS1.WaveLaneCountMin/Max` are 4/4). Each adapter matched its own width-aware CPU reference in all 328 words for both masks, on both the bridge and the direct-HLSL path, and both negative controls were caught at the predicted first word. The first difference between the adapters is output word 4 (the lane index each group thread observed): the RTX 4060 wrote 4 where WARP wrote 0, and 259 words differ in total. A CPU reference computed at width 32 reports the same word 4 as the first mismatch against the WARP readback, so the difference is the subgroup width the shader runs at, not WARP ballot or compaction arithmetic. Neither adapter exposes a 64-lane wave here, so an intra-wave 31/32 ballot-word boundary is a capability skip; the 31/32 mask-bit boundary is still exercised across the two 32-lane waves.

Both compute shaders now target SM 6.0. The compaction command requires SM 6.0 and WaveOps, and reports a capability skip (77) when either is unavailable. The 64-thread fixture also skips a reported 128-lane wave. A wrong lane index at a supported width fails the CPU comparison. Each negative control checks all 328 words, including guards, against its mutated reference before checking the first divergence from the original reference.

On Windows 10 22H2 build 19045.6456, the GTX 1060 (32.0.15.6094) ran at 32 lanes, in-box WARP (10.0.19041.5794) ran at four, and app-local WARP 1.0.22 with Agility 1.620.2 ran at eight. Debug and Release each matched the CPU model in both shader paths for both masks on all three backends. Repeats were identical, both controls diverged at words 65 and 257 as expected, and every dispatch reported zero D3D12 debug warnings and errors. No 64-lane wave was observed, so the intra-wave 31/32 boundary remains untested in these runs. [Wave operations](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/shader-model-6-0) · [Original replay follow-up](https://github.com/R0Y00/Xbox2Windows/issues/768)

```powershell
.\tools\setup-spirv-tools.ps1
cmake --preset dev
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^spirv_compaction_' -V
python -B tests/spirv_window/compaction.py out/tools/spirv/bin/spirv-as.exe out/tools/spirv/bin/spirv-val.exe out/tools/spirv/bin/spirv-cross.exe <dxc> out/build/dev/offscreen_check.exe hardware
```

`offscreen_check.exe <adapter> --spirv-compaction <dxil>` prints every one of the 328 observed words, so a mismatch can be placed before or after ballot, rank and address calculation.

## Offline CPU leaf recompilation

With the Python-enabled Windows dev preset configured, run:

```powershell
cmake --build --preset dev --target cpu_leaf_original_lifted cpu_leaf_inverted_lifted
ctest --preset dev --no-tests=error -R '^cpu_leaf_aot_branches$' -V
```

MSVC's x64 assembler and linker build [the original function](../tests/cpu/branch.asm) into a DLL. The [translator](../tools/cpu_leaf_aot.py) reads its exported entry RVA and the matching runtime-function interval from the PE. It translates those bytes into C++, which MSVC compiles to a separate x64 object and links into a second DLL. The intermediate source is in `out/build/dev/cpu_leaf_original_lifted.cpp`.

The supported signature takes an unsigned 32-bit integer in ECX and a writable word pointer in RDX, and returns a 32-bit integer in EAX. The decoder covers the fixture's register move, immediate compare (`imm8` sign-extended and `imm32`) paired with unsigned/equality (`JB`/`JAE`, `JE`/`JNE`, `JBE`/`JA`) and signed (`JL`/`JGE`, `JLE`/`JG`) short conditional branches, short forward jump, immediate add/subtract, output store and return. It rejects unknown encodings and branches outside the decoded instruction boundaries. Leaf functions can omit unwind metadata. This experiment uses an explicit interval provided by the assembler.

The Windows test calls both DLLs with the same 367 inputs covering `<`, `==`, and `>` at every unsigned and signed threshold, including the signed/unsigned boundary (`0x7fffffff`, `0x80000000`) and wraparound. It compares the returned word, the stored word and guards on either side, and checks the original against an independent reference model. A control assembles the same source with every conditional jump inverted (`INVERT_BRANCH`) and runs that PE through the same translator. Its results must differ from the original on every input. [Direct-call follow-up](https://github.com/R0Y00/Xbox2Windows/issues/22)

## Chained function ranges and early returns

The range reader groups runtime-function entries by their complete unwind-chain parent records. For chained trailer lookup it accepts both version 1 and version 2 `UNWIND_INFO` headers when `UNW_FLAG_CHAININFO` is the only flag set, without decoding general version-2 unwind codes. The early-return translator uses iced-x86 to decode each owned interval separately. It follows instruction-aligned branches and fallthrough, models stack slots and preserved registers, and checks their restoration at each return. A neighboring function can occupy the gap. The single-interval translators report split functions explicitly. [Microsoft chained unwind information](https://learn.microsoft.com/en-us/cpp/build/exception-handling-x64#chained-unwind-info-structures)

```powershell
cmake --build --preset dev --target cpu_chained_lifted cpu_chained_jne_lifted cpu_chained_zero_lifted cpu_chained_imm_lifted cpu_chained_frame_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_chained_ranges$' -V
```

The source-owned fixture has a primary interval, two levels of chaining and a neighboring export, built in `JE`, `JNE`, self-`XOR` zeroing, 32-bit register write (`r8d`/`edx` with 64-bit `lea` offsets, so a missing zero extension changes the result), and 32-bit immediate stack-frame (`sub`/`add rsp, 136`) variants. Both null inputs return 7 (or 0 in the `XOR`-zeroing variant), and three non-null inputs (1, 2^64-1 and 2^32) return 21. A Windows probe compares native returns with the generated result, physical RBX/RSI, and guards. A patched copy verifies version-2 chained header resolution. Negative controls cover broken and cyclic parent links, unsupported chained header versions and combined flags, an unsupported conditional branch after `TEST`, a non-self `XOR`, clobbering an unsaved callee-saved register with a 32-bit immediate, a stack frame exceeding 4096 bytes, jumps into a neighbor or instruction interior, an indirect call, and three bound-callback misuses (a call through another table slot, a read of volatile RDX after the callback, and a reload of the callee's home slot after the callback).

The translator reads each `MOV` memory operand once, in guest order, into a local declared at that point, through a `volatile` load: the original faults on an inaccessible operand even when the value is overwritten before use, and with an ordinary load an optimized MSVC build drops such a read. A load whose value nothing uses is written as the declaration followed by `static_cast<void>(...)`. Reads of the modelled stack slots are not emitted, and the table and allocator words that a bound callback or destroy call compares keep their ordinary read, since the comparison uses the value.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_aot_chained_dead_reads$' -V
```

The [dead-read fixture](../tests/cpu/chained_dead_read.asm) exports `read_dead` (a load overwritten by `MOV RAX, RDX`), `disp_dead` (a load at RCX + 8 into a register nothing reads), `pair_dead` (two loads, neither used), `read_skip` (a load skipped when y is zero) and the control `read_live`, which returns the value read. Each export is translated on its own and built by the project's build and again by a separate `cl /O2` command. `cpu_aot_chained_dead_reads` calls the original and both translations inside `__try` on a fresh page followed by an inaccessible page, and compares the structured exception code and the result. A readable operand returns the reference. A null operand, and an operand whose last read crosses one byte into the inaccessible page, raise an access violation in the original and in both translations; reads that end exactly at the page boundary return. On the path that skips the read, the same inaccessible addresses return normally. A source control checks that each memory operand is read once through the `volatile` load. An unoptimized build performs an ordinary load as well, so the `/O2` builds are the ones that show the difference.

For the local FidelityFX loader, run `cpu_chained_aot.py <loader.dll> ffxDestroyContext out/local_context.cpp --partial` using the Python dev environment. Compile the generated C++ as a Windows x64 DLL. Its `try_lifted(first, second, output)` returns 1 and writes the guest result for a completed path. Status 0 leaves the output untouched at an unsupported indirect call. Pointer arguments must reference readable fixture storage on paths that dereference them. The local inspection recovered four owned intervals. Running the generated code produced 6 for a null first argument and for a pointer to a null context. A valid non-null control stopped at the first indirect call with its output sentinel intact. The original package DLL was inspected as a file. Native differential results above are from the repository fixture. [Callback follow-up](https://github.com/R0Y00/Xbox2Windows/issues/29)

### Object-table callbacks on Windows

The source-built caller follows a handle to its context and loads a table pointer from that context. Slot 1 takes the context address and a 64-bit input and returns a 64-bit value. The test selects two tables with the providers in opposite orders. It compares the observed provider, both arguments, return and output guards with native Windows execution, including inputs with bit 63 set. Both null paths return 6 without calling either provider.

```powershell
cmake --build --preset dev --target cpu_callback_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_object_callback$' -V
```

For this fixture, `cpu_chained_aot.py` takes `--callback-offset 8` and emits `try_lifted_callback(first, second, output, binding)`. The host supplies readable fixture storage and a binding containing the expected table address and typed function pointer. Dispatch requires both the table address and the selected slot's target to match. A missing or mismatched binding returns status 0 and leaves the output untouched. The existing three-argument partial profile still stops at an unbound indirect call.

### FidelityFX destroy provider and cleanup

In the inspected Xbox loader, `ffxCreateContext` masks the effect ID and calls the internal resolver at RVA `0x1000`. For effect `0x10000` the resolver gets or loads `amd_fidelityfx_upscaler_xbox.dll`. It resolves the named export `ffxDestroyContext` and stores the address at loader RVA `0x4098`, slot 1 of the table at `0x4090`. After a successful provider create, the loader puts that table pointer and the provider context in the two words of a wrapper. The packaged upscaler export directory independently identifies the destroy entry at provider RVA `0x13a0`. The frame-generation route uses another table and its module is absent from this package.

The loader passes the **address of the wrapper's provider-context field** in RCX and the optional allocation-callbacks pointer in RDX. The upscaler dereferences the first argument and saves the second in a local slot. Its internal destroy method receives the effect object, the context-field address and the address of that saved allocator pointer. On the provider-object release path the upscaler also uses the allocator for deallocation or calls CRT free. These register uses agree with AMD's two-argument destroy declaration and unsigned 32-bit status. [AMD FidelityFX API header](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/main/Kits/FidelityFX/api/include/ffx_api.h)

After provider destroy, the **loader** frees its own wrapper, including when the provider returned an error. Custom cleanup receives the allocator's user data and the wrapper address. The default branch calls the imported CRT free. Both branches return the saved provider status. The explicit `--ffx-destroy` profile binds a stable provider table, allocator and cleanup routines for the duration of the call. The host must supply live storage compatible with the chosen deallocator. An unresolved binding stops before invoking the provider.

```powershell
cmake --build --preset dev --target cpu_destroy_lifted cpu_destroy_provider
ctest --preset dev --no-tests=error -R '^cpu_aot_destroy_contract$' -V
```

CI compares a source-built native caller with its static translation. On Windows the same harness also compared that native reference with the translation of all four owned ranges of the local loader export. Both null exits returned 6. With a source-built provider resolved by export name, both cleanup routes preserved 0, 6, 0x80000001 and 0xffffffff. The test checks the addresses passed to destroy and custom cleanup, and that the provider clears the inner field before the wrapper is released. The packaged upscaler's own effect teardown was inspected statically.

To repeat the local comparison in a VS x64 developer shell, translate the loader with `tools/cpu_chained_aot.py <loader.dll> ffxDestroyContext out/local_destroy.cpp --ffx-destroy`, compile it with `cl /nologo /LD /O2 /Fo:out/local_destroy.obj out/local_destroy.cpp /link /OUT:out/local_destroy.dll /IMPLIB:out/local_destroy.lib`, and run `tests/cpu/destroy_check.py` with absolute paths to `cpu_destroy.dll`, `local_destroy.dll` and `cpu_destroy_provider.dll`, in that order.

### First device calls in the packaged upscaler

For the upscaling effect (`0x10000`), the local upscaler's `ffxCreateContext` export at RVA `0x11d0` takes an output handle pointer in RCX, a descriptor chain in RDX, and an optional allocator in R8. On the inspected path, the backend extension has type `0x01000002` at offset 0, a next pointer at +8 and the device pointer at +16. The provider selection call at `0x129c` passes that pointer to a predicate that returns true without reading it. At `0x1349` the export calls the selected provider's create method at `0xc9b0`.

The provider reads the device from the extension at `0xcdca`, sets up a fresh 0x18f160-byte scratch buffer for one context and invokes its backend callback at `0x2720`. At the call site RCX points to the backend interface, EDX is `0x14`, R8 points to four 32-bit words `{0, 0, 0, 1}`, and R9 points to storage for the output context index. The callback reads the device at interface +0xd0 and scratch at +0xc0. On its first use, the device receives `IUnknown::AddRef(this)` at `0x2803`. It takes only the device in RCX. The callback stores the device at scratch +8 and continues without inspecting the returned reference count.

The first resource creation at `0x2893` uses device vtable slot 14 (+0x70). RCX holds the device, RDX points to a 16-byte `D3D12_DESCRIPTOR_HEAP_DESC`, R8 points to the IID of `ID3D12DescriptorHeap` (`8efb471d-616c-4f49-90f7-127bb763fa51`), and R9 points to the output slot. All five calls request node mask 0. [D3D12 CreateDescriptorHeap](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-createdescriptorheap) · [IUnknown AddRef](https://learn.microsoft.com/en-us/windows/win32/api/unknwn/nf-unknwn-iunknown-addref)

| Call RVA | Heap type | Descriptors | Shader visible | Output in scratch |
|---|---|---:|---|---|
| `0x2893` | CBV/SRV/UAV | 512 | No | +0x28 |
| `0x28ac` | CBV/SRV/UAV | 512 | No | +0x30 |
| `0x28cd` | CBV/SRV/UAV | 512 | Yes | +0x38 |
| `0x28fd` | CBV/SRV/UAV | 167,936 | Yes | +0x48 |
| `0x2935` | RTV | 8 | No | +0x20 |

On Windows the probe starts at RVA `0x27f8` with a live device, zero-initialized scratch, a context capacity of one and R15 zero. It executes the offline translation of the package's argument construction and binds the two COM methods to desktop D3D12. The RTX 4060 Laptop (driver 32.0.16.1656) and Microsoft WARP (10.0.26100.9818) each returned `S_OK` five times. The probe verified five distinct live heap handles, their output slots and their descriptions. Both debug-layer runs reported zero errors and zero warnings. The host releases the five heaps and its device reference on exit.

After the fifth heap the test continues into the backend slot scan at RVA `0x2938`. The host stops on a failed HRESULT so that a desktop failure identifies the exact heap. The package slice itself has no intervening HRESULT branches.

To run the local translation in a VS x64 developer shell, set `$upscaler` to the full path of the package's upscaler DLL and use a separate build directory. The translator checks the image hash before generating C++.

```powershell
cmake --preset dev -B out/build/ffx-device "-DFFX_UPSCALER_IMAGE:FILEPATH=$upscaler"
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_device_init_' --no-tests=error -V
```

The log identifies this run with `source=translated-package`. The default build uses a source-owned desktop control for the five D3D12 calls (`source=desktop-control`). Public CI runs that control on WARP and compares native and translated x64 heap-size arithmetic for six inputs, including 32-bit wraparound.

### Backend context slot selection

The provider takes the address of its 32-bit index field at +0xec (`0xcf94`) and passes it in R9. The backend callback saves that pointer in R14. On first use it finishes setting up the shared descriptor range at `0x2938`. Subsequent calls skip heap creation and rejoin at `0x2942`. The callback increments the 32-bit scratch counter before checking capacity, then scans from index zero. The table pointer is at scratch +0x1078, records are 96 bytes each, and bit 0 of the byte at record +0x3c means occupied.

At the first free slot, the store at `0x2980` writes the index directly to the provider. The callback writes effect `0x14`, sets flag bit 0, clears bit 1 and preserves the other six bits. It initializes two pairs of descriptor cursors to `512*i+1` and `512*i+511`. On the package's one-slot input, the four sizes `{0,0,0,1}` reserve the half-open range `[0x19000,0x19001)`. When all slots are occupied, the scan branches to the same zero-return exit at `0x2a9f` without writing a record or the output index.

| Windows input | Backend return | Provider index after call | Scratch counter | Slot change |
|---|---:|---|---|---|
| Fresh package table after five heaps | 0 | 0 | 0 → 1 | Slot 0 becomes occupied |
| Same table, second request | 0 | `0xfeedface` preserved | 1 → 2 | None |
| Controlled two-slot table, slot 0 occupied | 0 | 1 | 1 → 2 | Slot 1 becomes occupied. Its range starts after slot 0 at `0x32008` |
| Both controlled slots occupied | 0 | `0xfeedface` preserved | 2 → 3 | None |

The package translation and an independently compiled C++ control ran on the RTX 4060 Laptop and WARP using the commands above. The test compares the entire scratch buffer and provider output region, including bytes around the index. The exhaustion cases supply a new output field initialized to the sentinel. The dual-slot setup exercises a nonzero index with a controlled capacity and four nonzero sizes. Public CI runs the desktop control plus a source-built x64 scan and four-lane sum in `cpu_aot_heap_layout`. The same fixture compares 32-bit registers with sign-extended 8-bit immediates such as `cmp ecx,-1`, which the lowerer evaluates at the operand width.

The provider tests EAX at `0xcfc2`. A zero backend return continues initialization. It later loads the same index field into R8D at `0xd1f4` for the resource callback at `0x2e30`. On the default path the first descriptor identifies `FSR4UPSCALER_Recurrent` (ID 13), with its allocation mode and placed-resource flag both zero and the optional allocation callback unset. The intervening descriptor-builder call at `0xc4ff` resolves to a CPU-only helper at `0x7900`, and the import at `0x2ea1` is an XMem working-set query. The next call **on the device** is at `0x33cc`, Xbox vtable slot 76 (+0x260). Its arguments include heap properties, zero heap flags, a resource description, initial state, a null clear value, the `ID3D12Resource` IID and an output pointer. Those arguments match the shape of desktop committed-resource creation. The Windows replay below binds that call to the desktop device. [Desktop CreateCommittedResource signature](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-createcommittedresource)

### Creating the first recurrent texture

For the default provider, the builder at `0xbf60` receives the output-size pair in R8. The create descriptor supplies render width/height at +0x14/+0x18 and output width/height at +0x1c/+0x20. The first resource uses the latter pair and has ID 13 and name `FSR4UPSCALER_Recurrent`. The local replay translates the first descriptor stores, the provider's index-output address calculation and the default committed-resource path. It supplies zero for the optional Xbox working-set statistics and uses the default allocator route with the optional resource allocation callback unset.

| Field | Package path | Desktop D3D12 argument |
|---|---|---|
| Dimensions | Kind 2, output width and height, one layer, one mip | Texture2D, one sample, quality 0 |
| Format | Format code 10 selects value 28 in the format switch at `0x2160` | `DXGI_FORMAT_R8G8B8A8_UNORM` |
| Flags | Usage bits 0 and 1 produce flags 0x5 | `ALLOW_RENDER_TARGET` and `ALLOW_UNORDERED_ACCESS` |
| Initial state | Provider state 2 selects value 8 at `0x325a` | `D3D12_RESOURCE_STATE_UNORDERED_ACCESS` |
| Allocation | Default heap, zero heap flags, null optimized clear value | Committed resource, default alignment, single-node masks 1/1 |
| Layout | Xbox texture layout 0x101, extension word 3 and trailing zero | Driver-chosen `D3D12_TEXTURE_LAYOUT_UNKNOWN`. The adapter checks the Xbox extension fields before omitting them from the desktop descriptor |

With a render size of 1280×720, the RTX 4060 Laptop (driver 32.0.16.1656) and WARP (10.0.26100.9818) each ran two fresh contexts. For outputs of 1920×1080 and 1600×900, all four calls returned `S_OK`. Each resource's `GetDesc` reported the requested output dimensions, format 28, flags 0x5, one layer/mip/sample, quality zero, desktop layout 0 and 65,536-byte alignment. Initial state is a creation argument. The probe transitions the texture from UAV to shader read, then copy source, then back to UAV and waits for completion. Both debug layers reported zero errors and zero warnings.

On context slot 0 the provider passes +0x17c as the output for ID 13. The resource callback takes cursor 1, advances it to 2 and writes 1 to the output **before** the device call. On success it records provider state 2 in both state fields and stores the returned COM pointer in backend record 1 at `0x3408`. The test checks that pointer against the live texture and verifies all adjacent resource indices retain their sentinels.

Use the package build directory configured above in a VS x64 developer shell. Public CI runs the source-owned WARP fixture with the same desktop mapping and readback checks. It also compares native and translated descriptor-copy instructions using the MASM fixture.

```powershell
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_recurrent_' --no-tests=error -V
```

### Recurrent texture views and pixel readback

After the resource pointer is stored, the callback reads its description and builds views for the single-layer, single-mip Texture2D path. The SRV and UAV format switches both preserve `DXGI_FORMAT_R8G8B8A8_UNORM` (28). The SRV exposes mip 0 only, plane 0, minimum LOD 0 and the default RGBA component mapping (`0x1688`). Both UAV descriptors select mip 0 and plane 0 and use a null counter.

| Device call RVA | Descriptor | Heap pointer in scratch | Slot in fresh context 0 |
|---|---|---|---:|
| `0x37aa` | SRV | +0x28, CPU-only | 1 (resource index) |
| `0x3844` | UAV | +0x38, shader-visible | 1 (UAV base + mip) |
| `0x388a` | UAV mirror | +0x30, CPU-only | 1 (UAV base + mip) |

The resource record stores the UAV base at +0x34 and the count at +0x38, both 1. After creating both UAV descriptors, the callback advances the context's UAV cursor at +0x0c from 1 to 2. Each destination is the heap's CPU start plus the slot times the device-reported descriptor increment. That increment was 32 bytes on the RTX 4060 Laptop and 64 bytes on WARP.

The same `ffx_recurrent_hardware` and `ffx_recurrent_warp` commands above run the pixel check. A source-built compute shader writes a coordinate-dependent RGBA pattern through the original shader-visible UAV. A second pass writes a different pattern through a copy of the CPU UAV mirror. For shader access the probe copies the CPU-only SRV and UAV mirror into a separate, shader-visible test heap. A read shader samples the SRV at texel centers and writes packed RGBA bytes to a buffer. A separate `CopyTextureRegion` reads the original resource pointer directly. After a fence, both paths must match the CPU's expected RGBA value for every pixel. [D3D12 descriptor copying](https://learn.microsoft.com/en-us/windows/win32/direct3d12/copying-descriptors)

Both adapters passed both patterns at 1920×1080 and 1600×900. Each adapter checked 7,027,200 pixels through the SRV and the same number through direct copies, with zero mismatches, debug errors or warnings. The local build executes the package-derived view creation through the cursor update at RVA `0x389b`. Public Windows CI runs the source-owned WARP control. The test shaders exercise descriptor access. The provider loop is covered below.

### Provider resource loop and returned handles

At 1280×720 render and 1920×1080 output, the default provider visits twelve candidate descriptors in order. The builder at RVA `0xbf60` emits 104 bytes per candidate. The loop at `0xd1d0` skips ID 23 when bit 9 of the create flags is clear. The index destination is `provider + 0x148 + 4 * ID`. Three callbacks also allocate an upload resource from the same cursor.

| ID | Resource (FSR4UPSCALER_ prefix) | Shape | Default index | Index with flag 0x200 |
| ---: | --- | --- | ---: | ---: |
| 13 | Recurrent | 1920×1080 RGBA8 | 1 | 1 |
| 14 | History | 1920×1080 RGBA16F | 2 | 2 |
| 15 | HistoryReprojected | 1920×1080 RGBA16F | 3 | 3 |
| 16 | RcasIntermediary | 1920×1080 RGB9E5 | 4 | 4 |
| 5 | AutoExposure | 8-byte raw buffer | 5 | 5 |
| 17 | DefaultExposure | 1×1 RG32F | 7 | 7 |
| 6 | SpdAtomicCounter | 1×1 R32_UINT | 9 | 9 |
| 7 | Luma_Mip_5 | 80×45 R32F | 11 | 11 |
| 22 | ScratchBuffer | 21,045,760-byte raw buffer | 12 | 12 |
| 20 | InitializerBuffer | 131,072-byte raw buffer | 13 | 13 |
| 21 | InitializerBuffer_Upload | 131,072-byte upload buffer | 14 | 14 |
| 23 | DebugInformation | 1920×1080 R16_UNORM | 0 (skipped) | 15 |

The uploads for IDs 5, 17 and 6 occupy slots 6, 8 and 10 respectively. ID 21 is a named provider resource with its own index. The package row loop treats the zero height of ID 5 as zero rows to initialize, despite its eight-byte input payload. ID 17 supplies two float ones and ID 6 supplies a zero fill. The desktop binding uses committed resources, creates SRVs and paired UAVs for eligible resources, and submits the initialization copies with a fence. Default-heap buffers use desktop COMMON state and implicit promotion.

On success, the translated loop copies all 25 indices to the mirror at +0x1ac and writes the provider pointer through the output argument at RVA `0xd263`. The loader returns a separate 16-byte wrapper containing the callback table and that same provider pointer. With flags 0 and 0x200 both adapters returned 0, with next resource indices 15 and 16 respectively. Injecting a callback return of 7 at ID 20 returned 3 through the provider and loader, left both output handles null, preserved the previous resource indices, and left the mirror untouched. The test checks every live backend record against its D3D12 object, both index tables, and the loader wrapper fields. All three cases completed with zero debug errors or warnings on the RTX 4060 Laptop and WARP.

The first desktop format difference occurs at ID 16. WARP exposed typed RGB9E5 UAV stores. The RTX driver (32.0.16.1656) lacked that capability, so the binding created an R32_UINT texture and views for the packed bits. The texel test below checks the two storage formats.

The local build translates the descriptor builder, debug flag, loop and writeback, resource-slot reservation, upload-descriptor construction, and selected create-export branches. Registry selection and the backend device callbacks are bound on the host. When the pinned loader DLL is beside the upscaler DLL, the probe also executes the recompiled loader create path against that provider. Public CI runs the source-owned provider control on WARP. Configure the local package build as in the recurrent texture test above and run:

```powershell
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_provider_loop_' --no-tests=error -V
```

### RCAS intermediate texel access

The provider-loop CTests above also exercise resource ID 16 after successful context creation. The test uses the provider's UAV and SRV for the intermediate at resource index 4. A source-built compute shader writes a strip of 1,024 HDR colors into the 1920×1080 texture. Another shader loads the texels through the SRV. A separate texture copy returns the underlying 32-bit words.

On the RTX 4060 Laptop the writer packs RGB into an R32_UINT UAV and the reader unpacks it to floats. The shared HLSL helpers clamp finite inputs to [0, 65408], choose a shared exponent and round the three 9-bit mantissas to nearest even. On WARP the typed RGB9E5 views perform the conversion. A second WARP context forces the packed path to exercise the fallback in public CI. A double-precision CPU reference checks the stored bits and all four decoded components, including the implied alpha of 1. The inputs cover all 32 exponents, channel permutations, half steps, exponent carry, underflow, negative values and saturation. The native reference also accepts the documented nearest rounding alternative at ties. [Direct3D RGB9E5 conversion](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm#19.3.2)

On both local adapters the storage words and decoded channels matched nearest-even on every input. Both debug layers reported zero warnings and errors. The package supplies the local resource-creation path. These shaders test integer-coordinate texel loads. The next experiment traces the first package writer and reader of this intermediate.

The integrated AMD Radeon(TM) Graphics of a Ryzen 5 7600 (RDNA 2, device `164e`, driver 32.0.21045.29003) reports typed UAV stores for RGB9E5, so the native path runs on a physical GPU. Its stores round the mantissas toward zero. In 317 of the 1,024 inputs this gives a different word from nearest-even, and every stored word matches a round-toward-zero encoding. The SRV loads decode those words exactly. The forced packed context on the same adapter still matches nearest-even. For FLOAT -> RGBE, section 19.3.2.2 of the D3D11.3 functional spec says conversion "should be done with nearest even. Round to nearest is allowed", and notes that hardware does not perform it. Its general rule for conversion to another float format, in section 3.2.2, is round-to-zero. The native check therefore accepts nearest-even, round-to-nearest or round-toward-zero and reports how many texels needed each alternative. A store that mixes the two alternatives fails. The Xbox Series X GPU is also RDNA 2; whether its native stores round the same way is untested.

```powershell
ctest --preset dev --no-tests=error -R '^ffx_provider_loop_hardware$' -V
```

### Float-to-integer conversions in translated dispatches

The immediate control feeds positive infinity (0x7f800000) directly to both FFX emitter instructions and stores the results to a UAV. With DXC 1.8.2502.11 (239921522), cs_6_0, substituting the previous bare casts caused DXIL validation to fail with `Assignment of undefined values to UAV.` The guarded shader compiled and its DXIL returned 0xffffffff and 0x7fffffff on D3D12 WARP, with all 318 guard words intact.

The dispatch lowerer maps NaN to zero, truncates finite values toward zero and saturates out-of-range inputs to the signed or unsigned endpoint. The cast receives a finite value in range even if the compiler evaluates both arms of the endpoint selection.

The LLVM 15 check assembles both conversions and compares the translated register results with 19 pairs of expected words. The same inputs run through the emitted ALU expressions and production HLSL helpers on WARP. On Windows build 26300.9974, WARP and an RTX 4060 (driver 610.88) matched all pairs. The previous bare casts also matched in this D3D11 harness on these adapters. The fix removes dependence on out-of-range cast behaviour. Whether a normal dispatch supplies such values remains unmeasured.

```powershell
cmake --build --preset dev --target ffx_pass26_lds offscreen_check
ctest --preset dev --no-tests=error -R '^ffx_(immediate_)?float_to_int_warp$' -V
```

### FSR4 dispatch through the RCAS input

With sharpening enabled, the default i8 graph first touches the physical ID 16 texture in `FFX_FSR4_PASS_26`. It writes `rw_mlsr_output_color` (logical ID 10, UAV `u2`). The following `FFX_FSR4_RCAS_PASS` reads `r_rcas_input` (ID 16, SRV `t18`) and writes the external `rw_rcas_output` (ID 11, UAV `u11`). Each RCAS lane computes four output pixels. For each pixel it loads the center and four axial neighbors at integer texel coordinates. The native program contains 20 RGB `image_load` instructions with half-precision returns, four stores and zero sampler bindings.

| Provider RVA | Evidence from the pinned upscaler |
|---|---|
| `0xed10`, `0x1220f`, `0x13278` | Dispatch copies the sharpening byte into the staged constants at offset `0x5c`. When enabled, mirror entry 10 receives primary entry 16. When disabled, mirror entries 10 and 11 both receive the external output. |
| `0x1357d`, `0x13752`, `0x1375f` | The graph builder supplies workgroup counts. The scheduling loop indexes the pipeline array with stride `0x9690` and calls `0xe1d0`. The reflection check walks all 27 graph shader records in order. Case 26 selects the container at `0xa0e90`. |
| `0xe244`, `0xe2b9`, `0xe363`, `0xe3cd` | The scheduler resolves SRVs through the primary resource array and UAVs through the mirror, sets the job type to compute (2), then calls the enqueue callback. |
| `0x137b2`, `0x13898`, `0x138ac` | The sharpening flag gates the extra RCAS job. The separate pipeline at provider offset `0x248` selects the RCAS container at `0x701b0`. The provider then calls the execute callback. |

The package build translates the x64 alias updates and the two grid calculations. At 1920×1080 they produce 60×34×1 and 120×68×1 groups. The GPU replay uses an 8×4 logical image, one group per job, on the actual provider intermediate at index 4. Pass 26 has 256 threads per group and RCAS has 64. The host binds source-owned scene and history inputs as RGB buffers, uses unit exposure, and uses the provider's history and recurrent UAVs. Eight active lanes take the producer's direct texture path. The next experiment connects the two immediately preceding graph jobs through scratch.

Both desktop shaders come from offline decoding of the pinned native streams (3,482 producer instructions before specialization and 437 RCAS instructions). LLVM 15's gfx1030 decoder covers the producer's i8 dot instructions and its gfx1013 decoder covers RCAS. The translator keeps the original packed literal DWORDs, including the zero high half of the packed offset that decrements x. [AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

The GPU test checks all 32 intermediate texels and 257 surrounding guard texels. It then compares all 256 final pixels, including edges between different colors, with an independent FP32 five-tap reference (per-channel tolerance 2.5% plus 0.00005 for packed-half arithmetic). Alpha must equal red. RTX 4060 Laptop passed using R32_UINT. WARP passed with native RGB9E5 and with forced R32_UINT. Both debug layers reported zero errors and warnings. These are controlled single-group results.

From a VS x64 developer shell at the repository root, set `$upscaler` to the pinned local DLL. The middle command needs the LLVM 15 C library. On Windows, the official LLVM 15.0.7 installer (`winget install --id LLVM.LLVM --version 15.0.7 --exact`) provides `LLVM-C.dll` with the AMDGPU disassembler, so no WSL is required. All extracted programs and generated shaders stay under `out/`.

```powershell
./out/pe-env/Scripts/python.exe -B tools/ffx_dispatch_profile.py $upscaler out/ffx-dispatch
./out/pe-env/Scripts/python.exe -B tools/ffx_dispatch_aot.py out/ffx-dispatch --llvm "C:/Program Files/LLVM/bin/LLVM-C.dll"
# Alternatively, in WSL with libLLVM 15 installed:
# wsl -d Ubuntu-22.04 -- python3 -B tools/ffx_dispatch_aot.py out/ffx-dispatch --llvm libLLVM-15.so.1
cmake --preset dev -B out/build/ffx-device "-DFFX_UPSCALER_IMAGE=$upscaler" "-DFFX_DISPATCH_SHADER_DIR=$PWD/out/ffx-dispatch"
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_provider_loop_' --no-tests=error -V
```

The local log reports `dispatch=package-aot`. Through the native Windows route above (LLVM 15.0.7 `LLVM-C.dll`, Python 3.14.6), the translator decoded 1152, 100, 3482 and 437 instructions for pass 24, pass 25, the producer and RCAS. `ffx_provider_loop_hardware` passed on an RTX 4070 Ti (driver 32.0.16.1088) and `ffx_provider_loop_warp` passed on WARP, both with the debug layer enabled. Each reported `dispatch=package-aot` with 256 final pixels and zero mismatches for the bypass, zero and pattern scratch cases. Public Windows CI runs source-built descriptor and dispatch controls on WARP (`dispatch=desktop-control`). Linux CI assembles an independent integer-coordinate regression for the translator.

### First pass 26 LDS write block

At `0x2410`, the existing 8×4 dispatch skips to `0x26d8`. An optional D3D11 probe models wave zero of group (0,0) with 32 synchronized threads. It decodes the branch predicate and the first block's store addresses from the pinned producer. Distinct synthetic values identify each row, lane and component after the colour transform.

| Input | X scale (Y = 1) | Active lanes | Footprint | Tagged words |
| --- | ---: | ---: | --- | ---: |
| 32×4 | 243/256 | 32 | 32×6 | 576 |
| 32×4 | 244/256 | 32 | 33×6 | 0 |
| 31×4 | 243/256 | 30 | Unspecified | 0 |

On Windows build 26300.9974, NVIDIA GeForce RTX 4060 (610.88) and Microsoft WARP each matched an independent CPU predicate for 120 inputs. An inclusive horizontal limit changed only the 244/256 boundary case at 32×4 and unit vertical scale. All 576 shared-memory words matched their expected order or zero fill for each of the three rows on both adapters. D3D11 debug layer disabled. This probe covers the first gate and tagged stores. The production translator still skips the block. Later LDS reads and pixel output on this path remain unverified.

The Windows build compiles the readback host. The default CTest uses a source-owned HLSL fixture on WARP, exercising the same gate cases, inclusive-limit negative control and tagged stores against the CPU expectations. From the repository root, after configuring the dev preset:

```powershell
cmake --build --preset dev --target ffx_pass26_lds offscreen_check
ctest --preset dev --no-tests=error -R '^ffx_pass26_lds_warp$' -V
```

The physical adapter and pinned package are independent opt-ins. Use the producer generated by the dispatch profile above and a trusted LLVM 15 installation for the package comparison. Omit `--package` to run the source-owned input, or use `--backend hardware` for either input.

```powershell
out/pe-env/Scripts/python.exe -B tests/ffx_pass26_lds/verify.py --host out/build/dev/ffx_pass26_lds.exe --backend hardware
out/pe-env/Scripts/python.exe -B tests/ffx_pass26_lds/verify.py --host out/build/dev/ffx_pass26_lds.exe --backend warp --package out/ffx-dispatch/producer.bin C:/LLVM15/bin/LLVM-C.dll
```

### Last writers of the pass 26 scratch reads

The default FSR4-i8 graph with tier zero, unit render/output ratio and sharpening enabled schedules passes 24 and 25 immediately before pass 26. At provider RVA `0x136a7` the inclusion mask has bit 25 set. Other scheduling modes can skip the padding job. In the 8×4 case the host rounds both dimensions up to multiples of eight at `0x11f94`. Tensor 13 uses half that aligned size (4×4). The dispatch builder at `0x7e1c` and its padding helper at `0x7730` yield grids of 1×4×1 and 2×1×1 groups respectively.

Both kernels bind the provider's raw scratch UAV at `u11` (resource ID 22, index 12). Pass 24 performs its i8 convolution and stores 16 bytes at shader PC `0x218c`. With plane base `0x7f4bc0`, row stride `0x3c20` and 16 bytes per column, its output starts at column 1, row 1. The launched 64-wide groups also write beyond the four active columns. Pass 25's store at shader PC `0x200` writes four zero DWORDs. Its 44 active lanes cover the top and bottom rows, the left column and five columns of right padding around the 4×4 interior.

Pass 26 has eight active lanes with cell coordinates `(x,y)` in `[0,4) × [0,2)`. Its nine loads read at `base + (y+dy)*stride + 16*(x+dx)`. For a given address, interior columns 1–4 on rows 1–3 of the read window have pass 24 as last writer. All other addresses in that window have pass 25 as last writer.

| Pass 26 shader PC | (dx, dy) | Lanes reading pass 24 | Lanes reading pass 25 |
|---|---|---:|---:|
| `0x14c` | (0, 0) | 3 | 5 |
| `0x154` | (1, 0) | 4 | 4 |
| `0x15c` | (2, 0) | 3 | 5 |
| `0x164` | (0, 1) | 6 | 2 |
| `0x16c` | (1, 1) | 8 | 0 |
| `0x174` | (2, 1) | 6 | 2 |
| `0x17c` | (0, 2) | 6 | 2 |
| `0x184` | (1, 2) | 8 | 0 |
| `0x18c` | (2, 2) | 6 | 2 |

The reproduction commands above also translate the two upstream shaders and use four 64-byte constant ranges from the pinned model initializer. The test seeds the pass 24 input with either zero or a deterministic pattern, fills the output region with a sentinel, and uses the same GPU buffer across all three passes. It copies the 6×4 read window after each writer and records the address and all four DWORDs at each of the 72 pass 26 loads. A third control bypasses both writers and supplies zero scratch directly to pass 26. Model biases can produce nonzero activations even from a zero upstream input.

A separate Python evaluator checks the scratch words and the four history-blend logits per lane using signed-byte dot products. Spatially constant, distinct scene and history colors give a per-pixel blend reference for resource 16. The existing five-tap FP32 reference checks all 256 final RCAS pixels, including the surrounding pattern. Switching the upstream input changed all 32 active pixels at both stages on RTX 4060 Laptop, native RGB9E5 WARP and forced R32_UINT WARP. Both adapters reported zero debug warnings and errors. The tensor feeding pass 24 remains a fixture; the full-wave LDS branch of pass 26 is a separate open path.

### FidelityFX context creation and ownership

The Windows test calls the translated loader create function, then passes its returned handle directly to the existing destroy translation. Create binds a source-owned upscaler provider to the two table slots. On success, the loader allocates a 16-byte wrapper and stores the table pointer and provider context in it. A provider create error propagates its 32-bit return code, clears the caller's handle and skips wrapper allocation. Destroy calls the provider with the address of the inner field before releasing the wrapper, including on a provider destroy error.

Fourteen cases cover default CRT allocation and custom allocation with user data. For each route the test checks success, two destroy errors, three create errors, and failure to allocate the wrapper after the provider succeeds. It observes allocation and release addresses, order, sizes and the live-allocation count. In the allocation-failure case the host adapter calls provider destroy once and returns memory error 5. This rollback is a Windows adapter behavior. The inspected loader proceeds to a wrapper store after its allocator returns. The provider used in this comparison owns real CRT memory and has controlled return codes; creation of a live upscaling effect on a graphics device remains open.

```powershell
cmake --build --preset dev --target cpu_create_lifted cpu_destroy_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_context_lifecycle$' -V
```

CI compares an independently assembled native create caller and its translation, each paired with the fixture's lifted destroy. To check the package translation from a VS x64 developer shell, set `$loader` to the path of the local FidelityFX loader and run:

```powershell
New-Item -ItemType Directory -Force out/ffx-lifecycle | Out-Null
./out/pe-env/Scripts/python.exe -B tools/ffx_create_context.py $loader out/ffx-lifecycle/create.cpp
./out/pe-env/Scripts/python.exe -B tools/cpu_chained_aot.py $loader ffxDestroyContext out/ffx-lifecycle/destroy.cpp --ffx-destroy
foreach ($unit in 'create','destroy') {
    $prefix = "out/ffx-lifecycle/$unit"
    cl /nologo /O2 /EHsc /LD "/Fo:$prefix.obj" "$prefix.cpp" /link "/OUT:$prefix.dll" "/IMPLIB:$prefix.lib"
    if ($LASTEXITCODE) { throw "Compilation failed for $unit" }
}
./out/pe-env/Scripts/python.exe -B tests/cpu/context_lifecycle.py out/build/dev/cpu_create.dll out/ffx-lifecycle/create.dll out/ffx-lifecycle/destroy.dll
```

### FidelityFX version enumeration

The Windows query returned one upscaler, version ID `0xf5a5ca1e01001001` and name `4.1.1`. The first call requested the count. The second supplied one ID slot and one name slot. Both returned status 0. The descriptor uses AMD's null-context, type-4 version query. [AMD FidelityFX query descriptor](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/main/Kits/FidelityFX/api/include/ffx_api.h)

`tools/ffx_version_query.py` checks both input DLL hashes, decodes the selected x64 query intervals and the provider's two filter functions, and emits their integer operations and branches as C++. The generated Windows DLL combines both modules' selected paths. Its host binding specializes module resolution to one available upscaler and no frame-generation module. It reconstructs the single provider entry from the initializer's stores, including the full ID and the referenced name. Static storage keeps the returned name valid for the lifetime of the DLL.

On this build, capacity zero reports the available count without writing array elements. ID-only and name-only calls both succeed. Capacity two writes one result and leaves the second slot untouched. A missing frame-generation module returns 4 and an unknown effect returns 2. In both cases the loader clears the count **pointer in the descriptor** and leaves the caller's count value untouched. This entry point covers version enumeration only.

Run these commands in an x64 VS developer shell with the Python development environment available. Set `$loader` and `$upscaler` to the two local input paths.

```powershell
New-Item -ItemType Directory -Force out/ffx-query | Out-Null
./out/pe-env/Scripts/python.exe -B tools/ffx_version_query.py $loader $upscaler out/ffx-query/query.cpp
cl /nologo /O2 /EHsc /LD /Fo:out/ffx-query/query.obj out/ffx-query/query.cpp /link /OUT:out/ffx-query/query.dll /IMPLIB:out/ffx-query/query.lib
./out/pe-env/Scripts/python.exe -B tests/cpu/version_query_check.py out/ffx-query/query.dll query_versions 0xf5a5ca1e01001001 4.1.1
```

The public CTest `cpu_aot_version_query` runs the same output checks against an independent MASM fixture and its translation. The fixture uses a different name and 64-bit ID. The two proprietary inputs and generated code stay local.

The integer lowering also handles MOVBE, the byte-reversing move between memory and a 16-, 32- or 64-bit register, in both directions. It changes no flags. A 16-bit register destination keeps bits 16 to 63, and a 32-bit destination clears its upper half. `cpu_aot_movbe` exits 77 (capability skip) when CPUID leaf 1 ECX bit 22 (`MOVBE`) is clear. Otherwise it builds ten exports from [MASM source](../tests/cpu/movbe.asm), covering loads, stores, REX registers, displacements, a scaled index and a load stored back. It compares RAX, ZF/CF, the data bytes and 64 guard bytes on each side, at nine offsets including misaligned ones, against the native DLL and an independent reference. Three controls mutate the generated C++ and must mismatch: `noswap` copies the bytes in memory order (2592/2880 cases), `clear16` zero-extends a 16-bit destination (360/2880), and `flags` clears ZF and CF (2160/2880). Run `ctest --preset dev --no-tests=error -R '^cpu_aot_movbe$' -V`.

The shared integer lowering in this tool emits flat loads and stores, so it rejects FS or GS segment overrides and LOCK prefixes instead of dropping them. A thread-block access such as `gs:[0x30]` would otherwise read absolute address 0x30, and a locked read-modify-write would lose its atomicity. Segment overrides without effect in 64-bit mode and `rep ret` lower like the bare instruction. A host operation that replaces an instruction bypasses the check. `ctest --preset dev --no-tests=error -R '^cpu_lower_prefixes$' -V` checks 11 rejected encodings and 6 accepted controls with synthetic bytes.

**XCHG.**
- **Register forms:** register-register `xchg` reads both operands before writing either. In every 32-bit form both registers are zero-extended, so `87 C0` (`xchg eax, eax`) clears the upper half of RAX. The one-byte `90` and `66 90` stay NOPs.
- **Memory forms:** these are atomic on x86 without a LOCK prefix. The lowering therefore emits the host's `_InterlockedExchange8`, `16`, plain or `64` on the flat address, not a load and a store. The ordering of the flat accesses around the exchange is not modelled. An explicit, redundant `lock xchg` is still rejected by the prefix check above.
- **Value fixture:** `cpu_aot_xchg` builds 18 value exports from MASM source (`tests/cpu/xchg.asm`). They cover the 8-, 16-, 32- and 64-bit register forms (including `ah`, `ch`, `r8b` and the one-byte RAX forms), `87 C0`, and memory forms at several offsets, including a misaligned qword. The check compares RAX, RCX, R8, R9 and the memory slot with a reference for 64 value pairs per export.
- **Contention check:** two token exports run a loop of `xchg` on one shared qword or dword. Eight threads run them at once, each exchanging 100000 distinct tokens. The values the exchanges return, plus the final slot, must be exactly the tokens and the initial value, each once. The native DLL and the translation lose or duplicate none in three rounds.
- **Negative controls:** two mutated translations must each mismatch. One swaps through a plain load and store: every single-threaded value case still matches, but tokens are lost or duplicated under contention. The other writes only one register of a register swap.

Run `ctest --preset dev --no-tests=error -R '^cpu_aot_xchg$' -V`.

A 67h address-size prefix selects 32-bit addressing. The lowering wraps base + index × scale + displacement at 32 bits and zero-extends the result, ignoring the upper halves of the registers as the hardware does. In this mode iced reports a displacement of -8 as `0xfffffff8`, so the wrap is also needed when the upper halves are zero. EIP-relative operands are rejected because the hardware adds the low 32 bits of the runtime RIP, which an RVA binding cannot represent. `cpu_aot_address_size` builds `lea`, load and store forms such as `lea rax, [ecx+edx*8-8]` and `mov [ecx+edx], r8d` from MASM source. It translates them and compares both images with a 32-bit reference over wrapped sums and upper-half noise. The loads and stores use a page committed below 4 GiB, and each store must leave the rest of the page unchanged. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_address_size$' -V`.

XLATB replaces AL with the byte at RBX + AL. AL is zero-extended, and bits 8 to 63 of RAX and the flags are unchanged. With a 67h prefix the base is EBX and the sum wraps at 32 bits, so the upper half of RBX is ignored. A REX.W prefix (`48 d7`) does not change the operation. `cpu_aot_xlatb` builds four exports from [MASM source](../tests/cpu/xlatb.asm): the plain, REX.W and 67h forms, and two lookups in a row. Each export runs on a 256-byte permutation table in a page committed below 4 GiB. The check compares RAX and ZF/CF for every AL value, with noise in the upper bits of RAX and, for the 67h form, in the upper half of RBX, against the native DLL and an independent reference. Three controls mutate the generated C++ and must mismatch: `signed` sign-extends AL, `noaddr32` keeps the upper half of RBX in the 67h sum (whose reads fault), and `clear` writes the loaded byte over all of RAX. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_xlatb$' -V`.

RDTSC and RDTSCP use an explicit host TSC profile: they supply host ticks and, for RDTSCP, the host IA32_TSC_AUX, with no guarantee of a guest TSC frequency or of a virtual-CPU number. The counter passes through unscaled: EAX holds the low 32 bits and EDX the high 32 bits, with the upper halves of RAX and RDX zeroed and the flags unchanged. RDTSCP also writes TSC_AUX to ECX, zero-extending RCX. The lowering calls `__rdtsc()` and `__rdtscp()`, so each keeps its own instruction, with no fence added to the generated code. It stores the result in a `volatile` local, so a read stays in the code even when the guest overwrites its result; without it, MSVC `/O2` removes such an RDTSC. Because the counter changes between reads, the checks test properties rather than bytes. The [RDTSC source](../tests/cpu/rdtsc.asm) has one RDTSC and two RDTSC in a row, and the [RDTSCP source](../tests/cpu/rdtscp.asm) one RDTSCP, each after `add r8, r8` sets ZF and CF from one of four presets; each file also has one read without a preset whose result is overwritten with 7. The single reads first fill RAX and RDX, and RDTSCP also RCX, with all ones. A [native harness](../tests/cpu/rdtsc_harness.asm), never translated, supplies CPUID and the measurement reads. The checks pin the thread to one logical processor and confirm that it runs there before reading. For the single RDTSC and the RDTSCP they require that the translated read lies between two native reads, each serialized by LFENCE before and after. For every read with a preset they require that the upper halves of the results are zero and that ZF and CF keep their preset. The second of two RDTSC must not be earlier than the first and must differ from it in at least one of the 2,000 rounds. RDTSCP must return the TSC_AUX of the pinned processor, which the RDTSCP check chooses as the highest of the affinity mask with a nonzero TSC_AUX. The exports with overwritten results must return 7. Controls mutate the generated C++ and must break a property: `constant` returns a zero RDTSC counter, `swap` exchanges the halves, `noext` leaves the whole counter in RAX, `noaux` drops TSC_AUX, `stale` keeps the value of each RDTSC's first call, and `flags` and `pflags` flip ZF and CF after RDTSC and RDTSCP. The translation is also built with `cl /O2`. A walk that follows every branch from each export must find every RDTSC and RDTSCP of the source, including the overwritten ones, and the `/O2` build must keep the properties above. The `plain` control removes `volatile` and is built only at `/O2`; there the walk must find the overwritten RDTSC missing. `cpu_aot_rdtsc` checks RDTSC with `constant`, `swap`, `noext`, `stale`, `flags` and both `/O2` builds, and runs on every host. `cpu_aot_rdtscp` checks RDTSCP with `swap`, `noext`, `noaux`, `pflags` and the translated `/O2` build. Each source counts toward coverage only through its own test. `cpu_aot_rdtscp` exits 77 on a host without RDTSCP (CPUID 80000000h reports no leaf 80000001h, or its EDX bit 27 is clear). It also exits 77, after its other checks, on a host whose TSC_AUX is 0 on every processor of the affinity mask, where `noaux` cannot fail. Guest code that converts ticks with its own frequency will see host time; whether a guest-frequency profile belongs in the runtime is open in [#739](https://github.com/R0Y00/Xbox2Windows/issues/739). Run `ctest --preset dev --no-tests=error -R '^cpu_aot_rdtscp?$' -V`.

RDRAND and RDSEED are lowered only in an opt-in host-entropy profile, `lower(..., host_entropy=True)`; the default profile still rejects both. Their precondition is the host CPUID bit: CPUID.01H:ECX bit 30 for RDRAND and CPUID.(EAX=07H,ECX=0):EBX bit 18 for RDSEED. Each guest execution runs the host instruction once through `_rdrand16_step` to `_rdseed64_step`, with no retry and no fence; CF is the intrinsic's result, so guest code keeps control of retrying. On CF=0 the destination gets zero at its width. A 16-bit destination keeps bits 16 to 63, and a 32-bit one clears the upper half. The lowering clears ZF and OF, and PF only in a checked x87 function whose body reads it (JP, JNP, FCMOVU or FCMOVNU). SF and AF are not modelled by the shared lowering, so the translation cannot show the hardware's clearing of them. The result is a `volatile` local, so a read stays in the code when the guest discards it; without it, MSVC `/O2` removes such a read. `cpu_aot_rdrand` ([source](../tests/cpu/rdrand.asm)) and `cpu_aot_rdseed` ([source](../tests/cpu/rdseed.asm)) each exit 77 when the host lacks their own instruction. A [native harness](../tests/cpu/rdrand_harness.asm), never translated, supplies CPUID and confirms on the host that OF, SF, ZF, AF and PF are 0 after the 64-bit instruction, from two `add` presets that set them first. Each check confirms that the default profile rejects every read, then runs 2,000 rounds of the 16-, 32- and 64-bit reads, natively and translated, after `add r8, r8` sets ZF, CF and OF from four presets. Every read must be a success (CF 1) or a failure (CF 0, zero at the width) with ZF and OF clear and the width rules above. Natural failures are counted and printed, never required. Two deterministic variants replace the host read and must pass on every host: `succeed` reports CF 1 with a fixed pattern, and `fail` reports CF 0 after storing that pattern, so the zero and carry rules are checked even when the host never fails. The reads whose result is discarded must return the overwritten value. Controls mutate the generated C++ and must break a rule: `constant` succeeds with a fixed value, `nocarry` drops CF, `noext` keeps the upper half of a 32-bit destination, `wide16` replaces a 16-bit destination's upper bits, `flags` leaves ZF and OF as preset, and `nozero` keeps the pattern after a failed read. The translation is also built with `cl /O2`. A walk that follows every branch must find each RDRAND or RDSEED of the source, including the discarded one, and the `/O2` build must keep the rules. The `plain` control removes `volatile` and is built only at `/O2`; there the walk must find the discarded read missing. Variation checks require at least two different successful values at each width and one 64-bit success with a nonzero upper half. They catch a constant translation and do not assess entropy quality. A host with fewer than two natural successes at a width still runs every deterministic check, then exits 77 with the variation named as not measurable. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_rd(rand|seed)$' -V`.

CPUID and XGETBV are lowered only in an opt-in host-CPU profile, `lower(..., host_cpu=True)`; the default profile still rejects both. They report the host processor, not a console CPU: no guest feature table is applied, so translated code sees the host's feature bits. CPUID passes the guest EAX leaf and ECX subleaf to `__cpuidex` and writes all four results zero-extended. XGETBV passes ECX to `_xgetbv` unchanged and writes EDX:EAX zero-extended. Both keep the flags. The lowering executes whatever index the guest supplies, but the supported contract is narrower: the caller has checked CPUID.01H:ECX bit 27 (OSXSAVE), which the lowering does not check, and reads XCR0 (ECX = 0). Other indices are passed through without a supported contract. An index the host rejects faults as it does natively. Where the host supports XINUSE (ECX = 1), that read reports the host's current extended-state use, which this profile does not verify. A discarded CPUID stays in MSVC `/O2` code. A discarded XGETBV is removed there unless its result is `volatile`, which the lowering uses. `cpu_aot_cpuid` ([source](../tests/cpu/cpuid.asm)) and `cpu_aot_xgetbv` ([source](../tests/cpu/xgetbv.asm)) pin the thread to the logical processor it runs on, in its processor group, run every native and translated read and the harness reads they are compared with there, and restore the previous affinity afterwards, because some leaves, such as the APIC ID in leaf 01H EBX, differ per logical processor. For CPUID, EAX and ECX start with their upper halves set and RBX and RDX with all ones; for XGETBV, RAX and RDX start with all ones. The CPUID check covers leaves 00H, 01H, 04H, 07H, 0BH and 0DH, subleaves 0 and 1 where the leaf takes one, and 80000000H and 80000001H, up to the host's maximum leaves. Every result must equal the host's, read by a [native harness](../tests/cpu/cpuid_harness.asm), with zero upper halves and ZF and CF kept from four presets. A read whose result is overwritten must return the overwritten value, and the `/O2` walk below must still find it. The XGETBV check exits 77 without OSXSAVE before any XGETBV runs. It compares XCR0 only, the supported contract. A reserved index runs in a child process, where native and translated code must report the same fault; this checks fault passthrough only, not XINUSE. Controls mutate the generated C++ and must break a property: `noext` keeps an upper half, `swap` exchanges EBX and EDX (CPUID) or the two halves (XGETBV), `nosubleaf` drops ECX, `constant` returns a fixed result, and `flags` flips ZF and CF. `nosubleaf` is measurable only when a covered leaf differs between subleaf 0 and 1; otherwise `cpu_aot_cpuid` exits 77 after its other checks. The translations are also built with `cl /O2`. A walk that follows every branch must find each CPUID or XGETBV, including the discarded one, and XGETBV's `plain` control, without `volatile`, must lose the discarded read there. On a hybrid processor the values hold for the one logical processor the test ran on. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_(cpuid|xgetbv)$' -V`.

LDMXCSR and STMXCSR with a 32-bit memory operand are lowered only in an opt-in host-MXCSR profile, `lower(..., host_mxcsr=Mxcsr(reject))`; the default profile still rejects both, and the VEX forms stay rejected. LDMXCSR checks the requested word before it reaches the host register: a reserved bit (16 to 31), an unmasked exception (one of bits 7 to 12 clear), and FTZ (bit 15) or DAZ (bit 6) leave the lowered function through `reject` with the statuses 1, 2 and 3 of `MXCSR_STATUS`, and no later guest instruction runs. The word is read as a guest operand, so an inaccessible one faults before anything is loaded. A word that passes, with any rounding control and any sticky flags, is loaded with `_mm_setcsr`. STMXCSR stores `_mm_getcsr()`. The Windows x64 profile is opt-in for leaf fixtures. The caller supplies an initial MXCSR with all exceptions masked and DAZ and FTZ clear. Rounding control and sticky flags may vary. A lowered function with this profile runs inside `mxcsr_entry`, which saves the caller's whole MXCSR and restores it in a `__finally`: on a normal return, after a refusal, and when a structured exception such as a guest memory fault leaves the body. `<name>_status()` returns the refusal status of the calling thread's last call, or zero after normal return or a propagated memory fault. A file that uses the profile starts with `#pragma fenv_access(on)`, so the compiler keeps float operations after the `_mm_setcsr` that changes their rounding. `cpu_aot_mxcsr` ([source](../tests/cpu/mxcsr.asm)) loads every rounding control, with and without the six sticky flags. It sets a non-default caller word first: round toward zero with the precision flag set. For operands the test writes at run time, an ADDPS of 1 + 0.75 ulp and its negation, two halfway cases (1 + 0.5 ulp and (1 + 1 ulp) + 0.5 ulp, which to-nearest-even rounds in opposite directions) and a CVTSS2SI of 2.75 and -2.75 give a different result in each mode. The translation must match native execution and fixed expectations. STMXCSR must read back the loaded control bits and the requested sticky flags, the bytes next to every stored word must stay unchanged, and the caller's word must be back afterwards. The translated entry must also restore the caller's word when the guest returns without restoring it, after the four refused words, and when a guest load faults, both at the LDMXCSR operand and later. A refusal status stays with its thread while another thread calls the same entry successfully. The refused words must stop the guest before its next store. The native LDMXCSR of a reserved bit raises a #GP, which Windows reports as an access violation; it is a reference observation, not the promised status of the translated entry. Two controls must break a rule: `noload` checks the word but never loads it, and `fixed` stores 0x1F80 from STMXCSR. The default profile must refuse each LDMXCSR and STMXCSR of the fixture on its own. The translation is also built with `cl /O2`, must keep every rule there, and every export must still reach at least its source's LDMXCSR and STMXCSR. FTZ and DAZ, the VEX forms and unmasked exceptions are outside this profile. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_mxcsr$' -V`.

The RCX-counted branches `loop`, `loope`, `loopne` and `jrcxz` lower to a decrement and a conditional `goto`, and so do their 67h forms, which count in ECX. The behaviour is as follows:
- None of them changes a flag.
- `loope` and `loopne` test ZF after the decrement.
- `jrcxz` and `jecxz` only test the count.
- The ECX forms decrement ECX as a 32-bit write, which clears the upper half of RCX. The native fixture confirms this on the cases with noise in the upper half.

`cpu_aot_rcx_branches` builds eight exports from MASM source (`tests/cpu/rcx_branches.asm`) and compares 7712 cases against the native DLL, the translation and a Python reference. Each case records RAX, RCX, the final pointer and ZF/CF. The exports are:
- iteration counts with each ZF/CF preset, which must survive `loop`;
- byte scans under `loope` and `loopne` guarded by `jrcxz`, which exit on the first element, a later one or at count exhaustion;
- `jrcxz` and `jecxz` on counts that differ only in the upper half of RCX.

Three mutated translations must each mismatch:
- the ECX decrement keeping the upper half of RCX, with `jecxz` testing all of RCX;
- `loope` and `loopne` ignoring ZF;
- `jrcxz` inverted.

Run `ctest --preset dev --no-tests=error -R '^cpu_aot_rcx_branches$' -V`.

An immediate SHL, SHR or SAR whose masked count is zero leaves the flags unchanged, but a 32-bit destination is still written and its upper half clears. This covers `shl eax, 0` and `shl eax, 32`, because the count is masked to five bits. The lowering keeps the flags and emits the zero-extending write. A 64-bit destination keeps every bit. `cpu_aot_shift_count` builds these forms from MASM source, together with a nonzero control. It compares the native and translated images with a reference and checks that `sete` after a zero-count shift still observes the preceding compare. Before this change the translation returned `0xdeadbeef12345678` for `shl_zero(0xdeadbeef12345678)` where the hardware returns `0x12345678`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_shift_count$' -V`.

The shared lowering also handles 32-bit and 64-bit integer bit-scan, bit-count, byte-swap, and register bit-test forms (`bsf`, `bsr`, `bswap`, `popcnt`, `lzcnt`, `bt`, `bts`, `btr`, `btc`), including zero-extending 32-bit writes, ZF/CF updates, and rejection of memory-operand bit tests. `cpu_aot_bit_ops` probes CPUID for POPCNT and LZCNT before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on capable hosts it builds all 19 exports from MASM source (`tests/cpu/bit_ops.asm`), translates them, and compares values and flags against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_bit_ops_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_bit_ops$' -V`.

The shared lowering also handles `clc`, `stc` and `cmc`, and the rotations through the carry flag `rcl` and `rcr` in 8-, 16-, 32- and 64-bit register and memory forms, including `ah`, with a count of 1, an immediate or CL. The rotation steps one bit at a time, as in the Intel SDM pseudocode. The count is masked to five bits, or six for a 64-bit operand. The 8- and 16-bit forms rotate 9 and 17 bits, so reducing their counts modulo 9 and 17 only bounds the loop. A zero step count keeps CF, and ZF is never affected. OF, which the SDM defines only for a count of 1, is not modelled, like the other flags the lowering omits. A 32-bit register destination is written and its upper half cleared even when the masked count is zero, as for the zero-count shifts above. The native fixture confirms this.

`cpu_aot_carry_rotates` builds 102 exports from MASM source (`tests/cpu/carry_rotates.asm`, generated by `tests/cpu/carry_rotates.py asm`):
- every destination with counts 1 and CL;
- immediates 0, 5, 9, 17, 33 and 65 for `al`, `ax`, `eax`, `rax` and a dword in memory;
- the flag instructions alone;
- the 128-bit idioms `shl` + `rcl` and `shr` + `rcr`.

Each export starts from one of four ZF/CF presets. The check compares RAX, the memory operand, ZF and CF for 84000 cases against the native DLL, the translation and a Python reference. CL counts run from 0 to 70, plus 96, 128 and 255. Three mutated translations must each mismatch:
- the count mask widened to eight bits;
- the rotated-out bit fed back instead of CF, which turns the instructions into `rol` and `ror`;
- `stc` clearing CF.

Run `ctest --preset dev --no-tests=error -R '^cpu_aot_carry_rotates$' -V`.

The shared lowering also handles 32-bit and 64-bit BMI1 bit-manipulation forms (`andn`, `bextr`, `blsi`, `blsmsk`, `blsr`, `tzcnt`), including their zero-extending 32-bit writes and ZF/CF updates. `cpu_aot_bmi1` probes CPUID leaf 7 EBX bit 3 (`BMI1`) before invoking the native fixture and exits 77 (capability skip) on hosts without BMI1; on BMI1-capable hosts it builds all twelve 32-bit and 64-bit exports from MASM source (`tests/cpu/bmi1.asm`), translates them, compares both values and observed ZF/CF flags against the native DLL and an independent reference, and checks that 16-bit `tzcnt` is rejected. Run `cmake --build --preset dev --target cpu_bmi1_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_bmi1$' -V`.
The shared lowering also handles 32-bit and 64-bit rotates and double-precision shifts (`rol`, `ror`, `shld`, `shrd`) with both immediate and `cl` counts, preserving flags on zero masked counts while zero-extending 32-bit destinations. `cpu_aot_rotates_shifts` builds all 12 exports from MASM source (`tests/cpu/rotates_shifts.asm`), translates them, and compares values and ZF/CF flags against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_rotates_shifts_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_rotates_shifts$' -V`.

The string instructions `movs`, `stos`, `lods`, `scas` and `cmps` lower in byte, word, dword and qword widths, bare or with REP (`movs`, `stos`, `lods`), REPE or REPNE (`scas`, `cmps`), together with `cld` and `std`. The lowering selects them by iced `Code`, because the SSE2 scalar `movsd` and `cmpsd` share their mnemonics. Each iteration moves or compares one element and then steps RSI and RDI by the width, downward when DF is set. Overlapping copies therefore replicate as on the hardware, which a whole-range `memmove` would not. A repeated form tests RCX before each element and ZF after it. A zero count leaves the pointers, memory and flags unchanged. `lodsb` and `lodsw` merge into RAX and `lodsd` zero-extends. `cmps` compares the source element with the destination element and `scas` compares the accumulator with the destination; only ZF and CF are modelled, like the rest of the lowering. DF starts clear in each translated function. The [x64 software conventions](https://learn.microsoft.com/en-us/cpp/build/x64-software-conventions#x64-register-usage) expect DF clear on function exit and on entry to runtime and system calls, so a conforming caller never enters with it set. The 67h forms that walk ESI, EDI and ECX, REPNE on `movs`, `stos` and `lods`, and FS or GS sources are rejected. `cpu_aot_string_ops` builds 62 exports from MASM source (`tests/cpu/string_ops.asm`). In 13 of them the instruction runs with DF set, and one sets DF and clears it again before the instruction. Each export takes its pointers, count, RAX and a flag preset from the caller and reports memory, RAX, RSI, RDI, RCX, ZF and CF. The check runs 39984 cases against the native DLL, the translation and a Python reference. The cases cover overlaps of one byte and of less than an element, zero counts under each flag preset, and REPE and REPNE exits on the first element, on a later one and at count exhaustion, with both CF results. Three mutated translations must each mismatch: a `memmove` for REP `movs`, swapped `cmps` operands, and STD ignored. A 1 KiB guard on each side of the buffer is compared too. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_string_ops$' -V`.

The shared lowering also handles baseline x64 sign-extension (`cbw`, `cwde`, `cdqe`, `cqo`, `movsx`) and add-with-carry (`adc`) forms, preserving upper bits on 16-bit writes, zero-extending 32-bit writes, and propagating carry-in/carry-out and ZF on `adc`. `cpu_aot_sign_ext_adc` builds all 11 exports from MASM source (`tests/cpu/sign_ext_adc.asm`), translates them, and compares values and flags against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_sign_ext_adc_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sign_ext_adc$' -V`.

The shared lowering also handles the memory-ordering fences `lfence`, `mfence` and `sfence`, `pause`, and the cache hints `prefetcht0`, `prefetcht1`, `prefetcht2`, `prefetchnta`, `prefetch`, `prefetchw`, `clflush`, `clflushopt` and `clwb`, each through the MSVC intrinsic that emits the same instruction. A fence has no effect a single thread can observe, so `cpu_aot_fences_hints` checks that each of its 7 exports keeps the loads and stores around the instruction, including prefetches of a null and an unmapped address, which never fault. It compares return values and the buffer against the native DLL and a reference model over 76 cases. A segment-relative prefetch and the invalid `lock lfence` stay rejected. Fences, PAUSE and the standard PREFETCHT*/PREFETCHNTA cases run in the baseline test. AMD prefetch, CLFLUSH, CLFLUSHOPT and CLWB each have a separate CTest; a native illegal instruction skips only that test (exit 77). Run `cmake --build --preset dev --target cpu_fences_hints_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_fences_hints($|_)' -V`.

The shared lowering also handles 32-bit and 64-bit BMI2 forms (`bzhi`, `mulx`, `pdep`, `pext`, `rorx`, `sarx`, `shlx`, `shrx`), preserving flags on the flag-free forms, updating ZF/CF on `bzhi`, and resolving same-destination `mulx` to the high half. `cpu_aot_bmi2` probes CPUID leaf 7 EBX bit 8 (`BMI2`) before invoking the native fixture and exits 77 (capability skip) on hosts without BMI2; on BMI2-capable hosts it builds all 17 exports from MASM source (`tests/cpu/bmi2.asm`), translates them, compares values and flags against the native DLL and a reference model, and verifies that malformed 0x66-prefixed VEX sequences for all eight instructions are rejected. Run `cmake --build --preset dev --target cpu_bmi2_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_bmi2$' -V`.

All seven SHA-NI forms (`sha1msg1`, `sha1msg2`, `sha1nexte`, `sha1rnds4`, `sha256msg1`, `sha256msg2`, `sha256rnds2`) are also lowered for legacy XMM encodings. `cpu_aot_crypto_sha` probes CPUID leaf 7 subleaf 0 EBX bit 29 (`SHA`) before invoking the native fixture and exits 77 on hosts without SHA-NI; on SHA-NI-capable hosts it builds all 10 exports from MASM source (`tests/cpu/crypto_sha.asm`, covering all four `sha1rnds4` round-function selectors), translates them, compares exact 16-byte outputs against the native DLL, and checks that unmodeled registers are rejected. Run `cmake --build --preset dev --target cpu_crypto_sha_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_crypto_sha$' -V`.

The shared lowering also handles 32-bit and 64-bit `mul`, `div`, `idiv`, `xadd`, and `cmpxchg` (with `LOCK` still rejected by the prefix guard), using portable 128-bit arithmetic for 64-bit widening multiply/divide. `cpu_aot_mul_div` builds all 10 exports from MASM source (`tests/cpu/mul_div.asm`), translates them, and compares results and ZF/CF flags against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_mul_div_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_mul_div$' -V`.

The shared lowering also handles `cmpxchg8b` and `cmpxchg16b` with a memory operand, with `LOCK` still rejected by the prefix guard. Each compares EDX:EAX (RDX:RAX) with the operand. When they are equal it sets ZF and stores ECX:EBX (RCX:RBX), leaving RAX and RDX as they were. Otherwise it writes the existing operand value back, clears ZF and loads the operand into EDX:EAX (RDX:RAX), where `cmpxchg8b` zero-extends both 32-bit halves. The fixture checks that a read-only destination faults on write for either comparison outcome. The lowered stores do not provide locked atomicity. The other flags keep their values. A `cmpxchg16b` operand that is not 16-byte aligned raises #GP(0), which Windows reports as an access violation reading address -1; the translation raises the same exception. `cpu_aot_cmpxchg_wide` saves RBX in the home area with unwind data, so a faulting operand still restores it. It compares memory, RAX, RDX, ZF and CF against the native DLL and a reference model over 120 cases, including equal operands, either half differing, register upper halves the comparison ignores, and both CF values. It also checks that operands at offsets 4 and 8 raise the same exception in both DLLs. Run `cmake --build --preset dev --target cpu_cmpxchg_wide_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_cmpxchg_wide$' -V`.

The shared lowering also handles all six unsigned/equality `cmovcc` (`cmove`, `cmovne`, `cmova`, `cmovae`, `cmovb`, `cmovbe`) and `setcc` (`sete`, `setne`, `seta`, `setae`, `setb`, `setbe`) forms, unconditionally zero-extending 32-bit `cmovcc` destinations and preserving upper bytes on `setcc`. `cpu_aot_cmov_setcc` builds 13 exports from MASM source (`tests/cpu/cmov_setcc.asm`), translates them, and compares results against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_cmov_setcc_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_cmov_setcc$' -V`.

The shared lowering performs every read of guest memory through a `volatile` load, as the jump-table translator does: the processor reads an instruction's memory operand, and faults on an inaccessible one, whether or not the value or the flags are used, and an optimized MSVC build drops an ordinary load whose value is unused. Each instruction's memory operand is read once, at its own width, into a local `mem` declared before the rest of the statement, so the read precedes the instruction's writes: a read-modify-write such as `ADD [mem], reg` performs one read and then its store, a `CMOVcc` memory source is read before the condition is evaluated, and a `CMP` or `TEST` whose flags nothing reads still reads its operand. A scalar read uses one access at its width, including unaligned x64 operands. An aggregate vector read uses ordered volatile byte accesses over the operand's full extent. String instructions read each element through the same load in every iteration, and the x87 profile reads its memory operands through it as well. A read of the function's own frame array stays an ordinary load: an operand `[rsp + d]` that lies within the array, in a function where no instruction names RSP as a register and no binding mentions it, so that RSP points at the array throughout. The [dead-read fixture](../tests/cpu/lower_dead_read.asm) stores 1 to the trace before each read and 2 after it. Its exports read a value that is overwritten, compare and test memory with flags that are never read, execute a `CMOVNE` that never moves, add to memory in place, return the value read, load an x87 operand that is popped unused, and overwrite a 16-byte vector read. A REP MOVSB control checks zero count, the last readable byte and a second-iteration fault after a partial copy. `cpu_aot_lower_dead_reads` builds the translation with the project's build and again with a separate `cl /O2` command, calls the original and each translation inside `__try` with fresh trace words and a fresh page, and compares the structured exception code, the return value, the trace and the page. For a readable operand all three return the reference. For a null operand, an operand in an inaccessible page and an operand that crosses into one, the original raises an access violation after the first marker and before the second, and both translations must do the same; an operand that ends exactly at the inaccessible page returns. A source control checks that each memory operand is read once through the `volatile` load. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_lower_dead_reads$' -V`, and the same with `--preset release`.

The shared lowering also handles signed relational and flag-conditional forms across `cmovcc` (`cmovl`, `cmovge`, `cmovle`, `cmovg`, `cmovs`, `cmovns`, `cmovo`, `cmovno` in 32-bit and 64-bit), `setcc` (`setl`, `setge`, `setle`, `setg`, `sets`, `setns`, `seto`, `setno`), `jcc` (`jl`, `jge`, `jle`, `jg`, `js`, `jns`, `jo`, `jno`), two's complement integer negation `neg` (32-bit and 64-bit), and `cwd`. The lowering models the sign flag `SF` across arithmetic, comparison, and logical producers, evaluating signed ordering predicates (`l` as `SF != OF`, `ge` as `SF == OF`, `le` as `ZF || SF != OF`, and `g` as `!ZF && SF == OF`), sign conditions (`s` and `ns`), and overflow conditions (`o` and `no`), while preserving upper bytes on `setcc` and zero-extending 32-bit `cmovcc`. `cpu_aot_signed_conds` builds 45 exports from MASM source (`tests/cpu/signed_conds.asm`), translates them, and compares results and flags against the native DLL and a reference model over edge signed numbers, extreme bounds, signed-overflow pairs, and mixed-producer sequences (`xadd`, `cmpxchg`, `shld`/`shrd`, `andn`, `blsi`, `bzhi`, `popcnt`) where earlier SF state is updated or preserved. Run `cmake --build --preset dev --target cpu_signed_conds_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_signed_conds$' -V`. The baseline cases run on x64. The remaining native checks require BMI1, BMI2 and POPCNT. On a host missing any of these features, the fixture runs the baseline cases and reports a capability skip.

The shared lowering also handles 32-bit and 64-bit ADX forms (`adcx`, `adox`) and `seto`, tracking `OF` independently from `CF` so interleaved `adcx`/`adox` chains preserve each other's carry flag. `cpu_aot_adx` probes CPUID leaf 7 EBX bit 19 (`ADX`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on ADX-capable hosts it builds all five exports from MASM source (`tests/cpu/adx.asm`), translates them, and compares sums and `(CF << 1) | OF` against the native DLL and a reference model. Run `cmake --build --preset dev --target cpu_adx_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_adx$' -V`.

The shared lowering also handles 128-bit and 256-bit AVX2 integer broadcasts (`vpbroadcastb`, `vpbroadcastw`, `vpbroadcastd`, `vpbroadcastq`) and variable per-lane shifts (`vpsllvd`, `vpsrlvd`, `vpsravd`, `vpsllvq`, `vpsrlvq`), zeroing upper 128 bits on 128-bit VEX destinations and clamping out-of-range shift counts to match hardware. `cpu_aot_avx2_shifts` probes CPUID (`OSXSAVE`, `XCR0` YMM state, and leaf 7 EBX bit 5 `AVX2`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on AVX2-capable hosts it builds all 44 exports from MASM source (`tests/cpu/avx2_shifts.asm`), translates them, and compares outputs against the native DLL and a reference model across 128-bit and 256-bit register/memory operands, register-aliasing forms (`dst == src1`, `dst == src2`, `dst == src1 == src2`), and boundary shift counts while checking deterministic rejections for untracked registers, EVEX encodings, segment prefixes, and EIP-relative operands. Run `cmake --build --preset dev --target cpu_avx2_shifts_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx2_shifts$' -V`.

The shared lowering also handles AVX2 128-bit lane operations, dword blends, and cross-lane permutes (`vbroadcasti128`, `vextracti128`, `vinserti128`, `vpblendd`, `vperm2i128`, `vpermd`, `vpermps`, `vpermq`, `vpermpd`). `cpu_aot_avx2_permutes` probes CPUID (`OSXSAVE`, `XCR0` YMM state, and leaf 7 EBX bit 5 `AVX2`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on AVX2-capable hosts it builds all 17 exports from MASM source (`tests/cpu/avx2_permutes.asm`), translates them, and compares outputs against the native DLL and a reference model. The `vpblendd` memory tests check that native and translated code both fault before writing output when either only masked-off lanes cross into an inaccessible page or all lanes are masked off and the whole operand is inaccessible. Run `cmake --build --preset dev --target cpu_avx2_permutes_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx2_permutes$' -V`.

The shared lowering also handles 128-bit legacy `movdqu` and 14 SSE4.1 integer vector instructions (`pminsb`, `pmaxsb`, `pminuw`, `pmaxuw`, `pminsd`, `pmaxsd`, `pminud`, `pmaxud`, `pmulld`, `pmuldq`, `pcmpeqq`, `packusdw`, `pblendw`, `pblendvb`). `cpu_aot_sse41_int` probes CPUID leaf 1 ECX bit 19 (`SSE4.1`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on SSE4.1-capable hosts it builds all 14 exports from MASM source (`tests/cpu/sse41_int.asm`), translates them, and verifies bit-for-bit agreement with the native DLL. Run `cmake --build --preset dev --target cpu_sse41_int_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse41_int$' -V`.

The shared lowering also handles all 12 SSE4.1 sign/zero-extending vector moves (`pmovsxbw/bd/bq/wd/wq/dq`, `pmovzxbw/bd/bq/wd/wq/dq`) and legacy `movdqu`, reading only the narrow source lane extent for memory operands. `cpu_aot_sse41_extend` probes CPUID leaf 1 ECX bit 19 (`SSE4.1`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on SSE4.1-capable hosts it builds all 13 exports from MASM source (`tests/cpu/sse41_extend.asm`), translates them, and verifies bit-for-bit agreement with the native DLL. Run `cmake --build --preset dev --target cpu_sse41_extend_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse41_extend$' -V`.

Baseline 128-bit SSE2 packed saturating add/sub, multiply, min/max, average, and sum-of-absolute-differences forms (`movdqu`, `paddsb`/`sw`/`usb`/`usw`, `psubsb`/`sw`/`usb`/`usw`, `pmulhw`, `pmulhuw`, `pmullw`, `pmaddwd`, `pmuludq`, `pminub`, `pmaxub`, `pminsw`, `pmaxsw`, `pavgb`, `pavgw`, `psadbw`) are also lowered for legacy XMM encodings while preserving upper YMM bytes and rejecting 64-bit MMX forms. `cpu_aot_sse2_sat_mul` builds all twenty exports from MASM source (`tests/cpu/sse2_sat_mul.asm`), translates them, compares all 16 output bytes against the native DLL and an independent reference (including saturation extremes and `pmaddwd` signed-product wrapping), and checks that 64-bit MMX `paddsb mm0, mm1` is rejected. Run `cmake --build --preset dev --target cpu_sse2_sat_mul_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse2_sat_mul$' -V`.

Legacy and 128-bit VEX AES-NI and carryless-multiply forms (`aesenc`, `aesenclast`, `aesdec`, `aesdeclast`, `aesimc`, `aeskeygenassist`, `pclmulqdq`, and their `v*` 128-bit VEX counterparts) are also lowered using portable FIPS-197 GF(2^8) and GF(2) arithmetic while rejecting 256-bit VAES encodings. `cpu_aot_crypto_aes_pclmul` probes CPUID leaf 1 ECX bits 1 (`PCLMULQDQ`), 25 (`AES`), 27 (`OSXSAVE`), and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without those features; on capable hosts it builds all 15 exports from MASM source (`tests/cpu/crypto_aes_pclmul.asm`), translates them, compares exact 16-byte outputs against the native DLL, and checks that 256-bit `vaesenc` is rejected. Run `cmake --build --preset dev --target cpu_crypto_aes_pclmul_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_crypto_aes_pclmul$' -V`.

The shared lowering also handles all 24 VEX-encoded scalar FMA3 instructions (`vfmadd`/`vfmsub`/`vfnmadd`/`vfnmsub` × `132`/`213`/`231` × `ss`/`sd`) using single-rounding `std::fmaf`/`std::fma`, preserving upper XMM bytes of `dst`, zeroing the upper 128 bits of YMM, and rejecting EVEX forms. `cpu_aot_fma_scalar` probes CPUID (`OSXSAVE`, `XCR0` YMM state, and leaf 1 ECX bit 12 `FMA`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on FMA3-capable hosts it builds the exports from MASM source (`tests/cpu/fma_scalar.asm`), translates them, and verifies bit-for-bit agreement with the native DLL (including exact-cancellation inputs). Run `cmake --build --preset dev --target cpu_fma_scalar_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_fma_scalar$' -V`.

Baseline 128-bit SSE2 packed integer ALU forms (`movdqu`, `paddb`/`w`/`d`/`q`, `psubb`/`w`/`d`/`q`, `pand`, `pandn`, `por`, `pxor`, `pcmpeqb`/`w`/`d`, `pcmpgtb`/`w`/`d`) are also lowered for legacy XMM register operands while preserving upper YMM bytes and rejecting 64-bit MMX forms. `cpu_aot_sse2_int` builds all eighteen exports from MASM source (`tests/cpu/sse2_int.asm`), translates them, compares all 16 output bytes between the native and translated DLLs, and checks that 64-bit MMX `paddb mm0, mm1` is rejected. Run `cmake --build --preset dev --target cpu_sse2_int_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse2_int$' -V`.

SSSE3, SSE4.1, and SSE4.2 integer, insert/extract, `ptest`, and `crc32` forms (`pextrb`/`w`/`d`/`q`, `pinsrb`/`w`/`d`/`q`, `pmaddubsw`, `pmulhrsw`, `phminposuw`, `ptest`, `mpsadbw`, `pcmpgtq`, `crc32`) are also lowered for legacy XMM and GPR encodings. The packed binary ALU and PTEST paths accept tracked XMM register sources. The fixture retains the aligned native MOVNTDQA check and verifies that the translator rejects MOVNTDQA until its alignment and memory contract is supported. `cpu_aot_sse41_42_misc` probes CPUID leaf 1 ECX bits 9 (`SSSE3`), 19 (`SSE4.1`), and 20 (`SSE4.2`) before invoking the native fixture and exits 77 on hosts without those features; on capable hosts it builds the fixture exports from MASM source (`tests/cpu/sse41_42_misc.asm`), translates them, verifies all four `(ZF, CF)` outcomes of `ptest`, 8/32/64-bit `crc32`, and exact vector/scalar outputs against the native DLL, and checks that 64-bit MMX `pmulhrsw mm0, mm1` is rejected. Run `cmake --build --preset dev --target cpu_sse41_42_misc_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse41_42_misc$' -V`.

The shared lowering also handles 128-bit legacy `movdqu` and 14 SSSE3 integer vector instructions (`pabsb`, `pabsw`, `pabsd`, `psignb`, `psignw`, `psignd`, `pshufb`, `palignr`, `phaddw`, `phaddd`, `phsubw`, `phsubd`, `phaddsw`, `phsubsw`), with tracked XMM register sources. The SSSE3 operations reject 64-bit MMX forms, untracked XMM sources, and memory sources. `cpu_aot_ssse3_int` probes CPUID leaf 1 ECX bit 9 (`SSSE3`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on SSSE3-capable hosts it builds all 15 exports from MASM source (`tests/cpu/ssse3_int.asm`), translates them, and verifies bit-for-bit agreement with the native DLL plus rejection controls for all three unsupported source forms. Run `cmake --build --preset dev --target cpu_ssse3_int_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_ssse3_int$' -V`.

Baseline 128-bit SSE2 packed shifts, packs, unpacks, shuffles, and movemask forms (`movdqu`, `psllw`/`d`/`q`, `psrlw`/`d`/`q`, `psraw`/`d`, `pslldq`, `psrldq`, `packsswb`, `packssdw`, `packuswb`, `punpcklbw`/`lwd`/`ldq`/`lqdq`, `punpckhbw`/`hwd`/`hdq`/`hqdq`, `pshufd`, `pshuflw`, `pshufhw`, `pmovmskb`) are also lowered for legacy XMM encodings while preserving upper YMM bytes. The new operations support register and immediate operands and reject legacy memory sources until their alignment requirements are modeled. `movdqu` retains its existing unaligned move semantics. `cpu_aot_sse2_shifts_pack` builds all 37 exports from MASM source (`tests/cpu/sse2_shifts_pack.asm`), translates them, compares outputs against the native DLL and an independent reference across immediate, in-range, boundary, and out-of-range shift counts, and checks that MMX `packsswb` and memory forms of `packsswb`, `psllw`, and `pshufd` are rejected. Run `cmake --build --preset dev --target cpu_sse2_shifts_pack_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse2_shifts_pack$' -V`.

The shared lowering also handles all 36 VEX-encoded packed FMA3 instructions (`vfmadd`/`vfmsub`/`vfnmadd`/`vfnmsub`/`vfmaddsub`/`vfmsubadd` × `132`/`213`/`231` × `ps`/`pd`) in 128-bit and 256-bit forms using single-rounding `std::fmaf`/`std::fma` and rejecting EVEX forms. `cpu_aot_fma_packed` probes CPUID (`OSXSAVE`, `XCR0` YMM state, and leaf 1 ECX bit 12 `FMA`) before invoking the native fixture and exits 77 (capability skip) on unsupported hosts; on FMA3-capable hosts it builds all 44 exports from MASM source (`tests/cpu/fma_packed.asm`), translates them, and compares raw result bits with the native DLL across finite inputs, quiet and signaling NaNs, and pairs or triples with distinct NaN signs and payloads. The NaN result follows the arithmetic operand order for that encoding before any sign change. Extra cases cover 128-bit writes clearing the high YMM half and a 256-bit memory source. Finite-operand cases add a `132` memory form, 128-bit `vfnmadd`, `vfmaddsub` and `pd` forms, a destination that is also the first source, and a `vfmadd132pd` whose single rounding gives -2^-104 where separate rounding gives 0. These comparisons check result bytes under the host's default masked floating-point environment. Run `cmake --build --preset dev --target cpu_fma_packed_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_fma_packed$' -V`.

The shared lowerer accepts register-only VEX.256 `vminps` and `vmaxps` in its existing YMM0–YMM2 storage. With masked SIMD exceptions it uses two ordered host 128-bit operations, which retain second-operand selection for signed zeros, NaNs and denormals under DAZ. The MASM fixture compares both operand orders lane by lane against an integer bit reference for MXCSR 0x1f80 and 0x1fc0. Memory-source, untracked-register and EVEX forms are rejected. Run `cmake --build --preset dev --target cpu_avx_packed_fp_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_packed_fp$' -V`.

Baseline 128-bit SSE3 and SSE4.1 floating-point forms (`addsubps`, `addsubpd`, `haddps`, `haddpd`, `hsubps`, `hsubpd`, `movddup`, `movsldup`, `movshdup`, `lddqu`, `roundps`, `roundpd`, `roundss`, `roundsd`, `dpps`, `dppd`, `blendps`, `blendpd`, `blendvps`, `blendvpd`, `insertps`, `extractps`) are also lowered for legacy XMM register and memory encodings (and 32-bit GPR/memory destinations for `extractps`) while preserving upper YMM bytes and rejecting 64-bit MMX, VEX, and untracked XMM forms. `cpu_aot_sse34_fp` probes CPUID leaf 1 ECX bits 0 (`SSE3`) and 19 (`SSE4.1`) before invoking the native fixture and exits 77 on hosts without those features; on capable hosts it builds all exports from MASM source (`tests/cpu/sse34_fp.asm`), translates them, verifies bit-exact agreement against both the native DLL and an independent Python IEEE-754 reference (for `dpps`/`dppd` the result must match the native DLL bit-for-bit and lie in the set of pairwise reduction operand orders, because the chosen NaN is vendor-defined when multiple products are NaN) across NaN payloads, signed zeros, denormals, mixed-sign operands, unaligned `lddqu` and narrow memory operands (`movddup` `m64`, `roundss`/`insertps`/`extractps` `m32`, `roundsd` `m64`), all four rounding modes plus MXCSR and precision-suppression immediates, all lane-select immediates for `blendps`/`blendpd`/`insertps`/`extractps`, all 16 `dppd` and 18 representative `dpps` masks, and mutated lifted negative controls that must mismatch, while checking that MMX, VEX, untracked XMM, and `movntdqa` encodings stay rejected. Run `cmake --build --preset dev --target cpu_sse34_fp_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse34_fp$' -V`.

Legacy packed m128 sources in this profile require 16-byte alignment. Four source-built misalignment cases use readable storage at +4 and compare the Windows read-access violation and untouched output. LDDQU and the existing narrow memory forms retain their unaligned behavior. The DPPS/DPPD reference checks each enabled lane against the independently computed set of permitted words, with exact native and lifted words and exact zero in all disabled lanes.

VEX 128-bit and 256-bit AVX packed integer ALU forms (`vpaddb`/`w`/`q`, `vpsubb`/`w`/`d`/`q`, `vpand`, `vpandn`, `vpor`, `vpcmpeqb`/`w`/`d`/`q`, `vpcmpgtb`/`w`/`d`/`q`, `vpabsb`/`w`/`d`, `vpsignb`/`w`/`d`, `vpshufb`, `vpalignr`) are also lowered for `EncodingKind.VEX` while zeroing upper YMM bytes for 128-bit destinations and rejecting EVEX encodings. `cpu_aot_avx_packed_int` probes CPUID leaf 1 ECX bits 27 (`OSXSAVE`) and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without AVX; on AVX-capable hosts it builds all 26 exports from MASM source (`tests/cpu/avx_packed_int.asm`), translates them, compares exact 32-byte outputs (verifying upper-128-bit zeroing) against the native DLL, and rejects EVEX `vpaddb` and vector registers outside the shared profile. If CPUID reports AVX2, a separate 256-bit `vpshufb` with a 32-byte memory source checks both unequal shuffle lanes against a byte reference. The AVX1 comparisons still run before a missing AVX2 capability returns 77. Run `cmake --build --preset dev --target cpu_avx_packed_int_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_packed_int$' -V`.

VEX 128-bit and 256-bit AVX packed saturating add/sub, min/max, average, multiply, multiply-add, and SAD forms (`vpaddsb`/`sw`/`usb`/`usw`, `vpsubsb`/`sw`/`usb`/`usw`, `vpminsb`/`ub`/`sw`/`uw`/`sd`/`ud`, `vpmaxsb`/`ub`/`sw`/`uw`/`sd`/`ud`, `vpavgb`/`w`, `vpmulhw`/`huw`/`lw`/`ld`/`dq`/`udq`/`hrsw`, `vpmaddwd`/`ubsw`, `vpsadbw`, `vmpsadbw`) are also lowered for `EncodingKind.VEX` while zeroing upper YMM bytes for 128-bit destinations and rejecting EVEX encodings. `cpu_aot_avx_int_minmax_mul` probes CPUID leaf 1 ECX bits 27 (`OSXSAVE`) and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without AVX; on AVX-capable hosts it builds all 33 exports from MASM source (`tests/cpu/avx_int_minmax_mul.asm`), translates them, compares exact 32-byte outputs against the native DLL, and rejects EVEX `vpmulld` and registers outside the shared profile. The first `vpmaddwd` result checks the 0x80000000 wraparound. On AVX2, a 256-bit `vmpsadbw` with a 32-byte memory source checks different immediate-selected ranges in its two lanes. The 128-bit comparisons complete before the 256-bit capability skip. Run `cmake --build --preset dev --target cpu_avx_int_minmax_mul_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_int_minmax_mul$' -V`.

VEX 128-bit and 256-bit AVX packed sign/zero-extend, horizontal integer add/sub, and horizontal minimum forms (`vpmovsxbw`/`bd`/`bq`/`wd`/`wq`/`dq`, `vpmovzxbw`/`bd`/`bq`/`wd`/`wq`/`dq`, `vphaddw`/`d`/`sw`, `vphsubw`/`d`/`sw`, `vphminposuw`) are also lowered for `EncodingKind.VEX` while zeroing upper YMM bytes for 128-bit destinations and rejecting EVEX encodings. `cpu_aot_avx_int_extend_hadd` probes CPUID leaf 1 ECX bits 27 (`OSXSAVE`) and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without AVX; on AVX-capable hosts it compares 19 XMM MASM cases against the translated 32-byte outputs, including a minimum-position tie. AVX2 hosts also compare YMM `vpmovsxbd` from an 8-byte memory operand and `vphaddd` from a 32-byte memory operand against both native outputs and independent lane references. The helper refuses EVEX and registers outside the supported profile. `vphminposuw` has only an XMM form. Run `cmake --build --preset dev --target cpu_avx_int_extend_hadd_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_int_extend_hadd$' -V`.

VEX 128-bit and 256-bit AVX horizontal floating-point add and subtract forms (`vhaddps`, `vhaddpd`, `vhsubps`, `vhsubpd`) are also lowered for `EncodingKind.VEX` using 128-bit SSE3 intrinsics per lane while zeroing upper YMM bytes for 128-bit destinations, supporting unaligned `m128`/`m256` memory sources, and rejecting EVEX, non-VEX, and untracked register encodings. `cpu_aot_avx_horiz_fp` probes CPUID leaf 1 ECX bits 27 (`OSXSAVE`) and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without AVX; on AVX-capable hosts it builds all 22 exports from MASM source (`tests/cpu/avx_horiz_fp.asm`), translates them, compares exact 32-byte outputs (including upper-16-byte zeroing, unaligned memory sources, register aliasing, QNaN/SNaN payloads, signed zeros, infinities, subnormals, and 7 MXCSR rounding/DAZ/FTZ modes) against both the native DLL and an independent IEEE-754 Python reference, and verifies rejection and mutated controls. Run `cmake --build --preset dev --target cpu_avx_horiz_fp_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_horiz_fp$' -V`.

VEX 128-bit and 256-bit AVX alternating floating-point add and subtract forms (`vaddsubps`, `vaddsubpd`) use the same per-lane lowering with `_mm_addsub_ps` and `_mm_addsub_pd`: even-indexed elements compute `a - b` and odd-indexed elements compute `a + b` in each 128-bit lane, 128-bit destinations zero upper YMM bytes, unaligned `m128`/`m256` memory sources are accepted, and untracked registers, segment prefixes and EIP-relative operands are rejected. `cpu_aot_avx_addsub_fp` builds 14 exports from MASM source (`tests/cpu/avx_addsub_fp.asm`), exits 77 on hosts without AVX, and compares exact 32-byte outputs (register and memory forms, register aliasing, QNaN/SNaN payloads in either operand, signed-zero cancellation in the add and subtract positions, infinities, overflow, subnormals and the same 7 MXCSR rounding/DAZ/FTZ modes) against both the native DLL and the IEEE-754 reference model shared with `cpu_aot_avx_horiz_fp`. Three mutated controls (all-add, all-subtract and a missing upper-byte clear) must mismatch. Run `cmake --build --preset dev --target cpu_avx_addsub_fp_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_addsub_fp$' -V`.

`lahf` loads AH with SF, ZF, AF and PF, bit 1 set, bits 5 and 3 clear, and CF. The shared lowering keeps AF and PF only where a `lahf` reads them. Each `lahf` must take its flags from a register `add`, `sub` or `cmp` (register or immediate source) earlier in the same straight-line block, and that instruction must be the last writer of any status flag before it (`lahf_sources`). That writer then also assigns `aux = ((a ^ b ^ v) & 0x10) != 0` and the even parity of the low result byte, so every flag `lahf` reads is defined at the use. The `lahf` statement replaces bits 15:8 of RAX and nothing else, and changes no flag. Translation refuses `lahf` at the function entry, at a branch target, across a branch, a call or a branch target between the writer and `lahf`, in a function with an indirect branch, and after any other flag writer: `test`, `and`, `or`, `xor`, shifts, `imul`, `neg`, `inc`, `dec`, `adc`, `sbb`, an `add` with a memory destination and a `cmp` with a memory source. `inc` and `dec` stay refused because their CF comes from before them, and `sahf` outside the x87 profile is unchanged. The x87 suites still refuse `lahf` after `fucomi` and after `fcom` with the "unsupported query operation" message they expect. `cpu_aot_lahf` builds 20 exports from MASM source (`tests/cpu/lahf.asm`): `add`, `sub` and `cmp` at 8, 16, 32 and 64 bits, three immediate forms, a writer whose destination is RAX, `cmp ah, cl`, `sub al, dl`, flag-neutral `mov` and `lea` between the writer and `lahf`, and a consumer after `lahf` (`setl`, which reads SF and OF, and `jbe`, which reads CF and ZF). Every export loads RAX from its third argument before the writer and returns the whole RAX. That shows `lahf` replaces AH only, including reserved bits 5, 3 and 1 under all-clear and all-set RAX. 22 operand values near each carry, sign and nibble boundary are tried pairwise under four RAX values. Each result is compared with the native DLL and with a reference model, and the run must see SF, ZF, AF, PF and CF both set and clear. The test exits 77 when CPUID.80000001H:ECX bit 0 (LAHF/SAHF in 64-bit mode) is clear. 19 refused shapes must raise the query refusal, and five mutated translations must each mismatch: AF never set, odd parity, `lahf` writing the whole of RAX, reserved bit 1 clear, and `lahf` inverting OF before the consumer. On an Intel Core i5-14600K (Windows 11 build 26300) all 38,720 cases matched in the `dev` and `release` presets. Run `cmake --build --preset dev --target cpu_lahf_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_lahf$' -V`.

VEX 128-bit and 256-bit AVX packed shifts, saturating packs, unpacks, shuffles, integer blends, `vpmovmskb`, and `vpextr*`/`vpinsr*` forms (`vpsllw`/`d`/`q`, `vpsrlw`/`q`, `vpsraw`/`d`, `vpslldq`, `vpacksswb`/`dw`, `vpackuswb`/`dw`, `vpunpckl*`/`h*`, `vpshufd`/`lw`/`hw`, `vpblendw`, `vpblendvb`, `vpmovmskb`, `vpextrb`/`w`/`d`/`q`, `vpinsrb`/`w`/`d`/`q`) are also lowered for `EncodingKind.VEX` while zeroing upper YMM bytes for 128-bit destinations and rejecting EVEX encodings. `cpu_aot_avx_int_shifts_pack` probes CPUID leaf 1 ECX bits 27 (`OSXSAVE`) and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without AVX; on AVX-capable hosts it compares the 34 XMM cases in `tests/cpu/avx_int_shifts_pack.asm` against native 32-byte and scalar outputs and rejects EVEX and unmodeled registers. Its `vpsrlw` memory-count control uses the low 64 bits of a readable 128-bit operand and requires a fault before writing output when the higher eight bytes cross into an inaccessible page. On AVX2 hosts a YMM memory `vpshufd` checks the two 128-bit lanes against distinct expected bytes. Run `cmake --build --preset dev --target cpu_avx_int_shifts_pack_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_int_shifts_pack$' -V`.

VEX 128-bit and 256-bit AVX broadcast, lane extract/insert, permute, shuffle, unpack, blend, dot-product, round, and `vzeroall` forms (`vbroadcastss`/`sd`/`f128`, `vextractf128`, `vinsertf128`, `vperm2f128`, `vpermilps`/`pd`, `vshufps`/`pd`, `vunpcklps`/`hps`/`lpd`/`hpd`, `vmovddup`/`sldup`/`shdup`, `vblendps`/`pd`/`vps`/`vpd`, `vdpps`/`pd`, `vroundps`/`pd`, `vzeroall`) are also lowered for `EncodingKind.VEX` while rejecting MXCSR rounding mode (`imm8 & 4 != 0`). `cpu_aot_avx_shuffle_blend` probes CPUID leaf 1 ECX bits 27 (`OSXSAVE`) and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without AVX; on AVX-capable hosts it builds all 27 exports from MASM source (`tests/cpu/avx_shuffle_blend.asm`), translates them, compares exact 32-byte outputs against the native DLL, and checks that MXCSR-mode `vroundps` is rejected. The XMM memory form of `vmovddup` is also run with exactly eight readable bytes before a guard page. The `vdpps` cancellation case checks pairwise rounding. For multiple NaN inputs the selected lanes must be NaN, while the other lanes are compared bit for bit. Run `cmake --build --preset dev --target cpu_avx_shuffle_blend_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_shuffle_blend$' -V`.

VEX 128-bit and 256-bit AVX conversion, compare, flag-test, and aligned or unaligned move forms (`vcvtdq2ps`/`pd`, `vcvtps2dq`/`pd2dq`, `vcvttps2dq`/`ttpd2dq`, `vcvtps2pd`/`pd2ps`, `vcvtsi2ss`/`sd`, `vcvtss2sd`/`sd2ss`, `vcvtss2si`/`sd2si`, `vcvttss2si`/`ttsd2si`, `vcmpps`/`pd`/`ss`/`sd`, `vptest`, `vtestps`/`pd`, `vmovaps`/`apd`/`dqa`, `vlddqu`) are also lowered for `EncodingKind.VEX` while rejecting EVEX encodings. `cpu_aot_avx_cvt_cmp_test` probes CPUID leaf 1 ECX bits 27 (`OSXSAVE`) and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without AVX; on AVX-capable hosts it builds the exports from MASM source (`tests/cpu/avx_cvt_cmp_test.asm`), translates them, compares exact 32-byte/scalar outputs and all four `(ZF, CF)` outcomes of `vptest`/`vtestps`/`vtestpd` against the native DLL, and checks that EVEX `vcvtdq2ps` and streaming `vmovntdqa` are refused. Compare predicates use all five immediate bits. The fixture checks the four MXCSR rounding modes for float-to-integer conversions and distinguishes the signaling predicate using both the output bits and the invalid flag. Run `cmake --build --preset dev --target cpu_avx_cvt_cmp_test_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_cvt_cmp_test$' -V`.

VEX AVX scalar floating-point, half-register move, `vinsertps`/`vextractps`, and `vcomiss`/`vucomiss`/`vcomisd`/`vucomisd` forms (`vaddss`/`sd`, `vsubss`/`sd`, `vmulss`/`sd`, `vdivss`/`sd`, `vsqrtss`/`sd`, `vminss`/`sd`, `vmaxss`/`sd`, `vroundss`/`sd`, `vmovss`, `vmovhlps`/`lhps`/`lps`/`hps`/`lpd`, `vinsertps`, `vextractps`, `vcomiss`/`sd`, `vucomiss`/`sd`) are also lowered for `EncodingKind.VEX` while zeroing upper YMM bytes and rejecting EVEX encodings and MXCSR rounding mode. `cpu_aot_avx_scalar_fp` probes CPUID leaf 1 ECX bits 27 (`OSXSAVE`) and 28 (`AVX`) plus XCR0 XMM/YMM state before invoking the native fixture and exits 77 on hosts without AVX; on AVX-capable hosts it builds all 27 exports from MASM source (`tests/cpu/avx_scalar_fp.asm`), translates them, compares exact 32-byte/scalar outputs and all four ordering outcomes (`<`, `==`, `>`, unordered) against the native DLL, and checks that EVEX `vaddss` is rejected. Separate raw-bit checks cover quieting signaling NaNs for scalar round and the effect of DAZ on scalar min/max and round-down. In masked mode the COMI and UCOMI checks compare the MXCSR invalid flag for signaling and quiet NaNs. Run `cmake --build --preset dev --target cpu_avx_scalar_fp_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_avx_scalar_fp$' -V`.

The shared lowerer accepts legacy XMM packed compares (`cmpps` and `cmppd`), low and high 64-bit memory moves (`movlps`/`movhps` and `movlpd`/`movhpd`), and aligned XMM moves (`movaps`, `movapd` and `movdqa`). The compares use the low three immediate bits and the corresponding SSE intrinsic. Legacy memory compares and aligned moves enforce 16-byte alignment. Half moves access exactly eight bytes and preserve the other half of a destination register. `cpu_aot_sse2_packed_cvt` compares all 22 source-built exports with the native DLL. It includes register and memory packed-double comparisons and rejects a compare that writes nonvolatile XMM6. Separate controls verify packed conversions under round-down and round-toward-zero and scalar min with DAZ enabled. They restore the caller's MXCSR after the comparison. Run `cmake --build --preset dev --target cpu_sse2_packed_cvt_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_sse2_packed_cvt$' -V`.

## Recompiling a direct x64 call

The [caller and callee](../tests/cpu/direct.asm) each accept two unsigned 64-bit integers and an output pointer. The [translator](../tools/cpu_call_aot.py) starts at the caller export, follows the signed displacement in its E8 call and reads the target's runtime-function interval. It generates two C++ functions from the linked machine code. MSVC emits a separate x64 DLL with a non-inlined call. The Windows test checks the relative call against the two export addresses in both the input and output DLLs.

```powershell
cmake --build --preset dev --target cpu_call_original_lifted cpu_call_altered_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_direct_call$' -V
```

The caller changes both arguments and keeps its original inputs live across the call. The callee records its arguments and arithmetic result through R8. The caller uses the returned RAX and both live inputs to compute its result. A separate assembly probe seeds physical RBX and RSI and reads them immediately after each invocation. Tests exercise the whole call and the callee in isolation using the same 249 input pairs, including wraparound and high-word differences. They compare the return, three output words, adjacent guards and both register sentinels with the original and independent arithmetic.

A rebuilt callee that increments only its return after storing the result exposes a mismatch for every pair. A register-clobber control checks the probe itself. Targets redirected into the middle of the callee or back to the caller are rejected during translation.

A [second caller](../tests/cpu/repeat_call.asm) calls the same callee twice. Only RBX and RSI survive each call, so it parks the trace pointer in RSI and copies it back to R8 for the second call, whose arguments depend on the first result. The translator decodes 64-bit moves between R8 and the profile registers in both the opcode 8B form that ML64 emits and the opcode 89 form. A repeated direct call to the same target lifts that callee once, and a repeated import call through the same IAT slot binds that import once. It tracks which registers still hold the entry R8 value, so R8 can only be reloaded from such a copy and every store stays inside the trace.

```powershell
cmake --build --preset dev --target cpu_repeat_original_lifted cpu_repeat_altered_lifted cpu_repeat_dropped cpu_repeat_import_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_repeated_call$' -V
```

For the same 249 input pairs, the caller and callee match the original and independent arithmetic (for both the direct-call fixture and the repeated-import `IMPORT_MIX` build). The comparison covers the return, the trace written by the second call, the guards and the RBX/RSI sentinels. With a callee that returns one more, the caller's return and trace change on every pair. Both the original and the lifted DLL contain two rel32 calls to their callee. Replacing both R8 moves with their opcode 89 encodings leaves the translation unchanged. Translation rejects mixed direct/import calls, two distinct IAT slots, and six patched or rebuilt copies:
- one assembled without the R8 reload, as an undefined register;
- the first or the second call redirected into the callee interior, which owns no function interval;
- both calls redirected there;
- both calls redirected back to the caller;
- R8 reloaded from RBX, which holds an input word.

Hand-built callers that combine a direct call and an import call are rejected in either order, as mixed import and direct calls. A caller that calls two different IAT slots is rejected as a call to a second IAT slot.

A [third caller](../tests/cpu/call_pair.asm) calls two different callees, `mix_args` and then `mix_other`, which combines its arguments with XOR so the two calls do not commute. The translator lifts each distinct direct target once, in call order, as `lifted_callee`, `lifted_callee2` and so on. Each target must own a function interval that overlaps neither the caller nor another callee.

```powershell
cmake --build --preset dev --target cpu_pair_original_lifted cpu_pair_swapped_lifted cpu_pair_first_lifted cpu_pair_second_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_call_pair$' -V
```

For the same 249 input pairs, the caller and all four lifted callees match the original and independent arithmetic, with guards and RBX/RSI sentinels. A copy assembled with the two calls swapped follows its own reference and changes all 249 results, and its lifted DLL names `mix_other` first. Copies with only `mix_args` or only `mix_other` returning one more each change the caller's return on every pair. Each original and lifted caller contains exactly one rel32 call to each of its callees. Translation rejects three redirected calls: either call into the other callee's interior, and the second call back to the caller. It also rejects a copy whose `mov r8, rsi` before the second call is replaced by `mov rsi, rsi`, because R8 does not survive the first call. `mix_args` writes its `+ 11` as the signed-imm8 form `sub rax, -11`. A copy whose `mix_args` runtime-function entry is extended into `mix_other` is refused earlier, by the range reader, as an invalid runtime-function interval.

Coverage is limited to this integer ABI, its two fixed frame layouts and the decoded register operations. The fixtures exercise two distinct callees at most, and not a caller that returns to an earlier callee after calling another. They exercise the R8 moves only through RSI, and import calls are still limited to one IAT slot per caller. [Windows x64 calling convention](https://learn.microsoft.com/en-us/cpp/build/x64-calling-convention) · [Import-call follow-up](https://github.com/R0Y00/Xbox2Windows/issues/24)

## Calling another DLL through a named PE import

The source-built caller uses an x64 RIP-relative indirect call (`FF 15 disp32`). The translator computes the import address table (IAT) slot RVA from the end of the instruction and looks up the exact slot in the ordinary import descriptors. The build supplies an explicit binding for the fixture's integer ABI. The DLL name and case-sensitive symbol must match the slot, and the provider must have a local executable export for that name. MSVC compiles the generated caller and links the provider's import library. The test independently checks that the call in each linked caller addresses its own slot for `cpu_import_provider.dll!mix_args`. [PE import address table](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#import-address-table)

```powershell
cmake --build --preset dev --target cpu_import_original_lifted cpu_import_altered cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_import_call$' -V
```

Each provider configuration runs in a fresh Windows process. The provider records the two arguments it received and its arithmetic result in three output words. For 249 pairs the test compares both callers with independent arithmetic, checks the adjacent guard words and samples RBX and RSI. Replacing the provider with one that increments its return changes the caller's result on every pair and preserves the output writes.

With the provider absent, both callers fail to load with error 126. With a DLL of the expected name that lacks the requested export, both fail with error 127. The translator also diagnoses the missing provider or export before compilation and rejects a call displaced one byte into the IAT slot. The verified profile covers one ordinary named import with two 64-bit integers and an output pointer. [Ordinal-only import follow-up](https://github.com/R0Y00/Xbox2Windows/issues/26)

## Calling an ordinal-only export

The source-built provider exposes only ordinal 77 through a NONAME export. The build binds `#77` with the local import-library symbol `mix_args`. The translator checks the DLL and ordinal from the call's IAT slot against the actual export address table. The test reads both linked callers' import lookup words, checks the ordinal flag and value, and verifies the provider has no exported name for the entry.

```powershell
cmake --build --preset dev --target cpu_ordinal_original_lifted cpu_ordinal_altered cpu_register_probe cpu_import_provider
ctest --preset dev --no-tests=error -R '^cpu_aot_ordinal_import$' -V
```

For 249 input pairs both callers match the provider's observed arguments, guarded writes, register sentinels and expected return. Replacing the provider changes the return on all inputs. Both callers fail to load with error 126 when the DLL is absent and error 182 when the DLL exports the same link name but lacks ordinal 77. The static translator diagnoses a mismatched binding, a missing link name, a non-ASCII link name, a missing ordinal and a displacement into the middle of a slot. When the provider is renamed `ws2_32.dll`, pefile names ordinal 77 from its built-in table. The translator still requires the `#77` binding and rejects that name.

The local Bink DLL imports ordinal 1 from XAudio2_9.dll. A read-only check confirmed an executable export at that ordinal in this workstation's System32 provider. The Bink call's argument and result contract remains unverified.

## x87 in the shared lowering

`lower(..., x87=True)` in `tools/ffx_version_query.py` accepts an opt-in x87 leaf-function profile. The default (`x87=False`) still rejects every x87 instruction, so existing callers are unchanged. The profile accepts `fld` and `fstp` with an m32, m64 or m80 memory operand, `fst` with an m32 or m64 operand, `fld st(i)`, `fst st(i)`, `fstp st(i)`, `fxch st(i)`, `fchs`, `fabs`, `fsqrt`, `frndint`, `fxtract` and `fscale`, the constant loads `fld1`, `fldz`, `fldpi`, `fldl2t`, `fldl2e`, `fldlg2` and `fldln2`, `fild`, `fistp` and `fisttp` with an m16, m32 or m64 integer operand, `fist` with m16 or m32, `fbstp` with an m80 packed BCD operand, `fcomi`, `fcomip`, `fucomi` and `fucomip` with `st, st(i)`, `fcmovb`, `fcmove`, `fcmovbe`, `fcmovnb`, `fcmovne`, `fcmovnbe`, `fcmovu` and `fcmovnu` with `st, st(i)`, `fcom` and `fcomp` with `st(i)` or an m32 or m64 operand, `fcompp`, `fucom` and `fucomp` with `st(i)`, `fucompp`, `fnstsw ax`, and five forms of each of `fadd`, `fsub`, `fsubr`, `fmul`, `fdiv` and `fdivr`: an m32 or m64 operand, `st, st(i)`, `st(i), st`, and the popping form (`faddp`, `fsubp`, `fsubrp`, `fmulp`, `fdivp`, `fdivrp`) with `st(i), st` for `i` from 1 to 7. It also accepts `fiadd`, `fisub`, `fisubr`, `fimul`, `fidiv` and `fidivr` with an m16 or m32 integer operand, `fnstcw` and `fstcw` with an m16 operand, which store the fixed control word `0x027F`, and `wait`, which does nothing because every exception is masked. A popping form with `st(0)` as the destination is rejected. A function enters with control word 0x027F (53-bit precision, round to nearest-even, all exceptions masked), TOP 0, all eight registers empty and a clear status word, and it must return with the stack empty. The stack depth before each instruction is fixed at translation time (`tools/x87_lower.py`), so each register becomes a C++ slot and no TOP exists at run time. Translation rejects any other x87 instruction, including `fldcw`, `fninit` and `fbld` (which only the checked entry below accepts), `ffree`, `fnstsw` with a memory operand, and environment access; a ninth push; a pop, an `st(i)` operand or `faddp` without enough values; depths that differ where control flow joins; an x87 instruction that is not reachable from the entry through direct edges; and a nonempty stack at a return, a call, an indirect branch or an external edge.

The helpers in `tools/x87_soft.h` compute in integers and do not use host floating point. A register holds the full 80-bit encoding. m80 loads and stores copy all 80 bits, including NaN payloads, denormals, pseudo-denormals and unnormals. m32 and m64 loads convert exactly and quiet a signaling NaN. Under the fixed control word a sum rounds to 53 significand bits within the extended exponent range; a result below that range is denormalized first and then rounded at the same bit positions. The sum rounds again when it is stored to m32 or m64. NaN operands follow the x87 column of Intel SDM Vol. 1, Table 4-7: a QNaN beats an SNaN, and otherwise the larger significand wins. When two NaNs have equal significands, the positive one wins in either operand order. This tie rule was measured, not taken from the table. Infinities of opposite sign, unnormals, pseudo-infinities and pseudo-NaNs give the indefinite. An exact zero sum of nonzero addends is +0 (-0 when rounding toward negative infinity), and -0 + -0 is -0. Every rounding helper takes the control word and follows its precision and rounding control. The fixed profile passes `0x027F`, so only the checked entry below reaches the other words. Exceptions are not modelled, and of the status word only C0, C2, C3 and TOP are (see `cpu_aot_x87_status`). This is safe only because translation rejects every reader of the other status-word bits, and the checked entry stops at a word that unmasks an exception.

`cpu_aot_x87_add` builds 16 exports from generated MASM source (`tests/cpu/x87_add.asm`, from `tests/cpu/x87_add.py asm`) and translates them. The exports cover the nine load/store width pairs, additions stored to each width, and a three-value chain that uses `faddp st(2), st`. A native-only harness (`tests/cpu/x87_harness.asm`) saves the caller's whole x87 state with `fnsave` (environment and register values), which also leaves TOP 0 and an empty stack. It then loads control word 0x027F, calls the export, records the control, status and tag words the export left, and restores the caller's state with `frstor`. A harness check enters each export with a live caller value (pi with all 64 significand bits) in the register the export reuses and control word 0x0F7F loaded. It requires the value bytes, the control word, TOP and the tags to come back unchanged. A control harness that keeps only the environment (`fnstenv`/`fldenv`) must lose the value around every export. Native and translated exports receive the same input bytes and run through that harness. The check compares each output block and the 64 guard bytes on each side of the input and output against an independent `Fraction` model. It requires every export to return with control word 0x027F, TOP 0 and all tags empty. The 8085 cases include all pairs of 34 special encodings, halfway sums that separate 53-bit from 64-bit precision, double rounding through m64, sums beyond the binary64 range, extended-range overflow and underflow, denormal results, cancellation, NaN ties and 700 random pairs. Six mutated translations must each mismatch: 64-bit precision, the binary64 exponent range, m80 loads through binary64, a dropped sticky bit, the first NaN operand always winning, and -0 for an exact cancellation. On an AMD Ryzen 7 7700 (Windows 11 build 26200), every case matched in the native and translated DLLs, and the controls gave 1089, 3845, 1485, 48, 58 and 18 mismatches. On an Intel Core Ultra 7 358H (Windows 11 build 26300), every case likewise matched in the native and translated DLLs, and the controls gave 1089, 3845, 1485, 48, 58 and 18 mismatches. The check also confirms that the default lowering rejects `fld` and that the profile rejects 16 sequences outside it. Only `faddp st(1), st` and `faddp st(2), st` are exercised. Run `cmake --build --preset dev --target cpu_x87_add_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_add$' -V`.

`cpu_aot_x87_mul` checks the multiplication forms the same way, through the same harness, guards and live-value check, with 12 exports and 14951 cases. The product of the two 64-bit significands is exact in 128 bits and rounds like a sum. Its sign is the exclusive or of the operand signs, including for zero and infinite results, and infinity times zero gives the indefinite. An m32 or m64 operand of `fmul` converts exactly but keeps a signaling NaN signaling, so the NaN rules see the operand's own kind. A plain `fld` quiets it on load. The cases cover every pair of the 34 special encodings, each special m80 value times each of 17 m32 and 18 m64 special encodings, products that are exact halfway cases at 53 bits, products whose upper 64 bits sit exactly on a tie so that only the low 64 bits decide, `x * 1` rounding, products beyond the binary64 range, extended overflow and underflow thresholds, denormal results, the square through `fmul st, st(0)`, a three-value chain through `fmulp st(2), st`, a product followed by an add, and random operands. Six mutated translations must each mismatch: 64-bit precision, the binary64 exponent range, dropping the low 64 bits of the product, the first operand's sign, infinity times zero giving infinity, and quieting memory operands before the NaN rules. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 4836, 8059, 62, 4409, 73 and 36 mismatches. The check also confirms that translation rejects nine sequences, including `fmulp st(0), st`, `fldenv`, `fdecstp`, `fprem1`, `fincstp`, and multiplications without enough stack values. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_mul$' -V`.

`cpu_aot_x87_div` checks the division forms in the same way, with 17 exports and 23939 cases. `fdivr` and `fdivrp` divide the source by the destination. Each export's decoded x87 instructions must match the forms its sequence names, so an assembler that exchanged the `fdiv` and `fdivr` register encodings would fail before the run. The helper divides the 64-bit significands into a 64-bit quotient and folds a nonzero remainder into a sticky bit. That is enough to round to 53 bits, and the result goes through the same rounding as the sum and the product. Zero by zero and infinity by infinity give the indefinite. A finite nonzero value by zero gives a signed infinity (the masked divide-by-zero response). A finite value by infinity, and zero by a finite value, give a signed zero. The cases cover every pair of the 34 special encodings and every special m80 value against the m32 and m64 special encodings, in both orders. They also cover exact quotients halfway between 53-bit neighbours, quotients whose 64-bit significand ends exactly on a tie so that only the remainder decides, `x / x` through `fdiv st, st(0)`, quotients beyond the binary64 range, the extended overflow and underflow thresholds, denormal results, two three-value chains through `fdivp st(1), st`, `fdivp st(2), st` and `fdivrp st(1), st`, and random operands. Seven mutated translations must each mismatch: 64-bit precision, the binary64 exponent range, an ignored remainder, the indefinite for a finite value by zero, `fdivr` in the `fdiv` order, an unsigned zero quotient, and quieting memory operands before the NaN rules. On the AMD Ryzen 7 7700 every case matched in the native and translated DLLs, and the controls gave 8857, 14373, 253, 602, 6829, 551 and 54 mismatches; on the Intel Core Ultra 7 358H (Windows 11 build 26300) every case likewise matched, with the controls giving 8785, 14373, 253, 602, 6829, 551 and 54 mismatches. Translation still rejects `fdivp st(0), st`, `fdivrp st(0), st`, `fldenv`, `fdecstp`, `fnsave`, `fprem`, `fincstp` and divisions without enough stack values. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_div$' -V`.

`cpu_aot_x87_sub` checks `fsub`, `fsubr`, `fsubp`, `fsubrp` and the `fadd` forms besides `faddp` in the same way, with 22 exports and 35023 cases. `fsubr` and `fsubrp` subtract the destination from the source. The helper computes `a - b` as `a + (-b)`, but it selects a NaN or invalid operand before negating, so a NaN result keeps its own sign. `x - x` is +0, and -0 - +0 is -0. The cases reuse the addition pairs of `cpu_aot_x87_add`, and again with the second operand negated. They also cover `x - x` and `x + x` through `st(0)`, m32 and m64 operands against the special encodings, `float` and `double` differences, a three-value chain through `fsubp st(2), st` and `fsubrp st(1), st`, and random operands. Seven mutated translations must each mismatch: 64-bit precision, the binary64 exponent range, negating a NaN subtrahend, `fsubr` in the `fsub` order, -0 for an exact cancellation, a dropped sticky bit, and quieting memory operands before the NaN rules. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 15680, 24289, 2022, 7753, 777, 108 and 81 mismatches. Translation still rejects `fsubp st(0), st`, `fsubrp st(0), st`, `fldenv`, `fdecstp`, `fxsave`, `fnstenv`, `fcos` and arithmetic without enough stack values. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_sub$' -V`.

`cpu_aot_x87_moves` checks the register data movement in the same way, with 26 exports and 20224 cases. `fld st(i)` pushes a copy of `st(i)`, `fst st(i)` copies `st(0)` into `st(i)`, and `fstp st(i)` does the same and then pops. `fxch st(i)` exchanges `st(0)` and `st(i)`. These copy the full 80-bit encoding, so a signaling NaN stays signaling and an unnormal keeps its bits. `fchs` and `fabs` change only the sign bit, of every encoding, including zeros, NaNs, unnormals and pseudo-denormals. `fst` to m32 or m64 rounds and stores `st(0)` like `fstp` and leaves the register unchanged. The model moves raw encodings and shares no code with `tools/x87_soft.h`. The exports cover `fld st(0)`, `st(1)`, `st(2)` and `st(6)` at depth 7, `fst st(1)` and `st(2)`, `fstp st(0)`, `st(1)` and `st(2)`, `fxch st(0)`, `st(1)`, `st(2)` and `st(7)` at depth 8, and `fchs` and `fabs` stored to m80, m64 and m32. They also cover an `fst` to m64 and one to m32 before the final `fstp`, and movement combined with arithmetic: `a + (-b)`, `|a| * |b|` and a subtraction after `fxch`. The cases run every pair of the 34 special encodings through the two-value exports and 300 triples of them through the three-value exports, plus random values and 300 blocks of eight m64 values. Seven mutated translations must each mismatch: `fchs` as `0 - x`, `fabs` that leaves -0 and negative NaNs unchanged, `fxch` that does nothing, `fld st(i)` that always copies `st(0)`, `fstp st(i)` that only pops, `fst` to m32 or m64 that also writes the rounded value back to `st(0)`, and register copies that quiet a signaling NaN. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 969, 225, 3116, 2212, 1922, 1357 and 267 mismatches. Translation rejects an `st(i)` operand beyond the stack depth, a push at depth 8, `fchs` and `fabs` on an empty stack, `fnstenv`, `fsin`, `ffree` and `fdecstp`. It also rejects the undocumented alias encodings of `fstp st(i)` (`DF D0+i`, `DF D8+i`) and `fxch st(i)` (`DD C8+i`, `DF C8+i`), which assemblers do not emit and the fixture does not measure. Only `st(0)` to `st(2)`, `fld st(6)` and `fxch st(7)` are exercised as register operands. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_moves$' -V`.

`cpu_aot_x87_constants` checks the constant loads in the same way, with 30 exports and 2863 cases. Each constant is rounded to 64 significand bits by the rounding control, which the profile fixes at round to nearest-even, and the 53-bit precision control does not apply to it. The translation emits the encodings from a table in `tools/x87_lower.py`. The model does not read that table: it computes pi (Machin's formula), ln 2 and ln 10 from integer series with 320 fraction bits, derives log2(10), log2(e) and log10(2) from them, and rounds each value itself. The exports store each of the seven constants to m80, m64 and m32, store `fldpi` through `fst` m64 and then `fstp` m80, and use the constants as operands: `x + 1`, `1 / x`, `0 - x`, `x * ln 2`, `pi` times an m64 value and `log2(e)` times an m32 value. Two exports push `fldpi` or `fldlg2` as the eighth value. Four mutated translations must each mismatch: the constants rounded to 53 bits, the constants rounded toward zero, `fldz` loading -0, and the `fldlg2` and `fldln2` values exchanged. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 170, 40, 25 and 829 mismatches. Translation rejects each constant load at depth 8, a constant left on the stack at the return, and `f2xm1`, `fyl2x` and `fnstenv`. The other rounding modes are not measured, because the profile rejects `fldcw`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_constants$' -V`.

`cpu_aot_x87_integers` checks the integer conversions in the same way, with 18 exports and 11126 cases. `fild` converts a 16-, 32- or 64-bit two's-complement integer exactly, zero gives +0, and the 53-bit precision control does not apply to it. `fist` and `fistp` round `st(0)` to nearest-even and `fisttp` rounds toward zero. A NaN, an infinity, an unsupported encoding or a rounded value outside the destination range stores the integer indefinite (only the sign bit set), the masked invalid-operation response. `fist` keeps `st(0)`. The model converts with `Fraction` arithmetic. The exports load each integer width to m80, a 64-bit integer to m64 and m32, and a 32-bit integer added to an m80 value. They store every width through `fistp` and `fisttp`, and m16 and m32 through `fist`. They also cover round trips through `fild`/`fistp` m64 and `fild`/`fisttp` m16, two stores from a two-value stack, and `fild` as the eighth value. The integer inputs include each width's extremes, small values, values just past the binary64 and binary32 significand widths, and random values. The m80 inputs include the special encodings, integers, halfway values, quarter steps and values a small fraction past a half around zero and each width's limits, and 400 random magnitudes from 2^-3 up to just below 2^67. Six mutated translations must each mismatch: `fist` and `fistp` truncating, `fisttp` rounding to nearest, saturation instead of the integer indefinite, a NaN converting to 0, `fild` m16 and m32 zero-extending, and `fild` keeping only 53 significand bits. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 1510, 676, 1049, 60, 621 and 170 mismatches. Translation rejects `fild` at depth 8, the stores on an empty stack, `fist` that leaves a value at the return, `fldenv`, `fyl2xp1`, `fbld` and `fnsave`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_integers$' -V`.

`cpu_aot_x87_compare` checks `fcomi`, `fcomip`, `fucomi` and `fucomip` in the same way, with 12 exports and 12230 cases. A comparison sets the integer lowering's ZF and CF and clears OF: equal gives ZF 1 and CF 0, less gives 0 and 1, greater 0 and 0, and unordered (a NaN or an unsupported encoding) 1 and 1. With invalid operations masked, `fcomi` and `fucomi` give the same flags. All 64 significand bits count, +0 equals -0, and a pseudo-denormal equals the normal value with the same significand at biased exponent 1. In a function with `jp`, `jnp`, `fcmovu` or `fcmovnu` they also set PF, which is 1 only for unordered (`cpu_aot_x87_parity`). The integer lowering rejects the other readers of PF and the readers of SF and of the whole flags register. `seto` and `adox` read the cleared OF. After each comparison a record reads the flags through the integer lowering's flag readers, with only flag-neutral `mov`s in between: `je`, `jne`, `ja`, `jae`, `jb` and `jbe` as taken bytes, `cmove`, `cmovne` and `cmovbe` as dwords, `sete`, and last `rcl` of a zeroed register, which rotates CF in and changes it. CF is therefore observed on its own. `loope` and `loopne`, which also read ZF, are not used. The model compares with `Fraction` arithmetic. The exports cover all four mnemonics with `st(1)`, `fcomi st, st(2)`, `fucomip st, st(2)`, a value against itself through `fucomi st, st(0)` and `fcomip st, st(0)`, m64 and m32 operands, two comparisons in a row, and `seto` and `adox` after an `add` of two input dwords and `fcomi` (OF was set before the comparison in 211 of 317 cases), and they store the values left on the stack. The cases cover every pair of the 34 special encodings and values against themselves, their neighbours in the lowest significand bit and bit 11, their negation and the next exponent. They also cover pseudo-denormal and normal twins, denormals one bit apart, random values, and the m64 and m32 special encodings. Seven mutated translations must each mismatch: unordered reported as equal, the operands exchanged, -0 below +0, the low 11 significand bits ignored, the raw biased exponent for pseudo-denormals, unnormals compared by their bits, and OF left as it was. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 3023, 7260, 13, 745, 33, 812 and 211 mismatches. Translation rejects `jp` and `jnp` after `fucomi` and `inc eax`, which writes PF last, and `setp`, `cmovp`, `js`, `jl`, `lahf`, `cmovs` and `pushfq` after `fucomi`, and comparisons without enough stack values. It also rejects the forms that stay outside the profile (`fcom` in the DC D1 alias encoding, `fucompp` with one value, `fprem1`, `f2xm1`, `fptan` and `frstor`), and `fnstsw` with an m16 operand. On a CPU without ADX the native `add_fcomi_of` export, whose `adox` would fault, is not run; the test runs every other check and then exits 77. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_compare$' -V`.

`cpu_aot_x87_sqrt` checks `fsqrt` in the same way, with 9 exports and 13083 cases. The helper rounds the exact square root to 53 significand bits within the extended exponent range. It takes the 64-bit integer root of the significand scaled by 2^63 or 2^64, whichever makes the exponent even, and folds a nonzero remainder into the sticky bit. sqrt(-0) is -0 and sqrt(+inf) is +inf, a NaN is quieted, and any other negative operand or an unsupported encoding gives the indefinite. The model rounds an integer square root with about 160 bits, scaled to the operand's magnitude, and replaces an inexact root by the midpoint of its unit interval, which rounds the same way. The exports take the root of an m80 value and store it to m80, m64 and m32. They also take two roots in a row, the roots of m64 and m32 loads, `sqrt(b) + a`, `sqrt(a * a + b * b)` and a root at depth 8. The cases cover the special encodings, perfect squares at both exponent parities, and 600 values next to the squares of 53-bit midpoints, whose roots fall within a few units of the last bit of a rounding tie. They also cover signaling NaNs, random values over the whole exponent range, denormals, and the m64 and m32 special encodings. Six mutated translations must each mismatch: rounding to 64 bits, rounding in the binary64 exponent range, an ignored remainder, +0 for sqrt(-0), the root of a negative operand's magnitude, and an unquieted signaling NaN. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 4863, 9027, 168, 7, 741 and 104 mismatches. Translation rejects `fsqrt` on an empty stack and `fsin`, `fsincos`, `fnsave` and `frstor`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_sqrt$' -V`.

`cpu_aot_x87_rndint` checks `frndint` in the same way, with 8 exports and 7760 cases. `frndint` rounds `st(0)` to an integral value to nearest-even and keeps it in the extended format. The 53-bit precision control does not apply: integral results between 2^53 and 2^63 keep all their bits, which the native runs confirm. Zeros, infinities and values of 2^63 and above are unchanged, a value that rounds to zero keeps its sign, a NaN is quieted and an unsupported encoding gives the indefinite. The model rounds with `Fraction` arithmetic. The exports round an m80 value and store it to m80, m64 and m32, round m64 and m32 loads, compute the fractional part `x - rndint(x)`, store a rounded value through `fistp` m64, and round at depth 8. The cases reuse the m80 inputs of `cpu_aot_x87_integers` (integers, halves, quarter steps and values just past a half around zero and each integer width's limits). They add values with a fraction between 2^53 and 2^63, signaling NaNs, and m64 and m32 values from 2^-2 up to just below 2^53 and 2^24. Five mutated translations must each mismatch: truncation, ties away from zero, +0 for a value that rounds to zero, the result cut to 53 significand bits, and an unquieted signaling NaN. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 2236, 208, 321, 1207 and 42 mismatches. Translation rejects `frndint` on an empty stack, `fnstenv`, `fldenv`, `fbld` and `fxsave`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_rndint$' -V`.

`cpu_aot_x87_xtract` checks `fxtract` in the same way, with 8 exports and 5895 cases. `fxtract` replaces `st(0)` by its unbiased exponent and pushes its significand, which keeps the sign and gets biased exponent 16383, so it lies in [1, 2). Both results are exact and the precision control does not apply. A denormal or pseudo-denormal is normalized first, so its exponent is that of its value. A zero keeps its sign and gives an exponent of -inf, the masked divide-by-zero response. An infinity keeps its sign and gives an exponent of +inf. A NaN is quieted into both results, and an unsupported encoding gives the indefinite in both. The model splits each value with `Fraction` arithmetic. The exports split an m80 value and store both results to m80, m64 and m32, split m64 and m32 loads, split the significand again (its exponent is +0), add the two results, and split at depth 8. The cases cover the m80 singles of `cpu_aot_x87_add`, 400 random values over the whole exponent range, two values of exponent 0 for each leading-bit position (denormals, and pseudo-denormals at bit 63), values at small and extreme exponents, signaling NaNs, 30 each of pseudo-denormals, unnormals and pseudo-infinities or pseudo-NaNs, and the m64 and m32 special encodings and random bit patterns. Six mutated translations must each mismatch: +inf as the exponent of a zero, a denormal split without normalizing, the exponent left biased, the significand without its sign, an unquieted signaling NaN, and the two results pushed in the other order. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 16, 660, 5296, 2692, 84 and 4448 mismatches. Translation rejects `fxtract` on an empty stack and at depth 8, `fxtract` with only one result stored, `frstor` and `fxrstor`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_xtract$' -V`.

`cpu_aot_x87_scale` checks `fscale` in the same way, with 8 exports and 43003 cases. `fscale` multiplies `st(0)` by 2^n, where n is `st(1)` truncated toward zero, and keeps `st(1)` on the stack. The precision control does not apply: a result in the normal range keeps all 64 significand bits, and a result below it is denormalized and rounded to nearest-even at the same bit positions. A result above the range overflows to infinity. `st(1)` = -inf gives a zero and +inf an infinity, both with the sign of `st(0)`. An infinite `st(0)` scaled by -inf and a zero scaled by +inf give the indefinite. A zero or infinite `st(0)` is otherwise unchanged, and NaN and unsupported operands follow the rules of `fadd`. The helper clamps the truncated scale to 2^20 in magnitude before adding it to the exponent. The model scales with `Fraction` arithmetic. The exports scale an m80 value by an m80 value and store the result to m80, m64 and m32, scale m64 values, scale by a 32-bit integer from `fild`, scale twice in a row, rebuild a value through `fxtract` and `fscale`, and scale at depth 8. Every export also stores `st(1)`. The cases cover every pair of the 34 special encodings, and values with tie and sticky patterns in their low bits, denormals, pseudo-denormals and random values scaled by integers from -70 to 70, by the exponents at the edges of the normal and denormal ranges, by huge values, by non-integers of both signs and by denormals. They also scale values with tie patterns into each part of the denormal range, and cover m64 pairs and 32-bit integer scales. Six mutated translations must each mismatch: rounding to 53 significand bits, rounding toward zero, a negative non-integer scale rounded toward -inf, +0 for every scale by -inf, a zero scaled by +inf kept as zero, and the operands exchanged. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 11395, 1063, 3068, 26, 10 and 37337 mismatches. Translation rejects `fscale` on an empty stack and with one value, `fscale` that leaves `st(1)` at the return, `fnsave` and `fxrstor`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_scale$' -V`.

`cpu_aot_x87_fcmov` checks `fcmovb`, `fcmove`, `fcmovbe`, `fcmovnb`, `fcmovne` and `fcmovnbe` in the same way, with 16 exports and 13290 cases. Each copies `st(i)` into `st(0)` when its condition on the integer lowering's CF and ZF holds: CF, ZF, CF or ZF, and their negations. The copy moves the whole 80-bit encoding, so a signaling NaN stays signaling, which the native runs confirm. The flags come from an integer `cmp` of two input dwords or from `fcomi`. The model evaluates each condition and copies raw encodings. The exports run each form after `cmp` with `st(1)`, `fcmovb` with `st(2)`, a maximum (`fcomi; fcmovb`) and a minimum (`fcomi; fcmovnbe`) of two values, two moves under the same flags, `fcmove st, st(0)`, and `fcmovnb` with `st(7)` at depth 8. Four more read the CF of `cmp` before the move: `adc` and `sbb` set ZF from it, `cmc` inverts it, and `rcl` carries it through EAX to the ZF of `test`. Every export stores every value. The two-value `cmp` exports run every seventh pair of the 34 special encodings with dword pairs that compare below, equal and above, and random values with random dword pairs. The three-value exports run random triples of the special encodings with the same three dword pairs. The `fcomi` exports run every pair of the special encodings and random values. The depth-8 export runs random m64 values and compares the halves of the seventh. In 5807 of the cases a move changes the output. Eight mutated translations must each mismatch: `fcmovb` on a clear CF, `fcmovbe` that reads only CF, `fcmovnbe` that moves when either flag is clear, `fcmovne` that never moves, moves that ignore their condition, a copy that quiets a signaling NaN, a dropped `cmc`, and `adc` and `sbb` that ignore the incoming CF. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 3448, 185, 960, 1055, 5851, 276, 696 and 510 mismatches. Translation rejects `fcmovu` and `fcmovnu` after `cmp`, whose PF the integer lowering does not model (`cpu_aot_x87_parity` covers these two moves), and the forms with an `st(i)` beyond the stack depth. In a function with a move, or with `fnstsw`, `sahf`, `jp` or `jnp` (`cpu_aot_x87_status`), it also rejects any instruction that reads a status flag that no earlier instruction writes on every path from the entry, because the lowered function starts with both flags false and has no input for the caller's flags. Every reader counts, not only the moves, because `adc`, `sbb`, `rcl`, `rcr` and `cmc` pass an undefined CF on into the flags and registers they write, and a later writer of ZF can carry it to a move. The same holds for the other status flags. The integer lowering reads only PF of them, through `jp`, `jnp`, `fcmovu` and `fcmovnu`. `DF` is not tracked, because the Windows x64 calling convention clears it. A forward pass over the function computes which of the six status flags are defined before each instruction:

- Writes come from iced's flag information. Flags left undefined, as ZF after `imul` or after `stc`, do not count.
- A join keeps only the flags defined on all of its incoming paths.
- A call leaves every status flag undefined.
- A CL-count shift and a `rep` compare may change nothing, so they define no new flag.
- An 8- or 16-bit `shl`, `shr` or `sar` by the operand width or more leaves CF undefined.

Seven native-only exports move on the caller's flags. `fcmovb` and `fcmove` move right after two loads. `mov eax, 0FFFFFFFFh; adc eax, 0; fcmove` and `mov eax, 0; sbb eax, 0; fcmove` move on the ZF that the caller's CF produces. `adc eax, 0` or `rcl eax, 1` from EAX = 0, then `test eax, eax; fcmovne`, carry the caller's CF through EAX. `cmc; fcmovb` inverts it. They run through `x87_call_flags` in `tests/cpu/x87_harness.asm`, which loads the caller's CF and ZF with `sahf` just before the call. For 40 value pairs and all four flag pairs, each export's move followed the caller's flag in all 1120 runs. Translation rejects each export at its first reader of the undefined flag: the move, or the `adc`, `sbb`, `rcl` or `cmc`. Nine more rejection cases cover the move before any writer, a join with one undefined predecessor, `stc`, a call, `imul`, `shl eax, cl`, `shl ax, 16`, and a `cmc` or a `seto` before the `cmp` that defines the move's flags. The integer lowering would also reject `seto`, but the flag rule comes first. Eight sequences whose flags are written on every path must still translate: after `clc`, after a join where both paths write ZF, after `xor eax, eax`, after `fcomi`, `clc` before `adc`, `stc` before `sbb`, `cmp` before `cmc`, and `clc` before `rcl` and `test`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_fcmov$' -V`.

`cpu_aot_x87_iarith` checks `fiadd`, `fisub`, `fisubr`, `fimul`, `fidiv` and `fidivr` with an m16 or m32 integer operand in the same way, with 15 exports and 28512 cases. The integer is converted exactly, as `fild` converts it, and the operation then follows its floating-point form with `st(0)` as the destination: the result rounds to 53 bits under the precision control, a zero integer is +0, and the NaN, infinity and divide-by-zero rules are those of `fadd`, `fsub`, `fmul` and `fdiv`. `fisubr` and `fidivr` subtract `st(0)` from the integer and divide the integer by `st(0)`. The model combines the conversion of `cpu_aot_x87_integers` with the `Fraction` models of the add, subtract, multiply and divide tests. The exports run each operation at each width on an m80 value, `fimul` on an m64 value stored back to m64, `fiadd` with an m32 integer followed by `fisub` with an m16 integer, and `fidivr` at depth 8. The cases pair every special encoding with zero, small values of both signs, powers of two and the extremes of each width, and add values whose low significand bits sit at or beside a 53-bit rounding tie, full 64-bit significands, values at the edges of the exponent range, denormals and random values. The two bytes after each m16 integer are never its sign extension. Six mutated translations must each mismatch: `fisub` computed as `fisubr`, `fidiv` computed as `fidivr`, an m16 or an m32 integer zero-extended, an m16 integer read as 32 bits, and 64-bit rounding. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and the controls gave 4565, 3980, 4593, 5609, 9857 and 12275 mismatches. Translation rejects `fbld` and `fsincos`, and the forms without a value on the stack. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_iarith$' -V`.

`cpu_aot_x87_fnstcw` checks `fnstcw`, `fstcw` and `wait` in the same way, with 6 exports and 1776 cases. The profile fixes the control word at `0x027F`, so `fnstcw m16` stores that word, and `wait` does nothing because every exception is masked and none can be pending. iced decodes `fstcw` as `wait` followed by `fnstcw`, and the check requires both. The exports store the word alone, between loads and a division, at the first and last two bytes of the output block next to the guards, and beside the values of a full stack. One export runs `wait` between loads, a division and a store. Every export stores every value. Four mutated translations must each mismatch: a store of `0x037F`, a four-byte store, no store, and a one-byte store. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs, and each control gave 1068 mismatches, every case of the five exports that store the word. The fixed profile still rejects `fldcw`, also after `fstcw`, `fnstsw` with m16, `fninit` and `fdecstp`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_fnstcw$' -V`.

`cpu_aot_x87_examine` checks `ftst`, `fxam`, `ficom` and `ficomp` with an m16 or m32 integer, `fnop`, `fnclex` and `fclex` in the same way, with 4 exports and 782 cases. `ftst` compares ST(0) with +0.0 and `ficom` with the integer, converted exactly as for `fild`; both set C0, C2 and C3 like `fcom`, and `ficomp` pops. `fxam` writes C3, C2 and C0 from the 80-bit encoding: unsupported, NaN, normal, infinity, zero, empty and denormal, where a pseudo-denormal is a denormal, as the native run confirms. An empty ST(0) is known at translation time. C1, which `fxam` sets to the sign, stays unknown, as for every instruction in the profile except `fprem` and `fprem1` in the checked entry. `fnop` and `fnclex` change nothing the profile models, and `fclex` decodes as `wait` and `fnclex`; like every other x87 instruction they leave C0, C2 and C3 undefined. The inputs cover every class, both signs, the integer bounds and values on either side of an integer. Three mutated controls, which report a denormal as normal, convert the integer as unsigned, and compare `ftst` with 1.0, gave 5, 136 and 7 mismatches. Translation rejects `ftst` and `ficomp` on an empty stack, and a reader of C0 after `fnop` or `fnclex`. The rejection cases that used these instructions in the earlier x87 tests now use instructions that stay outside the profile. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_examine$' -V`.

`lower(..., x87=x87_lower.Checked(reject))` is the checked entry of the profile. It also accepts `fldcw m16`, and keeps the control word in a local of the lowered function, `fninit` and `finit`, which load `0x037F`, clear C0 to C3 and empty every register, `fbld` (`cpu_aot_x87_bcd`), and `fprem` and `fprem1` (`cpu_aot_x87_fprem`). The arithmetic, `fsqrt`, `fscale`, `frndint`, `fist` and `fistp`, the m32 and m64 stores, `fnstcw` and the five inexact constant loads all follow that word. The precision control applies only to the arithmetic and `fsqrt`. `fldcw` accepts a word with all six exceptions masked and precision control 00, 10 or 11 (24, 53 or 64 bits). It keeps the word as `fnstcw` reads it back: bit 6 reads as 1, bits 7 and 13 to 15 as 0, and bit 12 is kept. The lowered function leaves through the caller's `reject` statement with a status in these cases:

- status 1, at an `fldcw` whose word unmasks an exception;
- status 2, at an `fldcw` whose word has the reserved precision control 01;
- status 3, at a return whose control word is not `0x027F`;
- status 4, at an `fprem` or `fprem1` that reduces only partially (`cpu_aot_x87_fprem`);
- status 5, at an `fprem` or `fprem1` with a NaN result in a function that contains `fnstsw`;
- status 6, at an `fbld` whose operand has a digit above 9 (`cpu_aot_x87_bcd`).

Guest stores before that point stay done. The fixed profile (`x87=True`) is unchanged and its generated code is byte-identical. A function that loads the control word (with `fldcw`, `fninit` or `finit`) must not contain a call, an indirect branch or an external edge. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H, under every precision control, the constants round down for toward negative infinity and toward zero, and up for toward positive infinity. `fscale` truncates its scale toward zero under every rounding control.

`cpu_aot_x87_fldcw` checks the checked entry with 17 exports and 17792 cases. The translation is a checked entry `uint32_t name(word input, word output, word* result)`, which returns the status and writes `*result` only for status 0. The exports cover:

- each rounding site under all twelve supported words;
- the MSVC truncation idiom (`fnstcw`; `movzx`; `or eax, 0C00h`; `fldcw`; `fistp`; `fldcw` of the saved word);
- `fnstcw` and `fstcw` after words with each reserved-bit pattern;
- two returns that restore from a saved home-space slot and from an input word;
- guest stores before, between and after two loads;
- `fninit` under a loaded word after `fcom` has set C0, C2 and C3, then `fnstsw ax`, `fnstcw` and a quotient under `0x037F`, and `finit` at depth 8 followed by a return without a restore.

The model rounds with exact rational arithmetic under the word's precision and rounding control, and uses the fixed-word models of the earlier x87 tests for special operands. At `0x027F` it agrees with those models on 13231 operations. Native runs never load a word with an exception unmasked, which would trap. The 606 such cases ran only translated. The 404 cases with precision control 01 ran natively as an observation only. All of them matched a 64-bit model on this part, but the SDM leaves the value reserved. The translation matched the model in every case: status, output block, guards and `*result`. The statuses were 16374 accepted, 606 status 1, 404 status 2 and 408 status 3. On the AMD Ryzen 7 7700 the 16782 native cases matched the model, including the control word each export left. After `fninit` the native and translated C0 to C3 are 0 and `fnstcw` stores `0x037F`. The Intel Core Ultra 7 358H run (Windows 11 build 26300) matched the 14994 native cases of the 15 exports before `fninit` and `finit` were added. Twelve mutated translations must each mismatch: rounding control ignored (5277), precision control ignored (6634), `fist` ignoring the rounding control (562), constants ignoring it (75), the raw word kept (524), unmasked words accepted (606), precision control 01 accepted (404), no return check (408), the check on only one of the two returns (123), `fninit` keeping the control word (1680), `fninit` keeping C0 to C3 (850), and a rejection that records its status but runs on (1010). Translation rejects `fldcw` and `fninit` in the fixed profile. In the checked entry it rejects `fldcw` in a function with a call, an indirect jump or an external edge, or with a value left at the return, and it rejects `fnstsw` with an m16 operand, `fninit` in a function with a call, and a store after `fninit` empties the stack. It still translates a function with a call and no `fldcw`, and `fninit` with a value on the stack followed by a restore. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_fldcw$' -V`.

`cpu_aot_x87_status` checks the comparisons into the status word and `fnstsw ax` with 22 exports and 17843 cases. `fcom` and `fcomp` with `st(i)` or an m32 or m64 operand, `fcompp`, `fucom` and `fucomp` with `st(i)`, and `fucompp` set C3, C2 and C0 to 0 0 1 for less, 1 0 0 for equal, 0 0 0 for greater and 1 1 1 for unordered, with the order of `fcomi`. The P forms pop once, and `fcompp` and `fucompp` pop twice. `fnstsw ax` writes those bits and TOP to AX. TOP is the depth subtracted from 8, modulo 8, because a function enters with TOP 0. A forward pass over the function decides what a reader of AX may see, following the SDM:
- C0, C2 and C3 are known after a comparison on every path from the entry, and unknown at the entry.
- `fcomi`, `fcomip`, `fucomi` and `fucomip` leave them as they were.
- Every other x87 instruction leaves them undefined, including `wait`, `fnstcw` and `fnstsw` itself, and so does a call. `fstsw ax` assembles as `wait` and `fnstsw ax`, so after a comparison it exposes no known bit. This follows the SDM, although iced lists `wait` as changing no condition bit.
- C1, the exception flags, the stack fault and the busy bit are never known.

`sahf` copies C0 to CF, C2 to PF, C3 to ZF and the busy bit to SF. Each flag is defined only when its bit is known, and AF is left undefined. `test ah, imm8` and `and ah, imm8` define ZF, PF and SF only when the immediate selects known bits, and `and` makes the bits it clears known. Any other reader of AL, AH, AX, EAX or RAX that sees an unknown bit is rejected. So are a return, a call, an indirect branch and an external edge while AX holds one. The flag rule of `cpu_aot_x87_fcmov` then applies to every function with `fnstsw`, `sahf`, `jp` or `jnp`. The integer lowering sets PF only after `sahf`, the `fcomi` family (`cpu_aot_x87_parity`) and an 8-bit `test` or `and`, and `jp` and `jnp` are rejected when any other instruction may have written PF last. The lowering adds the status variable and PF only to functions that need them. Every other generated source of the build is byte-identical to the one built from the parent commit.

After each `fnstsw ax` a record reads AH through every accepted consumer, with only flag-neutral `mov`s in between:
- `sahf`, then `je`, `jne`, `jb`, `jae`, `ja`, `jbe`, `jp` and `jnp`;
- `test ah` with 01h, 04h and 40h (C0, C2 and C3) and with each TOP bit, then `jne`;
- `test ah` with 45h, 44h, 05h and 41h, then `jp`;
- `and ah, 45h`, stored, then `jp`, then `cmp ah` with 40h, 01h and 00h and `je`.

The record then clears EAX. The model compares with the `Fraction` model of `cpu_aot_x87_compare`. The exports run each register form with `st(1)`, `fcompp` and `fucompp`, `fcom st(2)`, `fucomp st(2)`, a value against itself, the four memory forms, and `fucom st(7)` at depth 8, where TOP is 0 again. They also cover a comparison followed by `fcomi` or `fucomip`, two comparisons each read through its own `fnstsw`, a join of `fcom st(1)` and `fucom st(2)` chosen by an input dword, and a branch on C3 whose successors test C0 and C2. Two exports run on all 256 byte values: the record on AH loaded by `mov`, and `jp` after `test al, al`, `test byte ptr [m], 0A5h` and `and al, 3Ch`. Every export stores the values left on the stack. The cases cover every pair of the 34 special encodings, values against themselves, their neighbour in the lowest significand bit, their negation and the next exponent, pseudo-denormal and normal twins, and random values, together with the m64 and m32 special encodings. At `fnstsw` they gave 4856 less, 2265 equal, 4914 greater and 5296 unordered, at TOP 0, 5, 6 and 7. Seven mutated translations must each mismatch: an unordered result without C2 (5340), C0 and C3 exchanged (7263), TOP stored as 0 (10895), PF loaded from bit 4 by `sahf` (8707), odd parity for `test` and `and` (16084), odd parity for the 22 `and` statements alone (16084, every case that reads PF), and `fcomi` clearing C0, C2 and C3 (779). On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs.

Two native-only exports read bits that the profile does not know. One stores the whole status word after `fcom`. Over 1759 runs its known bits matched the model, and the other bits took 0x0000, 0x0001 (IE) and 0x0002 (DE). The other is the branch export with `test al, 1` (IE) on the C3 successor, after `fucom`. An unordered `fucom` left IE clear for a quiet NaN and set for a signaling NaN, so the byte does not follow from C0, C2 and C3. Translation rejects both, at the store of AX and at `test al, 1`.

Translation rejects 35 more sequences:
- condition bits that are unknown at the reader: `fnstsw ax` without a comparison, or after `fadd`, `fld` and `fstp`, `fnstcw`, `wait` (`fstsw ax`), a second `fnstsw ax` or a call, and after a join where one path skips the comparison;
- the review's control, a branch whose other successor tests C1;
- direct reads of unknown bits: `test al, 1`, `test ax, 4500h`, `and ah, 47h` with a store of AH, a store of AX, a return (also in the checked entry after `fldcw`), an external edge or a direct call with the status word in AX, a store of AH after a join where one path skips `and ah, 45h`, and `test eax, eax` after `mov al, 0`;
- PF: `jp` after `fucomi` and `inc eax`, after `cmp` or after `test eax, eax`, or before any flag writer, `sahf` in a function without x87 instructions, and `lahf`;
- forms outside the profile: `fnstsw` with an m16 operand, the `fcom` and `fcomp` alias encodings DC D1, DC D9 and DE D1, `fsincos`, `fpatan` and `fldenv`, and comparisons without enough stack values.

Eight twins must still translate:
- `sahf; jb` after `fcom`;
- `fcomi` between `fcom` and `fnstsw`;
- the branch whose successors test C0 and C2;
- `test al, al; jp`;
- `and ah, 45h` with a store of AH;
- a return after `xor eax, eax`;
- `sahf` on AH from `mov`;
- `fnstsw ax` after `fcom` in the checked entry after `fldcw`.

Memory-form status stores, sticky exception flags, `fninit`, `fclex`, `ftst`, `fxam` and `ficom` are not part of this test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_status$' -V`.

`cpu_aot_x87_parity` checks the PF of `fcomi`, `fcomip`, `fucomi` and `fucomip`, and `fcmovu` and `fcmovnu`, with 16 exports and 17672 cases. In a function with `jp`, `jnp`, `fcmovu` or `fcmovnu`, each of the four comparisons sets the integer lowering's PF along with ZF and CF: 1 for unordered, 0 otherwise. `fcmovu` copies `st(i)` into `st(0)` when PF is set and `fcmovnu` when it is clear, moving the whole 80-bit encoding as the other moves do. A function without such a reader keeps its generated code. Every other generated source of the build is byte-identical to the one built from the parent commit, except the eight of `cpu_aot_x87_status`. They differ in two `fcomi` statements each, in functions whose `jp` reads the PF of a later `sahf`. A record stores whether `jp`, `jnp`, `je` and `jb` are taken, through flag-neutral `mov`s. The exports cover:
- each comparison with `st(1)`, read through the record;
- `fcmovu` and `fcmovnu` after `fucomi`, `fcomi`, `fucomip st, st(2)` and `fcomip`, and `fcmovu` with `st(7)` at depth 8;
- the equality test that compilers emit, `fucomip; fstp st(0); jp; jne`, and a classification by `jnp` and `je`;
- the last writer of PF: `fucomi` after `sahf`, `sahf` after `fucomi`, and `test al, al` after `fucomi`;
- `fcmovu` after `sahf` on all 256 AH values loaded by `mov`;
- a join, chosen by an input dword, of a `fucomi` path and an `fcom; fnstsw ax; sahf` path.

The model compares with the `Fraction` model of `cpu_aot_x87_compare` and copies raw encodings. The cases cover every pair of the 34 special encodings, values against themselves, their neighbour in the lowest significand bit and their negation, random values, triples and random m64 values. In the four records `jp` was taken in 2124 of 6624 cases, and the moves read PF set in 2361 and clear in 4675 cases. Five mutated translations must each mismatch, each in a named export:

| Control | Mutation | Mismatches | Export |
|---|---|---|---|
| `pfnever` | the comparisons clear PF | 4959/17672 | 531 in `equal_branch` |
| `pfequal` | PF is set for equal instead of unordered | 6244/17672 | 660 in `classify` |
| `pfkeep` | the comparisons leave PF as it was | 4904/17672 | 127 in `sahf_fucomi` |
| `uinvert` | `fcmovu` moves on a clear PF | 4050/17672 | 1522 in `fucomi_fcmovu` |
| `nuinvert` | `fcmovnu` moves on a set PF | 2122/17672 | 1522 in `fcomi_fcmovnu` |

`pfkeep` mismatches in `sahf_fucomi` only where the `sahf` before `fucomi` left a different PF. On the AMD Ryzen 7 7700 and the Intel Core Ultra 7 358H (Windows 11 build 26300) every case matched in the native and translated DLLs. Translation rejects 10 sequences: `fcmovu` before any flag writer, `fcmovnu` after `test eax, eax`, `fcmovu` after `fucomi` and `inc eax`, `jp` after `fucomi` and `add eax, 1`, `fcmovu` after a call, a join of a `fucomi` path and a `cmp` path, `sahf` on `fnstsw ax` without a comparison before `fcmovu`, `fcmovu st, st(2)` with two values, `fcmovnu st, st(1)` with one value, and `setp` after `fucomi`, which the integer lowering does not implement. It translates 5 twins: `fcmovu` after `fucomi`, `fcmovnu` after `fucomi` and a `mov`, `jp` after `fucomip` and `fstp st(0)`, a join of a `fucomi` path and a `sahf` path, and `fcmovu` after `fucomi` and `lea`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_parity$' -V`.

`cpu_aot_x87_fprem` checks `fprem` and `fprem1` in the checked entry, with 11 exports and 36952 cases. The fixed profile rejects both. Each replaces `st(0)` by `st(0) - q * st(1)`. q is the quotient of the magnitudes, truncated by `fprem` and rounded to nearest-even by `fprem1`. Each sets C0, C3 and C1 to bits 2, 1 and 0 of q and clears C2. The remainder is exact. A zero remainder keeps the sign of `st(0)`, a zero `st(0)` is kept, and an infinite `st(1)` gives `st(0)` with q = 0. A pseudo-denormal is re-encoded at biased exponent 1. Neither instruction reads the control word. The SDM leaves two outcomes to the implementation, and the checked entry rejects both:

- When the normalized exponent of `st(0)` exceeds that of `st(1)` by 64 or more, the instruction reduces `st(0)` only partially and sets C2 (status 4).
- A NaN result follows the NaN rules of the arithmetic instructions: a NaN or unsupported operand, an infinite `st(0)` or a zero `st(1)`. The status bits after it are not given, so a function with `fnstsw` leaves with status 5. Without `fnstsw` the NaN result is kept.

The exports cover:

- both instructions with `fnstsw ax; and ah, 47h` and a store of AH, and without `fnstsw`;
- `sahf` with `jb`, `je` and `jp`, and `test ah, 02h; jne`, which read C0, C3, C2 and C1;
- the fmod loop `fprem; fnstsw ax; sahf; jp` and the same loop with `fprem1`;
- `fprem` then `fprem1` of its result;
- `fprem` at depth 8;
- `fprem1` under each of the twelve control words with all exceptions masked.

The model divides with `Fraction` arithmetic. For a native run it leaves the partial result unknown, along with every status bit except C2 after a partial or NaN result. The cases cover:

- every pair of the 34 special encodings;
- random pairs with exponent differences from -66 to 70;
- each difference from 60 to 67;
- partial reductions up to the largest difference;
- denormal and pseudo-denormal divisors at normalized differences 62 to 65;
- denormal dividends and results;
- exact multiples, ties and their neighbours;
- `fprem1` with `st(0)` between half of `st(1)` and `st(1)`.

The translated statuses were 27623 ok, 4104 partial and 5225 NaN. Eight mutated translations must each mismatch: no partial rejection, no NaN rejection, the partial boundary at 63, `fprem1` truncating, ties rounding up, `fprem1` keeping `st(0)` above half of `st(1)` one exponent below it, C1 and C3 exchanged, and +0 for every zero remainder. They gave 4104, 5225, 431, 3374, 867, 472, 5498 and 1211 mismatches.

On the AMD Ryzen 7 7700 every translated case matched the model. Every native case matched it where it is defined: C2 was 1 in the 2119 single-step records that the checked entry rejects as partial and 0 after every full reduction, and the result was a NaN in exactly the 4598 single-step cases with a NaN model result. The native loops reduced partially and iterated in 794 cases, and their final value matched the full remainder. That value does not depend on how far each partial step reduces, because a partial step subtracts an even multiple of `st(1)`. The 1254 loop cases with a NaN result ran only translated, because C2 after a NaN result is not given and a native loop might not end.

Two observations on this processor are not modelled. A partial step clears C0, C1 and C3 and leaves an exponent difference of 32 + (D mod 32), where D is the difference before the step. After a NaN result, C0 and C3 keep their previous values and C1 and C2 are cleared. Translation rejects `fprem` and `fprem1` in the fixed profile, `fprem` with one value, `fprem1` on an empty stack, `test ah, 80h` (the busy bit) after `fprem`, `fadd` between `fprem` and `fnstsw`, a push inside the fmod loop, and a return with the status word in AX. It translates `test ah, 02h` after `fprem1`, `sahf; je` after `fprem`, the fmod loop, and `fprem` without `fnstsw`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_x87_fprem$' -V`.

### Ryzen 7 7800X3D measurements

The four x87 fixtures reported in [#592](https://github.com/R0Y00/Xbox2Windows/pull/592) also passed on an AMD Ryzen 7 7800X3D with Windows 11 Pro x64 build `26200.9974` and MSVC `19.44.35228.0`. The `dev` build used commit `af942385c9561a9ee4d5d202d79bae9a084fb87f`.

| CTest | Cases | Exports | Negative controls that detected a mismatch |
|---|---:|---:|---:|
| `cpu_aot_x87_compare` | 12,230 | 12 | 7 |
| `cpu_aot_x87_fcmov` | 13,290 | 16 | 8 |
| `cpu_aot_x87_iarith` | 28,512 | 15 | 6 |
| `cpu_aot_x87_fnstcw` | 1,776 | 6 | 4 |

The native and recompiled DLLs matched the reference for the output blocks and guards in all 55,808 cases. Every checked export returned with control word `0x027F` and an empty x87 stack. CTest reported four passes and no skips.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_aot_x87_(compare|fcmov|iarith|fnstcw)$' -V
```

## Mapping an image without LoadLibrary

`tools/pe_map.cpp` maps an x64 PE32+ image into memory it allocates itself, away from the preferred base, and applies its DIR64 and HIGHLOW base relocations. It binds ordinary and delay imports through a resolver supplied by the host, sets section protections, records the actual base in the mapped headers and registers `.pdata` with `RtlAddFunctionTable`. The original machine code runs unchanged. Header fields are checked against the file and image size before use. It then calls TLS callbacks and the entry point of a DLL; `pe_map_run` starts an EXE. An implicit TLS data template is served through a carrier DLL that Windows loads (see "Implicit TLS data" below). [Issue #46](https://github.com/R0Y00/Xbox2Windows/issues/46)

```powershell
cmake --build --preset dev --target pe_map cpu_reloc_original cpu_import_original_lifted cpu_ordinal_original_lifted cpu_import_altered cpu_ordinal_altered cpu_register_probe cpu_two_imports
ctest --preset dev --no-tests=error -R '^cpu_native_map$' -V
```

The test maps the named and ordinal import fixtures, `reloc_route` and a damaged copy of the register probe. In `reloc_route`, `call_mix` reaches the provider through an absolute code pointer in `.data`, which gives the image exactly one DIR64 relocation; the test checks that the mapped pointer equals its linked value plus the load delta. The two import fixtures have no relocation entries. For every image the test checks the following:

- the base differs from the preferred base and is recorded in the mapped headers;
- the IAT slot holds the export the resolver returned;
- `RtlLookupFunctionEntry` finds `call_mix` in the registered table until the image is released.

The mapped `call_mix` is compared over 249 input pairs on arguments, guarded writes, RBX/RSI sentinels and return value. It matches the reference and the same image loaded by Windows, and for the import fixtures it also matches the lifted DLL. If the import is bound to the altered provider instead of the linked one, only the return changes. An unbound import is rejected with its DLL and symbol or ordinal. Copies patched to have an entry point outside any executable section or an import directory that runs past the image are rejected before any import is requested. Like the Windows loader, `pe_map` finds the import table by its address and walks it to the null descriptor without using the directory `Size`; each descriptor is bounds-checked against the image. `two_imports` imports `mix_args` from the named provider and `probe` from the register probe. With its import directory `Size` left at 60 or patched to 0, 19 or 20 (ending after the first descriptor), `LoadLibraryExW` and `pe_map` both bind both IAT slots; the test only reads the slots and runs nothing in the image. The Windows loader does not read the delay-import directory at all: copies of the named fixture with an empty table, an address of 0, an address past the image or a descriptor naming a DLL all load. `pe_map` maps the first two, rejects a directory outside the image as a format error and one that lists a DLL as unsupported. In a copy of the register probe with the `clobber_control` export address slot zeroed, that name no longer resolves while `probe` still does.

With the preferred base reserved, Windows x64 and pe_map both mapped the source-built, entry-free relocation fixture elsewhere when its relocation directory RVA was zero. The two sizes tested were the linked size (12) and SizeOfImage + 1 (28673). Both loaders left the DIR64 slot at its linked value, 0x180001000. The fixture reads the slot without calling through it. A zero RVA is treated as an absent directory; the separate stripped-relocations control still rejects mapping away from the preferred base.

`cpu_native_seh` checks exception dispatch through a mapped image. In `seh_route`, `guarded_load(address, mode, trace)` names its own exception handler in its unwind data. The handler resumes an access violation raised by the load instruction at a recovery label with a fixed value in RAX, and declines every other exception. The cases run in one child process for the Windows-loaded original and another for the mapped copy, so a broken unwind crashes only the child and is reported as a failed check:

- a valid load returns the value;
- a load from address zero is caught by the image's own handler and returns the fixed value, with RBX and RSI restored;
- an illegal instruction, reached after the first trace write, is declined and unwinds through the mapped frame to the ctypes caller, which reports exception code `0xc000001d`;
- a later valid call still succeeds.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_seh$' -V
```

`cpu_native_section` compares every section of the register probe after `LoadLibraryExW` and after `pe_map`, byte for byte, on the original and on copies with one section header patched. The probe has no entry point and no imports, so the Windows load runs none of its code. On Windows 11 build 26300.9974, the loader reads section data in whole `0x200`-byte file sectors: it rounds `PointerToRawData` down and the end of the raw data, `PointerToRawData + SizeOfRawData`, up to a multiple of `0x200`. It copies that range up to the section's virtual size, and bytes past the end of the file stay zero. `pe_map` now reads the same range. The test covers a start inside a sector, a start in the first sector, which reads the headers from offset 0, a raw size below one sector and raw data that crosses a sector boundary. In each case Windows differs from an exact read of `SizeOfRawData` bytes at `PointerToRawData`. A last case shortens the file so that the rounded end of `.rsrc` lies past it.

The section test compares the original fixture's final page protections against Windows. On Windows 11 build 26200 the contributor observed that Windows mapped a copy with an unreadable `.pdata` section. The mapper applies final protections, validates the complete exception table, and returns `MAP_UNSUPPORTED` if any table page is unreadable. Only a readable table is registered for unwind lookup. The fixture also checks section alignment and rejects unsupported sub-page protection layouts.

A `PointerToRawData` of zero is the exception: Windows leaves that section zero-filled even when `SizeOfRawData` is set, and so does `pe_map`. The rounding cases patch `.rdata`, because in `.rsrc` a rounded start can put other data where the resource directory should be, and `LoadLibraryExW` then fails with `ERROR_BAD_EXE_FORMAT`. These results were measured with a `FileAlignment` of `0x200` and `0x400`. With `0x1000`, Windows filled more of a section whose raw size is below one sector than the rounded sector end allows, which remains open.

Against the end of the file, Windows checks raw data by two rules only. `LoadLibraryExW` fails with `ERROR_BAD_EXE_FORMAT` if any section's `PointerToRawData + SizeOfRawData` reaches 4 GiB, or if the last section in the table has raw data that ends past the file, compared without rounding. A section whose `SizeOfRawData` is zero is not checked. If the last section has no raw data, no section is checked against the file size. The raw data of any other section may run past the end of the file or start after it: Windows maps that section, and the missing bytes read as zero. `pe_map` now applies the same two rules. Before, it refused every section whose raw data ran past the file and accepted both copies that Windows refuses. The test adds three copies that Windows loads: `.rdata` raw data that runs past the file, `.pdata` raw data that starts after it, and `.pdata` with no raw data and an offset after the file. It also adds two copies that both loaders must refuse: `.pdata` with a raw range that reaches 4 GiB, and the file shortened by one byte so that the last section, `.rsrc`, runs past it.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_section$' -V
```


### HIGHLOW and mixed-width base relocations

The AMD64 mapper accepts `IMAGE_REL_BASED_HIGHLOW` as a four-byte addition of the
low 32 bits of the load delta, with unsigned wraparound. `DIR64` remains an
eight-byte addition. Unaligned entries are handled with byte copies. Bounds and
overlap checks use the actual fixup width; IAT writes retain their eight-byte
checks. Unknown types, header writes, writes into the relocation directory and
fixups extending past the image are rejected.

`cpu_native_highlow` modifies the relocation table of the source-built
`reloc_route` DLL and compares seven cases with `LoadLibraryExW` using
`DONT_RESOLVE_DLL_REFERENCES`. Both loaders are forced away from the preferred
base. The check compares relocated values and adjacent bytes, normalizing for
the different bases. Cases include zero/high-bit/all-one inputs, unaligned
entries, mixed DIR64/HIGHLOW entries, the final image dword and a dword ending
exactly before the relocation directory. Five malformed or unsupported controls
must fail. The patched pointer is never called. The original fixtures and test
are MIT source; no game inputs are included.

```powershell
cmake --build out/native --target pe_map cpu_reloc_original cpu_import_provider --parallel 4
ctest --test-dir out/native --no-tests=error -R '^cpu_native_highlow$' -V
```

Measured on Windows 11 build 26300.9974 with MSVC/ML64 19.44.35230 and
Python 3.12.8: the seven comparison cases and five rejection controls passed
with Debug and Release (`/O2`) builds. The selected existing native-map,
mutation and relocation-free tests also passed in both configurations.

[Microsoft PE relocation definitions](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#base-relocation-types)
define the four-byte HIGHLOW operation. This adds a mapper capability; no title
startup or use of this relocation by a particular title is established.

### Security cookie before entry

`cpu_native_load_cookie` uses a CRT-free DLL and EXE to observe the load-config cookie before either entry can initialize it. On Windows 11, the native loader replaces the default x64 cookie with a nonzero, non-default 48-bit value when the load-config sizes reach the 0x70-byte prefix. `pe_map` seeds the same case with the system random generator after relocation and before guest callbacks or entry. The fixture compares the behavior and value bounds across the two loaders. It checks the size boundary from both fields, the unused-cookie flag, and a preinitialized cookie. A read-only target must be rejected by the mapper before entry.

`ctest --preset release --no-tests=error -R '^cpu_native_load_cookie$' -V`

### Load-config callback field convention

`cpu_load_dispatch_present`, `_absent` and `_alternate` share a hand-written 0x138-byte `_load_config_used`. This fixture treats the field at +0x78 (`GuardCFDispatchFunctionPointer`) as its own direct callback pointer, not the real Windows guard-dispatch ABI: the field is in read-only `.rdata` and has a DIR64 relocation. `native_entry` loads it, calls it with `ECX=0x1234` when non-null, and encodes the callback count, argument and return through the existing `finish_exe` wrapper. The test inspects each image's own PE for the directory and structure sizes, the slot RVA and value, the read-only section and the DIR64 relocation, then runs the same image under Windows and `pe_map` with the same `cpu_exe_io.dll` provider.

Absent makes zero calls (exit `0xb211`); present calls once with `0x1234` and return `0x5a` (`0xb111`); alternate uses a different callback return (`0xb151`) so a wrong binding is visible. Native and mapped match for all three. A hand-built `GuardFlags=0x100` image without a check pointer or function table was refused by the local loader and hung the CI runner; a specific refusal code is not portable, so that image is not part of the test or an acceptance condition.

### Linker-produced CFG callback

`cpu_native_load_config_callback` builds a C++ caller with `/guard:cf`. The linker supplies the guard slots, function table and flags; the caller calls a read-only relocated callback table through `__guard_dispatch_icall_fptr`. The `record`, `other` (different callback) and `invalid` (target inside a function) images link without CFG and match across Windows and `pe_map`. With CFG, Windows replaces both guard slots (`0x30055` versus the callback's `0x55`) and ends the invalid target before it writes output; `pe_map` currently keeps the image's own slots, runs the valid callback (`0x55`) and runs the invalid target (`0x77`). The test records the unchecked mapped result as the current observation. It also accepts an explicit unsupported-CFG diagnostic before dispatch. An unrelated load error or missing output cannot establish CFG behavior. The local refusal code is printed but not asserted. The fixture is based on denn0x's `pe/load-config-callback` reference branch.

`ctest --preset dev --no-tests=error -R '^cpu_native_load_(dispatch|config_callback)$' -V`

### TLS callbacks and the entry point

`tls_route` has an entry point and a TLS directory whose callback list holds one routine and whose data template is empty. Both routines report each call to `cpu_tls_sink.dll`, which outlives the image, so calls made during release stay visible. `cpu_native_tls` loads the image with `LoadLibraryW` and maps it with `pe_map`, each in its own child process, and compares the process notifications:

- Attach: with both loaders the TLS callback runs before the entry point, each with `DLL_PROCESS_ATTACH`, the module base and a null reserved argument.
- Detach: on Windows 11 build 26300, `FreeLibrary` called the TLS callback first and then the entry point, both with `DLL_PROCESS_DETACH`. `pe_map_release` follows that measured order. No Microsoft documentation specifies it.
- Failed attach: in `cpu_tls_refuse` the entry point returns FALSE. Windows then sends a full detach in the same order, and `LoadLibraryW` fails with error 1114. In the same case `pe_map` sends the same detach and returns `MAP_INIT_FAILED`.

Like the Windows loader, `pe_map` finds the TLS directory by its address and does not use its `Size`. On Windows 11 build 26200, copies with `Size` 0, 1 or 39 still run the callback before the entry point, under both loaders; with the address set to 0 only the entry point runs; a directory past the end of the image fails `LoadLibrary` with error 998 and `pe_map` with "TLS directory outside image". Between attach and release the test starts and joins one thread whose start routine is `Sleep`. On that thread both loaders call the TLS callback and then the entry point with `DLL_THREAD_ATTACH`, then the same pair with `DLL_THREAD_DETACH`, all with a null reserved argument. `pe_map.dll` is itself loaded by Windows, and its `DllMain` forwards these notifications to every mapped image: attach in mapping order and detach in reverse, which is what two `LoadLibraryW` copies receive on Windows 11 build 26300. The sink records the calling thread, so notifications from unrelated threads in the process are ignored. A thread created between an image's process attach and its registration receives the detach without the attach, as a thread that existed before mapping does. Each mapped image holds one reference for its registration and one per thread notification in flight. `DllMain` takes those references under the registry lock and calls the guest callbacks after releasing it, so a callback may map or release any image, its own included. `pe_map_release` unregisters the image and returns 1 at once. The image's process detach and unmapping run when its last reference goes, inside `pe_map_release` or on the thread that finishes the last notification, and happen exactly once. A notification is admitted when its thread takes the image's reference under the registry lock, and none is admitted once release has unregistered the image. An admitted notification may still run after release returns; it skips an image already marked released before calling it, but one whose callback has started finishes, so an image whose TLS callback releases it still has its entry point called for that thread attach. `cpu_native_lifetime` compares the two-image order with two native loads, and releases an image from inside a thread-attach callback, both another image and the image itself, through a hook armed for that thread only. A second release returns 0. Null, an interior address, a foreign `PAGE_NOACCESS` page and a foreign read-write buffer also return 0. The buffer keeps its content and protection, and the unreadable page shows that release does not read its argument. Without a notification in flight, release finishes the process detach, the removal of the image's `.pdata` table and the unmapping before it returns: `RtlLookupFunctionEntry` finds the entry point's function before the release and nothing after it, and the base is no longer committed. In 50 rounds, eight native threads spin on a shared flag and then release the same image at once; each round has exactly one 1 and one process detach. A stale pointer cannot identify an earlier instance: once the address is reused by a new mapping, releasing it releases that new mapping. When another thread releases an image whose TLS callback is blocked inside a thread attach, release returns 1 at once while the unwind entry is still present and no process detach has run. Once the callback returns, the entry point still receives that attach, the worker performs the detach and the table removal once, and the thread detach is not delivered. Damaged copies are rejected before any notification: one with a nonzero `SizeOfZeroFill` as TLS data, one with the entry point moved into `.data`, and one with its TLS callback pointing into `.data`. The entry point and callbacks must lie in an executable section. The TLS directory and each callback slot are read after section protections are applied, so `pe_map` checks the live page protection with `VirtualQuery` before every such read, on attach and on release. In `tls_route` the callback list has its own read-only `.tlscb` section. Copies with `.data` or `.tlscb` made inaccessible are rejected as "TLS directory not readable" or "TLS callback list not readable". A list made `PAGE_NOACCESS` after mapping is skipped on release while the entry point is still detached. `pe_map_export` applies the same rule to the export directory, the address and name tables and each name string, and accepts an export target on an execute-only page; a copy whose export section is mapped without read access resolves no export instead of returning a pointer into it. A list longer than 256 callbacks is rejected as unsupported. With `IMAGE_FILE_DLL` cleared, with or without an entry point, `LoadLibraryW` loads and frees the copy without calling the TLS callback or the entry point, and `pe_map` does the same. `pe_map` still applies the executable-section checks to such an image, where Windows would load it and call nothing. The callback list collected before attach is reused for the detach that follows a failed attach. The list is re-read on release; if it was overwritten with a non-executable address or made unreadable after mapping, only the entry point is detached. Implicit TLS data would need a slot in each thread's `ThreadLocalStoragePointer` array, which ntdll owns. `pe_map` therefore leaves `AddressOfIndex` as linked. In the test process, Windows wrote index 8 for this image even though its template is empty.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_tls$' -V
```

### Implicit TLS values on the main and worker threads

`cpu_tls_value` is a source-built DLL with one `__declspec(thread)` value (`7`) and an 8 KiB `__declspec(thread)` array of distinct words in the same initialized template, so the template spans at least one 4 KiB page boundary. Two TLS callbacks, in `.CRT$XLB` (caller 1) and `.CRT$XLM` (caller 3), and the entry point (caller 2) each report the observed state in the sink's reserved slot: the value in the low 16 bits and the number of array words that differ from the template above them, so a block copied from the template reports 7. `cpu_native_tls_value` first checks with `pefile` that the array crosses a page boundary inside the template and that both callbacks appear in the callback table in that order. It then loads the image with `LoadLibraryW` and maps it with `pe_map`, each in a fresh child process. The child checks that the template in the loaded image matches the file, observes the main thread's copy and writes 20 to it, then runs two workers that are alive at the same time: worker A observes, writes 50 and waits, worker B starts after A's write, observes, writes 60 and waits, and then both observe again. Workers are joined with a 10 s limit. Sink events are kept in call order with raw thread IDs translated to `main`, `a` and `b`. A worker timeout, a missing callback, an unexpected value or a changed template fails the test. The empty-template `cpu_tls_original` fixture is the control: it must load under both loaders.

Measured on Windows 10 build 19045, MSVC 19.44, `dev` and `release` presets: on every notification (process attach and detach on the main thread, thread attach and detach on both workers) both loaders call caller 1, then caller 3, then the entry point. Every thread starts from the template, each thread's writes stay in its own copy, and the template in the loaded image is unchanged at the end. The two workers exit in either order, so only the per-thread sequences are compared between loaders. Two negative controls patch copies of the built DLL and must be rejected under both loaders: swapping the two callback table entries is reported as a callback order failure (`[3, 1, 2]` on process attach), and zeroing the template from its first page boundary on is reported as a template failure (array words differ on the main thread). See [issue #870](https://github.com/R0Y00/Xbox2Windows/issues/870).

Until `pe_map` gained the TLS carrier described in the next section, this test pinned the opposite result: `pe_map_image` rejected the image with `MAP_UNSUPPORTED` ("TLS data"). An EXE's own TLS template and callbacks under `pe_map_run`, including thread notifications, are covered in [An EXE's own TLS template and callbacks](#an-exes-own-tls-template-and-callbacks). This DLL test addresses the thread-local-value part of #165; it does not cover C-runtime startup in a mapped EXE, `GetModuleHandle(NULL)`, and the PEB remain untested and unsupported. [Issue #165](https://github.com/R0Y00/Xbox2Windows/issues/165)

```powershell
cmake --build --preset dev --target pe_map cpu_tls_value cpu_tls_original cpu_tls_sink
ctest --preset dev --no-tests=error -R '^cpu_native_tls_value$' -V
```

### Implicit TLS data

Compiled x64 code reaches a `thread_local` or `__declspec(thread)` variable through the thread's `ThreadLocalStoragePointer` array (`gs:[0x58]`), indexed by the DWORD at the TLS directory's `AddressOfIndex`. ntdll owns that array: it assigns the indexes, sizes the array, gives each thread one block per index and frees the blocks. `pe_map` does not write to it. For an image whose TLS directory has a data template or a zero-fill size, `pe_map_image` generates a small carrier DLL in a private directory under the temporary directory and loads it with `LoadLibraryExW`. The carrier has no code and no entry point. It holds a TLS directory whose template is a copy of the image's relocated template and whose `SizeOfZeroFill` and `Characteristics` (alignment) are the image's, plus an import table that is only the null descriptor. Windows assigns the carrier an index and allocates, aligns, initializes and frees one block per thread. `pe_map` stores that index at the image's `AddressOfIndex` before any callback or entry point runs, and unloads the carrier after the image's process-detach notifications.

`cpu_tls_data` is a source-built DLL with the static C runtime. Its template holds an initialized value, a zero-filled value, a 64-byte aligned array, a pointer to a global (a base relocation inside the template), a zero-filled range of three pages and a C++ `thread_local` with a dynamic initializer. `cpu_native_tls_data` loads it with `LoadLibraryW` and maps it with `pe_map`, each in a fresh child process. Measured on Windows 10 build 19045, MSVC 19.44:

- The two loaders agree on ten observations. The loading thread, a thread that existed before the load and a thread created after it each start from the template, in a block of their own, with the array on a 64-byte boundary and the pointer aimed at the global of the loaded image. A write on one thread is not seen by another. A second copy loaded at the same time gets a different index and its own values. After release and a new load the loading thread starts from the template again.
- Copies with `SizeOfZeroFill` 0x10, with a 4096-byte alignment and with the alignment cleared load under Windows, and `pe_map` agrees with Windows on each.
- A template that ends past the image fails `LoadLibraryW` with error 998 and one that ends before its start fails with error 193. `pe_map` rejects both with `MAP_FORMAT`. `pe_map` also rejects a template or zero-fill size above 16 MiB with `MAP_UNSUPPORTED`; Windows was not measured on that case.
- A DLL without an import directory loads under Windows but is given no TLS index, with or without an entry point. An import directory with only the null descriptor is enough. The carrier's index slot starts at `0xffffffff`, and `pe_map` fails the map if Windows leaves it unchanged.
- While the image is mapped, renaming the carrier or its directory and opening the carrier for writing fail with error 32. After release nothing is left in the temporary directory.
- A process that ends with an image still mapped leaves its carrier directory, since deletion is only requested on release. A later process that maps a TLS image in the same temporary directory leaves it untouched.
- A map that fails after its carrier was written (the child's temporary directory has an inherited low integrity label and the child sets `NoLowMandatoryLabelImages`, so `LoadLibraryExW` fails with error 5; a labeled copy of the fixture loads without that policy and fails with error 5 under it) and a map that fails after its carrier was loaded (the entry point of `cpu_tls_data_refuse` reads the template values, then returns FALSE; Windows fails the same DLL with error 1114) each remove their own carrier. Beside them sit a file in the earlier carrier layout named after a process that has ended and a carrier directory of the current layout; both are left as they were.

Carrier lifetime. Each map call creates a directory `pe_map_tls_<32 random hex digits>` under the temporary directory with a protected DACL that grants access only to the process's user, and holds it open without delete sharing. It writes `pe_map_tls_<same digits>.dll` there with `CREATE_NEW`, notes its file ID and closes the write handle, because the loader's open does not share writing. It reopens the file for reading and deletion, shared for reading only, checks that the file ID is unchanged and that the contents read back through that handle are the bytes written, loads the file, and checks that `GetMappedFileNameW` for the loaded carrier names the file behind the held handle. On release, or when the map fails, it unloads the carrier and deletes the file and the directory through those two handles. Nothing is deleted by name or pattern, so a map call removes only what it created. Measured on Windows 10 build 19045 with a scratch program: while the two handles are held and before the load, opening the file for writing, renaming it, deleting it and renaming its directory fail with error 32, renaming the directory above fails with error 5, and `LoadLibraryExW` still loads the file. A carrier deleted with the classic disposition after `FreeLibrary` stayed listed after its last handle closed in 1 of 10 runs, and its directory then could not be removed (error 145); with POSIX delete semantics the name was gone and the directory removed in 10 of 10 runs, so `pe_map` uses those and falls back to the classic disposition where the file system lacks them. A cleanup failure is appended to the failure detail of a failed map; `pe_map_release` has no way to report one, and the directory then stays.

Limits: carrier directories left by processes that ended with an image mapped are not removed; nothing but a handle establishes which process a carrier belongs to. Processes of the same user can still replace the carrier between the close of the write handle and the reopen; the file ID and read-back checks then fail the map instead of loading another file (no fixture provokes that race). The template is copied once, when the image is mapped, so a later write to the template inside the image does not reach threads created afterwards (not measured on Windows). Code that assumes TLS index 0 without reading `AddressOfIndex` cannot be served. An image with an empty template and no zero-fill size still keeps `AddressOfIndex` as linked. A template on pages that are unreadable after section protection is rejected; no fixture covers that case. Windows 11 was not measured.

```powershell
cmake --build --preset dev --target pe_map cpu_tls_data cpu_tls_data_refuse
ctest --preset dev --no-tests=error -R '^cpu_native_tls_data$' -V
```

### Starting a mapped EXE

`pe_map_image` maps an EXE without notifying it, as `LoadLibrary` does. `pe_map_run(mapped, &exit_code)` starts it: it calls the TLS callbacks in table order with `DLL_PROCESS_ATTACH` and a null reserved argument, then the entry point on the calling thread, and stores the entry point's return value in `exit_code`. It returns 0 and runs nothing for an image with `IMAGE_FILE_DLL` set, for an image without an entry point and for an unreadable callback list. `mapped` must be a live base returned by `pe_map_image`; the headers are read without validation, as in `pe_map_export`. The entry point and callbacks are arbitrary code running in the host process on the calling thread, so an image that crashes, hangs or calls `ExitProcess` takes the host with it; run unknown images in a child process, as the test does. Start each mapping at most once. The caller keeps the mapping live until the call returns and prevents any thread, TLS callback or entry point from starting or releasing the same mapping during that call. The PEB argument that Windows passes to an EXE's entry point is not forwarded, and no process-detach notification follows, because the host process goes on running.

`cpu_exe_start_original` is a source-built EXE without a C runtime. Its TLS callback and its body (`native_entry`) each append one base-4 digit to a log, then the body calls the named import `cpu_import_provider!mix_args(5, 3, out)`, which returns 13, and returns `(log << 8) | 13`. The PE entry point is the startup wrapper `native_startup` ([`exe_startup.asm`](../tests/cpu/exe_startup.asm)), which passes the body's value to `finish_exe`; a process launch of the EXE makes that value its exit code. `cpu_native_exe_start` compares the launch with `pe_map_image` and `pe_map_run` in a child process:

- Original: both give `0x60d`: the callback (digit 1, reserved null) ran before the body (digit 2), and the import returned 13.
- `cpu_exe_start_no_tls` has no TLS directory. Both give `0x20d`, so the log reflects the callback.
- The original bound to `cpu_import_altered`, whose `mix_args` returns one more, gives `0x60e` under `pe_map`.
- With `IMAGE_FILE_DLL` set in a copy, `pe_map_run` returns 0 and leaves the exit code untouched.
- With the PE entry point (the wrapper) moved into `.data` in a copy, the Windows launch faults with `0xc0000005`, while `pe_map_image` refuses it as "entry point outside executable section" and nothing runs.

As a one-off manual check (not in CI), I removed the callback loop from `pe_map_run`; the test then failed with `mapped original 0x20d != native 0x60d`.

This shows one EXE with an ordered start log and an import bound through the resolver. It does not run a C runtime startup, which would call `ExitProcess` in the host. It does not cover implicit TLS data, a second thread's TLS callbacks, static initializers in imported DLLs, `GetModuleHandle(NULL)` (which still returns the host) or the PEB. Those remain untested or unsupported. Measured on Windows 11 build 26300. [Issue #165](https://github.com/R0Y00/Xbox2Windows/issues/165)

```powershell
cmake --build --preset dev --target pe_map cpu_exe_start_original cpu_exe_start_no_tls cpu_import_provider cpu_import_altered cpu_exe_io cpu_import_live cpu_exe_io_hang
ctest --preset dev --no-tests=error -R '^cpu_native_exe_start$' -V
```

### Starting an EXE with two DLLs

`cpu_startup_exe` and two DLLs, all built without a C runtime, import each other in a known order. `cpu_startup_a` has no fixture DLL dependencies and keeps an event log and a value set by its initializer (`7`). `cpu_startup_b` imports `cpu_startup_a`, and its initializer publishes `a_value() * 2`, so it reads zero if it runs first. The EXE imports both. These initializers are the DLLs' `DLL_PROCESS_ATTACH` entry points, called by the loader, not C++ static constructors. The EXE's TLS callback logs `t` and its entry point logs `e`. The entry point then runs two worker threads one after the other and joins each with a 10 s limit. A's TLS callback allocates an explicit kernel32 `TlsAlloc` slot and sets `1` on the main thread. The workers set `2` and `3` in their respective copies of the slot. Both images use empty static TLS data templates. The entry point writes the event log and the callback trace to standard output and returns `b_value() | first worker << 8 | second worker << 16 | main << 24` as its exit code.

`cpu_native_startup` launches the EXE with the Windows loader in a directory holding the two DLLs, and starts it with `pe_map_run` in a child process. The harness reads the import tables, orders the images dependencies first, maps each DLL, serves the imports of the DLLs with `pe_map_export` on the mapped images and the kernel32 imports with the host. `pe_map` does the mapping, relocation, section protection, each DLL's attach notification and the start of the EXE; it does not read import tables to find dependencies. On Windows 11 build 26300:

```text
native: LOG:abtew2w3m1 exit 0x103020e
mapped: LOG:abtew2w3m1 exit 0x103020e
```

- The startup event logs and exit codes match, and both standard error streams are empty. Under both loaders `a` and `b` ran once and in dependency order, the TLS callback ran after the DLL initializers and before the entry point, the entry point ran once, and `b_value()` was `14`. The three threads read their own `TlsAlloc` values.
- Without B's initializer (`cpu_startup_b_noinit` under B's name) both loaders give `LOG:atew2w3m1` and exit code `0x1030200`. With another A under A's name (`cpu_startup_a_alt`, whose value is 9) both give the original log and `0x1030212`. Both controls differ from the original under both loaders.
- Mapping the EXE before its DLLs fails with `unresolved import cpu_startup_b.dll!b_value`.
- On Windows 10 build 19045.6456, A's TLS callback read `1` on process attach using the main thread ID. For each joined worker it read `0` on thread attach, then the worker's value (`2` or `3`) on thread detach. The harness checks the recorded IDs against the actual main and worker IDs in each process. Both loaders produce the same observations by thread role, including with B's initializer disabled and with the alternate A. The test measures callbacks of the dependency DLL. [Thread forwarding](https://github.com/R0Y00/Xbox2Windows/pull/139)

Not covered: `DLL_PROCESS_DETACH` ordering at exit, DLLs built with the C runtime (C++ constructors), implicit TLS and `GetModuleHandle(NULL)`. Measured on Windows 11 build 26300. [Issue #165](https://github.com/R0Y00/Xbox2Windows/issues/165)

```powershell
cmake --build --preset dev --target pe_map cpu_startup_exe cpu_startup_a cpu_startup_a_alt cpu_startup_b cpu_startup_b_noinit cpu_startup_exe_timeout cpu_startup_exe_create
ctest --preset dev --no-tests=error -R '^cpu_native_startup$' -V
```

### An EXE's own TLS template and callbacks

`cpu_exe_tls` is a CRT-free, source-built EXE whose own image holds the implicit TLS data and the two TLS callbacks of `cpu_tls_value`: an int (`7`) and an 8 KiB array of distinct words in one initialized template that spans a 4 KiB page boundary, with callbacks in `.CRT$XLB` (caller 1) and `.CRT$XLM` (caller 3). The EXE supplies the TLS directory the C runtime would otherwise provide (`_tls_used`, `_tls_index`, the `.tls` markers and the `.CRT$XLA`/`.CRT$XLZ` bounds). Each callback records `observe()`: the int in the low 16 bits and the number of changed array words above them, so a fresh block reports 7. The entry point writes 20 to the main thread's copy and starts two workers. Worker A writes 50 and waits. Worker B starts after A's write, writes 60 and waits. A is then released and joined, then B, so every notification comes in a fixed order. The EXE prints one ordered startup log and returns the main thread's final `observe()` as its exit code.

`cpu_native_exe_tls` checks the template layout and callback table with `pefile`, launches the EXE with Windows, and in a child process maps it with `pe_map_image` and starts it with `pe_map_run`. Both runs must print this log and exit code:

```text
LOG:m11=7 m13=7 E mf=7 mw=20014 a21=7 a23=7 af=7 aw=20032 b21=7 b23=7 bf=7 bw=2003c al=20032 a31=20032 a33=20032 bl=2003c b31=2003c b33=2003c ml=20014 X=20014
exit 0x20014
```

`m11` is the main thread, reason 1 (process attach), caller 1. `a2`/`a3` and `b2`/`b3` are thread attach and detach of workers A and B. `E` is the entry point, and `f`, `w` and `l` are a thread's own first, written and last observations. Measured on Windows 10 build 19045, MSVC 19.44, `dev` and `release` presets:

- Under both loaders both callbacks run in table order on process attach and on thread attach and detach of both workers. Every thread starts from the template, and each thread's writes stay in its own copy, including the values the detach callbacks see.
- Before this test, `pe_map_run` called the EXE's callbacks only for process attach: `notify` returns early for an image without `IMAGE_FILE_DLL`, so a started EXE received no thread notifications. Against that mapper the test fails with `notifications for ['m1']`. Now `pe_map_run` marks the EXE as started once every process-attach callback has returned, before it calls the entry point. From then until `pe_map_release`, `pe_map.dll`'s thread notifications call the EXE's TLS callbacks with `DLL_THREAD_ATTACH` and `DLL_THREAD_DETACH`, never its entry point. Release sends the EXE no process detach.
- Eligibility: `cpu_exe_tls_early` starts and joins a probe thread from its first process-attach callback. Under `pe_map` the probe runs (`pf=7` between the two process-attach callbacks) but receives no EXE notification. A mapper that marks the EXE as started before the process-attach callbacks fails with `notifications for [..., 'p2', 'p3']`. Threads that attach during the process-attach callbacks, whether a callback or the host started them, are outside the guarantee: Windows holds such a thread until process initialization ends, which `pe_map` does not reproduce, so this variant is not launched natively.
- The child binds only `kernel32.dll`, to the host's kernel32, and gives the callbacks per-reason counters (`exe_tls_mirror`) that it reads after release. A started run counts 2 process attaches, 4 thread attaches, 4 thread detaches and 0 process detaches. An EXE that is mapped but never started, and one whose `pe_map_run` is refused (a null exit code), count 0 for every reason while two host threads attach and detach and through release.
- Negative controls patch copies of the built EXE with the `cpu_native_tls_value` helpers. Both loaders reject both: a swapped callback table as `callback order: m1 ran callers ['3', '1']`, and a template zeroed from its first page boundary as a template mismatch on the first callback.

Not covered: process detach at exit (the log is written before the entry point returns), C-runtime startup in a mapped EXE, `GetModuleHandle(NULL)` and the PEB. [Issue #165](https://github.com/R0Y00/Xbox2Windows/issues/165), [issue #870](https://github.com/R0Y00/Xbox2Windows/issues/870)

```powershell
cmake --build --preset dev --target pe_map cpu_exe_tls cpu_exe_tls_early
ctest --preset dev --no-tests=error -R '^cpu_native_exe_tls$' -V
```

### Lifecycle of two DLLs with the static C runtime

`cpu_life_a` and `cpu_life_b` are built with the static MSVC runtime (`/MT`, linked without `/INCREMENTAL`, so the PE entry point is the runtime's `_DllMainCRTStartup` and not an incremental-link thunk). A real global constructor runs before `DllMain(DLL_PROCESS_ATTACH)` and a real global destructor after `DllMain(DLL_PROCESS_DETACH)`. Nothing calls them by hand. B imports A. A's constructor sets its state to 7. B's constructor stores `a_value() * 2`, and its destructor reads A's state again. `b_via_a()` returns `a_value() + b_value()` at the time of the call. Every constructor, destructor and `DllMain` call logs a token into `cpu_life_sink.dll`, a small CRT-free DLL that the test host loads first and keeps for the whole child process, so it is valid during every destruction and sees the last destructor. `DllMain` only calls into the sink, which takes no lock and needs nothing from the loader.

Each constructor also records its callers with `RtlCaptureContext`, `RtlLookupFunctionEntry` and `RtlVirtualUnwind`. The test reads the entry-point function from `.pdata`, follows its closing `jmp rel32` to the function that dispatches the process attach and detach (this is why the entry point itself is not on the stack), and requires that function on the constructor's stack with the next frame outside the image. That frame is the loader's under Windows and `pe_map`'s under `pe_map`. `RtlCaptureStackBackTrace` is not used: on an image that `pe_map` mapped it returns only the first frame.

`cpu_native_crt_lifecycle` runs one scenario in fresh child processes with a 30 s timeout: through `LoadLibraryExW` and `FreeLibrary`, where the loader finds A as B's dependency and decides every order, and through `pe_map_image`, `pe_map_export` and `pe_map_release`. For `pe_map` the harness orders the images from the import tables, maps A before B, serves B's import of A with `pe_map_export` and releases B before A; `pe_map` itself keeps no dependency graph. The scenario loads B, calls the exports, changes A's state with `a_set(99)`, unloads, reloads, unloads, and then runs two more load and unload cycles. Three fresh processes per loader must report identical stages. On Windows 11 build 26300 both loaders give identical events, values and residency at every stage:

```text
load:           A:ctor, A:attach, B:ctor, B:attach          a_value 7, b_value 14, b_via_a 21, a_ctor_runs 1
a_set(99):      (no events)                                 a_value 99, b_via_a 113
unload:         B:detach, B:dtor a=9, A:detach, A:dtor      A and B no longer mapped
reload:         A:ctor, A:attach, B:ctor, B:attach          a_value 7, b_via_a 21, a_ctor_runs 1
unload again:   B:detach, B:dtor a=7, A:detach, A:dtor      A and B no longer mapped
cycles 3, 4:    the load and unload lines above
```

The reload starts from fresh state (7, not 99) and runs each constructor once more. B's destructor still sees A alive, with A's state at that time (`a=9`, then `a=7`). Residency is `GetModuleHandleW` for Windows and `VirtualQuery` on the mapped base right after the releases for `pe_map`.

- Without A's constructor (`cpu_life_a_noctor`) both loaders log `A:attach, B:ctor, B:attach`, every value is 0 and A's constructor is not among the callers. Without B's destructor (`cpu_life_b_nodtor`) both log `B:detach, A:detach, A:dtor` on unload. Both controls differ from the original in the events under both loaders.
- Base addresses: Windows loaded B at the same address in all three processes, as the system randomizes a DLL's base once per boot. `pe_map` mapped it at a different address every time and never at the preferred base, so the relocation path ran on every mapping.
- Responsibilities: Windows counts references. A second `LoadLibrary` of B returns the same module and runs nothing, and A stays loaded while B or the harness holds it. `pe_map` keeps no module table and no reference counts. Mapping B twice gives a second image that runs its constructor again. The order of mapping and release and the import of A are the harness's work.
- Mutation checks, not in CI: making `pe_map_release` skip the process detach, or not free the image, each made `cpu_native_crt_lifecycle` fail.
- Not covered here: a double release, null and foreign or interior pointers, which `cpu_native_lifetime` already checks for `pe_map_release` (it returns 1 once and 0 otherwise); and releasing A before B, which would touch released memory. Neither this test nor the harness depends on freed memory, apart from asking `VirtualQuery` about the released range right after the release.

```powershell
cmake --build --preset dev --target pe_map cpu_life_sink cpu_life_a cpu_life_a_noctor cpu_life_b cpu_life_b_nodtor
ctest --preset dev --no-tests=error -R '^cpu_native_crt_lifecycle$' -V
```

### An empty relocation directory away from the preferred base

`cpu_import_original` contains no absolute addresses. Its base-relocation directory has Size 0 and `IMAGE_FILE_RELOCS_STRIPPED` is clear. The issue used `cpu_register_probe`, which has the same properties; this test uses `cpu_import_original` because it exports `call_mix`, so a call can be checked after loading, and the register probe is the harness.

`cpu_native_reloc_free` runs each case in its own child process. It reserves the preferred base (`0x180000000`) for `SizeOfImage` and checks with `VirtualQuery` that the whole range is held by that reservation. It then loads the image with `LoadLibraryExW` and maps it with `pe_map`. A second copy has `IMAGE_DLLCHARACTERISTICS_DYNAMIC_BASE` cleared.

The discriminating case is Windows with the `DYNAMIC_BASE`-cleared copy. With nothing reserved, Windows loads that copy at `0x180000000`. With the base reserved, it loads it elsewhere and `call_mix` matches the reference on all 249 input pairs. The as-built copy also moves under Windows, but ASLR would move it anyway. `pe_map` always allocates away from `ImageBase`, so for it the reservation only shows that an empty relocation directory is accepted at another base and that the image runs there.

The bases vary between runs, so the test checks only that they differ from the preferred base. With the base reserved, a copy that sets `IMAGE_FILE_RELOCS_STRIPPED` fails in `LoadLibraryExW` with error 487 (`ERROR_INVALID_ADDRESS`) and is rejected by `pe_map` as "relocations stripped"; `pe_map` would reject it with or without the reservation.

`pe_map` therefore does not reject an empty relocation directory when it maps away from `ImageBase`, because Windows loads such images. An image that contains absolute addresses but omits its relocations is malformed and would fail at run time under either loader. Measured on Windows 11 build 26300. [Issue #77](https://github.com/R0Y00/Xbox2Windows/issues/77)

```powershell
cmake --build --preset dev --target pe_map cpu_import_original cpu_import_provider cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_native_reloc_free$' -V
```

### Mutated images

`cpu_native_mutation` applies a fixed list of single-field edits to the header, the first section header, and the export, import and base-relocation directories of `cpu_register_probe` and `cpu_import_original`, plus three truncations. Each case runs in its own process and must print either `MAP_OK` or a named `MapStatus` with a detail string; a crash or a timeout fails the case. Across 46 cases the outcomes were 26 `format`, 16 `ok` and 4 `relocation`, with no crash. A three-line mapper that dereferences null makes the parent report `child exited 0xc0000005`, so the harness detects the failure mode it covers.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_mutation$' -V
```

### Forwarded exports

`cpu_forwarder` exports `local_value` and forwards `mix_args` to `cpu_import_provider.mix_args`; the linker records that string in the export directory. `pe_map_export` keeps returning null for the forwarder. `pe_map_export_resolved` follows the string through the host resolver, and the returned address matched `GetProcAddress` on a `LoadLibrary` copy in `cpu_native_forwarder`. `local_value` resolves to the same RVA under both loaders. An absent name and a resolver that returns null give null. The `Module.#ordinal` form is parsed but has no fixture yet.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_forwarder$' -V
```

## VEX.128 16-bit packed-integer leaf functions

The [translator](../tools/cpu_vex_aot.py) lifts Windows x64 leaf functions that use VEX.128 word forms. It supports:
- `vpaddw`, `vpsubw`, `vpmaxsw`, `vpminsw`, `vpmulhw` and `vpavgw`;
- `vpsraw`, `vpsllw` and `vpsrlw` with an immediate count;
- `vpunpcklbw`, `vpunpckhbw`, `vpunpcklwd` and `vpunpckhwd`;
- `vpackuswb`;
- the 16-byte moves `vmovdqu` and `vmovdqa`, the logic ops `vpxor`, `vpand` and `vpor`, and `vzeroupper`.

The ABI belongs to this profile: RCX and RDX hold the addresses of two 16-byte inputs, R8 the address of a 16-byte output, and no return bits are defined. Each XMM register becomes a 16-byte value and each form an explicit per-lane loop, so the generated C++ states the wraparound, the unsigned saturation of signed words in `vpackuswb`, the shift-count limits and the `vpavgw` rounding instead of using intrinsics. Memory operands must be exactly `[RCX]`, `[RDX]` or `[R8]` with a 16-byte access, and stores go only to `[R8]`. Only VEX encodings and the volatile XMM0-XMM5 are accepted. Any other instruction, an EVEX form such as a masked `vpaddw`, a legacy SSE encoding, XMM6-XMM15, a VEX.256 operand or another operand shape is rejected with its RVA. Decoding uses iced-x86, and function bounds come from `cpu_leaf_aot.bounded_code`.

```powershell
cmake --build --preset dev --target cpu_vex_words_original_lifted cpu_vex_words_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_vex_words$' -V
```

The [fixture](../tests/cpu/vex_words.asm) has five functions. The test runs each through the native and lifted DLLs on the same 4096 input pairs:
- every pairing of eight edge words, including 0x7fff, 0x8000, 0xffff and 0x00ff, broadcast to all lanes;
- the edge words rotated across lanes;
- seeded random lanes.

Each call runs in one 16-byte-aligned arena in which the two inputs and the output are separated by 0xcc guard zones. The whole arena must match bit for bit, and the native call must leave everything outside its output unchanged.

**Alignment.** `vmovdqu` accepts any address, and the four functions that use only `vmovdqu` also match on buffers 8 bytes off alignment. `vmovdqa` requires 16-byte alignment. `aligned_ops` is called only with aligned buffers, because the generated C++ does not check the precondition that the native instruction would fault on.

**Aliases.** The output may alias either input exactly. Reads and the store then happen in program order, each as one 16-byte copy. With the output aliasing A and then B, and with RCX equal to RDX, all five functions match on 512 pairs. `store_reload` stores, reads the inputs again and stores a second result; with the output aliasing an input, its result differs from the non-aliased one in 955 of these cases, so the order is observed. Partial overlaps are not tested.

**Controls.** A second fixture build replaces one `vpsraw` with `vpsrlw` in `shift_edges`. Its lifted DLL matches its own native build and differs from the original in 4044 of 4096 outputs, while the other functions are unchanged. Translation rejects three patched copies: a VEX.256 load, a VEX.256 store, which uses a three-byte VEX prefix because of R8, and a `vpaddw` turned into `vpaddd`. It also rejects eight hand-encoded bodies, each first checked against its iced disassembly: an EVEX `vpaddw xmm0{k1}`, a legacy `paddw`, `xmm6` as destination and as source, `[rcx+10h]`, `[rax]`, a store to `[rcx]` and `vpsraw` with an XMM count.

Sixteen mutations of the translator each fail the test:
- `vpavgw` without rounding;
- `vpmulhw` keeping bit 15;
- `vpsraw` counts above 15 not clamped;
- `vpsllw` and `vpsrlw` counts above 15 not cleared;
- `vpackuswb` without saturation;
- `vpunpckhbw` and `vpunpckhwd` reading the low half;
- `vpmaxsw` comparing unsigned;
- either YMM check removed;
- the VEX-encoding check removed;
- either nonvolatile-register check removed;
- stores deferred to the end of the function;
- input reads cached.

Only the alias checks catch the last two.

## String memory transfers in the control-flow CPU lifter

`tools/cpu_switch_aot.py` lowers MOVS and STOS, with and without REP, to copy and fill 1-, 2-, 4- and 8-byte elements in either direction. These instructions
can occur inside branches, loops and translated callees; they use the existing
register-definedness and nonvolatile-save proofs. A zero REP count touches no memory
and leaves the pointers unchanged. The loop updates RSI/RDI and RCX at their architectural widths.

Each element is read before it is written. Forward overlapping copies deliberately
observe bytes written by preceding iterations, unlike `memmove`. Volatile guest
reads and writes preserve access faults and iteration order under `/O2`. Multi-byte accesses allow unaligned addresses and keep the original element width.
Arithmetic flags survive a transfer and remain available to the existing bounded
flag-consumer proof. STD/CLD are represented by a local direction flag. A control-flow proof requires DF clear on every path to calls, tail jumps and returns, including joins with different direction states. The backwards path clears DF before leaving its copy region. Non-repeated forms preserve RCX.
Address-size overrides, segment prefixes, REPNE, LODS, CMPS and SCAS remain rejected
in this control-flow profile. The shared straight-line translator has its separate
string-instruction coverage. Guest exception resumption, native REP register values
at a fault, concurrent memory observation and throughput equivalence are not modeled.

The fixtures, reference and implementation are original MIT source. They use
source-built buffers, not title code. An assembly harness checks physical RSI/RDI
preservation and DF on return. The independent byte-array model checks complete
buffers and their surrounding bytes, pointer updates, counts, results and carry
from a compare before the transfer. Macro-generated instruction forms are recorded
from the decoded functions only after all comparisons and controls succeed.

```powershell
cmake --build out/native --target cpu_switch_strings_lifted cpu_switch_strings_lifted_o2 cpu_switch_strings_mutants cpu_string_probe cpu_guarded_call --parallel 4
ctest --test-dir out/native --no-tests=error -R '^cpu_aot_switch_strings$' -V
```

Windows 11 build 26300.9974, MSVC/ML64 19.44.35230 and Python 3.12.8:
43 exports, 24,915 native/AOT/reference comparisons and 96 access-violation
comparisons passed, including the `/O2` translation. Compiled incorrect
implementations must fail the comparison: a bulk `memmove` on destructive overlap
and a truncated repeat count, plus ignored backwards direction.
The comparison includes inaccessible addresses with a zero count, and a read that
copies three bytes before faulting at a protected page; no later trace store executes. The same fault must occur with a repeat count of 2^32 + 1, so the full 64-bit RCX is used. Non-overlapping copies run from an even and an odd source address, and an overlapping copy runs with the destination 8 bytes past the source. `rep stos` also runs with a nonzero fill whose low byte is zero, with RCX = 2^32 (zero ECX) at a protected destination, with a two-byte `rep stosb` from the last writable byte (the first byte is stored before the fault), with a word that straddles a protected page (nothing is stored), and with a one-byte store just below a 4 GiB boundary, so RDI must carry into its upper half. It also compares native and translated memory effects when a word, dword or qword straddles the protected page.
No title startup result is claimed.

The Windows ABI boundary follows [Microsoft's direction-flag convention](https://learn.microsoft.com/en-us/cpp/build/x64-software-conventions).
MOVS/STOS semantics follow the [Intel SDM](https://cdrdv2-public.intel.com/868137/325462-089-sdm-vol-1-2abcd-3abcd-4.pdf).

## Switch dispatch through jump tables

Compilers lower a dense `switch` to a bounds check and an indirect `JMP` through a table of 32-bit entries. The [jump-table translator](../tools/cpu_switch_aot.py) recognizes the two x64 layouts in common use. MSVC stores image-relative entries and loads them through a register holding `__ImageBase`. LLVM stores signed offsets from the table and adds the table address. The translator reads the table from the PE, then emits a C++ `switch` whose cases jump to labels at the decoded targets. The host compiler builds its own dispatch from that switch.

```powershell
cmake --build --preset dev --target cpu_switch_original_lifted cpu_switch_swapped_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_jump_tables$' -V
```

The decoder starts at the export and follows branches, fallthrough and table entries, so it never decodes the table bytes that MSVC places after a function's code. An indirect jump through a register is accepted only in the sequence `load entry; add base; jmp`, reached by the fallthrough of an unsigned `JA` or `JAE` against an immediate. The table length comes from that bound. A must-agree constant pass over the control-flow graph gives the base register's RIP-relative value, which also covers a base loaded once outside a loop. Value numbering over the single-entry block that contains the bound shows that the index register holds the compared value, zero- or sign-extended from 32 bits. Every table entry must be the first byte of a decoded instruction inside the function, and the table must not overlap any decoded instruction. Flags are modelled only between a producer (`CMP`, `TEST`, `ADD`, `SUB`, `AND`, `OR`, `XOR`, `INC`, `DEC` or `NEG`) and its consumers: a conditional jump, `SETcc` or `CMOVcc`. Producer and consumer must sit in one straight-line run that no edge enters; only instructions that leave the flags unchanged (moves, `LEA`, `NOT`, `SETcc`, `CMOVcc` and the sign extensions `CBW`, `CWDE`, `CDQE`, `CDQ` and `CQO`) may lie between them. Overflow, parity, signed and carry conditions are supported for these producers as defined by their affected flags, using their operand width and the low result byte respectively (`INC` and `DEC` preserve `CF`, so carry conditions after them remain rejected). A forward pass records how many low bytes of each register (0, 1, 2 or 8) every path defines, and rejects a read of more bytes than that. Writes to AH, BH, CH and DH, 16-bit `CMOVcc` and indirect calls are rejected; direct calls are described below. Stack operations and nonvolatile registers are accepted only under the frame rules described below.

The translator also lowers `JO/JNO/JP/JNP`, `SETO/SETNO/SETP/SETNP` and
`CMOVO/CMOVNO/CMOVP/CMOVNP`, along with `ADD` carry/signed conditions
(`B/AE/BE/A/L/GE/LE/G`), `INC`/`DEC` signed conditions (`L/GE/LE/G`), and all 16
`NEG` conditions (`CF = (result != 0)`, `OF = (result == sign_bit)`). `ADD`,
`SUB` and `CMP` overflow is computed from saved unsigned operands; `INC`, `DEC`
and `NEG` use their wrapped result at the signed boundary. Logical producers
clear overflow. Parity uses only the low result byte, as defined by the [Intel SDM](https://cdrdv2-public.intel.com/843827/253665-sdm-vol-1-dec-24.pdf).
The existing straight-line and register restrictions still apply, including
rejection of 16-bit `CMOVcc`, carry conditions after `INC`/`DEC`, and flags
from shifts or across joins.

`cpu_aot_flag_conditions` compares 289 MASM exports with their generated C++
and an independent Python reference over 532,140 inputs. The fixtures cover
ten flag producers at 8/16/32/64 bits, guarded byte stores, 32- and 64-bit
conditional moves, conditional branches, and a memory-operand `NEG [mem]` whose
lifted statement reads memory once via `load_always`. Both operand registers are
clobbered before the consumers to verify that the flag inputs were saved.
Negative controls verify that a variable-count `SHL RAX, CL` producer,
`INC`/`DEC` followed by carry conditions (`JB/JAE/JBE/JA/SETB/CMOVB`), and
`NOT` without a preceding producer or with `sets_flags` are rejected.
The former `SETO` rejection control now checks successful translation.
The test also records its decoded mnemonics through `FORM_COVERAGE_RECORD`.
Coverage reads that record for this parameterized MASM fixture instead of
interpreting macro tokens. Only a passing test with a current record covers the
arithmetic flag instruction forms; failed, skipped and missing records
cannot add green cells.

```powershell
cmake --build --preset dev --target cpu_flag_conditions_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_flag_conditions$' -V
```

`ROL` and `ROR` on 8-, 16-, 32- and 64-bit register and memory operands with counts `1`, `imm8` and `CL` lower through the shared `rotate_bits` helper in `PRELUDE`, masking the count to `bits - 1` (with count `0` as a no-op), zero-extending 32-bit register destinations, and preserving upper bytes on 8- and 16-bit writes. Like shifts, rotates are excluded from `FLAG_NEUTRAL` and `FLAG_PRODUCERS`, so any `Jcc`, `SETcc` or `CMOVcc` reading flags produced by or across `ROL`/`ROR` is rejected. `cpu_aot_switch_rotates` builds [`switch_rotates.asm`](../tests/cpu/switch_rotates.asm) and runs 8,363 differential comparisons across all four widths, register and memory operands, count `0`, counts `>= width * 8`, upper-byte preservation and a narrow 1-byte return, together with 12 rejection controls.

```powershell
cmake --build --preset dev --target cpu_switch_rotates_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_switch_rotates$' -V
```

The supported functions take three integer words in RCX, RDX and R8 and return RAX, of which only the low bytes described below are compared. An entry can instead be translated with a fourth integer word in R9, as described below. Stack arguments (the fifth and later) and floating-point arguments are not supported. A leaf function without a `.pdata` entry is bounded by its executable section. Its walk may not enter an interval owned by another function's unwind entry.

The [source fixture](../tests/cpu/switch.asm) exports seven functions. `select_rva` uses the MSVC layout, and `select_rel` computes the same function through the LLVM layout. Both have seven cases, including two selectors that share a target, a case that falls through into the next, and a default path. `run_ops` runs a byte program in a loop and dispatches every byte through a five-entry table. It stops at the first opcode above 4. Through the register probe, the test compares each original export with the matching lifted function, a reference written in Python, and RBX/RSI sentinels. It runs 600 selector and operand pairs per layout and 138 programs. Half of the pairs carry nonzero upper register halves that must not reach the table index. A control assembles the same source with two entries swapped in each table, and its translation must follow the swapped reference. `shift_narrow` dispatches through a six-entry LLVM-layout table to byte and word `SHL`, `SHR` and `SAR` of a memory destination by `CL`. x86 masks the count to five bits for 8- and 16-bit operands as well, so counts of 8 to 31 still shift (a byte holding 1 shifted left by 8 becomes 0). The translator performs these shifts in 64 bits and truncates the stored result; 4,608 inputs, covering every count from 0 to 31 and count bytes above 31, match the original and the reference. `flag_select` dispatches through an eight-entry LLVM-layout table to `SETL`, `SETBE`, `SETG` into a memory byte, `SETNE` after a 16-bit compare, and `CMOVA`, `CMOVNS` (64-bit, after `TEST`), `CMOVGE` from memory with a `MOV` between compare and consumer, and `CMOVE` after `TEST` with an immediate. It compares EDX with bits 32 to 63 of RDX and returns RAX whole. A 32-bit `CMOVcc` therefore shows that it clears bits 32 to 63 even when the condition is false, and a byte `SETcc` shows that it keeps them. The translation reads a memory source of `CMOVcc` on both outcomes, as the processor does. 4,000 inputs at the signed, unsigned and 16-bit boundaries must match the original and the reference. The swapped-table build exchanges the `SETL` and `CMOVA` entries and must follow the swapped reference. `cmov_read` executes `CMOVNE` after `CMP ECX, ECX`, which never moves but still reads its memory source: called with a null R8 it must fault in the original and in the translation. The translator therefore performs every read of guest memory through a `volatile` load; with an ordinary load, an optimized MSVC Release build drops a read whose value is unused and the null call returns. Each instruction's memory operand is read once, at its own width, into a local declared before the rest of the statement, and every use of the operand names that local: an `ADD` whose result and flags both come from one operand performs one read. Reads of the function's own frame array, which cannot fault, stay ordinary loads. The [dead-read fixture](../tests/cpu/dead_read.asm) stores 1 to the trace before each read and 2 after it. Its exports read a value that is overwritten, compare memory with flags that are never read, read one byte, read two operands whose sum is cleared, return the value read, skip the read on a branch, and take a sum and its zero flag from one read, into a register and in place. `cpu_aot_dead_reads` builds the translation with the project's build and again with a separate `cl /O2` command, calls the original and each translation inside `__try` with fresh trace words and a fresh page, and compares the structured exception code, the return value, the trace and the page. For a readable operand all three return the reference. For a null operand, and for an operand whose read crosses into an inaccessible page, the original raises an access violation after the first marker and before the second, and both translations must do the same; an operand that ends exactly at the page boundary returns, at each width, and the second read of the pair faults when only the first is readable. On the branch that skips the read, the same inaccessible addresses return normally with the first and third markers written. A source control checks that each memory operand is read once through the `volatile` load and that the shared reads are used twice. An unoptimized build performs an ordinary load as well, so the `/O2` builds are the ones that show the difference. Stores are unchanged.

`select_rel_edges` bounds a three-entry LLVM-layout table with `JAE`, places its default before the guard, maps all three entries to one target placed more than 32 KiB before the table, and leaves a zero DWORD after the table. Selectors 0 to 2 must return the operand and selector 3 its complement, in the original, the translation and the swapped-table build.

On a copy of the linked DLL, the test patches the code, the table or `.pdata`, and expects each change below to be rejected:

- the bound jump removed or made signed;
- a sign extension that no longer comes from the compared register;
- a table entry into an instruction interior or into the neighbouring function;
- a table placed over code, or a second entry into the dispatch block;
- a base that is not the image base, or that does not come from a RIP-relative `LEA`;
- an undefined register, a read of a nonvolatile register's entry value, or an unsupported instruction;
- a leaf whose walk reaches another function's unwind interval;
- in `flag_select`, a `SETcc` into `AH`, a flag-writing `SHL` between a compare and its `CMOVGE`, and a second entry that reaches a `SETBE` with another path's flags.
- a `JGE` bound on `select_rva`, a 64-bit load of its 32-bit table entries, and a `select_rel_edges` table load whose base register differs from the register the entry is added to.

When `.pdata` no longer covers `select_rva`, the translation must stay unchanged.

On Linux, the fixture was assembled and linked with LLVM (`llvm-mc`, `lld-link`) from an equivalent GNU-syntax copy, because `llvm-ml` does not accept `IMAGEREL`. The original machine code ran in-process through an `ms_abi` call. It matched the lifted C++ built with clang on every input, for both the original and the swapped tables. The same translator also handled C switch statements compiled by clang for `x86_64-pc-windows-msvc` at `-O1` and `-O2`. Those builds have no `.pdata` for the leaves, and the loop's table base is hoisted. The generated sources compile without warnings under clang-cl `/W4 /WX`. For `flag_select`, the same Linux run matched the original and the reference on all 4,000 inputs, and the swapped table changed 762 results. The translator before this change rejected `flag_select` at its first `SETL`. Clang output at `-O1` and `-O2` for small C comparisons, selects and clamps (`SETL`, `SETE` into `R8B`, 32- and 64-bit `CMOVcc`) matched its translation on 25,000 calls per level. On Windows 11 with MSVC 14.44 and ML64, `cpu_aot_jump_tables` passed before `flag_select` was added: 600 selector pairs per table layout and 138 programs match, the swapped tables change 60, 60 and 99 results, and all 13 rejection controls and the leaf-without-unwind translation pass. The table labels are declared with `LABEL DWORD`, because ML64 rejects `IMAGEREL` of a code label inside a memory operand.

The jump-table translator wraps a 67h (32-bit) effective address at 32 bits and zero-extends it, as the hardware does. It rejects EIP- and ESP-based operands. `cpu_aot_switch_addr32` builds `lea rax, [ecx+1]`, `lea rax, [ecx-8]`, `mov eax, [ecx-8]` and `mov [ecx+8], edx` from separate MASM source. It compares the native and translated images over wrapped sums and upper-half register noise, using a page committed below 4 GiB for the load and store. Before this change the translation returned `0x100000000` for `lea_wrap(0xffffffff)` where the hardware returns 0. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_switch_addr32$' -V`.

The generated C++ follows address order, so a function that continues in code below its entry starts with a jump to the entry. A leaf without unwind data can reach such code through a jump, because it may use any unowned bytes of its section. Before this jump was emitted, the translation of such a function began at its lowest instruction and never ran the entry. `cpu_aot_entry_order` builds `inc_square`, which increments RCX and jumps to `square_tail` below it, and `pick_tail`, which branches there on a comparison. Both match the native exports on return, guarded trace, RBX and RSI over 249 input pairs. The control export `square_tail`, whose entry is its lowest instruction, still starts at the entry. A function whose chained unwind range lies below its entry takes the same path and has no fixture yet. A conditional branch whose target is its own fallthrough also emits a `goto`, so its target gets a label. Before that label was emitted, such a branch was accepted but its translation did not compile (`C2094: use of undefined label`). The export `branch_next` (`cmp rcx, rdx; jb` to the next instruction) checks that the assembled branch targets its fallthrough, that the target is labelled, and that native and translated calls match over the same 249 input pairs.

An 8- or 16-bit register write leaves the other bytes of the register unchanged, as on x86. The generated C++ merges the new byte or word into the 64-bit variable, which is declared with an initial value. The defined byte count becomes the larger of the old count and the write width, and a 32- or 64-bit write defines all eight bytes. A `SETcc` into a byte register is such a write, so it no longer needs the rest of the register to be defined; after `setl al` only AL is defined. A 16-bit `CMOVcc`, which would merge into the low word only when its condition holds, stays rejected. A 16-bit `IMUL` multiplies in `std::uint32_t`, because two `uint16_t` factors are promoted to `int` and `0xffff * 0xffff` overflows it. A three-operand `IMUL` (`imul r, r/m, imm`) writes its destination without reading it, so the destination need not be defined before it, while the two-operand form reads its destination. `RET` and `RET 0` (`C2 00 00`, which MSVC emits) no longer read RAX. The return width is the number of consecutive low RAX bytes that are definitely defined at every reachable `RET` or `RET 0`: 0, 1, 2 or 8, the lower bound over joins and over returns. It counts defined bytes and does not establish a source-language type. When no normal return (a `RET`, or a tail jump through a bound import as described below) is reachable, the width is `None` and the output says `no reachable normal return`, which describes the reachable control flow and makes no termination claim. Microsoft's [x64 calling convention](https://learn.microsoft.com/en-us/cpp/build/x64-calling-convention#return-values) states that the state of unused bits in the value returned in RAX is undefined. The generated function still returns a 64-bit `word`, callers need no width declaration and may use only those bytes, and a comment states the width unless it is 8. The upper bytes that the generator initializes carry no guest result. A `RET` reached while RAX may hold a guest address, for example from a RIP-relative `LEA`, is rejected, so such a function is not reported with a narrower width.

`cpu_aot_narrow_returns` builds [`narrow.asm`](../tests/cpu/narrow.asm) and translates thirteen exports: byte merges into defined registers, word `ADD`, `SUB`, `IMUL` and `MOVZX`, byte `SHL` and `SAR` and word `SHR` and `SAR` by `CL`, byte `INC`, `NOT` and `DEC` and word `NEG`, a comparison that writes only AL, a function that writes EAX on one path and only AL on the other, a copy that writes only AX, `MOVZX` from AL, `SETL` with a byte `MOV` between the compare and the `SETL`, `SETB` after a byte compare followed by `MOVZX`, two functions that store a word and write no RAX byte, one ending in `RET 0`, and `self_loop`, a loop that branches to itself. `self_loop` is translated and inspected and is never called: it must report `None` and `no reachable normal return`, while the storing function with a reachable `RET` must report width 0. Through the register probe, each original and translated export is compared with a Python reference over the direct-call input pairs; the shift export uses counts 0 to 31 and the count bytes 0x21, 0x3f, 0x80 and 0xff under nonzero upper halves. The original must match the reference in all of RAX, the guarded trace and RBX/RSI; the translation must match in the low return-width bytes of RAX and in the rest. The probe enters each function with RAX holding the function's own address, and the original's RAX bytes above the return width still hold that value on every path that leaves them unwritten. The test also checks the widths reported by the translator. Seven controls must be rejected: `mov al, dl` patched to `mov ah, dl`, `movzx eax, al` patched to `add eax, 0` after `mov al, dl` and after `setb al`, `RET 0` patched to `RET 8`, a join where one path defines only AL before `add eax, 1`, `cmovb ax, dx`, and `lea rax, byte_merge; ret`. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 3,819 inputs matched, the two inspected exports reported `None` and 0, and all seven controls were rejected. The Windows job runs the same test. The export `imul3_fresh` writes R10, R9W and EAX with three-operand `IMUL` before any read of them and is compared the same way, and an eighth control, `imul2_undefined`, whose `imul eax, ecx` reads the undefined EAX, must be rejected with `undefined register rax`; on Windows 11 with MSVC 19.50, 4,568 inputs matched, 249 of them for `imul3_fresh`, and all eight controls were rejected. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_narrow_returns$' -V`.

Whoever invokes the translator can give an entry a fourth integer argument in R9 by listing it as `name:4` in the export list of the command line, of `translate` or of the CMake function `cpu_translation`. `name` and `name:3` keep three arguments, and any other count is rejected with `unsupported argument count`. The count is that caller's choice. The translator does not derive it from the machine code, which does not show how many parameters the source function declares. On a four-argument entry R9 is defined at entry and the generated function takes a fourth `word r9` parameter, so a read of R9 is accepted whether or not the source function declares a fourth parameter. A three-argument entry, the default, translates as it did before this option: R9 is undefined at entry, a read before a write is rejected with `undefined register r9`, and a write followed by a read is accepted with `r9` declared as a local. The host must declare and pass the same number of arguments as the entry was translated with. A four-argument entry that never references R9 has an unused parameter, which `/W4` reports.

`cpu_aot_fourth_argument` builds [`fourth_arg.asm`](../tests/cpu/fourth_arg.asm) and translates `r9_mix:4,r9_loop:4,no_r9:4,r9_scratch`. `r9_mix` computes `lea rax, [rcx+r9*2]; sub rax, rdx` and stores R9. `r9_loop` counts additions down in R9D through `CMP`/`JA`, `TEST`/`JZ` and `DEC`/`JNZ`, and stores R9 as it leaves it. `no_r9` does not touch R9. `r9_scratch`, a three-argument entry, writes R9B before any read of R9, reads only R9B and then overwrites R9. The register probe gains `probe4`, which passes its fifth argument to the function in R9 and samples RBX/RSI through its sixth. The test checks that the built source is the translation of that list. It calls each four-argument entry through `probe4` and the three-argument entry through `probe`, and compares native and translated calls with a Python reference on the return value, the guarded trace, RBX and RSI. The four-argument entries get the direct-call input pairs crossed with ten fourth arguments, among them 0, 1, 64, 65, 2^32 - 1, 2^32, 2^63 and all ones, and for `no_r9` the fourth argument must not change any result. As a control for an existing three-argument entry, `byte_merge` from `narrow.asm`, which writes R9 before reading it, keeps its three-parameter signature and its `r9` local and still matches through `probe`. `r9_mix` must be rejected with `undefined register r9` when translated with three arguments and accepted as `r9_mix:4`. The counts `5` and `x` must be rejected, and so must a copy of the image with the index register of `r9_mix` patched from R9 to R10, with `undefined register r10`. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 7,719 calls per side matched and every control passed. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_fourth_argument$' -V`.

Whoever invokes the translator can have RIP-relative data translated by passing `--image-data` on the command line, `image_data=True` to `translate`, or `IMAGE_DATA` to the CMake function `cpu_translation`. Without it, RIP-relative data is refused as before and the generated C++ is unchanged. With it, the generated DLL also exports `lifted_bind_image(base)` and `lifted_unbind_image()`. The host passes the base of a live mapping of the input image, loaded by Windows or by [`pe_map`](../tools/pe_map.cpp). The translated functions keep their signatures and read the bound base once, at entry. A RIP-relative memory operand becomes a load or store at base + RVA. The translator requires its whole width to lie inside one section's virtual extent (`VirtualSize`; a mapping zero-fills the part past the file-backed bytes), a readable section for a read, and a writable, non-executable section for a write. Otherwise it reports the target as outside the image, outside every section, crossing the end of a section, or a write to a read-only or executable section. A RIP-relative `LEA` yields base + RVA for any RVA inside the image, so the register can be stored, compared, used as a base or returned. A `LEA` that loads a recognised jump-table base stays a guest address that the switch replaces, so a function whose only RIP-relative reference is a table base needs no mapping.

`lifted_bind_image` accepts a mapping only if it is the input image. It compares an FNV-1a hash, computed by the translator from the file, of the DOS signature and `e_lfanew`, the PE signature and file header (with `TimeDateStamp`), `SizeOfImage`, `CheckSum` and the section table, and returns 2 when they differ. It then hashes the file-backed bytes of every readable, non-writable section, except the 8-byte slots that DIR64 relocations rewrite and the import address table, and returns 3 when they differ. A null base returns 1 and an accepted mapping 0; a refused bind keeps the current binding. The hash detects accidental differences, not a deliberately constructed image. Writable data and relocated slots are not compared. A loader write to other read-only bytes, such as the CFG pointer slots of an image linked with `/guard:cf`, would make bind refuse the mapping; this was not measured. While nothing is bound, a translated function that addresses image data raises the noncontinuable structured exception 0xE0494D47 at entry, before any access. An address that a translated function returns or stores is valid only while that mapping stays bound and mapped, and a later bind does not move it. A relocated pointer loaded from the image, such as a vtable entry or a pointer in `.data`, holds the absolute address in that mapping, as it does for the original code. A code address loaded this way cannot be called through the translation, because indirect calls stay rejected. Bind and unbind must not run concurrently with each other or with a translated function.

`cpu_aot_image_data` builds [`image_data.asm`](../tests/cpu/image_data.asm) with a counter and a relocated pointer in `.data`, a scratch word in its zero-filled tail, and a read-only table of two relocated code addresses and a constant. Its eleven exports read the counter, add to it and return the old value, return its address, load the relocated pointer, compare an argument with the counter's address, store the table's address into an object and return the object, load a table entry, exchange the scratch word, load through a pointer, dispatch through an MSVC-layout jump table whose cases update the counter or the scratch word, and dispatch through an LLVM-layout table without data. The test checks the disassembly: 18 RIP-relative operands, no absolute memory operand. The original runs in one `pe_map` mapping, and the translation is bound to a second mapping of the same file; both start from the image's initial data. After each of 29 calls, the test compares the return value, the guarded trace, RBX/RSI and the 1,048 writable bytes of the two mappings with each other and with a Python reference. An address inside the image is compared as its RVA, the address minus the mapping's base. The exports that return an address and the relocated slot in `.data` are known from the fixture, not inferred from values. Writing through the counter address that each side returned changes what both sides then read. In one mapping, the translation reads the original's write and the original reads the translation's. After unbind and a bind to a third mapping, results follow that mapping. Bind returns 1 for a null base, 2 for another DLL and for a copy with one byte of `TimeDateStamp` changed, and 3 for a copy with one byte changed in the read-only table or in a `.text` jump table; each refusal leaves the binding unchanged. A copy with a changed initial counter in `.data` is accepted. Bind also accepts the system loader's mapping. A child process calls translated functions while nothing is bound: the data exports raise 0xE0494D47, an object passed to `set_vtable` stays unchanged, and the two exports without image data run. On copies of the image, the translator rejects a load and a `LEA` outside the image, a load in the headers, an 8-byte load 4 bytes before the end of `.data` and a write to `.rdata`. Without `--image-data`, four exports are rejected with the messages they had before, and an export named `bind_image` is rejected. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, every comparison and control passed. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_image_data$' -V`.

A function may use its stack frame. A forward pass over the control-flow graph gives RSP at every instruction as the entry RSP plus a constant. `PUSH` and `POP` of a nonvolatile register and `SUB` or `ADD RSP` by a multiple of 8 are the only changes accepted. The constants must agree where paths join, so a loop that pushes on every iteration is rejected, and every `RET` must find RSP at its entry value. Any other write of RSP (`MOV RSP, RAX`, `AND RSP, imm`, `SUB RSP, RCX`, `LEAVE`, `ENTER`) is rejected as a dynamic frame. Any use of RSP as a value (`MOV RAX, RSP`, `LEA RAX, [RSP+8]`, an RBP frame pointer set from RSP) is rejected because the frame's address would escape, and so are `PUSH` and `POP` of other operands, which MSVC did not emit in the call-free functions measured. The generated function declares one zero-initialized byte array per call. It runs from the lowest RSP the function reaches to the end of the caller's 32-byte home space at entry RSP + 40, and each RSP-based operand becomes a `load` or `store` at a constant index into it, so RSP is never a C++ value. The array is at most 4,096 bytes: MSVC calls `__chkstk` before it allocates a page or more, and that call's target has no unwind entry, so it is rejected (see below). It is declared `alignas(16)` and starts at the lowest RSP rounded down to 8 mod 16, so each byte has the address residue modulo 16 of the stack byte it stands for, given the entry alignment the ABI guarantees. Every access goes through `memcpy` and the address never escapes, so this alignment cannot change a value; what the translator checks is the exact offset of every access, the bound and that every RSP change is a multiple of 8. An access below the current RSP (Windows x64 has no red zone), of the return address, or at or above entry RSP + 40 (the fifth argument and later) is rejected. A second forward pass follows every frame byte on every path. A read needs each byte written on every path, so home space, which the caller only reserves, must be written before it is read. Bytes are tracked one by one, so an 8-byte store can be read back as its upper 4 bytes and stores of 4, 2, 1 and 1 bytes as one 8-byte value. When `ADD RSP` or `POP` raises RSP, the bytes it leaves below RSP become unwritten again, after a `POP` has read its source: Windows may change memory below RSP, so a later allocation of the same bytes must write them before it reads them, and a register saved there can no longer be restored from them. RBX, RBP, RSI, RDI and R12 to R15 may be used if every `RET` is reached with each one holding its entry value again on every path. The same pass records which register and which frame bytes hold the entry value of which register, through `PUSH` and `POP` and through a 64-bit `MOV` to and from the frame. MSVC uses both, the second to save into home space before it allocates. An entry value is not an argument: it may be saved, and its saved bytes may only be restored into the same register. Any other read of it, including a read after the restore, is rejected. The frame bytes of a saved register hold the generated local's value at the save, which is its initial 0, and are never read as data. A register byte that may hold a guest address is never defined, as above, and a store reads its source register, so a guest address cannot be stored in the frame and reloaded. The generated function is ordinary C++ that the host compiler gives its own frame. Exception unwinding through the original frame layout is not reproduced, and memory outside the frame is accessed as before.

`cpu_aot_stack_frames` builds [`frame.asm`](../tests/cpu/frame.asm), with the unwind directives each prologue needs, and translates nine exports. `push_loop` pushes and pops RBX and RSI and uses them in a loop. `spill_widths` allocates 24 bytes, stores 8, 4, 2 and 1 bytes, reads them back in overlapping pieces, adds to a slot in place, and uses `SETB` into the frame and `CMOVL` from it. `home_save` has the MSVC `/O2` shape: it saves RBX and RSI into home space with `MOV [RSP+8], RBX` and `MOV [RSP+10h], RSI` before `PUSH RDI` and `SUB RSP, 20h`, restores them with `MOV` on each of two returns, and carries the unwind codes MSVC writes for that prologue. `home_args` is the `cl /Od` output for a small C function that stores its arguments to home space, writes a local on both sides of a branch and reloads it. `two_paths` pushes RSI on one path and RDI on the other, rejoins at the same depth, and returns early on a third path after restoring RBX. `home_spill` spills two arguments into home space with no allocation of its own, and `loop_counter` keeps a counter and an accumulator in the frame across a loop's back edge. `realloc_slot` releases its allocation with `ADD RSP`, allocates the same bytes again and writes the slot before reading it. `frame_bound`, whose frame array is exactly 4,096 bytes, is translated and compiled but never called. Through the register probe, each original and translated export is compared with a Python reference over the direct-call input pairs, on the return value, the guarded trace, RBX and RSI, and every path of each export is taken. The probe shows that the original saves and restores RBX and RSI and that the translation leaves the caller's RBX and RSI intact. It does not show that the translation reproduces the original's save and restore: the generated locals are not the physical registers, and the host compiler saves and restores those itself. The test checks that the built source is the translation of the export list, that each frame array has the expected size, and that `rsp` appears nowhere in the generated C++. Thirty-two controls must be rejected. Twenty-six are copies of the image with one or two instructions replaced by others of the same length: an unbalanced `POP`, two paths that join at different depths, a loop that pushes on every iteration, a nonvolatile register not restored on one path, restored from another register's slot by `MOV` or by `POP`, or loaded into RAX from its save slot, a local written on one path only, home space never written, a 4-byte store removed under a later 8-byte read, a loop counter written only on the back edge, a stack argument at entry RSP + 40, a read and a write of the return address, `LEA RAX, [RSP+8]`, `MOV RAX, RSP`, `SUB RSP, RCX`, `SUB` and `ADD RSP` by 14h, `ADD RSP` at entry, a `JAE` left reading the flags of `SUB RSP`, an access below RSP, a frame of 4,112 bytes, `realloc_slot` without the store into its second allocation, `ADD RBX, RCX` on the caller's RBX, `PUSH RAX` and `PUSH 5`. The six others are exports rejected as written: a saved RBX whose slot is released by `ADD RSP` and allocated again before `POP RBX`; a slot that `POP` read and released, allocated again and read without a new store; a RIP-relative address spilled to the frame and reloaded, refused at the store as a read of an undefined register; RBX saved twice and reloaded from the 8 bytes that straddle the two copies; RBX pushed as data after only BL was written; and a register read after `POP` restored it. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 1,992 calls per side matched and every control was rejected. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_stack_frames$' -V`.

The same translator follows direct calls and tail jumps to other functions of the image ([#317](https://github.com/R0Y00/Xbox2Windows/issues/317), step 2). A `CALL rel32`, or a direct `JMP` that leaves the function's owned ranges, must target a function entry of this image: the first byte of a verified primary runtime function (an unchained `.pdata` record with version 1 or 2 unwind data and a non-empty interval in executable code), or a non-forwarded code export in an executable section that lies outside every runtime-function interval. The start of a chained secondary range, an interior label (exported or not), a record start with any other unwind header, a forwarded export and a data export are not entries, and the message says which of these the target is. Each callee is translated once into the same source, as a static function named `fn_<rva>` that takes RCX, RDX, R8 and R9, and is defined before its first caller. A call to the caller's own entry is rejected as self-recursion and any longer cycle as mutual recursion. A caller is rejected when any callee is, and the message names the callee's RVA and its reason. A callee records how many low bytes of each argument register it may read while that register may still hold its entry value, and the caller must have those bytes defined at the call. After a call, RAX has the callee's return width of defined bytes. RCX, RDX and R8 to R11, the flags and the callee's 32-byte home space at RSP are undefined. The return address the `CALL` pushes and the callee's own frame lie below RSP, where the frame proof already holds every byte unwritten, because the bytes that `ADD RSP` and `POP` leave below RSP become unwritten; so they stay undefined if RSP later falls back over them. Nonvolatile registers keep their state, because each callee is itself checked to restore every one, and so do the other frame bytes, including saves by `MOV` into the caller's own home space above entry RSP, which the callee's home space cannot reach. RSP must be 8 mod 16 from entry at a call, so the native call is aligned, and the callee's home space must lie below the return address. A tail jump is checked like a `RET`, with RSP at its entry value and every nonvolatile register restored, and contributes the callee's return width. The callee's frame is its own array, so no frame address crosses a call. A frame address passed to a callee (`LEA RCX, [RSP+20h]`), stack arguments, indirect calls, callees with no reachable normal return, import thunks, and targets with no unwind entry or export (such as leaves that MSVC emits without `.pdata`) unless the invoker binds them as described below, stay rejected.

`cpu_aot_direct_calls` builds [`calls.asm`](../tests/cpu/calls.asm) and translates seven exports, which reach three unexported callees placed below them. `call_leaf` calls a leaf from a frame and keeps the trace pointer in RBX across the call. `chain3` calls `mid_step`, which spills RDX into its own frame above the callee's home space, calls the same leaf and reloads it, so the leaf is shared. `narrow_call` calls a callee that defines only AL and widens it with `MOVZX`. `narrow_tail` tail-jumps to that callee and reports width 1, `tail_call` tail-jumps to the leaf, and `tail_after_frame` pops its frame before tail-jumping to `mid_step`. `home_save_call` has the MSVC `/O2` save shapes around a call: RBX saved by `MOV` into its own home space before `SUB RSP, 28h`, and RSI saved by `MOV` into the allocation above the callee's home space. Through the register probe, the original must match a Python reference, and the translation must match the original on the defined return bytes, the guarded trace, RBX and RSI over 545 input pairs per export. The test checks that each callee is defined exactly once and that the reported return widths are 8 except for `narrow_tail`. Twenty copies must be rejected with their stated reason: a call to the caller's own entry, a call from `mid_step` back to `chain3`, calls into an instruction interior and to an instruction boundary that is not an entry, a target outside the image and one in `.rdata`, reads of RCX, R8 and the full RAX after a call, a second call to the leaf whose RCX and RDX the first call left undefined, a `SETE` after a call, a stack-argument read in a callee and an unsupported instruction two levels down, a spill and a `MOV` save of RSI moved into the callee's home space and reloaded after the call, a call at entry RSP - 0x30 and one at entry RSP - 0x8, a tail jump with a frame still allocated, a tail jump with RBX not restored, and, as built, `below_call`, which stores a slot at entry RSP - 0x40, raises RSP for a call whose return address overwrites it, and reloads it after RSP falls back (natively it returns the return address). Removing the volatile clobber accepts the RCX control, keeping RAX fully defined after a call accepts the narrow control, keeping the callee's home space defined accepts the spill control, dropping the callees' argument demands accepts the second-call control, translating a shared callee again for each caller emits it five times (and the C++ fails with `C2084`), and naming the wrong callee at a call fails `chain3`. On Windows 11 build 26200 with MSVC 19.50 and ML64, all 3,815 calls matched and every control was rejected. The C++ generated for the existing jump-table, `addr32`, entry-order, narrow, fourth-argument and frame fixtures is unchanged. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_direct_calls$' -V`.

`cpu_aot_call_entries` builds [`call_entries.asm`](../tests/cpu/call_entries.asm), whose `.pdata` and `.xdata` are written explicitly so the image has a primary function continued by a chained secondary range. It also exports an interior label of a primary function, a `.data` variable as `DATA` and a forwarder to `kernel32.GetTickCount`. The test reads the image's own records and checks that the translator's entries are exactly the four primary runtime functions. A framed `CALL` and a tail `JMP` to the primary leaf translate and match the original and a Python reference over 249 input pairs each. Eight retargeted copies must be rejected with the reason that names the distinction: the call retargeted to the exported interior label, the chained range's start, the data export and the forwarder; the tail jump retargeted to the interior label and the chained start; and the leaf's unwind header patched from version 1 to version 3, for both the call and the tail jump. The translator before this rule refused the first six later and for unrelated reasons (`no unique function interval`, `not in an executable section`, `edge outside owned ranges`), and accepted the last two. Removing the version check, or accepting an export at the start of an unverified record, accepts the version controls. Counting interior exports or chained ranges as entries, or classifying forwarders and data exports as code, fails the check of the entry table. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_call_entries$' -V`.

On a Release (`/O2`) build of this change with MSVC 19.50, every `.pdata` entry of the 96 readable non-generated images was translated with four arguments, each unique function body once. Of 2,343 unique functions, 78 translated with the frame translator before this change and 97 after it, and none was lost. Of the 19 new ones, 8 are in `calls.asm`, 2 in `call_entries.asm`, 8 are callers in the other MASM call fixtures and 1 is MSVC output, a tail jump in `cpu_life_a.dll`, which is translated but not run. The most common first refusals are now a call target with no unwind entry or export (806 functions), mostly MSVC leaves without `.pdata` and import thunks, and a refused callee (299). Among refused callees, the innermost reason is an indirect transfer (`unsupported control flow`) in 186 and again a target with no unwind entry in 102. A frame address passed to a callee is the first refusal of 32.

The fixed-frame proof also accepts `MOV RBP, RSP` and `LEA RBP, [RSP+disp]` as frame anchors. Both stack bases address the same checked frame array. Incoming paths must agree on the anchor at each join. An anchored RBP is usable only for a bounded stack access or register restoration. An ordinary import can receive a pointer into the frame under the explicit output contract below. The `anchored_frame` export in `cpu_aot_stack_frames` compares both paths of a shared slot written through RBP and updated through RSP. Its controls change the anchor on one path, turn a load into a frame-address LEA, and read an unwritten slot. `LEAVE` with the 64-bit operand size is accepted as the epilogue of such a frame: only where the anchor is proven on every path, not below RSP, and low enough that RSP stays at or below entry RSP, it sets RSP to the anchor and pops RBP from it, and the frame proof releases every byte from the old RSP through anchor + 8, the pop slot included. It writes no flags. The pop reuses the saved-register proof, so RBP counts as restored only when the slot still holds the saved RBP. `ENTER` and a separate `MOV RSP, RBP` stay rejected as dynamic frames. `cpu_aot_leave` ([source](../tests/cpu/leave.asm)) compares three anchored frames with native execution: `MOV RBP, RSP` with flags that cross `LEAVE`, `LEA RBP, [RSP+20h]`, and a join where both paths keep the anchor. It also checks that the translation reloads RBP from the slot `PUSH RBP` wrote. The register probe samples RBX and RSI, so the RBP restore is shown by that proof rather than by execution. Its controls remove the anchor, let the anchors differ at a join, leave the anchor slot unwritten, overwrite the saved RBP with ordinary data, read a local that `LEAVE` released, put the anchor below RSP, put it where RSP would rise above entry RSP, and use `MOV RSP, RBP`, `ENTER`, and 16-bit `LEAVE`.

An ordinary import binding can use `--stack-output ARG WIDTH` for one synchronous output argument. `ARG` is its 1-based position. The provider must write exactly `WIDTH` bytes before returning and must neither read their initial contents nor retain their address. The translator proves that a single `LEA` from RSP or an anchored RBP supplies the argument in the same basic block. The slot must be naturally aligned, inside the verified frame, and outside the callee's home space. The call initializes only the declared bytes. Tail calls and other uses of frame pointers remain outside the profile. Import bindings accept zero through four register arguments and can declare zero, one, two, four or eight defined return bytes.

`cpu_aot_stack_output` compares the native and translated results of an RBP-relative output passed to a provider that returns a 32-bit status and an RSP-relative output passed to a provider with a void result. Its rejection cases cover misalignment, overlap with callee home space, a branch that bypasses the `LEA`, a wider read than the provider wrote, an argument index outside the binding, and a missing output contract. Run `ctest --preset release --no-tests=error -R '^cpu_aot_stack_output$' -V`.

A target with no unwind entry and no export is refused by default. The message gives the target RVA and says that a range binding is needed. A missing `.pdata` entry, clean decoding, no RSP change and a path to `RET` together do not show that an address starts a distinct function: an aligned interior label has all four. The invoker can supply that evidence with `--bind-ranges MANIFEST` (or `RANGES` in the CMake function `cpu_translation`). The manifest is JSON with the image's SHA-256 and a `[start, end)` RVA pair per function, taken from trusted evidence for that exact build. [`tools/range_manifest.py`](../tools/range_manifest.py) writes one from the source labels that an MSVC `/MAP` file lists, a function's symbol and its `_end` label. The translator checks:
- that the hash matches the image;
- that each range lies in the file-backed bytes of an executable section;
- that no range overlaps another binding or any runtime-function interval.

A call or tail jump may enter a bound range only at its start. An interior target, an instruction interior included, is refused with the range it lies in. The bound callee is walked within its range alone: an edge that leaves it, reaches another function's unwind interval or another bound range, or enters unbound code is refused, and the refusal names which of these it is. The callee may not move RSP or call, may not be an import thunk, and is subject to the recursion rules above. PDB function symbols are another possible source of the same evidence and are not read yet.

`cpu_aot_bound_callees` builds [`bare_calls.asm`](../tests/cpu/bare_calls.asm) with `/MAP`, whose two leaves are declared without `FRAME`, so the image has no `.pdata` entry for them, and are not exported. The build writes the manifest from the map and translates `call_bare`, `twice_bare` (both calling the same leaf from a frame) and `tail_bare` (a tail jump to a leaf that defines only AL) with it. Through the register probe, the translation matches the original and a Python reference on the defined return bytes, the guarded trace, RBX and RSI over 545 input pairs per export. Without the manifest, the same image's call and tail jump are refused with the target RVA and the binding needed. `bare_mix_inner`, an aligned interior label from which `MOV`, `SUB` and `RET` translate, does translate when it is bound as its own range. Sixteen controls must be rejected with their stated reason:
- the manifest applied to a copy whose `TimeDateStamp` differs;
- a call to that interior label and to an instruction interior, with the leaf's range bound;
- a tail jump into the other leaf's interior;
- a range in `.rdata`, a range past the end of `.text`, a range overlapping a framed function and two overlapping ranges;
- a range that ends before the leaf's `RET`;
- the second leaf jumping into a framed function, into the first leaf's range and into unbound code;
- a bound `JMP [RIP+slot]` thunk;
- the leaf patched to call, to `PUSH` and `POP`, and to call itself.

Skipping the hash check, treating any address in a range as its start, walking a bound callee over its whole section, or allowing a range to overlap a runtime function each accepts its control. Removing the check against overlapping bindings, the executable-bounds check, the thunk check or the leaf rule leaves its control rejected by a later check (the other range as a foreign interval, the unbound target, `unsupported control flow`, the call-site alignment), so the test fails on the message. On Windows 11 build 26200 with MSVC 19.50 and ML64, all 1,635 calls matched and every control was rejected. The C++ generated for every earlier fixture of this translator is unchanged. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_bound_callees$' -V`.

The corpus walk above passes no manifest, so its counts are unchanged: 97 of 2,346 unique functions translate, before and after this option, and none is lost or gained. Unbound targets are counted only as candidates for a binding. Without one, 872 functions are refused first at a target that needs a binding (808 calls and 64 tail jumps); counting a refused callee's innermost reason, 974. They reach 421 distinct target RVAs, 129 of them shaped like an import thunk, which a binding would not admit.

One-operand `MUL` and `IMUL`, `DIV` and `IDIV` and the sign extensions `CBW`, `CWDE`, `CDQE`, `CDQ` and `CQO` read and write RAX and RDX without naming them. MSVC emits them for 64-bit multiply-high, division and modulo. Operand 0 of such a multiply or divide is a source. The translation copies every input into a `const` temporary before it writes RAX or RDX, because `mul rdx`, or a source addressed through RDX, reads a register that is also an output. A sign extension is computed in `std::uint64_t` as `(x ^ s) - s`, where `s` is the top bit of the source, and `CDQ` and `CQO` as `0 - (x >> (bits - 1))`. Unsigned arithmetic wraps by definition, so these forms involve no signed conversion and no shift of a negative value. Factors of up to 32 bits are widened to `std::uint64_t`, sign-extended for `IMUL`. Their product needs at most 64 bits, so the wrapped product is exact. An 8-bit multiply writes AX, and a 16-bit multiply merges into AX and DX; the byte-wise definedness pass counts these as 16-bit writes. 64-bit products use `_umul128` and `_mul128` from `<intrin.h>`, which the generated source now includes. Quotients use `_udiv64`, `_div64`, `_udiv128` and `_div128`. These execute the hardware `DIV` or `IDIV`, so a zero divisor or a quotient that does not fit raises the same divide error as in the original, and the translation emits no C++ `/` or `%`. The signed intrinsics take signed parameters. Converting a register value of 2^63 or more to them is implementation-defined in C++14 and C++17, which MSVC defines as wrapping, and defined as wrapping in C++20; the translator already relies on this conversion for `MOVSX`, `SAR` and the signed conditions. With MSVC 14.52 `cl /O2 /FAs`, each divide of the fixture compiles to one `div` or `idiv` and each 64-bit multiply to one `mul` or `imul`, with no call and no added check. The Debug build that CTest uses also keeps them inline. Reaching a divide raises its error even when both results are unused: the translation stores the quotient to a `volatile` local, so the compiler must perform the divide when the function overwrites RAX and RDX without reading them. Without that store, an `/O2` build removed such a divide, and a divide by zero returned in the translation and faulted in the original. `cpu_aot_dead_divides` checks this on [`dead_div.asm`](../tests/cpu/dead_div.asm), whose exports store 1 to the trace before the divide and 2 after it, then overwrite both result registers: 32-bit `DIV` and `IDIV`, a 64-bit `DIV` of RDX:RAX, a 64-bit `IDIV` after `CQO`, and `div_skip`, which branches around its divide when the divisor is zero. The translation is built twice, by the project's Debug build and by a separate `cl /O2` command, and a small C++ helper calls each function inside `__try` and returns the structured exception code. For zero divisors, quotients that do not fit, valid divides and the skipped divide, both builds must give the code, the return value and the trace that the original gives. The original is the oracle; the test itself only requires that an input expected to fault yields one of the two integer divide codes and leaves the second trace word unwritten. Inputs that do not fault are also compared through the register probe. 8-bit divides, which write AH, and 16-bit divides, which no intrinsic executes, are rejected. `MUL`, `IMUL`, `DIV` and `IDIV` set flags that the flag model does not describe, so a flag consumer after one of them is rejected. The Intel SDM lists "Flags Affected: None" for the sign extensions, so they may lie between a producer and its consumer. The constant pass for table bases and the value numbering for table indices treat these instructions as writes of RAX and RDX.

`cpu_aot_wide_arithmetic` builds [`wide_arith.asm`](../tests/cpu/wide_arith.asm) and translates thirteen exports: `div32` (EDX:EAX built from RCX, divided by EDX moved to R9D), `idiv32` (`CDQ`, then `IDIV`), `div64` (`XOR EDX`, then `DIV`), `udiv128` (RCX:RDX divided by a fixed divisor), `idiv64` (`CQO`, then `IDIV` of a memory operand), `mul64` (`mul rdx`), `imul64_1` (one-operand `IMUL` of a memory operand addressed through RDX, which it overwrites), `mul32` and `imul32`, `mul8` and `mul16` (8- and 16-bit `MUL` and `IMUL`), `widen` (`CBW`, `CWDE` and `CDQE`, alone and chained) and `sign_dx` (`CDQ` and `CQO` between a `CMP` and its `SETL`). Through the register probe, each original and translated export is compared with a Python reference over the direct-call input pairs that do not fault, plus the largest quotients that fit, the minimum dividends and inputs across the sign boundaries. The reference rounds signed quotients toward zero itself, since Python's `//` rounds toward minus infinity. RAX, the guarded trace and RBX/RSI must match. Eight faulting inputs, zero divisors, unsigned quotients that do not fit, -2^31 / -1 and -2^63 / -1, must raise the same `OSError` through ctypes in the original and in the translation, and leave the same trace. The test accepts `exception: integer divide by zero` or `exception: integer overflow`, provided both sides report the same. Eleven controls must be rejected: `div r9d` patched to `div r9b` and to `div cx`, a `JE` right after `DIV`, `CDQ` patched to `CLC` between `CMP` and `SETL`, a `MUL` and a `CDQ` whose RAX is no longer written (rejected at the `MUL` and at the `CDQ`), `JC` after `MUL`, two jump tables whose base or index register a `MUL` or `CQO` overwrites before the dispatch, a store of RDX after an 8-bit `MUL`, which leaves RDX holding a guest address from a RIP-relative `LEA`, and the same function with `div ecx`, which reads that EDX. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 2,929 inputs matched, all eleven controls were rejected, and all eight faulting inputs raised `exception: integer divide by zero` on both sides; Wine reports every divide error this way. Whether Windows reports the overflow cases as `integer overflow` was not observed here. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_wide_arithmetic$' -V`.
MSVC ends a function whose last action is a call to an imported function with `jmp qword ptr [rip+slot]`, a tail jump through the import address table (IAT). The jump-table translator accepts it only through an explicit binding, in the form the [import-call translator](#calling-another-dll-through-a-named-pe-import) uses: `--bind PROVIDER_DLL SYMBOL` names the provider and the slot's import by name or by `#ordinal` (with `--link-name` for an ordinal), `--import-arguments N` gives the number of integer arguments the import takes (3 or 4), and `--return-bytes N` gives the number of guaranteed low RAX bytes of its return (0, 1, 2 or 8; 0 is a normal return with no guaranteed result bits). `cpu_translation` forwards them from `PROVIDER`, `SYMBOL`, `LINK_NAME`, `IMPORT_ARGUMENTS` and `RETURN_BYTES`, and takes `NAME` so that one image can be translated with two bindings. Like an entry's own argument count (`name:4`), these numbers are chosen by whoever invokes the translator; none of them is derived from the machine code or from the provider. The entry's count and the import's count are separate choices. The decoder accepts an indirect `JMP` through a RIP-relative 64-bit memory operand without a segment prefix (`FF 25`, or `48 FF 25` with the REX.W prefix that MSVC also emits), and the jump ends the path; the 67h (EIP-relative) form, a segment prefix and a far jump stay rejected. As for import calls, the slot must hold a unique ordinary import whose DLL name equals the provider's file name and whose name or ordinal matches the binding, and the provider must export it as executable code. One binding covers one IAT slot for all exports of a run; several bindings, one per slot, are described with `cpu_aot_two_slots` below. A tail jump without a binding, a second distinct slot under a single binding, a binding that no reachable call or tail jump uses and a slot that is not the bound import are rejected. The generated C++ declares the import with exactly N `word` parameters and replaces the jump with `return import(...);`, passing RCX, RDX, R8 and, for four, R9. Each passed register must be fully defined at the jump and hold no guest address, otherwise the translation is rejected with `undefined register`. A four-argument entry therefore passes on its incoming R9, and a three-argument entry can call a four-argument import only after writing all of R9 itself. The jump ends the path, so no register or flag clobber after the call needs modelling. A tail jump is a normal return of the function: it contributes the binding's number of guaranteed bytes to the return width, which is the conservative minimum over tail jumps and reachable `RET`s and is `None` only when neither is reachable. A function whose only normal returns are tail jumps therefore has a width. For a function with a tail jump the generated comment always states the width and both of its sources, for example `// RAX: 2 low byte(s) at every reachable normal return: 2 defined at every reachable RET, 8 guaranteed after every tail jump to mix4 as the binding states` (`no RET is reachable` replaces the middle part when there is no `RET`), and the declaration of the import is preceded by `// mix4: bound import with 4 integer argument(s) and 8 guaranteed low RAX byte(s), as the binding states; neither number is read from the provider`. For such a function the CLI prints the same width text after `RAX: `.

The bound import must take three or four integer arguments and return in RAX. Stack arguments (the fifth and later) and floating-point arguments are not forwarded, and the translator cannot check the argument count or the guaranteed bytes against the provider: a wrong binding gives wrong code. The import's name must not be one that the generated prelude declares through `<cstdlib>` or `<cstring>`: with `free`, `memset` or `strlen`, MSVC 14.52 rejects the declaration with error C2733 (a second declaration of a function with C linkage). Binding C runtime functions is left for a later change.

`cpu_aot_import_tail_jump` builds [`tail_import.asm`](../tests/cpu/tail_import.asm) three times, each linked against one provider built from [`tail_provider.cpp`](../tests/cpu/tail_provider.cpp): with named exports, with the same exports returning one more, and with ordinal-only exports (`#3` and `#4`, bound with the link names `mix3` and `mix4`). `mix3(first, second, output)` and `mix4(first, second, output, fourth)` record their integer arguments in three output words and return a value that depends on each of them. Each caller is translated twice. `tail_mix`, a three-argument entry, changes RCX and RDX and jumps to `mix3`, bound with three arguments. `tail_choose,tail_pass4:4` is bound to `mix4` with four. `tail_choose`, a three-argument entry, either writes R9 and jumps to `mix4`, or stores a word, writes only AX and returns. `tail_pass4`, a four-argument entry, changes RDX and jumps to `mix4` with its incoming R9; its jump carries a REX.W prefix (`DB 48h` before the `JMP`). Both bindings state 8 guaranteed bytes, so `tail_mix` and `tail_pass4` report width 8 and `tail_choose` width 2, the bytes defined at its `RET`. The test checks in each caller that every tail jump is `FF 25` (`48 FF 25` in `tail_pass4`) with a displacement to its own import's IAT slot, and in the ordinal caller that both imports carry the ordinal flag. It checks that the built sources are the translations of those lists, that each declaration and call carry the binding's number of arguments and each signature the entry's own number, and that R9 is a local of `tail_choose` and a parameter of `tail_pass4`. It translates `tail_choose` with 8, 1 and 0 stated bytes and `tail_mix` with 8, 2 and 0 stated bytes, and inspects the widths and comments: the width is the lower bound over both kinds of exit, and a function without a reachable `RET` gets a width rather than `None`. Every native and translated export is compared with a Python reference over the direct-call input pairs, the three-argument entries through `probe` and `tail_pass4` through `probe4` with six fourth arguments, which change its result on every pair. The return value is compared whole after a tail jump and in its two defined bytes after the `RET`, together with the guarded trace, RBX and RSI. The translations bound to the altered provider must follow the altered reference and differ from the named ones only in the return value. Thirteen bindings or copies must be rejected inside the translator: no binding; the symbol `mix4` for the slot of `mix3`; the altered provider's file for the named caller; the name `mix3` for the ordinal-only slot; the jump's displacement patched to the IAT's terminating null entry; the jump patched to a far jump (`FF 2D`); an EIP-relative (`67 FF 25`) and an FS-relative (`64 FF 25`) tail jump in place of the `LEA` of `tail_guest_arg`; `tail_guest_arg` itself, which passes the address of `tail_mix` in RCX; `tail_pass4` translated as a three-argument entry with the four-argument binding, where R9 is not written; `tail_choose` with its R9 writes patched to `mov r9b, cl` and `NOP`s, so that R9 is only partly written at the jump; `tail_choose` with its AX writes patched to a tail jump through the slot of `mix3`, a second slot; and `tail_choose` with its `JAE` patched to `JMP`, so that no tail jump is reachable and the binding is unused. The test asserts no loader error codes. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 5,976 calls per side matched, 5,589 of them through a tail jump; the altered provider changed the return of all 249 `tail_mix` calls, of the 120 `tail_choose` calls that take the tail jump and of all 1,494 `tail_pass4` calls, and all thirteen controls were rejected. The providers' argument counts and return widths are known from their source, so the test does not show how a binding's numbers would be established for an import without source. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_import_tail_jump$' -V`.

An ordinary call of a bound import is translated through the binding of its IAT slot, which holds for all functions of a run. Three forms reach the slot: `call qword ptr [rip+slot]` (`FF 15`), a direct `CALL rel32` of an import thunk, and a direct `JMP` to one. An import thunk is the `jmp qword ptr [rip+slot]` (`FF 25 disp32`) that the linker adds when the caller declares the import without `__imp_`; it counts only at a target outside every unwind interval and range binding. In each form the slot must be an IAT slot of the ordinary import directory; otherwise the instruction keeps its earlier refusal (`unsupported control flow`, or a call target that is not a function entry). The binding then checks the slot as for a tail jump: a slot that holds another import is rejected by name, and a run without a binding is rejected with `no binding for the call ...` or `no binding for the tail jump ...`. A jump to a thunk becomes the tail jump through its slot described above. The slot's eight bytes must also lie in one section with `IMAGE_SCN_MEM_READ`, a check that now applies to tail jumps too, because the translation calls the import that the binding names instead of reading the slot. The ordinary import directory and readable section flag establish the slot identity and expected initial mapping. During execution of the original and its translation, the caller keeps the image and bound target valid and ensures that the slot stays readable and contains the declared target. Delay-loaded imports, rebinding and page protection changes are outside the explicit binding contract. The call becomes `rax = import(rcx, rdx, r8);` (with `r9` for four arguments) under the rules of a direct call: RSP is 8 mod 16 from entry at the call, the 32 bytes of home space lie below the return address, and each register of the binding's argument count must be fully defined, which also excludes a guest address (both are rejected as `callee argument ... is undefined`). After the call RAX holds the binding's number of guaranteed low bytes (none for 0); RCX, RDX, R8 to R11, the flags and the 32 bytes at RSP are undefined; the nonvolatile registers and the rest of the frame keep their state. That last part is a term of the binding: the import is taken to follow the Windows x64 calling convention, which the translator cannot check against a provider. A call adds nothing to the return width except through the RAX bytes defined at a later `RET`. As for any call, a function without unwind data may not call the import, and a callee reached through the call graph may itself call or tail-jump to it. The C runtime names of the previous paragraph cannot be bound for a call either.

`cpu_aot_import_calls` builds [`import_calls.asm`](../tests/cpu/import_calls.asm) twice, linked against the named and the altered provider of `cpu_aot_import_tail_jump`, and translates each caller three times; every export takes three arguments. `call_keep,call_twice,outer_call,pass_result,read_then_call,call_then_read` is bound to `mix3` with three arguments and 8 guaranteed bytes. `call_keep` keeps x in RBX and y in a frame slot above the import's home space across `call [__imp_mix3]` and combines both with the result; `call_twice` calls the import twice and writes RCX, RDX and R8 again between the calls; `outer_call` calls the unexported `inner_call`, which calls the import; `pass_result` returns the import's RAX. `low_result`, which reads only AL of the result, is bound to `mix3` with 1 guaranteed byte. `thunk_call,thunk_tail,outer_tail` is bound to `mix4` with four arguments: `thunk_call` calls the import thunk with a direct `CALL`, `thunk_tail` jumps to it, and `outer_tail` calls `inner_tail`, which tail-jumps through the IAT. The test checks that each call through the slot is `FF 15 disp32` and that the thunk is `FF 25 disp32` to the slot of `mix4`, outside every unwind interval and not exported. It checks that the built sources are the translations of those lists, with the import declared once and the expected numbers of calls, tail jumps and callees, and that `pass_result` reports widths 8, 2, 1 and 0 for those numbers of stated bytes and `thunk_tail` 8 and 0. Through the register probe, every export except the two that read memory at x is compared with a Python reference over the 249 direct-call input pairs on the whole return value, the guarded trace, RBX and RSI, for both providers and for the named caller's three translations built again with a separate `cl /O2` command. The altered provider's translations return a different value on every pair. `read_then_call` and `call_then_read` read the word at x before and after the call. Through `guarded_call`, with x readable, null and in an inaccessible page, the original and both builds of its translation give the same exception code, return value and trace: an access violation before the call leaves the trace unwritten, and one after the call leaves the arguments the import recorded. With a null trace pointer, `pass_result` raises the same access violation inside the import on all three. Thirty-four bindings or copies must be rejected inside the translator: no binding, for a call through the slot, a call and a tail jump to the thunk, and a call in an internal callee; `mix4` bound for the slot of `mix3`, and `mix3` for the thunk's slot; the second call of `call_twice` patched to the slot of `mix4`; the call of `call_keep` patched to `mov eax, 1`, which leaves the binding unused; the section that holds the IAT without `IMAGE_SCN_MEM_READ`; reads of RCX, RDX, R8, R9, R10 and R11 after the call; a `SETE` after the call; the full RAX read with 1 guaranteed byte and AL read with 0; RDX not written again before the second call; R9 not written before the call of the four-argument import; a guest address in RCX; a value spilled into the import's home space and reloaded after the call; RSP misaligned at the call; the call of the thunk retargeted to the thunk's second byte, to the `FF 25` tail jump inside the unwind interval of `inner_tail`, and to an `FF 25` through the IAT's terminating null entry, which is not an import slot; a range-bound callee without unwind data that calls the import; a call through the IAT's terminating entry; `call qword ptr [rax]`, `call rax`, a far call through the slot (`FF 1D`), and EIP-relative (`67 FF 15`) and FS-relative (`64 FF 15`) calls; and a jump-table base loaded into R10 before the call and used after it. The test asserts no loader error codes. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 5,976 probe calls per side and the nine fault cases matched, and all 34 controls were rejected. The providers' argument counts and return widths are known from their source, so the test does not show how a binding's numbers would be established for an import without source. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_import_calls$' -V`.

Several imports can be bound in one run by repeating `--bind`. With two or more, the `--link-name`, `--import-arguments` and `--return-bytes` after each `--bind` belong to it; an option before the first `--bind`, an option given twice for one `--bind` and a `--bind` without its numbers are command-line errors. A single `--bind` still takes its options in any position. `cpu_translation` forwards a second binding from `SECOND_PROVIDER`, `SECOND_SYMBOL`, `SECOND_LINK_NAME`, `SECOND_IMPORT_ARGUMENTS` and `SECOND_RETURN_BYTES`. Each binding must name a distinct ordinary import of the image, by DLL name and name or `#ordinal`, and binds the slot that holds it; each reached slot takes its own binding's argument count and guaranteed bytes. Two bindings of one import, a binding that names no import of the image, a reached slot that no binding names, a binding that no call or tail jump uses and two bindings that would declare the same C name are rejected. The imports are declared in IAT slot order, so the order of the bindings does not change the source. With one binding, the checks and messages above are unchanged.

`cpu_aot_two_slots` builds [`two_slots.asm`](../tests/cpu/two_slots.asm) four times, linked against a `mix3` provider of `cpu_aot_import_tail_jump` and a `fold4(first, second, output, fourth)` provider built from [`slot_provider.cpp`](../tests/cpu/slot_provider.cpp), which records its first, second and fourth argument and returns 16 bits: both named, `mix3` altered, `fold4` altered, and `mix3` imported by ordinal `#3` with the link name `mix3`. Each caller is translated once with `mix3` bound with three arguments and 8 guaranteed bytes and `fold4` with four and 2. `route2` selects through an MSVC jump table: an ordinary call through the slot of `mix3`, a tail jump through the slot of `fold4` after the epilogue, a direct call of the `fold4` thunk that reads only AX, and a path without an import; its return width is 2, from the tail jump. `call_both` calls both imports on one path and `call_second` only `fold4`. The test checks every site by its encoding and slot, the built sources (`rax = mix3(rcx, rdx, r8);` and `fold4(rcx, rdx, r8, r9)`, each import declared once with its own number of parameters) and `route2` with 8 and 2 stated bytes for `fold4`. Through the register probe, the three exports of each caller are compared with a Python reference over the 249 direct-call input pairs on the whole return value, the guarded trace, RBX and RSI. The altered `mix3` changes only the `route2` path through its slot and the altered `fold4` only the two through its slot; the ordinal caller returns what the named caller returns. Ten bindings must be rejected: only the `mix3` binding; two bindings of `mix3`; `fold4` bound to the `mix3` provider and as `fold3`; `call_second` with an unused `mix3` binding; 2 bytes stated for `mix3` and 1 for `fold4` where more of RAX is read; `mix3` bound with four arguments where R9 is undefined; `#3` without its link name, and with the link name `fold4`. Six command lines run the translator: the two `--bind` groups in reverse order give the same source, a single `--bind` after its options translates as before, and the options swapped between the groups, an option before the first group, an option given twice for one group and a group without `--return-bytes` are refused. Before the change, the two-group command failed with `unbound import cpu_tail_named_provider.dll!mix3 at IAT slot RVA 0x2010`, because only the last `--bind` counted, and the same translation with the `mix3` binding alone with `call or tail jump through a second IAT slot RVA 0x2000`. With Windows 10 Pro build 19045.7846, MSVC 19.52.37029 and Python 3.13.7, 2,988 probe calls per side matched and all fourteen controls were rejected with both presets; `cpu_aot_import_call`, `cpu_aot_import_calls` and `cpu_aot_import_tail_jump` passed unchanged. Delay imports and dynamically selected import targets are outside this test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_two_slots$' -V` and `ctest --preset release --no-tests=error -R '^cpu_aot_two_slots$' -V` after building that preset.

On the corpus of the earlier steps (MSVC 14.52 `/O2`, `/O1` and `/Od`, three entry arguments, no binding), the translated functions and their C++ are unchanged: 655, 651 and 400. For 143, 150 and 98 functions the first refusal now says that a binding is needed, instead of naming a target with no function entry or `unsupported control flow`. A binding names one import, so the gain was estimated with a script outside the repository. It binds every IAT slot that a function and its callees reach to the import that the import directory lists there, with 8 guaranteed bytes and without checking any provider. These are upper bounds that assume correct bindings. With three import arguments, 77, 69 and 13 more functions translate, all but one at `/O2` and at `/O1` through a single slot; with four, 2, 2 and 3. Most of the three-argument gains call `memset`, `free`, `memcpy`, `memcmp` or `strlen`, and as generated only 11, 11 and 5 of them compile. Declaring the import library's slot symbol instead, `extern "C" word (*__imp_memset)(word, word, word);`, and calling through it, all 159 sources compiled and linked against the C runtime import libraries; this change does not use that declaration. The functions whose single import only reads or writes memory through its arguments (`memset`, `memcpy`, `memcmp`, `strlen`, `memchr`: 49, 52 and 7 functions) were also run against the original with the inputs, arena and driver of the corpus differential tool, both sides calling the same runtime functions. Every input that completed on both sides matched (847, 1,000 and 176 inputs). Inputs that faulted on both sides compare nothing, memory outside the arena was not compared, and functions calling `free`, `VirtualFree`, `VirtualAlloc`, mutex or wait functions, or `SetLastError` were not run.

### Delay imports

`delay_route` imports `delayed_value` from `cpu_delay_provider.dll` by name and `delayed_ordinal` by ordinal 2 with `/DELAYLOAD`, and exports one caller for each. `cpu_native_delay` compares two paths: `LoadLibrary` plus the image's own delay helper, and `pe_map` with a resolver bound to the same provider. The mapped calls return the provider values, and IAT slot checks confirm both hold the `GetProcAddress` results before the calls. Binding the altered provider returns its values instead.

Windows leaves a delay IAT slot pointing at its thunk until the first call. `pe_map` resolves every delay descriptor at map time because it has no `LoadLibrary`, and the image's helper would bypass the host resolver. An absent provider therefore fails the map with `MAP_UNRESOLVED_IMPORT`, where Windows would map and fail on the first call. Descriptors must set `dlattrRva`; a descriptor without it stores virtual addresses and is rejected as unsupported.

Like the TLS and ordinary import tables, the delay-import directory is found by its address and `Size` is not used. The table is walked to the null descriptor; a copy with `Size` zero still binds, and a directory address of zero ignores a nonzero `Size`. Copies patched to clear `dlattrRva`, point the DLL name outside the image, zero the name table, point the IAT outside the image, or move the directory address outside the image are rejected before the provider is requested.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_delay$' -V
```

### Candidate programs for a named SGD5 effect

Supply the expected effect name, the group range, the program-array offset and the effect-record range. Use physical byte offsets in decimal or hexadecimal. Both ranges end at the first byte after the selected data.

```text
python -B tools/sgd5_effects.py archive.bin EFFECT --group GROUP_START GROUP_END --arrays ARRAY_OFFSET --record EFFECT_START EFFECT_END
ctest --preset dev --no-tests=error -R "^sgd5_effect_candidates$" -V
```

The report follows the effect's stored list order. Each entry gives its stored group, zero-based program index, name, numeric DXIL shader kind and decoded-container SHA-256. Check the DXIL kind when interpreting a group label. A stored VS group can contain a mesh shader (kind 13).

Two independently located local effects yielded 5 and 81 candidates across all groups. Their pixel lists contained 2 and 41 candidates respectively, and every pixel-container digest matched an independent decode. Identifying which candidate a draw uses still requires evidence of pass selection.

## Differential fuzz smoke test for the CPU translators

[#446](https://github.com/R0Y00/Xbox2Windows/issues/446) asks for native and translated functions to run side by side on generated inputs. `tools/fuzz_cpu.py` generates deterministic leaf functions for the shared `cpu_switch_aot` lifter. The default smoke generates 20 functions from seed `0xA902`: 8-, 16-, 32- and 64-bit ALU chains, shifts and rotates, CMP and TEST producers feeding SETcc, CMOVcc and Jcc consumers, a forward branch diamond, and a Jcc to its own fallthrough. Each function starts from RCX and RDX and writes to the R8 output buffer. The build writes one MASM DLL, translates all 20 exports into one C++ source, and builds that source at the preset's optimization and again at `/O2`. The test calls the native, default-translated and `/O2`-translated exports with 313 deterministic input pairs per function and compares RAX plus a guarded 256-byte output buffer. It performs 12,520 comparisons in about 0.35 s on Windows 11 build 26200 with MSVC 19.52.

Run `ctest --preset dev --no-tests=error -R '^cpu_aot_fuzz_smoke$' -V`. `CPU_FUZZ_FUNCTIONS` and `CPU_FUZZ_SEED` select a longer manual run; 96 functions at seed `0xCCD1` made 60,096 comparisons in about 1.1 s. A mismatch prints the seed, function, input and one-function MASM; `--only NAME --input INDEX` repeats that case. The leaf scope is ALU, flags and branches; calls, shared callees, tail jumps, dead reads and fault equivalence remain follow-ups. A case worth keeping can be reduced to a fixed fixture with the existing `cpu_asm_fixture` and `cpu_translation` helpers.

`cpu_aot_fuzz_frames` runs the same comparison on generated functions with fixed RSP frames (`--area frames`), from its own seed, so the leaf smoke above is unchanged. Each function saves one to three of RBX, RSI, RDI and R12 to R15 with `PUSH`, or saves RBX and RSI into home space with `MOV` before `PUSH RDI`, as MSVC does at `/O2`. It then allocates 16 to 40 bytes with `SUB RSP` and keeps RSP 16-byte aligned. The prologue carries the matching `.pushreg`, `.savereg` and `.allocstack` directives. Each saved register is overwritten from RCX and RDX before it is used, because the translator rejects any read of a nonvolatile register's entry value. A 16-byte region of the allocation is filled by a random sequence of 8-, 4-, 2- and 1-byte stores from registers or immediates. Some functions then add a register to a piece of the region in place, or overwrite one or two bytes on either side of a branch. Four to six reloads read 1, 2, 4 or 8 bytes at an offset within the region, zero- or sign-extended. The first is placed across a boundary between two stores. Every reloaded value goes to the output buffer and into RAX. The function restores the saved registers with `MOV` or `POP` and releases the frame with `ADD RSP`. The default run uses seed `0xF4A3` and 16 functions: 5 home-space saves, 90 stores into the regions and 82 reloads, of which 19 are 8-byte, 23 are 4-byte, 24 are 2-byte and 16 are 1-byte. Of the reloads, 44 span two or more stores (at least one in each function), 33 read part of one store and 5 read one store exactly. Nine functions have a branch and 9 have an in-place add. It makes 10,016 native, default and `/O2` comparisons in about 0.3 s. A planted translator fault must be detected: `--mutant` translates the same exports with every frame reload narrower than 8 bytes moved one byte higher in the frame array. The test fails unless that build differs from native. At the default seed, 4,945 of 5,008 calls differed, in all 16 functions. Passing the clean translation as `--mutant` fails with `the planted translator fault was not detected`, and passing the mutant as the translation fails at the first mismatch with the reproduction command. A dropped register restore would not serve as the planted fault, because the generated locals are not the physical registers (see `cpu_aot_stack_frames`). `CPU_FUZZ_FRAMES_SEED` and `CPU_FUZZ_FRAMES_FUNCTIONS` select a longer run. Seeds `0x1`, `0x2`, `0xBEEF` and `0xCCD1` at 128 functions each made 80,128 comparisons per seed in about 1.2 s, with no refusal and no mismatch; 1,450 of their 2,578 reloads span two or more stores. The generator does not use an RBP frame pointer, `LEAVE`, a mid-body RSP change or calls. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_fuzz_frames$' -V`.

## Mesh draw fallback

The bounded mesh translator can emit `cs_6_0` entry `main` and `vs_6_0` entry
`vertex_main` from the same validated three-vertex, one-primitive program using
`tools/mesh_entry_aot.py --compute-fallback`. The compute stage keeps the
original constant buffers and indexed vertex SRV. It writes counts, positions,
primitive indices and optional PARAM0 to a 128-byte buffer. The runner transitions
the buffer from UAV to vertex SRV and issues a triangle-list draw. The vertex stage
uses `SV_VertexID` to follow the generated indices and passes PARAM0 to the
translated pixel stage as `TEXCOORD0`.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^mesh_indexed_fallback_(hardware|warp)$' -V
```

On Windows 10 22H2 build 19045.6456, the GTX 1060 6 GB (32.0.15.6094) and in-box
WARP (10.0.19041.5794) both matched all 128 output bytes and all 1,024 pixels for
the full and partial triangles, two partial draws with independent changes to
the compute constant buffers, and both vertex-color gradients. The controls
required a clean debug layer when available. They detected a changed guard
at byte 76 with zero pixel differences, a collapsed index at byte 72 with 276
pixel differences, and a stale palette with 992 pixel differences and matching
exports. DXC 1.9.2602.17 compiled all stages
at Shader Model 6.0. The debug layer reported zero errors and warnings.
All seven native and fallback mesh tests also passed on app-local WARP 1.0.22
with Agility SDK 1.620.2.

This fallback covers the single-group specialization below. The supported
shape has three vertices, one triangle and optional PARAM0. The hardware's
native wave width is not involved because the translator specializes the
guest wave behavior offline for this shape.

## Single-group mesh entry specialization

The indexed variant, selected with `--vertex-header WORD`, specializes packed
16-bit geometry indices and the vertex header while leaving the actual float3
vertices in a 512-byte GPU SRV at t0. Table slots 0, 0x20 and 0x40 select
geometry, object transform and projection buffers. The profile binds the low/high
pointer inputs in s18/s19 and verifies the constructed four-word vertex
descriptor before accepting a read. A changed header or index stream requires
retranslation. A changed vertex position can use the same DXIL. Undefined
registers, reads before their waits and out-of-range vertex accesses are rejected.

```text
python3 -B tests/mesh_indexed_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_indexed_draw_" -V
```

The source-built `mesh_cbv_order` test binds two separate upload resources as
root CBVs and exchanges their GPU addresses for the second draw. The mesh entry
adds the float at byte 4 of b0 to the float at byte 12 of b1. The translated
pixel entry reads its color from b0 at byte 16. Binding A/B draws red at depth
0.5 and B/A draws white at depth 0.625. On RTX 4060 Laptop and WARP, the three
exported depths and all 1,024 RGBA pixels matched for both orders. Comparing
the swapped draw to the old color detected 1,024 mismatches. Comparing to the
old depth failed the mesh trace with zero pixel mismatches.

```sh
ctest --preset dev --no-tests=error -R "^mesh_cbv_order_" -V
```

The indexed attribute fixture binds three additional float3 colors in the vertex
buffer. Its mesh entry exports a four-component PARAM0 per vertex. The pixel
entry interpolates attr0, packs the channels to binary16 and exports MRT0.
The two 32×32 CPU references below differ only in the uploaded vertex colors.
Both matched every RGBA byte on RTX 4060 and WARP. Comparing the swapped
upload to the original reference produces 992 mismatches and a matching mesh
trace. The 32 equal-channel pixels lie on one diagonal.

![Two CPU reference gradients verified by GPU readback](mesh-attribute-reference.png)

```sh
python3 -B tests/mesh_attribute_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_attribute_draw_" -V
```

The textured mesh fixture passes indexed UVs through PARAM0 and samples a
source-built 2×2 RGBA8 texture with a translated `image_sample_lz` pixel shader.
The mesh buffer and pixel texture both use `t0`, with stage-specific root
signature visibility. The sample consumes only `xy` but declares a four-component
`TEXCOORD0` to match the mesh output. Point sampling, clamp addressing and the
single mip level make the reference selection deterministic.

The 32×32 readback compares a baseline, an independent texture change and a
horizontal UV reflection. Each stale baseline reference gives exactly 276
mismatches while the geometry trace still matches. The images are CPU
references checked byte for byte against each GPU readback.

![Baseline texture, changed texture, and reflected UVs](mesh-textured-reference.png)

```sh
python3 -B tests/mesh_textured_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_textured_draw_" -V
```

The same indexed textured scene can run through the Shader Model 6.0 compute
and vertex fallback. Compute reads the vertex buffer at `t0` and writes the
export buffer at `u0`. The graphics root binds the exported geometry to the
vertex shader at `t1` and the 2×2 texture to the pixel shader at `t0`. Both
paths use the same translated sample and CPU image reference.

On Windows 10 build 19045.6456, the GTX 1060 6 GB (32.0.15.6094) and in-box
WARP (10.0.19041.5794) matched all 128 export bytes and 1,024 RGBA pixels for
each of the three inputs. The stale texture and stale UV references each
produced 276 pixel mismatches and a matching export trace. The fallback pixel
shader also reads markers at `b0` and `b1`. Changing either marker alone
produced the same 276 pixel mismatches with matching exports. The D3D12 debug
layer reported zero warnings and errors.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^mesh_textured_fallback_' -V
```

The compute-to-mesh texture fixture reuses that translated mesh and pixel
shader. A translated compute program writes four packed RGBA8 texels to a raw
default-heap UAV, with rows at byte offsets 0 and 256. After a UAV-to-copy-source
transition, a GPU copy places those rows in a 2×2 texture. The draw samples the
texture after its transition to pixel-shader-resource state. The dispatch,
copy, and draw share one direct command list. The test reads back the final
1,024 pixels and the mesh export trace.

Two independently generated palettes match their CPU references. In the
stale-binding control, both producers run but the draw binds the first texture
while expecting the second. All 276 covered pixels differ while the mesh
export trace and all 748 clear pixels match.

```powershell
ctest --preset dev --no-tests=error -R '^compute_mesh_texture_(warp|hardware)$' -V
```

The GPU-produced texture can also feed the SM 6.0 textured fallback on the
same direct command list. The producer writes `u0` and copies its rows into
the texture. The fallback then binds its own compute root to export geometry,
and the vertex and pixel stages use the exported geometry and selected texture.
The case file contains the export and image references and no texture upload
payload.

The GTX 1060 and Windows 10 in-box WARP matched all 128 export bytes and all
1,024 pixels with one producer and with the second of two producers selected.
Selecting the first texture against the second texture's reference changed
exactly 276 pixels while the exports matched. The debug layer reported zero
warnings and errors.

```powershell
ctest --preset dev --no-tests=error -R '^compute_mesh_texture_fallback_' -V
```

The mesh depth-pair fixture draws two partially overlapping indexed triangles
with translated mesh and pixel stages. The two dispatches share one RGBA8
target and one D32_FLOAT depth buffer with depth writes and LESS enabled.
It clears both targets once and compares both complete 32×32 readbacks
against a CPU edge test. It also checks each dispatch's geometry trace.

Reversing draw order preserves every color and depth value. Exchanging the
two depths changes the winning color at the 190 overlapping pixel centers.
At the 172 centers covered by one triangle, the color stays fixed and the
depth changes. Separate stale-color and stale-depth references verify both
comparators. These CPU reference images matched the GPU readbacks on
RTX 4060 and WARP.

![Mesh depth-pair color references before and after the depth exchange](mesh-depth-pair-reference.png)

```powershell
ctest --preset dev --no-tests=error -R '^mesh_depth_pair_(hardware|warp)$' -V
```

The SM 6.0 depth-pair fallback uses two compute dispatches to export both
triangles into separate ranges of one buffer. After a single UAV-to-SRV
transition, two vertex draws read those ranges in order and share the same
color and depth targets, cleared once. The fixture uses the native color and
depth references and checks 256 bytes of exports, including both sets of
PARAM0 values. The GTX 1060 and Windows 10 in-box WARP matched all 1,024
color and 1,024 depth pixels for baseline, reversed order and swapped depths.
The stale-color and stale-depth controls reproduced 190 and 172 mismatches,
respectively, with matching exports and zero D3D12 debug warnings or errors.

```powershell
ctest --preset dev --no-tests=error -R '^mesh_depth_pair_fallback_(hardware|warp)$' -V
```

The lit mesh fixture reuses the indexed PARAM0 mesh program. The three vertex
attributes are now normals whose x and y equal the vertex NDC position and
whose z is 1/4. The source-built pixel program interpolates attr0. It loads a
light direction and an ambient term from b0 at byte 0x80 and computes N·L with
`v_mul_f32` and two `v_fmac_f32`. It clamps the result with `v_max_f32`, adds
the ambient term and exports gray RGB with the interpolated constant one as
alpha. The b0 bytes it reads are unused by the mesh program. The normals are not
normalized: the fixture checks the dot product, the clamp and the attribute
path, not a lighting model.

The CPU reference evaluates N·L exactly at each pixel center of the 276 covered
pixels and applies the round-toward-zero binary16 export and the 8-bit target.
At pixel centers N·L is a multiple of 1/128, so with an ambient term of exactly
1/4 every result is itself a binary16 value. On the RTX 3070, 133 of the 276
interpolated results of the base draw were one or two binary32 ulps (at most
2^-23) below that value. Round toward zero then dropped a whole binary16 step,
which changed the 8-bit byte at 17 pixels. WARP interpolated every pixel center
exactly. The ambient term is therefore 1/4 + 2^-17: every positive result lies
at least 2^-17 from both neighbouring binary16 values, 64 times the observed
error, and still exports the same binary16 value. The reference asserts this
for every pixel. On RTX 3070 and WARP, with the debug layer enabled,
three lights match every RGBA byte: the base light, a second light and the
inverted base light. Three negative controls
compare against the base reference:

- **Inverted light:** the same shaders and normals with the light negated in b0.
  All 276 covered pixels differ; the 748 clear pixels match.
- **Ignored normal:** a pixel program that lights a fixed +Z normal (the
  attribute's constant fourth component) gives 276 mismatches. It matches its
  own flat-shaded reference.
- **Missing clamp:** without `v_max_f32`, only the 132 covered pixels that face
  away from the light differ. It matches its own unclamped reference.

Each draw also checks the mesh export trace.

![Base light, second light and inverted light references](mesh-lit-reference.png)

```sh
python3 -B tests/mesh_lit_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_lit_draw_" -V
```

The SM 6.0 lit mesh fallback uses the same mesh and pixel programs and CPU
image reference. The compute shader writes the indexed positions and PARAM0
normals to the export buffer. The vertex shader reads the exports and passes
the normals to the rasterizer for interpolation. The pixel shader reads the
light from b0 and computes the same clamped lighting.

On the GTX 1060, Windows 10 in-box WARP and app-local WARP, all five positive
cases matched 128 export bytes and 1,024 RGBA pixels. The stale light, normal
and clamp controls gave 276, 276 and 132 pixel mismatches, respectively, with
matching exports. The D3D12 debug layer reported zero warnings and errors.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^mesh_lit_fallback_(hardware|warp)$' -V
```

The shadow fixture (`tests/mesh_shadow_check.py`, `offscreen_check
--mesh-shadow`) adds a depth pass and a comparison sample to the lit fixture.
All draws use the PARAM0 mesh program of the textured fixture on a 64×64 target.
The scene is orthographic with depth z. A directional light projects (x, y, z)
to (x − z/2, y + z/2) with depth z. Every vertex lies on the pixel grid in both
views, so each receiver pixel center samples a texel center of the map.

- **Light pass:** the caster triangle at depth 1/4 and a second triangle at
  depth 7/8 go into an `R32_TYPELESS` texture through a `D32_FLOAT` view. The
  depth test is `LESS` and the map is cleared to 1. The pixel program is the
  caster's, with color writes masked. On WARP (10.0.26100.9818), a mesh
  pipeline without a pixel shader removed the device in this fixture.
- **Camera pass:** a receiver plane at depth 3/4 covers the target. Its
  source-built pixel program interpolates the map coordinate from attr0.xy and
  loads its light-space depth from b0 at byte 0x90. It compares that depth
  against the map with `image_sample_c_lz` (point, `LESS`, through the #718
  path, `SampleCmpLevelZero` on an `R32_FLOAT` view with a static comparison
  sampler from the decoded words). It exports 1/4 + result/2 as the plane color.
  The caster is then drawn at depth 1/4 with the #749 N·L program, reading its
  normal from attr0.xyz. The normal's z is 17/32, so N·L at every pixel center
  is an odd multiple of 1/128 and no result exports as 1/2. The ambient term is
  1/4 + 2^-17, as in the lit fixture.

The CPU reference rasterizes both views exactly with `Fraction` and asserts that
no pixel or texel center lies on an edge. It evaluates N·L at each caster pixel
and applies the comparison, `CLAMP` addressing and the 8-bit target. It does not
use D3D12. Every RGBA byte, every texel of the current map (576 at 1/4, 176 at
7/8, the rest 1) and the mesh trace of each of the five draws are compared. On
RTX 3070 and WARP with the debug layer, the base scene matches, with 342 plane
pixels in shadow. The second triangle is behind the plane: its 176 texels are in
the map, and the plane pixels that sample them stay lit. The scene does not tell
`CLAMP` from `WRAP`: the texels a wrapped coordinate would reach are clear or
behind the plane. Negative controls:

- **`GREATER` for `LESS`** (translated program and host sampler): every one of
  the 3,520 plane pixels flips. The draw matches its own `GREATER` reference.
- **Stale map:** the camera pass binds the map drawn in the same command list
  for a previous light, (x + z/2, y + z/2). 356 pixels differ. The draw matches
  its own reference for that map.
- **Ignored map:** a receiver program that exports the lit color without
  sampling gives 342 mismatches. It matches its own unshadowed reference.
- **Coverage instead of depth:** a reference where any written texel shadows
  differs at 52 pixels, the plane pixels that sample the triangle behind the
  plane.
- **Map without the second triangle:** the map readback differs at its 176
  texels.

![Shadow scene reference and the light-space depth map](mesh-shadow-reference.png)

```sh
python3 -B tests/mesh_shadow_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_shadow_draw_" -V
```

A separate draw binds two immutable constant buffers through a shader-visible
D3D12 table. The translated mesh reads depth from b0 and a horizontal offset
from b1. The translated pixel stage reads a packed color from b0. Both draws
reuse the same shaders and upload bytes. Exchanging the two CBV slots moves the
triangle's left edge from column 0 to column 8 and changes the covered pixels
from red to green. The test compares all 1,024 pixels and the mesh export trace.
Using the old pixel reference reports 1,024 mismatches. Using just the old mesh
reference fails the trace check while every pixel still matches.

```sh
ctest --preset dev --no-tests=error -R "^mesh_descriptor_swap_" -V
```

A local 1,028-byte entry (197 instructions) read three distinct vertex
positions and produced visible geometry with controlled indices, transforms and
vertex data. The two sets of positions cover 1,024 and 276 of the 1,024 pixel
centers. Both plain and instrumented mesh DXIL match the CPU raster reference.
The instrumented draw also checks counts, three float4 positions, the primitive
indices and guard bytes. Aliasing one vertex to another changes exactly the
276 covered pixels and fails the export check. Measured with the debug layer
enabled on Windows 26200.9974, RTX 4060 Laptop 32.0.16.1656 and WARP
10.0.26100.9818. The source-built fixture also permutes the packed indices.

`tools/mesh_entry_aot.py` takes an extracted mesh container and a JSON array of
unsigned geometry words. It checks the native extent and the PSV triangle
topology, group size and output bounds. It decodes the entry with LLVM in
wave64 mode, follows two waves of forward control flow and specializes the
geometry reads and lane masks. The resulting desktop mesh shader reads position
and transform data from b0 and b1, emits three vertices and one primitive, and
can record its exports to a diagnostic UAV when compiled with `TRACE_EXPORTS`.

The specialization assumes one 128-thread group, wave ID in bits 24–27 of s3,
a descriptor table token in s17, and zero for s20, s21 and v5. Table offsets
0x20, 0x60 and 0x80 select static geometry, point data and the transform,
respectively. Changing geometry requires retranslating the entry. The native
draw state and application bindings remain unverified. Each public test builds
its own inputs. The visible triangle control uses an independently authored
native program, and the CPU checks its 32×32 target, exported vertices,
primitive index order and guard bytes.

```text
python3 -B tests/mesh_entry_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^mesh_entry_draw_" -V
```

A local scan based on main `933cbad` selected the shortest validated native
stream among 9,282 MS records (832 bytes, 153 instructions). For the tested
geometry its three vertices receive the same transformed position. The raster
target therefore retains its clear color. Two independently calculated
point/matrix cases matched all three exported positions and the (0,1,2) index
triple. The uninstrumented shader also completed a draw for both inputs. The
source-built visible triangle filled all 1,024 pixels, while altering a
reference index caused the comparison to fail. Tested on Windows build
26200.9974, RTX 4060 Laptop driver 32.0.16.1656 and WARP 10.0.26100.9818,
with the debug layer enabled.

## Compute UAV stores from a native container

`tools/compute_store_aot.py` accepts a local extracted DXBC compute container. It checks the stage in both the DXIL and PSV0 parts, the group dimensions, the FSSM v0x101 extent and termination, then decodes the gfx1013 instructions with LLVM. The store profile translates one or more non-overlapping raw UAV stores of one to four known 32-bit constant words per invocation, including constants loaded via `s_mov_b32`, `s_movk_i32`, and `v_mov_b32` register copies, along with `s_clause` hints and combined `lgkmcnt(0)`/`vmcnt(0)` waits. A test binding maps one four-word descriptor to u0. Its table pointer has a known low register and a zero high word. Scalar loads must complete before use. For an indexed store the caller also supplies the group-X register and byte stride, v0 carries local X, and the decoded shift and add must produce the dispatch-thread index. The tests bind in-range raw buffers with no swizzle, format conversion or out-of-range accesses. The original descriptor contents and dispatch configuration require separate evidence.

```sh
python3 -B tools/compute_store_aot.py local.dxbc --llvm libLLVM-15.so.1 --table-reg 5 --descriptor-offset 64 --group-reg 6 --stride 4 --output out/store.hlsl
python3 -B tests/compute_store_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

Build the emitted source with `dxc -T cs_6_0 -E main`. The Windows fixture reads back all 320 words, including gaps between records and the untouched tail, after one and three indexed groups. A wrong stored word and a wrong guard word must both fail. Run `ctest --preset dev --no-tests=error -R '^compute_store_aot_'`.

### Bounded gfx1030 DS lane routing

The `gfx1030` compute profile lowers `ds_bpermute_b32` (gather) and
`ds_permute_b32` (scatter) for a full wave32 in a 32×1×1 group. It requires
a proven `((local_x + bias) & 31) << 2` byte address, zero DS offset and
an immediate `s_waitcnt lgkmcnt(0)`. The bijective address
proves a single writer per destination for scatter. HLSL stages words in
thread-group shared memory and synchronizes the group. The gather reads the
forward rotation and the scatter reads its inverse. This uses group-thread
IDs for routing, so the desktop adapter's wave width is irrelevant.
[AMD RDNA 2 ISA](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

Source-built gfx1030 wave32 fixtures exercise both directions, wraparound,
and address/destination aliasing. They read 320 UAV words after dispatches of
one and three groups. Reversing the expected routing in HLSL must mismatch at
word 0 (5 vs. 27 for gather and 27 vs. 5 for scatter).

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^compute_store_aot_ds_route_(warp|hardware)$' -V
```

### Private gfx1030 LDS word access

The `gfx1030` compute profile lowers a thread-private LDS word in a full
wave32 with a 32×1×1 group. The caller supplies `--lds-bytes` for the native
allocation. The profile requires at least 128 bytes, a verified
`local_x << 2` address and an immediate
`s_waitcnt lgkmcnt(0)` after every DS operation. `ds_write_b32` initializes
or replaces the thread's word, `ds_read_b32` returns it in full, and
`ds_read_u16` zero-extends the low or high half at offset 0 or 2. This
footprint has no inter-thread aliasing, so the translator can retain the
written value as a per-invocation expression. Reads require initialization.

`ds_write_b8` and `ds_write_b16` replace the selected byte or halfword with
low source bits while preserving the rest of the initialized word.
`ds_read_u8` zero-extends a byte, and `ds_read_i8` and `ds_read_i16`
sign-extend their results to 32 bits. Byte offsets are 0 through 3 and
halfword offsets are 0 or 2. Subword accesses use the same address, LDS
allocation, full EXEC and immediate wait checks as full-word accesses.

`ds_read_u8_d16`, `ds_read_i8_d16` and `ds_read_u16_d16` replace the low
half of a known destination VGPR. `ds_read_u8_d16_hi` and `ds_read_u16_d16_hi`
replace the high half. The other half keeps its previous bits. The byte
forms extend to 16 bits before insertion. The fixtures use different
initial destination words and LDS offsets and compare the entire 320-word
UAV after one and three groups. Zeroing the preserved half or changing the
signed byte read to unsigned must mismatch at word 0.

The no-return forms `ds_add_u32`, `ds_sub_u32`, `ds_and_b32`, `ds_or_b32`
and `ds_xor_b32` update the same initialized word in place at offset 0.
Addition and subtraction wrap modulo 2^32, with subtraction taking the
old word minus the VGPR value. Each lane has exclusive use of its own word,
so the updates can use per-invocation arithmetic in HLSL.
The corresponding `ds_add_rtn_u32`, `ds_sub_rtn_u32`, `ds_and_rtn_b32`,
`ds_or_rtn_b32` and `ds_xor_rtn_b32` forms also copy the word's value before
the update into the destination VGPR. They capture the inputs before writing
that destination, which may overlap the address or data VGPR.

At offset 0, `ds_rsub_u32` stores the source VGPR minus the old word modulo
2^32. `ds_min_i32` and `ds_max_i32` compare the operands as signed 32-bit
integers. `ds_min_u32` and `ds_max_u32` compare them as unsigned integers.
Each comparison stores the selected operand's 32-bit pattern in the private
LDS word. Their returning forms `ds_rsub_rtn_u32`, `ds_min_rtn_i32`,
`ds_max_rtn_i32`, `ds_min_rtn_u32` and `ds_max_rtn_u32` also write the
pre-update word to the destination VGPR.

`ds_inc_u32` and `ds_dec_u32` update the private word using an unsigned
limit from a VGPR. Increment resets to zero at or above the limit.
Decrement resets to the limit when the old value is zero or exceeds it.
`ds_wrap_rtn_b32` subtracts the limit at or above it and adds a second
VGPR otherwise. The three returning forms copy the old word into their
destination. The fixtures compare old and new words around zero and the
limit and with high-bit values. Replacing the update with a read or no-op
must mismatch at word 1.
[AMD RDNA 2 ISA](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

`ds_cmpst_b32` and `ds_cmpst_rtn_b32` replace the private word when
it equals the first data operand. `ds_mskor_b32` and `ds_mskor_rtn_b32`
clear the bits selected by the first operand and OR in the second operand.
`ds_wrxchg_rtn_b32` replaces the word directly. The returning forms write
the old word to the destination VGPR. The fixtures compare old and new words
and exercise successful and unsuccessful comparisons. Reversing the comparison
operands, masking the OR operand or omitting the exchange must mismatch at word 1.

The same 16-byte record supports `ds_write_b128` and `ds_read_b128` at
byte offset zero, `ds_write2_b32` at distinct dword offsets and
`ds_write2_b64` and `ds_read2_b64` at qword offsets. Both elements must fit
and a dual write must not overlap itself. The fixtures export four words
per lane for one and two groups. Clearing the fourth source or swapping
paired offsets must produce a readback mismatch.

`ds_write_b8_d16_hi` selects source bits 23:16 and `ds_write_b16_d16_hi`
selects bits 31:16, preserving bytes outside the addressed field.
`ds_read_i8_d16_hi` sign-extends the selected byte into bits 31:16 and
preserves the destination's low half. The fixtures replace the high source
or signed extraction with its low or unsigned counterpart and expect a mismatch.

`ds_write_addtid_b32` and `ds_read_addtid_b32` use the same private word
when M0 is explicitly zero, the instruction offset is zero and a single
full wave32 makes TID equal to local X. The fixture writes through each
addressing mode and reads through the other, then compares both words.
Returning the first value for the second read must mismatch at word 1. The implicit address uses a four-byte stride. The lowerer rejects it if a 16-byte private LDS stride also appears in the program, regardless of which form comes first.

Paired strided LDS accesses use `local_x * width + offset * 64 * width`,
where width is four or eight bytes. The profile accepts initialized read
planes starting at byte 512 or higher and entirely within the LDS binding.
Each kernel uses one strided element width. The dword exchange captures
both old values and both inputs before replacing the destination VGPRs.
The fixtures compare two or four words per lane for one and two groups.
Swapping the paired offsets must mismatch at word 0.

```powershell
ctest --preset dev --no-tests=error -R '^compute_store_aot_lds_strided_(warp|hardware)$' -V
```

The private qword replacement fixtures cover compare-and-store, masked OR
and write-exchange at byte offset 8. The compare checks both dwords. Masked
OR clears the selected bits before OR-ing the full replacement pair. Returning
forms preserve the old pair even when the output overlaps a source. Swapping
the compare operands, masking the OR data, or omitting exchange each changes
the readback.

```powershell
ctest --preset dev --no-tests=error -R '^compute_store_aot_lds_wide_replacement_(warp|hardware)$' -V
```

Ten private qword updates operate on an initialized pair of record slots
at byte offset 0 or 8. `ds_add_u64`, `ds_sub_u64` and `ds_rsub_u64` update
modulo 2^64 using a low-word carry or borrow. `ds_and_b64`, `ds_or_b64` and
`ds_xor_b64` update both words independently. `ds_min_i64`, `ds_max_i64`,
`ds_min_u64` and `ds_max_u64` compare the high words first, signed for the
i64 forms and unsigned for the u64 forms, then compare the low words when
the high words are equal. Fixtures read back the pair at offset 8 and cover
both outcomes of the carry, borrow and comparison tests. The min/max fixtures
alternate the prior high word between `0x7fffffff` and `0x80000000`: even lanes
cross the signed boundary, while odd lanes repeat the high word so the
low-word tie-break applies. Swapping signed and unsigned comparisons must
mismatch word 0; dropping the low-word comparison must mismatch word 34 for
min and word 2 for max. Discarding carry or borrow, interchanging the bitwise
operations, or replacing min with max must also change the readback.
Run these variants with `ctest --preset dev --no-tests=error -R
'^compute_store_aot_lds_qword_(warp|hardware)$' -V`.


The private LDS f32 profile requires `--lds-f32-mode normal-rne` as a
caller guarantee that interpreted operands and results are normal binary32
or positive zero and that the native arithmetic uses round-to-nearest-even.
It lowers add, returning add, min, max and compare-and-store at byte offset
zero. Each add emits a separate `precise float` operation. Min/max copy the
selected word and compare-and-store copies the replacement on equality.
The fixture uses exact results for normal inputs. NaNs, signed zero and
subnormal arithmetic require a separate profile.

```powershell
ctest --preset dev --no-tests=error -R '^compute_store_aot_lds_f32_(warp|hardware)$' -V
```

The source-built wave32 fixture reads the full word and both halves after
reassigning the source VGPR. It compares 320 UAV words for dispatches of one
and three groups. Selecting the low half for the high-half read must
mismatch at word 2 (`0x000089ab` expected, `0x0000fff0` observed).

Six subword variants cover all supported offsets. Each compares the updated
word, the unsigned extraction and the signed extraction, with values on both
sides of the sign boundary. Two successive partial writes check that the
second preserves the earlier change. The fixture overwrites the source VGPR
after the subword write and overwrites the LDS word again before export to
check that earlier reads retain their values. Each dispatch checks the entire
320-word UAV, including the untouched tail. Replacing the partial write with a full
write must mismatch at word 0. Replacing sign extension with zero extension
must mismatch at word 50, the signed result of lane 16.

Five update variants read the initial word and the results of two consecutive
updates with different operands. The inputs cover wraparound, underflow and
high-bit operands. Each variant compares all 320 UAV words after one-group
and three-group dispatches. Replacing the first update with a full-word
write must mismatch at word 1.

For each returning update, the first destination overlaps the data VGPR and
the second overlaps the address VGPR. Both returned values and the final LDS
word are compared across the entire UAV for one and three groups. Returning
the updated word from the first operation must mismatch at word 0. Omitting
the second update while preserving its return value must mismatch at word 2.

The reverse subtract and min/max fixtures read the initialized word and the
results of two consecutive updates. Operands cross the sign bit and wrap to
zero at different lanes. All 320 UAV words are compared for dispatches of one
and three groups. Swapping the subtraction operands or changing a comparison's
signedness must mismatch at word 1. The returning fixtures exercise both
destination aliases and compare both old words and the final LDS word.
Returning the updated word or omitting a write must also cause a mismatch
at the first lane affected by each mutation.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^compute_store_aot_lds_(private|packed|arithmetic|replacement)_(warp|hardware)$' -V
```

```sh
python3 -B tests/compute_store_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```


A separate private LDS layout uses `local_x << 4` and at least 512 bytes of
allocation. Each lane owns a 16-byte record. This layout supports
`ds_write_b64` and `ds_read_b64` at offset 0 or 8, `ds_write_b96` and
`ds_read_b96` at offset 0, and `ds_read2_b32` with independently ordered
dword offsets within the record. Each read requires all referenced dwords to
have been written. The profile rejects mixing the two LDS address strides
because their per-lane regions can overlap. The fixtures compare every UAV
word and detect a wrong qword offset, a missing data word, or reversed dual
read offsets at word 0.

An optional `--constant-descriptor BYTE_OFFSET` binds a second table entry to
b0. It must be distinct from the 16-byte u0 descriptor. The translator accepts
aligned immediate `s_buffer_load_dword`, `s_buffer_load_dwordx2`, and
`s_buffer_load_dwordx4` reads within the 64-byte binding. It tracks scalar waits,
register copies, and each loaded DWORD through the raw UAV store. The Windows
test dispatches one compiled shader with two different root-constant uploads and
compares all 320 output words each time. The stale reference fails on exactly
eight stored words. The binding is limited to unsigned 32-bit words and fixed
byte offsets.

### Compute programs with two exits

Compute programs (stage 5) now use the [pixel two-exit check](#pixel-programs-with-two-exits) from #477 when LLVM is supplied. Only exactly two aligned `s_endpgm` markers qualify. One marker is unchanged, three or more are rejected, and mesh programs (stage 13) with more than one marker are still rejected. `compute_store_aot.py` now passes `--llvm` to the extractor, and `shader_profile_audit.py` already did. This step extracts the code span only. The store profile does not lower branches, so a two-exit program now stops at its first unsupported instruction instead of at extraction.

The fixtures are original kernels compiled with LLVM 15 and reassembled with `llvm-mc-15`:

```sh
llc-15 -mtriple=amdgcn--amdpal -mcpu=gfx1013 -O2 -filetype=obj kernel.ll -o kernel.o
llvm-objdump-15 -d --mcpu=gfx1013 kernel.o
```

A wave-uniform `if (gid >= n) return;` with branch weights that mark the return likely compiles to `s_cmp_ge_u32`, then `s_cbranch_scc0` over the early `s_endpgm` to the store block. That is the layout #477 verifies. Both an `amdgpu_kernel` and an `amdgpu_cs` form are extracted unchanged, and the CLI and the audit then stop at the lowering. Without branch weights, LLVM 15 branches to one shared exit instead. A per-lane bound (`local X >= n`) compiles to `s_and_saveexec_b32` and `s_cbranch_execz` over the store to a single `s_endpgm`, which already passes as a one-exit program.

`tests/compute_store_check.py binary` rejects:

- the same program without LLVM, or marked as a mesh program;
- a branch one instruction late, a branch to the final exit, a target separated from the early exit by a NOP, and a skipped non-NOP word;
- an unconditional branch that disconnects the early exit, and a missing branch that disconnects the store block;
- a third exit, or a literal word equal to `s_endpgm` that makes a third marker;
- in a one-exit program, the same literal, which gives two markers but one decoded exit;
- a branch into a literal operand or into the trailing `s_code_end` padding.

An `s_branch` that skips an alignment NOP at the start is still accepted, and the NOP stays in the span.

These are fixture results only. The 1,002 multi-exit CS records in the [fixed-profile rescan](#fixed-profile-archive-rescan) have not been split by marker count, so it is not known how many have exactly two exits or this layout. Their counts have not been remeasured after this change.

### Per-lane guards around compute stores

A per-lane bound such as `if (gid < n) store` compiles under LLVM 15 to one exit, with no early `s_endpgm`. A `v_cmp` sets `vcc_lo`, `s_and_saveexec_b32` saves `exec_lo` and masks it, and an optional `s_cbranch_execz` skips the masked block when no lane passes. `s_or_b32 exec_lo, exec_lo, sN` then restores the mask, or the program exits with the mask still applied. The store profile lowers this form as control flow: the stores issued under the mask go inside an HLSL `if`, and stores after the restore stay outside it.

The accepted subset is small:

- one guard per program, not nested;
- `v_cmp_{gt,lt,ge,le}_u32_e32 vcc_lo, a, vN`, where both operands are known words (a dispatch index, a constant word or an immediate). The operator and operand order are kept;
- `s_and_saveexec_b32` from `vcc_lo` only, and a restore from an SGPR that still holds the saved mask. With a branch, that SGPR must already hold it at the branch, so that it holds the mask when the block is skipped too;
- `s_waitcnt_depctr` as a no-op.

The CLI names each decoded `s_cbranch_execz` target as a label. The target must be forward, at a decoded instruction start, and exactly at the end of the masked block. Only `s_waitcnt_depctr` or `s_nop` may come between the target and the restore or the exit. A register written under the guard is unknown after the restore, since the lanes that did not pass, or a skipped block, keep the old value. A scalar load that was waited for only under the guard is pending again after the restore.

The two fixtures are unchanged LLVM 15 code generator output for original `amdgpu_cs` sources with a group of 32. One keeps the guard to the exit and sinks the u0 descriptor load under it. The other restores the mask before a second, unguarded store. Their bindings are `--table-reg 0 --descriptor-offset 0 --constant-descriptor 32 --group-reg 1` with `--stride 4` or `--stride 8`, and the bound `n` is `params[0].x`.

The Windows fixture reads back all 320 words with the untouched words at a sentinel value. It runs seven dispatches per program, from one group with `n` of 0, 17 and 32 to three groups with `n` of 1, 70, 96 and 200. The guarded words of lanes at or past the bound must stay untouched. On three groups with `n = 70`, a reference built for 69 or 71 must fail, and so must recompiled HLSL with the guard inverted, off by one or removed. For the second program, HLSL that keeps the second store under the guard must also fail.

The CPU controls reject a missing or wrong restore, a saved mask overwritten under the guard, a restore from a copy made only inside the skipped block, a branch target before, inside or after the end of the masked block, a branch without a label, a label without a branch, a second branch to another label, `s_cbranch_execnz`, wave64 `s_and_saveexec_b64`, a mask from another source, a compare of an unknown, pending or non-word operand, a nested or second guard, a guard around no store, and registers written or waited for only under the guard and then used after the restore. Removing both the restore and the branch is accepted with both stores inside the `if`, because the mask then holds until the exit.

`shader_profile_audit.py` is unchanged. It does not bind b0, so it does not try guards that read their bound from a constant, and it does not label branches.

### Vector reads from a second raw buffer

The store profile can copy one word per read from a second, read-only raw buffer to the store. The caller binds it with `--source-descriptor`, the table byte offset of its four-word descriptor, which becomes `ByteAddressBuffer inputData : register(t0)`. `--source-stride` gives its byte stride for indexed reads. The descriptor may come from its own `s_load_dwordx4` or from one `s_load_dwordx8` with a neighbouring bound descriptor, which is how the LLVM 15 code generator loads u0 and the source together. The word read is stored, copied with `v_mov_b32`, or used by the small integer ALU subset of the next subsection.

The accepted subset is small:

- `buffer_load_dword vN, ..., s[a:a+3], 0` with a zero SGPR offset and an aligned instruction offset under 4096, from the t0 descriptor only. A read from the u0 descriptor is rejected, because it would have to be ordered against the stores of other invocations;
- the address is a fixed `off` offset, an `offen` byte offset `dispatch index << shift` with a shift of at least 2, or an `idxen` dispatch index with `--source-stride`, where the instruction offset must stay inside one source element;
- the loaded VGPR is unknown until `s_waitcnt vmcnt(0)`, alone or combined with `lgkmcnt(0)`. Reading or overwriting it before that wait is rejected, and so is a partial `vmcnt` count.

The caller keeps the source bytes valid and unchanged for the dispatch, with no overlap between the bytes read through t0 and the bytes written through u0. This lets the HLSL read t0 where the word is used, inside the guard when the store is guarded. Loads follow the guard rules above. A load issued under the guard is unknown after the restore, and a load from before the guard that was waited for only under it is pending again after the restore. If a register written by an earlier load was overwritten under the guard, it is unknown after the restore.

The two fixtures are unchanged LLVM 15 code generator output (`clang 15.0.7 -target amdgcn--amdpal -mcpu=gfx1013 -O2 -x ir -Xclang -disable-llvm-passes`, equivalent to `llc-15`) for original `amdgpu_cs` sources with a group of 32, `if (gid < n) out[gid] = src[...]`. One reads source byte offset `gid * 4` through `offen`. The other reads word 1 of an indexed source element through `idxen offset:4`. Its stride is a descriptor field, bound here as 8 bytes. Their bindings are the guard bindings with `--source-descriptor 16`, and `--source-stride 8` for the indexed read. With the full `-O2` IR pipeline, the code generator loads the two descriptors separately. That form is accepted and produces the same HLSL.

The Windows fixture binds t0 to a buffer of 320 known words followed by 320 sentinel words, and u0 to 320 sentinel words. It reads back all 320 words of u0. It runs three dispatches per program: one group with `n` of 0 and 17, and five groups with `n = 150`, where every lane reads one of the known words. On the last dispatch, a reference with one copied word off by one bit, or with lane 150 also copying, must fail. So must recompiled HLSL that ignores the read index, reads the next source word, or reads u0 instead of t0.

The CPU controls reject a read without its `vmcnt(0)` wait, with only an `lgkmcnt(0)` wait, or copied or overwritten before its wait. They also reject a read from the u0 descriptor or the constant buffer, a source bound at another or overlapping table slot or not bound at all, and a descriptor still pending. Further controls cover an address that is not the dispatch index or uses the other addressing mode, an indexed read without a stride, an unaligned, 4096-byte or out-of-element instruction offset, and a word read under the guard or overwritten there and then stored after the restore.

```sh
python3 -B tools/compute_store_aot.py local.dxbc --llvm libLLVM-15.so.1 --table-reg 0 --descriptor-offset 0 --constant-descriptor 32 --group-reg 1 --stride 4 --source-descriptor 16 --output out/copy.hlsl
```

### Integer ALU between a read and a store

The store profile accepts these unsigned 32-bit vector operations on known words:

| Instruction | Result |
|---|---|
| `v_and_b32_e32 vD, a, vB` | `a & b` |
| `v_lshrrev_b32_e32 vD, a, vB` | `b >> (a & 31)`, logical |
| `v_mad_u32_u24 vD, a, b, c` | `(a & 0xffffff) * (b & 0xffffff) + c`, modulo 2^32 |
| `v_min_u32_e32 vD, a, vB` | unsigned minimum |
| `v_mul_lo_u32 vD, a, b` | low 32 bits of `a * b` |

A source is a VGPR, an SGPR that holds a known word, an integer inline constant or a 32-bit literal. The VOP2 forms take a VGPR as the second source. A known word is a literal, a constant-buffer word, a dispatch index, a word read from t0, or an earlier result. The result becomes an inline HLSL expression of its sources. An expression longer than 1024 characters is rejected. `v_mad_u32_u24` with a byte offset as an operand is still the row pitch of a store address, as before.

Results are data only. They can be stored or copied, but they cannot form a store or read address. A result or a word read from t0 is also rejected as a lane-guard compare operand. These instructions leave `vcc` intact, so the guard compare stays valid. A result written under the guard is unknown after the restore, like any other register.

Anything else stops the lowering at that instruction, with the instruction in the error. This includes signed and other operations (`v_min_i32`, `v_max_u32`, `v_ashrrev_i32`, `v_or_b32`, `v_mad_i32_i24`), carry forms that write `vcc` (`v_add_co_u32`), 16-bit, 64-bit and SDWA forms, register ranges, an SGPR as a VOP2 second source, and output modifiers such as `clamp`.

The fixture is unchanged LLVM 15 code generator output for an original `amdgpu_cs` source, with the read bindings and `cap = params[0].y`:

```c
if (gid < n) {
    uint v = src[gid];
    out[gid] = umin((v & 0xffff) * (v >> 16) + gid, cap);
}
```

```hlsl
if (params[0].x > tid.x) {
    outputData.Store(tid.x * 4u + 0u, min(params[0].y, ((((0x0000ffffu & inputData.Load((tid.x << 2u) + 0u)) & 0xffffffu) * ((inputData.Load((tid.x << 2u) + 0u) >> (0x00000010u & 31u)) & 0xffffffu)) + tid.x)));
}
```

The Windows fixture uses the read harness and its three dispatches, with `cap = 0x45000000`, which clamps about half of the lanes. The expected words come from a CPU model of the source. On five groups with `n = 150`, each of these must fail: a reference with one word off by one bit, with `max` instead of `min`, a shift of 15, a 24-bit instead of a 16-bit mask, or lane 150 also writing. So must recompiled HLSL with `max` for `min`, a shift of 15, a 24-bit mask, the `+ gid` dropped, or the cap read from `params[0].z`. The operands of `v_mad_u32_u24` stay below 2^16 in this fixture, so the 24-bit masks are checked by the exact output and not on the GPU.

The same check includes a separate MAD24 fixture that holds bit 24 in the
first multiplicand and bit 25 in the second. It enables lane zero only and
sets the cap to UINT_MAX. The full 320-word readback requires word 0 to be
`0x0df050e1` with all other words unchanged. Removing source truncation
individually yields `0xfef050e1` and `0xeff050e1`; each must fail at word 0.

#### Guarded full-width integer multiply

The `v_mul_lo_u32` fixture reads each active lane's word from t0, multiplies it
by the unsigned constant-buffer word `0x01000003`, and stores the low 32 bits
to u0. The first lane's product wraps and both operands have nonzero bits
above bit 23. The full 320-word readback covers zero active lanes, a split
group, and five groups with 150 active lanes. Two stale references truncate
one operand at a time, and a third changes the first inactive word. The test
requires each control to fail at the expected word. Run
`ctest --preset dev --no-tests=error -R '^compute_store_aot_guard_read_(warp|hardware)$' -V`.

### Two- and three-dimensional thread groups

The PSV0 group size can be any `numthreads(x,y,z)` within the `cs_6_0` limits
(x and y up to 1024, z up to 64, x·y·z up to 1024). The translator emits it
unchanged, so an 8×8 group stays 8×8. On gfx10, compute waves receive local X,
Y and Z in separate registers `v0`, `v1` and `v2`. LLVM 15 packs the three IDs
into `v0` only for targets with `FeaturePackedTID` (gfx90a, gfx940 and gfx11), and
`FeatureISAVersion10_1_3` does not have it. `v1` and `v2` are bound only when
that group dimension is larger than one. `--group-reg-y` and `--group-reg-z`
name the group-ID SGPRs in the same way as `--group-reg`. LLVM allocates them
as consecutive system SGPRs in X, Y, Z order, each only when the program reads
it. For an original program this is still a caller assumption.

A dispatch index is `v_lshl_add_u32` of a group ID and the local ID of the
same axis, shifted by log2 of a power-of-two group dimension. An indexed store
takes one of three forms:

- one `idxen` axis, when every other group dimension is one;
- a row axis through `idxen` and a column axis through `offen`, with a
  `v_lshlrev_b32` byte offset. The store must fit in one element, and one
  group's row must fit in the stride.
- a plane axis through `idxen` and an `offen` byte offset that folds a row
  axis and a column axis: `v_lshl_add_u32` of the row dispatch index, a shift
  and the column's `v_lshlrev_b32` byte offset. The three axes must differ.
  The store must fit in one element, one group's columns in one row
  (`1 << shift`), and one group's rows in the stride.

A byte offset can also come in the form LLVM 15 emits for gfx1013:
`s_lshl_b32` of a group ID by log2 of its power-of-two group dimension gives
the group's first dispatch index, and `v_add_lshl_u32` of that SGPR and the
local ID of the same axis, with a shift, gives the same byte offset as
`v_lshlrev_b32` of the dispatch index. The group-base SGPR is not a storable
word. The `llvm_cube` fixture keeps the address instructions that `llc-15`
produced for the 4×4×4 store below, with their registers:

```sh
llc-15 -mtriple=amdgcn--amdpal -mcpu=gfx1013 -O2 -filetype=obj cube.ll -o cube.o
llvm-objdump-15 -d --mcpu=gfx1013 cube.o
```

The source is an `amdgpu_kernel`; only its kernel-argument load is replaced by
the descriptor-table load of the other fixtures. Its bindings are
`--table-reg 0 --descriptor-offset 0 --group-reg 2 --group-reg-y 3
--group-reg-z 4 --stride 136`.

A store with `offen` and no `idxen` can also take a row pitch:
`v_mad_u32_u24` of a row dispatch index, a literal pitch and the column's
`v_lshlrev_b32` byte offset, with the pitch in either multiplicand. LLVM 15
emits this form when the source masks the row index to 24 bits, and places the
pitch in src0. The instruction multiplies only bits [23:0] of the row, so the
HLSL masks the row with `0xffffff` before the multiply. The pitch must be a
multiple of 4 from 4 to 2^24 - 4. The store must fit in one element and one
group's columns in the pitch, and one group's rows must not wrap the 32-bit
address. Without `idxen` the descriptor stride is unused,
so `--stride` does not change the output.

All stores in a program use one layout, so no two invocations of a group write
the same bytes. A dispatch-thread index can also be stored as data. The
translator does not model out-of-range behavior for structured buffers: the
caller's stride and dispatch must keep every row in range. Addresses that need
other ALU forms are still rejected: a `v_mul_lo_u32` row pitch on an unmasked
index, a pitch read from a constant or a register, a `v_lshl_or_b32` dispatch
index and a pitch offset combined with `idxen`.

```sh
python3 -B tools/compute_store_aot.py local.dxbc --llvm libLLVM-15.so.1 --table-reg 8 --descriptor-offset 0 --group-reg 10 --group-reg-y 11 --stride 72 --output out/tile.hlsl
```

The Windows fixture covers five group shapes, one of them in two address
forms, and a row-pitch kernel, each with a full 320-word readback:

| Group | Dispatches |
|---|---|
| 8×8×1 | 1×1×1 and 1×2×1 |
| 4×1×8 | 1×1×1 and 1×1×2 |
| 4×4×1 | 1×1×1 and 3×2×1 |
| 256×1×1 | 1×1×1 |
| 4×4×4 | 1×1×1 and 1×2×2 |
| 4×4×4, LLVM 15 form | 1×1×1, 1×1×2 and 1×2×2 |
| 4×4×1, row pitch | 1×1×1 and 2×3×1 |

The 4×4×4 kernel stores dispatch Z at `tid.z * 132 + (tid.y << 4) + (tid.x << 2)`.
Its 132-byte stride covers both groups' rows in Y and leaves one guard word per
plane. The LLVM 15 form stores dispatch Z at `tid.z * 136 + (tid.y << 4) +
(tid.x << 2)`, with two guard words per plane.

The row-pitch kernel stores `(tid.x, tid.y)` at
`(tid.y & 0xffffff) * 72 + (tid.x << 3)`. Two groups of 8-byte columns in X
leave two guard words per row. The fixture is hand-assembled in the operand
order LLVM 15 emits. It does not cover row indexes of 2^24 or more, where the
mask changes the address.

The expected words come from an independent per-invocation model. For each
shape, the following must fail on the adapter:

- a flipped guard word;
- the first group count against the larger reference;
- recompiled HLSL with swapped axes;
- recompiled HLSL that ignores Z (4×1×8 and 4×4×4);
- recompiled HLSL with half the group X size;
- for the LLVM 15 form, recompiled HLSL with X and Z or Y and Z swapped;
- recompiled HLSL with a 64-byte pitch (row pitch only).

Each recompiled variant runs on the shape's largest dispatch, with one
exception: Y and Z both span 8 in the 1×2×2 dispatch, so swapping them leaves
the buffer unchanged. That swap runs on 1×1×2, where Y spans 4 and Z spans 8
(`MUTATION_DISPATCH` in the test).

The CPU controls reject:

- swapped local or group axes;
- a non-power-of-two size whose shift still matches;
- `v1` in a group whose Y is one;
- duplicate or missing group registers;
- an element or row overflow;
- an index and a byte offset taken from the same axis;
- two stores with transposed layouts;
- a plane offset whose row and column, or whose index and row, share an axis;
- a plane offset built from a raw local ID or from an index instead of a byte
  offset;
- a group's columns overflowing a row, or its rows overflowing the stride;
- a plane offset stored without its `idxen` index, or stored as data;
- a group base with the wrong shift, a non-power-of-two size whose shift still
  matches, a source that is not a group ID, a destination past `s105` or still
  pending from a descriptor load, or a group base scaled again; the Y form, with
  a group Y size different from X, checks the shift and the power of two against
  Y;
- `v_add_lshl_u32` of an unscaled group ID, of the local ID of another axis or
  into a register past `v255`, and a group base stored as data;
- a pitch that is unaligned, zero, negative, wider than 24 bits, narrower than
  one group's columns, so wide that one group's rows wrap the address, or read
  from a register;
- a pitch offset built from a raw local ID, from the column's axis or from an
  index instead of a byte offset, or written with `clamp`;
- a pitch offset combined with `idxen`, stored as data, or used next to a
  store with another pitch.

They also check that the pitch is accepted in either multiplicand, as a
decimal inline constant, with or without a stride binding, and with a column
byte offset in the LLVM 15 group-base form.

`tools/shader_profile_audit.py` passes the PSV0 size and tries the next one or
two SGPRs after the group-X candidate for group Y and Z. A CPU control in
`compute_store_check.py` checks that a 4×1×8 kernel reading group Z from the
SGPR after group X is accepted, and one reading it two SGPRs later is not.
Another checks that the audit accepts the row-pitch kernel although it always
offers a stride for groups larger than one thread. Its
record counts have not been remeasured after this change.

A local scan of 2,689 compute records accepted 12 records (10 distinct instruction streams). Ten use one-thread groups and fixed addresses. Two use 32- and 64-thread groups and indexed addresses. Tested table bindings used s4 or s5 with descriptor offsets of 0, 32 or 64. The indexed bindings used strides of 4, 8, 12 or 16 bytes, selecting the first compatible profile. On RTX 4060 Laptop (32.0.16.1656) and WARP (10.0.26100.9818), each record compiled to desktop DXIL and dispatched. Each adapter completed 14 full-buffer comparisons with zero mismatches, including two different group counts for both indexed records.

## Vector bit operations in pixel shaders

`pixel_aot.py` adds vector bitfield extraction (`v_bfe_u32`, `v_bfe_i32`),
mask construction (`v_bfm_b32`) and population count plus accumulator
(`v_bcnt_u32_b32`) through the shared integer lowering. The fixture also
checks the existing bit reversal (`v_bfrev_b32`) and first-bit searches
(`v_ffbh_u32`, `v_ffbh_i32`, `v_ffbl_b32`) in e32 and e64, for twelve
forms on gfx1013 and gfx1030.
Offset and width use their low five bits, including width 32 becoming zero.
Signed extracts preserve sign when a field crosses bit 31; scans preserve the
all-bits-set sentinel. Integer results remain raw VGPR bits through bitcasts.

The semantics follow the [AMD RDNA2 ISA](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)
and [LLVM's constant BFE folding](https://llvm.org/doxygen/AMDGPUInstCombineIntrinsic_8cpp_source.html).
Bit scans map to [HLSL first-bit intrinsics](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/firstbithigh),
with the required leading-count conversion and signed input normalization.
No SDWA/DPP modifiers, lane-mask counts, floating input modifiers or clamp are
accepted by this lane-local profile. Existing pending-load, pending-sample and
execution-mask checks still apply.

The original source fixtures run each form on sixteen constant triples. Each
32-bit result is exported as four exact byte values using BFE, half widening
and power-of-two scaling. The Python reference uses explicit bit-position loops,
signed fields and masked operand widths. Each backend performs 192 positive
draws and twelve deliberately wrong references that must report pixel mismatch.
Each form uses one renderer batch with its sixteen inputs and one wrong
reference. The renderer reuses its device and pipeline within the batch.
The acceptance-only controls do not add forms to other draw tests' records.
`tests/form_coverage_check.py` requires precisely the twelve forms in both GPU
reference lists and leaves them uncovered for failed or skipped tests.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_aot_bits_(hardware|warp)$' -V
```

Before switching to the batched renderer, on Windows 11 build 26300.9974, MSVC 19.44.35230 and Windows SDK
10.0.26100 DXC, based on main `6ee2406` plus this change: both new CTests
passed, without capability skips, with zero pixel error. NVIDIA GeForce RTX 3060
(10de:2503), driver 32.0.16.2101, completed in 113.72 seconds; WARP
10.0.26100.9818 completed in 8.46 seconds. The debug layer was unavailable.
The earlier serial hardware matrix exceeded its 180-second suite deadline;
completed draws matched. Bounded parallel device initialization completed the
same matrix within that budget. The existing `pixel_aot_warp` also passed;
the legacy `pixel_aot_hardware` reached its separate 60-second timeout and is
not counted as a pass. No full-suite success is claimed.

The batched fixture passed all 192 inputs and 12 wrong-reference controls per
adapter on Windows 11 build 26200.9974 with Windows SDK 10.0.26100 DXC. WARP
10.0.26100.9818 took 5.7 seconds and the RTX 4060 Laptop (32.0.16.1656) took
19.3 seconds, with the debug layer enabled on both. These timings cover the
dedicated test command, including compilation.

The native AMDGPU binary round trip is also part of `pixel_aot_check.py binary`;
LLVM 18 assembled and disassembled all twelve forms for gfx1013 and the generated
HLSL matched source lowering. The tests and implementation are original MIT code.
During this work, Leonida's independently implemented SPIR-V semantic tests were
reviewed at revision `67204bf8c0471335d9046890288b33e8d5e2875b`; no Leonida source
was incorporated into this translator.


## AVX2 leaf recompilation

[The AVX2 leaf translator](../tools/cpu_avx2_aot.py) lifts bounded straight-line Windows x64 leaf functions covering all 28 AVX2 forms in `docs/coverage/x64_zen2.tsv` group `AVX2`:

- 128-bit lane broadcast and permute: `vbroadcasti128` (`m128` source), `vextracti128`, `vinserti128`, `vperm2i128`
- Element broadcasts: `vpbroadcastb`, `vpbroadcastw`, `vpbroadcastd`, `vpbroadcastq` (XMM and YMM destinations)
- Cross-lane permutes: `vpermd`, `vpermpd`, `vpermps`, `vpermq`
- Blend: `vpblendd` (XMM and YMM widths)
- Variable shifts: `vpsllvd`, `vpsllvq`, `vpsravd`, `vpsrlvd`, `vpsrlvq` (XMM and YMM widths)
- Indexed gathers (VSIB): `vgatherdps`, `vgatherdpd`, `vgatherqps`, `vgatherqpd`, `vpgatherdd`, `vpgatherdq`, `vpgatherqd`, `vpgatherqq` (all 16 VEX encodings across `vm32x`, `vm32y`, `vm64x`, `vm64y` and XMM/YMM widths)
- Masked loads and stores: `vpmaskmovd`, `vpmaskmovq` (all 8 VEX encodings across XMM/YMM load and store forms)

Each lifted export takes `(word rcx, word rdx, word r8)` pointing to caller-owned flat buffers with 16-byte-aligned bases and returns `void`. The caller supplies live buffers for the whole invocation and keeps them private to the executing thread. This fixture uses the default MXCSR controls (masked exceptions, round-to-nearest-even, DAZ and FTZ disabled) and compares result bytes. Memory forms using a full 128-bit operand require a displacement divisible by 16. For transport and ABI glue the translator accepts `vmovdqu` (128- and 256-bit flat loads, stores and register copies), `vzeroupper` and terminal `ret`.

### State model, memory semantics and explicit rejections

YMM registers `ymm0`..`ymm5` are represented as a portable C++ value struct (`struct Vec256 { std::uint64_t q[4]; }`) rather than MSVC AVX2 intrinsics, so the generated C++ compiles with default x64 flags without `/arch:AVX2` and avoids C++ shift-width undefined behavior when shift counts are `>= 32` or `>= 64`. Every VEX write to an XMM destination zeroes bits 128..255 of the corresponding YMM register, and `vzeroupper` zeroes bits 128..255 of all defined YMM registers. `vpermd` and `vpermps` mask each 32-bit index to its low 3 bits; floating-point permutes and gathers transfer raw bits without canonicalization or FP exceptions. Per the AVX2 ISA, variable shifts do not wrap counts modulo lane width: unsigned lane counts `>= 32` (32-bit shifts) or `>= 64` (64-bit shifts) zero the lane in `vpsllvd`, `vpsllvq`, `vpsrlvd` and `vpsrlvq`, and fill the lane with the sign bit in `vpsravd`.

Memory-domain AVX2 instructions follow the Intel SDM lane, fault-suppression and register-state rules:

- **Indexed gathers (`vgather*` / `vpgather*`)**: each lane `i` within the instruction's gathered element count checks the MSB of `mask[i]`; when set, it loads 32 or 64 bits from `base + SignExtend(vindex[i]) * scale + disp` inside an `if` guard so masked-off lanes never dereference memory. Masked-off lanes within the gathered element count preserve their prior destination value; upper bits beyond the gathered element count are zeroed (bits `64..255` for `vgatherqps`/`vpgatherqd` with `vm64x`, bits `128..255` for other XMM-destination gathers). After every gather, the entire 256-bit mask register is zeroed.
- **Masked loads and stores (`vpmaskmovd` / `vpmaskmovq`)**: masked loads read only lanes whose mask MSB is set and zero masked-off lanes (and bits `128..255` for XMM destinations) while preserving the mask register; masked stores write only lanes whose mask MSB is set and never touch masked-off memory locations.

Memory-operand scope remains explicit: `vbroadcasti128` (`m128`), `vmovdqu` and `vpmaskmovd`/`vpmaskmovq` require flat `[base + disp]` addressing rooted in `rcx`, `rdx` or `r8`, and gathers require VSIB `[base + vindex*scale + disp]` with base in `rcx`/`rdx`/`r8`, SIB scale in `{1, 2, 4, 8}`, and pairwise distinct `dest`/`index`/`mask` vector registers. The translator explicitly rejects:

- memory operands on all 17 register-only AVX2 forms;
- overlapping gather destination/index/mask registers (including XMM/YMM cross-width aliases), illegal VSIB scales, missing/non-argument/32-bit/`rsp` bases, RIP-relative or GPR-indexed `vpmaskmov` operands, and reads of undefined `dest`/`index`/`mask`/`src` vector registers;
- EVEX encodings (including EVEX gathers), instruction prefixes, nonvolatile vector registers (`xmm6`..`xmm15` / `ymm6`..`ymm15`), branches, out-of-scope instructions, missing terminal `ret` and unreachable instructions after `ret`.

```powershell
cmake --build --preset dev --target cpu_avx2_regs_original cpu_avx2_regs_original_lifted cpu_avx2_regs_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_avx2_regs$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/avx2_regs.asm) builds 68 exports covering all 28 AVX2 forms (42 register-domain exports plus 18 gather exports and 8 `vpmaskmov` load/store exports) across XMM and YMM widths, ignored immediate bits, `vperm2i128` lane-zeroing control bits, IEEE-754 special bit patterns (signed zero, subnormals, infinities, sNaN and qNaN), boundary lane indices (`0`..`7` with high-bit noise), boundary shift counts (`0`, `1`, `15`, `31`, `32`, `33`, `63`, `64`, `65`, `128`, `0x100000001`, `0x7fffffff`, `0x80000000`, `0xffffffff`, `0xffffffffffffffff`), `vzeroupper` upper-lane clearing, a chained `op_pipeline` function, full/partial/zero masks (including `0x7fffffff`/`0x7fffffffffffffff` non-MSB checks), negative and boundary signed 32-bit and 64-bit VSIB indices, scales `1`/`2`/`4`/`8`, positive/negative displacements, all four gather dest-width/index-width combinations (including `vm64x` bits `64..255` zeroing and post-gather mask zeroing), and Windows `VirtualAlloc` + `PAGE_NOACCESS` guard-page fault-suppression checks for every gather and `vpmaskmov` export. [The Windows test](../tests/cpu/avx2_regs_check.py) checks 1,120 differential cases (772 register-domain + 348 memory-domain) and 34 guard-page fault-suppression cases across all 68 exports with 16-byte pre/post guards against both the native MASM DLL and an independent Python reference model.

An `ALTER_IMM` variant changes the immediates or memory displacements of `op_perm2i128_mix`, `op_permq`, `op_pblendd_ymm`, `op_gatherdps_ymm` and `op_pmaskmovd_load_ymm`, and must match the altered reference while diverging from the unmodified reference on 14, 14, 16, 12 and 10 cases respectively. An output-mutation negative control flips one bit of `op_pipeline` and confirms the comparator fails. Rejection controls cover 59 unit cases (memory-operand variants of all 17 register-only AVX2 forms, 7 out-of-scope ops, 23 gather/maskmov/EVEX violations including overlapping dest/index/mask registers and unsupported addressing, and 12 ABI/state violations), 4 direct invalid-scale checks (`0`, `3`, `5`, `16`), and 9 end-to-end PE-patched DLL cases. In `tools/form_coverage.py`, `avx2_regs.asm` accounts for all 28 of 28 forms in `x64_zen2.tsv` group `AVX2`.

Not covered by this experiment: memory operands on the 17 register-only AVX2 forms, control flow or calls mixed with AVX2 state, nonvolatile `XMM6`..`XMM15` save/restore prologues, and title CPU code execution.

## Two-table depth-only pixel shaders

### Coverage-only texture sampling

`tools/sampled_coverage_aot.py` translates a two-exit PS which selects a texture
through a buffer word, samples an interpolated UV and compares the scaled sample
with a threshold. Both exits export only coverage. The selector, table pointers
and point-clamp sampler are specialized for a supplied snapshot. The texture
and two comparison inputs remain GPU resources. Changing the selector requires
another translation. The tested inputs are finite and the textures have one mip.

The JSON bindings used by the source-built fixture are constructed by
`bindings(slot)` in `tests/sampled_coverage_check.py`. A descriptor entry lists
the table's entry SGPR, its byte offset, the descriptor kind and its value.
For a CB the value is a word offset into desktop b0. For a texture it selects
t0 or t1. `words` supplies concrete entry SGPR values and `selector_byte`
identifies the known word in the selector buffer.

```sh
python3 -B tools/sampled_coverage_aot.py local.dxbc --bindings snapshot.json --llvm libLLVM-15.so.1 --output out/coverage.hlsl
python3 -B tests/sampled_coverage_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R '^sampled_coverage_' -V
```

The Windows fixture measures a complete triangle with a D3D12 occlusion query,
then checks all 64 pixel locations with individual scissored draws. The color
target must retain its clear value. It exercises both bound textures, mixed
coverage, full survival and full rejection. A reference computed for the
opposite texture must fail at the sample comparison.

### Buffered depth export

`tools/depth_mask_aot.py` follows the two entry table pointers through scalar
moves, four-word descriptor loads and completed constant-buffer reads. Supply the
two `--table SGPR BYTE_OFFSET` pairs in desktop b0, b1 order. The destination
buffers each contain 2 KiB. The input convention supplies window x, y and depth
in v2, v3 and v4. The supported exits export depth for surviving lanes or discard
them. Unknown table addresses, outstanding reads, changed EXEC provenance and
unrecognized exit sequences reject translation.

The Windows fixture binds distinct seeds and thresholds, changes each separately,
then exchanges the two root CBV addresses. It checks all 1,024 D32_FLOAT
pixels and requires comparison against the old reference to fail after the
exchange. LLVM CI assembles an original gfx1013 program and translates it from a
synthetic container through the CLI.

```text
python3 -B tests/depth_mask_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
ctest --preset dev --no-tests=error -R "^depth_mask_draw_" -V
```

In a local inventory based on main `933cbad`, 354 of 12,101 PS records
shared one additional native program. With the explicit bindings, the first
read followed entry s8 at byte offset 0x480 and consumed an integer at byte
offset 0x5f0 of the resulting buffer. The second followed the saved entry s9
at offset zero and consumed a float at byte zero. Physically exchanging the
buffers changed 897 of 1,024 depth samples against the old reference on both a
GeForce RTX 4060 Laptop (32.0.16.1656) and WARP (10.0.26100.9818), Windows
build 26200.9974, debug layer enabled. Matching results use controlled inputs
and the specified binding convention. Target application resource identity and
original render state remain unverified.
## AVX packed-integer leaf recompilation

[The AVX packed-integer leaf translator](../tools/cpu_avx_int_aot.py) lifts bounded straight-line Windows x64 leaf functions covering 90 AVX packed-integer and state forms in `docs/coverage/x64_zen2.tsv` group `AVX` (excluding the 28 packed-integer forms carved out to `cpu_vex_aot.py (PR #325)` so each form has a single translator):

- state management: `vzeroall`, `vzeroupper`
- packed absolute value: `vpabsb`, `vpabsw`, `vpabsd`
- saturated packs: `vpacksswb`, `vpackssdw`, `vpackusdw`
- packed add and subtract (wrapping and signed/unsigned saturating): `vpaddb`/`q`, `vpaddsb`/`usb`/`usw`, `vpsubb`/`d`/`q`, `vpsubsb`/`sw`/`usb`/`usw`
- align and blend: `vpalignr`, `vpblendvb`, `vpblendw`
- packed compares and min/max: `vpcmpeqb`/`w`/`d`/`q`, `vpcmpgtb`/`w`/`d`/`q`, `vpmaxsb`/`sd`/`ub`/`uw`/`ud`, `vpminsb`/`sd`/`ub`/`uw`/`ud`
- horizontal operations: `vphaddw`/`d`/`sw`, `vphsubw`/`d`/`sw`, `vphminposuw`
- multiply and multiply-add: `vpmaddwd`, `vpmuldq`, `vpmuludq`, `vpmulhrsw`, `vpmulhuw`, `vpmulld`
- sums of absolute differences: `vpsadbw`, `vmpsadbw`
- shuffles and packed sign: `vpshufhw`, `vpshuflw`, `vpsignb`, `vpsignw`, `vpsignd`
- sign-extend and zero-extend moves: `vpmovsxbw`/`bd`/`bq`/`wd`/`wq`/`dq`, `vpmovzxbw`/`bd`/`bq`/`wd`/`wq`/`dq`
- unpack/interleave: `vpunpckldq`/`hdq`/`lqdq`/`hqdq`
- packed shifts: `vpslld`/`q`, `vpsrld`/`q` (both `imm8` and `xmm` count encodings)
- movemask, extract and insert: `vpmovmskb`, `vpextrb`/`w`/`d`/`q` (register and memory forms), `vpinsrb`/`w`/`d`/`q` (register and memory forms)

Each export uses the leaf ABI `void fn(word rcx, word rdx, word r8)` where `rcx`, `rdx` and `r8` carry flat host pointers to caller-owned buffers. `vmovdqu` (128/256-bit flat load, store and register copy), flat 32/64-bit `mov` load/store for volatile scratch GPRs (`rax`, `r9`, `r10`, `r11`) and terminal `ret` serve as transport/ABI glue.

### State model, ISA semantics and explicit rejections

YMM registers `ymm0`..`ymm5` use a portable C++ value struct (`struct Vec256 { std::uint64_t q[4]; }`) so the generated C++ compiles under default x64 flags without `/arch:AVX` and avoids signed overflow or shift-width undefined behavior. Every VEX.128 write zeroes bits `128..255` of the destination YMM register. `vzeroupper` zeroes bits `128..255` of all defined YMM registers; `vzeroall` zeroes all 256 bits of all six modeled registers `ymm0`..`ymm5`.

Key ISA rules enforced by the translator:

- **VEX.128 only**: AVX1 has no 256-bit packed-integer encodings (`VEX.L=1` packed-integer instructions belong to AVX2). Every `VEX.L=1` encoding of the 88 packed-integer forms is rejected explicitly (`vzeroall` is the sole `L=1` form in scope).
- **Shift counts**: `vpslld`/`q` and `vpsrld`/`q` support both `imm8` and `xmm` count encodings. Per the Intel SDM, the `xmm` count operand reads bits `63:0` as an unsigned 64-bit count (ignoring bits `127:64`), and counts `>=` lane width zero the lane without wrapping.
- **Saturation, rounding, compares and blends**: signed/unsigned saturating add, subtract, horizontal add/sub and pack clamp at exact 8-bit/16-bit bounds; `vpmulhrsw` rounds and scales per `(((a * b) >> 14) + 1) >> 1`; `vpblendvb` selects solely on bit 7 of each mask byte; `vpcmpeq*`/`vpcmpgt*` produce all-ones or all-zero lanes; `vpsign*` negates, zeroes or preserves per the sign of the second operand; `vphminposuw` breaks ties toward the lowest word index.
- **GPR bridge and memory operands**: `vpmovmskb` and `vpextr*` register forms zero-extend results into volatile scratch GPRs (`rax`, `r9`, `r10`, `r11`). Flat `[base + disp]` memory operands rooted in `rcx`, `rdx` or `r8` are accepted only on `vpextrb`/`w`/`d`/`q`, `vpinsrb`/`w`/`d`/`q` and transport `vmovdqu`/`mov`.

The translator explicitly rejects:

- `VEX.L=1` (256-bit) packed-integer encodings;
- memory operands on all register-only AVX packed-integer instructions;
- the 28 packed-integer forms carved out to `cpu_vex_aot.py (PR #325)` (`vpackuswb`, `vpaddd`, `vpaddw`, `vpaddsw`, `vpsubw`, `vpand`, `vpandn`, `vpor`, `vpxor`, `vpavgb`, `vpavgw`, `vpmaxsw`, `vpminsw`, `vpmaddubsw`, `vpmulhw`, `vpmullw`, `vpshufb`, `vpshufd`, `vpunpcklbw`, `vpunpckhbw`, `vpunpcklwd`, `vpunpckhwd`, `vpsllw`, `vpsraw`, `vpsrad`, `vpsrlw`, `vpslldq`, `vpsrldq`);
- undefined vector or scratch GPR reads, nonvolatile vector registers (`xmm6`..`xmm15` / `ymm6`..`ymm15`), nonvolatile GPRs (`rbx`, `rbp`, `rsi`, `rdi`, `rsp`, `r12`..`r15`), and scalar reads/writes of pointer-argument GPRs (`rcx`, `rdx`, `r8`);
- EVEX encodings, instruction prefixes, branches, out-of-scope instructions (AVX2-only ops, AVX floating-point ops, legacy SSE/MMX, GPR ALU), non-flat/non-argument memory operands, register-to-register `mov`, missing terminal `ret`, and unreachable instructions after `ret`.

```powershell
cmake --build --preset dev --target cpu_avx_int_regs_original cpu_avx_int_regs_original_lifted cpu_avx_int_regs_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_avx_int_regs$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/avx_int_regs.asm) builds 132 exports covering all 90 surviving AVX packed-integer forms across their valid encodings: both `xmm`-count and `imm8` shifts (including in-range, overflow `>=` lane width, and upper-64-bit nonzero `xmm` counts), register and flat-memory forms of `vpextrb`/`w`/`d`/`q` and `vpinsrb`/`w`/`d`/`q`, `vpalignr` across offsets `0`/`5`/`16`/`23`/`32`, `vmpsadbw` ignored immediate bits, saturation edges, full `vpsign*` sign matrices, `vpmulhrsw` rounding boundaries, `vphminposuw` tie-breaking, `vpmovsx*`/`vpmovzx*` sign/zero-extension extremes, `vpblendvb` bit-7 mask checks, `vzeroupper`, a 192-byte `vzeroall` check across `ymm0`..`ymm5`, and a chained `op_pipeline` export. [The Windows test](../tests/cpu/avx_int_regs_check.py) checks 1,728 differential cases across all 132 exports with 16-byte pre/post guards against both the native MASM DLL and an independent Python reference model.

An `ALTER_IMM` variant changes the immediates of `op_palignr_5`, `op_pblendw`, `op_mpsadbw_0`, `op_pshufhw` and `op_pextrb_reg`, and must match the altered reference while diverging from the unmodified reference on 12, 11, 8, 10 and 10 cases respectively. An output-mutation negative control flips one bit of `op_pipeline` and confirms the comparator fails. Alternate-encoding lift checks cover 6 r64/W1 GPR bridge forms (`vpmovmskb`, `vpextrb`/`w` and `vpinsrb`/`w` `r64` variants that MASM does not emit by default). Rejection controls cover 111 unit cases (25 `VEX.L=1` packed-integer encodings, 25 register-domain memory operands, 32 carved-out encodings covering all 28 carved-out forms, 10 out-of-scope instructions and 19 EVEX/ABI/GPR/addressing/state violations) and 11 end-to-end PE-patched DLL cases. In `tools/form_coverage.py`, `avx_int_regs.asm` covers all 90 surviving AVX packed-integer forms (plus transport `vmovdqu`, 91 AVX forms total), advancing x64 Zen 2 `AVX` group coverage alone from 7/256 to 96/256 (+89 net new forms; total x64 Zen 2 from 55/948 to 144/948).

Not covered by this experiment: the 28 packed-integer forms carved out to `cpu_vex_aot.py (PR #325)`, memory operands on register-only AVX packed-integer forms, 256-bit AVX2 packed-integer forms (`VEX.L=1`), AVX floating-point/move/test/string-compare forms, control flow or calls mixed with AVX state, nonvolatile register save/restore prologues, and title CPU code execution.

## AVX bit-move, shuffle, blend and logical leaf recompilation

[The AVX bit-move leaf translator](../tools/cpu_avx_move_aot.py) lifts bounded straight-line Windows x64 leaf functions covering 50 bit-transfer, shuffle, blend, broadcast, lane, permute, unpack, logical and masked load/store forms in `docs/coverage/x64_zen2.tsv` group `AVX` (excluding the 5 move forms `vmovdqa`, `vmovups`, `vmovaps`, `vmovq` and `vmovd` carved out to `cpu_vex_aot.py (PR #325)`; `vmovdqu` remains here as transport glue):

- Logical bitwise ops: `vandpd`, `vandps`, `vandnpd`, `vandnps`, `vorpd`, `vorps`, `vxorpd`, `vxorps` (XMM and YMM widths)
- Blends: `vblendpd`, `vblendps` (`imm8`), `vblendvpd`, `vblendvps` (sign-bit mask) across XMM and YMM widths
- Broadcasts: `vbroadcastf128` (`m128`), `vbroadcastsd` (`m64` and `xmm`), `vbroadcastss` (`m32` and `xmm`, XMM and YMM destinations)
- Lane ops: `vextractf128`, `vinsertf128`, `vextractps` (`r32`/`r64`), `vinsertps`
- Permutes: `vperm2f128`, `vpermilpd` (`imm8` and variable register-control), `vpermilps` (`imm8` and variable register-control)
- Shuffles and unpacks: `vshufpd`, `vshufps`, `vunpcklpd`, `vunpckhpd`, `vunpcklps`, `vunpckhps` (XMM and YMM widths)
- Vector and scalar moves: `vmovapd`, `vmovupd` (128/256-bit load, store and register copy), `vlddqu`, `vmovntdq`, `vmovntdqa`, `vmovntpd`, `vmovntps`, `vmovddup`, `vmovshdup`, `vmovsldup`, `vmovhlps`, `vmovlhps`, `vmovhpd`, `vmovhps`, `vmovlpd`, `vmovlps`, `vmovsd`, `vmovss`, `vmovmskpd`, `vmovmskps`
- Masked loads and stores: `vmaskmovpd`, `vmaskmovps` (all 8 VEX encodings across XMM/YMM load and store forms)

Each lifted export takes `(word rcx, word rdx, word r8)` pointing to caller-owned flat buffers with 16-byte-aligned bases and returns `void`. The caller supplies live buffers for the whole invocation and keeps them private to the executing thread. This fixture uses the default MXCSR controls (masked exceptions, round-to-nearest-even, DAZ and FTZ disabled) and compares result bytes. Memory forms using a full 128-bit operand require a displacement divisible by 16. For transport and ABI glue the translator accepts `vmovdqu` (128/256-bit flat load, store and register copy), flat argument-rooted 32/64-bit `mov` between volatile scratch GPRs (`rax`/`eax`, `r9`/`r9d`, `r10`/`r10d`, `r11`/`r11d`) and `[base + disp]`, `vzeroupper`, and terminal `ret`.

### State model, bit-transparent semantics and explicit rejections

YMM registers `ymm0`..`ymm5` use a portable `struct Vec256 { std::uint64_t q[4]; }` value model, so the generated C++ compiles with default x64 MSVC flags without `/arch:AVX`. All 50 instructions transfer or combine raw bits with no FP arithmetic, no NaN canonicalization and no MXCSR involvement; `vcmp*`, `vtest*`, `vcomis*` and `vucomis*` are out of scope. Every VEX write to an XMM destination zeroes bits `128..255` of the corresponding YMM register; `vmovss` (`m32`) and `vmovsd` (`m64`) zero-extend into their destination XMM register; `vmovmskpd`, `vmovmskps` and `vextractps` zero-extend to 64 bits; variable `vpermilpd` inspects bit 1 of each 64-bit control lane; `vinsertps` applies `ZMask` after inserting `CountS` into `CountD`; and non-temporal moves (`vmovnt*`) lower as plain loads/stores. `vmaskmovps` and `vmaskmovpd` check the MSB of each mask lane inside an `if` guard so masked-off lanes never dereference memory, zero masked-off load lanes and preserve the mask register.

Memory operands are allowed as flat `[base + disp]` rooted in `rcx`, `rdx` or `r8` only where the form requires memory (loads, stores, broadcasts, half/scalar memory moves, non-temporal moves, masked loads/stores and transport `vmovdqu`/`mov` glue). The translator explicitly rejects:

- memory operands on all 28 register-domain AVX forms that have memory encodings (`vand*`, `vor*`, `vxor*`, `vblend*`, `vextractf128`, `vinsertf128`, `vextractps`, `vinsertps`, `vperm2f128`, `vpermil*`, `vshuf*`, `vunpck*`, `vmovddup`, `vmovshdup`, `vmovsldup`);
- the carved-out move forms owned by `cpu_vex_aot.py (PR #325)` (`vmovdqa`, `vmovups`, `vmovaps`, `vmovq`, `vmovd`);
- out-of-scope ops (`vcmp*`, `vtest*`, `vcomis*`, `vucomis*`, FP/integer arithmetic, AVX2-only ops, legacy SSE, branches);
- EVEX encodings, instruction prefixes, nonvolatile vector (`xmm6`..`xmm15` / `ymm6`..`ymm15`) or GPR (`rbx`, `rbp`, `rsi`, `rdi`, `r12`..`r15`) registers, clobbering argument GPRs, reg-to-reg `mov`, reads of undefined vector or scratch GPR registers, RIP-relative/indexed/32-bit/`rsp`/non-argument memory operands, missing terminal `ret` and unreachable instructions after `ret`.

```powershell
cmake --build --preset dev --target cpu_avx_move_regs_original cpu_avx_move_regs_original_lifted cpu_avx_move_regs_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_avx_move_regs$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/avx_move_regs.asm) builds 150 exports covering all 50 surviving AVX forms across XMM and YMM widths, memory and register encoding variants, all 4 `vbroadcastsd` (`m64`) and all 8 `vbroadcastss` (`m32`) lane offsets, ignored immediate bits, `vperm2f128` lane-zeroing bits, `vinsertps` `CountS`/`CountD`/`ZMask` interactions (including zeroing `CountD`), `vpermilpd` bit-1 vs bit-0 variable control, IEEE-754 special bit patterns (`+0`, `-0`, subnormals, infinities, sNaN and qNaN), 32-byte aligned buffers for aligned and non-temporal moves, unaligned `+1`/`+3`/`+7` byte offsets for `vmovupd`/`vlddqu`/scalar/broadcast/half-XMM paths, GPR zero-extension checks (`vmovmskpd`, `vmovmskps`, `vextractps`), `vzeroupper`, a chained `op_pipeline` function, full/partial/zero/non-MSB-noise masks, and Windows `VirtualAlloc` + `PAGE_NOACCESS` guard-page fault-suppression checks for all 8 `vmaskmovps`/`vmaskmovpd` exports. [The Windows test](../tests/cpu/avx_move_regs_check.py) checks 2,418 differential cases (1,845 standard across 137 exports + 192 unaligned across 16 exports + 192 variable-blend across 4 exports + 176 `vmaskmov` across 8 exports + 13 negative-displacement across 1 export) and 16 guard-page fault-suppression cases with 32-byte pre/post guards against both the native MASM DLL and an independent Python reference model.

An `ALTER_IMM` variant changes immediates or memory displacements on 10 exports (`op_blendpd_ymm`, `op_blendps_ymm`, `op_broadcastf128`, `op_insertps_s3d2_zmask`, `op_perm2f128_mix`, `op_permilpd_imm_ymm`, `op_permilps_imm_ymm`, `op_shufpd_ymm`, `op_shufps_ymm`, `op_maskmovps_load_ymm`), matching the altered reference while diverging from the original on 13, 13, 11, 10, 11, 11, 11, 11, 11 and 10 cases respectively. An output-mutation negative control flips one bit of `op_pipeline` and confirms the comparator fails. Unit and end-to-end controls cover 7 alternate-encoding lift checks (`0F 11` reg-reg `vmovsd`/`vmovss`, `W1` 64-bit `vmovmskpd`/`vmovmskps`/`vextractps`), 79 unit rejection controls (30 register-domain memory encodings across all 28 register-only forms, 13 carved-out move encodings, 13 out-of-scope ops, 23 EVEX/ABI/state/addressing/prefix violations) and 11 PE-patched DLL rejection controls. In `tools/form_coverage.py`, `avx_move_regs.asm` accounts for all 50 surviving AVX forms (plus `vmovdqu` and `vzeroupper` ABI glue, 52 AVX forms total), advancing x64 Zen 2 `AVX` group coverage alone from 7/256 to 57/256 (+50 net new forms; total x64 Zen 2 from 55/948 to 105/948). Together, `avx_int_regs.asm` and `avx_move_regs.asm` advance x64 Zen 2 `AVX` group coverage from 7/256 to 146/256 (+139 net new forms; total x64 Zen 2 from 55/948 to 194/948).

Not covered by this experiment: the 5 move forms carved out to `cpu_vex_aot.py (PR #325)`, memory operands on the 28 register-only AVX forms, FP arithmetic/compare/convert/test instructions, control flow or calls mixed with AVX state, nonvolatile `XMM6`..`XMM15` save/restore prologues, and title CPU code execution.

## AVX packed string compare leaf recompilation

[The AVX packed string compare translator](../tools/cpu_avx_pcmpstr_aot.py) lifts bounded straight-line Windows x64 leaf functions exercising the four `VEX.128` (`W=0`) AVX packed string compare forms in `docs/coverage/x64_zen2.tsv` group `AVX`: `vpcmpestri`, `vpcmpestrm`, `vpcmpistri`, and `vpcmpistrm` (both `xmm` and flat `m128` second operands).

Each lifted export takes `(word rcx, word rdx, word r8)` pointing to caller-owned flat buffers and returns `std::uint64_t` in `rax` (packed `CF`/`ZF`/`SF`/`OF`/`PF` flags). `YMM0`..`YMM5` use the same portable `Vec256` model without `/arch:AVX`. Unlike legacy SSE4.2 `pcmpestrm`/`pcmpistrm`, `VEX.128` `vpcmpestrm` and `vpcmpistrm` zero `YMM0[255:128]` when writing the implicit `XMM0` mask; `vpcmpestri` and `vpcmpistri` zero-extend the 32-bit `ECX` index into `RCX`; `vpcmpestri` and `vpcmpestrm` read only the low 32 bits of `RAX`/`RDX` as signed lengths; and all four instructions clear `AF` and `PF` while setting `CF`, `ZF`, `SF`, and `OF` per the Intel SDM rules. Transport and ABI glue covers `vmovdqu` (128/256-bit), `vzeroupper`, volatile GPR `mov`/`or`/`xor`/`shl`/`setb`/`sete`/`sets`/`seto`/`setp`/`movzx`, and terminal `ret`. The translator explicitly rejects `VEX.256` (`L=1`), `VEX.W=1` (`vpcmpestri64`/`vpcmpestrm64`), EVEX, legacy SSE4.2 `pcmp*str*`, nonvolatile vector or GPR registers, writes to `r8`, reads through clobbered pointer registers, unsupported memory operands, prefixes, branches, undefined registers or flags, missing `ret`, and unreachable instructions after `ret`.

```powershell
cmake --build --preset dev --target cpu_avx_pcmpstr_original cpu_avx_pcmpstr_original_lifted cpu_avx_pcmpstr_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_avx_pcmpstr$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/avx_pcmpstr.asm) builds 22 exports covering `vpcmpestri`, `vpcmpestrm`, `vpcmpistri`, and `vpcmpistrm` across `xmm` and `m128` operands, all four element formats (`ub`, `uw`, `sb`, `sw`), all four aggregation modes, all four polarity modes, LSB/MSB and bitmask/byte-word mask outputs, ignored `imm8[7]`, `xmm0` self-aliasing, `xmm1` preservation, `RCX` 32-bit zero-extension, `RAX`/`RDX` upper-32-bit ignoring, `PF` clearing, and `YMM0[255:128]` zeroing. [The Windows test](../tests/cpu/avx_pcmpstr.py) skips with exit code 77 when CPUID/XCR0 lacks AVX and otherwise verifies 1,274 differential cases (including unaligned `[rdx]` offsets `+1` and `+3`, zero/negative/`>16`/`INT32_MIN` explicit lengths, and embedded NUL implicit lengths) with 16-byte pre/post guards against both native MASM DLLs and an independent Python reference model, plus 4 `ALTER_IMM` divergence controls, 1 output-mutation comparator control, 32 unit rejection controls, and 5 PE-patched DLL rejection controls.

A separate memory-source fixture discards the compare result. Windows native and translated execution still report a read fault on an inaccessible operand after the first output store and before the second. The valid operand executes both stores. The verifier also rejects a return with undefined RAX.

Not covered by this experiment: `VEX.W=1` 64-bit length forms (`vpcmpestri64`, `vpcmpestrm64`), control flow or calls mixed with AVX state, nonvolatile save/restore prologues, and title CPU code execution.

## Scalar SSE and SSE2 leaf functions

Compilers keep `float` and `double` values in XMM registers and use the scalar SSE and SSE2 instructions for them. The [scalar float translator](../tools/cpu_sse_aot.py) adds these instructions to the control flow and integer instructions of the [jump-table translator](../tools/cpu_switch_aot.py), without its jump tables. It lifts `MOVSS` and `MOVSD` between registers and memory, register `MOVAPS`/`MOVUPS` copies, `ADD`/`SUB`/`MUL`/`DIV` in both precisions, `CVTSI2SS`/`CVTSI2SD` from 32- and 64-bit integers, the truncating `CVTTSS2SI`/`CVTTSD2SI`, `CVTSS2SD`/`CVTSD2SS`, the four `COMISS`-family compares, `MOVD`, `MOVQ` between XMM registers and memory, and `XORPS`/`XORPD`/`PXOR` of a register with itself.

```powershell
cmake --build --preset dev --target cpu_float_original_lifted cpu_float_swapped_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_scalar_float$' -V
```

A PE does not record argument types, so each export is named with its signature, such as `blend_ss=f:ffqf`: the result, then up to four slots, each `f` (float), `d` (double) or `q` (a 64-bit integer or pointer). Under the Windows x64 convention, slot N arrives in XMM0 to XMM3 when it is floating point and in RCX, RDX, R8 or R9 otherwise. The generated function has the matching C++ signature. XMM6 to XMM15 are nonvolatile and a leaf has no frame to save them in, so any use is rejected.

The generated C++ keeps the low quadword of each XMM register in a 64-bit integer. Every supported instruction computes that quadword from low quadwords only, so the upper half of a register never reaches a result and is not modelled. The two dwords of the quadword are tracked as defined or undefined along every path. A `float` argument defines the low dword alone, a register copy carries undefined parts with it, and a read of an undefined part is rejected. `MOVSS` between registers and every single-precision result replace the low dword and keep bits 32 to 63.

Results must match the processor bit for bit, and three rules are written out because C++ leaves them to the host compiler:

- **NaN operands.** `ADDSS xmm0, xmm1` returns the quieted NaN of its destination when that is a NaN, otherwise the quieted source NaN. A compiler may swap the operands of `a + b`, which changes the payload when both are NaN, so the generated code tests the operands before the arithmetic. An invalid operation such as `inf - inf` gives the default NaN with the sign bit set.
- **Truncation.** `CVTTSS2SI` and `CVTTSD2SI` return the integer indefinite, `0x80000000` or `0x8000000000000000`, for a NaN or a value outside the destination range. The C++ cast is undefined there, so the range is tested first.
- **Compares and integer flags.** An unordered float compare sets ZF, PF and CF together; the three flags are stored by the compare and may feed a run of conditional jumps, as in the `JP`, `JNE` pair compilers emit for equality (`JE`, `JNE`, `JB`, `JAE`, `JBE`, `JA`, `JP` and `JNP` are accepted). Integer flag producers (including `NEG`) still feed only an immediately following conditional jump (`Jcc`), whereas `SETcc` and `CMOVcc` reuse the [jump-table translator](../tools/cpu_switch_aot.py) flag-producer rules across straight-line flag-neutral instructions within a block (without crossing joins or branches, and rejecting `COMISS`/`UCOMISS` as a flag producer). An 8-bit `SETcc` to a GPR preserves bits 8..63 and requires the register to be defined beforehand, a 32-bit `CMOVcc` zero-extends into bits 32..63 even when the condition is false, and a 16-bit `CMOVcc` is rejected.

Addition, subtraction, multiplication, division and the conversions run as one host instruction each under the thread's MXCSR, as in the original, with contraction to fused multiply-add switched off. Exceptions are assumed masked.

The [source fixture](../tests/cpu/scalar_float.asm) exports 14 functions (`blend_ss`, `blend_sd`, `convert`, `from_int`, `classify_ss`, `classify_sd`, `round_trip`, `zero_xorps`, `zero_pxor`, `zero_xorpd`, `probe_ss`, `neg_jcc`, `setcc_mix`, `cmovcc_mix`): single- and double-precision arithmetic through register and memory operands, conversions in both directions and between the precisions, signalling-NaN quieting, self-XOR zeroing, two classifiers that turn compare flags into a bit mask, `probe_ss`, `NEG`-driven conditional jumps, 8-bit `SETcc` preserving high bits after `CMP` and `NEG`, and 32-bit zero-extending and 64-bit `CMOVcc` after `CMP` and `NEG`. The test passes every floating-point slot as a `double` whose bit pattern it chooses, so signalling NaNs reach XMM unchanged and the upper dword of a `float` slot holds junk. Inputs cover zeros of both signs, denormals, infinities, quiet and signalling NaNs with distinct payloads, the values around ±2^31 and ±2^63, ties in the narrowing conversions, integer flag edge cases, and random patterns. For 13,690 calls it compares the result bits and a guarded output buffer between the original DLL and its translation, and again between a control build and the control's translation. The control assembles the same source with one operand order, condition, or source changed in each function. In `blend_ss` the control computes `b + a` instead of `a + b`, and the test requires that its results differ exactly when both operands are NaN. 43 calls have results fixed in the test from the instruction set reference, including both integer indefinites, the sign of the default NaN, 8-bit `SETcc` high-bit preservation on both true and false outcomes, and 32-bit `CMOVcc` zero-extension.

On a copy of the linked DLL, the test patches one instruction at a time and expects all 20 controls to be rejected: packed `ADDPS`, `SQRTSS`, VEX-encoded `VADDSS`, `ADDSD` on `float` arguments, `XORPS` of two different registers, a write to `XMM6`, a read of an unwritten register, a RIP-relative load, `CVTSS2SI` (which rounds by MXCSR), `MOVQ` to an integer register, `JLE` after `COMISS`, a float compare without a conditional branch, a second jump after `CMP`, `SETcc` after `UCOMISS`, `CMOVcc` after `UCOMISS`, `SETcc` across a branch after `NEG`, `CMOVcc` at a join target, a second jump after `NEG`, `SETcc` partial write to an undefined GPR, and a 16-bit `CMOVcc`. Eight wrong signatures are rejected as well, for example a `double` result from a function that defines only a `float`.

Not covered: RIP-relative constants, packed operations, `SQRT`, `MIN` and `MAX`, the rounding conversions, stack frames and calls.

On Linux, `llvm-ml` assembled the fixture and `lld-link` linked it. The original machine code ran in-process through a Windows-ABI trampoline that loads each slot into both its integer and its XMM register. It matched the lifted C++ built with clang at `-O0`, `-O2` and `-O3` on every call, for both builds, and all rejection controls passed. The generated sources compile without warnings under clang-cl `/W4 /WX`. A Windows run of `cpu_aot_scalar_float` with ML64 and MSVC has not been recorded yet.

## Legacy SSE2 leaf recompilation

[The SSE2 leaf translator](../tools/cpu_sse2_aot.py) lifts bounded straight-line Windows x64 leaf functions covering 23 of the 64 forms in `docs/coverage/x64_zen2.tsv` group `SSE2`:

- Packed compare: `cmppd` (all eight `imm8` predicates, `xmm` and `m128` sources)
- Packed converts: `cvtdq2pd`, `cvtdq2ps`, `cvtpd2dq`, `cvtpd2ps`, `cvtps2dq`, `cvtps2pd`, `cvttpd2dq`, `cvttps2dq`
- MMX-domain source convert: `cvtpi2pd` (flat `m64` memory source only)
- Scalar convert: `cvtsd2si` (`r32`/`r64` destinations, `xmm` or `m64` source)
- Scalar double ops: `maxsd`, `minsd`, `sqrtsd` (merge into the destination's low qword)
- Fences: `lfence`, `mfence`
- Aligned moves: `movapd`, `movdqa` (register copies, loads, stores)
- Half-qword moves: `movhpd`, `movlpd` (`m64` load merge and `m64` store)
- Non-temporal stores: `movntdq`, `movntpd`, `movnti` (`r32` and `r64`)

Each lifted export takes `(word rcx, word rdx, word r8)` pointing to caller-owned flat buffers with 16-byte-aligned bases and returns `void`. The caller supplies live buffers for the whole invocation and keeps them private to the executing thread. This fixture uses the default MXCSR controls (masked exceptions, round-to-nearest-even, DAZ and FTZ disabled) and compares result bytes. Memory forms using a full 128-bit operand require a displacement divisible by 16. For transport and ABI glue the translator accepts 32/64-bit `mov` between the volatile scratch GPRs (`rax`, `r9`, `r10`, `r11`) and flat `[base + disp]` memory rooted in an argument, plus terminal `ret`.

### State model and explicit rejections

XMM registers `xmm0`..`xmm5` are represented as a portable C++ value struct (`struct Vec128 { std::uint64_t q[2]; }`) rather than MSVC intrinsics, so the generated C++ compiles under default x64 flags. Moves and compares transfer raw bits. The packed-conversion helpers use host floating-point operations and reproduce the SSE2 rounding and truncation rules written out in the code: `cvtpd2dq`/`cvtps2dq`/`cvtsd2si` round to nearest even, `cvttpd2dq`/`cvttps2dq` truncate, NaN or out-of-range results return the integer indefinite value (`0x80000000` or `0x8000000000000000`), `maxsd`/`minsd` return the second source on ties and unordered inputs, and `sqrtsd` returns the quieted source NaN or the real indefinite for negative inputs. `lfence` and `mfence` emit the corresponding host x64 SSE2 fence. All memory operands must be flat `[base + disp]` rooted in `rcx`, `rdx` or `r8`.

The translator explicitly rejects:

- the 36 SSE2-group forms reserved for sibling translators (`addpd`, `movdqu`, `pshufd`, `ucomisd` and the others in the fixture check), so each SSE2 form has a single owner;
- the five task forms that cannot be expressed through the leaf ABI: `cvtpd2pi`, `cvttpd2pi`, `movdq2q` and `movq2dq` use MMX register operands, and `maskmovdqu` stores through implicit `DS:rdi`;
- the `mm`-register source of `cvtpi2pd`, `cmppd` immediates above 7, non-legacy (VEX/EVEX) encodings, `lock`/`rep`/segment prefixes, nonvolatile vector registers (`xmm6`..`xmm15`), nonvolatile or argument GPR destinations, undefined registers, non-flat memory operands, missing terminal `ret` and instructions after `ret`.

```powershell
cmake --build --preset dev --target cpu_sse2_regs_original cpu_sse2_regs_original_lifted cpu_sse2_regs_altered cpu_sse2_regs_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_sse2_regs$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/sse2_regs.asm) builds 57 exports covering all 23 forms: eight `cmppd` predicate exports plus an `m128`-source case, register and memory forms of every scalar op and convert, with the six upper-zeroing converts run on dirty-seeded destinations so zeroing versus merging is observable, `cvtpi2pd` from memory, aligned load/store and register copies, `movhpd`/`movlpd` merges including unaligned `+1` memory loads and `+1` stores, unaligned `+1` `m64` sources for `maxsd` and `cvtsd2si`, non-temporal stores and a chained `op_pipeline` export. [The Windows test](../tests/cpu/sse2_regs_check.py) ran 1,168 differential cases across the 57 exports against both the native MASM DLL and an independent Python reference model, covering IEEE-754 specials (signed zero, subnormals, infinities, sNaN and qNaN with distinct payloads), integer-indefinite boundaries at 2^31 and 2^63, boundary conversion values and random patterns, with 32-byte pre/post guards on every buffer.

An `ALTER_IMM` variant changes the `cmppd` predicates of `op_cmppd_1` (1 to 5), `op_cmppd_3` (3 to 7) and `op_cmppd_6` (6 to 2); the altered translation must match the altered reference while diverging from the unmodified reference, which it did on 28 cases each. An output-mutation negative control flips one bit of `op_pipeline` and confirms the comparator fails. Rejection controls cover 10 alternate-encoding lift checks (non-`rax` `cvtsd2si`/`movnti` operands, displaced memory forms, standalone fences), 69 unit cases (36 reserved SSE2 forms, 7 MMX/implicit-address encodings, 1 `cmppd` immediate, 4 VEX/EVEX encodings, 21 ABI/state/addressing/prefix violations) and 12 end-to-end PE-patched DLL cases. In `tools/form_coverage.py`, `sse2_regs.asm` accounts for exactly its 23 SSE2 forms, advancing x64 Zen 2 `SSE2` group coverage from 14/64 to 37/64 and total x64 Zen 2 coverage from 549/948 to 572/948 (+23).

Not covered by this experiment: the 36 reserved SSE2 forms, MMX-domain operands and `maskmovdqu`, control flow or calls mixed with SSE2 state, nonvolatile `XMM6`..`XMM15` save/restore prologues, faults caused by violating the aligned-base or valid-buffer preconditions, and non-temporal caching effects, and title CPU code execution.

## MMX/SSE2 conversion and bridge leaf recompilation

[The MMX/SSE2 bridge translator](../tools/cpu_mmx_sse2_aot.py) lifts bounded straight-line Windows x64 leaf functions covering `cvtpd2pi`, `cvttpd2pi`, `movdq2q`, `movq2dq` (`SSE2`) and `emms` (`MMX`), alongside `movdqa`, `movdqu`, `movq` (between `mm`, volatile scratch `r64` `rax`/`r9`/`r10`/`r11`, and flat `[rcx|rdx|r8]` memory) and `ret`.

Call with an empty x87 stack, no live MMX values, and default MXCSR controls (round-to-nearest-even, all exceptions masked, DAZ and FTZ off). The leaf ABI compares the output bytes. Floating-point status flags and physical x87 tag transitions are outside this profile. The caller provides valid buffers and a 16-byte-aligned base for each aligned memory operand.

- `XMM0`..`XMM5` are modeled as `Vec128` (`std::uint64_t q[2]`) and `MM0`..`MM7` as `std::uint64_t mm[8]`.
- `mmx_active` is tracked across the function; any `MM` register touch activates it, `emms` clears it (and invalidates prior `MM` definitions) unconditionally, and `ret` rejects if `mmx_active` remains true.
- `cvtpd2pi` (round-to-nearest-even) and `cvttpd2pi` (truncate toward zero) lower to portable `<cmath>`/`<cstdint>`/`<cstring>` helpers returning `0x80000000` on NaN, `+/-Inf`, or out-of-`int32_t` range after rounding. `movq2dq` zeroes `xmm[127:64]`.
- Memory operands must be flat `[base + disp]` rooted in `rcx`, `rdx` or `r8`, memory stores are restricted to `r8`, and `cvtpd2pi`/`cvttpd2pi` `m128` and `movdqa` require `disp % 16 == 0`. VEX/EVEX encodings, `xmm6`..`xmm15`, nonvolatile/argument GPRs, undefined registers, prefixes and unsupported instructions reject.

```powershell
cmake --build --preset dev --target cpu_mmx_sse2_original cpu_mmx_sse2_original_lifted cpu_mmx_sse2_altered cpu_mmx_sse2_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_mmx_sse2$' -V
```

[The MASM fixture](../tests/cpu/mmx_sse2.asm) builds 12 exports covering register and displaced-memory `cvtpd2pi`/`cvttpd2pi`, `movdq2q`, dirty-seeded `movq2dq`, standalone `emms`, `movq` (`mm`/`r64`/`[rcx|r8]`), aligned `movdqa`, unaligned `movdqu` and a chained bridge re-entering MMX after `emms`. [The check script](../tests/cpu/mmx_sse2_check.py) verified 210 native/lifted/reference differential cases (including `2147483647.5`, `2147483648.0`, `-2147483648.5`, `-2147483649.0`, `+/-0.0`, `+/-Inf` and NaNs), an `ALTER_IMM` variant swapping `cvtpd2pi`/`cvttpd2pi` (diverging on 4 cases each), an output-mutation negative control, 8 alternate-encoding lift checks, 46 unit rejections and 9 PE-patched DLL rejections. Not covered: remaining `MMX`/`SSE2` forms outside these 5 ops, control flow/calls, x87/MMX aliasing across calls, and title CPU code execution.

## D3D12 pixel-position inputs

`pixel_position_centers` compiles a standalone Shader Model 6.0 pixel shader. Its inputs are `float4 TEXCOORD0` and `float4 SV_Position`, in the order emitted by the existing full-screen vertex shader. Red and green depend only on `SV_Position`. The test reads an 8×8 RGBA32F target with a 5×3 viewport at (2, 1). The CPU reference expects red and green to contain the absolute pixel center coordinates, with a half-pixel offset on each axis. The other 49 pixels retain the clear color. A second shader exchanges red and green and must first disagree at (2, 1), where the expected coordinates are (2.5, 1.5). [HLSL system-value semantics](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl-semantics)

The optional viewport in the pixel-case trailer is enabled by bit 1 of the flags byte. Four little-endian uint32 values follow in left, top, width, height order and must fit inside the 8×8 target. Existing optional NaN masks, when present, follow those fields. The fixture measures desktop screen-space coordinates. Scarlett register assignments require separate input-layout evidence.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_position_centers_(warp|hardware)$' -V
```

## Passing compute output directly to a pixel shader

`compute_to_pixel` translates a fixed `buffer_store_dwordx4` compute producer
and the scalar-buffer pixel color fixture. The producer dispatch writes four
words at byte offset 16 of a 256-byte default-heap buffer. The runner
transitions that resource from unordered access to constant-buffer state
and waits for the dispatch fence before binding its GPU address to the
pixel shader. Only the final RGBA32F render target is read back.

Two producer variants write different exactly representable colors. The
pixel shader is shared, and the CPU comparison checks every channel in
the 64×64 target for each palette. The stale-binding control dispatches
both producers and binds the earlier buffer while keeping the later
reference. The first pixel must differ.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^compute_to_pixel_(warp|hardware)$' -V
```

## Offline pixel shader recompilation

The 64-bit scalar set and clear forms are s_bitset0_b64 and s_bitset1_b64.
Run their standalone cases with `ctest --preset dev --no-tests=error -R '^pixel_aot_bitset64_'`.
They read the destination SGPR pair, mask the source index to six bits and
clear or set that bit while preserving the other 63 bits and SCC. The fixture
exports both words and SCC at indices 0, 31, 32, 63 and 64 with both incoming
SCC values. It also carries the exact all-ones NaN-shaped pair from #680 at
index 31, where setting the bit must retain 0xFFFFFFFF, and reads the index
from either destination word. A wrong reference that applies index 31 to the
high word, and a reference that re-reads an aliased index after the destination
write, must both report pixel_mismatch. TomBox executes both 64-bit forms over
boundary indices, and Leonida's 64-bit value and GPU suite covers SCC and word
selection. Run
ctest --preset dev --no-tests=error -R '^pixel_aot_blend_scalar_(warp|hardware)$' -V.
On Windows build 26300.10075, the 30 positive draws and three negative controls
passed on WARP 10.0.26100.10068 and an AMD Radeon RX 9060 XT (driver
32.0.32029.2010), with all 4,096 pixels matching on every positive case. No
NVIDIA adapter was available locally. The D3D12 debug layer was unavailable.
[Leonida scalar validation](https://github.com/ItsNotRexed/Leonida/blob/4eb6155d/docs/shader_validation.md#L265) ·
[TomBox SOP1 execution](https://github.com/omonkeyz/xbox-emulator/blob/2be7211/crates/tombox-xshader/tests/exec.rs#L932-L936) ·
[AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

The floating-point predicate draw uses one `--check-pixel-batch` process per
compiled shader group. All 20 reference draws still run. For the final group,
the finite-instead-of-NaN control produces a mismatch inside the batch and the
following valid draw must pass. The fixture checks each status in order and
checks the process exit code. This leaves four device creations for the
predicate groups in place of 21, without changing the number of DXC calls.

The 32-bit integer, 16-bit integer and SDWA predicate draws use the same
runner, one batch per compiled group, with the same references. The
signed-against-unsigned control of the first `i32` e32 group and the
inverted-unordered control of each SDWA group run inside their batch as
expected mismatches, each followed by a passing draw. The three 16-bit
`must_mismatch` controls, two of them on mutated shaders, keep one single-case
draw each. These three suites
go from 108 device creations to 23, and DXC compiles one shader fewer, because
the signed/unsigned control now reuses the group's compiled shader.

The remaining multi-case `--check-pixel-case` loops across `draw-core`,
`draw-predicate-rounding`, `draw-blend-scalar`, `draw-e64`, `draw-integer-nc`,
`draw-bfm`, `three-way` and the full-RGBA `buffer-color` pass now group cases
per compiled shader through `run_pixel_batch()`, preserving every reference case
and expected mismatch control (`--check-pixel`, `--check-pixel-quad-lod`,
`--dual-pixel` and `--check-pixel-case-rg` keep their single-draw paths).
Across all eight modes, total renderer launches per backend drop from 494 to
263 (`draw-core`: 127 to 65; `draw-predicate-rounding`: 147 to 74;
`draw-blend-scalar`: 126 to 61; `draw-e64`: 57 to 50; `draw-integer-nc`: 12 to
3; `draw-bfm`: 15 to 3; `three-way`: 4 to 3; `buffer-color`: 6 to 4).

`draw-predicate-rounding` grew back to 80 renderer launches and 284 cases.
With the D3D12 debug layer on an RTX 3070, each launch costs about 0.25 s of
start-up and each case about 0.03 s; the hardware entry took 31.6 to 33.2 s
there on an idle machine (up to 45 s under load), and an RTX 3080 reached its
60 s timeout
([issue #448](https://github.com/R0Y00/Xbox2Windows/issues/448)). The
comparison and class draws (`v_cmp_*` f16, i16, i32, u32 and SDWA groups and
`v_cmp_class`) now run as their own mode, `draw-comparisons`, registered as
`pixel_aot_comparisons_{hardware,warp}` with the same 60 s timeout. The cases,
references and expected-mismatch controls are unchanged; only the CTest entry
that runs them moved.

The first raw buffer_load_dword path remains a proposal while
[issue #319](https://github.com/R0Y00/Xbox2Windows/issues/319) is open. The
[binding contract](pixel_mubuf_contract.md) records the separate DW0/DW1
address masks, the 14-bit stride field, OOB_SELECT = 3 whole-DWORD bounds, a
bounded space1 title-lookup table for ps_6_0 and a separately observable host
binding failure for an unmapped title address. The tests keep an aligned
partial tail apart from rejected unaligned inputs. The gfx1013 bounds rule
remains open. No vector memory instruction is accepted by this change.

#The bitwise fixture uses AND, OR, XOR, NOT, BFI and three reversed shifts. Two constant sets exercise counts above 31. Exact mantissa bits distinguish the arithmetic and logical right shifts in GPU readback.

The scalar quad reduction forms are s_quadmask_b32 and s_quadmask_b64. Each
output bit is the OR of one four-bit group. The b64 result has sixteen quad
bits in the low word and zero in the high word, and both widths set SCC from
the complete result. s_bitreplicate_b64_b32 expands every source bit into two
adjacent destination bits and preserves SCC. The fixture covers sparse and
alternating groups, the null word, bits 0 and 31 and SCC in both incoming
states. Negative references cover an AND-within-group, a missing second copy,
the wrong source word, a quad in the high destination word and recomputed SCC.
Run ctest --preset dev --no-tests=error -R '^pixel_aot_blend_scalar_(warp|hardware)$' -V.
On Windows build 26300.10075, the 37-case fixture passed on WARP
10.0.26100.10068 and an AMD Radeon RX 9060 XT (driver 32.0.32029.2010)
with all 4,096 pixels matching on every positive case and every negative
control mismatching. The D3D12 debug layer was unavailable.
[Leonida scalar semantics](https://github.com/ItsNotRexed/Leonida/blob/4eb6155d/tests/semantic/test_salu_quadmask_bitreplicate.py) ·
[Leonida RDNA2 measurements](https://github.com/ItsNotRexed/Leonida/pull/1685) ·
[AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

## Pixel color without a texture sample

For an unsampled pixel program, a matched P1/P2 interpolation in the entry mask can supply attr0.x/y/z/w from a four-component TEXCOORD0. The profile binds entry v0/v1 to the barycentric pair and the observed m0 source to the interpolation base. Each completed P2 produces a distinct value, including when the destination overwrites v0. The test vertex shader varies all four channels across a full-screen triangle. CPU references check all four output words at 1,024 pixel centers; an incorrect channel at an interior pixel must fail. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_attribute_'`.

A local scan found 35 additional single-exit unsampled pixel records (12 different native instruction streams). All 35 compiled to DXIL and passed readback on RTX 4060 Laptop 32.0.16.1656 and WARP 10.0.26100.9818. The two constant-buffer records used two buffer inputs, giving 37 draws per adapter. This is a controlled TEXCOORD0 binding with linear screen-space test values.

In a separate two-exit population, 2,151 records read four words at byte offset zero through a zero-extended s9 address. Tracking that register range to its first direct use found 912 single-word, five two-word, 1,222 four-word and one eight-word scalar buffer reads. Eleven first uses remained unresolved. These counts identify consumers of a descriptor in the native program; they do not establish its original bound contents or the output of those programs.

Use `--no-texture` in place of `--sampler-words` for a program that writes MRT0 without sampling. For a constant-buffer program, `--texture-sgpr` identifies the entry register holding the low descriptor-table address and `--constant-descriptor` gives the byte offset of the four-word descriptor in that table. Offset zero is valid with this profile. At a tested call site the program copies entry s9 to the low address register, sets the high word to zero, fetches the descriptor and loads color words. Bind the test buffer at b0. Other programs can copy s9 to m0 for interpolation. Select the binding from the program's use of the register.

An RG-only compressed MRT0 export produces a two-component output. Match it with a red/green render-target write mask. The Windows fixture clears all four channels to -1, draws with two different constant buffers and compares every channel, including preserved blue and alpha. A stale reference must fail. A second draw profile checks the four-component export with all channels enabled. To reproduce:

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_buffer_color_' -V
```

A local audit against the 3310cb8 baseline retained the four sampled profiles and added six zero-sample profiles (entry table register 8 or 9, interpolation register 24, and no constant descriptor or a descriptor at byte 0 or 32). Of 26,503 records, 334 pixel records compiled with DXC `-Gis -T ps_6_0` and created D3D12 pipelines, up from 2. These represent 333 distinct containers and 21 distinct native instruction streams. Of the 332 additional records, 315 exported immediate values and 17 read the constant buffer; 298 wrote RG and 34 wrote RGBA. Each was drawn on an RTX 4060 Laptop (32.0.16.1656) and WARP (10.0.26100.9818). Reference colors for the new records were computed from the decoded operands, test buffer words and truncation to binary16. Two buffer inputs per constant-buffer record plus the original copy and day/night cases gave 352 draws per adapter. Each draw matched all 4,096 float4 pixels. These results use controlled buffers and render-target masks.

### Pixel programs with two exits

For FSSM version 0x101 the pixel extractor reads the 64-bit code offset, byte count and section end at 0xC0, 0xC8 and 0xE8. Instructions start 40 bytes after the code offset. The end must agree with offset plus count. For the unset end sentinel the extractor also checks for four padding words at the computed end. It strips aligned trailing `s_code_end` words inside that extent and requires a terminal `s_endpgm`. A marker in the following metadata cannot extend or terminate the code. The field interpretation agrees with the [independent FSSM cross-check](https://github.com/ItsNotRexed/Leonida/blob/dcd8ecf10b138566d567020562b7afd853a89024/docs/fssm_crosscheck.md).

Among 6,189 pixel records previously rejected by the unique-marker rule, 5,465 had two aligned end markers, 682 had three, 38 had four and four had five. The largest layout had two markers 20 bytes apart (3,561 records). In the supported two-exit layout, a direct branch targets the block immediately after the early exit. The extractor uses LLVM instruction boundaries to follow both conditional successors, preserves both exits and permits skipped alignment NOPs. Unresolved transfers, jumps into instruction operands or padding, and disconnected executable blocks are rejected. This verifies a code span; lowering branch or resource semantics is a separate step.

The source-built binary checks include a literal word resembling a terminator, a metadata terminator, conflicting extents, a branch into a literal and a disconnected exit. Run with LLVM 15:

```bash
python3 -B tests/pixel_aot_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

`tools/pixel_aot.py` extracts one named pixel program from a local archive, decodes gfx1013 instructions with LLVM and emits HLSL. DXC compiles the result to desktop DXIL before the draw. The lowerer tracks register writes across whole-quad interpolation of attribute 0, a scalar texture-descriptor load, a 2D RGBA sample, packed round-toward-zero binary16 conversions and one compressed color export, or an uncompressed export of four distinct computed or sampled registers whose result may instead be returned as `uint4` for a lossless integer readback. Unknown instructions, mismatched bindings and reads before the required wait cause translation to fail. [LLVM disassembler API](https://llvm.org/docs/doxygen/group__LLVMCDisassembler.html) · [Leonida public format reference](https://github.com/ItsNotRexed/Leonida/blob/56f023a10a726da20702d4e121656ceda2391d93/tools/fssm_unpack.py)

The integer readback profile declares the primary constant buffer as `uint4`. Integer consumers read its words directly, and floating-point consumers reinterpret the same bits. The byte layout is unchanged. The constant-index bitset draw checks copying `0xffffffff` through a VGPR and overwriting the destination with a floating-point result. On RTX 4060 Laptop (driver 32.0.16.1656), the previous float-typed load returned `0x7fffffff` for the immediate-index-31 copy; the integer load preserves `0xffffffff`. [Original report and independent reproduction](https://github.com/R0Y00/Xbox2Windows/pull/680#issuecomment-7099515543)

For normalized compressed whole-pair exports, the Python API can return `(source, mask)` via `return_write_mask=True`. The caller applies the returned RT0 mask (RG = `0x3`, BA = `0xC`, RGBA = `0xF`) in the PSO. Disabled components in the emitted `float4` are placeholders. The regular API still returns `float2` for its existing RG form and rejects a BA-only export. Because disassembly text can hide raw enable-bit differences, this API requires known whole-pair input. The draw fixture uses distinct clear sentinels and checks that a full PSO mask or incorrect enabled-channel values fail. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_compressed_export_(warp|hardware)$' -V`.

The LLVM 15 binary fixture for #361 changes only the four EN bits of an assembled gfx1013 export and checks all 16 raw values. Reassembling the printed text keeps a packed pair when its first printed operand is enabled: raw 0x2 and 0xA reassemble as 0x0, and 0x5 reassembles as 0xF. Fully disabled source fields reassemble as zero. The normal lowering accepts 0x3, 0xC and 0xF through its explicit RT0-mask API. Other masks are rejected. These checks characterize the encoding and LLVM 15 behavior; they establish no gfx1013 channel-write semantics for non-pair masks.

The explicit RT0-mask API also accepts uncompressed binary32 and integer exports with any nonzero per-channel mask. The caller applies the result to the target. Disabled channels keep the clear value, and enabled channels preserve their binary32 or raw integer data. Partial uncompressed exports require the opt-in; the `pixel_aot.py` CLI accepts `--rt0-write-mask` and returns `rt0_write_mask` in its JSON summary for the caller to apply to the PSO. Partial uncompressed exports are refused without that flag. The source-built round trip and `pixel_aot_uncompressed_export_*` plus `pixel_aot_uncompressed_integer_export_*` draws cover individual and disjoint channel masks, wrong PSO masks and wrong enabled-channel reference values.

The caller identifies the entry registers for the texture address and interpolation base, and supplies the four sampler descriptor words to match. The generated pixel shader uses `TEXCOORD0`, texture `t0` and sampler `s0`. The test binds a point-clamp sampler, one mip and a full-screen triangle. These bindings are an explicit specialization. The test measures finite UNORM inputs under full pixel coverage. Execution with a title's original pipeline state, helper-lane behavior at triangle edges and other texture formats require separate validation.

With the entry execution mask restored, `image_sample` also accepts `dmask:0x7` for a 2D RGB sample. It writes three consecutive VGPRs and preserves the following register. The LLVM 15 fixture presets that register to 0.5 and exports it as alpha alongside the sampled RGB. On WARP and RTX 5080, all four channels of 4,096 pixels matched the CPU reference bitwise. A reference that expects the sampled alpha of 1.0 instead fails first at (0, 0) on both adapters. [AMD RDNA 2 image instruction reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

For a guest shader whose execution width is known to be 32, the pixel lowerer accepts `wave_size=32` and tracks the low-half EXEC save, `s_wqm_b32`, and the matching `s_and_b32` or `s_mov_b32` restore. The caller supplies the width from the guest profile. The low-half forms are rejected with an unspecified width or `wave_size=64`. The LLVM 15 binary checks assemble and decode both restore forms and compare the emitted HLSL with the established full-width fixture.

For ordinary 2D sampling, masks `0x1` through `0xf` place enabled channels into consecutive destination VGPRs in RGBA order. A one-channel mask names one VGPR. In the `0xb` draw, R, G and A go to three adjacent VGPRs while the next VGPR retains a 0.25 sentinel. A control that overwrites the sentinel must fail the same readback. The primary texture descriptor can move with `--texture-offset` (default 0, aligned to 32 bytes). It may sit next to the second texture descriptor, including with the second descriptor at offset zero. The translator rejects overlap with either texture or an active constant descriptor. The existing LOD-zero and comparison-sampling profiles retain their own mask requirements.

For a single 2D RGBA sample, `image_sample_lz` lowers to `SampleLevel` at level 0 of the bound texture view. Both halves of an interpolation must execute under the same tracked mask. Entry-mask interpolation supplies the covered lanes for explicit LOD, while implicit sampling requires whole-quad coordinates. The BC7 fixture draws an 8×8 texture into a 4×4 viewport with different colors at mip levels 0 and 1. On WARP and RTX 5080, the explicit sample matched level 0 across 16 pixels and 64 channels. The implicit sample chose level 1 and failed the level-0 reference at (0, 0). [HLSL SampleLevel](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl-to-samplelevel)

For the same single-texture RGBA profile, `image_sample_l` takes `(u, v, lod)` and lowers to `SampleLevel`; `image_sample_b` takes `(bias, u, v)` and lowers to `SampleBias`. Explicit LOD also accepts defined numeric coordinates, including constant-buffer values. Bias sampling still needs whole-quad UVs for derivatives. Both new forms keep the full `dmask:0xf` requirement. The `pixel_aot_sample_lod_{warp,hardware}` tests compare mips with distinct colors, check an 8 by 8 UV ramp and exercise constant-buffer coordinates without whole-quad mode. Substituting implicit sampling for LOD zero, changing the explicit LOD or bias from one to zero, or swapping the bias sample's UVs must each cause a mismatch.

Depth-comparison sampling covers one form: `image_sample_c_lz vD, v[a:a+2], ... dmask:0x1 dim:SQ_RSRC_IMG_2D` and its NSA spelling `[vR, vU, vV]`. In RDNA 2 the reference comes before the coordinates. The result lowers to `tex.SampleCmpLevelZero(samp, float2(u, v), reference)` on `Texture2D<float> tex : register(t0)` and `SamplerComparisonState samp : register(s0)`. The four sampler words must still match the caller binding. `comparison_sampler_state()` decodes them, and the generated HLSL names the desktop state it needs in a comment. The compare function comes from word 0 bits 14:12, in the same order as `D3D12_COMPARISON_FUNC` minus one, and the result is `reference OP texel`. One point or bilinear XY filter maps to `D3D12_FILTER_COMPARISON_MIN_MAG_MIP_POINT` or `..._MIN_MAG_LINEAR_MIP_POINT`. Other values are refused, each with its own message:
- X/Y addressing other than `CLAMP_LAST_TEXEL`;
- anisotropy, unnormalized coordinates, `MC_COORD_TRUNC` or `FORCE_DEGAMMA`;
- a min/max filter mode;
- a non-zero `MIN_LOD` or LOD bias;
- different min and mag filters;
- mip filter 3 or word 2 bits 28–31;
- word 3 bits 12–29, unnamed on gfx10 (bit 29 is `UPGRADED_DEPTH` on GFX8);
- `TRUNC_COORD` 0 with point filtering, or 1 with bilinear filtering.

`TRUNC_COORD` (word 0 bit 27) is part of the binding contract. On gfx10 it applies to X and Y for every filter ([`ac_gpu_info.h`](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-23.3.6/src/amd/common/ac_gpu_info.h)). With 1 the texel-space coordinate is truncated, so point sampling takes the floor, which is the D3D rule ([D3D11.3 §7.18.7](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm#7.18.7%20Point%20Sample%20Addressing)). With 0 the coordinate is rounded to nearest even at six fraction bits and point sampling then drops the fraction ([Mesa d573d1d8](https://gitlab.freedesktop.org/mesa/mesa/-/commit/d573d1d82524b8a2e5f56938069cabc0f0176a0e)), so it can take the next texel when the coordinate is up to 1/128 texel below an edge. The accepted pairs are the ones RADV programs on gfx10 by default ([`radv_sampler.c`](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-23.3.6/src/amd/vulkan/radv_sampler.c)): point with 1 and bilinear with 0. RADV clears the bit for DXVK and vkd3d-proton ([`00-radv-defaults.conf`](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-23.3.6/src/util/00-radv-defaults.conf)), so point with 0 is a setting drivers use; it is refused here. Bilinear results are in the contract where the texel-space position (u × width − ½) is a multiple of 1/64, where that rounding is exact and D3D's 8-bit subtexel precision is exact too.

The reference and the texels are in the contract within [0, 1], ends included. D3D quantizes the reference to the texture format and clamps it to the format range before comparing ([`sample_c`](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm#22.4.19%20sample_c)). The lowering does not decode the image format and the draws bind `R32_FLOAT`, so references or texels outside [0, 1], and NaN, are outside the contract.

Fields that cannot change a 2D LOD-zero result under this profile are not read: `CLAMP_Z`, `DISABLE_CUBE_WRAP`, `Z_FILTER`, the anisotropy threshold and bias, `MAX_LOD`, `PERF_MIP`/`PERF_Z`, the border colour and word 0 bit 31. A program that mixes comparison and ordinary sampling, or uses a second texture, is refused, because both would need `s0`. Field positions follow `SQ_IMG_SAMP_WORD0`…`3` in Mesa [`src/amd/registers/gfx10-rsrc.json`](https://gitlab.freedesktop.org/mesa/mesa/-/blob/mesa-23.3.6/src/amd/registers/gfx10-rsrc.json), and the enum values follow Linux `navi10_enum.h` (`SQ_TEX_DEPTH_COMPARE`, `SQ_TEX_CLAMP`, `SQ_TEX_XY_FILTER`). [HLSL SampleCmpLevelZero](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl-to-samplecmplevelzero)

The fixed root signature of `tests/pixel_aot_vs.hlsl` has one ordinary static sampler, so `offscreen_check --check-pixel-compare` uses its own root signature with a descriptor-heap sampler. The case file supplies the filter and compare function from the decoded state. `tests/pixel_compare_check.py` draws an 8×8 `R32_FLOAT` texture into a 16×16 target, so bilinear weights are 1/4 and 3/4 and the expected results are exact multiples of 1/16. Three source-built programs cover all eight compare functions with both filters:
- the reference from constant word 0, equal to some texels, so `EQUAL` and the `LESS`/`LESS_EQUAL` split are visible;
- the reference interpolated from `attr0.x`, which varies per pixel and is never equal to a texel;
- the reference computed by `v_fma_f32` from constants, in the NSA form.

Three more programs move u and v by a constant in whole-quad mode before the sample, all eight compare functions each:
- point filtering 1/256 texel below an edge at every even pixel: truncation takes the texel below, rounding at six fraction bits the one above (96 of 256 pixels differ for `LESS`);
- point filtering exactly on an edge at even pixels: the floor takes the texel above;
- bilinear filtering on texel centres (one weight 0) and halfway between them.

Two more cover the ends of the interval: constant references 0 and 1 on a texture with texels equal to 0 and 1, both filters, so the strict and inclusive functions differ.

The expected images come from an independent CPU model: compare each texel, clamp to the edge, then weight bilinearly. The sampler words are written from the field layout in the test, not by the decoder.

On Windows 11 build 26200.9974 with the D3D12 debug layer enabled, NVIDIA GeForce RTX 3070 (driver 621.01) and WARP each matched all 104 draws (256 pixels, four channels, bitwise) with no debug messages. On each adapter, all 18 negative controls failed at pixel comparison:
- expected images for `GREATER` and `GREATER_EQUAL` (and `LESS_EQUAL` for the constant reference) against a `LESS` descriptor;
- bilinear depths compared once instead of compared texels filtered (192 and 159 of 256 pixels differ);
- a point sampler in place of bilinear;
- recompiled HLSL that ignores the reference;
- recompiled HLSL that swaps u and v;
- below an edge, the image for `TRUNC_COORD` 0, and on an edge, the texel below;
- on texel centres, the image without the offset;
- at references 0 and 1, the inclusive function against the strict one.

The draws show the desktop side. They show that the accepted point rule is the floor and that D3D does not round at six fraction bits; they cannot show the console rule.

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_compare_' -V
```

```bash
python3 -B tests/pixel_compare_check.py
python3 -B tests/pixel_compare_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

Not covered: implicit-LOD `image_sample_c`, `_c_l`, `_c_b`, `_c_o`, `_c_d`, `image_gather4_c*`, cube and array shadow maps, border-colour addressing, references or texels outside [0, 1] (outside the contract above) and the two refused `TRUNC_COORD` pairs. How many archive records use these forms has not been measured.

For a shader that reads constants, pass `--constant-descriptor OFFSET` to identify its four-word descriptor in the same table as the texture descriptor. The desktop test binds 256 bytes at `b0`. A four-word read may span two HLSL constant vectors. Arithmetic support includes log2, exp2, reciprocal, rounding up (`v_ceil_f32`) and to nearest even (`v_rndne_f32`, lowered to HLSL `round`, which DXC emits as `Round_ne`), two-operand min/max, three-operand minimum, maximum and median (`v_min3_f32`, `v_max3_f32`, `v_med3_f32`), addition, multiplication, subtraction in either operand order, legacy zero-preserving multiplication, multiply-add, destination accumulation and saturation. The three-operand forms are emitted as nested HLSL `min` and `max`, with the median as `max(min(a, b), min(max(a, b), c))`. Each result is one of its operands, so finite inputs involve no rounding; NaN operands are not measured and may differ from the hardware's NaN rules. `v_mov_b32` copies a sample component, a loaded constant, an earlier result or an inline or literal constant, and rejects interpolation state, packed values and source modifiers. The lowerer rejects moves and floating-point arithmetic writes to the destination registers of an outstanding sample until its result is ready. With two samples in flight, `vmcnt(1)` permits moving the older result while all four destination registers of the newer sample remain protected. The dual-texture fixture uses that move in the rendered blend. As in the other operands, an integer inline constant is a bit pattern, not a converted value. Ordered and unordered `v_cmp_*_f32` comparisons, including the always-false and always-true predicates, but excluding class and exec-writing forms, become HLSL booleans, and `v_cndmask_b32` selects between two numeric operands with one. The lowerer accepts a mask in `vcc` or a scalar register under either wavefront-width spelling and rejects a select whose mask it did not see a comparison write. For `nlg`, equality or an explicit NaN check on either operand preserves the unordered case in DXIL. The earlier ordered comparisons produced a mismatch on the [reported RX 9070 XT driver](https://github.com/R0Y00/Xbox2Windows/issues/179). The pixel draw checks cover all 16 truth tables with less-than, signed-zero equality, greater-than and a quiet NaN on either side. Replacing a NaN with an equal finite operand must fail against the saved unordered reference. Compile arithmetic shaders with DXC `-Gis`. Transcendental precision and multiply-add rounding may vary by device. [AMD RDNA instruction reference](https://gpuopen.com/wp-content/uploads/2019/08/RDNA_Shader_ISA_5August2019.pdf) · [HLSL mad precision](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/mad)

`s_movk_i32` sign-extends its 16-bit immediate, and the translator folds the result. One draw checks the rule end to end on the GPU. It moves 0x8000 and 0x7fff, extracts bytes 1 and 3 of each with `s_bfe_u32`, and widens each byte as a binary16 subnormal scaled by 2^24 to the exact integer, so the draw exports 255, 128, 127 and 0. A reference for zero extension (byte 3 of 0x8000 as 0), and one for treating 0x7fff as negative (byte 3 as 255), must each report a mismatch.

Scalar ALU support also covers the 64-bit SOP1/SOP2 family (`s_cmov_b64`, `s_not_b64`, `s_brev_b64`, `s_and_b64`, `s_or_b64`, `s_xor_b64`, `s_andn2_b64`, `s_orn2_b64`, `s_nand_b64`, `s_nor_b64`, `s_xnor_b64`, `s_lshl_b64`, `s_lshr_b64`, `s_ashr_i64`, `s_bfm_b64`) and the 32-bit scalar bitfield extracts (`s_bfe_u32`, `s_bfe_i32`), complementing the 32-bit scalar ALU, comparison and full SCC tracking scope of PR #257 without overlapping it. Each 64-bit scalar result becomes an HLSL `uint2` local bound across an even-aligned SGPR pair (`s0`..`s105`), and each 32-bit extract becomes a `uint` local; constant-buffer inputs are read with `asuint` and vector operands consume scalar results through `asfloat`. Inline decimal integers sign-extend to 64 bits, while 32-bit hex literals zero-extend for `b64` operations and sign-extend for `s_ashr_i64`. Operations that update SCC in RDNA hardware (`s_not_b64`, the 64-bit bitwise and shift forms, `s_bfe_u32` and `s_bfe_i32`) track a nonzero-result boolean so `s_cmov_b64` selects between the source and existing destination pair. The complement forms invert the 64-bit AND, OR or XOR, and each sets SCC when its result is nonzero. The draw takes their register-pair results into the hash and probes SCC in both states: `s_nand_b64 -1, -1` and `s_nor_b64` with -1 are zero, and `s_xnor_b64` of the alternate pair with -1 is zero only in the second constant set. Replacing each with its uncomplemented form, or the zeroing operand of the `s_nor_b64` probe with 0, must produce a pixel mismatch; `s_brev_b64`, `s_bfm_b64` and `s_cmov_b64` preserve SCC, and `s_wqm_b64` and the exec-restoring `s_and_b64` clear it. Pending scalar loads, unaligned or out-of-range pairs, descriptor/exec/vcc/VGPR operands, untracked SCC reads, exec save/restore ops (`s_*_saveexec_b64`) and out-of-scope scalar ops are rejected.

`s_bfm_b32` (issue #596) builds a bitfield mask with the 32-bit scalar ALU table: `D = ((1u << (S0 & 31)) - 1u) << (S1 & 31)`, so a count of 0 or 32 gives an empty mask and the offset wraps modulo 32. `s_bfm_b64` uses the same formula on 64 bits with six-bit counts (`s_bfm_b64` helper, two 32-bit halves). Neither writes SCC. The RDNA 2 ISA guide lists the five- and six-bit count fields in SOP2 opcodes 36 and 37. The HLSL masks both counts explicitly, so no 32-bit shift reaches 32. The standalone integer-export draw (`pixel_aot_bfm_warp` or `pixel_aot_bfm_hardware`) covers counts 0, 1, 31, 32, 33, 63 and 64 and offsets that wrap; one control narrows the count mask to four bits and one swaps count and offset, and each must produce a pixel mismatch. That `s_bfm_b32` leaves SCC unchanged is checked only statically: a following `s_cselect_b32` still selects on the earlier compare. No draw covers it. Dropping the mask entirely is not observable on the GPU (DXIL masks shift amounts itself); that variant is caught by the static lowering check. Vector, pair or out-of-range operands, a missing operand and an unset source are rejected. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

`v_swap_b32 vD, vS` (optional `_e32`; VOP1, VGPR operands only, llvm-mc 15 rejects an SGPR source) exchanges the old values of two VGPRs. Every tracked VGPR names an immutable HLSL temporary, so the lowering exchanges the two translator entries and emits no code; both values are read before either is written, so `v_swap_b32 v1, v1` is an accepted no-op. Both registers must hold tracked numeric values: an untracked register, a barycentric input, a register with an outstanding sample, `v256`, an SGPR or constant operand, `_e64`, DPP, SDWA and modifiers are rejected with a `ValueError`. The integer-export draw (`pixel_aot_swap_warp` or `pixel_aot_swap_hardware`) moves four distinct binary32 words into v18 to v21, runs two swaps, a chained swap and a self swap, and compares the exported words with a Python reference (the draw does not cover non-normal words, which pass through float VGPRs). Four wrong lowerings must each produce a pixel mismatch: a one-way copy, two sequential copies without a snapshot, a reordered chain and a self swap lowered as a copy. Reproduce with `python -B tests/pixel_aot_check.py draw-swap <dxc> <offscreen_check> <adapter>` or `ctest --preset dev -R "^pixel_aot_swap_"`. This establishes the exchange on a desktop GPU, not the console's behavior. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

`s_brev_b32` reverses the 32 bits of its scalar source (`D = reversebits(S0)`) and, like `s_brev_b64` above, does not write SCC; it sits in the 32-bit scalar ALU table with no SCC expression. The standalone integer-export draw (`pixel_aot_brev_warp` or `pixel_aot_brev_hardware`) reverses scalar words 0, all ones, the top bit, bit 0, `0xaaaaaaaa` and `0x12345678` (plus an inline literal) and compares them with an independent bit-loop reference. Between an `s_cmp_eq_u32` and an `s_cselect_b32` it runs two reversals, and the select channel proves SCC survived both outcomes of the compare. Three controls must produce a pixel mismatch: `reversebits` replaced by `~` in the HLSL, an `s_not_b32` (which writes SCC) inserted before the select, and the `s_brev_b32` replaced by `s_not_b32`. A VGPR source or destination, a pair operand, a missing or extra operand, an out-of-range or unset register and a pending scalar load are rejected, and so is a select without a prior compare. The form is counted as SOP1 coverage in both reference lists. Only the synthetic fixture is measured, not a real title shader. Reproduce with `ctest --preset dev -R "^pixel_aot_brev_" -V`.

`s_ff0_i32_b32`, `s_ff1_i32_b32`, `s_ff0_i32_b64` and `s_ff1_i32_b64` return the least-significant matching bit index or `0xffffffff` when no bit matches. All four preserve SCC. The standalone draw checks the no-match cases, the word boundary, and a deliberately omitted high word. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_first_bit_'`.

The SDWA forms of the 16 `v_cmp_*_f32` predicates are lowered when both `src0_sel` and `src1_sel` are `DWORD`. With whole-dword selections they compare the operands exactly as the e32/e64 forms do; the translator emits identical HLSL for both, and a control asserts it. A byte or word selection would compare a zero-extended field as binary32 and is refused. The predicate draws are repeated with the SDWA compares for all four groups and the same five input pairs, including NaN on either side. A reference that inverts each group's unordered result must report a mismatch.

Binary32 floating-point vector comparisons (`v_cmp_{f,lt,eq,le,gt,lg,ge,o,u,nge,nlg,ngt,nle,neq,nlt,tru}_f32` in e32, e64, and whole-dword SDWA forms) evaluate the 16 IEEE-754 relations against scalar and vector operands, including subnormals, signed zeroes, infinities, and quiet/signaling NaNs. The e32 (VOPC) encoding requires destination `vcc` or `vcc_lo`; SGPR destinations (`s52`, `s[52:53]`) are accepted on suffix-less and explicit `_e64` (VOP3) encodings. Source modifiers (`neg`, `abs`) are accepted on e64 operands (`-s44`, `|v18|`, `-|s44|`), while `clamp` and `op_sel` remain rejected on comparisons. Rejection controls verify that untracked source registers, SGPR destinations on explicit e32, and sub-dword SDWA selections (`src0_sel:WORD_0`, `src1_sel:BYTE_2`) raise `ValueError`. The synthetic fixture `f32_compares()` verifies all 16 predicates across e32, e64, and whole-dword SDWA, validating them against an exact reference oracle (`f32_compares_reference()`). Mutating predicates or modifiers produces a mismatch.

Scalar comparison lowering covers 18 RDNA2 SOPC instructions: 32-bit signed comparisons (`s_cmp_eq_i32`, `s_cmp_lg_i32`, `s_cmp_gt_i32`, `s_cmp_ge_i32`, `s_cmp_lt_i32`, `s_cmp_le_i32`), 32-bit unsigned comparisons (`s_cmp_eq_u32`, `s_cmp_lg_u32`, `s_cmp_gt_u32`, `s_cmp_ge_u32`, `s_cmp_lt_u32`, `s_cmp_le_u32`), 64-bit scalar comparisons (`s_cmp_eq_u64`, `s_cmp_lg_u64`), 32-bit bit comparisons (`s_bitcmp0_b32`, `s_bitcmp1_b32`), and 64-bit bit comparisons (`s_bitcmp0_b64`, `s_bitcmp1_b64`). Each comparison sets the scalar condition code boolean (`scc`) to the comparison outcome, making it available for subsequent conditional moves (`s_cmov_b64`). Signed integer comparisons cast operands with `asint`, 64-bit comparisons inspect both low and high dwords across even-aligned SGPR pairs, and bit comparisons mask by `src1 & 31u` (selecting low or high dword via bit 5 for 64-bit forms). Unaligned register pairs, out-of-range registers (`s106`), and unsupported comparison types (`s_cmp_gt_u64`, `s_cmp_eq_i64`) are rejected.

Scalar ALU instructions act on uniform 32-bit values: immediates, loaded constant words and earlier scalar results. Each result becomes an HLSL `uint` local, and a vector move or operand reads it through `asfloat`. Supported forms are `s_add_u32`, `s_sub_u32`, `s_add_i32`, `s_sub_i32`, `s_absdiff_i32`, `s_mul_i32`, `s_min_u32`, `s_max_u32`, `s_min_i32`, `s_max_i32`, `s_and_b32`, `s_or_b32`, `s_xor_b32`, `s_andn2_b32`, `s_orn2_b32`, `s_not_b32`, `s_nand_b32`, `s_nor_b32`, `s_xnor_b32`, `s_lshl_b32`, `s_lshr_b32`, `s_ashr_i32`, the scaled addition family (`s_lshl1_add_u32`, `s_lshl2_add_u32`, `s_lshl3_add_u32`, `s_lshl4_add_u32`), the six `s_cmp_*_i32` and six `s_cmp_*_u32` relations, and `s_cselect_b32`, besides the existing `s_mov_b32` and `s_movk_i32`. SCC follows the RDNA definitions: carry or borrow for the unsigned add, shifted additions and subtract, signed overflow for the signed ones, the comparison for min, max and `s_cmp_*`, and a nonzero result for the bitwise, shift, and absolute difference forms. For `s_lshl<K>_add_u32`, SCC records whether the 64-bit sum `(S0 << K) + S1` generated a carry out of bit 31, evaluated in 32-bit unsigned arithmetic as `(S0 >> (32 - K) != 0) || (dest < S1)`. `s_mul_i32` leaves it unchanged. A shift uses the low five bits of its count. `s_cselect_b32` is rejected unless the lowerer saw SCC written, and `s_wqm_b64` and the exec restore clear the tracked SCC. Scalar results are not folded to constants, so they cannot supply a texture address or sampler word. Carry-in, 64-bit, high-half multiply, bitfield, `s_cmpk_*`, `s_bitcmp*` and `s_cmov*` forms remain unsupported. The draw test packs 23 SCC outcomes and a 17-bit hash of the 32-bit results into four channels, ten bits each, exported as 1 + k/1024, which binary16 holds exactly. The program is drawn with two sets of constant words, including negative and sign-bit inputs, and every select takes a different arm between the sets. A CPU interpreter written from the instruction definitions supplies the expected color. Replacing `s_ashr_i32` with `s_lshr_b32`, `s_cmp_lt_i32` with `s_cmp_lt_u32`, `s_nand_b32` with `s_and_b32`, `s_nor_b32` with `s_or_b32`, `s_xnor_b32` with `s_xor_b32`, or swapping the subtraction's sources must produce a mismatch. In addition, `check_lshl_add_u32_draw` verifies `s_lshl[1-4]_add_u32` end-to-end via `R32G32B32A32_UINT` readback across small values, shift-wrap, and add-wrap arithmetic cases, alongside selective SCC carry-out cases through `s_cselect_b32`; corrupted expectation words and inverted carry flags report pixel mismatches. `check_absdiff_i32_draw` verifies `s_absdiff_i32` end-to-end via `R32G32B32A32_UINT` readback across positive, negative, zero (clearing SCC), double-negative, mixed-sign, INT_MIN edge, and (2^31, 2^32) overflow-wrap cases alongside SCC nonzero checks; corrupted expectation words report pixel mismatches.

`s_add_i32` sets SCC on signed overflow, which the lowerer computes as `((a ^ r) & (b ^ r)) >> 31`: the result's sign differs from both operands. The earlier form `(~(a ^ b) & (a ^ r)) >> 31` is the same function, and DXC 1.7.2308 emits plain `add i32` (no `nsw`) for both, but an NVIDIA GeForce RTX 3090 (`10de:2204`, driver 32.0.16.1664, debug layer enabled, Windows 11 build 26300) returned SCC 0 for `0x7fffffff + 5` with it, so `pixel_aot_blend_scalar_hardware` reported `pixel_mismatch` on SCC flag bit 2 (channel 0 `3fa04000`, expected `3fa0c000`) while WARP passed. With the current form both pass. `s_sub_i32` keeps its form, which passed its overflowing case on the same adapter. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_blend_scalar_(hardware|warp)$' -V`.

The `s_bcnt0_i32_b32` and `s_bcnt1_i32_b32` instructions count zero or one bits in a 32-bit scalar. Their `b64` forms count across an SGPR pair. All four set SCC when the count is nonzero. The predicate and rounding draw checks the count and SCC against a Python reference and rejects a 64-bit count that ignores the upper word.

`s_bfe_u64` and `s_bfe_i64` extract a field from two scalar dwords. The control supplies the offset in bits 5:0 and the width in bits 22:16. An empty field returns zero. A field extending past bit 63 stops at bit 63, and the signed form extends the top bit of the resulting field. The lowerer reuses the two-dword shifts and sets SCC when either result word is nonzero. The source-built test uses raw UINT readback to check both words and SCC independently. Cases include an extraction across the dword boundary, a result with only its high word set, and offsets and widths at their limits. The signed and unsigned draws each require a mismatch against the other's cross-dword reference. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_blend_scalar_(warp|hardware)$' -V`. [AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

`s_cselect_b64` selects both words of the first source when SCC is set and both words of the second source otherwise. The pixel lowerer uses the tracked SCC and emits a `uint2` for the destination SGPR pair. It reads both operands before writing the destination, so the destination can alias a source. It preserves SCC. The source-built fixture exports both selected words and uses a second select between inline `0` and `-1` to expose SCC in the other two channels of a UINT target. Swapping the register sources must cause a mismatch on all three constant sets. The fixture runs in the LLVM binary check and in `ctest --preset dev --no-tests=error -R '^pixel_aot_blend_scalar_(warp|hardware)$' -V`. [AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

From the repository root on Linux with LLVM 15 installed, assemble the original test shaders, wrap them in synthetic records, then extract, decode and translate them. The test compares the result with lowering the source operands directly.

```sh
python3 -B tests/pixel_aot_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

On Windows, use the Python-enabled build from the project README. Each backend draws a copy, a red/blue swap, a nonlinear curve and a compare-and-select program, the last two with two sets of constants each. It compares all four channels of 4,096 pixels bitwise with an independent CPU reference. Negative controls change the expected channel order, the bound gain or the comparison threshold and must report a mismatch. The select program uses only comparisons, one subtraction and selects, so its reference contains no transcendental or fused step and assumes only correctly rounded UNORM sampling. Its inputs are finite, so NaN operands remain unmeasured. The absolute-value fixture also draws abs(0.5 - red) and checks that removing the modifier produces a mismatch. The add, multiply and move program is drawn with two sets of constants, and replacing its reversed subtractions with forward ones must produce a mismatch. Its reference asserts that every addition, subtraction and multiplication is exact in binary32 for the test inputs, so it does not depend on rounding direction or fusion. The `scalar64` fixture exercises all 14 newly lowered scalar forms across two 12-word constant sets, plus a third drawn without mutations for a high-word-only `s_not_b64` result and an `s_bfe_i32` width of 64 (including NaN bit patterns, negative 64-bit inputs, zero/small/large/wrapped shift counts, `s_bfm_b64` width boundaries, signed/unsigned `s_bfe_*` across `offset + width < 32` and `>= 32`, and SCC preservation through `s_cmov_b64`, `s_brev_b64` and `s_bfm_b64`), folding the 64-bit accumulator and 10 flipping SCC observation bits into four exact binary16 fields (`1 + k/1024`). Four mutations (`s_ashr_i64` to `s_lshr_b64`, `s_bfe_i32` to `s_bfe_u32`, `s_andn2_b64` to `s_and_b64`, and replacing an `s_cmov_b64` `-1` source with `0`) must each report `pixel_mismatch`. What this measures is the synthetic fixture translation, DXC compilation and bitwise D3D12 readback; exec save/restore control flow, SOPP branches, scalar-computed descriptors and real title pixel-shader execution remain unverified.

`v_cubeid_f32`, `v_cubesc_f32`, `v_cubetc_f32` and `v_cubema_f32` follow LLVM's `ConstantFoldAMDGCNCubeIntrinsic` (`llvm/lib/Analysis/ConstantFolding.cpp`). The major axis is z if |z| is at least |x| and |y|, otherwise y if |y| is at least |x|, otherwise x; a face is negative only for a nonzero, non-NaN negative coordinate, and `v_cubema_f32` returns twice the signed major coordinate. The lowering computes each result inline from the three operands. One draw evaluates all four operations for six directions that cover every face, the y/x and z/x ties and -0 on the major axis, which selects face 4 and exports -0 for the major axis. Each direction has a wrong reference for the property it isolates (the tie winner, the sign of -0, the sign of the major axis, the sign of a coordinate), and each must report a mismatch. NaN coordinates are not drawn.

Signed and unsigned 32-bit integer comparisons (`v_cmp_{f,lt,eq,le,gt,ne,ge,t}_{i32,u32}` in both e32 and e64 encodings) reinterpret operands via HLSL `asint` or `asuint`, reject float source modifiers and `clamp`, and feed `v_cndmask_b32` through the same tracked lane-mask path as `v_cmp_*_f32`. The binary and draw fixtures cover all 32 forms across normal and signed-zero bit patterns (`0x80000000` vs `0x00000000`, `0xbf800000` vs `0xbf800000`, `0x3f800000` vs `0xbf800000`, and `0xc0000000` vs `0xbf800000`) to separate signed, unsigned, and floating-point orderings; evaluating the signed shader against the unsigned oracle on `(0x80000000, 0x00000000)` must report a pixel mismatch.

A focused `v_cmp_lt_u32` fixture exports both operand orders through `v_cndmask_b32`, with `vcc_lo` for e32 and `s52` for e64. Its three draws check the sign boundary (`0x80000000` vs `0x7fffffff`), equal words, and the raw patterns `0x7f800002` and `0x7fc00001`. Quieting the first NaN would reverse that pair's unsigned order. Each draw checks all four channels of 4,096 pixels against a CPU reference. Replacing both comparisons with `v_cmp_lt_i32` must produce a pixel mismatch at the sign boundary.
SDWA forms of `v_mov_b32` and `v_cvt_f32_f16` select part of the source and place the result. `src0_sel` takes a byte, a word or the whole dword. The selection is zero-extended, or sign-extended under `sext()`. `dst_sel` writes the low bits of the result into a byte or word of the destination. Following the instruction equivalences in LLVM's `SIPeepholeSDWA` (for example, `v_lshlrev_b32` by 16 is `dst_sel:WORD_1 dst_unused:UNUSED_PAD`, and `v_ashrrev_i32` by 16 is a sign-extended `src0_sel:WORD_1`), `UNUSED_PAD` zeroes the other bits and `UNUSED_PRESERVE` keeps them from the destination's tracked value and is refused when the destination holds no tracked numeric value. The SDWA `v_cvt_f32_f16` widens the selected half and applies `abs`, `neg` and `clamp` after widening, as the e64 form does. Two programs are drawn with two constant sets each. The first moves `WORD_1` and a sign-extended `BYTE_2` into the low half of a register holding 1.0, then splits `(v - 1) × 2^23` into bytes. The second moves a byte into `BYTE_3` and a word into `WORD_1` with padding, and widens both halves of one word. Wrong references must each report a mismatch: the neighbouring byte or word, a zero-extended byte, a sign-extended positive byte, and the other half. `UNUSED_SEXT` is refused, because the instruction equivalences do not pin down its effect on the bits below the field. Byte selections for half widening are also refused.

The rounding program applies `v_ceil_f32` and `v_rndne_f32` in both the VOP1 and VOP3 encodings to power-of-two multiples of the sample and to constant ties (2.5 and -3.5, which round to 2 and -4). It is drawn with two sets of constants against a CPU reference that rounds ties to even. Replacing either instruction with `v_floor_f32`, or the generated `round` with `floor(0.5 + x)`, must produce a mismatch. No input rounds to a signed zero, so the sign of a zero result remains unmeasured.
SDWA forms of `v_mov_b32` and `v_cvt_f32_f16` select part of the source and place the result. `src0_sel` takes a byte, a word or the whole dword. The selection is zero-extended, or sign-extended under `sext()`. `dst_sel` writes the low bits of the result into a byte or word of the destination. Following the instruction equivalences in LLVM's `SIPeepholeSDWA` (for example, `v_lshlrev_b32` by 16 is `dst_sel:WORD_1 dst_unused:UNUSED_PAD`, and `v_ashrrev_i32` by 16 is a sign-extended `src0_sel:WORD_1`), `UNUSED_PAD` zeroes the other bits and `UNUSED_PRESERVE` keeps them from the destination's tracked value and is refused when the destination holds no tracked numeric value. The SDWA `v_cvt_f32_f16` widens the selected half and applies `abs`, `neg` and `clamp` after widening, as the e64 form does. Two programs are drawn with two constant sets each. The first moves `WORD_1` and a sign-extended `BYTE_2` into the low half of a register holding 1.0, then splits `(v - 1) × 2^23` into bytes. The second moves a byte into `BYTE_3` and a word into `WORD_1` with padding, and widens both halves of one word. Wrong references must each report a mismatch: the neighbouring byte or word, a zero-extended byte, a sign-extended positive byte, and the other half. `UNUSED_SEXT` is refused, because the instruction equivalences do not pin down its effect on the bits below the field. Byte selections for half widening are also refused.

The SDWA forms of the exact arithmetic (`add`, `sub`, `subrev`, `mul`, `mul_legacy`, `max`, `min`, `floor`, `ceil`, `fract` and `trunc`) use the e32/e64 lowering and place the result with the SDWA move rules. Only `src0_sel:DWORD` and `src1_sel:DWORD` are accepted. A byte or word source would reach the arithmetic as a zero-extended, usually subnormal binary32, and Scarlett's binary32 denormal mode is not measured. Output modifiers (`mul:2`, `div:2`) are refused. Four programs draw every operation once, each with source modifiers, an inline constant, `clamp`, the legacy zero rule (0 × ∞ = 0) and a byte placed into `BYTE_3`. A result is kept in `WORD_0` and `BYTE_2` of 1.0 under `UNUSED_PRESERVE`, and the low word of another is padded into `WORD_1`. Every result is exact in binary32 and after the half export. Each channel has a wrong reference that must report a mismatch.

The SDWA forms of the remaining unary binary32 operations (`v_exp_f32`, `v_log_f32`, `v_rcp_f32`, `v_rcp_iflag_f32`, `v_rsq_f32`, `v_sqrt_f32`, `v_sin_f32`, `v_cos_f32`, `v_rndne_f32` and `v_frexp_mant_f32`) go through the same path: each reuses its e32/e64 lowering unchanged, with `src0_sel:DWORD` only, and the result is placed with the SDWA move rules. The translator emits the same shader for the whole-dword SDWA spelling and the e32 spelling of each operation, and the controls assert that. A byte or word source, a `src1_sel`, a second operand, an output modifier, `UNUSED_SEXT` on a partial destination and `UNUSED_PRESERVE` over an untracked destination are refused for every one of the ten. Three programs are drawn with two constant rows each. D3D allows `exp2`, `log2`, a reciprocal, `rsqrt`, `sqrt`, `sin` and `cos` an error, so the first two programs choose inputs whose true result is an integer, add 0.5 and take the floor, which returns that integer for any result within 0.5 of it: exp of `-A`, log of `|B|`, rcp of C and rcp_iflag of `-|D|` give 8, 5, 8, -4 and then 16, 3, -2, -16; rsq of `|A|`, sqrt of B, sin of C and cos of `-D` (in revolutions) give 4, 7, 1, -1 and then 2, 3, -1, 1. That checks which operation ran and its source modifiers, not last-place accuracy. The third program is exact and is compared directly: `v_rndne_f32_sdwa` of the ties 2.5 and -3.5 (2 and -4), `v_frexp_mant_f32_sdwa` of `-B` with `clamp`, the low byte of a rounded tie kept in `BYTE_2` of 1.0 under `UNUSED_PRESERVE`, and the low byte of a significand padded into `BYTE_3`. 26 wrong references, each replacing one channel, must report `pixel_mismatch`: a neighbouring operation for each approximate one (natural exponential and logarithm, rsq for rcp, sqrt and rcp for rsq, rsq for sqrt, sin and cos swapped, radians for revolutions), a copied source, a dropped negation, ties rounded up or toward zero, a dropped clamp, padding for preserving, the neighbouring byte and an unplaced result. That is 32 draws from three compiles and three renderer launches. They run as their own tests, `pixel_aot_sdwa_unary_hardware` and `pixel_aot_sdwa_unary_warp` (`tests/pixel_aot_check.py draw-sdwa-unary`), with their own coverage record, and the LLVM 15 round trip covers the three programs. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. Not shown: last-place accuracy of the approximate operations on any adapter, a partial destination for an approximate result (its low bits have no single reference), inputs outside these rows (zero, negative arguments to log, rsq and sqrt, infinities, NaN, denormals), the integer-divide flag of `v_rcp_iflag_f32`, and what the console computes.

Signed and unsigned 16-bit integer comparisons (`v_cmp_{eq,ne,lt,le,gt,ge}_{i16,u16}` in e32 and e64 encodings) compare only the low 16 bits of each source: the lowering sign-extends them with `asint(x << 16) >> 16` or masks them with `& 0xffff`, and the result feeds `v_cndmask_b32` through the same tracked lane mask as the other comparisons. A source may be a register, an integer inline constant from -16 to 64 or a literal up to `0xffff`. LLVM prints a float inline constant on a 16-bit integer operand as its binary16 literal (`1.0` as `0x3c00`), so float tokens, wider literals, source modifiers, `clamp` and `op_sel` are refused. The exec-writing `v_cmpx_*` forms stay rejected (the f16 comparisons and `v_cmp_class_f16` are lowered by their own branches in `tools/pixel_aot.py`), and `tests/form_coverage_check.py` asserts that all 24 16-bit integer forms, the 32 32-bit integer forms, the 32 64-bit integer forms, and the 34 binary16 and 34 binary32 floating-point forms (156 compare forms in all) are counted while those neighbours are not. The binary fixtures cover the 24 forms, and the draws (`tests/pixel_aot_check.py draw-comparisons`) evaluate all six predicates per kind and encoding on six word pairs. Each word is a normal binary32 value whose upper half differs from the lower one where a 32-bit comparison would disagree, and the pairs include `0x8000` against 0, equal low halves, -1 against 1, `0x7fff` against `0x8000` and both ends of the inline-constant range. Three references must report a pixel mismatch: a signed shader against the unsigned reference, `eq` replaced by `ne`, and a 32-bit comparison against the 16-bit reference on equal low halves.

Signed and unsigned 64-bit integer comparisons (`v_cmp_{f,lt,eq,le,gt,ne,ge,t}_{i64,u64}` in e32, suffix-less and e64 spellings, [#794](https://github.com/R0Y00/Xbox2Windows/issues/794)) compare two words per source, so the HLSL needs no 64-bit type. Without a suffix the encoding is chosen as LLVM 15 does: e32 when the destination is `vcc`/`vcc_lo` and src1 is a VGPR pair, e64 otherwise. The high words decide unless they are equal, and then the low words, which always compare unsigned. A signed comparison reads only the high word as `int`. Every word is read as raw bits with `asuint` (`asint` for a signed high word), bound to a local before the comparison, and no arithmetic touches a float view. The result is a tracked lane condition like the other comparisons, so `v_cndmask_b32` consumes it unchanged. A source is a VGPR pair `v[a:a+1]` with `a` from 0 to 254 whose two registers hold tracked numbers, an even-aligned SGPR pair, or an integer inline constant from -16 to 64 sign-extended to 64 bits (`-1` is all ones). On e32 (VOPC) the destination must be `vcc_lo` or `vcc` and src1 must be a VGPR pair, because LLVM 15 rejects an SGPR there. On e64 the destination is `vcc_lo`, `vcc`, an SGPR or an aligned SGPR pair, as for the narrower comparisons, and the wave64 spellings (`vcc`, `s[52:53]`) lower to the same HLSL as the wave32 ones. Refused with a message that names the cause: 32-bit literals, float and hex tokens, constants outside -16..64, register ranges other than a pair, untracked or pending sources, an SGPR destination or SGPR src1 on e32, and misaligned or out-of-range SGPR pairs. Source modifiers, `clamp`, `op_sel`, the `v_cmpx_*` forms, f64 and the unsupported predicates, and the DPP and SDWA forms are refused with the generic `Unsupported instruction` message, which does not name the cause. The 32-bit literal extension rule is not sourced, so literals stay outside this change; refusing float inline constants (LLVM accepts `1.0`) is the translator's choice.

Assumed, not sourced: LLVM 15 assembles unaligned VGPR pairs such as `v[3:4]` and `v[19:20]`, and the translator accepts them, but hardware legality is not shown. The sign extension of inline constants is the scalar b64 rule of this section applied to VOPC; the pairs against `-1` pin the translator's model only. Two SGPR pairs as the sources of one e64 comparison are accepted because LLVM assembles them, with the constant-bus limit unsourced. The controls (`tests/pixel_aot_check.py`, no GPU) translate all 32 forms, compare the exact HLSL, and check the refusals above, the overlap of `v[18:19]` with `v[19:20]` (one value of `v19`, read as the high word of one operand and the low word of the other) and each destination spelling. The draw (`tests/pixel_aot_check.py draw-cmp64`, tests `pixel_aot_cmp64_hardware` and `pixel_aot_cmp64_warp`, 60 s limit, capability skip 77) compiles eight programs, i64 and u64 by e32 and e64 by two groups of four predicates, with -denorm preserve. Each channel runs one predicate on one operand shape: two VGPR pairs, an SGPR pair, the overlapping pair, or inline `-1`. The 13 word pairs go through the constant buffer and `v_mov_b32` copies and include a high-word-only difference (`0x1_00000000` against `0x0_ffffffff`), identical high words with each order of the low words, `-1` against 0, `0x7fffffff_ffffffff` against `0x80000000_00000000`, high words that differ only in the sign bit, equal values, words that are not normal binary32 values (`0x00000001`, `0x7fc00000`, `0xffffffff`, `0x80000000`) and two pairs that make the overlapping operands equal. Six mutated shaders must each mismatch (the test enforces this): high and low words swapped, only the low words compared, only the high words compared, `lt` replaced with `le`, signed compared as unsigned, and the overlapping pair read as `v[20:21]`. The draw pins the translator's model on a desktop adapter; it is not evidence for gfx1013 hardware. The LLVM 15 round trip covers the eight programs. The scanner applies the translator's operand rules (inline decimals -16..64, VGPR pairs `v[a:a+1]` with `a` from 0 to 254, even-aligned SGPR pairs up to `s105`, a VGPR pair as e32 src1, `vcc`/`vcc_lo` only as the e32 destination) and `tests/rdna2_tools_check.py` pins the accepted and refused spellings. The VOPC e32 destination rule follows the same restriction as the narrower comparisons.

The 64-bit `VOP3` shifts `v_lshlrev_b64`, `v_lshrrev_b64` and `v_ashrrev_i64` (issue #928) follow the [AMD RDNA 2 ISA guide](https://gpuopen.com/download/rdna2-shader-instruction-set-architecture.pdf) §12.12 (opcodes 767-769): `D = S1 << S0[5:0]`, `S1 >> S0[5:0]` and `signext(S1) >> S0[5:0]`. The opcodes match the LLVM 15 gfx1013 encodings, and LLVM 15 folds an IR shift by `x & 63` into the bare instruction while keeping `x & 31`. The value uses the two-word representation of the 64-bit comparisons above, so the HLSL needs no 64-bit type: both source words are read into temporaries before the destination pair is written, so the in-place form keeps the original value, and the result is selected for amounts below and above 32, so no 32-bit HLSL shift receives a count of 32 or more. Only `v_ashrrev_i64` fills with the sign bit. The amount is a VGPR, an SGPR, an inline integer from -16 to 64 or a literal; the value is a VGPR pair, an SGPR pair or an inline integer, sign-extended to 64 bits as for the comparisons (assumed, not sourced). At most one source may be an SGPR or a literal (the ISA's one scalar broadcast constant; LLVM 15 refuses the others with `violates constant bus restrictions`). `_e32`, `clamp`, `op_sel` and source modifiers, a destination that is not a pair in `v0`..`v255` or has an outstanding image sample, float amounts and use inside a masked region are refused. The draw (`tests/pixel_aot_check.py draw-shift64`, tests `pixel_aot_shift64_warp` and `pixel_aot_shift64_hardware`) compiles three programs per instruction (register sources; the in-place pair next to the literal amount `0x41`, which wraps to 1; and an SGPR-pair value next to the inline `-1`) and reads the four result words back through `R32G32B32A32_UINT` for three values (one with a negative high word) and the amounts 0, 1, 31, 32, 33, 63, 64, 65, `0xffffffc1` and `0x80000020`, against a Python reference written from the ISA formulas. Three mutated `v_ashrrev_i64` shaders must each mismatch: the amount masked to five bits, the sign fill replaced with zeros, and the result words swapped. The LLVM 15 round trip covers the nine programs; the scanner accepts the same operand shapes and `tests/rdna2_tools_check.py` pins them. The draw pins the translator's model on a desktop adapter; it is not evidence for gfx1013 hardware.

The non-packed 16-bit integer `VOP3` ops (issue #669) `v_add_nc_u16`, `v_add_nc_i16`, `v_sub_nc_u16`, `v_sub_nc_i16`, `v_min_u16`, `v_max_u16`, `v_min_i16`, `v_max_i16`, `v_mul_lo_u16`, `v_lshlrev_b16`, `v_lshrrev_b16` and `v_ashrrev_i16` compute on the low 16 bits of two sources and reuse the lane arithmetic of the packed forms: results wrap modulo 2^16, signed min, max and shifts sign-extend the 16-bit value, and shift counts are masked to 4 bits (`& 15`, so a count of 16 shifts by 0). Only the low 16 bits of the result are defined and tracked: the lowering records the destination as `("low16", alu)`, a value whose HLSL variable holds the zero-extended result but whose upper half no accepted reader observes. The destination's previous value is not merged, so a destination may hold anything (a tracked value, an untracked register or another low16 result); a destination with an outstanding image sample is refused. The same applies to the unsuffixed and the `_e64` spelling. The readers that see a low16 value are the 12 ops themselves (a chain of two ops is accepted), the 16-bit integer compares `v_cmp_{eq,ne,lt,le,gt,ge}_{i16,u16}` and `v_mov_b32_sdwa` with `src0_sel:WORD_0`, `BYTE_0` or `BYTE_1` (also with `sext()`), which widens the low half into a fully defined word. Every whole-word or high-half reader is refused: 32-bit integer and float arithmetic, moves, `v_cndmask_b32` data operands, DPP, packed `VOP3P` forms, conversions (including `v_cvt_f32_f16`, which is out of scope for integer low16; binary16 arithmetic results use the distinct `half16` tag, below), dot products, class compares, packs, both export forms and image sample coordinates raise `Expected a numeric register`, `Expected a numeric VGPR in DPP`, `Unsupported pack operand`, `Unsupported export` or `Unsupported texture coordinate`; `v_cvt_f16_f32` into a low16 destination raises `Unsupported conversion destination`; and `v_mov_b32_sdwa` with `WORD_1`, `BYTE_2`, `BYTE_3` or `DWORD` on a low16 source, or with `dst_unused:UNUSED_PRESERVE` into a low16 destination, raises `Reads the undefined high half of a 16-bit result`. A source may be a VGPR in 0 to 255 that holds a tracked value, a tracked SGPR in 0 to 105, an integer inline constant from -16 to 64 or a literal up to `0xffff`. `op_sel`, `clamp`, source modifiers, DPP, SDWA, the f16 ops, float tokens, wider literals, inline integers outside -16 to 64 (llvm-mc would encode `-17` as the literal `0xffef`; the translator refuses it by policy, as for the 16-bit compares), an untracked source, a register out of range and a destination with an outstanding image sample are refused. `tests/form_coverage_check.py` asserts that the 12 unsuffixed forms are counted while the f16 neighbours are not. `tools/scan_unsupported_shaders.py` matches instructions syntactically and cannot see the low16 tags, so it can report a shader as supported that the translator then refuses at a whole-word reader. The binary fixtures (four programs; groups 0 to 2 widen all four results and group 3 its two low-half results with `v_mov_b32_sdwa ... src0_sel:WORD_0`) round-trip through LLVM 15.0.7, and the controls refuse every form above with its message. Reproduce with `ctest --preset dev --no-tests=error -R "^pixel_aot_int16_vop3_"`: groups 0 to 2 preload `v24` to `v27` with a `0xa5c3xxxx` sentinel, run four ops on constant words, widen each result into `v12` to `v15` with SDWA `WORD_0` and export through the bit-exact `R32G32B32A32_UINT` target against a Python reference written from the semantics that contains only the zero-extended low halves; group 3 chains `v_add_nc_u16` into `v_mul_lo_u16` and selects between whole words with `v_cmp_lt_u16` and `v_cmp_lt_i16` on low16 results. Six word sets cover `0x7fff` against `0x8000`, `0xffff + 1`, `0 - 1`, shift counts 15, 16, 17 and 31, a negative operand under `ashrrev`, signed and unsigned min and max that disagree, the overflow `0x1234 * 0x0567`, and sources whose high half differs from the low half. Five mutations of the generated shader must each report a pixel mismatch (a source read from bits 31:16, a shift mask of 31 instead of 15, `v_min_i16` replaced by `v_min_u16`, and a `>> 16u` read of a low16 result by the chained op and by the first compare), and a widening that selects `WORD_1` instead of `WORD_0` must be refused at lowering. The sentinel preloads do not detect a high-half leak, because every accepted reader skips the high half; that half is guarded only by the refusal controls. The binary round trip disassembles through libLLVM; `tools/disasm_rdna2.py` decodes the same encodings. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

The three-operand 16-bit integer `VOP3` ops (issue #669) `v_mad_u16`, `v_mad_i16`, `v_min3_i16`, `v_min3_u16`, `v_max3_i16`, `v_max3_u16`, `v_med3_i16` and `v_med3_u16` follow the same low-16-only contract as the paragraph above: the result is tagged `("low16", alu)`, the sources go through the low16-aware `int16_operand` (a register, an SGPR up to `s105`, an inline integer from -16 to 64 or a literal up to `0xffff`), and only the 16-bit ops, the 16-bit compares and `v_mov_b32_sdwa` with `WORD_0`, `BYTE_0` or `BYTE_1` read the result. The lowering is `pk_int16_lane_expr` with the keys the packed `v_pk_mad_*` ops already use plus the six min3, max3 and med3 keys. Semantics follow LLVM 15.0.7 `llvm/lib/Target/AMDGPU/VOP3Instructions.td`: `V_MAD_U16` and `V_MAD_I16` are `VOP3_Profile<VOP_I16_I16_I16_I16, VOP3_CLAMP>` with `Ternary_i16_Pats<mul, add>` (lines 318-319 and 392-393), so the result is `(a * b + c) mod 2^16` and the signed and unsigned forms have the same low half when `clamp` is not modelled; `V_{MIN3,MAX3,MED3}_{I,U}16` map to `AMDGPUsmin3`, `AMDGPUumin3` and the max3 and med3 equivalents (lines 520-529), so the signed forms sign-extend the low halves and med3 is the median. `clamp` and `op_sel` are accepted by llvm-mc for these forms (`v_mad_u16 v5, v1, v2, v3 clamp` encodes with bit 15 of the second dword, `op_sel:[0,0,0,1]` with bit 14) and are refused here with `Unsupported instruction` because they are not modelled; source modifiers, floats, wide literals, inline integers outside -16 to 64, an untracked source, a register out of range, a wrong operand count, DPP and SDWA are refused as for the two-operand forms. llvm-mc prints no `_e64` suffix for these ops (`v_mad_u16_e64` is parsed and printed as `v_mad_u16`); the translator accepts both spellings. The LLVM 15 gfx1013 encodings used by the fixtures use the VOP3 opcodes (bits 25:16 of the first dword) 0x340 for `v_mad_u16`, 0x35e for `v_mad_i16`, 0x353 for `v_min3_u16` and 0x358 for `v_med3_i16`; `v_mad_u16 v5, v1, v2, v3` encodes as `05 00 40 d7 01 05 0e 04`. Only one literal operand is allowed by the assembler. `tests/form_coverage_check.py` asserts that the 8 forms are counted in `gfx1013.tsv` and `rdna2.tsv` while `v_mad_u32_u16`, `v_mad_i32_i16`, `v_sad_u16` and `v_min_f16_e64` are not, and `tools/scan_unsupported_shaders.py` reports the forms as supported by syntax only (it cannot see the low16 tags). The binary fixtures (four programs, round-tripped through LLVM 15.0.7) and the controls cover each form, a chain from a two-operand result into `v_mad_u16`, a med3 and a max3, the refusals above with their messages and a full-word reader (`v_add_nc_u32`) of a mad and of a med3 result. Reproduce with `ctest --preset dev --no-tests=error -R "^pixel_aot_int16_ternary_"`: groups 0 to 2 run four ops each on constant words with the destinations preloaded with a `0xa5c3xxxx` sentinel, widen the results with SDWA `WORD_0` into `v12` to `v15` and export them through `R32G32B32A32_UINT` against a Python reference written from the semantics; group 3 chains `v_add_nc_u16`, `v_mad_u16`, `v_med3_i16` and `v_max3_u16`. Six word sets separate signed from unsigned order (`0x7fff` against `0x8000`, `0xfffe` against 1), the median from the minimum and maximum, the wrap of `0x1234 * 0x0567 + 0xffff`, and sources whose high half differs from the low half. Five mutations must each report a pixel mismatch (signed min3 and max3 as unsigned, signed med3 as unsigned, med3 as min3, the mad addend dropped, and the first source read from bits 31:16), and a widening that selects `WORD_1` must be refused at lowering. As for the two-operand ops, the high half of the destination is not measured and the sentinels do not detect a leak.

Plain non-fused binary16 `VOP2` arithmetic (issue #839) `v_add_f16`, `v_sub_f16`, `v_subrev_f16` and `v_mul_f16`, in the unsuffixed, `_e32` and `_e64` spellings, computes on the low 16 bits of two sources. `v_subrev_f16` is `src1 - src0` (LLVM 15.0.7 `VOP2Instructions.td`: `V_SUBREV_F16` is the reversed operand form of `v_sub_f16`; the order is not measured natively). Each half is widened exactly with `widen16`, one binary32 add, subtract or multiply is evaluated under `precise`, and `narrow16` rounds once to nearest even, keeping binary16 subnormals and signed zeros. A product of two 11-bit significands is exact in binary32, and a sum rounded to 24 bits before the 11-bit rounding is innocuous because 24 >= 2 * 11 + 2. A one-off exhaustive numpy run (every pair of finite binary16 values, 63,488 squared = 4,030,726,144 pairs, for add and for mul: the binary32 result narrowed to binary16 against the binary64 result narrowed to binary16, 0 mismatches) is not part of CI; the repository check repeats the claim with a pure-Python model over several hundred operands against a set of boundary values for all four ops, and the translator's own exact reference agrees with it. Nothing is claimed beyond finite operands and results. A source is accepted only when its low half is statically proven finite: a binary16 inline constant (`0.5`, `-0.5`, `1.0`, `-1.0`, `2.0`, `-2.0`, `4.0`, `-4.0`, `0.15915494`; the negative ones are read before a leading minus counts as a modifier), the integer inline constant `0`, a 16-bit literal up to `0xffff`, an SGPR holding a known integer, a VGPR copied by `v_mov_b32` from a literal or an inline constant (also through further `v_mov_b32` copies), or the result of an earlier accepted op, whose value the translator computes with an exact integer reference (`f16_vop2_result`) so that a chain stays proven. Anything else (a constant-buffer word, a sample, an interpolant, an uninitialized register, a 16-bit integer result) is refused with `binary16 operand lacks a finite proof: <tok>`, a NaN or infinity encoding with `binary16 operand is not finite: <tok>`, and a result that rounds to infinity with `binary16 result overflows: v_<op>_f16` (the boundary is 65520, so `0x7bff + 0x4bff` is accepted and `0x7bff + 0x4c00` is refused, for both signs and for all four ops). Integer inline constants other than `0` are refused (`Unsupported binary16 integer inline constant`): LLVM 15 prints a 16-bit literal that equals an integer inline constant as that integer (`v_add_f16 v1, 0xffff, v3` prints `-1` and `0x0001` prints `1`, pinned in `check_binary`), so `-16..-1` can be NaN patterns and `1..64` subnormals whose hardware reading as an inline constant is not established; subnormal operands therefore enter through `v_mov_b32` of a 32-bit literal. Source modifiers (`Unsupported binary16 source modifier`), other floats and operands such as `vcc_lo`, `m0` and `exec` (`Unsupported binary16 operand`), a literal wider than 16 bits, an e32 or unsuffixed form whose src1 is not a VGPR, `clamp`, `mul:`, `div:`, `op_sel`, DPP, SDWA and a wrong operand count are refused as well (the last group with `Unsupported instruction`), and so are the neighbouring `v_min_f16`, `v_max_f16`, `v_fmac_f16` and `v_ldexp_f16`. The entry profile is explicit: `lower(..., half_denorm="preserve")` (CLI `--half-denorm preserve`; `pixel_aot.HALF_DENORM_PRESERVE`) declares that MODE[7:6] holds binary16 denormals as preserved at shader entry, taken by the caller from validated guest state. The default `None` means unknown, and the four forms are then refused with `binary16 denormal mode unknown at entry`; any other value is refused with `Invalid binary16 denormal entry state`. This is a declaration, not a tracked MODE write: `s_denorm_mode`, `s_round_mode`, `s_setreg_b32` and every `s_setreg_imm32_b32` of MODE other than bits 3:2 stay refused, so the declaration holds for the whole program, and the existing rounding gate refuses the forms under round toward zero. The lowering itself never reads MODE, so the claim is that the translated result equals nearest-even arithmetic with binary16 denormals preserved, not that the console runs in that mode; the console's denormal state is not measured (`F16_DENORM_MODE` in the tests is asserted equal to the constant). The result is tagged `("half16", alu)`, distinct from `("low16", alu)`: bits 15:0 are defined and bits 31:16 are not, the destination's old value is not merged, and no integer reader (the 16-bit ops, the 16-bit compares, `v_mov_b32_sdwa` `BYTE_0` and `BYTE_1`) can see it. The readers are `v_cvt_f32_f16` (`_e32` and `_e64`, no modifiers and no `clamp`), `v_cvt_f32_f16_sdwa` with `src0_sel:WORD_0`, `v_mov_b32_sdwa` with `src0_sel:WORD_0` (no `sext()`) and the four ops themselves (a chain). Every other reader refuses the tag (`Expected a numeric register`, `Expected a numeric VGPR in DPP`, `Unsupported pack operand`, `Unsupported conversion destination`, `Unsupported export` for both export forms, `Reads the undefined high half of a 16-bit result` for `WORD_1`, `BYTE_0` to `BYTE_3`, `DWORD`, `sext()` and an `UNUSED_PRESERVE` into a binary16 destination, and `Modifiers on a binary16 arithmetic result are not lowered`); the tag name must not start with `sample` or `alu`, which the compressed export and pack checks accept for string pairs, and a control pins that. The controls (`tests/pixel_aot_check.py`, no GPU; `check_f16_arith_controls`) cover each spelling, every operand kind, the refusals above with their exact messages (unknown and invalid entry state, a dynamic operand, NaN and infinity in every provenance, overflow at both boundaries and signs, modifiers, integers, floats, registers, the pending, sample and mask checks, the mode instructions before and after the op, all readers) and the Python reference against pinned rows. The draw (`tests/pixel_aot_check.py draw-f16-arith`, tests `pixel_aot_f16_arith_hardware` and `pixel_aot_f16_arith_warp`, 120 s limit, capability skip 77) bakes every operand into the program, so the constant buffer is unused: 20 programs run the 40 operand pairs through all four ops in a rotation of e32, e64, SGPR and literal forms and pack two results per word with SDWA `WORD_0` and `WORD_1`; six programs read the four results through the four readers, and two programs chain four ops. The pairs cover halfway cases that round down and up to even, just above and below a tie, a sticky bit beyond binary32 (`0x7bff + 0x0001`), multiplication ties into subnormals, results that become subnormal, the carry from the largest subnormal into `0x0400`, positive and negative subnormal inputs, the signed zero of every sum, difference and product (`-0 + -0`, `+0 + -0`, exact cancellation, underflow signs) and plain values; no pair overflows. Bits 31:16 of every source word hold junk. The readback is the lossless `R32G32B32A32_UINT` target against `f16_arith_reference`, an integer implementation written from the semantics without `pixel_aot`. The programs with subnormal operands also run under a binary32 `-denorm ftz` build and must still pass. Eleven deviations must each mismatch, one batch per mutation with the statuses printed (truncation, ties away from zero, flushed subnormal results, flushed subnormal inputs, negative subnormal inputs read as positive, the sign of a tiny result dropped, a source read from bits 31:16, `v_subrev_f16` lowered as `v_sub_f16`, `v_sub_f16` as `v_add_f16`, `v_mul_f16` as `v_add_f16`, and a sum that turns `-0` into `+0`), and a Python pre-check requires that each changes an expected word of some program and leaves unrelated rows alone. A `WORD_1` widening of a result is refused at lowering. Not measured: the high half of the destination (the draw cannot detect a leak, and only the refusal controls guard the readers), the console's denormal state, NaN and infinity operands, overflow and the NaN payload rules, runtime operands, the hardware reading of integer inline constants on binary16 operands, the `v_subrev_f16` order natively, and binary32 round to nearest even on the desktop adapters beyond what the draw shows.

Plain binary16 `VOP1` rounding (issue #839) `v_floor_f16`, `v_ceil_f16`, `v_trunc_f16` and `v_rndne_f16`, in the unsuffixed, `_e32` and `_e64` spellings, rounds the low 16 bits of one source to an integer under the rules of the `VOP2` arithmetic above. LLVM 15.0.7 `VOP1Instructions.td` (lines 449-452) selects `ffloor`, `fceil`, `ftrunc` and `frint` for them; `frint` is round to nearest even under the default rounding mode. The half is widened exactly with `widen16`, one binary32 `floor`, `ceil`, `trunc` or `round` is evaluated under `precise` (HLSL `round` is round-half-to-even in DXIL, as the binary32 `rndne` entry already relies on), and `narrow16` narrows the result. The integer of a finite binary16 is at most 65504 in magnitude and therefore representable, so the narrowing is exact and no rounding rule for the result is needed; an exact zero keeps the sign of the source (`trunc(-0.5)`, `ceil(-0.3)` and `rndne(-0.4)` are `-0`, `floor(-0)` is `-0`). The source proof, the entry profile, the result tag and the audited readers are those of the `VOP2` arithmetic: a source is accepted only when its low half is statically proven finite (a binary16 inline constant, `0`, a 16-bit literal, a tracked SGPR, a VGPR copied from a constant, or the result of an earlier accepted binary16 op), `lower(..., half_denorm="preserve")` must declare the entry state (`binary16 denormal mode unknown at entry` otherwise, `Invalid binary16 denormal entry state` for another value), the destination is tagged `("half16", alu)`, and the translator computes the result with an exact integer reference (`f16_round_result`) so that a chain stays proven (`v_floor_f16 v24, 0x7bff` followed by `v_add_f16 v25, v24, 0x4bff` is accepted and `0x4c00` is refused as an overflow). The refusals carry exact messages: `binary16 operand lacks a finite proof: <tok>`, `binary16 operand is not finite: <tok>` for a NaN or infinity constant in any provenance, `Unsupported binary16 source modifier <tok>`, `Unsupported binary16 integer inline constant <tok>`, `Unsupported binary16 operand <tok>`, `Invalid binary16 rounding operation` outside the entry mask, `Unsupported binary16 or binary64 operation under round toward zero: <mnemonic>`, and `Unsupported instruction` for `clamp`, `mul:`, `div:`, `op_sel`, DPP, SDWA, a second source and the neighbouring `v_fract_f16`, `v_rcp_f16` and `v_sqrt_f16`. `clamp` and the output modifiers assemble on `_e64` (pinned in `check_f16_round_binary`, together with the encodings `v_floor_f16_e32 v1, v2` = `02 b7 02 7e`, `v_ceil_f16_e64 v1, v2` = `01 00 dc d5 02 01 00 00`, `v_trunc_f16_e32 v1, 1.0` and `v_rndne_f16_e32 v1, v2` = `02 bd 02 7e` from LLVM 15) and are refused. Every other reader of the tag refuses it as for the arithmetic results (`Expected a numeric register`, `Reads the undefined high half of a 16-bit result`, `Modifiers on a binary16 arithmetic result are not lowered`, `Unsupported export`). The controls (`check_f16_round_controls`, no GPU) cover each spelling, every operand kind, the refusals above, the scheduling and mask checks, the mode instructions before and after the op, the readers, pinned rows of the Python reference and a full comparison of the reference with `struct` format `e` plus `math.floor`, `math.ceil`, `math.trunc` and Python `round` (half-even) over every finite binary16 value, including the sign of zero, and with the translator's own copy. The draw (`tests/pixel_aot_check.py draw-f16-round`, tests `pixel_aot_f16_round_hardware` and `pixel_aot_f16_round_warp`, 60 s limit, capability skip 77) bakes every operand into the program: 21 programs run 42 values through all four ops in a rotation of e32, e64, SGPR and literal forms and pack two results per word with SDWA `WORD_0` and `WORD_1`; four programs read the four results through the four audited readers; two programs chain a `v_mul_f16` result into `v_trunc_f16` and `v_rndne_f16` and the `v_trunc_f16` result into `v_sub_f16`. The values cover both signs of zero, negative inputs that round to `-0`, the halfway cases 0.5, 1.5, 2.5, 3.5 and their negatives, 0.5 plus and minus one ulp, 1022.5 and 1023.5 (ulp 0.5, so halfway cases still exist), values from 1024 up (already integers, ulp 1, then ulp 2 from 2048) and +-65504, the subnormals `0x0001` and `0x03ff` with both signs and the smallest normal. A half-integer such as 2047.5 is not representable, the largest halfway case is 1023.5. Bits 31:16 of every source word hold junk. The readback is the lossless `R32G32B32A32_UINT` target against `f16_round_reference`, an integer implementation without `pixel_aot`; the programs with subnormal operands also run under a binary32 `-denorm ftz` build and must still pass. Five deviations must each mismatch, one batch per mutation with the statuses printed (`floor` and `ceil` swapped, `trunc` lowered as `floor`, `rndne` lowered as round half away from zero, the sign of a `-0` result lost, and a source read from bits 31:16), and a Python pre-check requires that each changes an expected word of some program and leaves unrelated rows alone. Not measured: the high half of the destination, the console's denormal state, NaN and infinity operands, runtime operands, the hardware reading of integer inline constants on binary16 operands, and the `frint` rounding mode of the console beyond the default mode that the entry assumption names.

Plain integer to binary16 conversion (issue #839) `v_cvt_f16_i16` and `v_cvt_f16_u16`, in the unsuffixed, `_e32` and `_e64` spellings, converts the low 16 bits of one source under the rules of the binary16 forms above. LLVM 15.0.7 `VOP1Instructions.td` (lines 432-433) selects `sint_to_fp` for `V_CVT_F16_I16` and `uint_to_fp` for `V_CVT_F16_U16`, and the entry assumption is round to nearest even. The source is read through the audited low-16 integer reader (`int16_operand`): bits 15:0 are sign-extended for the signed op and zero-extended for the unsigned one, and bits 31:16 are never read. The 16-bit value converts to binary32 exactly (at most 65535, below 2^24), and `narrow16` rounds once to nearest even, so the result is the exactly rounded binary16 value, with ties to even (2049 is 2048, 2051 is 2052, -2049 is -2048, 4098 is 4096, 4102 is 4104) and zero as +0 (`0x0000`). The signed op accepts a runtime source (a constant-buffer SGPR, a VGPR copy of one, a sample, or a 16-bit integer result such as `v_add_nc_u16`) without a proof: every i16 value is finite in binary16 (32767 rounds up to 32768, `0x7800`, and -32768 is exact), so no constant proof is needed. The result is tagged `("half16", alu)` like the other binary16 results, but a runtime source gives no known value, so it cannot feed the binary16 arithmetic or rounding ops (`v_add_f16`, `v_floor_f16` and the rest refuse it with `binary16 operand lacks a finite proof: <tok>`); an i16 source that is itself statically known (an inline constant, a literal, a tracked SGPR, a VGPR copied from a constant) also records its exact result (`f16_cvt_int_result`, an integer reference), so a chain stays proven (`v_cvt_f16_i16 v24, -7` followed by `v_mul_f16 v25, v24, v24` is accepted, and `0xf7ff` squared overflows and is refused). The unsigned op is accepted only for a statically proven source below 65520: 65519 rounds to 65504 (`0x7bff`), and 65520 is the halfway case that ties to even and rounds up to infinity. A runtime unsigned source is refused with `binary16 conversion source lacks a range proof: <tok>`, a proven value of 65520 or more with `binary16 result overflows: v_cvt_f16_u16` (also through the inline constants -16 to -1, which LLVM prints for the literals `0xfff0` to `0xffff`), and only the low half of a proven word counts (`0xffff0003` is 3). Both ops need the explicit entry profile (`lower(..., half_denorm="preserve")`; `binary16 denormal mode unknown at entry` otherwise, `Invalid binary16 denormal entry state` for another value) and are refused under round toward zero (`Unsupported binary16 or binary64 operation under round toward zero: v_cvt_f16_i16_e32`). Modifiers stay refused: a source modifier, a float or another operand is `Unsupported 16-bit integer operand <tok>`, an inline integer outside -16 to 64 is `Unsupported 16-bit integer inline constant`, a literal wider than 16 bits is `16-bit literal out of range`, and `clamp`, `mul:`, `div:`, `op_sel`, DPP, SDWA, a second source, and the neighbouring `v_cvt_i16_f16` and `v_cvt_u16_f16` are `Unsupported instruction` (LLVM 15 assembles `clamp` and `mul:2` on `_e64`; the encodings `v_cvt_f16_i16_e32 v1, v2` = `02 a3 02 7e`, `v_cvt_f16_u16_e32 v1, v2` = `02 a1 02 7e` and `v_cvt_f16_i16_e64 v1, v2 clamp` = `01 80 d1 d5 02 01 00 00` are pinned in `check_f16_cvt_int16_binary`). The readers of the result are those of the other binary16 results (`v_cvt_f32_f16` e32 and e64, its SDWA `WORD_0` form, `v_mov_b32_sdwa` `WORD_0` and the binary16 ops); every other reader, including the 16-bit integer reader that a conversion result would otherwise reach (`v_cvt_f16_i16 v12, v24` of a result `v24`), refuses the tag. The controls (`check_f16_cvt_int_controls`, no GPU) cover each spelling, every operand kind, the refusals above with their exact messages, the 65519 and 65520 boundary for each provenance, the scheduling, mask and mode checks, and the readers; the Python reference (`f16_cvt_int_reference`, integer arithmetic on the spacing of binary16 values) is compared with `struct` format `e` and with the translator's copy over all 65,536 values of both kinds. The draw (`tests/pixel_aot_check.py draw-f16-cvt-int16`, tests `pixel_aot_f16_cvt_int16_hardware` and `pixel_aot_f16_cvt_int16_warp`, 60 s limit, capability skip 77) compiles ONE signed shader that reads four words from the constant buffer and converts them in every operand form (VGPR and SGPR sources, e32 and e64, and two `v_add_nc_u16` and `v_sub_nc_u16` results), packing two results per word with SDWA `WORD_0` and `WORD_1`; ten cases feed it distinct values with the high halves varied independently of the low halves (-32768, 32767, 2049, -2049, zero, the halfway cases 4098 and 4102 with their negatives, 16392, 16408 and 32760, small odd values, and one value under four different high halves, including patterns that change the result if bits 31:16 were read). Six baked programs convert 24 proven u16 and 24 proven i16 constants (65519 gives 65504, 2049 gives 2048, 65472 to 65504 at the top of the range) in four operand forms (junk in bits 31:16 of the register forms), and two programs feed proven conversion results into `v_add_f16` and `v_sub_f16`. The readback is the lossless `R32G32B32A32_UINT` target against `f16_cvt_int_reference`, an integer implementation written from the semantics without `pixel_aot`. Three deviations must each mismatch, one batch per mutation with the statuses printed (an i16 source zero-extended instead of sign-extended, ties away from zero instead of ties to even, and a source read from bits 31:16; the first two also on a baked program), and a Python pre-check requires that each changes an expected word of some case and leaves unrelated rows alone. 65520 and a runtime unsigned source are refused at translation inside the draw as well. Not measured: the high half of the destination, the console's denormal state, the hardware rounding of the conversion beyond the default mode that the entry assumption names, runtime unsigned sources, and the hardware reading of the integer inline constants.

What a 16-bit result leaves in the high half of its destination is not measured and stays open. LLVM's `GCNSubtarget::zeroesHigh16BitsOfDest` ("on gfx10, all 16-bit instructions preserve the high bits", as cited in #669) is a hypothesis for RDNA 2, not a result, and the same question applies to `v_cvt_f16_f32` and to the binary16 arithmetic results above. A native control must separate preservation, zeroing and any other defined behavior before a reader of the high half can be lowered. Until then this lowering refuses every reader of that half.

`v_cvt_f32_u32_e32` and `v_cvt_f32_i32_e32` convert one 32-bit word to binary32, reading it as unsigned or two's-complement. The e32 encoding is lowered without modifiers: source modifiers, clamp or omod suffixes on the e32 spelling, and DPP or SDWA variants are refused, as is a scalar source whose constant load is still pending. The source is consumed through the same bit-copy representation `v_mov_b32` produces, so `asuint` or `asint` of a moved literal such as `0xFFFFFFFF` or `0x80000000` recovers the exact word before the conversion; the result is emitted as a `precise float` local like the other ALU lowerings. The CPU references convert through Python's binary64 float, which holds every 32-bit integer exactly, then pack to binary32, rounding ties to even; the Scarlett pipeline's MODE.FP_ROUND is unmeasured, so nearest-even is the harness's recorded assumption (`F32_ROUND_ASSUMPTION`), following the binary32 denormal convention, and an adapter disagreeing on a row would be recorded as a mismatch rather than chosen. The inputs are shader literals, not constant-buffer words (issue #360): each draw moves two words with `v_mov_b32_e32`, converts each unsigned and signed, and exports four uncompressed binary32 channels. The words cover 0, 1, the last exactly representable integer 2^24, the ties 2^24+1 and 2^25+2, which round to the even neighbor, the non-tie neighbors 2^25+1 and 2^25+3, which round down and up, and the extremes 0xFFFFFFFF, 0x80000000 and 0x7FFFFFFF. A program that swaps the unsigned and signed conversions on the 0xFFFFFFFF/0x80000000 pair, where every channel changes, must report a mismatch. Reproduce with `ctest --preset dev --no-tests=error -R '^pixel_aot_'`; the LLVM 15 assemble-and-disassemble round trip runs as `python3 -B tests/pixel_aot_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1`.

`v_cvt_f32_u32_e64` and `v_cvt_f32_i32_e64` (issue #360, stage 2) lower the same conversion with the e64 clamp modifier, bit 15 of the raw encoding's first dword: the plain e64 spelling emits exactly the e32 lowering, and with `clamp` the result is wrapped in HLSL `saturate(...)`, so the same word pairs and nearest-even references cover both encodings, ties included. omod is explicitly rejected rather than lowered, per the issue's fallback option: LLVM 15.0.7's assembler rejects every probed omod syntax on these forms — `omod:1`, `omod:2`, `omod:3`, `omod(2)`, `mul:2`, and `clamp` combined with `omod:2` — and also on `v_add_f32_e64` and `v_mul_f32_e64`, and the disassembler never prints omod for the probed raw encodings with both 2-bit fields swept over values 1 to 3, so an omod-carrying program cannot round-trip through the assembler. abs and neg on the integer source (`|s44|`, `-s44`) are refused as well; the assembler refuses them too. The clamp draws pair (0, 1), (2, 0xFFFFFFFF) and (0x80000000, 0x7FFFFFFF), expecting min(1.0, max(0.0, .)) of the u32f/i32f reference for each word: rows (0, 0, 1, 1), (1, 1, 1, 0) — 0xFFFFFFFF is 4294967295.0 unsigned, which rounds to 4294967296.0, but -1.0 signed — and (1, 0, 1, 1), where 0x7FFFFFFF rounds to 2147483648.0 before clamping. Two negative controls run on every backend: dropping `clamp` from the program exports u32 of 2 as 2.0 against the clamped 1.0 reference still on disk, and swapping the unsigned and signed conversions under clamp on the 0xFFFFFFFF/0x80000000 pair exchanges 1.0 and 0.0 on every channel. The rounding assumption is unchanged: the clamp rows contain no tie words and every conversion either is exact or clamps to exactly 0.0 or 1.0, so they do not depend on it, while the plain e64 rows keep the stage-1 tie coverage under the recorded nearest-even reference. Binary and draw fixtures additionally verify SGPR, VGPR and literal operands across negative and positive integers. Mutating `v_cvt_f32_i32` to `v_cvt_f32_u32` or dropping `clamp` produces a pixel mismatch. The reproduction commands are unchanged.

`v_cvt_u32_f32_e32` and `v_cvt_i32_f32_e32` convert one binary32 value to a 32-bit word. Only the plain e32 encoding with a VGPR, an SGPR or a hex literal source is lowered, through the shared helpers in `src/ffx_float_to_int.hlsli`, emitted once before `main`; the signed path is restricted to the entry mask; the result word is held as its bit pattern in a `precise float` local, like a `v_mov_b32` literal, and the draws read it back through the integer export. A float export of such a word is translated the same way, but no draw checks it, so whether a float render target keeps subnormal or NaN bit patterns there is untested. Decimal inline constants are refused because `fp_operand` reads them as bit patterns and the f32 reading has not been checked against the ISA. e64 spellings, source modifiers, clamp and omod suffixes, DPP and SDWA variants, `v_cvt_rpi_i32_f32`, `v_cvt_flr_i32_f32` and a pending scalar load are refused. The rule is the one in issue #360, which cites the RDNA 2 ISA; it was not re-checked against the ISA document and not measured on the console (`F32_TRUNC_ASSUMPTION`): results truncate toward zero, NaN gives 0, and out-of-range values saturate (u32: x <= 0 gives 0, x >= 2^32 gives 0xFFFFFFFF; i32: x <= -2^31 gives 0x80000000, x >= 2^31 gives 0x7FFFFFFF). Truncation is assumed to ignore MODE.FP_ROUND. The input is exact in every denormal mode, since a subnormal truncates to 0; result words 0x00000001 to 0x007FFFFF are binary32 subnormal bit patterns in a `precise float`, and the harness compiles with `-denorm preserve`. Whether an FTZ build would flush them is not claimed and not tested, and a later float instruction that reads such a result would follow the pipeline's `FLOAT_MODE`. The draws move literal words with `v_mov_b32_e32`, convert each unsigned and signed, and export through the lossless `R32G32B32A32_UINT` readback. They cover 18 words in 9 pairs: both zeros, 1.5, -2.75, -0.5, -1.0, a quiet NaN and a negative NaN with payload, both infinities, 2^32, 4294967040, 2^31, 2147483520, -2^31, the next binary32 below -2^31 (the issue's -2^31-1 is not representable and rounds to -2^31), the smallest subnormal and 0.99999994; a further draw reads 3.75, -3.75, 1e10 and -1e10 from SGPRs. A program that swaps the two conversions on -0.5 and -1.0 must report a mismatch. Measured on WARP (driver 10.0.26100.9818) and an NVIDIA GeForce RTX 3080 (driver 32.0.16.2124): every row passes on both. Dev-time mutants (not in CI): replacing the unsigned helper's guards with a bare `uint(x)` fails on both WARP and the RTX 3080, so the saturation guards matter. The helpers apply the cast only to an operand that is made NaN-free and clamped to the largest binary32 below 2^32 or 2^31, because an out-of-range or NaN cast is undefined in HLSL and poison in LLVM. NaN fails every guard compare, so it reaches the cast as 0. Dropping that NaN replacement from both helpers fails on both adapters: the NaN rows then export 0x80000000 from the signed conversion instead of 0. The LLVM 15 round trip (`pixel_aot_check.py binary`) runs in the Ubuntu CI job and also passed locally with LLVM 15.0.7 on Windows. Reproduce with `ctest --preset dev --no-tests=error -R '^pixel_aot_'`.

`v_cvt_u32_f32_e64` and `v_cvt_i32_f32_e64` extend the truncating float-to-word lowering to VOP3 with optional source `abs` and `neg` modifiers (`|src|`, `abs(src)`, `-src`, `neg(src)`, `-|src|`, `-abs(src)`, `neg(abs(src))`, `neg(|src|)`) on VGPR, SGPR or hex literal sources under the entry mask. Source modifiers operate bitwise on the raw binary32 word (`& 0x7fffffffu` for `abs`, then `^ 0x80000000u` for `neg`) before calling the same shared `cvt_u32_f32` and `cvt_i32_f32` helpers from `src/ffx_float_to_int.hlsli`, so plain e64 emits identical HLSL to e32 and NaN/infinity/subnormal inputs avoid FP modifier quirks. `clamp`, `omod`, decimal/float literals, bare `-0x...` hex negation, reversed modifiers (`|-src|`), DPP/SDWA variants, pending scalar/sample reads, WQM mask execution, and `v_cvt_flr_i32_f32` / `v_cvt_rpi_i32_f32` remain rejected. The binary round-trip and lossless `R32G32B32A32_UINT` draws (`check_cvt_float_int_e64_draw`) cover plain e64 parity across all 18 `CVT_FLOAT_WORDS` (SGPR and VGPR), five modifier sets across fractions, saturation endpoints (`-(2^32-256)`, `-2^31`, `-2^32`, `2^31-128`), `±Inf`, `-0.0`, quiet/signaling NaNs and `-min_subnormal`, bitwise single-bit flip negative controls, a plain `u32`/`i32` swap control, and shader-mutation negative controls dropping each `abs` and `neg` modifier individually. Reproduce with `ctest --preset dev --no-tests=error -R '^pixel_aot_cvt_float_int_e64_'`.

`v_cvt_flr_i32_f32` and `v_cvt_rpi_i32_f32` lower in both VOP1 (`_e32`) and VOP3 (`_e64`, with optional bitwise source `abs` and `neg` modifiers) under the entry mask, through lazy HLSL helpers (`cvt_flr_i32_f32` and `cvt_rpi_i32_f32`), **restricted to inputs statically proven finite and in `[-2^31, 2^31)` after source modifiers** (hex literals and constant-initialized SGPR/VGPR moves). Because the AMD RDNA 2 VOP1 table states only the finite rounding formulas and denormal support for opcodes 12 and 13 without an exceptional-input rule like opcodes 7 and 8, arbitrary runtime inputs (constant-buffer SGPRs, interpolated/sample/computed VGPRs) and any NaN, infinity, or out-of-range constant (including post-modifier overflow such as `|0xcf000000|`) are explicitly rejected, and `tools/scan_unsupported_shaders.py` leaves them unsupported for generic title scans until a native RDNA reference establishes exceptional-input behavior. `cvt_rpi_i32_f32` evaluates `floor(x) + ((x - floor(x)) >= 0.5f ? 1.0f : 0.0f)` rather than naive `floor(x + 0.5f)` so binary32 inputs one ULP below `0.5` (`0x3effffff`) stay `0` and large odd integers (`2^23 + 1 = 8388609.0f`) do not round up to even. `clamp`, `omod`, decimal/float literals, bare `-0x...`, reversed modifiers, DPP/SDWA spellings, pending scalar/sample reads and WQM mask execution remain rejected. Binary round-trip and lossless `R32G32B32A32_UINT` draws (`check_cvt_flr_rpi_draw`) test signed zeros, positive/negative halves and fractions, single-ULP half boundaries (`0x3effffff`, `0xbf000001`, `0xbeffffff`), `8388609.0f`, `-min_subnormal`, in-range i32 bounds (`2^31 - 128`, `-2^31`), post-modifier in-range `neg(0x4f000000)`, bitwise 1-bit flip controls, `flr`/`rpi`/`i32` swap controls, and modifier-drop mutations. Reproduce with `ctest --preset dev --no-tests=error -R '^pixel_aot_cvt_flr_rpi_'`.

The same binary and draw checks cover `v_trunc_f32` in e32 and e64 forms. The lowerer emits HLSL `trunc`, which rounds toward zero and returns a floating-point value. With two constant sets, the RTX 5080 and WARP draws matched all four channels of 4,096 pixels per draw against the CPU reference. The fixture covers both signs, signed zero, source negation and absolute value, and both clamp bounds. Replacing truncation with floor changed the first pixel on both adapters. Removing the upper clamp changed alpha from 1 to 2 in the first pixel on WARP. The source-built fixture verifies desktop readback with the bindings above. Execution with a title's own pipeline state remains unverified.

The `v_ceil_f32` fixture checks rounding toward positive infinity in e32 and e64 encodings. It compares finite integral and fractional inputs of both signs, including signed zero, and exercises e64 absolute value, negation and clamp. Replacing the e32 ceiling with a floor must produce a pixel mismatch. The translator rejects a ceiling write into a pending texture sample destination before `vmcnt(0)`. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

The `v_cvt_f32_ubyte0` to `v_cvt_f32_ubyte3` fixture converts byte 0 to 3 of a source word to binary32, as an unsigned integer. The source is read as bits through `asuint`, so float modifiers do not apply and the translator does not accept them. Each output reads a different byte of a constant word in e32 and e64 encodings, and the last uses clamp. The words are normal binary32 values with four distinct bytes, so the bits survive the constant buffer and reading the wrong byte changes the result. Replacing `ubyte1` with `ubyte0` must produce a pixel mismatch.

The `v_rcp_iflag_f32` fixture lowers the reciprocal that compilers emit in integer-division sequences as an ordinary `1.0f / x`. The flag it raises for the integer divide has no desktop equivalent, and nothing in the fixture reads it. Power-of-two inputs of both signs keep every reciprocal exact. Zero and negative zero give signed infinities, and clamp turns +inf into 1, through e32 and e64 encodings with absolute value and negation. Replacing the reciprocal with a square root must produce a pixel mismatch.

The `v_cvt_off_f32_i4` fixture converts bits 3:0 of a source word, read as a signed nibble, to binary32 in units of 1/16, from -0.5 for 0x8 to 0.4375 for 0x7. The source is read as bits through `asuint`, so float modifiers do not apply and the translator does not accept them. Four draws present all 16 nibbles under the same upper bits, through e32 and e64 encodings, and the last lane uses clamp. Reading the nibble as unsigned must produce a pixel mismatch.

The `v_mad_legacy_f32` fixture lowers the legacy multiply-add as the existing legacy multiply followed by an unfused add, so a zero factor gives zero even when the other factor is infinite or NaN. The constant sets pair a zero with +inf and with NaN, and also use finite values. A finite cancellation case uses uncompressed binary32 export and rejects a fused CPU reference. The draw tests measure the generated DXIL on desktop D3D12. The e64 negation, absolute-value and clamp modifiers are included. Replacing the instruction with `v_fma_f32` must produce a pixel mismatch, because 0 * inf is NaN there.

`v_mac_legacy_f32` is the same legacy multiply-add with the destination as the accumulator: D = legacy multiply of the two sources, plus the old D, with an unfused add. LLVM 15.0.7 defines it in `VOP2Instructions.td` as `VOP_MAC_LEGACY_F32` (line 416, `HasExtDPP = 0, HasExt32BitDPP = 0`) and `defm V_MAC_LEGACY_F32` (line 630, `Constraints = "$vdst = $src2"`, `SubtargetPredicate = isGFX6GFX7GFX10`), so it has an e32 and an e64 encoding (e64 with `clamp` and source modifiers) and no DPP form. The lowerer takes two sources, reads the destination as the third operand, and refuses `v_mac_legacy_f32_dpp`, an SDWA spelling, a third source and a destination that holds no tracked value. `v_fmac_legacy_f32` is not supported on gfx1013 and stays refused. The fixture covers e32, and e64 with negation, absolute value and clamp, on constant sets that pair a zero with +inf and with NaN, use a non-zero accumulator, and cancel to a finite 0 where a fused reference gives -2^-46. A Python model computes the expected values. Removing the zero rule, dropping the accumulator, and using either source as the accumulator must each produce a pixel mismatch. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_mac_legacy_(warp|hardware)$' -V`. [LLVM 15.0.7 VOP2Instructions.td](https://github.com/llvm/llvm-project/blob/llvmorg-15.0.7/llvm/lib/Target/AMDGPU/VOP2Instructions.td)

The translator accepts the gfx10 scheduling hints `s_clause`, `s_setprio` and `s_sleep`, which affect timing only, and lowers each exactly like `s_nop 0` at the same position. Each operand must fit its RDNA 2 field: 6 bits of clause length minus one, a 2-bit wave priority and a 7-bit sleep duration. A larger value is rejected. `s_code_end` stays rejected: the native program ends at `s_endpgm`, so padding after it never reaches the translator. Any instruction after `s_endpgm` and any hint between the color export and `s_endpgm` is rejected. The hinted program is part of the LLVM assemble and disassemble round trip.

`s_setreg_imm32_b32` writes hardware state, so it is accepted only where the effect on the lowered program is known. Two targets are accepted:

- A whole-register write to `HW_REG_FLAT_SCR_LO` or `HW_REG_FLAT_SCR_HI`, with any 32-bit immediate. These hold the scratch base address. Only `scratch_*` and `flat_*` memory instructions and `s_getreg_b32` can observe it, none of them is lowered, and the fixture checks that each stays rejected after the write.
- `hwreg(HW_REG_MODE, 2, 2)` with 0 or 3. LLVM's `SIDefines.h` names bits 3:2 of MODE the rounding mode of binary16 and binary64 results; 0 is round to nearest even and 3 is round toward zero. The lowerer tracks the value. Shader entry is taken as nearest even, which the existing binary16 lowerings already assume. Under round toward zero every binary16 or binary64 form is rejected by name except `v_cvt_f32_f16`, which is exact, and `v_cvt_pkrtz_f16_f32`, which always truncates. A truncating `v_cvt_f16_f32` is not lowered.

Every other register, field, width or value is rejected, including the directed rounding modes 1 and 2, the binary32 rounding field, the denormal fields (issue #312) and a partial scratch-base write. `s_setreg_b32` and `s_getreg_b32` stay unsupported. The fixture opens with both scratch-base writes, widens under round toward zero, restores nearest even for two narrowings whose truncated results differ, and selects round toward zero again before the pack. It must translate exactly like the same program with `s_nop 0` in place of each write, is part of the assemble and disassemble round trip, and is drawn on both backends; a truncated reference in either narrowing channel must report a `pixel_mismatch`. The translation controls require that dropping the restore, or writing round toward zero before a narrowing or a packed binary16 operation, is rejected.

In the local archive used for the fixed-profile rescan, linear-order counts over the 12,101 PS records (825 not decoded by the count) gave 2,011 records with the scratch-base pair, always at the second and third instruction and never with a `scratch_*` or `flat_*` instruction in the same program; 4,187 whose first MODE write is 3, each before 16-bit interpolation (`v_interp_p1ll_f16`, `v_interp_p2_f16`) and none with a rounding binary16 operation while 3 is selected; and 996 with a rounding binary16 operation after a write of 0. No other register, field or value occurred in any stage. These are counts from one local input, not a statement about other builds.
The same binary and draw checks cover two SOPK forms, `s_addk_i32` and `s_mulk_i32`, on 32-bit scalar values. LLVM prints a negative `simm16` as an unsigned 16-bit word such as `0xfffe`; the lowerer sign-extends it to 32 bits before adding or multiplying modulo 2**32. Each SOPK instruction has one SGPR operand: the destination is also the source. It can hold an inline scalar bit pattern or one constant word from the tracked descriptor, and the result is read by a vector move through `asfloat`. The fixture copies two constant words into scratch SGPRs, multiplies the first by 3, adds -2 to the second and exports both bit patterns. Out-of-range immediates, descriptor-register sources, vector destinations and writes that overlap an outstanding constant load are rejected. `s_addk_i32` writes SCC with signed 32-bit overflow, which the lowerer tracks so a following `s_cmov_b64` can consume it; `s_mulk_i32` leaves SCC unchanged. Changing the multiply immediate from 3 to 5 must report a `pixel_mismatch`. [AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

`v_sin_f32` and `v_cos_f32` take their operand in revolutions and return sin(2πx) and cos(2πx). LLVM 15.0.7 selects them the same way, as the trigonometric instruction applied to x · 1/(2π), and applies a `fract` to the argument only on Southern, Sea and Volcanic Islands (`FeatureTrigReducedRange`), not on GFX9 or GFX10. The lowerer first removes the nearest integer number of turns, which is exact in binary32, then multiplies by 2π and calls HLSL `sin` or `cos`. That keeps the radian argument within [-π, π], where the D3D11.3 functional specification bounds the `sincos` error to 0.0008 absolute in [-100π, 100π]. Inputs of at most half a turn are passed unchanged, so a negative zero keeps its sign. Because the specification allows that error, the fixture quantizes each result to steps of 1/32 with exact operations (multiply by 32, add 0.5, floor, multiply by 1/32), and every reference value lies at least 0.1 step from a step boundary. Four constant sets cover all four quadrants and an input of millions of turns in each set. The program uses both encodings of each instruction, e64 negation and absolute value, and clamp. It is compared bitwise with a CPU reference that reduces each input in double precision. Swapping sine and cosine, and dropping the 2π scale, must each produce a pixel mismatch for all four sets. A third variant drops the reduction and multiplies the unreduced input by 2π. Its mismatch count is printed as `trig_unreduced_mismatches` rather than required. On WARP 10.0.26100.9818 and an RX 9070 XT (driver 32.0.32015.7029), the reduced program matched all four sets, and so did the unreduced variant (`0/4`). In the DXIL that DXC 1.8 emits, the multiply by 2π carries no `dx.precise` flag. One explanation is that both drivers fold it into their own scaling to revolutions, but the drivers were not inspected. The reduction therefore makes no measured difference on these two adapters. It is kept because it is exact and does not depend on that folding. The instruction's own precision on a gfx1013 GPU, infinities, NaN payloads and the sign of a zero result are not measured. [LLVM 15.0.7 trigonometric lowering](https://github.com/llvm/llvm-project/blob/llvmorg-15.0.7/llvm/lib/Target/AMDGPU/SIISelLowering.cpp#L9262) · [D3D11.3 sincos](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm)

`v_cvt_f32_f16` widens the binary16 in the low 16 bits of S0. The lowering builds the binary32 encoding from those bits, so a subnormal half stays exact as m × 2^-24 whatever the desktop denormal mode, and applies `abs` and `neg` modifiers after widening. The draw converts 1.0 with junk in the high half, -|-5.0|, the largest finite half 65504 and the smallest subnormal half 2^-24, which a multiply by the literal 2^24 turns into 1.0. References that read the high half, drop the negation, use a wrong exponent or expect the subnormal flushed must report a mismatch. NaN and infinite halves are not drawn; whether Scarlett quiets a signaling NaN when widening is not measured, and the lowering keeps the payload. SDWA selections of the high half are refused.

Signed and unsigned 32-bit integer minimum, maximum and median instructions (`v_min_{i32,u32}` and `v_max_{i32,u32}` in e32 and e64 encodings, plus `v_min3_{i32,u32}`, `v_max3_{i32,u32}` and `v_med3_{i32,u32}`) reinterpret operands with HLSL `asint`/`asuint`, store the winning 32-bit pattern via `asfloat`, and reject float source modifiers, `clamp` and writes to pending sample destinations. Binary and draw fixtures cover all 14 forms using exact binary16 float bit patterns whose signed, unsigned and floating-point orderings differ (`-2.0`, `-1.0`, `1.0` and `0.5`, `-0.5`, `4.0`), including tie inputs for `med3`. Replacing `v_min_i32` with `v_min_u32` or `v_med3_i32` with `v_med3_u32` must report a pixel mismatch.
`v_cvt_f16_f32` narrows S0 to binary16, rounding to nearest even. The lowering builds the encoding in integer arithmetic, so desktop conversion and denormal handling cannot change it. Overflow, including the 65520 tie, gives infinity. A binary16 subnormal result is kept, and a binary32 subnormal input gives a signed zero. On GFX10, a 16-bit result replaces only the low half of the destination and keeps the high half (LLVM `GCNSubtarget::zeroesHigh16BitsOfDest`). The lowering merges into the destination's tracked value, or into zero when it has none. One draw reads the whole word by narrowing into a register that holds 1.0, then splitting `(v - 1) × 2^23` into the two bytes of the encoding. It also widens `-|S0|` and a scaled subnormal back to binary32. Four constant sets cover:

- ties to even at 1 + 2^-11, 1 + 3 × 2^-11, 1.5 × 2^-24 and 2^-25;
- the largest value below overflow and the overflow tie;
- the carry from the largest subnormal into 2^-14;
- negative subnormals, -0 and a binary32 subnormal input.

Wrong references must each report a mismatch: round-half-up, truncation, early or saturating overflow, a missing carry, and a lost sign or negation. NaN inputs are not drawn; the lowering sets the quiet bit and keeps the top payload bits. Whether Scarlett titles run with binary16 denormals enabled, as the lowering assumes, is not measured. `clamp` and SDWA forms are refused.

Integer vector arithmetic covers 9 RDNA2 VOP2 instructions: logical right shift (`v_lshrrev_b32`), arithmetic right shift (`v_ashrrev_i32`), logical left shift (`v_lshlrev_b32`), bitwise operations (`v_and_b32`, `v_or_b32`, `v_xor_b32`), and 32-bit addition/subtraction without carry (`v_add_nc_u32`, `v_sub_nc_u32`, `v_subrev_nc_u32`; the VOP3 spellings are listed in the next paragraph). Shift operations preserve RDNA reverse operand ordering, shifting `src1` by `src0 & 31u`. `v_ashrrev_i32` casts `src1` with `asint` to perform signed arithmetic right shifting. Arithmetic addition and subtraction (`v_sub_nc_u32` for `src0 - src1` and `v_subrev_nc_u32` for `src1 - src0`) wrap modulo 32 bits without generating carry bits. All results preserve their 32-bit integer bit pattern in vector registers through `asfloat`. Float modifiers (`-`, `abs`) and uninitialized vector registers are rejected.

The remaining 32-bit no-carry forms lower as well, building on the e32 `v_subrev_nc_u32` lowering and its reverse-subtract draw check from #570: `v_subrev_nc_u32_e64`, `v_add_nc_u32_e64`, `v_sub_nc_u32_e64`, and the VOP3-only `v_add_nc_i32` and `v_sub_nc_i32`, in the spelling LLVM 15 prints (no `_e64` suffix for the two `_i32` forms). All of them wrap modulo 2^32 and keep the bit pattern through `asfloat`; the `_i32` forms are the same bits as the `_u32` forms. Subrev computes `src1 - src0`, following the translator's existing float `subrev` convention and the LLVM 15 AMDGPU backend, where the gfx10 `V_SUBREV_NC_U32` is an encoding of the reversed `V_SUBREV_U32`; this order is not checked against the ISA document here. These spellings are refused: ` clamp` on any of them, the hand-written `v_add_nc_i32_e64` and `v_sub_nc_i32_e64` (LLVM 15 assembles them but prints the form unsuffixed, and `docs/coverage/gfx1013.tsv` has no `_e64` row), `abs(...)` and `-` on an e64 source, and any DPP control other than `quad_perm` and `dpp8`, which the existing DPP table already lowers for the three `_u32` spellings. The scanner has no DPP pattern and reports every DPP form as unsupported. The `v_add_nc_u32_e64` and `v_sub_nc_u32_e64` spellings, which the previous check refused, are now accepted. The scanner pattern for the family is one expression over the existing coarse `INTEGER_SOURCE` class, so it also accepts operands such as `v106` that the translator refuses. Float-written inline constants are taken as their binary32 bits by both, as for the #495 forms. Three programs draw the forms bit for bit through the `R32G32B32A32_UINT` readback target, with A and B taken from constants s44 and s45: group 1 is `v_add_nc_u32_e64`, `v_sub_nc_u32_e64`, `v_subrev_nc_u32_e32` and `v_subrev_nc_u32_e64`; group 2 is `v_add_nc_i32`, `v_sub_nc_i32` and the two e32 forms; group 3 uses inline constants and register-first operands (`B - 64`, `64 + B`, `A - 64`, `A - B`). Groups 1 and 2 run on five (A, B) rows chosen to wrap unsigned, overflow signed, differ between sub and subrev, borrow and wrap through the high bit, and group 3 runs on one, which gives 11 draws from 3 compiles. Every input and result is a normal binary32 or a signed zero, which the test asserts, so the draw does not depend on NaN or denormal handling. Group 1 with the expected sub and subrev words swapped must report `pixel_mismatch`. The draws run as `pixel_aot_integer_nc_hardware` and `pixel_aot_integer_nc_warp` (`tests/pixel_aot_check.py draw-integer-nc`), and the LLVM 15 round trip covers the three programs. They passed on the NVIDIA GeForce RTX 3080 (driver 32.0.16.2124) and on WARP (Microsoft Basic Render Driver 10.0.26100.9818), Windows 11 (10.0.26200.9974), debug layer enabled with no errors or warnings. They show that the translated shader computes the stated order on desktop GPUs, not that the console does.

`v_xnor_b32` lowers in the VOP2 (e32) and VOP3 (e64) encodings as `~(src0 ^ src1)` on the register bits, through the same integer table and operand rules as `v_xor_b32`; before this it was accepted only as a quad-local DPP form. The operation is taken from the LLVM 15 AMDGPU backend, where `VOP2Instructions.td` selects `V_XNOR_B32` for `not (xor a, b)`; it is not checked against the ISA document here. A float modifier, ` clamp`, a wrong operand count and an unwritten register are refused; the SDWA spelling is described in the next paragraph. The integer e64 encodings that the translator already accepted but no fixture drew are now drawn with it: `v_and_b32_e64`, `v_or_b32_e64`, `v_xor_b32_e64`, `v_not_b32_e64`, `v_lshlrev_b32_e64`, `v_lshrrev_b32_e64`, `v_ashrrev_i32_e64` and `v_cvt_f32_ubyte2_e64`. Three programs export through the `R32G32B32A32_UINT` readback target and are compared bit for bit, with A, B and C taken from constants s44, s45 and s46. The shift group computes `B << (A & 31)`, `B >> (A & 31)` logical and arithmetic, and `~B`, with both sources in SGPRs, which the e32 encoding cannot express for the second source; the bitwise group computes and, or, xor and xnor of A and B the same way; the third group draws `v_xnor_b32_e32` and `v_cvt_f32_ubyte2_e64` from an SGPR, from a VGPR and with ` clamp`. Each group runs on two rows. The shift counts are 0x3f800024 and 0x40000023, so a count that is not reduced modulo 32 gives a different word, and B is negative in the first row, so the logical and arithmetic shifts differ. Every input and every expected result is a normal binary32 or zero, which the test asserts, so the draw does not depend on NaN or denormal handling. Fourteen wrong references, each replacing one channel on the first row, must report `pixel_mismatch`: swapped shift operands, an unreduced shift count, arithmetic for logical shift and the reverse, a dropped not, or for and, and for or, xnor for xor and the reverse (in both encodings), byte 1 and byte 3 for byte 2, the wrong source register, and a dropped clamp. That is 20 draws from 3 compiles and 3 renderer launches. They run as their own tests, `pixel_aot_e64_integer_hardware` and `pixel_aot_e64_integer_warp` (`tests/pixel_aot_check.py draw-e64-integer`), with their own coverage record, and the LLVM 15 round trip covers the three programs; LLVM 15 prints all twelve instructions as written for gfx1013 and gfx1030. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. They show that the translated shader computes the stated operations on desktop GPUs, not that the console does.

The SDWA forms of fifteen 32-bit integer operations lower as well: `v_and_b32`, `v_or_b32`, `v_xor_b32`, `v_xnor_b32`, `v_not_b32`, `v_lshlrev_b32`, `v_lshrrev_b32`, `v_ashrrev_i32`, `v_add_nc_u32`, `v_sub_nc_u32`, `v_subrev_nc_u32`, `v_min_i32`, `v_max_i32`, `v_min_u32` and `v_max_u32` (`_sdwa`). Each is the e32 operation applied to the selected source words, with the result placed by the SDWA move rules above (`sdwa_source` and `sdwa_dest`). Unlike the SDWA float arithmetic, byte and word sources are accepted, zero-extended or sign-extended under `sext()`, on either source, including the shift count: an integer operation has no denormal mode, so the reason given above for whole-dword float sources does not apply. The source for these semantics is the same as for the SDWA moves, the instruction equivalences in LLVM 15's `SIPeepholeSDWA.cpp`, read at tag `llvmorg-15.0.7`. That pass matches `v_lshrrev_b32` or `v_ashrrev_i32` by 16 or 24, `v_bfe_u32` or `v_bfe_i32` of a whole byte or word, and `v_and_b32` with 0xff or 0xffff, and rewrites whichever SDWA-capable instruction uses the extracted value so that the matching `src0_sel` or `src1_sel` (with the sext modifier for the signed matches) replaces the extract; it has no list of consumer opcodes and no check that the consumer is a move. It folds `v_lshlrev_b32` by 16 or 24 of a result into `dst_sel` with `UNUSED_PAD` on the defining instruction in the same way, and a `v_or_b32` of two padded SDWA results into `UNUSED_PRESERVE`. So every accepted shape is, by that pass, equal to an extract, the e32 operation and a placement, each of which the translator already lowers. This is an argument from the compiler, not a measurement on the console, and it is not checked against the ISA document here. Refused, each with a diagnostic: `UNUSED_SEXT` (the pass never produces it), `UNUSED_PRESERVE` onto a destination without a tracked numeric value, ` clamp`, an inline constant or literal source (LLVM 15 assembles one, but no equivalence above covers it), float modifiers, a spelling that does not print every field, and the SDWA spelling of any other integer operation. Four programs draw every form once through the `R32G32B32A32_UINT` readback target, bit for bit, with A, B, C and D taken from constants s44 to s47: the bitwise group selects a sign-extended `BYTE_1`, `WORD_1`, two bytes placed into `BYTE_3`, and a sign-extended `WORD_0`; the shift group takes its counts from `BYTE_1`, `BYTE_0` and `BYTE_2` of A (0x24, 0x25 and 0x23 in the first row, so a count not reduced modulo 32 differs), shifts a sign-extended `WORD_1` into `WORD_1`, and writes `~BYTE_2` into `WORD_0` of a register under `UNUSED_PRESERVE`; the arithmetic group adds `BYTE_3`, subtracts a sign-extended `BYTE_0`, reverse-subtracts `WORD_0`, and adds two bytes into `BYTE_1` under `UNUSED_PRESERVE`, where the carry out of the byte must be dropped; the min/max group compares sign-extended and zero-extended words and bytes and one whole-dword pair. Each group runs on two rows. Every input and every expected result is a normal binary32 or zero, which the test asserts, so the draw does not depend on NaN or denormal handling. Forty wrong references, each replacing one channel, must report `pixel_mismatch`: a dropped or added sign extension on either source, the neighbouring byte or word, an ignored selection, swapped selections, a result placed in the wrong field, padding for `UNUSED_PRESERVE`, a carry leaving its byte, a shift count from the wrong byte or not reduced, arithmetic for logical shift, swapped subtraction order, signed for unsigned comparison, and a neighbouring operation for each form. That is 48 draws from 4 compiles and 4 renderer launches. A static control asserts that with whole-dword selections each form emits the statement of its e32 form. They run as their own tests, `pixel_aot_sdwa_integer_hardware` and `pixel_aot_sdwa_integer_warp` (`tests/pixel_aot_check.py draw-sdwa-integer`), with their own coverage record, and the LLVM 15 round trip covers the four programs; LLVM 15 prints all fifteen instructions as written for gfx1013 and gfx1030. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. They show that the translated shader computes the stated words on desktop GPUs, not that the console does.

The carry-out add and subtracts `v_add_co_u32`, `v_sub_co_u32` and `v_subrev_co_u32` lower as a 32-bit wraparound result plus a per-lane condition written to the lane mask operand, tracked like a comparison result so that `v_cndmask_b32` can read it. The add writes 1 when the 33-bit sum does not fit in 32 bits; `v_sub_co_u32` computes `src0 - src1` and writes 1 when `src1 > src0` (unsigned); `v_subrev_co_u32` computes `src1 - src0` and writes 1 when `src0 > src1`. The semantics are taken from the LLVM 15 AMDGPU backend, which selects `V_ADD_CO_U32` for `uaddo` and `V_SUB_CO_U32` for `usubo` (`AMDGPUISelDAGToDAG.cpp`, `SelectUADDO_USUBO`) and defines `V_SUBREV_CO_U32` with `v_sub_co_u32` as its reversed-operand counterpart (`VOP2Instructions.td`); they are not checked against the ISA document here and not measured on a console. gfx10 encodes the three as VOP3 only and LLVM 15 prints them without a suffix, so only that spelling is accepted. The mask is `vcc`, `vcc_lo`, one SGPR or an SGPR pair, and each source is a register, an inline constant or a literal. An `_e32` or `_e64` suffix, ` clamp`, a float modifier, `exec` or a VGPR as the mask, a missing or extra operand, an unwritten register and DPP controls are refused. Three programs export through the `R32G32B32A32_UINT` readback target and are compared bit for bit. Each computes op(A, B) with the carry in `vcc_lo` and op(B, A) with the carry in `s60`, and two `v_cndmask_b32_e64` selects turn each carry into one of two constants. The add runs on four rows: a carry, no carry, a zero addend (the sum equals `src0` and must not carry) and a sum that wraps to exactly zero (it must carry). Each subtract runs on three rows: a borrow in one operand order and none in the other, equal operands, and the larger operand first. Both carry states are drawn for each mask register, and every input and every expected result is a normal binary32 or zero, which the test asserts. Seventeen wrong references, each replacing one channel of one row, must report `pixel_mismatch`: subtract for add, add for subtract, reversed operands, a dropped carry or borrow on each mask register, a carry without overflow, a carry on a sum equal to `src0`, a dropped carry on a sum of zero, a borrow on equal operands and the borrow of the other operand order. That is 27 draws from 3 compiles and 3 renderer launches. They run as their own tests, `pixel_aot_carry_out_hardware` and `pixel_aot_carry_out_warp` (`tests/pixel_aot_check.py draw-carry-out`), with their own coverage record, and the LLVM 15 round trip covers the three programs. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled. They show that the translated shader computes the stated operations on desktop GPUs, not that the console does. A lane mask written by these instructions is read by `v_cndmask_b32` and by the carry-in forms below; scalar reads of the mask register and execution-mask use stay refused.

The carry-in add and subtracts `v_add_co_ci_u32`, `v_sub_co_ci_u32` and `v_subrev_co_ci_u32` lower in both encodings, VOP2 (`_e32`) and VOP3 (`_e64`), as the carry-out operations above with the carry or borrow of each lane read from a lane mask. The add computes `src0 + src1 + carry` and writes 1 when the 33-bit sum does not fit in 32 bits; `v_sub_co_ci_u32` computes `src0 - src1 - borrow` and writes 1 when `src1 + borrow > src0` (unsigned); `v_subrev_co_ci_u32` computes `src1 - src0 - borrow` and writes 1 when `src0 + borrow > src1`. The lowering keeps both steps in 32-bit unsigned arithmetic: the add carries when either step wraps, and a subtract borrows when the subtrahend exceeds the minuend or the borrow-in exceeds the partial difference. The source is again the LLVM 15 AMDGPU backend, read at tag `llvmorg-15.0.7`: it selects `V_ADDC_U32_e64` for `ISD::ADDCARRY` and `V_SUBB_U32_e64` for `ISD::SUBCARRY` (`AMDGPUISelDAGToDAG.cpp`, `SelectAddcSubb`), the multiple-precision add and subtract with carry or borrow in and out; it defines `V_SUBBREV_U32` with `v_subb_u32` as its reversed-operand counterpart (`VOP2Instructions.td`); and LLVM 15 prints the three on gfx10 as `v_add_co_ci_u32`, `v_sub_co_ci_u32` and `v_subrev_co_ci_u32`. The semantics are not checked against the ISA document here and not measured on a console. LLVM 15 prints both encodings with their suffix, so only those spellings are accepted. The `_e32` form reads and writes `vcc` or `vcc_lo` and takes a VGPR as `src1`; the `_e64` form names both masks, each `vcc`, `vcc_lo`, one SGPR or an SGPR pair, and both must have the same width. Each source is a register, an inline constant or a literal. The carry-in must be a per-lane condition the translator tracks, written by a comparison or a carry-out, exactly as for `v_cndmask_b32`, and the carry-out replaces the condition in its mask. The forms without a suffix, ` clamp`, a float modifier, `exec` or a VGPR as either mask, an `_e32` form with another mask or an SGPR `src1`, masks of different widths, a carry-in that no instruction wrote or that holds a constant, a missing or extra operand, an unwritten register, and the DPP and SDWA forms are refused. Three programs, one per operation, export through the `R32G32B32A32_UINT` readback target and are compared bit for bit. Each takes A, B, C and D from the constants, computes the carry-out form of the same operation on (A, B) into `vcc_lo` and on (A, D) into `s60`, then op(C, D, `vcc_lo`) in the `_e32` encoding and op(D, C, `s60`) in the `_e64` encoding with its carry-out in `s61`. Two `v_cndmask_b32_e64` selects turn the carry-outs into 0.5 or 1.0, and 2.0 (0x40000000) is added to both results, a bijection modulo 2^32 that moves the all-ones word of a borrow taken from the borrow-in alone to a normal number. Each operation runs on four rows: both carry-ins set with both carry-outs coming from the carry-in alone (C + D is 0xffffffff, or C = D), both carry-ins clear with a carry or borrow from the operands, and one row each with only `vcc_lo` or only `s60` set. Each carry-in and each carry-out takes both states, and every input and every exported word is a normal binary32 or zero, which the test asserts. Twenty-seven wrong references, each replacing one channel of one row, must report `pixel_mismatch`: a dropped carry-in or borrow-in, a carry-in read as set, a carry-out or borrow-out without the carry-in term on each mask, a dropped carry-out of the operands, the carry-in of one encoding read from the other's mask, subtract for add, add for subtract, a borrow-in added, reversed operands, and the borrow-out of the other operand order. That is 39 draws from 3 compiles and 3 renderer launches. As a check of the fixture, dropping the `{r} < {p}` term from the add's carry-out in `tools/pixel_aot.py` makes the first row mismatch on WARP. They run as their own tests, `pixel_aot_carry_in_hardware` and `pixel_aot_carry_in_warp` (`tests/pixel_aot_check.py draw-carry-in`), with their own coverage record, and the LLVM 15 round trip covers the three programs. They passed on the NVIDIA GeForce RTX 5080 (driver 32.0.16.2124), on the AMD Radeon(TM) Graphics iGPU (vendor 1002, device 13c0, driver 32.0.11034.2, selected with `XBOX2WINDOWS_ADAPTER=Radeon`) and on WARP (Microsoft Basic Render Driver 10.0.26100.9818), Windows 11 (build 26300), with the debug layer unavailable. They show that the translated shader computes the stated operations on desktop GPUs, not that the console does. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_carry_in_' -V`.

`v_cvt_pk_u16_u32` and `v_cvt_pk_i16_i32` lower through the integer table as two-source operations that exist in the VOP3 encoding only on gfx10. The AMD RDNA 2 ISA reference (30 November 2020, VOP3 opcodes 874 and 875) gives the layout: `D.u16_lo = u32_to_u16(S0.u32); D.u16_hi = u32_to_u16(S1.u32)` and `D.i16_lo = i32_to_i16(S0.i32); D.i16_hi = i32_to_i16(S1.i32)`, so src0 is the low half and src1 the high half. Neither that document nor the RDNA 3.5 reference defines `u32_to_u16` or `i32_to_i16`, and LLVM 15 gives the instructions no meaning beyond their types (`VOP_V2I16_I32_I32`, selected only from the `llvm.amdgcn.cvt.pk.u16` and `llvm.amdgcn.cvt.pk.i16` intrinsics). The saturation used here is the one Mesa states: its NIR operations `pack_uint_2x16` and `pack_sint_2x16` are documented as "Convert two unsigned integers into a packed unsigned short (clamp is applied)" and the signed counterpart, their constant folding clamps each source to 0..65535 or to -32768..32767, and the ACO backend selects exactly these two instructions for them with no clamp of its own. That is a statement by the open source driver for this hardware family, not by the ISA document, and it is not measured on the console. The lowering is `min(S0, 65535) | (min(S1, 65535) << 16)` on unsigned words, and the signed form clamps each source as a signed word and keeps its low 16 bits. The e32 spelling (gfx10 has none), ` clamp`, a float modifier, a wrong operand count, an unwritten register, a literal wider than 32 bits and the SDWA spelling are refused. The float packs `v_cvt_pknorm_i16_f32`, `v_cvt_pknorm_u16_f32`, `v_cvt_pknorm_i16_f16`, `v_cvt_pknorm_u16_f16` and `v_cvt_pk_u8_f32` stay unsupported: no public source found here states their rounding, their bounds or their NaN result. Two programs export through the `R32G32B32A32_UINT` readback target and are compared bit for bit, with A and B taken from constants s44 and s45. A normal binary32 word is outside both 16-bit ranges, so each program first derives `B >> 16`, `B >> 12` and the arithmetic `B >> 16` in the shader and packs those with A, a literal 0x12345 and an inline -16. A word whose high half saturates is not a normal binary32, so those channels are exported through an xor with 0x40000000. Each program runs on two rows. In the first row A is positive and B negative: the unsigned pack must turn the literal, `B >> 12`, A and the negative arithmetic shift into 0xffff, and the signed pack must turn A and the zero-extended `B >> 16` into 0x7fff and keep the inline -16 as 0xfff0. In the second row A is negative, which the signed pack turns into 0x8000, and `B >> 12` is 0xfff, which both packs keep. Every input and every expected result is a normal binary32 or zero, which the test asserts. Fifteen wrong references, each replacing one channel on the first row, must report `pixel_mismatch`: truncation instead of saturation in either half, swapped halves, the signed bound on the unsigned pack and the reverse, a negative source clamped to zero or kept, a value above 32767 kept, and the inline constant read as unsigned. That is 19 draws from 2 compiles and 2 renderer launches. They run as their own tests, `pixel_aot_pack_int16_hardware` and `pixel_aot_pack_int16_warp` (`tests/pixel_aot_check.py draw-pack-int16`), with their own coverage record, and the LLVM 15 round trip covers both programs; LLVM 15 prints the two instructions without an encoding suffix. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466). They show that the translated shader computes the stated operations on desktop GPUs, not that the console does.

`v_pack_b32_f16` lowers through the integer table as a two-source operation that exists in the VOP3OpSel encoding only on gfx10. The AMD RDNA ISA reference gives `D[15:0].f16 = S0.f16; D[31:16].f16 = S1.f16`: each source's low binary16 encoding is copied into its half of the destination with no conversion, rounding or NaN handling, so the lowering is `(S0 & 0xffff) | ((S1 & 0xffff) << 16)` on the raw words. LLVM 15 prints the gfx1013 form without a suffix, and `tools/ffx_dispatch_aot.py` already lowers the same mnemonic with this expression. The e32 spelling (gfx10 has none), ` clamp`, ` op_sel` (only the default `[0,0]` shape is modelled), a float modifier, a wrong operand count, an unwritten or out-of-range register, a literal wider than 32 bits and the SDWA spelling are refused; sources go through the existing integer operand rules, so an outstanding sample is refused as well. An inline float constant is the exception: `llvm-mc` types these sources as binary16 and prints the constant as a decimal float, and its binary16 encoding is used (`1.0` is `0x3c00`), as for the packed `VOP3P` forms (not measured on the console); any other decimal float is refused. One program exports through the `R32G32B32A32_UINT` readback target and is compared bit for bit, with A and B taken from constants s44 and s45, copied to v18 and v19 and carrying the distinct noisy upper halves 0xa5a5 and 0x5a5a. Three packs read A and B as register/register, SGPR/register and register/SGPR; a fourth writes back to v18, its own src0, so a lowering that writes the destination before reading src0 differs. Five rows vary the low halves: an asymmetric pair for the controls, a quiet NaN half, a signed-zero half, a NaN half in src1 (which lands in bits 31:16 but still yields a normal binary32 word) and two zero halves, which must give zero. Every input and every expected word is a normal binary32 or zero, which the test asserts; the NaN and signed-zero half patterns are copied as bits with no quieting or conversion and stay in the low halves of the packed words. Five wrong references, each replacing one channel of the first row, must report `pixel_mismatch`: swapped halves, src0's high half leaked into the low half, src0 taken from bits 31:16, src1 placed in the low half, and src0 read after the in-place destination write. The tests run as `pixel_aot_pack_b32_hardware` and `pixel_aot_pack_b32_warp` (`tests/pixel_aot_check.py draw-pack-b32`), with their own coverage record, and the LLVM 15 round trip covers the program. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466). They show that the translated shader computes the stated operation on desktop GPUs, not that the console does. The inline-constant variant also goes through the LLVM 15 round trip. Its integer render target compares 0xc0003c00 with the old binary32-low-half result 0xc0000000, which must report a mismatch on each desktop backend.

`v_sad_hi_u8`, `v_sad_u32` and `v_msad_u8` lower through the integer table as three-source VOP3 operations. The definitions are the ones in the AMD RDNA 2 ISA reference (30 November 2020, VOP3 opcodes 347, 349 and 369), all with `ABSDIFF(x, y) := (x > y ? x - y : y - x)` under an unsigned comparison: `v_sad_hi_u8` is `(SAD_U8(S0, S1, 0) << 16) + S2`, where `SAD_U8` sums `ABSDIFF` over the four byte lanes; `v_sad_u32` is `ABSDIFF(S0, S1) + S2` on the whole words; `v_msad_u8` is `S2` plus `ABSDIFF` of each byte lane whose `S1` byte is not zero ("Components where the reference value in S1 is zero are not included in the sum"). The RDNA 3.5 reference gives the same three definitions, and LLVM 15 agrees on the dword form: `SIInstructions.td` selects `V_SAD_U32` for `umax(a, b) - umin(a, b) + c`. In the text extracted from the RDNA 2 PDF each definition box sits beside the following opcode row, so the dword definition appears next to opcode 350; the description sentences and the RDNA 3.5 text settle the attribution. The sums are taken modulo 2^32, which the documents imply through the 32-bit unsigned destination and the note that overflow into the upper bits is allowed. A helper `sad4_u8` is emitted only when a program uses a byte-lane form. The e32 spelling, ` clamp` (LLVM 15 assembles the bit, but the ISA text gives it no meaning for an integer result), a float modifier, a wrong operand count, an unwritten register and a literal wider than 32 bits are refused; `v_sad_u8`, `v_sad_u16`, `v_qsad_pk_u16_u8`, `v_mqsad_pk_u16_u8` and `v_mqsad_u32_u8` stay unsupported. Two programs export through the `R32G32B32A32_UINT` readback target and are compared bit for bit, with A, B and C taken from constants s44, s45 and s46. The byte group draws `v_sad_hi_u8` with a register and with an inline zero accumulator and `v_msad_u8` in both source orders; the dword group draws `v_sad_u32` with a register and with an inline accumulator, `v_msad_u8` against a literal reference whose byte 2 is zero, and `v_sad_hi_u8` with a VGPR accumulator. No instruction uses more than two scalar registers and literals, the gfx10 limit LLVM 15 enforces when assembling. Each group runs on two rows. In the first byte row A and B each have one zero byte in a different lane and bytes on both sides of 0x80, so the masked, unmasked and wrongly masked sums are 0x1c3, 0x242 and 0x17f; in the first dword row A and B differ in the sign bit and the sum with C passes 2^32. Every input and every expected result is a normal binary32 or zero, which the test asserts. Eighteen wrong references, each replacing one channel on the first row, must report `pixel_mismatch`: a sum not moved to the high half or moved when it should not be, masking applied to `v_sad_hi_u8` or dropped from `v_msad_u8`, the mask taken from src0, signed byte lanes, a dropped top lane, a dropped or wrong accumulator, unswapped operands, a signed dword comparison, and byte lanes for the dword difference. That is 22 draws from 2 compiles and 2 renderer launches. They run as their own tests, `pixel_aot_sad_hardware` and `pixel_aot_sad_warp` (`tests/pixel_aot_check.py draw-sad`), with their own coverage record, and the LLVM 15 round trip covers both programs; LLVM 15 prints the three instructions without an encoding suffix. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466). They show that the translated shader computes the documented operations on desktop GPUs, not that the console does.

To supply a reference for another arithmetic program, run `offscreen_check.exe hardware --check-pixel-case shader.cso reference.case`. The case contains 256 bytes of constants followed by 64 expected RGBA colors in row-major little-endian binary32, one for each texel of the test texture. Generate the expected colors after round-toward-zero half export and widening back to float.

Every draw fixture compiles its translated shader under an explicit binary32 denormal mode: DXC runs with `-denorm preserve` at `ps_6_2`, the minimum shader model that accepts `-denorm`, matching CPU references built from Python floats, which keep binary32 subnormals. The Scarlett pipeline's `FLOAT_MODE` is unmeasured, so preserve is the harness's recorded assumption rather than a claim about the console. A dedicated control draws one program with `v_min_f32` and `v_max_f32` against 1.0 and -1.0, and a `v_mul_f32` by 1.0, under both `-denorm preserve` and `-denorm ftz` with mode-matched references: the least subnormal `0x00000001` survives min and max under preserve and exports zero under ftz, while the multiply by 1.0 folds to a move and must export identical raw bits in both modes; the preserve-compiled shader has to mismatch the ftz reference. Binary16 and binary64 denormal behavior is established by separate fixtures, not by this control. The curve fixture records its mode as `any`: an RTX 5060 Ti (driver 32.0.16.2124) refuses PSO creation for that one program under `-denorm preserve` (`E_OUTOFMEMORY`), while WARP and the RX 9060 XT accept it; the curve rows contain no binary32 subnormals, so the mode cannot change their exported bits, and the fixture asserts that flushing leaves every expected word unchanged. The `e64_approx` program and `sdwa_unary_exp`, the first `SDWA_UNARY` program, also record `any`: on an RTX 5080 (drivers 32.0.16.2101 and 32.0.16.2124) `CreateGraphicsPipelineState` returns `E_OUTOFMEMORY` for any pixel shader that uses `log2` under `-denorm preserve`, while `ftz` and `any` are accepted, and WARP and an AMD Radeon integrated GPU accept `preserve` ([#828](https://github.com/R0Y00/Xbox2Windows/issues/828)). The mode switches only for translated programs that contain `log2`, and each such fixture asserts that no input, intermediate result, expected value or wrong reference is a binary32 subnormal, so the mode cannot change what they compute or export; `sdwa_unary_root` and `sdwa_unary_exact` keep `preserve`. With the change, `pixel_aot_e64_approx_hardware` and `pixel_aot_sdwa_unary_hardware` pass on the RTX 5080 (driver 32.0.16.2124), including every wrong reference, and still pass on WARP.

The binary16 denormal mode is recorded as `preserve` and holds by construction rather than by a compile flag. DXIL has no binary16 denormal attribute: `-denorm` only sets `fp32-denorm-mode`. The packed f16 lowering widens each half from its encoding, computes in binary32 and narrows in integer arithmetic, and every binary16 subnormal, and every product or sum of two, is a binary32 normal, so a binary32 flush cannot reach it. The packed result is held as binary32 bits; packed f16 consumers and the export only reinterpret it, which the draw below checks with a word that is a binary32 subnormal. A CPU-side control checks that the lowered `v_pk_*_f16` body uses neither driver conversions (`f32tof16`, `f16tof32`) nor native half types. A draw runs `v_pk_add_f16` on two normal inputs whose sum is the subnormal `0x8001` (the required row: DXIL's `fadd` FTZ text requires such a result to flush), and as supplementary rows `v_pk_max_f16` on the least and largest binary16 subnormals, `v_pk_mul_f16` that makes the subnormal `0x0200` from normal inputs, and `v_pk_fma_f16` whose halves round to nearest even. It reads the packed words back through the integer target, because one of them (`0x80000200`) is a binary32 subnormal as a word. The same preserve reference must pass under both `-denorm preserve` and `-denorm ftz`, and a reference that flushes binary16 subnormals must fail. This shows that the harness's f16 results do not depend on the f32 flag. It says nothing about the Scarlett `FLOAT_MODE` f16 bits, which remain unmeasured, and the compressed-export path, which narrows through the driver's `f32tof16`, is not covered by this control.

A case file may carry a trailer after the 64 expected colors: one flags byte, whose bit 0 selects a `R32G32B32A32_UINT` readback target, followed by 64 per-texel mask bytes whose bit ch accepts a NaN of any sign or payload on that channel. A masked channel passes when the expected word and the exported word are both NaN, and the renderer logs a `nan_class` line with both raw words even on pass; masking a channel whose expected word is not NaN is a case-format error. Unmasked channels stay strict bitwise, comparison-predicate results stay strict, and integer moves, bit operations and bitcasts round-trip bit-for-bit through the integer readback. Any normalization observed on the float render target is specific to that path. The fixtures never claim a payload, sign or quieting rule for NaN results, only the class.

`flat_load_dword` and the `off` form of `global_load_dword` (issue #319) read one DWORD through a 64-bit title address with `TitleLookup` from `src/title_va_lookup.hlsli`, which `lower()` inlines when a program loads through it. The address is the VGPR pair plus the instruction offset, sign-extended to 64 bits with the carry or borrow taken into the high word. LLVM 15 assembles gfx1013 flat offsets as 11-bit unsigned (0 to 2047) and global offsets as 12-bit signed, so a negative offset is only a global load; the flat encodings with bit 11 set, which LLVM prints as offsets 2048 to 4095, are refused. The range table is a `StructuredBuffer<TitleRange>` at `t0, space1` and the aliased buffers are `ByteAddressBuffer TitleAliases[8]` at `t1, space1`, indexed with `NonUniformResourceIndex`, on the same `ps_6_0`-compatible path as `tests/gpu_alias_lookup.hlsl`. The range count is fixed at translation time (`lower(..., title_ranges=N)`); without it both forms are refused. Flat loads additionally require `flat_global_only=True`. The caller guarantees that all effective flat addresses name global memory and exclude the scratch and LDS apertures. An address in no range, a DWORD that does not lie inside a single range, a misaligned address and a slot past the eighth all read `0xdeadbeef`, never zero. The word is ready after a `vmcnt` wait that covers it, counted in issue order with image samples. Wider and sub-DWORD loads, D16 loads, stores, atomics, scratch, the global `saddr` and `lds` forms and cache-policy bits stay refused, each by a control, and the scratch-base writes still translate as no-ops before a flat load. The source-built program reads addresses A and B from constant words 3 to 6 and exports A, A + 2044, B - 2048 and B through the `R32G32B32A32_UINT` target. Six cases cover the first and last whole DWORD of a range, a carry and a borrow across the 32-bit boundary, misses in a hole and below the first range, a DWORD straddling a range end, and four ranges in four slots, two of them adjacent. The address words include quiet-NaN and subnormal patterns, which reach the lookup only through bit copies, as in the integer readback draw. Eight wrong references must report `pixel_mismatch`: the high address word dropped, the immediate ignored, the immediate masked instead of sign-extended, the carry dropped, zero instead of the sentinel, a straddling DWORD accepted, the slot masked to one bit and the range count one short. Each is followed by a passing draw, and the 22 draws run in one `--check-pixel-batch` process. The case trailer's flag bit 2 carries the ranges and the eight slot buffers; the renderer then binds them after the two textures with a root signature that extends the vertex shader's. Run `ctest --preset dev --no-tests=error -R '^pixel_aot_title_loads_(warp|hardware)$' -V`; the LLVM 15 round trip covers the program. The draws show what the translated shader reads from the bound ranges on a desktop GPU, not how the console resolves flat apertures.

`v_fmaak_f32` computes S0 × S1 + K and `v_fmamk_f32` computes S0 × K + S1, where K is the 32-bit literal that LLVM prints in that position. Both use the `v_fma_f32` lowering; the translator refuses a register where the literal belongs. The draw computes each instruction twice with inputs 1.5, 2.5, -3.0 and 0.75 and the literals 0.25, 3.0, -0.5 and 0.5. Every product and sum is exact in binary32, so the reference does not depend on fusion. A reference that swaps the literal with S1 must report a mismatch in each channel.

`v_madak_f32` computes S0 × S1 + K and `v_madmk_f32` computes S0 × K + S1 with the same literal rules as the fma forms: K is the 32-bit literal LLVM prints in that position, a register there is refused, and so are the e64, DPP and SDWA spellings (LLVM 15 itself rejects e64 for these opcodes). They are not fused: both use the `v_mad_f32` helper, which rounds the product to binary32 before the add and flushes binary32 denormals. The draw checks 4097 × 4097 − 16785408 through both instructions, which is 0 unfused and 1.0 fused (exact rational arithmetic gives 1.0), and 2^-127 × 2^126 + 0.5 through both, which is 0.5 with the denormal input flushed and 1.0 without. A reference that expects the fused or the denormal-preserving result in any one channel must report a mismatch, and a dev-time mutation (not part of CI) that lowered the two instructions through `fma` made the draw report `pixel_mismatch`. The draw matched on an RTX 3080 (driver 32.0.16.2124) and on WARP under Windows 11 (10.0.26200.9974). Whether Scarlett flushes a denormal product or sum in these forms, as the helper does, is not measured.

Several VOP3 (e64) encodings were accepted by the translator but drawn by no fixture. One draw now covers `v_floor_f32_e64` of `-|x|`, `v_fract_f32_e64` of `-x`, `v_max_f32_e64` and `v_min_f32_e64` with negation and absolute value on SGPR operands, and another covers `v_fmac_f32_e64` with modified sources. All 16 `v_cmp_*_f32_e64` predicates are also drawn, writing an SGPR mask from negated operands. Negating both operands swaps less and greater and keeps equality and unordered, so the five predicate inputs (including NaN on either side) give a different expected value wherever a dropped negation would change the result. Each such reference must report a mismatch, as must a wrong result in each arithmetic channel. The approximate `v_rcp`, `v_rsq` and `v_sqrt` e64 forms are not drawn by this test, and `v_exp` and `v_log` are drawn by the one described in the next paragraph. These draws run as their own tests, `pixel_aot_e64_hardware` and `pixel_aot_e64_warp` (`tests/pixel_aot_check.py draw-e64`), apart from `pixel_aot_hardware` and `pixel_aot_warp`, so each stays within the 60 s test budget; the controls run once, in the draw and binary modes, and the hardware test reports a skip when the renderer returns 77.

`v_exp_f32_e64`, `v_log_f32_e64` and `v_mac_f32_e64` were also accepted with no fixture drawing them; the translator is unchanged and one program now draws all three. D3D allows `exp2` and `log2` a last-place error, so a raw result has no single binary32 reference. Each input therefore makes the true result a small integer, and the program adds 0.5 and takes the floor, which returns that integer for any result within 0.5 of it: `v_exp_f32_e64` of `-A` and `v_log_f32_e64` of `|B|` are drawn for (A, B) = (-3, -32) and (-4, -8), giving 8 and 5, then 16 and 3. This checks the operation and its source modifiers, which are what e64 adds over e32, and not last-place accuracy. `v_mac_f32_e64` is exact and is compared directly: one channel accumulates `-A * |B|` onto C (96.25 and 32.5), and one repeats the 4097 * 4097 case of the `v_mad_f32` fixture with both sources negated, which is 0 through the unfused helper and 1.0 if fused. Eight wrong references on the first row, each replacing one channel, must report `pixel_mismatch`: a dropped negation on exp, the natural exponential, log for exp, the natural logarithm, exp for log, a dropped negation on mac, a dropped accumulator and a fused multiply-add. That is 10 draws from one compile and one renderer launch. They run as their own tests, `pixel_aot_e64_approx_hardware` and `pixel_aot_e64_approx_warp` (`tests/pixel_aot_check.py draw-e64-approx`), with their own coverage record, and the LLVM 15 round trip covers the program for gfx1013; `v_mac_f32` is not in the gfx1030 list. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. Not shown: last-place accuracy of exp and log on any adapter, inputs outside these rows (zero, negative arguments to log, infinities, NaN), and what the console computes.

Three VOP3 (e64) spellings were unrecognised: `v_interp_p1_f32_e64`, `v_interp_p2_f32_e64` and `v_nop_e64`. LLVM defines each as a second encoding of an instruction the translator already lowers, and LLVM 15 assembles `v_interp_p1_f32_e64 v8, v0, attr0.x` to the two-word VOP3 form (`D6000008 00020000`) and prints it back with the suffix. The translator now accepts the interpolation pair in that spelling only in the operand shape of the e32 form, a plain VGPR source and `attr0`, under the same pairing and mask rules, and emits the same shader as for e32; the two encodings may be mixed within a pair. What the VOP3 encoding adds is not lowered: a negated or absolute source, `clamp`, `mul:2` and `div:2`, an SGPR source and another attribute are each refused, because nothing here establishes how they apply to an interpolation. `v_nop_e64` is accepted wherever `v_nop` is (entry mask, before the export, no operand). LLVM 15 prints both encodings of the no-op as `v_nop`, so a decoded binary already reached that branch; the new spelling is what an assembly fixture writes to select the VOP3 encoding (`D5800000 00000000`), and the round trip assembles it and lowers the decoded text to the same shader. Three programs are drawn, each with one `v_nop_e64`: the sampled fixture with both whole-quad pairs in e64, the channel-swapped one with e32 for p1 and e64 for p2, and the entry-mask attribute fixture with all eight interpolations in e64. The sampled ones are compared with the renderer's texture reference in their own channel order and must report `pixel_mismatch` against the other order; the attribute one is compared per pixel with the CPU reference (1 - v, 0.25 + 0.5u, u, v) over 32 by 32 pixels, and a zeroed channel at one interior pixel must report a mismatch at that pixel, once for each of the four channels. That is 3 matching draws and 6 mismatching ones from three compiles. They run as their own tests, `pixel_aot_e64_spellings_hardware` and `pixel_aot_e64_spellings_warp` (`tests/pixel_aot_check.py draw-e64-spellings`), with their own coverage record. They passed on the NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) and on WARP (Microsoft Basic Render Driver 10.0.19041.5794), Windows 10 (10.0.19045.6466), debug layer enabled with no errors or warnings. That the VOP3 encoding of the interpolation pair computes what the VINTRP encoding does is taken from LLVM's instruction definitions, not measured on a console; the draws show only that the translated shader is the e32 one.

The e64 draws use `offscreen_check <backend> --check-pixel-batch <shader.cso> <case> [<case> ...]`. Each process creates one D3D12 device for a shader and checks the case files in argument order. Each draw retains its own resources and readback. The runner prints a zero-based case index and one of `pass`, `mismatch` or `error`. It exits with 1 if any case fails, including an intentional mismatch, and 77 if hardware is unavailable. The Python fixture checks both the ordered statuses and the process exit code. Each arithmetic batch ends with a matching reference after its negative controls.

`v_fract_f32` returns x - floor(x) clamped to `0x3f7fffff`, so its result stays below 1.0. The lowering uses an explicit clamp, not a bare D3D `frac`, which has no clamp and differs between adapters. For an input of -2^-30 the unclamped difference rounds to 1.0. An RTX 4070 Ti (driver 32.0.16.1088) exported 1.0 for that input, while WARP 10.0.26100.9818 already exported the clamped value. The e64 lanes take the absolute value of -0.25 and the negated absolute value of 2.75 before computing the fraction. Each exports 0.25. Ignoring either source modifier would give 0.75 in that lane. An e32 lane checks -2^-23. A reference that expects 1.0 for the first input must report a mismatch. NaN still propagates: it fails the clamp comparison.

`v_mad_f32` and `v_mac_f32` round the product to binary32 before the add; they are not fused. LLVM 15's AMDGPU backend selects them for an unfused multiply-add only when binary32 denormals are flushed: `SITargetLowering::isFMADLegal` notes that they do not support denormals. Both go through a helper that flushes each operand, the product and the sum to signed zero and keeps the two roundings with `precise`; `v_mac_f32` accumulates into its destination. The draw checks 4097 × 4097 − 16785408 for both instructions, which is 0 unfused and 1.0 fused, and 2^-127 × 2^126 + 0.5, which is 0.5 with the denormal input flushed and 1.0 without. References that expect the fused or the denormal-preserving result must report a mismatch. At commit d311159 the draw matched on an RTX 4070 Ti (driver 32.0.16.1088) and on WARP 10.0.26100.9818, and all three wrong references were detected. Whether Scarlett also flushes a denormal product or sum, as the helper does, is not measured.

`v_frexp_mant_f32` (in e32 and e64 encodings) and `v_ldexp_f32` lower through explicit IEEE-754 binary32 bit helpers (`frexp_mant32` and `ldexp32`) so subnormals, signed zero, infinity, NaN, and large exponent shifts stay independent of desktop driver flush-to-zero modes. `v_ldexp_f32` accepts float source modifiers on `src0` and `clamp`, while decoding `src1` as a signed 32-bit integer and rejecting float modifiers on the exponent. Binary and draw fixtures check normal values, signed zero, source negation and absolute value, runtime scalar exponents, negative exponents, and output clamping; replacing `v_frexp_mant_f32` with `v_mov_b32` or inverting a `v_ldexp_f32` exponent sign must report a pixel mismatch.

`v_frexp_exp_i32_f32` in e32 and e64 encodings returns a signed 32-bit exponent. The binary32 bit helper handles normal and subnormal inputs and returns zero for signed zeros, infinities and NaNs. Source negation and absolute value leave the magnitude unchanged, so the lowerer can omit float sign operations that could flush subnormals. The lowerer keeps the result as integer bits in the destination register. The synthetic draw compares those bits before pixel export for 12 input words, including the smallest and largest subnormals. A reference with an exponent one too large for the smallest subnormal must report a pixel mismatch. Run the dedicated cases with `ctest --preset dev --no-tests=error -R '^pixel_aot_frexp_exp_'`.

Plain `v_mul_lo_u32` multiplies two full 32-bit operands and keeps the low 32 bits. The `pixel_aot_e64_{warp,hardware}` draw reads back the integer word for `0xffffffff * 0xffffffff` and `0x12345678 * 0x9abcdef0`, whose low words are `0x00000001` and `0x242d2080`. Replacing multiply with addition must fail the same readback. The LLVM 15 binary fixture verifies the opcode through the normal decoding path.

Quad-local `DPP16` (`quad_perm` with `row_mask:0xf bank_mask:0xf`) and `DPP8` (`dpp8` repeating the same 2×2 quad permutation on lanes 0–3 and 4–7) lower across 23 32-bit integer, 24-bit multiply, bitwise, shift and bit-scan `VOP1`/`VOP2` forms (`add_nc_u32`, `sub_nc_u32`, `subrev_nc_u32`, `min`/`max` `i32`/`u32`, `mul`/`mul_hi` `i24`/`u24`, `and`/`or`/`xor`/`xnor`/`not_b32`, `lshlrev`/`lshrrev_b32`, `ashrrev_i32`, `bfrev_b32`, `ffbl_b32` and `ffbh_u32`/`ffbh_i32`) via SM 6.0 quad intrinsics, emitting 12-bit-split `dpp_mul_hi_{u,i}24` and MSB-indexed `dpp_ffbh_{u,i}32` helpers only when used. The `dpp_int32` fixture derives integer quad lane ID `{0, 1, 2, 3}` from `uv.x`/`uv.y`, tests all 23 forms across three constant sets (including negative/extreme `i24` products, zero/all-ones bit scans and shift counts `>= 32`), exports all four bytes of the 32-bit accumulator as exact `[0, 255]` integers, and checks five mutations (`AcrossX` vs `AcrossY`, cyclic `dpp8` rotation, `ashrrev` vs `lshrrev`, signed vs unsigned `mul_hi` and `ffbh`) that must each report a mismatch. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

Quad-local `DPP16` (`quad_perm` with `row_mask:0xf bank_mask:0xf`) and `DPP8` (`dpp8` where lanes 0–3 and 4–7 apply the same 2×2 quad permutation) lower across the 20 supported `VOP1`/`VOP2` pixel forms (`v_mov_b32_dpp`, `v_cvt_f32_f16_dpp`, `v_cndmask_b32_dpp`, the 9 unary `f32` ops and 8 binary/accumulate `f32` ops) by swizzling `src0` as `uint` bits into an unconditional local variable via SM 6.0 `QuadReadAcrossX`, `QuadReadAcrossY`, `QuadReadAcrossDiagonal` or uniform `QuadReadLaneAt` (selected by `WaveGetLaneIndex() & 3u`), so subnormal/f16 bit patterns stay intact, quad intrinsics never execute inside conditional expressions (`v_mul_legacy_f32_dpp`/`v_cndmask_b32_dpp`), and results work across wave sizes 4 through 64; cross-quad `DPP8`, `row_*`/`wave_*` controls, partial masks, non-VGPR operands and source modifiers on `DPP8`/move/select are rejected. The `dpp` fixture derives the per-quad lane ID `{0, 1, 2, 3}` from interpolated `uv.x`/`uv.y` (`v8`/`v9`) and exercises all 20 forms across two constant sets, with eleven mutations (`AcrossX` vs `AcrossY`, `AcrossY` vs `AcrossX`, cyclic `dpp8` rotation, lane broadcast, `DPP16` negation of each source, `floor` vs `ceil`, lane 3 of a generic `quad_perm`, `abs` on `v_max_f32_dpp`, `abs` on a negative half in `v_cvt_f32_f16_dpp`, and an integer instead of a fractional `v_exp_f32_dpp` input) that must each report a mismatch; helper-lane behavior at primitive edges and cross-quad DPP remain unverified. Like `curve`, the `dpp` fixture records its binary32 denormal mode as `any` because NVIDIA driver 32.0.16.2101 (RTX 5080) refuses PSO creation under `-denorm preserve` (`E_OUTOFMEMORY`) while WARP passes; its expected words contain no binary32 subnormals and the fixture asserts `flush_ftz(word) == word` for every expected word. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

The `FLAT` scratch memory load and store instructions (`scratch_load_dword`, `scratch_load_dwordx2`, `scratch_load_dwordx3`, `scratch_load_dwordx4`, `scratch_store_dword`, `scratch_store_dwordx2`, `scratch_store_dwordx3`, `scratch_store_dwordx4`) lower to private per-thread local storage arrays in HLSL (`uint scratch[256] = (uint[256])0;`). The profile accepts a signed, dword-aligned 12-bit immediate and an optional statically known SGPR base. The VGPR address must be off. The full access must be aligned and fit the 1 KiB frame. Every loaded word must have an earlier defining store. Load destinations stay pending until a full VM wait. Non-entry masks, invalid register spans and unsupported modifiers are rejected. The `draw-flat-scratch` fixtures test all 8 forms across five constant sets covering bit patterns, signed values, extreme words and zero-spills with bit-exact integer exports, verifying negative detection via store offset mutations and register swaps. Reproduce with `ctest --preset dev --no-tests=error -R "^pixel_aot_flat_scratch_" -V`, or `python -B tests/pixel_aot_check.py draw-flat-scratch <dxc> <offscreen_check> <hardware|warp>`. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

The plain `VOP1` spellings of `v_bfrev_b32`, `v_ffbl_b32`, `v_ffbh_u32` and `v_ffbh_i32` (`_e32` and `_e64`) lower with the semantics of the merged DPP path above: `reversebits`, `firstbitlow` (0xffffffff for zero) and the `dpp_ffbh_{u,i}32` helpers (emitted once, only when used). The fixtures also use `v_xnor_b32`, which lowers through the integer table as described above. The sources are `s44`..`s47`, so no input passes through a float VGPR; VGPR sources lower as well. Source modifiers, `clamp`, wrong operand counts and `v256` are rejected with a `ValueError`; SDWA forms, 64-bit and 16-bit variants stay out of scope. The `draw-plain-bitscan` fixtures compare two shaders (plain spellings, and mixed `_e32`/`_e64` with a literal) over nine cases that rotate the words `0`, `0xffffffff`, `0x80000000`, `1`, `0x7fffffff`, `0xffff`, `0xaaaaaaaa`, `0x12345678` and `0xfffffffe` through every source register against a Python bit-loop reference, and four mutations (`ffbh_i32` to `ffbh_u32`, `ffbl_b32` to `ffbh_u32`, `xnor_b32` to `xor_b32`, `bfrev_b32` to `not_b32`) must each report a mismatch. Reproduce with `ctest --preset dev --no-tests=error -R "^pixel_aot_plain_bitscan_" -V`, or `python -B tests/pixel_aot_check.py draw-plain-bitscan <dxc> <offscreen_check> <hardware|warp>`; `tests/form_coverage_check.py` asserts these forms, and the `v_xnor_b32` forms the fixtures use, count as covered. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

24-bit vector integer multiplies (`v_mul_i32_i24`, `v_mul_u32_u24`, `v_mul_hi_i32_i24` and `v_mul_hi_u32_u24` in e32 and e64 encodings) lower through lazy 32-bit HLSL helpers (`mul24_u32`, `mul24_i32`, `mul24_hi_u32` and `mul24_hi_i32`) that mask or sign-extend bits `[23:0]` and compute the 48-bit high product shifted right by 32 without requiring 64-bit integer shader ops. Float source modifiers, `clamp`, SDWA forms, out-of-range registers and writes to pending sample destinations are rejected. The binary and batched draw fixture covers all 8 forms across five constant sets with nonzero upper bytes (`bits [31:24]`), exact `-2^23 * 512 = -2^32` low-zero negative carry, `-2^23 * -2^23`, `0xffffff * 0xffffff`, zero with upper-byte garbage and mixed bit patterns, folding all eight 32-bit results into a 32-bit checksum unpacked through `v_cvt_f32_ubyte0`..`3`, and checks both wrong-mode references (unmasked 32-bit operands, signed/unsigned swaps and missing low-zero carry) and a mutated shader replacing `v_mul_hi_i32_i24_e32` with `v_mul_hi_u32_u24_e32`.

Packed binary16 `VOP3P` arithmetic (`v_pk_add_f16`, `v_pk_mul_f16`, `v_pk_min_f16`, `v_pk_max_f16` and `v_pk_fma_f16`) widens each selected 16-bit lane with `op_sel`, `op_sel_hi`, `neg_lo` and `neg_hi`, applies IEEE `minNum`/`maxNum` or arithmetic in binary32, optionally clamps to `[0, 1]`, and repacks both lanes with an explicit round-to-nearest-even helper (`pack16_rne`) so results do not depend on `f32tof16` rounding differences across desktop adapters. Inline binary16 float constants occupy bits `[15:0]` with bits `[31:16]` zeroed, matching `gfx1013` hardware, while 32-bit hex literals and integer inline constants supply both halves. The `vop3p` fixture draws three constant sets covering lane selection, negation, inline high-half zero extension, `minNum` against a quiet NaN literal (`0x40007e00`), `[0, 1]` clamp and exact binary16 halfway ties, with five mutations (`neg_lo`, `neg_hi`, `min` vs `max`, inline high-half zero extension and `clamp`) that must each report a mismatch; packed integer `VOP3P`, `v_fma_mix_*` and dot-product forms stay rejected. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

The `VOP2` packed binary16 fused multiply-accumulate form (`v_pk_fmac_f16` and `v_pk_fmac_f16_e32`, AMD RDNA 2 ISA §12.11 opcode `0x3c`, LLVM 15 `VOP2Instructions.td`: `V_PK_FMAC_F16`) uses both halves of `vdst` as the implicit packed accumulator (`vdst.f16[0] = fma(src0.f16[0], vsrc1.f16[0], vdst.f16[0])`, `vdst.f16[1] = fma(src0.f16[1], vsrc1.f16[1], vdst.f16[1])`) under the finite preserve/RNE profile (`lower(..., half_denorm="preserve")`). Rather than rounding through binary32, each lane lowers through a pure 32-bit unsigned integer helper (`fma16_rne`) that forms the exact 22-bit product of the two 11-bit significands, aligns the smaller-magnitude term against the larger at bit 30 with a sticky bit at bit 0 (exact with zero bits dropped whenever exponent difference `<= 8` so arbitrary cancellation is exact, and strictly below the binary16 guard bit whenever exponent difference `>= 9`), and rounds once to binary16 round-to-nearest-even while preserving binary16 subnormals and remaining unaffected by binary32 `-denorm ftz`. Both halves of `src0`, `vsrc1` and `vdst` must be statically proven finite (VGPR from a known constant or earlier `v_pk_fmac_f16`, tracked SGPR integer, 32-bit hex literal, `0`, or inline `f16` constant with `gfx1013` zero-extended upper half), and the translator checks exact rational (`fractions.Fraction`) lane results so overflowing results (`|exact| >= 65520`) are refused and chained `v_pk_fmac_f16` results stay proven. Unproven runtime operands, NaN/infinity halves, overflowing results, unknown or unsupported `half_denorm` entry states, round-toward-zero mode, an uninitialized `vdst` (or scalar `half16` value), non-VGPR `vsrc1`, non-zero integer inline constants, any modifier (`clamp`, `op_sel`, `op_sel_hi`, `neg_lo`, `neg_hi`, `abs`, `-`, `mul`), and `_e64`/DPP/SDWA spellings are refused with `ValueError`. The `draw-pk-fmac` fixture (`pixel_aot_pk_fmac_warp` and `pixel_aot_pk_fmac_hardware`) draws two baked programs (`precision` and `alias_chain`) through lossless `R32G32B32A32_UINT` readback against an independent `fractions.Fraction` reference asserting `half_denorm="preserve"` under both `-denorm preserve` and `-denorm ftz`. It covers the exact fused binary16 discriminator in both halves (`0x3e00 * 0x3956 + 0x0001 = 16785409/16777216`, which rounds once to `0x3c01` directly vs `0x3c00` through intermediate binary32), dedicated denormal-preserve lanes (`0x0001 * 0x3c00 + 0x0001 = 0x0002`, `0x0400 * 0x3800 + 0x0000 = 0x0200`, and `0x0001 * 0x4000 + 0x03ff = 0x0401`, each differing from flush), exact binary16 halfway ties, VGPR/SGPR/literal/inline-`f16` `src0`, chained accumulation, and destination aliasing (`dst == src1`, `dst == src0`, `dst == src0 == src1`). Seven HLSL mutations must each report `pixel_mismatch`: an intermediate-binary32 rounding mutant failing specifically at the discriminator lane (`0xbc003c01` vs `0xbc003c00` on channel 0 and `0x3c010401` vs `0x3c000401` on channel 3), input-denormal flush and output-denormal flush failing on the dedicated denormal lane (channel 1), dropped accumulator, swapped halves, truncated rounding, and ties-away-from-zero. Both programs are included in `tests/pixel_aot_check.py binary` (Refs #990). Not measured: native console denormal mode, NaN/infinity/overflow handling, native signed-zero rules, and runtime operands.

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_pk_fmac_' -V
```

Mixed-precision `VOP3P` FMA (`v_fma_mix_f32`, `v_fma_mixlo_f16` and `v_fma_mixhi_f16`) interprets each source as binary32 (`op_sel == 0`) or binary16 (`op_sel == 1`, low or high half via `op_sel_hi`, widened through `widen16`), applies `abs` and `neg` after widening, evaluates the multiply-add with optional `[0, 1]` clamp, and for `v_fma_mixlo_f16`/`v_fma_mixhi_f16` packs the destination half with `pack16_rne` while preserving the untouched 16 bits of `vdst` (rejecting an uninitialized destination). The `fma_mix` fixture draws three constant sets covering mixed f16/f32 inputs, `|.|`/`-`, `op_sel_hi`, preserved high/low destination halves, `[0, 1]` clamp, and binary16 halfway RNE ties, with five mutations (`abs`, `neg`, `op_sel_hi`, `mixhi` preservation and `clamp`) that must each report a mismatch. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

`VOP3P` dot products (`v_dot2_f32_f16`, `v_dot2_i32_i16`, `v_dot2_u32_u16`, `v_dot4_i32_i8`, `v_dot4_u32_u8`, `v_dot8_i32_i4` and `v_dot8_u32_u4`) compute the packed lane products plus the 32-bit accumulator. `v_dot2_f32_f16` widens its `f16` halves through `widen16` with `op_sel`, `op_sel_hi`, `neg_lo`, `neg_hi` and optional `[0, 1]` `clamp`, while `v_dot2_i32_i16` and the three unsigned integer forms accumulate in emulated 64-bit unsigned pairs (`uint2`) so wrapping and saturating `clamp` (`INT32_MIN`/`INT32_MAX`/`UINT32_MAX`) stay exact on SM 6.0 without requiring 64-bit integer shader support. `v_dot4_i32_i8` and `v_dot8_i32_i4` add their product sums, which lie in `[-65024, 65536]` and `[-448, 512]`, to the accumulator in 32 bits. A signed overflow is then only possible for an accumulator of at least `2^30` or below `-2^30`, where it flips the sign of the wrapped result, so saturating `clamp` is decided from the accumulator and that result. On an Intel UHD Graphics (drivers 32.0.101.7026 and 32.0.101.7092) the 64-bit pair form gave wrong `v_dot4_i32_i8` results inside the full `dot` program while isolated probes were exact; with the 32-bit form `pixel_aot_hardware` passes there, as on an NVIDIA GeForce RTX 3050 Laptop GPU and on WARP ([#736](https://github.com/R0Y00/Xbox2Windows/issues/736)). The `dot` fixture assembles on `gfx1030` (since LLVM's `gfx1013` subtarget omits dot-product instructions) and draws six constant sets covering mixed signs, `INT32_MAX`/`INT32_MIN`/`UINT32_MAX` saturation and wrapping, and signed results one step inside `INT32_MAX` and `INT32_MIN` without saturation, with five mutations (`neg_lo`, signed vs unsigned `dot2`/`dot8`, and signed/unsigned `clamp`) that must each report a mismatch. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

Packed 16-bit integer `VOP3P` arithmetic and shifts (`v_pk_add_i16`, `v_pk_add_u16`, `v_pk_sub_i16`, `v_pk_sub_u16`, `v_pk_min_i16`, `v_pk_min_u16`, `v_pk_max_i16`, `v_pk_max_u16`, `v_pk_mul_lo_u16`, `v_pk_mad_i16`, `v_pk_mad_u16`, `v_pk_lshlrev_b16`, `v_pk_lshrrev_b16` and `v_pk_ashrrev_i16`) select each half-word lane through `op_sel` and `op_sel_hi`, mask shift counts to 4 bits (`& 15`), and saturate `clamp` on the six clamping add/sub/mad forms to `[-32768, 32767]` or `[0, 65535]` (rejecting `clamp` on min/max/mul/shift and rejecting `neg_lo`/`neg_hi` or float inline constants). The `pk_int16` fixture draws three constant sets covering all 14 forms, shift counts `>= 16`, inline `-1`, signed/unsigned clamping boundaries and cross-lane swaps, exporting all four bytes of the packed accumulator as exact `[0, 255]` integers with five mutations (`ashrrev` sign fill, signed vs unsigned min, `sub_u16` clamp, `op_sel` swap and `mad_i16` clamp) that must each report a mismatch. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

`s_buffer_load_dword` reads one word from the tracked four-SGPR constant descriptor. Supported offsets are dword-aligned immediates within the 256-byte desktop binding, with `null` denoting offset zero. The lowerer requires the descriptor to be ready and tracks the destination until `s_waitcnt lgkmcnt(0)`. The synthetic binary and draw checks cover offsets 0, 8 and 252. Two sets of constant words matched all four channels of 4,096 pixels per draw on WARP and RTX 5080. Reading the neighboring word changes the first pixel on both adapters. [AMD RDNA 2 scalar memory rules](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

The gfx10 SOPK wait forms `s_waitcnt_vmcnt null, K`, `s_waitcnt_lgkmcnt null, K` and `s_waitcnt_vscnt null, K` are accepted with `null` as the only SGPR operand, because `null` reads as 0 and the count is then the immediate `K` (decimal or hex, 0 to 63, the gfx10 six-bit field). `s_waitcnt_vmcnt null, K` and `s_waitcnt_lgkmcnt null, K` act exactly like the `vmcnt(K)` and `lgkmcnt(K)` tokens of `s_waitcnt`. `s_waitcnt_vscnt null, K` is a no-op, because the translator lowers no vector memory stores and nothing is outstanding on the store counter. An SGPR operand (its runtime value would add to or replace the count and is not modelled), a count above 63 and a missing operand are refused with a `ValueError`. Like the other wait forms they are accepted only before the color export, and `tools/scan_unsupported_shaders.py` marks the `null` forms with a count of 0 to 63 as supported. The controls compare each form with its `s_waitcnt` token equivalent, including a partial `vmcnt` with two outstanding samples, and `tests/pixel_aot_check.py binary` round-trips one sampled fixture that contains all three forms through LLVM 15 (gfx1013) and the disassembler. No GPU draw covers them; the identical-HLSL controls are the evidence. The semantics follow the LLVM 15 gfx1013 encodings (`s_waitcnt_vmcnt null, 0x0` is `00 00 7d bc`, `s_waitcnt_lgkmcnt null, 0x0` is `00 00 7d bd`, `s_waitcnt_vscnt null, 0x0` is `00 00 fd bb`); they were not measured on hardware.

The SOPK form `s_version K` is accepted as a no-op marker, like `s_nop`: it has no destination and does not write SCC (LLVM 15 `SOPInstructions.td`: `S_VERSION` has `has_sdst = 0` and sits outside the `Defs = [SCC]` block), so the lowerer neither emits code nor changes SCC, pending-sample or descriptor-load state. `K` is a 16-bit immediate, decimal or hex as LLVM prints it (`s_version 0x1004` is `04 10 80 b0` on gfx1013). A value above 0xffff, a negative value, a register operand and extra operands are refused with a `ValueError`. Like `s_nop`, it is still accepted after the color export, where any other instruction is refused. The controls insert the marker at every position before the export of a program with SCC, a pending sample and descriptor loads, and require the same HLSL as without it (up to temporary renumbering); `tests/pixel_aot_check.py binary` round-trips a fixture containing it through LLVM 15, and `tools/scan_unsupported_shaders.py` and `tools/disasm_rdna2.py` decode and count it as supported. No GPU draw covers it, because nothing is observable; the identical-HLSL controls are the evidence, and it was not measured on hardware.

`s_buffer_load_dwordx2` reads two consecutive words under the same descriptor, offset and wait rules. The destination is an even-aligned SGPR pair, the form LLVM prints, and both words must lie inside the 256-byte binding, so the last accepted offset is 248. The offset needs dword alignment only: the synthetic binary and draw checks read the pairs at offsets 12, 20 and 248, and the first pair spans two `float4` rows of the desktop buffer. Each of the six words feeds its own exact binary32 step, and two sets of constant words matched all four channels of 4,096 pixels per draw on WARP 10.0.19041, WARP 1.0.22 and RTX 3060 (driver 32.0.15.9636). Changing the second word of any pair in the bound buffer, or reading the middle pair one word early, changes the first pixel on each adapter. Register offsets and the eight-word and sixteen-word buffer loads remain rejected.

A pixel program may read more than one constant buffer. `--extra-constant SGPR OFFSET`, the `extra_constants` argument of `lower`, names one further 16-byte descriptor by the entry register that holds the low word of its table address and by its byte offset in that table. Repeated options map in order to `b1`, `b2` and onward, each a 256-byte `float4 paramsN[16]` buffer under the same read rules as `b0`. The register may be the texture table or another entry register. A table register is accepted only as the low address word of an `s_load_dwordx4` whose high word is a tracked zero. A descriptor the contract does not name, a nonzero or unknown high word, arithmetic on a table register, a texture descriptor load from a constant table and a four-register span that mixes words of two descriptors are rejected, and an entry register outside the contract stays unbound. These are explicit desktop bindings supplied by the caller. The translator does not infer which descriptors a title uses as constant buffers, how large they are, or which desktop register a native slot had in the original source.

The `three-buffers` fixture loads the `b0` descriptor and a second descriptor from the texture table and a third from the table at entry s9, reads word 2 of each buffer and exports one buffer per channel. Every other word of the three buffers holds a sentinel. On Windows 10 build 19045 with the D3D12 debug layer enabled, two constant sets matched all four channels of 4,096 pixels per draw on WARP 10.0.19041.5794 and RTX 3060 (driver 32.0.15.9636). A reference with `b1` and `b2` exchanged, a reference that reads `b0` in place of `b1`, and a translation with the two contract entries exchanged each report a mismatch on both adapters. The pixel-case trailer carries the extra buffers under bit 3 of the flags byte: 512 bytes follow the flags byte and any viewport, 256 for `b1` and then 256 for `b2`, and a shorter file is a case-format error. The renderer's root signature gains root CBVs `b1` and `b2`; they are set only for a case that supplies them.

A descriptor may also sit in a nested table whose address the program loads from a bound table. `--pointer-constant SGPR POINTER_OFFSET DWORDS OFFSET`, the `pointer_constants` argument of `lower`, names the entry register of the parent table as in `extra_constants`, the byte offset and width of the pointer in that table, and the byte offset of a 16-byte descriptor in the table it addresses. Entries map in order to the slots after the `extra_constants` ones, each a 256-byte `paramsN[16]` buffer (`uint4` under the integer export, `float4` otherwise). `s_load_dwordx2` reads a two-dword pointer as a full 64-bit address, and the following `s_load_dwordx4` must use the loaded high word unchanged. `s_load_dword` reads a one-dword pointer, the low address word, and the program must set the high word to a tracked zero, as for an entry table. Both loads use the scalar-load completion model of the other SMEM forms: the pointer SGPRs are outstanding until `s_waitcnt lgkmcnt(0)`, a descriptor fetch or write through them before then is refused, and a pointer register is never a numeric operand or a texture table. Several descriptors may share one pointer. A pointer offset or width the contract does not name, a descriptor offset not named for that pointer, an address that is itself a loaded pointer, an SGPR offset operand, `glc` or `dlc`, an odd destination pair, and a contract whose pointer overlaps another pointer, a constant descriptor or a texture descriptor in the same table are rejected. The contract is supplied by the caller; the translator does not infer how a title lays out nested tables. The pixel-case trailer may now combine the extra-buffer flag with the integer render-target flag.

The `smem-pointers` fixture keeps `b0` at 0x40 of the texture table, loads a 64-bit pointer from 0x50 of that table and a 32-bit pointer from 0x8 of the table at entry s9, then fetches the `b1` descriptor at 0x20 and the `b2` descriptor at 0x10 of the two nested tables. It exports `b1` word 2, `b2` words 5 and 6 and `b0` word 3 through the `R32G32B32A32_UINT` target, and every other word holds a sentinel naming its buffer and index. On Windows 10 build 19045 with the D3D12 debug layer enabled, two word sets (a quiet NaN payload, a binary32 denormal, negative zero, all ones and zero among them) matched all four channels bit for bit on WARP 10.0.19041.5794 and RTX 3060 (driver 32.0.15.9636). A reference with `b1` and `b2` exchanged, a reference that expects the `b0` sentinel, a translation with the two contract entries exchanged and a translation that reads `b1` word 3 in place of word 2 each report a mismatch on both adapters, and the exchanged translation matches the exchanged reference. The CPU controls in `tests/pixel_aot_check.py` cover the refusals listed above, and the fixture is part of the LLVM 15 binary round trip.

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_smem_pointers_warp$' -V
```

Before a single sample, whole-quad addition, FMA and legacy multiplication can compute affine coordinates from paired interpolation and ready uniform constants. Each coordinate used for implicit LOD must have a value defined on helper lanes. An entry-only write results in a value without that proof, and multiplication of two varying operands, absolute value and saturation fall outside the affine subset. The source-built draw checks a partial triangle on an 8×8 target with two solid-color mips. WARP and RTX 5080 select mip 1 at every covered pixel and preserve clear color outside the triangle. Replacing helper coordinates with the quad's top-left coordinates changes the first mismatched pixel at (6, 0) to mip 0 on both adapters.

`v_cmp_class_f32` is true when the class of S0 is in the 10-bit mask S1, with the bits of LLVM's `SIInstrFlags::ClassFlags`: signaling NaN, quiet NaN, -inf, -normal, -denormal, -0, +0, +denormal, +normal and +inf. The lowering classifies the raw binary32 encoding, so desktop denormal handling does not change the class. The mask may be an inline constant, a literal or a register. The draw classifies +inf against the +inf bit, a quiet NaN against both NaN bits, -0 against the +0 bit and the smallest positive denormal against its bit, and selects 1.0 or 0 for each. A reference with any single result flipped must report a mismatch. Signaling NaN inputs are not drawn, and whether Scarlett classifies a denormal input without flushing it first is not measured.
Quad-local `DPP16` (`quad_perm` with `row_mask:0xf` and `bank_mask:0xf`) and quad-local `DPP8` (`dpp8` repeating the same 2×2 quad permutation on lanes 0–3 and 4–7) forms for the remaining extra `f32` (`v_rndne_f32_dpp`, `v_sin_f32_dpp`, `v_cos_f32_dpp`, `v_rcp_iflag_f32_dpp`, `v_frexp_mant_f32_dpp`, `v_frexp_exp_i32_f32_dpp`, `v_mac_f32_dpp`), carry-in/carry-out (`v_add_co_ci_u32_dpp`, `v_sub_co_ci_u32_dpp`, `v_subrev_co_ci_u32_dpp`), and `gfx1030` integer dot-accumulate (`v_dot4c_i32_i8_dpp` and plain VOP2 e32 `v_dot4c_i32_i8_e32`) instructions lower via the shared SM 6.0 quad intrinsics (`QuadReadAcross*`, `QuadReadLaneAt`, `WaveGetLaneIndex() & 3u`) and helpers on raw 32-bit `uint` bits. `v_sin_f32_dpp` and `v_cos_f32_dpp` lower through `sin32`/`cos32` (with `turns()` range reduction matching plain `v_sin_f32`/`v_cos_f32`), and `v_mac_f32_dpp` lowers through `mad32` (unfused, denormal-flushing multiply-add matching plain `v_mac_f32`). Carry-in/carry-out forms read and update the tracked `vcc` per-lane condition; `DPP16` source modifiers (`-`/`|.|`) are allowed only on supported floating-point forms, the plain VOP2 e32 integer dot-accumulate form accepts VGPR/SGPR/literal/inline `src0` with VGPR `vsrc1` and reject source modifiers, `clamp`, and `_e64` spellings, and unproved `fi:`/`bound_ctrl:` modifiers, cross-quad or non-repeated `DPP8`, `row_*`/`wave_*`, partial masks, duplicate modifiers, non-VGPR DPP operands, uninitialized accumulators, WQM, and `vmcnt` hazards are refused with specific `ValueError` diagnostics. The `pixel_aot_dpp_extra_{hardware,warp}` test runs `DPP_EXTRA_BODY` (`gfx1013`, compiled with `mode="any"` + `flush_ftz` assertion matching `curve` and `dpp`), `DPP_DOTC_BODY` (`gfx1030`, covering the DPP and plain VOP2 e32 integer dot-accumulate forms), `DPP_EXTRA_SEMANTICS_BODY` (compiled under `mode="preserve"`, distinguishing large-turns `sin32`/`cos32` at `123457.125` and `123457.375`, unfused `mad32` at `4097 * 4097 - 16785408`, denormal-flushing `mad32` at `2^-127 * 2^126 + 0.5`, and all four lanes of `dpp8:[1,2,3,0,5,6,7,4]` under injective base-4 weights `(1, 4, 16, 64)`), `DPP_EXTRA_SUBNORMAL_BODY` (lossless integer readback of `v_frexp_mant_f32_dpp` and `v_frexp_exp_i32_f32_dpp` on binary32 subnormals `0x00000001`, `0x007fffff`, `0x80000001`), and `DPP_DOTC_WRAP_BODY` (lossless integer readback of 32-bit wrap-around on `v_dot4c_i32_i8_{dpp,e32}` at `0x7ffffff0 + 32 = 0x80000010` and `0x80000010 - 32 = 0x7ffffff0`), each with negative mutations that must report `pixel_mismatch`. The floating-point `v_dot2c_f32_f16` forms are refused pending independent evidence of their accumulation rounding. Reproduce with `ctest --preset dev --no-tests=error -R "^pixel_aot_dpp_extra_" -V`, or `python -B tests/pixel_aot_check.py draw-dpp-extra <dxc> <offscreen_check> <hardware|warp>`.

```powershell
ctest --preset dev -R '^pixel_aot_' -V
```

For a compatible local record, substitute the archive, exact record name, two register numbers and four descriptor words below. Run the translator from the repository root in WSL, then compile and draw from a Windows development shell. The JSON printed by the translator identifies each input and output by SHA-256 for local reproduction. The renderer's `rgba` expectation checks a copy and `bgra` checks a red/blue swap.

```sh
python3 -B tools/pixel_aot.py "$archive" "$record" --llvm libLLVM-15.so.1 \
    --texture-sgpr "$texture_reg" --interp-sgpr "$interp_reg" \
    --sampler-words "$word0" "$word1" "$word2" "$word3" --output out/pixel-aot/translated.hlsl
```

When several records share the exact name, add `--container-sha256` with the SHA-256 of the full decoded DXBC container. Run the [shader extractor](#optional-compressed-record-inventory) with `--length-prefixed-names --format dxbc`, then hash the desired `.dxbc` file using `sha256sum` or `Get-FileHash -Algorithm SHA256`. Exactly one record must match both the name and the digest. Two containers can carry identical native instructions and different metadata, so their native program hashes can be identical.

```powershell
.\tools\dev.ps1
dxc -T ps_6_2 -denorm preserve -E main -Fo out/pixel-aot/translated.cso out/pixel-aot/translated.hlsl
.\out\build\dev\offscreen_check.exe hardware --check-pixel out/pixel-aot/translated.cso rgba
.\out\build\dev\offscreen_check.exe warp --check-pixel out/pixel-aot/translated.cso rgba
```

## Package integrity

For a local build with a multi-file v1 torrent and `layout*.xml`, run the offline verification with Python 3.9 or newer. Substitute your local directory name.

```powershell
python tools/verify_build.py "game-files/<build-directory>"
```

The script verifies SHA-1 pieces across file boundaries in torrent order and compares layout destinations separately with disk and torrent entries. The report is `out/integrity/<build-directory>.json`. Exit code 0 means the payload passed piece verification. `layout.absent_from_disk` lists missing layout entries separately.

## PE dependency baseline

The scanner uses x64 Windows, Python 3.11 or newer and pefile. Set up an isolated environment once.

```powershell
python -m venv out/pe-env
.\out\pe-env\Scripts\python.exe -m pip install pefile==2024.8.26
```

Capture a DLL-only baseline. When an EXE becomes available, write a separate report and compare it with the saved baseline.

```powershell
$pePython = '.\out\pe-env\Scripts\python.exe'
$buildDir = 'game-files/<build-directory>'
& $pePython tools/pe_inventory.py $buildDir --output out/dependencies/pre-exe.json
# Run the next command after adding the main executable.
& $pePython tools/pe_inventory.py $buildDir --baseline out/dependencies/pre-exe.json --output out/dependencies/with-exe.json
```

`summary` gives counts. `unresolved_edges` lists requested exports missing from both the package and the host. Each `imports` entry identifies the caller, import mechanism, provider and symbol. `#N` denotes an ordinal. The scanner reads import descriptors and lookup tables itself. pefile skips names outside letters, digits and `._?@$()<>` and drops a descriptor left empty, so those entries would otherwise vanish from the graph. Ordinal imports stay `#N` even where pefile's built-in table supplies a name. Each provider is listed under the file name the loader looks for: a descriptor name without an extension gets `.dll` appended, while a trailing dot suppresses the append and is dropped, as with `LoadLibrary`. Without this, an import of `intake_forward` would miss the package's `intake_forward.dll` and report `provider_not_found`; `bundled_export_forwarders` rewrites the caller's descriptor to both forms. [LoadLibraryW extension rule](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryw) · [Wine DLL name lookup](https://github.com/wine-mirror/wine/blob/master/dlls/ntdll/loader.c) `images` includes architecture, SHA-256, export names, ordinals and forwarders. `diff` distinguishes new caller relationships from dependencies seen for the first time in the build.

The scanner reads System32 export tables and applies the host API-set v6 map at each hop, using the forwarding DLL as the importer at the next hop. Each import's `desktop_resolution.chain` records the requested module, selected provider, symbol and any advertised forwarder. Its final status identifies a terminal export, missing provider or export, malformed forwarder, or cycle. `desktop_export` retains the first-hop result for older report consumers. The comparison covers System32 DLLs on the measured machine. Package selection at runtime, parameter layouts and behavior require separate evidence. Export names are read as stored, because pefile stops at the first name outside its character set and would list every later named export, for the package and System32 alike, by ordinal only. An export directory, address, name or name-ordinal table, name or forwarder that does not lie in a section's file data, or a name or forwarder with no terminating NUL there, stops the scan with an error naming its RVA. pefile instead drops the name, forwarder or whole table with a warning. `static_exe_intake` covers each case on a patched copy of `intake_provider.dll`. [PE export address table](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#export-address-table) · [Wine API-set lookup](https://github.com/wine-mirror/wine/blob/master/dlls/ntdll/loader.c)

Package DLLs can also forward exports, to another package DLL, to themselves, by ordinal or to System32. An edge whose provider is in the package gets a `package_resolution` chain. It uses the same statuses, adds `ambiguous_package_basename`, and marks each hop's `package` origin. Plain module names select a uniquely named package image before System32. API-set contracts always select System32 hosts. So do the names in the host's `\KnownDlls` object directory, plus `ntdll.dll`: the loader maps a known DLL from System32 before it searches the application directory, at the first hop and at every forwarder hop. The report lists these names under `known_dlls` and marks such a hop `known_dll`. `dependency_paths` does not treat a package file with a known DLL's name as loaded and reports the edge under its System32 status. An edge counts as resolved through the package only if its chain reaches a terminal export. `dependency_paths` adds each packaged forwarder target after its provider with the reason `forwarder` and reports a broken chain under the chain's terminal status. Earlier tools treated any matching export name as resolved. They omitted the target module and accepted forwarders to missing exports, missing modules or cycles. The `bundled_export_forwarders` test builds `intake_forward.dll` with seven placeholder exports, because linkers differ in how a .def file declares forwarders. It moves the export table of a temporary copy into a new section and turns the seven exports into forwarders: by name, by ordinal, a two-hop chain through itself, a missing target export, a self-cycle, an absent module and a by-name forwarder whose target module is written in uppercase. The uppercase target must resolve to the same provider hop as the lowercase ones. It then checks the inventory and path report for a caller that imports all seven. A second scan adds a copy of `intake_provider.dll` named `kernel32.dll` to the package. The caller's KERNEL32 imports must still follow the System32 chain, and the copy must stay unlinked in the path report. `conditional_import_paths` checks the same precedence with synthetic export tables, including a packaged forwarder whose next hop is a known DLL. [Dynamic-link library search order](https://learn.microsoft.com/en-us/windows/win32/dlls/dynamic-link-library-search-order)

On Windows build 26200, the pre-executable inventory covered 16 package DLLs and 1,337 import edges. Of these, 171 edges (78 distinct requested module/symbol pairs) went through a single export forwarder. All 171 reached a terminal export. In total 1,329 edges resolved on the desktop. The eight remaining edges requested six distinct interfaces across `xmem.dll` and `d3d12_xs.dll`. The 16 package DLLs had zero forwarded exports of their own. The historical baseline was preserved for comparison.

## Heap and process memory

`src/era_heap.cpp` is a desktop handler. It splits the attribute word using SlimEra's Xbox One ERA layout: object type in bits 0–13, page size in bits 14–15, allocator ID in bits 16–23, log2 alignment in bits 24–28, memory type in bits 29–32 and reserved bits 33–63. Object type and allocator ID are carried as tags and do not affect placement, as in SlimEra's default heap.

The desktop handling is:

- **Heap (memory type 0):** served from the CRT heap at the requested alignment, minimum 16 bytes, and zero-filled. The page size does not affect these requests. SlimEra passes every heap request to `_aligned_malloc`, which commits the alignment padding as heap memory, so a 257-byte request with exponent 31 would commit more than 2 GiB. The desktop handler instead gives alignments above the page size their own reservation and commits only the pages that hold the payload; `VirtualAlloc` returns them zeroed.
- **Graphics (memory types 1–6):** committed as a separate `VirtualAlloc` region. `release_with_attributes` frees it with `VirtualFree`. The region starts on the desktop allocation granularity, which covers 4 KB and 64 KB pages. This is ordinary CPU-visible memory; no GPU mapping or caching behavior is modeled.
- **Rejected:** physical memory types 7–9, undefined types 10–15, the undefined page-size value 3, reserved bits, and graphics requests whose page size or alignment exceeds the allocation granularity all return a null pointer.

`release` frees heap allocations. It frees a page-backed block with `VirtualFree` when `VirtualQuery` reports it as the base of its own allocation; a CRT block never is, because `_aligned_malloc` stores its base pointer below the returned address. `release_with_attributes` takes the allocation's attribute word, as `XMemFree` callers do.

What is measured is the desktop behavior above. Under the ERA layout, the two recorded words give alignments that match their callers' own arithmetic: 32 bytes for Bink and 256 bytes for AMD. The following remain unconfirmed on Scarlett:

- that Scarlett uses the ERA layout at all;
- what allocator ID `0xe5` means;
- the GPU visibility and caching of memory type 5 and the other graphics types;
- the console's error conventions.

[SlimEra attributes](https://github.com/XWine1/SlimEra/blob/main/era.h#L105-L137) · [SlimEra default heap](https://github.com/XWine1/SlimEra/blob/main/kernelx.cpp#L600-L657) · [Issue #126](https://github.com/R0Y00/Xbox2Windows/issues/126)

| CTest case | Inputs and checks |
|---|---|
| `era_heap_allocation` | Allocate 257 bytes using six alignment exponents. Check alignment of at least 16 bytes and the requested alignment, initial zeroing, writes and release. Debug builds also compare CRT heap snapshots. |
| `era_heap_failure` | Each reserved bit, combined with a heap alignment and with the AMD graphics word, returns a null pointer. So do memory types 7–15, page-size value 3, graphics memory with 4 MB pages or a 128 KiB alignment, zero size and excessive size. |
| `era_heap_reuse` | For three alignment exponents and three sizes, fill a block with `0xa5` and release it. Then hold up to 32 new blocks of the same shape until the released address returns. Every new block must read as zero. The output reports `recycled_address`, the number of the nine cases in which the CRT returned the dirty address. glibc returned all nine in a Linux build with a `posix_memalign` stand-in. Windows 11 build 26200.9974 with MSVC 14.44.35207 returned the dirty address in all nine cases in both Debug and Release. Low-fragmentation heap randomization can lower it. |
| `era_heap_observed_words` | Desktop handling of the two recorded words. Under the ERA layout, the Bink word `0x05e50000` splits into heap memory, 4 KB pages, allocator field `0xe5` and 32-byte alignment. The AMD word `0xa8004000` splits into memory type 5, 64 KB pages, allocator field 0 and 256-byte alignment. Bink-style 97- and 193-byte blocks are 32-byte aligned and zeroed, and each wrapper recovers its base. AMD-rounded requests of 256 to 65,792 bytes start a committed read-write region on a 64 KiB boundary and are zeroed and writable. After release, `VirtualQuery` reports the region as free. Nonzero object-type and allocator tags leave heap placement unchanged. |
| `era_heap_large_alignment` | Exponents 13, 16, 17, 20 and 31 with sizes of 1 and 257 bytes, one page, one page plus one byte and three pages minus one byte. Each block must be aligned and zeroed. `VirtualQuery` must report the block as the base of its own private read/write allocation, with a committed region equal to the payload rounded up to whole pages. After release the address must be `MEM_FREE`. The output reports the 25 cases, the committed bytes and the summed alignments. With the previous `_aligned_malloc` path, the first case already fails because the block lies inside a CRT heap allocation. |
| `era_heap_graphics_types` | Each graphics memory type 1–6 with 4 KB and 64 KB pages and alignment exponents 0, 4, 8 and 16, at 1 byte and one allocation granularity plus one byte. Exponent 16 is the 64 KiB granularity boundary itself. Each block must start on the granularity, be zeroed and writable, and be reported by `VirtualQuery` as the base of its own committed private read/write allocation. After release with the same attributes the address must be `MEM_FREE`. For every type, 4 MB pages, exponent 17 and page-size value 3 return a null pointer. The output reports 96 served and 18 rejected cases at granularity 65,536 on Windows 11 build 26200. Changing the granularity comparison from `>` to `>=` fails exponent 16 with 4 KB pages, and narrowing the graphics range to types 1–5 fails type 6; `era_heap_observed_words` catches only the first change. These are desktop policy checks; the console semantics of the six graphics types remain unconfirmed. |
| `era_heap_concurrency` | Four threads run 100 rounds each. They hold simultaneous allocations, write distinct values, compare every byte and release the memory. |
| `era_heap_caller_layout` | Owned desktop storage covers all 32 base residues at eight payload sizes. Each Bink-style wrapper recovers its original base after a payload write and preserves the surrounding guard bytes. Ten AMD-style requests check 256-byte size rounding and the unused tail. These cases establish caller-side arithmetic. The Bink-style storage uses the recorded Bink word. |

`desktop_memory_query` supplies Windows observations for `XMemVirtualQuery` and `XMemGetWorkingSetStatistics`. The test reserves memory, commits and writes 1,024 pages, checks the read-only/writable boundary, then decommits. It verifies residency of a locked page and records working set, peak working set and private commit at three points. Values vary between runs. Scarlett structure layout, units and accounting scope remain unconfirmed. [Windows working sets](https://learn.microsoft.com/en-us/windows/win32/memory/working-set)

The same test creates writable and copy-on-write views of a two-page, pagefile-backed mapping. After a write through the copy view, `VirtualQueryEx` reports `MEM_MAPPED` for both pages. With both pages locked and resident, `QueryWorkingSetEx` clears `Shared` only for the written copy page. Writes through the other view remain visible on the untouched page. [Windows copy-on-write queries](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualqueryex#remarks)

`src/era_query.cpp` is a desktop handler for `XMemVirtualQuery`, shaped by the four BVH call sites. It writes the Win32 `MEMORY_BASIC_INFORMATION` to bytes `0x00`–`0x2F`; the callers read State at `+0x20` and Protect at `+0x24`, matching that layout. The byte at `+0x30` has no public definition: the Microsoft reference for `XMemVirtualQuery` and its result structure is restricted to the Xbox developer program.

The handler therefore fills that byte by a desktop policy, not by a confirmed Scarlett field:

- it writes `0x01` when the queried address lies in a region registered with `register_gpu_region`, and `0x00` otherwise;
- later bytes are left untouched;
- the policy applies only to queries of the current process;
- a buffer shorter than 48 bytes fails with `ERROR_BAD_LENGTH`, as `VirtualQueryEx` does.

With registration, the callers' predicates accept GPU-intended memory and reject everything else. What the console's tail byte means, and what sets it, remain open. The selector `0x20000001` of `XMemGetWorkingSetStatistics` has no public reference either. Its callers use the value at `+8` as a byte count of GPU memory (see the call evidence below); the desktop `xmem.dll` below returns this process's DXGI GPU memory usage there.

| CTest case | Inputs and checks |
|---|---|
| `era_query_fields` | Reserved, committed read/write, read-only and no-access pages, each queried with and without registration. Bytes `0x00`–`0x2F` match a plain `VirtualQueryEx` result byte for byte, and bytes after `+0x30` keep their sentinel. |
| `era_query_tail` | Negative control: before registration a committed read/write page fails the tail check. After registering two pages, the tail byte is set at the first, middle and last byte and clear on the next committed page and on a reserved page. The callers' predicates accept read/write and read-only, reject the source check on no-access, and reject both after decommit. A real handle to the process counts as the current process, and unregistering clears the flag. |
| `era_query_registry` | Null, empty and wrapping ranges are refused. Exact, front, back, containing and contained overlaps are refused, adjacent ranges are accepted, and unregistering needs the exact base. |
| `era_query_short` | A 47-byte buffer returns 0 with `ERROR_BAD_LENGTH` and is left untouched. A 48-byte buffer succeeds without writing the tail byte. A null output returns `ERROR_BAD_LENGTH` at 47 bytes and `ERROR_NOACCESS` at 48 or 64 bytes, matching desktop `VirtualQueryEx`. |

### Desktop xmem.dll

`src/xmem_shim.cpp` builds `xmem.dll`, which exports the four functions the package DLLs import by name. The call shapes come from the observed call sites, not from a published header:

| Export | Observed call shape | Desktop handling |
|---|---|---|
| `XMemAlloc` | Size and attribute word (Bink and AMD callers). | `era_heap::allocate`. Graphics memory types are also tracked by `era_title_gpu`, which registers them with `era_query` so they count as GPU-intended, and gives them desktop GPU addresses once a D3D12 device is attached (see below). |
| `XMemFree` | Allocation and the same attribute word. | Untracks graphics allocations, dropping any alias and the `era_query` registration, then `era_heap::release_with_attributes`. |
| `XMemVirtualQuery` | The four BVH callers pass the process in `rcx`, the address in `rdx` and a 64-byte buffer in `r8`; `r9` is not set, so there is no length argument. They test the 32-bit result in `eax`. | `era_query::query` on the 64-byte buffer; returns `TRUE` when it succeeds. |
| `XMemGetWorkingSetStatistics` | The seven AMD callers pass `0x20000001` in `ecx` and a 128-byte stack buffer in `rdx` and test the 32-bit result. On failure each uses zero instead of the 64-bit value at `+8`. They use only the difference of that value around GPU heap and committed-resource creation and release. | For `0x20000001`, writes the qword at `+8` with this process's GPU memory in bytes and returns `TRUE`; no other byte is written. The value is DXGI `QueryVideoMemoryInfo` `CurrentUsage`, local plus non-local on every node of an adapter LUID, combined by the aggregation rules below. A LUID's nodes are its linked physical adapters: DXGI enumerates a linked set as one adapter, and `QueryVideoMemoryInfo` takes the physical adapter index as its node. The shim reads their count with D3DKMT (`KMTQAITYPE_PHYSICALADAPTERCOUNT`). `dxgi.dll` and `gdi32.dll` (D3DKMT) are delay-loaded on the first call. When the rules give no total (including when the adapter list cannot be stored), or for other selectors, the call returns `FALSE` with `ERROR_NOT_SUPPORTED`; a null output returns `ERROR_INVALID_PARAMETER`. A failed call leaves the whole output untouched. |
| `XMemTransferMemory` | Partition selector in `ecx` and byte count in `rdx`. | On a no-tools desktop runtime without devkit tools-memory backing (`XMemIsToolsMemAvailable() == FALSE` per #896), returns `FALSE` with `ERROR_NOT_SUPPORTED` without modifying memory or accounting state. Resolving this export allows PE import binding to succeed at load time, which is distinct from establishing that a caller's startup sequence can proceed past an unsupported transfer. |

Windows can list one device under several LUIDs, and each view reports the same allocations. Without a GPU, Windows Server 2025 lists the Microsoft Basic Render Driver with flags 0 and as a software adapter. A hybrid laptop on Windows build 26200 (RTX 4060 Laptop, driver 32.0.16.1656) listed its NVIDIA GPU twice with flags 0. In that case both local counters moved from 7,294,976 to 74,403,840 bytes on one 64 MiB heap, so a plain sum reported 2x. Matching vendor and device IDs, or equal readings, do not show that two LUIDs are one device: separate adapters of the same model must each be counted. The shim therefore identifies each LUID by the PnP hardware registry keys of its physical adapters, which contain the device instance path, read with D3DKMT (`src/adapter_identity.cpp`), and `src/xmem_gpu_usage.h` publishes a total only when it is complete and non-duplicated:

- an incomplete enumeration, no adapters, an adapter whose node count cannot be read, any failed reading, or an overflowing sum gives no total;
- registry key names are not case sensitive, so keys are compared ordinally ignoring case (`CompareStringOrdinal` with the operating system's uppercase table);
- LUIDs with the same keys count once and must report the same value; LUIDs with different keys are summed;
- LUIDs that share some but not all keys give no total;
- an adapter whose keys cannot be read, including a key query whose reported length is not a nonempty terminated string inside the buffer, may report usage only when no other adapter does;
- a total of zero is valid.

The registration of graphics memory and the working-set value are desktop policies, not confirmed Scarlett behavior. The console value's scope and what it counts besides GPU allocations remain open; the callers depend only on its differences.

A local scan of the inspected package's 16 DLLs together with this `xmem.dll` (`tools/pe_inventory.py`) resolved all six xmem import edges to it. The unresolved edges dropped from eight to two, both `d3d12_xs.dll` root-signature functions. The scan reads the images and runs no package code.

| CTest case | Inputs and checks |
|---|---|
| `xmem_shim_exports` | The four names resolve through `GetProcAddress`, and the test's named imports bind to the same module. Neither `d3d12.dll` nor `dxgi.dll` is loaded (the test executable delay-loads both for its GPU cases). |
| `xmem_shim_allocation` | The Bink word returns 32-byte-aligned, zeroed heap memory. The AMD word returns committed read/write memory on a 64 KiB boundary that `XMemFree` releases (`MEM_FREE`). Zero size, a reserved bit and a physical memory type return null, and freeing null is a no-op for both memory types. |
| `xmem_shim_query` | Graphics memory from `XMemAlloc` passes both BVH predicates and keeps the sentinel after `+0x30`, through the pseudo-handle and a real handle. After `XMemFree` the address reads as `MEM_FREE` with a clear tail. Heap memory fails the tail check, and an address outside user space returns `FALSE` without touching the buffer. |
| `xmem_shim_working_set` | Selector `0x20000001` returns `TRUE` and writes only the qword at `+8` of a 128-byte buffer. Selector `0x20000002` returns `FALSE` with `ERROR_NOT_SUPPORTED` and leaves the buffer untouched; a null output returns `ERROR_INVALID_PARAMETER`. |
| `xmem_shim_transfer_memory` | Verifies that `XMemTransferMemory` returns `FALSE` with `ERROR_NOT_SUPPORTED` under caller sequences (including partition 1 and 2 MiB / 16 MiB counts), leaving existing heap/graphics allocations, physical page grants, query registration, tools memory status and working-set statistics untouched. |
| `xmem_shim_working_set_aggregation` | Synthetic readings for the aggregation rules. Regression reference: the laptop readings above give no total when the two LUIDs have no identity, and a 64 MiB difference when both carry the same key. Also covered: Basic Render Driver aliases count once, distinct adapters of the same model with equal readings are summed, a genuine zero is published, and an all-failed, partly failed or incomplete enumeration, disagreeing aliases, an active adapter of unknown identity next to another active adapter, partly overlapping linked adapters and an overflowing sum give no total. Keys that differ only in case count once, must agree, and still give no total when they partly overlap. Malformed key query output (nothing written, an empty key, a length past the buffer, no terminator, an embedded terminator) gives no key. Per-node readings: usage only on node 1 of a linked pair is counted, and an unknown node count, a segment query that fails after a successful one (on the same node or a later node) and an overflow between segments or nodes give no total. |
| `xmem_shim_working_set_warp`, `xmem_shim_working_set_hardware` | As the callers do, reads `+8` (zero on failure) before and after creating and releasing 64 MiB DEFAULT, UPLOAD and READBACK heaps and a 32 MiB committed buffer on a new device. Each creation must add at least the allocation size (`GetResourceAllocationInfo` for the buffer) and at most 4 MiB more, and each release must remove as much. Negative control: with the former `ERROR_NOT_SUPPORTED` stub every difference is 0 and both cases fail. Prints each DXGI adapter with its LUID, node count and PnP keys. The hardware case measures the first D3D12 adapter in high-performance order, or the first whose description contains `XBOX2WINDOWS_ADAPTER` (ignoring case; no match fails), and prints it. Skips (77) without the adapter or a D3D12 device. |

### Default allocation pair under the Windows loader and pe_map

Refs #875, #879 and #870. This is a test of the exports #879 added (see [Default allocation imports](#default-allocation-imports)), not a change to them. `cpu_native_xmem_default_pair` builds `tests/cpu/xmem_default_pair.asm` into four x64 EXEs that start through `exe_startup.asm`. Each imports by name:

- `XMemAllocDefault` and `XMemFreeDefault` from `xmem.dll`;
- `VirtualQuery`, `SetLastError` and `GetLastError` from `kernel32.dll`;
- `heap_block_busy` from `cpu_heap_probe.dll`;
- `finish_exe` from `cpu_exe_io.dll`.

The test stages each EXE with its DLLs, launches it normally, then maps it in a child process and starts it with `pe_map_run`. The body returns `0x875` when its 21 steps pass, or `0xF00` plus the first failing step. The test prints one line per named check:

| Check | Steps and evidence |
|---|---|
| `pe_import_names` | `pefile` reads each EXE's import table. `xmem.dll` must be imported for exactly `XMemAllocDefault` and `XMemFreeDefault`, by name. |
| `mapped_resolver_names` | The `pe_map` resolver callback records every request. It must ask for both names and the five other imports, and nothing else. |
| `heap_block` | 1–5. `XMemAllocDefault(257, 0x05e50000)` returns a block that is 32-byte aligned and zero-filled, and takes writes. `heap_block_busy` finds a busy heap entry containing it. |
| `graphics_block` | 6–9. `XMemAllocDefault(4096, 0xa8004000)` returns a block that is 64 KiB aligned and zero-filled, and takes writes. |
| `rejected_inputs` | 10–14. Reserved bit 33, a zero size, page size 3 and memory type 15 each return null. The last-error value set to 0 before these calls is still 0, so no system allocator ran. |
| `induced_failure` | 15–18. A valid graphics word with 2^46 bytes returns null with a nonzero last-error value set by `VirtualAlloc`. A valid heap word with 2^46 bytes returns null. The same graphics word with 4096 bytes then succeeds, and that block is freed. |
| `graphics_release_virtualquery` | 19–20. After `XMemFreeDefault` on both blocks and on null, `VirtualQuery` reports the graphics address as `MEM_FREE`. |
| `heap_release_ownership` | 21. No busy heap entry contains the heap block any more. A freed CRT block normally stays inside committed heap memory, so `VirtualQuery` cannot show its release. `tests/cpu/heap_probe.cpp` instead locks each process heap and walks it with `HeapWalk`. |

| Variant | Windows launch | `pe_map` | First failing check |
|---|---|---|---|
| `original` | `0x875` | `0x875` | none |
| `skip_gpu_free` | `0xf14` | `0xf14` | `graphics_release_virtualquery` |
| `skip_heap_free` | `0xf15` | `0xf15` | `heap_release_ownership` |
| `uninduced` (4096 bytes at step 15) | `0xf0f` | `0xf0f` | `induced_failure` |

A provider that binds only one of the pair stops the mapping before the entry point. The diagnostic is `unresolved import xmem.dll!XMemAllocDefault` or `unresolved import xmem.dll!XMemFreeDefault`.

What this establishes, and what it does not:

- **ABI.** [SlimEra `era.h` at 912bb11](https://github.com/XWine1/SlimEra/blob/912bb1123e84f32023af6685989ac5d6747d767a/era.h) declares `PVOID WINAPI XMemAllocDefault(SIZE_T, ULONGLONG)` and `void WINAPI XMemFreeDefault(PVOID, ULONGLONG)`. This is an independent reimplementation's declaration, not console evidence. The fixture calls the pair with that shape.
- **Desktop behavior.** The steps record what the desktop shim does: alignment, zero fill, null on rejected input or allocation failure, and release with the matching word. The last-error results distinguish the two failure kinds on the desktop only. Console failure, alignment and release behavior remain unverified.

```powershell
cmake --build --preset dev --target pe_map cpu_exe_io cpu_heap_probe cpu_xmem_default_original cpu_xmem_default_skip_gpu_free cpu_xmem_default_skip_heap_free cpu_xmem_default_uninduced
ctest --preset dev --no-tests=error -R '^cpu_native_xmem_default_pair$' -V
```

### Desktop d3d12_xs.dll

`src/d3d12_xs_shim.cpp` builds `d3d12_xs.dll`, exporting the two root-signature functions that remained unresolved across the 16 package DLLs after `xmem.dll`:

| Export | Observed call shape | Desktop handling |
|---|---|---|
| `D3D12SerializeVersionedRootSignature` | `XboxBvhBuilder.dll` (`0xc2d0`/`0xc32f`) copies a 48-byte descriptor, sets console flag bits `0x03000000` at `+0x28`, and passes a null error blob. | Clears only the observed `0x03000000` bits in a local copy for v1.0/v1.1 descriptors before calling desktop `d3d12.dll`. Unobserved invalid flag bits stay untouched so desktop `d3d12.dll` still returns `E_INVALIDARG`. |
| `D3D12CreateRootSignatureDeserializer` | `amd_fidelityfx_upscaler_xbox.dll` (`0x4f88`) passes a pointer and length from an embedded container plus an interface IID and output address. | Forwards directly to desktop `d3d12.dll`. |

This covers only the two root-signature imports observed in the pre-executable DLL inventory. Any `d3d12_xs.dll` APIs imported by a future main executable remain separate work.

| CTest case | Inputs and checks |
|---|---|
| `d3d12_xs_shim_exports` | Both names resolve through `GetProcAddress` inside `d3d12_xs.dll`, and the test's named import accepts `0x03000000` while desktop `d3d12.dll` rejects it with `E_INVALIDARG`. |
| `d3d12_xs_shim_serialize` | Baseline and console-tagged (`0x01000000`, `0x02000000`, `0x03000000`) v1.1 and v1.0 descriptors serialize identically to desktop `d3d12.dll` without mutating caller input while preserving desktop flags. Unobserved flag bits (`0x04000000`, `0x07000000`, `0x08000000`) return `E_INVALIDARG`, and a null descriptor or unknown version fails with the same HRESULT as desktop `d3d12.dll`. |
| `d3d12_xs_shim_deserialize` | Round-trips a sanitized blob from `d3d12_xs.dll` and reads the embedded `RTS0` part of `pso_compute_embedded`; rejects `pso_compute_stripped` and empty/null buffers with `E_INVALIDARG`. |
| `d3d12_xs_shim_warp_root` | Serializes a v1.1 `SRV(t0),UAV(u0),RootConstants(16,b0)` descriptor tagged with `0x03000000` through `d3d12_xs.dll` (verified rejected by desktop `d3d12.dll`), creates a WARP `ID3D12RootSignature`, and builds a compute PSO with `pso_compute_stripped`. |

#### D3D12ConfigureVirtualMemoryX call shape (#874)

The main executable imports `d3d12_xs.dll!D3D12ConfigureVirtualMemoryX` and calls it once, before the D3D12 device is created. A static read of the call site shows:
- one pointer argument to a 32-byte block, zero-filled except for the DWORD at `+4`, which holds 3 or 4;
- the HRESULT checked with `SUCCEEDED`;
- two `XMemVirtualAlloc(NULL, size, 0x20402000, …)` reservations afterwards, whatever the result was. `size` is 16 GiB for the sample 3 and 24 GiB for 4.

The meaning of the DWORD, the purpose of the other 28 bytes and the effect the call must have are open. `d3d12_xs.dll` exports it with a strict profile limited to that shape. It accepts the block only when the DWORD at `+4` is 3 or 4 and every other byte is zero, and returns `S_OK`. It accepts one configuration per process, before `D3D12XboxCreateDevice` has created a device. Otherwise it returns `E_POINTER` for a null block, `E_INVALIDARG` for any other content, and `DXGI_ERROR_INVALID_CALL` for a second or late call. It only reads the block, and a refusal changes no state.

The accepted value is recorded and does not change device creation yet. `src/d3d12_xs_range.h` and `src/d3d12_xs_range.cpp` implement the GPU range it describes, for a later owner of the title's device. `d3d12_xs_range::create` reserves a range of `(value - 2) × 16 GiB` of GPU virtual address space: 16 GiB for 3, and 32 GiB for 4, which covers the caller's 24 GiB. The caller computes the value as `ceil(size / 16 GiB) + 2`, so this size is a policy that covers its range. The space is split into reserved buffers ("windows") of at most 4 GiB each, or less where the adapter's per-resource limit is lower. A caller maps a window's 64 KiB tiles with `UpdateTileMappings` on its queue; the title maps its range's pages through a copy queue in the same way. `create` returns an owning `IX2WGpuRange` reference. If any window cannot be reserved, it returns that HRESULT and no range, and every window created before the failure is released. The windows are device children and hold the device until the range releases them, on `Close()` or on the range's last release. Nothing else holds the range, so a closed or released range leaves the device to its own last release. The owner waits for GPU work that uses the windows (for example with a fence on each queue that used them) before `Close()` or the final release. `Close()` takes the range's lock exclusively, and the queries take it shared, so a query that overlaps `Close()` sees either the open range or an empty one; a second `Close()` does nothing. `D3D12XboxCreateDevice` returns an ordinary `ID3D12Device` that its caller can release without this protocol, so attaching a range there waits for an owner that can close it before the device's final release. The 4 GiB limit comes from the #874 probe: on an RTX 3080 Ti, tile mappings at or above 4 GiB inside one reserved buffer had no effect, and in-box WARP 10.0.19041 refuses reserved buffers of 4 GiB.

`tests/cpu/configure_vm.asm` reproduces that call shape in two source-built EXEs, one per sample. It surrounds the block with canaries and returns the configuration HRESULT if the call fails. Recording stand-ins named `d3d12_xs.dll` and `xmem.dll` (`tests/cpu/configure_vm_recorder.cpp`, in the build's `configure_vm` directory) only log each call and return a chosen HRESULT.

| CTest case | Inputs and checks |
|---|---|
| `cpu_native_configure_vm` | Each EXE imports `D3D12ConfigureVirtualMemoryX`, `XMemVirtualAlloc` and the shared `finish_exe` by name. The native run starts a worker before process termination, and the mapped entry returns to its host. With either recording stand-in missing, `pe_map` stops at that import before entry. Both loaders log the same 32-byte snapshot and both reservations in order. A success returns 0. A stand-in failure (`0x8007000E`) reaches the exit code unchanged after both reservations. A stand-in write inside or after the block exits with 1 or 2. |

Run `ctest --preset dev --no-tests=error -R '^cpu_native_configure_vm$' -V`.

#### CPU/GPU range translation against a desktop D3D12 heap (#874)

A static read of the main executable shows that, after device creation, it keeps the first reservation as a CPU range A and the second as a range B of the same size. It translates by offset: `gpu = cpu - A + B`, and a GPU address inside `[B, B + size)` maps back to `gpu - B + A`. Other addresses pass through unchanged. One helper applies the reverse direction to an `ID3D12Resource`'s GPU virtual address. Without a device, the executable releases both reservations and uses one range for A and B.

`tests/vm_translation_check.cpp` mirrors both helpers against desktop D3D12. On the device path, a committed 64 MiB `VirtualAlloc` range is opened with `ID3D12Device3::OpenExistingHeapFromAddress`, and one cross-adapter buffer is placed over all of it. That buffer's GPU virtual address is used as B. The compute shader (`tests/vm_translation.hlsl`) receives only root UAV addresses. The sequence cases repeat those checks after the title's startup order through `d3d12_xs.dll`: configure, create the device, then create the first DIRECT queue.

| CTest case | Inputs and checks |
|---|---|
| `vm_translation_shared_range` | No device. A and B are one range, so both directions are the identity inside it, and addresses just outside pass through. |
| `vm_translation_device_warp`, `vm_translation_device_hardware` | A reserve-only range and an address inside the allocation both fail to open as a heap. A dispatch addressed by `p - A + B` reads 1,024 CPU-written words and writes 1,024 words the CPU reads back. A second buffer placed at `+0x200000` reports GPU address `B + 0x200000`. That address translates to `A + 0x200000`, and GPU writes through it are visible there. An unrelated committed buffer's GPU address lies outside `[B, B + size)` and passes through. A B shifted by 64 KiB misses all 1,024 destination words. Skips (77) without the adapter or `ID3D12Device3`. |
| `vm_translation_sequence_warp`, `vm_translation_sequence_hardware` | Before any device, `D3D12ConfigureVirtualMemoryX` refuses a null block and blocks with the DWORD set to 0, 1, 2, 5, `0x80000003` or `0xFFFFFFFF`, or with a non-zero byte at `+0`, `+3`, `+8` or `+31`. It accepts the 4 sample, then refuses a second call with 4 or 3. The 32-byte block and 16 guard bytes on each side are unchanged after every call. `D3D12XboxCreateDevice` creates the device on the same adapter as the device cases. On that adapter it refuses a parameter block (`E_INVALIDARG`) and another interface (`E_NOINTERFACE`), with the output cleared. Configuration after the device is refused. The first DIRECT queue with the default description is created. Then queues with an unknown type, an unknown priority or an absent node are refused without returning a queue, and the device is not removed. The device-case checks then run on that first queue. The adapter's per-resource and per-process GPU virtual address bits are printed and not checked. Skips (77) as the device cases. |
| `vm_translation_range_{warp,hardware}` | `d3d12_xs_range::create` reserves 32 GiB in windows of at most 4 GiB. Spans across a window edge or past the end have no GPU address. An unmapped tile reads zero. The test maps three tiles on the first DIRECT queue: the range start, the first tile of the second window, and the last tile. It copies distinct words into each from a 64 KiB pagefile section, and a dispatch reads them back at the range's GPU address. One tile further reads the unmapped neighbour, and staging writes after the copy are not seen. After the queue is idle, `Close()` leaves an empty range. Before the range checks, a test-owned reserved buffer must read back a copied tile through a dispatch; otherwise the case skips (77). |
| `vm_translation_range_lifetime_{warp,hardware}` | The test registers an `ID3DDestructionNotifier` callback on each device. First, a device created through `d3d12_xs.dll` after the 4 sample carries no range (`DXGI_ERROR_NOT_FOUND`), and its last ordinary release destroys it. Next, a range is created on a fresh desktop device. Releasing the device's last caller reference while the range is open leaves one reference per window and no callback. Releasing the range then destroys the device. In a second round, a thread reads the range's GPU address, window count and first window while the main thread calls `Close()`. Every read matches the open range or the empty one. A second `Close()` does nothing, and the device's last release then returns 0 with one callback. |
| `vm_translation_range_partial_{warp,hardware}` | `d3d12_xs_range::create` against a desktop device, with window creation failing at window 1 or at window 3. It returns the failing `E_OUTOFMEMORY` after that many calls and no range, and the device's last release destroys it, so the windows created before the failure were released. |

On Windows 10 19045 with D3D12 10.0.19041.7291, both device cases passed on in-box WARP 10.0.19041.7811 and on an RTX 3080 Ti (driver 32.0.16.1047). The second placed buffer's address following `B + offset` is observed on these adapters; D3D12 does not document it for placed resources. Both sequence cases passed on the same adapters, in-box WARP 10.0.19041.7811 reporting 32 GPU virtual address bits per resource and 47 per process, and the RTX 3080 Ti 40 and 40. The range case passed on the RTX 3080 Ti (eight 4 GiB windows) and on WARP 1.0.22 loaded app-locally. On in-box WARP 10.0.19041.7811 (nine 4095 MiB windows), `range_warp` skips at the capability check. The lifetime and partial-failure cases passed on both adapters. With the range open, the released device kept 8 references on the RTX and 9 on in-box WARP.

Because reserve-only ranges and addresses inside an allocation cannot be opened, the committed-heap layout of the device cases needs all of A committed and wrapped once. A 16 or 24 GiB range would be committed in full, and separately opened pieces would each get their own GPU base. The range uses reserved buffers instead, filled by copy. Neither the CPU range A nor the title's device is connected to it yet.

Run `ctest --preset dev --no-tests=error -R '^vm_translation_' -V`.

#### Ordering a tile remap across D3D12 queues (#874)

`tests/vm_copy_queue_check.cpp` maps two tiles in a reserved buffer and uploads a distinct 64 KiB pattern to each on a COPY queue. A DIRECT queue waits for the upload fence and reads the first pattern. The COPY queue waits for this read, remaps the first virtual tile to the second physical tile, and signals a new fence value. The DIRECT queue waits for that value and reads the second pattern from the same virtual address. The CPU waits only for the final readback.

The test checks all 16,384 words in each read. The deliberately early read differs from all 16,384 words of the remapped pattern. Queue waits order both reads with respect to the remap, so the early result does not rely on a race. The fixture uses stock D3D12 queues, and skips (77) when the requested adapter or tiled-resource support is unavailable.

Run `ctest --preset dev --no-tests=error -R '^vm_copy_queue_' -V`.

`vm_page_provenance_warp` and `vm_page_provenance_hardware` test the source side independently. Two pagefile sections hold distinct data and are mapped into adjacent CPU addresses in reverse creation order. D3D12 reopens those same sections as heaps. The test copies a 4 KiB span centered on the page boundary into a GPU buffer using the logical page order, then compares its readback with the two expected token patterns. It writes new data through the CPU aliases and reads the GPU buffer again before uploading, which must still return the old data. A second upload must produce all the updated words. The tests skip (77) if the requested adapter or section-backed D3D12 buffers are unavailable. [Opening a D3D12 heap from a file mapping](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device3-openexistingheapfromfilemapping)

Run `ctest --preset dev --no-tests=error -R '^vm_page_provenance_' -V`.

#### Xbox frame events and token wait semantics (#874)

The source-built frame events fixture (`tests/cpu/frame_events_caller.asm`, `tests/cpu/frame_events_provider.cpp`, `tests/cpu/frame_events_check.py`, CTest `cpu_native_frame_events`) models the scheduling and wait semantics of the Xbox frame-pacing extensions in `d3d12_xs.dll`:
- `SetFrameIntervalX(output, interval, period, flags)`: configures frame cadence.
- `ScheduleFrameEventX(event_type, offset_us, event_handle, flags)`: registers frame event scheduling.
- `WaitFrameEventX(event_type, timeout_ms, event_handle, flags, out_token)`: waits for frame pacing milestones and produces a 64-bit frame pipeline token (`D3D12XBOX_FRAME_PIPELINE_TOKEN`).

The supported call sequence follows Microsoft's public Xbox sample ([`DeviceResources.cpp`](https://github.com/microsoft/Xbox-GDK-Samples/blob/41b5942c4fba910332123070e51827761f6040fd/Samples/Live/InGameStore/DeviceResources.cpp)):
1. `SetFrameIntervalX` initializes cadence.
2. `ScheduleFrameEventX(D3D12XBOX_FRAME_EVENT_ORIGIN, 0, nullptr, 0)` registers the frame origin once.
3. `WaitFrameEventX(D3D12XBOX_FRAME_EVENT_ORIGIN, ...)` waits each frame and retrieves the frame token. Passing the token to `PresentX` is a cited follow-up (this caller verifies token delivery but does not call `PresentX`).

Contract boundaries and fixture choices:
- **Established call shape**: `SetFrameIntervalX → ScheduleFrameEventX(ORIGIN) once → WaitFrameEventX(ORIGIN) each frame` with `nullptr` event handle and token delivery.
- **Fixture choices (labeled as requiring separate evidence)**:
  - Exposing these methods as PE imports from `d3d12_xs.dll` is a fixture choice (the GDK sample invokes device methods).
  - Passing a null output to `SetFrameIntervalX` is a fixture choice (the sample passes an actual `IDXGIOutput*`).
  - Numeric constants (e.g. 60) and seeded token representation (monotonically advancing 64-bit integer 1, 2, ...).
  - After one registration, an internal worker delivers each of two ticks only when the caller enters a condition-variable wait and releases the shared lock. No delay drives delivery. The mapped recorder checks one registration, two delivered waits, one timeout and two refusals.
  - Bounded timeout before tick leaves the output sentinel unchanged and returns `WAIT_TIMEOUT` (`0x00000102`).
  - Unrepresented event refusal: event types other than `D3D12XBOX_FRAME_EVENT_ORIGIN` (value 0) return `E_INVALIDARG` (`0x80070057`) leaving the output sentinel intact.
  - Separate fault controls: `cpu_frame_events_provider_clobber` tests refusal sentinel overwrite (specific exit `0xE03`), and `cpu_frame_events_provider_early` tests premature release before tick (specific exit `0xE04`).
- **Loaders and withholding**: The fixture runs under native Windows (with and without a live worker thread) and under `pe_map`. Withholding `d3d12_xs.dll` halts `pe_map` at import resolution with `STATUS["unresolved"]`.

Run `ctest --preset dev --no-tests=error -R '^cpu_native_frame_events$' -V`.

### Attribute words at xmem call sites

`tools/xmem_attribute_scan.py` reads a PE image without loading or running it. It reports the attribute word at each call or tail jump into `xmem.dll`'s `XMemAlloc` or `XMemFree`, from a normal or delay-load import. It finds sites reached through the IAT slot, through an import thunk, or through a register loaded from the slot.

Code is decoded one `.pdata` function at a time, and linearly between functions, so memory stays bounded for a large image.

From each site the scanner walks backwards over the function's control flow to every definition of RDX:
- **Jumps:** jumps inside the function are followed. A join reports each distinct definition, and a loop that does not change RDX adds nothing.
- **Calls:** a call on the path may change RDX under the Microsoft x64 convention, so it ends that path as unknown. Non-volatile registers are followed across calls.
- **Immediates:** an immediate, including `xor edx, edx`, is a constant. A 32-bit register copy keeps only the low 32 bits.
- **Data loads:** a load from RIP-relative data is a constant when the data is in a read-only section. In a writable section it is reported as data and is never a constant, because a write through a pointer can change the word before the call and such writes are not tracked. The report keeps the initial value and the RIP-relative writes found (`written_at`, possibly empty) as evidence only.
- **Function entry:** reaching the entry means the caller supplies the register still being tracked, including the register of a followed copy such as `mov rdx, r8`. Direct calls and jumps to the entry are resolved one level deep from that register; a 32-bit copy on the way truncates the caller's value. An exported function can also be called from outside the image, so its sites never count as complete.
- **Everything else:** partial-register writes, arithmetic, pointer loads, blocks reached only through jump tables, and chained unwind fragments are reported as computed or unknown, never as constants.

Constant words are decoded with the `era_heap` layout and classified with `era_heap::allocate`'s desktop rules for a non-zero size.

The report lists each site with its route and region, and the import thunks found. Its summary gives:
- every distinct word with its sites;
- the words for which `era_heap` returns null;
- the sites with a path that does not end in a constant.

`--fail-on-null` and `--fail-on-unresolved` exit with 1 in those cases.

| CTest case | Inputs and checks |
|---|---|
| `xmem_attribute_scan` | Two source-built MASM fixtures are linked against the desktop `xmem.dll` import library. `tests/cpu/xmem_attributes.asm` has 25 sites, one per form: IAT call with unwind data, linker thunk, read-only constant data, writable data with no RIP-relative writer, writable data written through an address held in a register before the RIP-relative load into EDX, written data, register copy, call through a register, exported and internal pass-throughs with a caller, caller-copy pass-throughs whose entry register is R8 (64-bit, and 32-bit with the caller's value truncated), 64-bit immediate with a reserved bit, `xor`, a computed word, direct and indirect jumps, if/else joins with equal and different words, a loop, a call that clobbers RDX, a non-volatile register across a call, a 32-bit copy of a 64-bit constant, and a partial-register write. Each site's route, source, word and classification must match, as must the summary and the exit codes of both flags. Both writable-data sites must be unresolved, so `--fail-on-unresolved` fails on them. `tests/cpu/xmem_delay.asm` is linked with `/DELAYLOAD:xmem.dll` and has one constant site through the delay-load IAT. Both flags must exit with 0 on it. The 13 decoding cases also run in the Linux Python job. |

Sixteen mutants of the scanner each fail this test. They cover call clobbering, join following, merging, read-only data, writers, truncation, export completeness, thunks, delay imports, indirect blocks, register following, `xor`, partial writes, caller resolution, register calls, and the null flag. After the writable-data change, two more mutants also fail it: treating writable data with no RIP-relative writer as a constant (the earlier behaviour, which reported the pointer-written site as constant `0xa8004000`), and dropping `written_at`. After the entry-register change, two more also fail it: resolving callers with the default RDX instead of the recorded entry register, and dropping the 32-bit truncation of the entry register.

A local read-only scan of the inspected package's 16 DLLs found `xmem` call sites in two of them. All five are constant on every path and resolve to the two words already pinned in `era_heap_observed_words`:
- the Bink DLL: two sites, both through linker import thunks, word `0x05e50000` (heap, 32-byte alignment);
- the AMD upscaler: three sites through the IAT slot, word `0xa8004000` (memory type 5, an allocation-granularity graphics allocation), loaded from a read-only section.

One full decoding pass over the largest DLL, 0.7 MB of code with 2,452 `.pdata` functions, read 342,768 instructions in 0.07 s.

```powershell
.\out\pe-env\Scripts\python.exe -B tools/xmem_attribute_scan.py "game-files/<build-directory>/<image>" --output out/xmem-scan.json --fail-on-null
```

### Desktop GPU addresses for title allocations

On the console a graphics allocation's CPU pointer is also its GPU virtual address. Titles pass those addresses to D3D12 calls, and the BVH builder's shaders read 64-bit addresses from their input records. A desktop D3D12 resource gets a GPU virtual address chosen by the driver.

- `era_gpu_alias::open` (`src/era_gpu_alias.cpp`) opens an existing `VirtualAlloc` allocation, such as an `era_heap` graphics allocation, with `ID3D12Device3::OpenExistingHeapFromAddress`. It then places one cross-adapter buffer with unordered access over it.
- The title pointer and the buffer's GPU virtual address then name the same system-memory bytes, without copies.
- `era_title_va::Table` (`src/era_title_va.cpp`) maps title address ranges to those desktop GPU virtual addresses.

What is covered:
- Translating title addresses that D3D12 calls receive. Root views at translated addresses are exercised below.
- Shader loads through 64-bit title addresses read from memory, via `src/title_va_lookup.hlsli`.
- Stores, integer add and sub atomics, and float min and max (a compare-exchange loop) through those addresses, on the same aliased system-memory heaps.

Not covered:
- Performance. GPU access to this heap goes over the bus.

How the shader lookup works:
- `era_title_va::shader_ranges` packs the table's sorted ranges: low and high words of begin and end, and a descriptor slot.
- `TitleLookup` binary-searches them with 64-bit comparisons on `uint2`. It takes the access size and succeeds only for a 4-byte-aligned address whose whole access lies inside one range. It then returns the slot of the buffer that aliases the range, and the byte offset, which is exact because a range is shorter than 4 GiB. `shader_ranges` also requires each range to start on a 4-byte boundary.
- The shader then loads through that slot's raw UAV.

| CTest case | Inputs and checks |
|---|---|
| `era_title_va_translate` | Offsets inside two ranges, the last word of a range, and a zero byte count translate to the matching desktop address. |
| `era_title_va_bounds` | Bytes just before and after a range, spans crossing its end, a span longer than the range and a wrapping span return 0. |
| `era_title_va_registry` | Empty, wrapping, zero-GPU and overlapping ranges are rejected; adjacent ranges are accepted and listed in order; a removed range no longer translates. |
| `gpu_alias_warp`, `gpu_alias_hardware` | A 256 KiB memory-type-1 allocation from `era_heap` is opened as a heap. A compute shader reads 4,096 words through a root UAV at the translated title address `+0x1000` and writes transformed words through a root UAV at `+0x20000`. The CPU checks every result and the unchanged source through the title pointer, with no readback buffer. 64 guard words after the written range must keep their sentinel. Opening a pointer `0x1000` into the allocation must return `E_INVALIDARG` from `era_gpu_alias::open` without reaching D3D12; passing it to `OpenExistingHeapFromAddress` makes the debug layer report message 1088. Returns 77 when the adapter does not report `D3D12_FEATURE_EXISTING_HEAPS`. |
| `era_title_va_shader` | Packing splits begin and end into low and high words, including a range across a 4 GiB line; slots follow table order; unsorted, overlapping, empty, 4 GiB and unaligned-start ranges are rejected, and a length that is not a multiple of 4 is accepted. |
| `gpu_alias_lookup_warp`, `gpu_alias_lookup_hardware` | Three memory-type-1 allocations and one placed across a 4 GiB line are opened as heaps, packed, and bound as raw UAVs in a descriptor table. The placed range ends two bytes short of its allocation, so its last aligned word crosses the range end. A compute shader loads one DWORD through each of 150 title addresses read from an input buffer: starts, middles, each range's last valid word and the aligned word after it, misaligned starts at both ends, the words just outside each allocation, the null page, an address above user space, and the same low words 4 GiB above and below the placed allocation. Values and slots must match the CPU reading through the title pointers; misaligned, tail-crossing and out-of-range addresses must return the miss marker. The hit and miss split varies with where the allocations land. |
| `gpu_alias_lookup_writes_warp`, `gpu_alias_lookup_writes_hardware` | The same four allocations. A compute shader runs 1,047 records through the lookup: 14 stores (range starts, middles, each last valid word, and the words on either side of the 4 GiB line, all distinct); 256 contending threads each for add, sub, float min and float max on one word per operation; and nine rejected records (stores to unmapped addresses, a store and a float min on the tail-crossing word, misaligned store, add and sub inside ranges), which must report 0. Every word of all four allocations is then compared with the CPU expectation through the title pointers, so rejected writes, including the in-range bytes of the tail-crossing word, must leave the backing words unchanged. A compare-exchange loop that does not retry after losing a race passes on WARP, which showed no conflicting update. It fails on hardware. Before the fixture, a probe on four plain zeroed buffers in the same kind of UAV table has 256 threads each add 1 through a per-thread array index; each buffer must read 64, and the expected and actual counts are always printed. The fixture then runs in full and reports two groups, each with a mismatch count and its first mismatch: the shared add and sub words, and everything else (the per-record results and every other word). Wrong probe counts or any mismatch fail the test. It returns 77 only when the adapter does not support existing heaps. |

Measured at commit `55cea41` on Windows 11 build 26300, with the debug layer unavailable (Graphics Tools not installed):
- `gpu_alias_hardware` passed on an NVIDIA GeForce RTX 3070 Laptop GPU (driver 32.0.16.1088).
- `gpu_alias_warp` passed on WARP 10.0.26100.9818.
- On both, the interior open returned `E_INVALIDARG` (`0x80070057`).
- At commit `985f0ab`, `gpu_alias_lookup_hardware` and `gpu_alias_lookup_warp` passed on the same two adapters with 4 ranges and 122 records, mismatches 0 (one run: 104 hits and 18 misses).
- At commit `8bf7666`, `gpu_alias_lookup_writes_warp` passed on WARP 10.0.26100.9818: 1,041 records, sum 128416, min -1250, max 1241.62, three misses, 147,456 words checked, mismatches 0. At the next commit, `gpu_alias_lookup_writes_hardware` passed on the NVIDIA GeForce RTX 3070 Laptop GPU (driver 32.0.16.1088) with the same values, 10 of 10 repeated runs. A compare-exchange loop without retry is caught there (the max word came out low).
- At commit `1c19cb6` (whole-DWORD validation), both lookup tests passed on WARP 10.0.26100.9818: lookup 150 records with 48 misses; writes 1,047 records with 9 rejected, 147,456 words checked; mismatches 0. Removing the span check, the alignment check, or both (the earlier first-byte check) fails both tests. The hardware variants were not re-run for this commit (the GPU was in use).
- On Windows 10 22H2 (build 19045) the in-box WARP 10.0.19041.5794 gives a wrong result, so `gpu_alias_lookup_writes_warp` fails there: in the atomics probe each buffer reads 256, not 64. In the fixture this made the add word `0x7d680` (four times the sum `0x1f5a0`) and the sub word `0x76bc0` (1,000,000 minus four times the sum); stores, compare-exchange and the per-record results were correct. The factor is 4 for arrays of 2, 3, 8 and 16 UAVs, for raw, structured and typed views, with or without `NonUniformResourceIndex`, and `InterlockedXor` is also applied an even number of times. A single UAV and an index read from a constant buffer are exact; a literal index of 2 updates element 0. DXC 1.7.2308, 1.8.2502 and 1.9.2609 give the same result there. App-local WARP 1.0.9 through 1.0.22 and an NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) are exact for the array of 8 and for the literal index, and pass the fixture. On the failing WARP the fixture reports the add and sub words as the only two mismatches: the other group, 1,047 per-record results and 147,454 words, has none. At commit `246990b` the Windows 11 in-box WARP 10.0.26100.9818 read 64 in all four probe buffers with mismatches 0 (measured by the fixture's author).
- At commit `9bdbb20` two RDNA 4 adapters give the same probe result, so `gpu_alias_lookup_writes_hardware` fails on both by design: each of the four buffers reads 256, not 64, on all three runs of each adapter. The adapters are an AMD Radeon RX 9070 XT (device `7550`, driver 32.0.32015.7029, debug layer enabled; [report](https://github.com/R0Y00/Xbox2Windows/issues/369)) and an AMD Radeon RX 9060 XT (device `7590`, driver 32.0.32029.2010, debug layer unavailable; [report](https://github.com/R0Y00/Xbox2Windows/issues/369#issuecomment-7071098620)). On both, the title-address fixture itself records zero mismatches in both groups: the add and sub words, and the 1,047 per-record results and 147,454 other words. In the same runs an NVIDIA GeForce RTX 5060 Ti (driver 32.0.16.2124) and the in-box WARP 10.0.26100.9711 and 10.0.26100.9818 read 64 in all four buffers. Local probe variants on the RX 9070 XT, not committed, isolated the condition. A resource index that varies within a wave gives 256 in every buffer, both without the returned value and when lane pairs share an index. An index that is uniform across each wave, a literal index of 0 and plain stores through `NonUniformResourceIndex` are exact. An explicit waterfall that selects the index with `WaveReadLaneFirst` and runs the atomic once per distinct index reads 64 in all four buffers. The cause is not established.
- At commit `65492448` on Windows 10 22H2 (build 19045.6456, MSVC 19.44.35230.0, Windows SDK 10.0.26100.0 DXC, debug layer enabled on both backends), a GTX 1060 6 GB (driver 32.0.15.6094) reads 64 in all four probe buffers in 3/3 runs, and the in-box WARP 10.0.19041.5794 on the same host reproduces the Windows 10 behavior above: each buffer reads 256 in 3/3 runs, with the fixture reporting the shared add and sub words as the only two mismatches. Both backends matched all 1,047 per-record results and the other 147,454 words, with zero debug errors or warnings ([report](https://github.com/R0Y00/Xbox2Windows/issues/369#issuecomment-7071770464)).
- At commit `185b9d6c` on Windows 11 build 26200 with the debug layer enabled, an RDNA 3 adapter reproduces the same probe failure: an AMD Radeon RX 7900 XTX (device `744c`, driver 32.0.32029.2010) reads 256 in each of the four buffers, with the title-address fixture recording zero mismatches across all 1,047 records and 147,454 checked words and zero D3D12 debug errors or warnings ([report](https://github.com/R0Y00/Xbox2Windows/issues/369)).

- The [RX 6500 report](https://github.com/R0Y00/Xbox2Windows/pull/793) used
  commit `a4ade3032fec08767ed411cc83834a3543c626f2`, driver 32.0.21045.29003,
  Windows 11 build 26300 and the debug layer. The direct probe read
  `256,256,256,256` instead of `64,64,64,64`; its title-address lookup/write
  comparisons still matched. The separate wave diagnostic read
  `64,64,64,64` in four runs on the RX 6500 and passed on WARP
  10.0.26100.10068. These measurements leave the cause open.

The wave diagnostic is run explicitly, with the same dispatch sizes, resource
setup and readback as the direct probe. Run both from the build directory:

```powershell
.\offscreen_check.exe hardware --gpu-alias-lookup-writes
.\offscreen_check.exe hardware --gpu-alias-atomic-waterfall
.\offscreen_check.exe warp --gpu-alias-atomic-waterfall
```

An incorrect readback or setup failure fails the diagnostic. Exit 77 denotes
an adapter without the required SM 6.0 or WaveOps capability.

### XG swizzle selection against AddrLib

`tests/xg_swizzle_select_check.cpp` (CTest `xg_swizzle_select`) compares XG-style swizzle selection with AMD addrlib, the supported swizzle selector for gfx10.3. It concerns the XG helper only and is separate from the graphics configuration work in #874. It does not change `xg_xs.dll` binding, and it does not repeat the layout and address checks reported on #870.

The results are **AddrLib observations**. A match establishes the chosen PC reference behavior only. The Xbox-specific XG selection and its inputs still need separate evidence.

Public inputs:

- **XG caller sequence.** From [microsoft/DirectXTex `Auxiliary/DirectXTexXboxTile.cpp` at a8e5dbf](https://github.com/microsoft/DirectXTex/blob/a8e5dbf42909aeeb9fcf9c2ae399ae3c06737379/Auxiliary/DirectXTexXboxTile.cpp) (MIT). A caller with no specific mode asks XG with `XGComputeOptimalSwizzleMode(dimension, format, width, height, arraySize, sampleCount, bindFlags)` and stores the answer in `XG_TEXTURE2D_DESC::SwizzleMode`. DirectXTex does not select a mode itself.
- **Reference implementation.** AMD addrlib from [Mesa `src/amd/addrlib` at tag mesa-25.1.0, commit 5c142e46f3f6e752ed745fd48912ebb8fad67145](https://gitlab.freedesktop.org/mesa/mesa/-/tree/5c142e46f3f6e752ed745fd48912ebb8fad67145/src/amd/addrlib), SPDX-License-Identifier MIT, copyright Advanced Micro Devices. The GPUOpen PAL copy was not used, because its `addrtypes.h` has no 64-bit integer types for MSVC.
- **Create inputs.** These follow Mesa's `ac_addrlib_create`: `chipEngine` 0xD (`CIASICIDGFXENGINE_ARCTICISLAND`), `chipFamily` 0x8F (NV), `chipRevision` 0x28, `gbAddrConfig` 0x444, `createFlags` 0.
- **GB_ADDR_CONFIG.** 0x444 is Mesa's "navi21" surface-test configuration (`src/amd/common/ac_fake_hw_db.h` at the same tag): base 0x40 with `NUM_PIPES` 4 and `NUM_PKRS` 4.
- **Selection inputs.** These follow Mesa's `gfx9_get_preferred_swizzle_mode`: `ADDR_RSRC_TEX_2D`, `ADDR_RSRC_LOC_INVIS`, 256 B and variable blocks forbidden, one slice, one sample.

Not public: the console's `GB_ADDR_CONFIG`, XG's own selection rules and enum values, and the signature of the imported `XGSuggestSwizzleMode`.

Proposed desktop policy, in the test-only provider: every request sets `texture`. A render target adds `color`, a display target adds `color` and `display`, a depth target adds `depth`, and an unordered-access target adds `unordered`. A forced-linear request forbids every non-linear block.

| Request (one slice, one sample) | addrlib flags | Selected mode | Base alignment |
|---|---|---|---|
| 128x128 `8_8_8_8`, 8 mips, shader resource | 0x200 | `SW_64KB_R_X` | 65,536 |
| 1024x1024 `8_8_8_8`, 11 mips, shader resource | 0x200 | `SW_64KB_R_X` | 65,536 |
| 64x64 `8`, 1 mip, shader resource | 0x200 | `SW_4KB_D_X` | 4,096 |
| 16x16 `8_8_8_8`, 1 mip, shader resource | 0x200 | `SW_4KB_D_X` | 4,096 |
| 4096x1 `32`, 1 mip, shader resource | 0x200 | `SW_LINEAR` | 256 |
| 1920x1080 `8_8_8_8`, render target | 0x201 | `SW_64KB_R_X` | 65,536 |
| 1920x1080 `8_8_8_8`, display render target | 0x221 | `SW_64KB_R_X` | 65,536 |
| 1920x1080 `32`, depth target | 0x202 | `SW_64KB_Z_X` | 65,536 |
| 512x512 `32_32_32_32`, 10 mips, unordered access | 0x600 | `SW_64KB_R_X` | 65,536 |
| 2048x2048 `16`, 1 mip, unordered access | 0x600 | `SW_64KB_R_X` | 65,536 |

Every call returned `ADDR_OK`. The test requires each row to match exactly. A first run with placeholder expectations failed every row, so the comparison is not vacuous.

Controls:
- **Linear fallback.** A second provider sends each request to addrlib with only the linear block allowed. It goes through the same table comparison as the provider under test, and the comparison rejects it on all 9 tiled rows. The 8 tiled colour rows get `SW_LINEAR`, the depth target is refused, and it agrees only on the 4096x1 `SW_LINEAR` row.
- **Mutation.** `xg_swizzle_select_mutation` (`--mutate-linear-fallback`) substitutes that provider for the provider under test. The same comparison must find exactly the nine expected mismatches and agree on the linear row. An unrelated error fails the test. It skips with 77 without addrlib.
- **Forced linear.** With only the linear block allowed, the 9 colour requests select `SW_LINEAR`. The depth target, which has no linear mode, is refused with `ADDR_INVALIDPARAMS` rather than given a tiled mode.
- **Tiled request.** With the linear block forbidden, no request selects `SW_LINEAR`, including the 4096x1 row.
- **Explicit pitch.** A linear 100x64 `8_8_8_8` surface needs a pitch of 128 elements. An explicit pitch of 128 is accepted. A pitch of 129 (misaligned) and a pitch of 64 (tight) return `ADDR_INVALIDPARAMS`.
- **Mismatched input.** The requests in the table do not all select the same mode.

addrlib is an optional local dependency and is never vendored. `tools/fetch_addrlib.py` makes a shallow sparse fetch of `src/amd/addrlib` at the pinned commit into `out/deps/mesa`. A re-run checks the Mesa origin and refuses local changes before updating the checkout. Use an empty `--dest` to preserve existing work and prepare another pinned checkout. CMake builds it as a static library with the source list and definitions from Mesa's `meson.build` (`ADDR_FASTCALL=`, `LITTLEENDIAN_CPU`, `DEBUG=0`). The first configure detects it under `out/deps/mesa`. For an existing build that was configured without AddrLib, pass `XBOX2WINDOWS_ADDRLIB_DIR` explicitly to replace the cached empty path. Without it, the rest of the build is unchanged and `xg_swizzle_select` returns 77, a capability skip. CI therefore skips this test unless addrlib is present.

Measured on Windows 10 Pro 10.0.19045.6466 with MSVC 19.44.35207.1 x64, `dev` and `release`. No GPU is involved.

```powershell
python tools/fetch_addrlib.py
cmake --preset dev -DXBOX2WINDOWS_ADDRLIB_DIR=out/deps/mesa/src/amd/addrlib
cmake --build --preset dev --target xg_swizzle_select_check
ctest --preset dev --no-tests=error -R '^xg_swizzle_select' -V
```

## Offscreen rendering and readback

`offscreen_hardware` selects a high-performance adapter. `offscreen_warp` selects Microsoft's software rasterizer. Both draw the same DXC-compiled triangle to a 64×64 RGBA8 texture. After GPU completion the test reads the texture using its row pitch and compares colors and coverage pixel by pixel. Outputs are `triangle_hardware.bmp` and `triangle_warp.bmp` in the build directory. `ctest --preset dev -V` reports the adapter, driver, debug layer and pixel differences. Error or corruption messages from an available debug layer fail the test.

The original test machine had Windows Graphics Tools installed. A second build in `out/build/agility` used Microsoft Agility SDK 1.620.2. Set `-DOFFSCREEN_AGILITY_620_DIR=<directory containing the x64 SDK DLLs>` to reproduce it. This configuration verifies debug interface activation. [Agility SDK package](https://www.nuget.org/packages/Microsoft.Direct3D.D3D12/1.620.2)

### Desktop GPU addresses for xmem graphics allocations

`era_title_gpu` (`src/era_title_gpu.cpp`) is a process-wide registry that connects `xmem.dll` to the GPU address pieces above.

How it works:
- `xmem.dll` tracks every graphics allocation through it. Without a device this only registers the region with `era_query`, as before.
- `attach(device)` opens every tracked allocation as an `era_gpu_alias` heap and adds its range to an `era_title_va::Table`. Allocations tracked later are aliased at once.
- `translate` gives the desktop GPU address of a title span, and `locate` gives the buffer and offset behind it, for descriptors.
- `shader_view` returns the packed ranges and the buffers in slot order for `TitleLookup`.

Failures and lifecycle:
- An allocation that cannot be aliased stays tracked without a translation. `attach` reports the first such failure after trying every allocation.
- `detach` drops every alias and keeps tracking. Attaching a different device re-aliases everything.
- The registry does not wait for GPU work before an alias is released: releasing memory the GPU still uses is a title error on the console as well.
- The first `track` or `attach` creates the registry, and it is never destroyed, so unloading the module releases no D3D12 object. The other functions never create it; without it they behave as for an empty registry.
- These functions are `noexcept`, and `track` runs while `XMemAlloc` is being serviced. An allocation failure, including while creating the registry, returns `false` from `track` (`E_OUTOFMEMORY` from `attach`) instead of ending the process. It registers nothing, and the call can be retried.

The registry is private to the module that links it. `xmem.dll` keeps its four exports and its imports, and loading it does not load D3D12. The direct-linked tests cover attach and lookup within that module. A D3D12 client in another DLL cannot reach the registry: a shared registry, with a fixture that loads both DLLs and shows the client sees the real provider's allocations, is left for the first such client.

| CTest case | Inputs and checks |
|---|---|
| `gpu_alias_registry_warp`, `gpu_alias_registry_hardware` | Two memory-type-1 allocations and an interior block of a region placed below the first one (not an allocation base, so it cannot be aliased) are tracked before attach. `era_query` must report them GPU-intended, with nothing translated. `attach` must report the interior block's `E_INVALIDARG` and still alias both allocations. `locate` and `translate` must agree. A span past the end and the interior block must not be translated or located. A third allocation tracked after attach is aliased at once, and an unaliasable block tracked after attach stays tracked without a translation. Re-attaching the same device keeps existing aliases. The registry's shader view then drives `TitleLookup` on the GPU: 15 addresses, 12 hits, matching CPU reads through the title pointers. `untrack`, `detach` and a full re-attach are checked against the same translations. Duplicate, overlapping, null and empty input are rejected. |
| `xmem_shim_exports` | Now also requires that loading `xmem.dll` has not loaded `d3d12.dll`. |
| `era_title_gpu_alloc` | No GPU. The program replaces the global `operator new` so that one chosen allocation fails. Before the first `track`, `counts`, `translate`, `locate`, `shader_view`, `untrack` and `detach` must make no allocation. Then every allocation of a first `track` (registry creation included) and of a later one is failed in turn. Each failure must return `false`, leave nothing tracked, registered with `era_query` or translated, and the call is retried. The first `track` must have more allocations that can fail than the later one. An `abort()` is reported on stderr instead of the debug CRT's dialog, and the test has a 30 s timeout. |

Eight mutants of the registry each fail the test:
- never aliasing on `track`;
- stopping `attach` at the first failure;
- keeping the `era_query` registration after `untrack`;
- keeping aliases on `detach`;
- no bounds in `locate`;
- untracking on an alias failure;
- reversing the shader view's buffers;
- skipping the `era_query` registration.

Measured at commit `f847956`, Windows 11 build 26300, debug layer enabled:
- `gpu_alias_registry_warp` passed on WARP 10.0.26100.9818, with attach reporting `0x80070057` for the interior block, mismatches 0 and `debug_errors=0`. It passed 100 of 100 repeated runs.
- `gpu_alias_registry_hardware` passed on an NVIDIA GeForce RTX 3070 Laptop GPU (driver 32.0.16.1088), with the same results. It passed 200 of 200 repeated runs.
- The full suite passed: 148 tests, with 3 capability skips (`bvh_wave64_hardware`, `bvh_wave64_warp`, `wave_shuffle_warp`).
- The stop-at-first-failure mutant failed 20 of 20 runs on each adapter ("two of three aliased"). All eight mutants were first measured at `d52905d`, on the RTX 3070 with the debug layer unavailable.

Before `f847956`, the interior block's search tried only the 256 MB directly below the first allocation. That search found no free block in about 1 of 40 hardware runs, and the test failed in setup ("region below the first allocation").

Measured at commit `21a65e5`, Windows 11 build 26300, debug layer enabled:
- The full suite passed: 149 tests, with the same 3 capability skips.
- `era_title_gpu_alloc` handled 6 failing allocations in a first `track` and 2 in a later one. It passed 50 of 50 repeated runs.
- `gpu_alias_registry_hardware` passed 100 of 100 repeated runs on the RTX 3070, and `gpu_alias_registry_warp` passed 50 of 50.
- Four mutants fail `era_title_gpu_alloc`:
  - creating the registry with a throwing `new`: `abort()`, exit 3;
  - building `shader_view`'s empty result by construction, which allocates under the debug STL: `abort()`, exit 3;
  - a `counts` that creates the registry: exit 1;
  - a `track` that keeps the `era_query` registration after a failed insertion: exit 1.

### BVH buffer operations on in-box WARP

The five source-built buffer shaders compile at `cs_6_0`. The fixture compares
copy, fill, numbering, span copy and bounds reset against a CPU reference,
including sentinel words outside the written ranges. Adapters below SM 6.0
return the capability skip code 77. A mismatch in a dispatched fixture fails
the test.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -V -R '^bvh_buffer_ops_(hardware|warp)$'
```

On Windows 10 22H2 build 19045.6456, the GTX 1060 6 GB (32.0.15.6094) and
in-box WARP (10.0.19041.5794) each matched all 384 words in each of 26 cases.
The D3D12 debug layer reported zero errors and warnings.

### Windows 10 with the app-local Agility SDK and WARP

The Windows 10 22H2 in-box runtime caps an RTX 3060 at SM 6.5 and the in-box WARP (10.0.19041) at SM 6.2 with no DXR, so the `cs_6_5` BVH fixtures, the DXR checks and forced wave64 skip there. Without Graphics Tools, the debug layer reports `DXGI_ERROR_SDK_COMPONENT_MISSING` even when the SDK layers are app-local. The Agility build therefore continues with `debug_layer=unavailable` for that one result, and other debug-layer failures still stop the test. Capabilities reported on Windows 10 19045.6466:

| Runtime | RTX 3060 (32.0.15.9636) | WARP |
|---|---|---|
| In-box | SM 6.5, DXR tier 1.2, 32 lanes | 10.0.19041: SM 6.2, no DXR, 4 lanes |
| Agility SDK 1.620.2 | SM 6.8, DXR tier 1.2, 32 lanes | 10.0.19041: SM 6.2, no DXR, 4 lanes |
| Agility SDK 1.620.2 and WARP 1.0.22 | SM 6.8, DXR tier 1.2, 32 lanes | 1.0.22: SM 6.8, DXR tier 1.2, 4 to 128 lanes |

Extract the [Agility SDK](https://www.nuget.org/packages/Microsoft.Direct3D.D3D12/1.620.2) and [WARP](https://www.nuget.org/packages/Microsoft.Direct3D.WARP/1.0.22) packages, then configure with both directories. The Agility directory holds `D3D12Core.dll`, `d3d12SDKLayers.dll` and the package renamed to `sdk.nupkg`. The WARP directory is the package's `build/native/bin/x64`:

```powershell
cmake --preset dev -B out/build/w10 -DOFFSCREEN_AGILITY_620_DIR=<agility dir> -DOFFSCREEN_WARP_DIR=<warp dir>
cmake --build out/build/w10
ctest --test-dir out/build/w10 --no-tests=error
```

`OFFSCREEN_WARP_DIR` requires `OFFSCREEN_AGILITY_620_DIR`. On the in-box runtime, WARP 1.0.22 reports DXR tier 1.2, but `dxr_query_warp`, `dxr_update_warp` and `bvh_differential_warp` fail with every ray reported as a miss. With the Agility runtime the same WARP passes them. Configuring the same build directory again without `OFFSCREEN_WARP_DIR` deletes the copied `d3d10warp.dll`, so `offscreen_check` goes back to the in-box WARP. `app_local_warp_toggle` checks this with a stand-in DLL.

In this configuration all `bvh_*_warp`, `dxr_*_warp` and `bvh_differential_warp` tests pass, and the hardware wave64 check skips at its 32-lane range with SM 6.6 available. `bvh_wave64_warp` now runs and fails at field 25, `WaveReadLaneAt(v, (lane*7+3) % 64)`. In two groups, all 128 lanes received the value of lane m where `7m+3 ≡ lane (mod 64)`, which is the inverse of the requested shuffle. Field 24's shuffle, the mirror read and the native-width run pass. At WARP's native width of 8, `7n+3 ≡ 3-n (mod 8)` is self-inverse, so the default `bvh_waves_warp` cannot show the difference. `wave_shuffle_warp` narrows the cause. WARP 1.0.22 runs a forced wave as blocks of its native 8 lanes, and it picks the source block using the request of the wrong lane: the lane of the reader's block that sits at the requested position. For this shuffle that equals the inverse permutation. A rotation by one lane gets 16 of 64 reads wrong at widths 16, 32 and 64, and none of them match its inverse. Reads where every lane of a block requests the same source block are unaffected. [Open question](https://github.com/R0Y00/Xbox2Windows/issues/145)

On Windows 11 build 26200 at `1ccc326`, the in-box WARP (10.0.26100.9818) reports a 4..4 lane range with SM 6.6, so `bvh_wave64_warp` and the forced 16/32/64 variants of `wave_shuffle_warp` skip on capability; its native-width shuffle reads are all correct (affine and rotation 64/64, no block-misrouted reads, no control errors). The GTX 1660 SUPER (driver 32.0.16.1074) reports 32..32 lanes: `bvh_wave64_hardware` skips, and `wave_shuffle_hardware` passes at native and forced width 32 with every read from the requested lane. The lane-block misroute was observed in the app-local WARP 1.0.22 + Agility SDK setup above. The in-box WARP run could exercise only native width 4, because the fixture has no override for the reported lane range. [Cross-check report](https://github.com/R0Y00/Xbox2Windows/issues/145#issuecomment-7114625084)

## Mapped DLL draw with a Windows reference

`cpu_native_frame_hardware` and `cpu_native_frame_warp` load the same source-built DLL with `LoadLibraryW` and with `pe_map`. Both paths record TLS before the DLL entry on process attach and detach. The DLL's `guest_draw` export selects the shared two-texture shader fixture and calls the graphics provider through its bound import. The provider calls the host test, which runs the existing offscreen renderer in a child process. The renderer checks every RGBA channel against the CPU reference, and the test compares the two 64×64 captures byte for byte. Withholding the graphics provider must fail mapping before startup or drawing.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_frame_(hardware|warp)$' -V
```

On Windows 11 build 26200, the RTX 3060 Ti (driver 621.14) and WARP each matched all 4,096 pixels per load path with zero D3D12 debug warnings or errors.

## Static executable intake

By default, the intake tool locates the Scarlett entry in the sole build's `MicrosoftGame.Config`. It accepts the layout path or a separate candidate file and opens the game files for reading only.

```powershell
.\out\pe-env\Scripts\python.exe -B tools/intake_exe.py
# Inspect a separately supplied candidate.
.\out\pe-env\Scripts\python.exe -B tools/intake_exe.py "E:\incoming\game_scarlett_master.exe"
```

Each run writes a new JSON report in `out/intake/`. Exit codes are 3 for a missing layout executable, 2 for an input or inspection failure and 0 for completed inspection. The report links the previous verification time, torrent SHA-256, install chunks and baseline summary. The inspected torrent omitted the EXE. The 13,345 matching pieces and 223,880,932,870 bytes verified in the historical report cover that original payload. A later EXE needs separate provenance. Intake rescans the package PEs and lists changed DLLs.

For each candidate the tool records SHA-256, architecture, entry RVA and section, TLS directory and callback order, then compares imports, modules and unresolved items with the baseline. Source-built PE fixtures and a miniature torrent cover named, ordinal and delayed imports, import names that pefile rejects, missing providers, TLS callbacks, invalid callback addresses, truncation, an absent entry and path conflicts. These checks establish static properties. Authenticity and startup remain unverified.

`pe.startup.locations` lists TLS callbacks in table order followed by the executable entry. Each location includes its RVA, section and file offset. When the x64 exception directory supplies a covering function interval, the report includes its inclusive start, exclusive end and unwind data RVA. A location can legitimately lack a covering record. An absent or unusable table leaves the independently observed startup locations available. In the local debug fixture the table had 110 records, one covering its TLS callback. The entry had no covering record. CTest cross-checks against pefile and exercises both absent and incomplete directories. [Microsoft x64 unwind data](https://learn.microsoft.com/en-us/cpp/build/exception-handling-x64)

### Frame plane requests in BinkAllocateFrameBuffers

One iteration of the exported frame-buffer allocator issues up to five requests to the [group descriptor append helper](#grouped-allocation-descriptor-builders). Slots 0, 3 and 4 use the first pitch and height. Slots 1 and 2 use the second pair. With pitches 96 and 32 and heights 7 and 3, enabling all five produced sizes 672, 96, 96, 672 and 672 in that order. Each call received the address of the selected plane's destination field, and its pitch field was written after the call.

Build `cpu_bink_descriptors_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_bink_frame_planes$'`. Setting `BINK_DESCRIPTOR_IMAGE` also enables the `cpu_bink_package_frame_planes` test after building `cpu_bink_package_descriptors`. That comparison translates the loop body from RVA `0x2257` up to `0x2325` in the fingerprinted image. It supplies a frame index, the matching cursor and two preselected pitches. A desktop observer records the arguments at the five append sites. Pitch selection, the outer loop and backing storage allocation are outside this check.

### Frame buffers through the shared Windows heap

The integrated package replay enters the allocator with a context, frame table and requested alignment. It translates the function body, including format-based pitch selection, frame count adjustment, the five plane sites, the group allocation and the owner and accounting writes. The Windows compiler provides the stack prologue and epilogue. The group and heap functions are the same translations used in their individual tests. The resulting DLL imports `XMemAlloc` and `XMemFree` by name from the shared desktop `xmem.dll`.

The public control combines a source-written outer function with the translated MASM plane loop, descriptor helpers and grouped heap caller. Both builds run 36 cases, including all plane-enable masks on one frame, empty requests, two-frame allocation and the three-to-two count adjustment. Each enabled plane is filled independently and the entire backing block is compared, including padding. A second link of the same objects records the import arguments and forwards to the shared DLL, or injects one null allocation. On failure the caller returned zero, kept the owner null and destination slots unchanged for requested planes, and retained the pitch and accounting updates. Retrying the same context succeeded. The normal release returned the original heap base once with the allocation attributes.

Build the dev preset and run `ctest --preset dev --no-tests=error -R 'frame_to_heap$' -V`. For the local package case, configure the same fingerprinted DLL for `BINK_DESCRIPTOR_IMAGE` and `BINK_HEAP_IMAGE` and the grouped and heap entries documented below. The scenarios supply context and frame metadata; video stream decoding is outside this test.

### Default allocation imports

The desktop shim exports `XMemAllocDefault(size, attributes)` and `XMemFreeDefault(pointer, attributes)` through its existing unhooked allocator. The arguments use the same x64 widths as the normal allocation pair. The original four exports retain their ordinals. Both named allocation pairs run the same source-built checks for zero-filled heap and graphics memory, alignment, matching release, null release and rejected attribute words on Windows. Run `ctest --preset release --no-tests=error -R '^xmem_shim_(exports|allocation|default_allocation)$'`. [Public ABI reference](https://github.com/XWine1/SlimEra/blob/397807a9563f62b56cf7aaea9809b6fa635ba651/era.h)

### Physical-page allocation imports

Refs #880 and #870. With the default allocation pair bound, the startup mapping probe stopped at `XMemAllocatePhysicalPages`. The desktop `xmem.dll` now exports it with `XMemFreePhysicalPages`. Mapping (`XMemMapPhysicalPages`), scatter mapping and transfer are not modeled.

**Source-built comparison.** `cpu_native_xmem_physical_pair` builds `tests/cpu/xmem_physical_pair.asm` into three x64 EXEs. Each imports the pair by name from `xmem.dll` and `finish_exe` from `cpu_exe_io.dll`, and starts through `exe_startup.asm`. The comparison follows triggered02's independent fixture posted on #880 (Windows loader vs `pe_map`, a canary-filled page array, a missing-provider control). It adds a nonzero success code and mutation variants. The body returns `0x880` when its 10 steps pass, or `0xF00` plus the first failing step:

1. `XMemAllocatePhysicalPages(MEM_64K_PAGES, &count = 2, pages)` returns `TRUE`.
2. The count is still 2.
3. The two page numbers are nonzero and distinct, and the canary after them is untouched.
4. Count 1 for the two-page grant is refused.
5. The grant still releases with count 2.
6. The same release again (double free) is refused.
7. `MEM_LARGE_PAGES` alone is refused.
8. That refusal sets the count to 0.
9. That refusal leaves all three canaries untouched.
10. `XMemFreePhysicalPages(1, NULL)` is refused.

Steps 4 and 7–9 check proposed desktop policy (see the table below). The test stages each EXE with the two DLLs, launches it normally, then maps it in a child process and starts it with `pe_map_run`. Both exit codes must match the expected value, and the mapping must request exactly the three imports.

| Variant | Windows launch | `pe_map` |
|---|---|---|
| `original` | `0x880` | `0x880` |
| `full_count` (count 2 at step 4, so the grant is released there) | `0xf04` | `0xf04` |
| `skip_release` (step 5 omitted, so step 6 releases the live grant) | `0xf06` | `0xf06` |

A provider that binds only one of the pair stops the mapping before the entry point. The diagnostic is `unresolved import xmem.dll!XMemAllocatePhysicalPages` or `unresolved import xmem.dll!XMemFreePhysicalPages`.

| CTest case | Inputs and checks |
|---|---|
| `era_physical_controls` | Links `era_physical.cpp` twice, once as built into `xmem.dll` and once with the test-only `ERA_PHYSICAL_LENIENT_RELEASE`, whose release clears any listed page as SlimEra's `FreeTitlePhysicalPages` does. Seven ownership and page-count controls each run from a clean registry with live grants of 4, 3 and 1 pages: short count, long count, a single page of a grant, double free, never-issued page, forged mix and duplicate entry. The policy must refuse each one with the granted total unchanged, and the lenient build must accept each one. That shows every control detects the faulty handling. |
| `xmem_shim_physical_pages` | Through the named imports: grants of 4, 3 and 1 pages keep their counts and return unique nonzero page numbers in range, with the slot after them untouched. 14 refused releases each return `ERROR_INVALID_PARAMETER` and leave the array unchanged: short, long and single-page counts, a never-issued number, 0, a number past the capacity, a forged mix, a duplicate, a zero count, a null array and an overflowing count, then three double frees. The grants still release afterwards, one reversed and two in one call. 10 refused allocations each leave a count of 0 and the array untouched: a null count, five bad flag words, a zero count, a null array, and counts of 2^18 + 1 and all-ones. Exhaustion with 2^18 - 1 pages granted refuses 2 pages, grants 1, then refuses 1. After cleanup the full 2^18 pages can be granted again. |
| `era_physical_lifetime` | Exact granted totals (5, then 7, then 0 after one release of both grants in mixed order). Exhaustion leaves the count at 0, the array untouched and the total unchanged. Cleanup returns the total to 0. |
| `xmem_shim_exports` | Also resolves the two new names. |

**Public call evidence.** Microsoft's Xbox GDK samples (MIT, commit `b1e77b6`) establish the 64 KiB allocate/free pairing, the in/out count with a page array, and the Boolean success check:

- [`AdvancedESRAM/PageAllocator.cpp`](https://github.com/microsoft/Xbox-GDK-Samples/blob/b1e77b6ac10f95b07296025fda171ca2ddc42dbe/Samples/Graphics/AdvancedESRAM/PageAllocator.cpp#L193-L232) passes `MEM_64K_PAGES`, a `ULONG_PTR` count by address and a `PULONG_PTR` page array, and tests the `BOOL` result. It later releases the block with `XMemFreePhysicalPages(count, pages)` using the same count and array slice.
- [`AMDFidelityFX_FSR3FrameInterpolation/FSR3FrameInterpolation.cpp`](https://github.com/microsoft/Xbox-GDK-Samples/blob/b1e77b6ac10f95b07296025fda171ca2ddc42dbe/Samples/Graphics/AMDFidelityFX_FSR3FrameInterpolation/FSR3FrameInterpolation.cpp#L146-L167) casts the flag to `ULONG_PTR` (`XMEM_2MB_CLUSTERS` for 2 MiB pages, `MEM_64K_PAGES` for 64 KiB pages) and passes an in/out count. It asserts that the count comes back unchanged and that both calls return a nonzero `BOOL`.
- The [GDK DirectStorage overview](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/console/storage/directstorage/directstorage-overview?view=gdk-2604) describes arrays of 64 KiB physical pages from this path as a request destination.

The samples do not settle what happens to the count and array on failure, or whether a partial release is permitted. [SlimEra `kernelx.cpp`](https://github.com/XWine1/SlimEra/blob/397807a9563f62b56cf7aaea9809b6fa635ba651/kernelx.cpp) (MIT) implements the kernelx-level `AllocateTitlePhysicalPages`/`FreeTitlePhysicalPages`, which have the same count and array shape plus a process handle. It returns 64 KiB page indices and can return fewer pages than requested. It sets the count to 0 for bad flags, and its release clears whatever indices it is given. It is a reimplementation, not console evidence.

**Desktop contract** (`src/era_physical.cpp`, exported from `src/xmem_shim.cpp`):

| Item | Desktop behavior | Basis |
|---|---|---|
| `XMemAllocatePhysicalPages(ULONG_PTR flags, PULONG_PTR count, PULONG_PTR pages)` | `rcx`, `rdx` and `r8` as declared. Returns `BOOL`. | Samples. A full 64-bit flag argument is inferred from the `ULONG_PTR` cast. |
| Page-count unit | 64 KiB pages for `MEM_64K_PAGES`. | Samples |
| Flags | Only `MEM_64K_PAGES` (`0x20400000`). Any other bit, including the upper 32, is refused. `XMEM_2MB_CLUSTERS` is refused because its value and array layout are not public. | Proposed desktop policy |
| Valid allocation | Returns `TRUE` and `ERROR_SUCCESS`. Writes `count` opaque, nonzero page numbers, unique among live pages, and leaves the count unchanged. All or nothing. Nothing is committed: the numbers are bookkeeping until mapping is modeled. | Count unchanged: samples. The rest is proposed desktop policy. |
| Failure | Returns `FALSE` and sets `*count = 0`. The array is left untouched. A zero count, a null count or array, bad flags, or a count above the desktop capacity of 2^18 pages (16 GiB) gives `ERROR_INVALID_PARAMETER`. Too few free pages gives `ERROR_NOT_ENOUGH_MEMORY`. | Proposed desktop policy. The capacity matches SlimEra's 16 GiB. |
| `XMemFreePhysicalPages(ULONG_PTR count, PULONG_PTR pages)` | Returns `BOOL`. Releasing the same count and array that were allocated succeeds. | Samples |
| Release beyond that | Several whole grants may be released in one call, in any order. A zero count, duplicate entry, unallocated number or incomplete grant returns `FALSE` with `ERROR_INVALID_PARAMETER` and changes nothing. | Proposed desktop policy |

The caller supplies a valid count and a separate array large enough for the
request. Release reads a valid array that stays unchanged during the call. Page
numbers refer to current grants and may be reused. A caller must discard its
numbers after release. An immediate repeat while the numbers are free is refused.

Open questions for #870:

- **Flag width.** Is the flag argument `ULONG_PTR` or `DWORD` on the console? The desktop refuses upper bits, so a caller that leaves garbage in the upper half of `rcx` would be refused.
- **2 MiB clusters.** What is the value of `XMEM_2MB_CLUSTERS`, and are the count and array then in 2 MiB or 64 KiB units?
- **State on failure.** Does the console leave the count and array alone on failure? Can it grant fewer pages than requested?
- **Release rules.** What do console page numbers encode? Does `XMemFreePhysicalPages` accept a subset of a grant?
- **Limits and errors.** What error codes does the console return, and what is the title's physical-page budget?

```powershell
cmake --build --preset dev --target xmem_shim_check era_physical_check pe_map cpu_exe_io cpu_xmem_physical_original cpu_xmem_physical_full_count cpu_xmem_physical_skip_release
ctest --preset dev --no-tests=error -R '^(xmem_shim_(exports|physical_pages)|era_physical_(controls|lifetime)|cpu_native_xmem_physical_pair)$' -V
```

### Shared physical pages on Windows

The desktop XMem shim can map live 64 KiB physical grants into caller-owned, 64 KiB Windows placeholders. Contiguous and scatter mappings share section backing by page number. Unmapping restores the placeholders and retains the bytes for later remapping. Releasing an entire grant remains subject to the allocation registry and is refused while any of its views is live. The caller must keep the placeholders alive and use the shim to unmap views. This desktop profile requires Windows placeholder reservations supplied by the caller. Console reservation flags and system-partition transfers remain open.

`xmem_shim_mapped_pages` checks the shared values at two addresses, scatter order, readback after remapping, refusal of release while mapped, and rollback if a later destination has no placeholder.

The reserve-only desktop cases from [#888](https://github.com/R0Y00/Xbox2Windows/pull/888) use 16 or 24 GiB Windows placeholder ranges. The page mapper splits out a 64 KiB destination on demand and coalesces the reservation after its last view is unmapped. The same test writes and reads one interior page after remapping, releases the entire reservation and reserves the same range again. Other allocation modes remain outside this profile.

`windows_placeholder_partition` tests the Windows APIs directly. A single `VirtualFree` call with `MEM_RELEASE | MEM_PRESERVE_PLACEHOLDER` isolates an interior allocation unit. `VirtualQuery` observes both neighbors and the middle as three independent reservations. An aligned request crossing two reservations fails and preserves all three. A pagefile-backed view replaces only the middle, and unmapping restores its placeholder. Releasing the middle and then the left preserves their remaining neighbors. [Windows placeholder flags](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualfree)

Run `ctest --preset dev --no-tests=error -R '^windows_placeholder_partition$' -V`.

### Aligned committed CPU memory

`xmem_committed_memory` reserves three ordinary host pages at each guest base alignment (64 KiB and
2 MiB). A request for one host page plus five bytes commits exactly two host pages and leaves the
third reserved. The test checks every committed byte for zero-fill, writes a pattern across the
page boundary and at the last rounded byte, and verifies the readback and release with
`VirtualQuery` and CPU reads.

The test-only profile accepts `PAGE_READWRITE`. A rounding overflow, a `PAGE_READONLY` request, and a
commit one page beyond the reservation leave the original data and reserved tail intact. The last
case preserves the Win32 allocator's `ERROR_INVALID_ADDRESS`. This is a CPU backing fixture for
[#870](https://github.com/R0Y00/Xbox2Windows/issues/870). The desktop XMem shim and GPU address
registration are outside its scope. [Windows address requirements](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-mem_address_requirements) · [Win32 commit behavior](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualalloc)

```powershell
cmake --build --preset dev --target xmem_committed_memory_check
ctest --preset dev --no-tests=error -R '^xmem_committed_memory$' -V
```

### Second commit and protection comparison

`xmem_commit_protection` ([`tests/xmem_commit_protection_check.cpp`](../tests/xmem_commit_protection_check.cpp)) is a source-built x64 check for [#870](https://github.com/R0Y00/Xbox2Windows/issues/870) using synthetic 64 KiB page patterns. Its scope is separate from the first-commit alignment/zero-fill/rounding check (`xmem_committed_memory`), the reserve-only profile in [#888](https://github.com/R0Y00/Xbox2Windows/pull/888), and the device/queue work in [#874](https://github.com/R0Y00/Xbox2Windows/issues/874).

The fixture compares two proposed desktop policies on Windows 10 Pro (build 19045, MSVC VS2022 x64, `dev` and `release` presets):

- **Ordinary committed pages (`MEM_PRIVATE` via `VirtualAlloc`)**: CPU read/write and `VirtualProtect` transitions (`PAGE_READWRITE` -> `PAGE_READONLY` -> `PAGE_NOACCESS` -> `PAGE_READWRITE`) work across 64 KiB pages. Negative controls verify that writes to `PAGE_READONLY` and reads/writes to `PAGE_NOACCESS` raise `EXCEPTION_ACCESS_VIOLATION` via SEH while leaving stored bytes unchanged. Invalid protection flags (`0`, `PAGE_READWRITE | PAGE_READONLY`, error `87`), cross-boundary protection into an uncommitted page (error `487`), and over-committing past the reservation boundary (error `487`) all fail while leaving the committed page's bytes and protection intact. `VirtualAlloc(..., MEM_COMMIT, PAGE_READWRITE)` is idempotent, and `VirtualFree(..., MEM_DECOMMIT)` returns the page to `MEM_RESERVE`.
- **Live shared-page view (`MEM_MAPPED` via `xmem.dll` `XMemAllocatePhysicalPages` and `XMemMapPhysicalPages` / `XMemMapPhysicalPagesScatter`)**: two aliased views of one 64 KiB physical grant share bytes live, with per-view `VirtualProtect` isolation. Protecting view 1 to `PAGE_READONLY` blocks writes through view 1 (`EXCEPTION_ACCESS_VIOLATION`) while view 2 remains `PAGE_READWRITE` and writes live updates visible through view 1; protecting view 1 to `PAGE_NOACCESS` faults reads and writes on view 1 while view 2 stays accessible. Failed reprotection (error `87` for `0`, error `487` across an unmapped placeholder), refused `XMemVirtualAlloc` second-commit combinations (`ERROR_INVALID_PARAMETER`, `87`), refused remap over a live view (`87`), and refused `XMemFreePhysicalPages` while mapped (`87`) all leave both live views, their bytes, and their protections intact. Bytes survive unmapping both views and remapping the grant.
- **First observable discrepancy**: ordinary committed pages report `MEM_PRIVATE` (`0x20000`), accept `VirtualFree(..., MEM_DECOMMIT)`, and cannot alias a second VA; live shared-page views report `MEM_MAPPED` (`0x40000`), refuse `VirtualFree(..., MEM_DECOMMIT)` with `ERROR_INVALID_PARAMETER` (`87`) without disturbing the live view, support live VA aliasing with independent per-view protection, and preserve bytes across unmap/remap. Whether a console caller requires a live shared-page view or ordinary committed pages for a given second-commit site remains a console-side question.

```powershell
cmake --build --preset dev --target xmem_commit_protection_check
ctest --preset dev --no-tests=error -R '^xmem_commit_protection$' -V
cmake --build --preset release --target xmem_commit_protection_check
ctest --preset release --no-tests=error -R '^xmem_commit_protection$' -V
```

### Tools-memory availability fixture

The source-built caller ([`tools_mem_caller.asm`](../tests/cpu/tools_mem_caller.asm)) imports `XMemIsToolsMemAvailable` by name. It queries twice and checks the recorded count before choosing a path. The controlled recording provider ([`tools_mem_provider.cpp`](../tests/cpu/tools_mem_provider.cpp)) returns FALSE for the fallback path (exit 42) or TRUE for the tools-memory path (exit 84). The same EXE exits with a live test worker under the Windows loader and returns its result to the host under `pe_map`. Withholding the query export stops mapping at import resolution before entry, with the trace provider still available.

The paired outcomes describe the synthetic caller. The desktop `xmem.dll` has no devkit tools-memory partition, so its named query reports FALSE. `xmem_shim_tools_memory` checks successive queries against the desktop shim. Actual console availability and the runtime behavior of individual callers remain separate questions. [Independent recording fixture and results](https://github.com/R0Y00/Xbox2Windows/pull/900)

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_tools_mem$' -V
ctest --preset release --no-tests=error -R '^cpu_native_tools_mem$' -V
```

### Physical-page mapping caller and recorder

This fixture exercises proposed contiguous and scatter call shapes. The names and signatures in public reimplementations are independent reference material. [SlimEra declaration](https://github.com/XWine1/SlimEra/blob/397807a9563f62b56cf7aaea9809b6fa635ba651/era.h)

A source-built caller ([`map_pages_caller.asm`](../tests/cpu/map_pages_caller.asm)) imports both functions from `xmem.dll` by name and `finish_exe` from `cpu_exe_io.dll`, starting through [`exe_startup.asm`](../tests/cpu/exe_startup.asm). Under a controlled recording provider ([`map_pages_provider.cpp`](../tests/cpu/map_pages_provider.cpp)), it exercises ten checks:
- **Step 1**: Contiguous mapping to a 64 KiB aligned virtual address, returning the base address.
- **Step 2**: Contiguous unmapping (`NULL` page array), returning the base address.
- **Step 3**: Scatter mapping across an array of 64 KiB aligned virtual addresses, returning `TRUE` (`1`).
- **Step 4**: Scatter unmapping (`NULL` page array), returning `TRUE` (`1`).
- **Step 5–7**: Negative controls for unaligned virtual address, null virtual address, and null scatter array, each returning `0`.
- **Step 8**: Changed-argument control with wrong page IDs (`[999, 888]`), returning `NULL`.
- **Step 9**: Changed-argument control with swapped scatter destinations (`[0x10010000, 0x10000000]`), returning `0`.
- **Step 10**: Internal verification of provider call counters and mismatch counts directly within the caller process.

The specific fixture inputs tested (`VirtualAddress = 0x10000000`, `cPages = 2`, `rgPages = [1, 2]`, and scatter destinations `[0x10000000, 0x10010000]`) are synthetic fixture inputs chosen for this test. The provider's recorded trace validates the expected count, page IDs and ordered scatter destinations.

On success, the caller exits with the success sentinel `0x870`. With `CPU_CRC_TEST_LIVE_WORKER=1`, a live worker thread outlives the body under the Windows loader, and `finish_exe` terminates the process with code `0x870`. Under `pe_map`, `finish_exe` preserves the host return. The two loaders consuming the controlled provider verify caller contracts and error propagation; they do not establish console return types, that `NULL` unmaps, that 64 KiB alignment is required, or that virtual address reservations survive unmapping. These remain proposed desktop policies and hypotheses under investigation. Production XMem behavior and physical page registry tracking remain separate (#892).

Two independent negative controls confirm that withholding either `XMemMapPhysicalPages` or `XMemMapPhysicalPagesScatter` stops `pe_map` at import resolution before entry with `STATUS["unresolved"]` naming the missing export. Run:

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_map_pages$' -V
ctest --preset release --no-tests=error -R '^cpu_native_map_pages$' -V
```

### Memory transfer caller and recorder

This fixture exercises the proposed `XMemTransferMemory` contract under test-provider policies. The source-built caller ([`transfer_mem_caller.asm`](../tests/cpu/transfer_mem_caller.asm)) imports `XMemTransferMemory` by name from `xmem.dll` and `finish_exe` from `cpu_exe_io.dll`, starting through [`exe_startup.asm`](../tests/cpu/exe_startup.asm). Under a controlled recording provider ([`transfer_mem_provider.cpp`](../tests/cpu/transfer_mem_provider.cpp)), it exercises seven checks:
- **Step 1**: Valid 2 MiB transfer under test-provider policy partition 1 (`rcx = 1`, `rdx = 0x200000`), returning `TRUE` (`1`).
- **Step 2**: Valid 16 MiB transfer under test-provider policy partition 1 (`rcx = 1`, `rdx = 0x1000000`), returning `TRUE` (`1`).
- **Step 3**: Rejection control with invalid partition (`rcx = 0`), returning `FALSE` (`0`).
- **Step 4**: Rejection control with zero byte count (`rdx = 0`), returning `FALSE` (`0`).
- **Step 5**: Rejection control with unaligned byte count (not 2 MiB aligned under test-provider policy, `rdx = 0x200100`), returning `FALSE` (`0`).
- **Step 6**: Rejection control with oversized byte count exceeding the 16 MiB policy maximum (`rdx = 0x2000000`), returning `FALSE` (`0`).
- **Step 7**: Internal verification of provider call counters, rejection counts, and the ordered trace of calls directly within the caller process.

The specific fixture inputs tested (`partition = 1`, `byte_count = 0x200000` and `0x1000000`) are synthetic fixture inputs chosen for this test. Partition selector 1, 2 MiB alignment, the 16 MiB maximum boundary, and `BOOL` return semantics are labeled as test-provider policy throughout; native console transfer semantics and partition effects remain open. The provider's recorded trace validates all six ordered argument pairs and call outcomes.

A changed-first-call control patches the first 2 MiB request in the caller binary to 4 MiB: under both the Windows native loader and `pe_map`, the caller's internal trace check rejects the modification with exit code `0xF0B`, and the recorded trace reflects the changed 4 MiB argument in call 0.

On success, the caller exits with the success sentinel `0x870`. With `CPU_CRC_TEST_LIVE_WORKER=1`, a live worker thread outlives the body under the Windows loader, and `finish_exe` terminates the process with code `0x870`. Under `pe_map`, `finish_exe` preserves the host return. An independent negative control confirms that withholding `XMemTransferMemory` stops `pe_map` at import resolution before entry with `STATUS["unresolved"]` naming the missing export. Run:

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_transfer_mem$' -V
ctest --preset release --no-tests=error -R '^cpu_native_transfer_mem$' -V
```

### Memory transfer prerequisite and failure chain

This fixture exercises the sequence where a caller checks tools-memory availability, queries working-set statistics, evaluates available memory budget, and transfers memory under partition 1. The source-built caller ([`transfer_chain_caller.asm`](../tests/cpu/transfer_chain_caller.asm)) imports `XMemIsToolsMemAvailable`, `XMemGetWorkingSetStatistics`, and `XMemTransferMemory` by name from `xmem.dll` and `finish_exe` from `cpu_exe_io.dll`, starting through [`exe_startup.asm`](../tests/cpu/exe_startup.asm). Under a controlled test provider ([`transfer_chain_provider.cpp`](../tests/cpu/transfer_chain_provider.cpp)), it verifies the prerequisite and failure isolation paths:

- **Case 1 (Success reaches transfer)**: Tools-memory query returns `TRUE`, `XMemGetWorkingSetStatistics` with selector `0x2000000A` reports total and used budget (32 MiB total, 16 MiB used; difference 16 MiB), and the recorder accepts a 16 MiB request under test-provider policy partition 1. Returns milestone exit code `0x870`.
- **Case 2 (Tools-memory unavailable)**: When tools-memory query returns `FALSE` (the no-tools desktop policy established in #896), the sequence is blocked immediately. `XMemTransferMemory` is never called (`transfer_calls == 0`), returning exit code `0x101`.
- **Case 3 (Working-set statistics failure)**: When `XMemGetWorkingSetStatistics` returns `FALSE` or fails, the sequence is aborted immediately. `XMemTransferMemory` is never called (`transfer_calls == 0`), returning exit code `0x102`.
- **Case 4 (Zero available budget)**: When working-set statistics reports zero available budget (`total <= used`), transfer is skipped. `XMemTransferMemory` is never called (`transfer_calls == 0`), returning exit code `0x103`.
- **Case 5 (Transfer failure isolation)**: The provider records one rejected 16 MiB request and the caller returns exit code `0x104`. The recording provider has no backing-page or ownership state.

The specific fixture inputs tested (`selector = 0x2000000A`, `partition = 1`, 2 MiB alignment rounding, 16 MiB clamp) are synthetic fixture inputs chosen for this test. Partition selector 1, alignment, and maximum bounds are test-provider policy behaviors; native console transfer semantics remain open.

A changed-selector patch control alters `0x2000000A` to `0x2000000B` in the caller binary: under both the Windows native loader and `pe_map`, the provider rejects the unrecognized selector, the query fails, and the caller exits with `0x102` with transfer uncalled.

On success, the caller exits with `0x870`. With `CPU_CRC_TEST_LIVE_WORKER=1`, a live worker thread outlives the body under the Windows loader, and `finish_exe` terminates the process cleanly with code `0x870`. Under `pe_map`, `finish_exe` preserves the host return across all cases. Independent negative controls confirm that withholding any of `XMemIsToolsMemAvailable`, `XMemGetWorkingSetStatistics`, or `XMemTransferMemory` stops `pe_map` at import resolution before entry with `STATUS["unresolved"]` naming the missing export. Run:

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_transfer_chain$' -V
ctest --preset release --no-tests=error -R '^cpu_native_transfer_chain$' -V
```

### Audio-side import inventory

Binding probes on #870 leave 19 audio-side imports unresolved: 5 from `chad.dll`, 5 from `xapu.dll` and 9 from `acphal.dll`. None of these modules exists in System32. This inventory records the public evidence for each name. Microsoft's reference pages for these APIs sit behind the NDA developer program, so they give no public signature. Microsoft's MIT-licensed [GDK Learn index at `f8c3438`](https://github.com/microsoft/XBOX-GDK-dotnet/blob/f8c3438aa366fbbb662d8a5e724021a9d0b65f5d/eng/gdk-learn-index.json) confirms that the `XDsp*` and `XApu*` functions are documented APIs and gives each one a page path. It lists AcpHal and the Audio Processing Unit only as member pages. Public MIT call sites in [DirectXTK12 at `f3555e3`](https://github.com/microsoft/DirectXTK12/tree/f3555e3014a53c81a6202b786dc4d6509b95a936) cover `ApuAlloc` and `ApuFree`. Several unaffiliated reimplementations also export these names. Their declarations do not trace to a public Microsoft source, so this inventory does not use them.

| Module | Import | Public evidence | Desktop replacement candidate | Status |
|---|---|---|---|---|
| `chad.dll` | `XDspActivate` | Learn index entry only | None known. XAudio2 and WASAPI mix and play audio but do not provide this stream queue. | needs evidence |
| `chad.dll` | `XDspConnectWithMaximumStreamLimit` | Learn index entry only | None known | needs evidence |
| `chad.dll` | `XDspDeactivate` | Learn index entry only | None known | needs evidence |
| `chad.dll` | `XDspDisconnect` | Learn index entry only | None known | needs evidence |
| `chad.dll` | `XDspSubmitCommand` | Learn index entry only | None known | needs evidence |
| `xapu.dll` | `XApuConnect` | Learn index entry only, plus input and output parameter struct names | None known | needs evidence |
| `xapu.dll` | `XApuDequeueResult` | Learn index entry only | None known | needs evidence |
| `xapu.dll` | `XApuDisconnect` | Learn index entry only | None known | needs evidence |
| `xapu.dll` | `XApuEnqueueCommand` | Learn index entry only | None known. Desktop XAudio2 has no XMA decoder. | needs evidence |
| `xapu.dll` | `XApuGetResultCount` | Learn index entry only | None known | needs evidence |
| `acphal.dll` | `AcpHalCreate` | None (index lists only the AcpHal member page) | None known | needs evidence |
| `acphal.dll` | `AcpHalAllocateShapeContexts` | None | None known | needs evidence |
| `acphal.dll` | `AcpHalReleaseShapeContexts` | None | None known | needs evidence |
| `acphal.dll` | `ApuCreateHeap` | Named in a DirectXTK12 diagnostic as the heap that `ApuAlloc` draws from. No call site and no signature. | Process memory (proposed, untested) | needs evidence |
| `acphal.dll` | `ApuAlloc` | Call sites at [SoundEffect.cpp L246](https://github.com/microsoft/DirectXTK12/blob/f3555e3014a53c81a6202b786dc4d6509b95a936/Audio/SoundEffect.cpp#L246), [WaveBankReader.cpp L799](https://github.com/microsoft/DirectXTK12/blob/f3555e3014a53c81a6202b786dc4d6509b95a936/Audio/WaveBankReader.cpp#L799) and [SoundStreamInstance.cpp L470](https://github.com/microsoft/DirectXTK12/blob/f3555e3014a53c81a6202b786dc4d6509b95a936/Audio/SoundStreamInstance.cpp#L470) | Aligned process memory (proposed, fixture only) | partial call shape |
| `acphal.dll` | `ApuFree` | Call sites at [SoundEffect.cpp L95](https://github.com/microsoft/DirectXTK12/blob/f3555e3014a53c81a6202b786dc4d6509b95a936/Audio/SoundEffect.cpp#L95) and [SoundStreamInstance.cpp L60](https://github.com/microsoft/DirectXTK12/blob/f3555e3014a53c81a6202b786dc4d6509b95a936/Audio/SoundStreamInstance.cpp#L60) | Release of the block above (proposed, fixture only) | partial call shape |
| `acphal.dll` | `ApuIsVirtualAddressValid` | Address validation per GDK index member page | Buffer validity query (proposed, fixture only) | partial call shape |
| `acphal.dll` | `ApuMapApuAddress` | Address mapping per GDK index member page | APU address to CPU virtual address (proposed, fixture only) | partial call shape |
| `acphal.dll` | `ApuMapVirtualAddress` | Address mapping per GDK index member page | CPU virtual address to APU address (proposed, fixture only) | partial call shape |

No import has both a public signature and a usable desktop replacement. The 17 imports marked "needs evidence" stay unresolved, and this change adds no binding for any of them. Those calls cover the stream, command-queue and SHAPE context interfaces, which are console audio hardware with no desktop counterpart known. They need a public declaration or a source-built observation before any caller.

The DirectXTK12 call sites establish the following:

- `ApuAlloc` returns an `HRESULT` that callers test with `FAILED`.
- The first argument is the address of a `void*` that receives the block.
- The second argument is pointer-sized, and every call site passes `nullptr`. Its type is not public.
- The third argument is a byte count cast to `UINT32`.
- The fourth is the alignment constant `SHAPE_XMA_INPUT_BUFFER_ALIGNMENT`. Its value and width are not public.
- `ApuFree` takes the returned `void*`. Every call site discards its result, so the return type is not public.
- When `ApuAlloc` fails, DirectXTK12 asks whether the heap from `ApuCreateHeap` was large enough. Running out of heap is therefore one failure mode. The exact error code is not public.

### APU allocation caller and recorder

A source-built caller ([`apu_alloc_caller.asm`](../tests/cpu/apu_alloc_caller.asm)) imports `ApuAlloc` and `ApuFree` from `acphal.dll` by name with the call shape above, plus `finish_exe` from `cpu_exe_io.dll`, and starts through [`exe_startup.asm`](../tests/cpu/exe_startup.asm). A test-only recording provider ([`apu_alloc_provider.cpp`](../tests/cpu/apu_alloc_provider.cpp)) serves both names from a 64 KiB arena. The arena stands in for the heap `ApuCreateHeap` would create. That function's signature is not public, so neither the caller nor the provider uses it. This is not a production `acphal.dll`. The arena size, the refusal code and the untouched output on failure are proposed desktop policy for the fixture, not console evidence.

1. **Observable result:** a 4 KiB request at 2 KiB alignment returns `S_OK` and a 2 KiB-aligned block, and the caller writes and reads back the block's last word.
2. **Refusal:** a 128 KiB request returns `E_OUTOFMEMORY` (`0x8007000E`) and leaves the caller's sentinel output unchanged.
3. A 64 KiB request is refused the same way while the first block is live.
4. After `ApuFree` releases the first block, the same 64 KiB request succeeds, and the caller writes and reads back its last word before releasing it.
5. The caller checks its own provider's counts: 4 allocations, 2 releases, no live blocks and no rejected releases. It then exits with `0x8A0`.

The check runs the EXE under the Windows loader, with and without a live worker at exit, and under `pe_map`. Both loaders exit with `0x8A0`, and the mapped run's recorded sizes, alignments and results match the expected sequence (`S_OK`, `E_OUTOFMEMORY`, `E_OUTOFMEMORY`, `S_OK`). It also runs these controls:

- A lenient provider build (`APU_TEST_LENIENT`) accepts every size. Under both loaders the caller stops at the refusal step with `0xF02`.
- Withholding `ApuAlloc` or `ApuFree` stops `pe_map` at import resolution before entry (`unresolved import acphal.dll!ApuAlloc` or `!ApuFree`). The provider records no calls.

The fixture shows that the caller's success path, refusal path and release accounting are distinguishable and identical under both loaders. It does not establish the console's error codes, the second argument's meaning, the alignment width, the heap size, or whether the startup path calls `ApuAlloc` before `ApuCreateHeap`.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_apu_alloc_caller$' -V
ctest --preset release --no-tests=error -R '^cpu_native_apu_alloc_caller$' -V
```

### ACP address and memory mapping caller and recorder

A source-built caller ([`apu_address_caller.asm`](../tests/cpu/apu_address_caller.asm)) imports the ACP address and memory mapping exports from `acphal.dll` by name (`ApuAlloc`, `ApuFree`, `ApuMapVirtualAddress`, `ApuMapApuAddress`, `ApuIsVirtualAddressValid`), plus `finish_exe` from `cpu_exe_io.dll`, starting through [`exe_startup.asm`](../tests/cpu/exe_startup.asm). A test-only recording provider ([`apu_address_provider.cpp`](../tests/cpu/apu_address_provider.cpp)) serves the five exports from a 64 KiB arena with a synthetic APU device base address (`0x200000000`).

- **Public evidence**:
  - `ApuAlloc` and `ApuFree` call shapes from MIT Microsoft/DirectXTK12 call sites.
  - Microsoft Learn GDK index confirms `acphal.dll` member APIs.
- **Convention vs Evidence**:
  - **Public/observed evidence**: allocation of audio buffer memory, linear address translation between CPU pointers and APU addresses, and validity query lifecycle.
  - **Recorder convention**: the 64 KiB arena size, synthetic 64-bit APU device base address, 0 / null refusal sentinels on unmapped inputs, and `E_INVALIDARG` / `E_OUTOFMEMORY` HRESULTs are proposed desktop policy for this fixture.
- **Verified cases**:
  1. **Bidirectional mapping identity**: `ApuMapVirtualAddress` maps an active CPU virtual allocation to a device APU address, and `ApuMapApuAddress` maps that APU address back to the exact CPU pointer (`ApuMapApuAddress(ApuMapVirtualAddress(ptr)) == ptr`).
  2. **Offset preservation**: `ApuMapVirtualAddress(ptr + offset) == apu_addr + offset` and `ApuMapApuAddress(apu_addr + offset) == ptr + offset`.
  3. **Validity query lifecycle**: `ApuIsVirtualAddressValid` returns `TRUE` (`1`) for an active allocation, and returns `FALSE` (`0`) after `ApuFree`.
  4. **Data access**: Memory written through the mapped pointer is verified byte-for-byte upon readback.
  5. **Refusal controls**: Null pointer, stack pointer, and fabricated/out-of-range APU addresses are refused (`ApuIsVirtualAddressValid` returns `FALSE`, `ApuMapVirtualAddress` returns 0, `ApuMapApuAddress` returns `nullptr`).
  6. **Faulty provider control**: A provider that returns null on inverse mapping (`APU_TEST_FAULTY`) is detected and causes the caller to abort at Step 4 (`0xF04`).
  7. **Withholding negative controls**: Withholding any of `ApuMapVirtualAddress`, `ApuMapApuAddress`, or `ApuIsVirtualAddressValid` stops `pe_map` at import resolution before entry with `STATUS["unresolved"]`.

On success, the caller exits with `0x8A1`. With `CPU_CRC_TEST_LIVE_WORKER=1`, a live worker thread outlives the body under the Windows loader, and `finish_exe` terminates the process cleanly with code `0x8A1`. Under `pe_map`, `finish_exe` preserves the host return across all cases. Run:

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_apu_address$' -V
ctest --preset release --no-tests=error -R '^cpu_native_apu_address$' -V
```

### DirectStorage factory and queue adapter

This fixture exercises the DirectStorage initialization sequence and submission controls. The source-built caller ([`dstorage_caller.asm`](../tests/cpu/dstorage_caller.asm)) imports `DStorageGetFactory` by name from `xcrdapi.dll` and `finish_exe` from `cpu_exe_io.dll`, starting through [`exe_startup.asm`](../tests/cpu/exe_startup.asm). Under a controlled adapter provider ([`dstorage_provider.cpp`](../tests/cpu/dstorage_provider.cpp)), it verifies factory acquisition, staging buffer configuration, queue creation, delayed submission, and exact readback:

- **Public sources**:
  - [Microsoft DirectStorage](https://github.com/microsoft/DirectStorage) (`include/dstorage.h`, MIT License) defines the public PC `IDStorageFactory` (`{6924ea0c-c3cd-4826-b10a-f64f4ed927c1}`), `IDStorageQueue` (`{cf90e079-90c1-4320-be50-b01ec69e0637}`), `DSTORAGE_QUEUE_DESC`, `DSTORAGE_REQUEST`, and public COM vtable slot orders.
  - [Microsoft Learn GDK DirectStorage Overview](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/console/storage/directstorage/directstorage-overview) describes factory acquisition, staging buffer allocation, queue dispatch, delayed submit, and auto-submission thresholds.

- **Convention vs Evidence**:
  - **Public DirectStorage evidence**: the header identifies the PC `IDStorageFactory` interface. The GDK documentation describes explicit submission and the half-capacity auto-submission threshold. The recorder's PC IID refusal and unchanged output pointer are fixture behavior.
  - **Recorder convention**: Fresh fixture-owned interface identifiers `IID_FixtureFactory` (`{ca891497-6f8f-4d03-b1a2-f239b4ce41af}`) and `IID_FixtureQueue` (`{82af1362-6445-4b96-9fbf-5d2d416b605e}`); mock provider structures; 32 MiB staging buffer configuration; and the adapter vtable layout (slot 3 `CreateQueue`, slot 8 `SetStagingBufferSize`, slot 3 `EnqueueRequest`, slot 6 `Submit` following the recorder convention). A public source would be needed before a fixture or production adapter presents other IDs or slots as a platform contract. Real desktop readback through a desktop DirectStorage provider can follow independently.
  - **Windows runtime evidence**: Export binding of `xcrdapi.dll!DStorageGetFactory` and `cpu_exe_io.dll!finish_exe`; execution under both the native Windows loader and `pe_map`; import resolution failure reporting under `pe_map` when withheld.

- **Verified cases**:
  - **Case 1 (Success path reaches submit with exact readback)**: `DStorageGetFactory` queries `IID_FixtureFactory`, configures a 32 MiB staging buffer via `SetStagingBufferSize(0x2000000)` (vtable slot 8), creates a queue with descriptor `DSTORAGE_QUEUE_DESC` via `CreateQueue` (vtable slot 3) under `IID_FixtureQueue`, enqueues a memory request with destination buffer, verifies the delayed-submit negative control, explicitly submits via `Submit()` (vtable slot 6), verifies exact readback data transfer into destination buffer, releases objects, and exits with code `0x870`.
  - **Case 2 (PC interface refusal)**: Requesting the public MIT DirectStorage PC `IDStorageFactory` interface (`{6924ea0c-c3cd-4826-b10a-f64f4ed927c1}`) returns `E_NOINTERFACE` and leaves the caller's output pointer untouched.
  - **Case 3 (Factory failure refusal)**: When factory acquisition fails or is refused, the sequence aborts with exit code `0x301` leaving queues uncreated.
  - **Case 4 (Queue failure refusal)**: When queue creation fails, the sequence aborts with exit code `0x303` with staging buffer configuration preserved.
  - **Delayed-submit negative control**: Confirms that staged requests below the queue's half-capacity threshold remain pending and destination buffer bytes remain unmodified (`0xAA`) prior to explicit `Submit()`. Calling `Submit()` completes the staged request and sets completion.
  - **Eager-provider negative control**: A faulty provider that completes prematurely on `EnqueueRequest` prior to `Submit()` is detected and rejected with exit code `0x304`.
  - **Changed-IID patch control**: Patching the requested factory interface in the caller binary causes `DStorageGetFactory` to fail with `E_NOINTERFACE`, exiting with `0x301` under both loaders.

On success, the caller exits with `0x870`. With `CPU_CRC_TEST_LIVE_WORKER=1`, a live worker thread outlives the body under the Windows loader, and `finish_exe` terminates the process cleanly with code `0x870`. Under `pe_map`, `finish_exe` preserves the host return across all cases. An independent negative control confirms that withholding `DStorageGetFactory` stops `pe_map` at import resolution before entry with `STATUS["unresolved"]` naming the missing export. Run:

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_dstorage$' -V
ctest --preset release --no-tests=error -R '^cpu_native_dstorage$' -V
```

### Grouped allocation descriptor builders

In the inspected Bink image, the function interval beginning at RVA `0x67c0` contains direct calls to the group initializer (`0x25b80`), append helper (`0x25b20`) and grouped allocator (`0x25a40`). The initializer places two adjacent arrays after a 64-byte header. Each successful append records a destination and a padded stride. With five available slots, requests of 0, 33, 1, 1025 and 32 bytes produced strides of 32, 64, 128, 1280 and 512 bytes in order. The stride in 32-byte units is congruent to the previous total in units plus one, modulo 32. All five stored strides were even. These helpers do not establish who produces odd tagged entries.

Build `cpu_bink_descriptors_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_bink_descriptors$'`. The test compares a source-written MASM implementation and its translation against explicit expected arrays after every append. Set `BINK_DESCRIPTOR_IMAGE` to the local Bink DLL to enable the fingerprint-checked `cpu_bink_package_descriptors` target and test. In the translated interface a capacity breakpoint returns status zero, allowing the test to check that both arrays and their guard word survive an extra append. The native control runs only within capacity.

### Bink allocation and release on the Windows heap

The exported binding now serves requests from `era_heap`, so the Bink shim and the heap experiments share one attribute decoder, alignment rule and release path. `bink_heap_check.py` checks on the binding directly that a 97- or 129-byte request with `0x05e50000` returns a 32-byte-aligned, zeroed block and that the matching release frees it.

The local test lowers the allocation and release callers and the exported `BinkSetMemory` setter from x64 machine code to C++, links the result to a Windows process-heap adapter, and runs the new DLL. The translator follows the setter's export jump and binds its two pointer stores to the same slots the allocation path reads. It also resolves the named PE imports, including tail transfers through linker thunks. The console-specific meaning of the heap attributes remains open.

On Windows, the public native fixture, its translation and the locally translated callers passed the same checks:

- Lengths 0 and `0xffffffff` returned null before reaching the allocator. Three forced allocation failures returned null after one request each. The largest input, `0x100000001`, verified 64-bit size handling without a large allocation.
- Each accepted length requested 64 extra bytes with attributes `0x05e50000`. Returning heap-backed storage at each of the 32 possible base residues produced a 32-byte-aligned user pointer. The check wrote the entire payload and compared both header bytes and the surrounding guards.
- With 32 allocations live, releasing them in reverse order passed each original base to the adapter once with the allocation attributes. All 32 backing blocks were freed by `HeapFree`. Releasing null made no allocator call.
- Callback pairs A and B each received one allocation request (97 and 193 bytes for 33- and 129-byte payloads). Both blocks contained the corresponding release function address, and the payload and guards matched. After installing B, releasing A's block called A's release function once with A's original base. After clearing the callbacks, releasing B's block likewise called B once with B's base. Neither operation called the XMem adapter.
- A null custom return for a 65-byte payload led to exactly one 129-byte `XMemAlloc` request. After switching callbacks, that block went back to `XMemFree` once with the original base and attributes. When both allocation attempts failed the caller returned null. A separate all-ones custom return stopped the request before the XMem adapter.

For the public control, build `cpu_bink_heap_lifted` in the dev preset, then run `ctest --preset dev -R '^cpu_aot_bink_heap$' --no-tests=error -V`. For a local package, set the CMake cache entries `BINK_HEAP_IMAGE` to the DLL path and `BINK_HEAP_ENTRIES` to the semicolon-separated allocation and release function **entry** RVAs for that image. Build `cpu_bink_package_lifted` and run `cpu_aot_bink_package_heap`. Generated translations stay in the build directory.

The grouped caller at RVA `0x25a40` in the inspected Bink DLL tail-calls the allocator at `0x25bf0` when its first argument is null. A 33-byte prefix becomes 64 bytes before the allocator adds its own 64 bytes. With a group and 48 extra bytes, the same input produces a 176-byte request at the heap provider. On success, an odd length word selects a pointer slot relative to the returned block and an even word selects an external slot. Clearing the low bit gives the payload stride. On allocation failure both the extra-byte total and the entry count are cleared and the external slot retains its value. The [descriptor builders](#grouped-allocation-descriptor-builders) produce even strides, and no code in the inspected image produces an odd one. The append helper at `0x25b20` is the only writer of the length array. It stores the request rounded up to 32 bytes plus a multiple of 32, so bits 0–4 of every stored word are clear. Of the ten call sites of `0x25a40`, three pass no group: `BinkUtilMalloc`, the asynchronous thread start and the subtitle loader through `0x8460`. The other seven pass a stack group that the same function filled only through `0x25b20`: one each in `BinkAllocateFrameBuffers` and `BinkOpenTrack`, and five in the open routine at `0x67c0`. None of these functions writes the group header or its arrays directly. The initializer, append helper and grouped allocator are not exported and have no data references outside `.pdata`. In this image the relative pointer-slot branch is therefore unreachable, and the odd length in the group controls below is synthetic. This was established by static analysis, without execution.

Build `cpu_bink_heap_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_aot_bink_group$'` for the source-built assembly control and its translation. For the inspected local DLL (SHA-256 `9eaf045d004cd7d6a8382aaff0c6ea23a29fec4ef8bfacd726b54f5bc26fafd0`), set `BINK_HEAP_ENTRIES` to `0x25bf0;0x25bc0` and `BINK_GROUP_ENTRY` to `0x25a40` alongside `BINK_HEAP_IMAGE`. Build `cpu_bink_package_lifted` and run `cpu_aot_bink_package_group`. The test supplies both descriptor arrays and runs the translated caller with the Windows test heap.

## Upscaler working-set counter updates

Immediately after the statistics calls at RVAs `0x74d2` and `0x75b1`, both paths add the new sample minus the previous sample to a 64-bit counter. A zero status substitutes zero for the new sample even if the supplied output word is nonzero. With starting counters 11 and 101, a previous sample of 23, a new sample of 70, successful status and flag 256, creation produces 58 and 148. Release produces 58 and 101 because it reads only the low flag byte. Both counters wrap modulo 2^64.

Build `cpu_working_set_delta_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_working_set_delta$'` for the source-built assembly control and its translation. With `FFX_UPSCALER_IMAGE` set to the fingerprinted local upscaler, build `ffx_package_working_set_delta` and run its namesake test for the two translated instruction ranges. The test supplies the status, samples and flags at the point where the query returns. The Xbox statistics provider and real sample values remain unverified. The call evidence below gives the samples as bytes of GPU memory, and the [desktop `xmem.dll`](#desktop-xmemdll) supplies them from DXGI.

`ffx_package_working_set_xmem_warp` and `ffx_package_working_set_xmem_hardware`, defined with the same image, feed the two translated ranges with samples from the desktop `xmem.dll` taken around creating and releasing a 64 MiB D3D12 heap, once with the aliasable flag clear and once set. The total counter must grow by the heap size and the aliasable counter by the heap size only when the flag is set, and release must restore both. The test substitutes zero for a failed earlier sample as the caller does at `0x7475` and `0x7577`, outside the translated ranges. Negative control: with the former `ERROR_NOT_SUPPORTED` stub both counters stay unchanged and both cases fail. The hardware case selects its adapter as `xmem_shim_working_set_hardware` does. The resource creation and release pairs (`0x2ea1`/`0x39a9`, `0x3b40`) are not translated.

```powershell
cmake --preset dev -B out/build/pkg "-DFFX_UPSCALER_IMAGE=<upscaler DLL>"
cmake --build out/build/pkg --target ffx_package_working_set_xmem
ctest --test-dir out/build/pkg --no-tests=error -R '^ffx_package_working_set_'
```

## Six API call sites

After loading the toolchain, inspect three local DLLs. `out/calls/six-apis.json` records image SHA-256, import slots, callers of thunks, embedded container sections and code references. Addresses below are RVAs in the relevant DLL. Indirect calls need execution evidence to identify the selected path.

```powershell
.\tools\dev.ps1
.\out\pe-env\Scripts\python.exe -B tools/call_evidence.py "game-files/<build-directory>"
```

| Import | Observed inputs, return handling and desktop check |
|---|---|
| `XMemAlloc` | At `0x25c35`, Bink's fallback requests the length plus 64 with attributes `0x05e50000` and checks for null. It advances the base by `64 - (base & 31)` bytes (33–64), giving a 32-byte-aligned user pointer. The two preceding bytes hold the offset at `[-1]` and a tag at `[-2]`. At `0x565d`, AMD rounds the nonzero request up to a multiple of 256, reads attributes at `0x6eee0` (on-disk initial value `0xa8004000`) and checks for null. Under the ERA layout the alignment fields give 32 and 256 bytes, matching this caller arithmetic. `era_heap_observed_words` checks the desktop handling of both words; allocator-ID and GPU-visibility semantics remain unconfirmed. |
| `XMemFree` | At `0x25be4`, Bink uses the saved offset to recover the allocation base and passes the same attributes. Custom allocations use a matching callback. AMD cleanup at `0x5712` and release at `0x5921` both use the allocation's attribute slot. The latter also clears the stored pointer. No observed caller consumes the return value. |
| `XMemVirtualQuery` | Four BVH callers pass the current process, target address and a zeroed 64-byte buffer. Each checks a nonzero return, bit `0x1000` of the word at `+0x20` and either low bit of the byte at `+0x30`. Source acceleration structure validation at `0x19c69` also requires the byte at `+0x24` to intersect mask `6`. Destination acceleration structure (`0x19ccb`), index buffer (`0x19d9d`) and vertex buffer (`0x19df9`) use the common checks. Diagnostics describe the whole check. The individual tail bits have no confirmed meaning. |
| `XMemGetWorkingSetStatistics` | Seven AMD calls pass selector `0x20000001` and a 128-byte stack buffer (`0x2690`: `rsp+0x20` up to the cookie at `rsp+0xa0`). A zero return contributes zero; success uses the 64-bit value at `+8`. The calls bracket four operations, and only `after - before` is used: resource creation `0x2ea1`/`0x39a9` and heap creation `0x7452`/`0x74d2` (`ID3D12Device::CreateHeap`, vtable `+0xe0`) add it, and resource release (getter `0x26b3`, called twice from `0x3b40`) and heap release `0x7568`/`0x75b1` subtract it. The targets are two qwords at `+0x40` and `+0x48` of 96-byte per-effect records; the second changes only for aliasable resources and heaps. This matches the per-effect `vramUsage` (`totalUsageInBytes`, `aliasableUsageInBytes`) of the public FidelityFX SDK DX12 backend: v2.3.0, whose FSR upscaling version 4.1.1 is the version string in this DLL, `Kits/FidelityFX/backend/dx12/ffx_dx12.cpp`, MIT-licensed per `docs/license.md`. That backend adds known allocation sizes at the same four points, with the same callback argument shapes. The value at `+8` is therefore used as a byte count of GPU memory. Its scope and the selector's meaning remain open. |
| `D3D12SerializeVersionedRootSignature` | At `0xc2d0` the BVH code copies a caller's 48-byte descriptor and adds flags `0x03000000` at `+0x28`. At `0xc32f` it passes the descriptor, a blob output pointer and a null error output. Negative HRESULT enters diagnostics. Success uses the blob length and data pointer. A synthetic v1.1 descriptor returned `S_OK` with baseline flags and `E_INVALIDARG` with the two added bits. |
| `D3D12CreateRootSignatureDeserializer` | At `0x4f88`, AMD reads a byte pointer and a 32-bit length from record offsets `+0/+8`, then supplies an interface IID and object output address. A negative HRESULT or null object triggers partial cleanup. Success retrieves the descriptor via the object. Desktop code read the root signature from a complete Microsoft sample shader and returned `E_INVALIDARG` for a synthetic shader without one. |

### Graphics input paths

- The BVH serializer helper has 16 direct calls. At `0xd967–0xd998` the caller builds a version, six parameters and the parameter-array address on the stack. At `0x2151d` another path references container `0x25700` and passes it downstream with length `0x1134`. The DLL contains 52 code-referenced DXBC containers with structurally valid DXIL programs and `RTS0` sections. All 52 root signature headers carry `0x03000000`. The desktop failure case reproduces these flag bits. Their meaning remains unconfirmed.
- AMD registers the deserialization function in a callback table at `0xcea3–0xceaa`. A producer at `0xe9b8/0xe9c3` selects container `0x714f0` of length `0x1910`, copies its descriptor record and calls through the table at `0xebad`. The DLL has 29 structurally complete containers, each with a matching pointer-table entry and `RTS0` and `ACCC` sections. None contains DXIL. These observations support an embedded source. Runtime initialization determines the indirect path.
- Counts cover the inspected DLLs. Scene shaders, archive read order and the graphics path used at startup have no execution record here. An entry executable would allow its imports and initialization calls to be examined.

### Microsoft sample on desktop

The test uses Microsoft's SimpleTriangle at `Xbox-GDK-Samples` commit `b1e77b6ac10f95b07296025fda171ca2ddc42dbe`. The project includes a `Gaming.Xbox.Scarlett.x64` configuration and a published reference image. Desktop builds compile the two HLSL files using the sample's three vertices, colors, sRGB render target and clear color. The test checks SHA-256 for both shaders and the image. Set `SIMPLE_TRIANGLE_DIR` to that revision's sample directory and run `ctest --preset dev -R reference_triangle -V`. A sparse checkout into the default location needs no cache entry: `git clone --filter=blob:none --no-checkout https://github.com/microsoft/Xbox-GDK-Samples out/reference/b1e77b6ac10f95b07296025fda171ca2ddc42dbe`, then in that directory `git sparse-checkout set --no-cone /Samples/IntroGraphics/SimpleTriangle/` and `git checkout b1e77b6ac10f95b07296025fda171ca2ddc42dbe`. The sample repository's `.gitattributes` writes `*.hlsl` with CRLF. The test hashes the shaders with LF line endings, as they are stored in the repository, so a raw download and a checkout give the same result. [Microsoft sample overview](https://learn.microsoft.com/en-us/samples/microsoft/xbox-gdk-samples/simpletriangle/) · [Pinned source and reference image](https://github.com/microsoft/Xbox-GDK-Samples/tree/b1e77b6ac10f95b07296025fda171ca2ddc42dbe/Samples/IntroGraphics/SimpleTriangle)

RTX 2060 and WARP rendered at 64×64. Excluding triangle edges by normalized coordinates left 10,812 color channels per adapter. Differences from the source-derived colors were at most 2 on an 8-bit scale. Against the published image the mean absolute difference was 6.88 and the maximum was 17. The two desktop images differed at 94 pixels, by at most 1 per channel. The published image came without a device model, build hash or lossless GPU readback. Source-derived colors supply the acceptance values. Microsoft distributes console GDKX separately. [Microsoft developer resources](https://developer.microsoft.com/en-us/games/resources/)

## Compute and ray tracing before executable intake

The inspection reads bytes from two local DLLs. Desktop parsers inspect serialized descriptors and DXIL. GPU tests execute HLSL built from this repository. After building, run:

```powershell
$pkg = (Get-ChildItem game-files -Directory)[0].FullName
.\out\pe-env\Scripts\python.exe -B tools/probe_roots.py $pkg --controls out/build/dev
ctest --preset dev -R 'compute_|dxr_(query|pipeline)|shader_metadata|bvh_buffer_ops' -V
```

`out/calls/desktop-roots.json` records per-container return codes, root signature summaries, matching desktop fixtures, static call counts and validator diagnostics. It also hashes the input inventory and probe executable. Each container was checked independently.

| Windows check | XboxBvhBuilder | AMD upscaler |
|---|---|---|
| Both D3D12 deserializers read the embedded signature from the full container | 52/52 accepted, 16 distinct signatures | 29/29 accepted, 3 distinct signatures |
| Reserialize unmodified v1.1 descriptor | 52/52 returned `E_INVALIDARG` | 29/29 accepted |
| Clear only `0x03000000` in a memory copy and reserialize | 52/52 accepted. Console flag semantics are unknown. | Original flags were zero. |
| Validate the entire container and the internal bitcode separately using the Windows SDK | Both checks rejected all 52 Shader Model 6.5 compute programs. | `ACCC` present, no DXIL input. |

Both source-built control shaders passed container and module validation. For the BVH modules the validator reported an unrecognized `dxil-ms-xb2` target and `xbox.compiler.options` metadata. DXC disassembly showed 43 modules directly calling 33 distinct `xbox.` functions, including cache-controlled reads, cross-lane operations and floating-point atomics. The other nine had no such direct calls. Diagnostics retain up to 2 KiB per message. Counts use the complete disassembly. [DXC validator interface](https://github.com/microsoft/DirectXShaderCompiler/blob/main/include/dxc/dxcapi.h)

The first synthetic compute layout uses an SRV, a UAV and 16 root constants. Its `RTS0` payload matches 11 BVH containers byte for byte after clearing the two flags. The second uses 13 UAVs, 21 SRVs, four root CBVs and a static sampler. It matches 27 unmodified AMD containers. Both tests access the last registers in the descriptor tables. RTX 2060 and WARP wrote 257 correct integer words per layout and preserved 63 sentinel words.

The native DXR fixture builds a bottom-level structure (BLAS) from one triangle and a top-level structure (TLAS) from two instances. Five rays cover hits, a geometric miss, distance clipping, translation and instance masks. The existing `dxr_query` test uses inline RayQuery on DXR Tier 1.1 with Shader Model 6.5. The `dxr_pipeline` test uses a source-built `lib_6_3` ray generation, miss and closest-hit library with TraceRay and DispatchRays on DXR Tier 1.0 or later with Shader Model 6.3 or later. The standalone GPU check includes `dxr_pipeline`. The original RTX 2060 and WARP inline runs hit instances 7 and 11 at distance 1 and missed the other three rays. The GTX 1060 pipeline and both app-local WARP paths reported the same instance IDs, primitive index 0 and hit distance 1. The three miss rows were all zero. Integration of the game's acceleration structure layout and consumers with desktop DXR remains unverified. [Microsoft DXR specification](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#d3d12_raytracing_tier)

Four priority BVH containers use the first layout, are referenced by the function starting at `0x20270` and use 32×1×1 thread groups. Source-built HLSL reproduces the data operations inferred from static instructions. Independent CPU results supply the expected values.

| Container RVA | Observed input and write behavior |
|---|---|
| `0xb3ae0` | Copy the specified count of 16-byte input blocks to matching output offsets. |
| `0xb82e0` | Write the same constant to all four 32-bit words of each valid block. |
| `0xb4bd0` | Write consecutive unsigned 32-bit integers starting at a seed. |
| `0xb5be0` | Read the length parameter from the second input word and copy `64 × (parameter + 1)` bytes in steps. When reset is enabled, write the positive and negative maximum finite float bit patterns to six words at output offsets 16–39. |

Tests use disjoint buffers and cover zero length, thread-group boundaries, integer wraparound and untouched memory. Each desktop backend runs 26 cases and checks 384 words per case. The fourth operation schedules its optional reset in a second dispatch with a UAV barrier. Source cross-group synchronization, aliasing and actual game call parameters remain open.

### Module-only validation by DXC release

`shader_metadata_probe` validates each control shader twice through `root_probe`: the whole container with default flags, then the bare bitcode with `DxcValidatorFlags_ModuleOnly`. With the DXC 1.7.2308 in the original Windows SDK 10.0.26100 release, the second call returns `E_INVALIDARG` without a validation status. [Issue #98](https://github.com/R0Y00/Xbox2Windows/issues/98) The test still runs the root signature, container and disassembly checks. It reports a skip only when both module calls return that code and `dxil.dll` has a 101.x file version. `shader_metadata_controls` checks that rule without a validator and prints each case. A refusal from a 1.x validator, one refused and one validated shader in either order, another call failure and a failed module status are all rejected. The Linux Python job runs the same controls.

The table repeats `root_probe`'s two calls with the `dxcompiler.dll` and `dxil.dll` of each official x64 release. The inputs were `compute_root`, `compute_tables` and `triangle_vs` compiled by DXC 1.7.2308. Every release accepted the three containers. The calls ran under Wine 11.0. The 1.7.2308 and 1.9.2609 rows match the native Windows 10 results in #98. `IDxcVersionInfo` reports 1.8 on both sides of the boundary, so the test reads the file version instead.

| DXC release | `dxil.dll` file version | `IDxcVersionInfo` | Module-only call |
|---|---|---|---|
| 1.7.2308 | 101.7.2308.12 | 1.7 | `E_INVALIDARG` |
| 1.8.2403, 1.8.2405, 1.8.2407 | 101.8.2403.18, 101.8.2405.17, 101.8.2407.12 | 1.8 | `E_INVALIDARG` |
| 1.8.2502 | 1.8.2502.8 | 1.8 | Status 0 |
| 1.8.2505, 1.8.2505.1, 1.9.2602, 1.9.2602.24, 1.9.2607, 1.9.2609 | 1.x | 1.9 | Status 0 |

The SDK's own validator depends on its servicing level. On native Windows 11 build 26200.9974, Windows SDK 10.0.26100.8249 installs `dxcompiler.dll` and `dxil.dll` 1.8.2502.11 under `bin/10.0.26100.0/x64`. CMake selected that `dxc.exe` by default, and `shader_metadata_probe` passed with `module_only=validated`. Which servicing update first shipped 1.8.2502 is untested.

To check another release on Windows, configure a separate build directory with its compiler, then run the test:

```powershell
cmake --preset dev -B out/build/dxc-check -DDXC_EXECUTABLE=<path>\bin\x64\dxc.exe
cmake --build out/build/dxc-check
ctest --test-dir out/build/dxc-check --no-tests=error -R '^shader_metadata_probe$' -V
```

### Retargeting dxil-ms-xb2 modules to desktop DXIL

`tools/dxil_retarget.py` parses LLVM 3.7 bitcode blocks and records without modifying opaque subblocks such as function bodies. It rewrites `MODULE_CODE_TRIPLE` from `dxil-ms-xb2` to `dxil-ms-dx` and removes the `METADATA_NAME`/`METADATA_NAMED_NODE` pair for `xbox.compiler.options` in `METADATA_BLOCK_ID`, preserving numbered metadata indices. For a DXBC container it also canonicalizes the `DXIL` program header to `BitcodeOffset=16`, retargets any DXIL `STAT` reflection part, clears the `0x03000000` root signature flag pair in `RTS0` (unless `--keep-root-flags` is set) and zeroes the stale 16-byte DXBC digest. Desktop DXC rejects `dxil-ms` as an unknown triple; `dxil-ms-dx` is the accepted desktop DXIL triple.

On the local `XboxBvhBuilder.dll` (SHA-256 `99f803ac22c01ef329dddb1d40be67238d2a812d01e8d040b3cb5135bc23707e`; 52 SM 6.5 compute containers), 9 modules make no direct `xbox.*` calls (`0x25700`, `0x35720`, `0x3ab00`, `0x46880`, `0x92e30`, `0xb3ae0`, `0xb4bd0`, `0xb5be0`, `0xb82e0`). With `dxil.dll` 1.9.2602.17 through `root_probe`:

- Before retargeting, all 9 failed both container and `ModuleOnly` validation with `0x80aa0009` (`Unknown target triple 'dxil-ms-xb2'` and `Named metadata 'xbox.compiler.options' is unknown`).
- With `--keep-root-flags`, all 9 passed `ModuleOnly` (`0x00000000`, zero diagnostics) and all 9 failed container validation with `0x80aa0013` (`Unsupported bit-flag set (root signature flags 0x3000000)`).
- With default container retargeting (clearing `0x03000000`), all 9 passed both `ModuleOnly` and container validation (`0x00000000`, zero diagnostics).

The other 43 BVH modules still fail validation after retargeting because of direct `xbox.*` calls and aggregate `insertvalue`/`extractvalue` instructions used with those intrinsics. The 29 AMD upscaler containers carry `RTS0` and `ACCC` without a `DXIL` part.

Passing `--sign` to `tools/dxil_retarget.py` (with `--probe root_probe.exe`) re-signs retargeted DXBC containers in place through `dxil.dll` (`IDxcValidator::Validate` with `DxcValidatorFlags_InPlaceEdit`). Adding `--pso-probe root_device_probe.exe` (and optional `--warp-only`) tests `CreateRootSignature` and `CreateComputePipelineState` on desktop D3D12 against the re-signed container.

Each container entry of the `--pe` report also carries `xbox_intrinsics`, a read-only inventory of the `xbox.*` names in the module as it was submitted. `calls` counts the symbols referenced by call instructions, `declarations` lists the function-table entries, `references` counts the symbols the module holds without calling them, with their form (alias, store, bitcast, global initializer or call operand; `debug-only` when the only use is a debug intrinsic's metadata), and `metadata_only` lists names carried by metadata string values (`!"xbox.…"`) without being declared, called or otherwise referenced; a named-metadata key such as `!xbox.compiler.options` names the node and is not counted. An unused declaration, or a name that only metadata mentions, is therefore never reported as a call. `--only-without-xbox-calls` selects the modules where none of those forms reach an `xbox.*` symbol, and the report counts them as `skipped_xbox_intrinsics`. The inventory itself changes no retargeting or submission behaviour; the submission gate described below does. Passing `--probe root_probe.exe` in the container mode of the same tool prints the inventory inside `validation.ir.xbox_intrinsics`.

`tools/xbox_intrinsic_gate.py` gates the submission path. The module it reads is the one that would be submitted, after retargeting, and it is refused while it still reaches an `xbox.*` builtin, either by calling it or by any other form that keeps it reachable — an alias, a stored pointer, a bitcast, a global initializer or a call operand — and the refusal names that form. A call and every other executable reference are listed separately, so a diagnostic says whether the module calls the builtin or only holds it. A symbol that appears only as metadata inside a debug intrinsic (`@llvm.dbg.value`, `llvm.dbg.declare`, `llvm.dbg.assign`) is not executable use: it does not refuse the module and is reported as debug-only, and only the callee decides, so a line that merely mentions a debug intrinsic still counts as an executable call. The scanner reads the LLVM 3.x text that `dxc -dumpbin` prints; the newer `#dbg_value` record form would be over-blocked rather than missed, which keeps the gate closed. The archive filter counts anything reachable, debug-only uses included, while the gate counts executable uses, so `--only-without-xbox-calls` also skips a module whose only reference is debug metadata — the conservative direction. A lowering counts only when the transformed module no longer calls or holds that symbol, because the gate reads instructions and not adapter claims. An adapter status table is advisory — it annotates each blocking entry with what the adapter reports and never clears a call or a reference the module still holds. A refused module names those calls and the probe process is never started, so it produces zero submissions, and the gate does not fall back to a creation failure such as `E_FAIL`. The inventory is mandatory: without a disassembly, which `--probe` produces, nothing is submitted and the result carries `gate.verdict = "NOT_CHECKED"` with `reason = "module-not-disassembled"`, so an unexamined module cannot look like a cleared one. A declaration, a name that only metadata mentions, and a module whose calls were removed all pass. Each `--pe` entry carries the decision as `gate`, the report counts `gate_rejected` and, with a probe, `pso_refused`, and `python tools/xbox_intrinsic_gate.py check-text <module.ll>` prints the same decision without a probe. `dxil_retarget_controls` covers the refusal of all six reachable forms — a call, an alias, a store, a bitcast, a global initializer and a call operand — the three admitting shapes, an advisory LOWERED status that still refuses, a missing inventory that starts no probe process, the archive filter treating every form as reaching an intrinsic, a metadata string that only spells a call and the `--pso-probe` without `--probe` path.

Public CI tests pure-Python bitcode/container/signing controls (`dxil_retarget_controls`) and source-built `compute_root.cso` (SM 6.0) and `CopyBlocksSM65.cso` (SM 6.5) shaders synthetically injected with `dxil-ms-xb2`, `xbox.compiler.options`, `BitcodeOffset=112` and `RTS0` flags `0x03000000` (`dxil_retarget_probe`). Negative controls verify that unmodified `xb2`, `--target-triple dxil-ms` and `--keep-root-flags` fail both validation and `--sign`, and that unsigned (zero-digest) and tampered-digest containers fail `CreateComputePipelineState` on WARP. Re-signed retargeted containers pass `CreateComputePipelineState` on WARP (for `compute_root.cso`, and for `CopyBlocksSM65.cso` when WARP reports SM 6.5 or newer), and the re-signed `compute_root.cso` executes on WARP with CPU-checked readback through `offscreen_check warp --compute-root-file`. `dxil_retarget_probe_hardware` runs the same signing, `CreateComputePipelineState`, and `offscreen_check hardware --compute-root-file` checks against a physical D3D12 adapter and skips with exit code `77` when no hardware adapter is visible.

```powershell
ctest --preset dev --no-tests=error -R '^dxil_retarget_' -V
# Local validation, re-signing and compute PSO check of the 9 modules without xbox.* calls:
.\out\pe-env\Scripts\python.exe -B tools/dxil_retarget.py --pe "game-files/<build-directory>/XboxBvhBuilder.dll" --only-without-xbox-calls --sign --probe out/build/dev/root_probe.exe --pso-probe out/build/dev/root_device_probe.exe --output-dir out/calls/dxil-signed --report out/calls/dxil-retarget.json
```

### BVH reads and cross-lane operations

In `out/calls/desktop-roots.json`, `static_operands` from `tools/probe_roots.py` groups functions by module count, call site and operand form. The counts below describe static instructions across the 52 BVH programs. Runtime frequency is unknown.

| Read form | Modules | Static calls |
|---|---:|---:|
| Global, one word | 17 | 227 |
| Global, two words | 3 | 129 |
| Global, three words | 12 | 184 |
| Global, four words | 10 | 44 |
| Scalar, one word | 16 | 32 |

Across 27 distinct modules, all 616 reads use 64-bit addresses and end with two LLVM `undef` arguments. They provide no definite cache policy. At RVA `0xa4780` a count read from base plus 12 limits active threads. A loop in `0xc6260` reads a word at base plus four times the index and writes it to the destination. The fixture exercises both data behaviors and the scalar offset `(index << 8) | 44`.

Two successive writes to the same GPU buffer are each ordered by a UAV-to-SRV transition before reads of all four widths. Each round submits 128 synthetic requests covering low-word carry, shared and scattered addresses, a final valid vector, overrun, misalignment and different high words. The CPU uses independent 64-bit range arithmetic and compares each returned word. Out-of-range sentinels are a fixture rule. Console cache hit rate, address ownership, invalid access behavior and visibility without explicit ordering remain unknown.

Eight modules contain 75 calls to `LaneSwizzle.i32`, with immediates 30, 60, 120, 240 and 480 occurring 15 times each. The `0x95e20` sequence conditionally accumulates exchanged values and reads lane 31. This motivated a candidate five-stage prefix sum within each 32-lane subgroup. Tests exercise native waves and group-shared memory separately. The logical 64-lane path also checks both halves of the ballot mask, prefix counts, dynamic indices and lane 31. The native path checks the active minimum, broadcast, divergent votes and dynamic reads. The CPU reconstructs expectations from GPU-reported native lane IDs.

Each backend runs 38 groups with empty, full and sparse ballots and 32 single-point inputs covering every position in each half. RTX 2060 reported native width 32–32 and WARP 4–4. Both passed the group-shared 64-logical-lane test and skipped forced native wave64 on capability grounds. A capable adapter will run that case. Single-point inputs, CPU prefix sums and final ray hits provide the PC acceptance checks. [WaveReadLaneAt semantics](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/wavereadlaneat)

```powershell
ctest --preset dev -R 'bvh_reads|bvh_waves|bvh_wave64|bvh_hits' -V
```

### From buffer reads to ray hits

`bvh_hits_hardware` and `bvh_hits_warp` connect bounded address reads and half-group prefix sums to ray intersections. Box and triangle tests follow AMD GPURT's software approach. A synthetic root holds 64 child bounds with logical 64-bit triangle addresses. The 64 logical lanes test bounds and compact candidates using both half-group sums. Receiving lanes fetch vertices and reduce nearest hits within and across native waves. The fixture breaks distance ties by the smallest primitive ID. [AMD GPURT intersections](https://github.com/GPUOpen-Drivers/gpurt/blob/dev/src/shaders/IntersectCommon.hlsl)

Three successive scenes occupy the same GPU buffer. Each is written as a UAV and then read as an SRV. A CPU brute-force, double-precision plane intersection independently checks each ray's box count, triangle hit count, closest distance and primitive ID. Each round uses the same 132 rays, covering each leaf in isolation, forward, reverse and oblique rays, edges, vertices, zero distance and box-only intersections. Two rays lie in the first row's plane with a zero depth component. They enter the flat boxes through the zero-direction slab branch, one ending exactly on a box face, and never hit a triangle.

| Scene | Hits | Misses |
|---|---:|---:|
| Sparse layout, isolate all 64 leaves | 112 | 20 |
| Permuted addresses, overlapping geometry and equal-distance hits | 14 | 118 |
| Permuted again, with invalid address high bits, an out-of-range tail and a degenerate triangle | 108 | 24 |

RTX 2060 and WARP agreed with the CPU on every field. Closed intervals, tie breaking and invalid-leaf handling are fixture rules. The tests establish synchronized desktop reads and hit results. Scarlett node layouts and cache hints remain hypotheses to check against producer writes and consumer reads.

### Independent BVH checks on PC

Each scene sends the same 136 rays through a double-precision brute-force CPU test, a custom shader reading packed nodes, and standard DXR. DXR builds its own BLAS and TLAS from the original accessible vertices. The differential test prefers inline queries on Tier 1.1 and uses DispatchRays on Tier 1.0. Its custom BVH shader still requires Shader Model 6.5. All paths check hit status and closest distance. The CPU also checks that the specific DXR-selected primitive intersects at that distance, allowing valid ties. Comparisons use triangle interiors and distance margins. Edge and endpoint cases use the preceding fixture's fixed conventions. [DXR ray extents](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-extents)

RTX 2060 and WARP each completed 408 comparisons. In fault controls only the custom shader's input changes. Swapping child 0 and 32 addresses changed four results, replaying the previous scene changed 102 and aliasing invalid address high bits changed one. Each fault must alter its designated witness ray. The GTX 1060 with driver 32.0.15.6094 on Windows 10 build 19045.6456 matched all 408 comparisons through DispatchRays. App-local WARP 1.0.22 with Agility SDK 1.620.2 matched all 408 through each DXR path. All three fault witnesses fired in each run. The five-ray and differential checks reported zero D3D12 debug warnings and errors. In-box WARP 10.0.19041.5794 reports no DXR and skips these checks.

```powershell
$env:XBOX2WINDOWS_ADAPTER = 'GTX 1060'
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^(dxr_(pipeline|query)|bvh_differential)_hardware$' -V
# After configuring out/build/w10 with the Windows 10 app-local instructions
cmake --build out/build/w10 --target offscreen_check
ctest --test-dir out/build/w10 --no-tests=error -R '^(dxr_(pipeline|query)|bvh_differential(_pipeline)?)_warp$' -V
```

Project acceptance uses reproducible PC behavior. The next game-specific check is to identify the builder selected by the main executable, its node-buffer inputs and outputs, and the downstream queries, then add the observed constraints to PC tests.

### Building bounds under contention

Static disassembly identified 51 direct calls each to `xbox.global_atomic_fmin` and `xbox.global_atomic_fmax` across seven modules each. Each call takes a 64-bit address and a float after the opcode. Integer atomic add and subtract each occurred at four sites with a 64-bit address and a 32-bit operand. Regenerate `out/calls/desktop-roots.json` with `probe_roots.py` to inspect per-module counts and operand forms.

`bvh_atomics` builds seven sets of axis-aligned bounds in GPU memory. Contending threads use 32-bit compare-and-exchange to extend six raw float fields. Two counters cross the unsigned wrap boundary. The CPU checks every field and verifies that each accepted point received a unique prior value from each counter. Inputs cover positive-only and negative-only bounds, both signed zeros, subnormals, finite extremes, invalid points and an empty bucket. The fixture skips a point with any non-finite coordinate and orders negative zero before positive zero. These are explicit desktop conventions.

The query dispatch reads the accumulated bounds after a UAV barrier and tests 54 axis-aligned rays. The CPU computes each expected bucket mask by projecting the original points onto the three axes. RTX 2060 and WARP each passed 270 ray masks over five input batches, including the empty input and a reversed 1,025-point submission. Each full submission accepted 897 points. This exercises a source-built producer and consumer with shared bounds. The observed atomic calls alone do not identify the game's node layout or special-value rules.

### Packed leaves and initialization stores

The per-module `validation.ir.packing` report follows the immediate definition of the byte selector at all 106 `V_ALIGNBYTE_B32` sites. Each selector is masked with 3. The three modules contribute 44, 44 and 18 sites. Across seven other modules, 47 `BFI` sites include variable masks, zero and contiguous low-bit masks through 1023. The desktop test uses these constraints and AMD's bit selection and byte alignment operations to build a source-owned leaf format. Its field offsets and flag assignments are fixture conventions. [AMD RDNA 2 ISA reference](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

`bvh_packed` uploads 64 packed leaf records per scene. A compute shader reconstructs their fields at four byte offsets, selects the ID and flags through a mask, then writes bounds, triangle vertices and logical addresses. The next dispatch traces the existing 132 rays directly against that buffer. A separate readback compares all 1,280 scene words plus four guard words per scene against the CPU layout. Three scenes cover address carry, an invalid high word, a truncated vertex span, degeneracy and ties. Flipping one selected ID bit in the first scene changed exactly four returned IDs. Distances and counts matched the independent geometry reference in both runs on RTX 2060 and WARP.

The static inventory records one scalar, six three-word and nine four-word cached stores. All have two trailing undefined operands. At module RVA `0xb92d0`, two pairs write the largest finite float three times, then its negative three times at the same base plus 12 bytes. `bvh_atomics` now initializes the six bounds on the GPU from dirty storage with this pair of stores. A UAV barrier orders initialization before accumulation. Empty buckets expose incomplete resets, and independent checks cover the two adjacent counters. Cache policy and the game's allocation layout remain unknown.

### Numeric operations on bounds

All 180 observed `Med3_F32` calls had two constant operands. In 156 calls they were the negative and positive largest finite floats. Six used the positive lower bound with float bits `0x0f000000` and the same upper bound. The remaining 18 used zero and `0x3f7ff972`. For ordered values these medians clamp the variable input to the two constants. The operand inventory now includes all 33 distinct direct Xbox intrinsic names.

`bvh_numeric` checks those three clamps at their boundaries, including neighboring float encodings and infinite inputs. It also tests three-input float extrema, three-input unsigned min/median/max and two independent packed binary16 lanes. For half precision the CPU decodes the exponent and fraction in double precision. Inputs cover every finite nonzero binary16 encoding and positive zero. The fixture maps signed zeros to positive zero and excludes non-finite half inputs. Each desktop backend agreed with the CPU on all 655,360 words from 65,536 input rows. NaN propagation and native Scarlett behavior remain open.

### Updating a live DXR scene

`dxr_update` moves a triangle from depth 1 to 3, changes two instance transforms and changes one visibility mask. It updates BLAS and TLAS in place using separate build and update scratch-size requirements. Seven rays cover old and new positions, visibility masks and distance clipping. Four change hit status. Both `dxr_update` (inline RayQuery) and `dxr_update_pipeline` (DispatchRays) compare hit status, instance and primitive IDs, and distance with the same CPU reference. All 28 words from each in-place update must match a fresh full build of the changed scene. Stale-scene and stale-instance controls differ on rays 0–3. Keeping the old vertices changes distances on rays 2 and 3 and restores the clipped hit on ray 6. The controls build valid acceleration structures from the old inputs they exercise. [DirectX acceleration structure updates](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#acceleration-structure-update-constraints)

The pipeline test requires DXR Tier 1.0 and Shader Model 6.3. It passed on the GTX 1060 (driver 32.0.15.6094, Windows 10 build 19045.6456). Using only `PERFORM_UPDATE` for the refit caused device removal on that driver. Retaining `ALLOW_UPDATE` on both structures while adding `PERFORM_UPDATE` passed the seven-ray fixture and all controls. Both backends passed on app-local WARP 1.0.22 with Agility SDK 1.620.2. Passing update checks reported zero D3D12 debug warnings and errors. Windows 10 in-box WARP skips both checks because it exposes no DXR. The GTX 1060 skips the inline check, which requires DXR Tier 1.1 and Shader Model 6.5.

```powershell
ctest --preset dev -R 'bvh_packed|bvh_atomics|bvh_numeric|dxr_update|era_heap_caller_layout|static_exe_intake' -V
```

### Face culling under mirrored instances

`dxr_facing` and `dxr_facing_pipeline` each send 96 rays at eight instances of one two-triangle BLAS. Each instance receives twelve rays covering both triangles, both directions along world z, and ray flags `RAY_FLAG_NONE`, `RAY_FLAG_CULL_BACK_FACING_TRIANGLES` and `RAY_FLAG_CULL_FRONT_FACING_TRIANGLES`. The instances combine `D3D12_RAYTRACING_INSTANCE_FLAG_TRIANGLE_FRONT_COUNTERCLOCKWISE`, `D3D12_RAYTRACING_INSTANCE_FLAG_TRIANGLE_CULL_DISABLE`, non-uniform scale and mirroring on x, y or z. The DXR specification defines a triangle as front facing when its vertices appear clockwise from the ray origin in a left-handed frame, evaluates that winding in object space so a negative-determinant instance transform leaves facing unchanged, and makes both cull flags inert on a cull-disabled instance. [Ray flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-flags) · [Instance flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#d3d12_raytracing_instance_flags)

The CPU reference in `tests/dxr_facing_reference.h` moves each ray into object space, intersects every triangle in double precision and applies those three rules. Before any GPU work the test compares it with a hand-derived table of front and back letters per instance. The reference expects 72 hits, 36 of them front facing, and 24 culled rays. A world-space winding test would disagree on 48 of the 96 rays, which all target the four mirrored instances. For each ray the shader reports hit status, facing, instance ID, primitive index and distance. Status, facing, instance and primitive must match exactly. A hit distance of 2 or 3 may differ by 1e-5 relative, and a miss must leave all four words zero. As a control, comparing the readback from the unculled ray for each primitive and direction with the reference for all three flag sets must produce exactly 24 hit-status mismatches. Those 96 rays cross triangle interiors inside the distance interval. A separate CPU-only ray meets the z-scaled instance at the open upper endpoint of 2. Single-precision division in the inverse direction transform gave 1.9999999403953568 and incorrectly admitted the hit. The reference performs that division in double precision.

A second fixture sends 12 rays through one identity instance of a square split into two consistently wound triangles in one BLAS geometry. Six rays cross its internal diagonal from opposite directions at three points. Each must commit a hit at distance 1 with the expected facing, and either incident primitive ID is valid. Four interior rays require a specific primitive and two rays outside the square must miss. The shader reports only the committed hit. Candidate multiplicity, silhouette edges, vertices and GPU distance endpoints remain unmeasured. [DXR shared-edge rules](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#fixed-function-ray-triangle-intersection-specification)

`dxr_facing` reads `CommittedTriangleFrontFace` on DXR Tier 1.1 with Shader Model 6.5. `dxr_facing_pipeline` uses `HitKind` in the existing `lib_6_3` DispatchRays pipeline on DXR Tier 1.0 or later. The GTX 1060 (32.0.15.6094, Windows 10 build 19045.6456) passed DispatchRays and skipped inline RayQuery. App-local WARP 1.0.22 with Agility SDK 1.620.2 passed both backends. Windows 10 in-box WARP exposed no DXR and skipped both tests. Every passing run had zero D3D12 debug warnings and errors. [DXR HitKind](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#hitkind)

```powershell
ctest --preset dev --no-tests=error -R '^dxr_facing_' -V
```

### Opacity and candidate handling in inline queries

The other DXR fixtures build one opaque geometry and trace with a `RAY_FLAG_FORCE_OPAQUE` query, so no non-opaque candidate ever reaches a shader. `dxr_opacity` builds a BLAS of four single-triangle geometries stacked along z at depths 1 to 4. Geometries 1 and 3 carry `D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE`. Three instances use no flags, `D3D12_RAYTRACING_INSTANCE_FLAG_FORCE_OPAQUE` and `D3D12_RAYTRACING_INSTANCE_FLAG_FORCE_NON_OPAQUE`. Each instance receives 42 rays: both directions along z, seven ray flag sets (`RAY_FLAG_NONE`, `FORCE_OPAQUE`, `FORCE_NON_OPAQUE`, `CULL_OPAQUE`, `CULL_NON_OPAQUE`, `ACCEPT_FIRST_HIT_AND_END_SEARCH` alone and with `FORCE_NON_OPAQUE`) and three accept masks. `tests/ray_candidates.hlsl` declares `RayQuery<RAY_FLAG_NONE>` and commits a non-opaque candidate when its geometry index is set in the accept mask, a fixture convention carried in bits 16 to 19 of the flags word. [Ray flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-flags) · [TraceRayInline control flow](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#tracerayinline-control-flow)

The CPU reference in `tests/dxr_opacity_reference.h` intersects every triangle in double precision and applies the specification's order: ray flags override instance flags, which override geometry flags, and the cull flags then remove primitives by the resulting opacity. Before any GPU work the test compares it with a hand-derived table of opacity letters per instance and flag set. Swapping the ray and instance precedence in the reference breaks that table at ray 48. The reference expects 94 hits, 42 of them committed by the shader, and 32 misses. Ignoring instance flags would change the closest hit on 26 rays.

Without the first-hit flag, status, instance, geometry and primitive must match the closest committable hit exactly. With it, the query may end on any committable hit, but must present no candidate after a commit. Every presented candidate must be a non-opaque triangle inside the ray extents and not behind the current committed hit. A candidate at exactly the committed distance is allowed, because an implementation may present the committed triangle or an equal-distance one again. Each non-opaque triangle in front of the closest hit, and each committed one, must have been presented. A miss under the first-hit flag requires every non-opaque triangle. Hit distances of 1 to 4 may differ by 1e-5 relative. Rays cross triangle interiors well inside the distance interval, so edge and endpoint rules stay out of scope. Candidate counts and visit order are implementation-defined and are not compared. The fixture skips below DXR tier 1.1 or Shader Model 6.5.

The shader separates a candidate presented after its own commit from one presented after an automatic opaque commit only. The test prints up to 16 failing rays with their categories and a count per category. It also records `CandidateTriangleRayT` at each of its own commits.

An RTX 5080 (driver 32.0.16.1692, DXR tier 1.2) matched all 126 rays in both runs. WARP 10.0.26100.9818 (DXR tier 1.1) produced the expected committed hit on every ray, but does not end a first-hit search at a committed hit, automatic or shader. The specification ends traversal after the first committed hit, whether it comes from fixed-function intersection or from the shader. [Ray flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-flags) · [TraceRayInline control flow](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#tracerayinline-control-flow) The first WARP run reported 11 mismatching rays and printed only ray 15: with `RAY_FLAG_ACCEPT_FIRST_HIT_AND_END_SEARCH` and accept mask 0, WARP committed opaque geometry 1 at distance 2, then presented geometry 0 at distance 1. The second run separated the categories over 11 rays. Rays 19, 20, 61, 62, 100, 101, 103 and 104 had candidates presented after a shader commit, and rays 16 and 17 after both kinds of commit. One more ray had candidates presented after an automatic opaque commit only. On WARP alone, the test counts a first-hit ray with candidates after either kind of commit as `warp_after_commit` and does not fail on it, provided the final hit is committable and every other check on the ray passes. A committable hit is opaque or accepted by the shader. Every other category still fails on WARP, and hardware stays fully strict.

On an AMD RDNA 4 GPU, a run reported by denn0x in the review of #216 failed `dxr_opacity_hardware`. Every mismatch was a shader-committed hit with the correct instance and geometry, but its `CommittedRayT()` equalled the true distance plus TMin. The specification makes a committed non-opaque triangle the closest hit at the candidate's distance, and `CandidateTriangleRayT` and `CommittedRayT` both measure the distance from the ray origin, not from TMin. [TraceRayInline control flow](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#tracerayinline-control-flow) · [CommittedRayT](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#rayquery-committedrayt) · [CandidateTriangleRayT](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#rayquery-candidatetrianglerayt) All 126 rays use TMin 0.25, so the offset is visible at every hit distance. On the RX 9070 XT with driver 32.0.32015.7029 the reporter observed 43 affected rays. Each such ray increments both `committed_t_plus_tmin` and the committed-hit mismatch count, and the device test fails. The diagnostic identifies shader-committed hits with the expected identity and candidate distance whose committed distance differs by TMin. The same tolerance applies on every adapter. A synthetic control adds 0.25 to a known hit distance and requires the committed-hit check to reject it.

A cross-check at `1ccc326` on a GTX 1660 SUPER (driver 32.0.16.1074, DXR Tier 1.0, Windows 11 build 26200) and in-box WARP 10.0.26100.9818 (Tier 1.1) found no shader-commit TMin offset on any runnable path (#1011). The GTX 1660 SUPER skipped inline RayQuery at Tier 1.0 and matched all 126 rays on the DispatchRays path with `committed_t_plus_tmin=0`. In-box WARP matched the same 94 hits, 42 shader commits and 32 misses on both the inline and pipeline paths with `committed_t_plus_tmin=0`; the inline run counted 11 tolerated `warp_after_commit` rays. The debug layer was unavailable on every run. This cross-check adds no NVIDIA inline result. The RTX 5080 result above is a separate Tier 1.2 comparison, and the TMin offset has been observed here on the RX 9070 XT inline path. [Cross-check report](https://github.com/R0Y00/Xbox2Windows/issues/1011#issuecomment-7114619229)

### Opacity in DispatchRays

`dxr_opacity_pipeline` traces the same 126 rays with a `lib_6_3` ray generation, any-hit, closest-hit and miss library. Each geometry selects its own hit group record using a TraceRay geometry multiplier of 1. The record provides the geometry index as a local root constant, so the shaders can identify the four geometries on Tier 1.0. AnyHit records each presented geometry and calls `IgnoreHit` when the accept mask rejects it. [Hit group table indexing](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#hit-group-table-indexing) · [Any hit shaders](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#any-hit-shaders)

The CPU checks require the closest committable hit or a legal first hit, verify instance, geometry, primitive and distance, and compare the required and allowed any-hit geometry masks. The pipeline check also rejects another invocation after a shader-accepted first hit. The diagnostic for candidates after an automatic opaque commit is specific to inline RayQuery.

The pipeline matched 94 hits and 32 misses on the GTX 1060 (driver 32.0.15.6094, Windows 10 build 19045.6456) and app-local WARP 1.0.22 with Agility SDK 1.620.2. Both reported zero D3D12 debug warnings and errors. App-local WARP also passed inline RayQuery with three instances of its known first-hit continuation. The GTX 1060 skipped inline RayQuery at Tier 1.0, and Windows 10 in-box WARP skipped both opacity tests because it has no DXR.

Two local shader mutations confirmed that the check detects each failure independently. Accepting every non-opaque candidate changed the first ray from geometry 1 at distance 2 to geometry 0 at distance 1. Flipping the low bit of the geometry index read from the hit record changed that ray's reported geometry from 1 to 0 at distance 2. Both mutations failed on the GTX 1060 and app-local WARP.

```powershell
ctest --preset dev --no-tests=error -R '^dxr_opacity_(pipeline_)?(hardware|warp)$' -V
```

### Desktop checks for the query tail and root flags

On x64 Windows, `desktop_memory_query` observed a 48-byte write from `VirtualQueryEx`. State and protection occupy offsets `0x20` and `0x24`. All 16 trailing sentinel bytes survived. With the caller's zero initialization, even a committed read/write page fails the tail check. The test supplies seven tail bytes after the query and compares the four callers' two predicate shapes on reserved, committed read/write, read-only and no-access pages. It also checks the zero-return path.

`bvh_root_roundtrip` uses a source-built compute shader with a v1.1 root signature. The desktop serializer rejected the added `0x03000000` bits with `E_INVALIDARG`. Clearing just those bits and serializing, decoding and serializing again produced the same bytes as the original control. RTX 2060 and WARP both created the root signature and pipeline from the control shader and recovered signature. Each dispatch matched 257 CPU reference words and preserved 63 guard words. This establishes the desktop behavior for that shader. The two flags' Scarlett semantics remain open.

`desktop_bvh_root_flags` asserts the same three cases with a synthetic v1.1 descriptor and no local inputs: baseline flags serialize, while `0x01000000`, `0x02000000` and the pair each return `E_INVALIDARG` with no blob produced. The suite therefore fails if a future SDK or driver starts accepting either console bit on its own.

### Creating root signatures on a second workstation

The second maintainer workstation ran Windows 26200.9974 with SDK 10.0.26100.0, an RTX 4060 Laptop GPU (driver 32.0.16.1656) and WARP (10.0.26100.9818). At commit `127fa86`, both backends passed 408 differential ray queries with the debug layer enabled. Forced native wave64 skipped on both. The hardware reported 32 native lanes and WARP reported 4. [Hardware report](https://github.com/R0Y00/Xbox2Windows/issues/1#issuecomment-7058907095)

The device probe groups containers in each DLL by the SHA-256 of their serialized root signature (`RTS0`). For one representative per group it deserializes the v1.1 descriptor, changes the flags in a memory copy, and records the serializer HRESULT. For each successful serialization it calls `CreateRootSignature` on each selected device and records that HRESULT separately.

| Local input | Containers / distinct layouts | Serializer and device results |
|---|---:|---|
| BVH builder | 52 / 16 | All 16 returned `E_INVALIDARG` in each of three cases: original flags, clear only `0x01000000`, or clear only `0x02000000`. Clearing both returned `S_OK` for serialization and device creation on both backends for every layout. |
| AMD upscaler | 29 / 3 | All three original layouts returned `S_OK` for serialization and device creation on both backends. |

The observed boundary is root signature creation. The desktop DXIL validator still rejected all 52 BVH programs, and the 29 upscaler containers contained no DXIL. An open binding test is the BVH layout shared by seven containers, including the one at RVA `0x99d60`. A source-built dispatch with CPU-checked readback would extend coverage beyond device creation for this layout.

After the first build with the Python dependencies, run the two source-built controls on WARP:

```powershell
ctest --preset dev -R '^root_device_controls_warp$' -V
```

With local inputs, generate the call inventory first. The runner verifies the DLL hashes and writes per-layout HRESULTs to `out/calls/root-devices.json`. Use `--warp-only` for a software-only report in `out/calls/root-devices-warp.json`.

```powershell
.\tools\dev.ps1
$pkg = 'game-files/<build-directory>'
$python = '.\out\pe-env\Scripts\python.exe'
& $python -B tools/call_evidence.py $pkg
& $python -B tools/probe_root_devices.py $pkg
```

### Dynamic dependencies at startup

Five `GetProcAddress` calls were inspected in the local AMD loader. The `0x10000` descriptor selects the packaged upscaler, whose five exports match. `0x20000` and `0x30000` select `amd_fidelityfx_framegeneration_xbox.dll`, absent from the inspected directory and torrent manifest. The loader checks for an already-loaded module before calling `LoadLibraryA`. Whether the main executable requests frame generation remains unknown. [AMD FidelityFX API](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/main/Kits/FidelityFX/api/include/ffx_api.h)

The executable intake report's `dependency_paths` gives an explanatory path from the entry to each reachable dependency, preferring ordinary imports and marking delay imports and confirmed AMD branches. The AMD branch data applies only to a SHA-256-matched loader. New inventories include System32 forwarder chains. Older inventories provide first-hop evidence only. Dynamic lookups and execution order remain open. With DLLs alone, trace from each one:

```powershell
.\out\pe-env\Scripts\python.exe -B tools/dependency_paths.py out/dependencies/pre-exe.json |
    Set-Content out/dependencies/pre-exe-paths.json -Encoding utf8
```

The conditional FidelityFX routes are pinned to one loader hash. When a reachable image has the loader's basename but a different SHA-256, the report adds an `unverified_loader_hash` finding that names both hashes, instead of returning no conditional edge and exiting successfully. `dependency_paths_controls` checks the pinned hash and a changed one with a synthetic inventory.

```powershell
.\out\pe-env\Scripts\python.exe -B tests/dependency_paths_check.py
```

To rehearse intake with the locally built candidate, keep its report in a separate directory:

```powershell
.\out\pe-env\Scripts\python.exe -B tools/intake_exe.py out/build/dev/intake_candidate.exe --reports out/intake-rehearsal
```

The tool reads the candidate as a file and uses the manifest slot as its graph name. The local rehearsal matched the candidate's SHA-256 to the fixture and preserved all 16 baseline DLL hashes. The fixture supplied ordinary imports by name and ordinal from the missing `intake_provider.dll`, a delay import from the missing `intake_delay.dll`, and an ordinary import of `ffxQuery` from the packaged AMD loader. From that loader, descriptor `0x10000` reached the packaged upscaler. Descriptors `0x20000` and `0x30000` reached the absent frame-generation module conditionally. The independent CTest uses a temporary package without that loader and checks that the report omits the frame-generation branch. The game's startup dependencies still require its own entry image.

## Resource chunks and shader formats

### Relocated resource root

Run `python -B tools/resource_root.py <archive> --offset <byte-offset> --stored <compressed-bytes> --decoded <decoded-bytes>` for one known zlib member. Both sizes must be exact and at most 2 MiB. The output contains the selected root's numeric type hash and byte size. The source-built control runs with `ctest --preset dev --no-tests=error -R '^resource_relocated_root$' -V`.

The measured little-endian PRD0 v0x7a header stores a one-based root block index at byte 32, schema and instance directory pointers at bytes 40 and 56, and their counts at bytes 72 and 76. The reader resolves pointers relative to virtual address `0x50000000` within the decoded member. Each 16-byte instance entry gives a type, length and pointer. Exactly one 32-byte schema entry must have the root type, and its declared structure size must equal the root length. The root and both directories must fit in disjoint spans, and the block-map pointer must not fall within any of these extents. This is a profile of the observed relocated records. The section-based script walker below uses separate LSCH and PSIN tables. [Related metadata row layouts and root selection](https://github.com/dexyfex/CodeWalker/blob/485d56bec00262ed7fa472261cce7bbc6202b96e/CodeWalker.Core/GameFiles/MetaTypes/Meta.cs) · [Observed-version header reference](https://github.com/rpatty-dev/PercyStar/blob/1b710c5349ef1911d94092f19173e9813dfb300c/src/anim.rs#L180-L219)

Three 8 MiB windows per archive (start, middle, end) yielded 556 verified roots of 352 bytes in an animation archive, 189 of 304 bytes in a navigation archive and 43 of 192 bytes in a pack archive. Each archive had a distinct root type across these samples. A further 102 marked members lacked a required in-range extent. The marker alone cannot identify an animation class or establish clip counts or playback behavior.

### State-linked settings in a script archive

`python -B tools/script_state_probe.py path/to/script.rpf --state <label>` checks aligned zlib pages, section lengths through CHKS, schema and array extents, and parent-to-child attribute ranges, and rejects a state node with more than one label-kind `id` attribute or with an `id` attribute of any other kind, as the replay and the native path host do. It emits aggregate counts and at most one matching example. The supported profile has five root arrays. Other layouts and entries outside the recognized zlib framing require separate inspection.

In the local base script archive, 532 children of state nodes carried orbit distance, heading and pitch bounds, blend duration and priority together. Reformatting the stored integers, floats and vectors as decimal strings reproduced all five saved value hashes for each child. This establishes authored camera settings associated with states. Activation conditions, time units, interpolation and native calls still need execution evidence. The source-built regression changes a scalar while preserving its saved hash and separately introduces a parent cycle. Further synthetic trees cover a non-state parent, a missing camera attribute, a root with a parent, a state label that selects nothing, and integer and boolean `id` attributes whose hash equals the label. Such an `id` makes the record malformed, matching `resolve_script_path` (-3) and the `script_dispatch_pairs` regression. [CodeWalker field-name reference](https://github.com/dexyfex/CodeWalker/blob/master/CodeWalker.Core/GameFiles/MetaTypes/MetaNames.cs)

The Windows path replay locates a record by an anchor state ID. It consumes that record's node and attribute rows in memory. After building with the dev preset, run this command with the local base archive:

```powershell
python -B tools/script_path_replay.py path/to/script.rpf --host out/build/dev/script_path_host.dll --record-with bumper_f motorbike ./bumper_f ../bumper_r ../bumper_f car/bumper_f
```

Bare paths begin at the record's `states` container. A leading `./` starts at the active node and `../` moves to its parent. Each input is supplied by the experiment. The native host walks the stored first-child and sibling links, checks reverse links and selects a unique matching child ID. It reports a state whose `id` attribute is not label-kind as malformed; the replay rejects such a record before calling the host. A second lookup uses parent indices to verify the resulting node. A missing or ambiguous input leaves the test host at its last selection. The local motorbike record yields distinct front and rear camera bounds and restores the original bounds on returning to the front. The runtime event that supplies the state ID remains unresolved.

### One synthetic BCPack mode-6 block

The [fixture supplied in #442](https://github.com/R0Y00/Xbox2Windows/issues/442) pins 104 packed bytes and the expected 16-byte BC7 block in `tests/bcpack_mode6_vector.py`. The optional [Leonida reference decoder](https://github.com/timelery/Leonida/tree/03d1e118a676d73aba66b1793cf033f6ad23b2b2/tools) reproduces all 16 bytes in strict mode with an empty provisional set. To repeat the packed-byte comparison, point `BCPACK_REFERENCE_TOOLS` at that revision's local tools directory and run `python -B tests/bc7_blocks_check.py -v`. The normal suite requires no external decoder.

The CPU block test and the D3D12 BC7 draw tests check all 16 RGBA texels against fixed expected bytes. Changing the last selector from 15 to 0 selects the opposite endpoint only for the final texel. This covers a single zero-history synthetic vector. The local BCPack probe does not decode arbitrary BC7 streams.

### Two synthetic BCPack mode-6 blocks

`tests/bc7_mode6_multiblock_check.py` uses the [132-byte synthetic stream supplied by stan495 for #769](https://github.com/stan495/Xbox2Windows/blob/5e1af1695674866a784a80720426a0a5384263b1/tests/bcpack_mode6_multiblock_vector.py). The test builds the expected blocks from two sets of mode-6 endpoints and selectors. It strictly decodes the stream without external history, requires the exact 32 BC7 bytes in left-to-right order and uploads those decoded bytes. The translated copy shader samples the resulting 8×4 texture. The test compares the entire RGBA8 capture with the independent CPU reference derived from the expected blocks.

Use the same pinned Leonida tools as for the single-block check. From the repository root with the Python environment set up:

```powershell
. .\tools\dev.ps1
git init out/Leonida
git -C out/Leonida fetch --depth=1 https://github.com/timelery/Leonida.git 03d1e118a676d73aba66b1793cf033f6ad23b2b2
git -C out/Leonida checkout --detach FETCH_HEAD
$env:BCPACK_REFERENCE_TOOLS = (Resolve-Path 'out/Leonida/tools').Path
cmake --preset dev
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^(bc7_mode6_multiblock_(warp|hardware)|bc7_draw_(warp|hardware)|bc7_block_reference|bcpack_rans_symbols)$' -V
```

The two `bc7_mode6_multiblock_*` tests report a skip when `BCPACK_REFERENCE_TOOLS` is unset. To select a physical adapter by name, set `XBOX2WINDOWS_ADAPTER` before running CTest.

On Windows 11 build 26200.9974 with the debug layer enabled, WARP (Microsoft Basic Render Driver 10.0.26100.9818) and Radeon RX 7900 XTX (32.0.32029.2010) matched all 32 texels with zero channel error, debug errors or debug warnings. The first mismatches were `(0, 0)` after swapping the two blocks and `(7, 3)` after changing the right block's last selector from 15 to 0. Truncation to 131, 66 or 32 bytes failed before upload. The 131-byte case first reported that lane 10 ran out of input at byte 131. The GPU runs used the test change on base `1e8c6a6`.

### Exporting two TTPF entries to DDS

These measurements use Leonida's Python exporter on Windows with Python 3.14.2. Both indices are zero-based and refer to the same local `ttdb_ui.ttpf` pool, 845,115,648 bytes, SHA-256 `dc983ed7f6b78d1693fa5757f17c03bafb790e851594fdf4d412579b61d867f6`. The SHA-256 values in the table cover each complete DDS, including its header. Original pixel colours and coverage across the full set remain unverified. [Measurement request](https://github.com/R0Y00/Xbox2Windows/issues/458#issuecomment-7074603775)

| Pool index | BCPack codec / DXGI format | Dimensions / mips | Export mode | Observed result |
|---:|---|---|---|---|
| 0 | 1 / BC1_UNORM (71) | 512 × 512 / 1 | Strict | Exit 0, 131,220 bytes, SHA-256 `cd39c88921eb395ad633b1bd9ee05a160f693bea8b1e85ad8543bc9cd272022a` |
| 8253 | 7 / BC7_UNORM (98) | 512 × 256 / 1 | Strict | Exit 2, `BC7 signature rank 149 is inferred-only (unconfirmed)`, no DDS written |
| 8253 | 7 / BC7_UNORM (98) | 512 × 256 / 1 | Fitted, history revision `3c997c411666b31372b0d2b82afd5ea669b4d774` | Exit 0, 131,220 bytes, SHA-256 `84e7abf023a568056178f6353d8e86e5d848e536e2aa92f1a028266231b1e15f` |

Run the following PowerShell commands from the Xbox2Windows repository root with a fresh output directory. Set `$pool` to the matching local pool. The layout commands report the dimensions, mip count and the codecs used by each mip. [Pinned exporter](https://github.com/ItsNotRexed/Leonida/blob/b0befb4b88d6640354940117448ea19d52507584/tools/ttpf_mips.py) · [Pinned decoder](https://github.com/ItsNotRexed/Leonida/blob/b0befb4b88d6640354940117448ea19d52507584/tools/bcpack.py)

```powershell
$pool = (Resolve-Path 'game-files/ttdb_ui.ttpf').Path
(Get-Item -LiteralPath $pool).Length
Get-FileHash -LiteralPath $pool -Algorithm SHA256
$out = New-Item -ItemType Directory -Path out/ttpf-dds-measurements
git init --quiet "$out/Leonida"
git -C "$out/Leonida" fetch --depth 1 https://github.com/ItsNotRexed/Leonida.git b0befb4b88d6640354940117448ea19d52507584
git -C "$out/Leonida" checkout --detach b0befb4b88d6640354940117448ea19d52507584
$exporter = "$out/Leonida/tools/ttpf_mips.py"
python -I $exporter layout $pool 0
python -I $exporter export $pool 0 "$out/strict-0.dds"
Get-FileHash "$out/strict-0.dds" -Algorithm SHA256
python -I $exporter layout $pool 8253
python -I $exporter export $pool 8253 "$out/strict-8253.dds"
$LASTEXITCODE
Test-Path "$out/strict-8253.dds"
```

For the fitted run, use the same PowerShell session. The pinned calibration module exposes the BC7 endpoint and index history, including which bits were confirmed and which endpoint slots used a last-resort estimate. The commands serialize these fields to the JSON format accepted by the exporter. [Calibration source](https://github.com/timelery/Leonida/blob/3c997c411666b31372b0d2b82afd5ea669b4d774/tools/bcpack_tables_bptc.py)

```powershell
Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/timelery/Leonida/3c997c411666b31372b0d2b82afd5ea669b4d774/tools/bcpack_tables_bptc.py' -OutFile "$out/history_snapshot.py"
@'
import json
import runpy
import sys
from pathlib import Path

out = Path(sys.argv[1])
table = runpy.run_path(str(out / "history_snapshot.py"))["load_bc7"]()
bc7 = {
    "historyEndpoints": table["history_endpoints"],
    "historyEndpointBits": table["history_endpoint_bits"],
    "historyEndpointConfirmedBits": table["history_endpoint_confirmed_bits"],
    "historyIndices": table["history_indices"],
    "historyIndexBits": table["history_index_bits"],
    "historyIndexConfirmedBits": table["history_index_confirmed_bits"],
    "historyEndpointLastResort": sorted(table["last_resort"]),
}
(out / "history.json").write_text(json.dumps({"bc7": bc7}), encoding="utf-8")
'@ | python -I - "$out"
python -I $exporter export $pool 8253 "$out/fitted-8253.dds" --allow-provisional --history "$out/history.json"
Get-FileHash "$out/fitted-8253.dds" -Algorithm SHA256
```

### Batch export TTPF textures to DDS

`tools/ttpf_batch_export.py` runs batch export over a range of texture indices in a TTPF (RDS5) pool. It enforces strict export by default, optionally retrying with fitted provisional calibration history when `--allow-provisional` is specified. Exports are written to temporary paths and published atomically on completion. Entry status, dimensions, DXGI codec, output DDS file size and SHA-256 are recorded in `manifest.json`. A failed export preserves existing DDS files. Use successful entries in the current manifest to identify this run's output.

```powershell
python -B tools/ttpf_batch_export.py $pool -o out/exported_dds --indices 0,8253 --allow-provisional --history "$out/history.json" --tools-path "$out/Leonida/tools"
```

The batch exporter uses the native BCPack kernels when the selected Leonida tools provide them. The commands below stage the measured revision separately from the reference checkout above. Pass `--require-rust` to check that the library loads before the batch starts. Individual chunks can still use the reference decoder. [Leonida native backend](https://github.com/ItsNotRexed/Leonida/pull/5116)

```powershell
$rustCheckout = "$out/Leonida-rust"
$rustCommit = 'ff5f36bae141b0eb5df4a0029f6fc3a7d4cb111e'
git init "$rustCheckout"
git -C "$rustCheckout" fetch --depth=1 https://github.com/ItsNotRexed/Leonida.git $rustCommit
git -C "$rustCheckout" checkout --detach $rustCommit
$rustTools = "$rustCheckout/tools"
cargo build --release --locked --offline --manifest-path "$rustTools/bcpack_calibration/Cargo.toml" --target-dir "$rustTools/bcpack_calibration/target"
python -B tools/ttpf_batch_export.py $pool -o out/exported_dds --tools-path "$rustTools" --require-rust
```

Add `--calibration path/to/profile.json` to retry strict decode failures using a local version-1 Leonida profile. This option requires NumPy and the calibration library. Use it by itself or with `--require-rust`. The separate legacy history mode uses `--allow-provisional --history`. Output produced by the local profile has `"mode": "fitted"` in the manifest.

### Drawing BC7 textures with a recompiled pixel shader

`linearize_4k_s_tail(tile, width, height, tail_level)` reads a single BC7 level from a shared GFX10 4 KiB mip-tail tile. Supply the decoded tile, the requested pixel dimensions and its zero-based position within the tail. Whole-tile storage and per-slot bounds are checked before reading. [AMD mip-tail address calculation](https://github.com/GPUOpen-Drivers/pal/blob/c5e800072a32f68b6ccc4422936d96167c6e0728/src/core/imported/addrlib/src/gfx10/gfx10addrlib.cpp)

To test the published fixed-symbol rANS hypothesis against an isolated BCPack prefix, run `python -B tools/bcpack_probe.py packet.bin --blocks N` with an independently established block count for that stream. A chunk can contain several streams. A header match checks the predicted byte budget and initial state. A lane that reaches the expected terminal state without refills reports `verified_constant_lane`. When the supplied read masks assign every required refill byte, the probe decodes all symbols using those bytes. A result of `verified_constant_lane_with_supplied_refills` confirms that the lane reaches its terminal state under the proposed assignment. The zero-based `refill_symbol_indices` identify the symbols that consumed the bytes. An empty result leaves the lane models unresolved. This probe produces diagnostics only. [Microsoft BCPack modes](https://learn.microsoft.com/en-us/gaming/gdk/docs/reference/system/dstorage/enums/dstorage_bcpack_mode)

`tools/bc7_tiles.py` converts one padded GFX10 `SW_4K_S` surface of 16-byte BC7 blocks to desktop block rows. The caller supplies the pixel dimensions and the bytes of one mip level. Each 4 KiB tile contains 16 × 16 BC7 blocks. The address equation applies to the standard non-XOR mode. [AMD GFX10 swizzle tables](https://github.com/GPUOpen-Drivers/pal/blob/c5e800072a32f68b6ccc4422936d96167c6e0728/src/core/imported/addrlib/src/gfx10/gfx10SwizzlePattern.h)

The desktop check uploads those rows as `DXGI_FORMAT_BC7_UNORM`, runs the translated copy shader, and checks its floating-point readback against CPU-decoded RGBA values after half-precision export. Its case file starts with `BC7C` and two little-endian 32-bit pixel dimensions, followed by tightly packed BC7 block rows and one reference RGBA8 value per texel. The base texture dimensions must be divisible by four. Each resident mip uses its logical dimensions, up to 4096 on either axis and 4 Mi pixels total, with one reference value per texel. Even a 1×1 mip occupies a complete 16-byte BC7 block.

```powershell
ctest --preset dev -R '^bc7_draw_' -V
.\out\build\dev\offscreen_check.exe hardware --check-bc7 copy.cso base.case capture.rgba
.\out\build\dev\offscreen_check.exe hardware --check-bc7-mip1 copy.cso base.case mip1.case
.\out\build\dev\offscreen_check.exe hardware --check-bc7-tail copy.cso 3 mip3.case mip4.case mip5.case mip6.case mip7.case
```

The tail check uses one logical texture per draw. For a tail whose resident width or height has already reached one texel, specify the original logical dimensions with `--base-size WIDTH HEIGHT` immediately after the first resident mip index. Without this option the command shifts both first-resident dimensions left by that index. For a magnified fallback pixel exactly on a source texel boundary, the readback must match one complete neighboring RGBA texel. The same is accepted, per axis, for the next texel when the pixel center lies up to 1/512 texel below a boundary, tie included. The exact texel is compared first, the last texel has no such neighbor under clamp addressing, and a non-magnified draw accepts no neighbor. Observed: on the Windows 10 in-box WARP 10.0.19041.5794, drawing the 2×1 mip of a 4096×4 texture returned the second texel at 16 pixels of the 4096×4 target (x = 2044 to 2047 in each row), 4 of 2048×2 (x = 1022 and 1023), 1 of 1024×1 (x = 511) and 1 of 512×1 (x = 255), and at none of the smaller targets. The 4×4096 texture gave the same counts along y, as did the same texels uploaded as `R8G8B8A8_UNORM`. WARP 1.0.9 through 1.0.22 and an RTX 3060 on driver 596.36 returned the first texel at all of these pixels. Inferred: these positions are exactly those where rounding the scaled coordinate to the nearest multiple of 1/256 texel, ties upward, reaches the boundary. `D3D12_SUBTEXEL_FRACTIONAL_BIT_COUNT` in `d3d12.h` is 8, which fits this model but does not show how that WARP is implemented. Controls replace single pixels with a texel fetched at its center, so they give the same readback on every adapter: the next texel passes at pixel 2044 of 4096 and at the tie, pixel 255 of 512, and fails at pixels 2043 and 254. A mix of channels from both texels at pixel 2044, the previous texel at pixel 2048 and the first texel at the clamped edge pixel 4095 also fail. It uploads all supplied consecutive levels at their original subresource indices and sets the SRV's `MostDetailedMip` to the first supplied level. The command above takes a 64×64 first resident level and tests viewports from 512×512 down to 4×4. Point sampling at the three largest sizes must reproduce magnified pixels from the 64×64 reference. Each smaller viewport must match its own reference. The test uses committed resources and measures sampling correctness.

To check a proposed first read round in the BCPack probe, append `--prefetch-mask MASK`. Set bit *i* for each of the first eight lanes expected to read a byte in that round. The probe uses the proposed lane order to locate the byte and compares it with the last byte emitted by the reverse encoder. A matching first byte tests a single refill prediction. `--next-mask MASK` supplies the immediately following round in the same way. Each constant lane takes its next refill byte in every round in which its bit is set, so a lane that skips the first round takes its first refill byte from the second. A read beyond the lane's header budget is listed under `unbudgeted_read_offsets` instead of being ignored. Refill positions after these two rounds and the assignment of BC7 block fields require separate evidence.

The default header layout is interleaved at an eight-byte stride. Pass `--lane-stride 6` to read eight contiguous six-byte records, as proposed for BC7 BCPack streams in [#15](https://github.com/R0Y00/Xbox2Windows/issues/15). `--scan-lanes` prints each lane's mode, budget and state bytes without claiming a decode. The option changes only how the 48-byte header is read; the constant-lane model, the variable BC7 lane models and an independently known BC7 block remain separate questions.

`tools/bc7_blocks.py` supplies independently known BC7 blocks for [issue #15](https://github.com/R0Y00/Xbox2Windows/issues/15). `layout(mode, partition)` lists the name, first bit and width of every field in a 16-byte block, with bit 0 as the low bit of the first byte. `unpack(block)` and `pack(fields)` convert between a block and those named values, and `decode(block)` returns sixteen RGBA8 texels. Every bit of the eight valid modes belongs to one field, so `pack(unpack(block))` reproduces any such block. `fixture()` builds a 64×128 surface of 512 blocks. Each field value comes from a SHA-256 digest of the mode, case number and field name. It cycles through the modes and covers every partition, rotation and index selection value. The module contains no BCPack logic. It gives a candidate lane-to-field mapping the complete block rows and field values to compare against. [Microsoft BC7 format](https://learn.microsoft.com/en-us/windows/win32/direct3d11/bc7-format)

```powershell
ctest --preset dev --no-tests=error -R '^bc7_block_reference$' -V
python -B tools/bc7_blocks.py out/bc7_fields.case --fields out/bc7_fields.json
```

The test compares the first bit of each field group with offsets transcribed from the published mode layouts, checks that every anchor texel lies in its subset, and decodes three blocks whose texels are worked out by hand in the test comments. It also decodes the mode-6 blocks of the draw fixture, which builds its own reference texels, and pins SHA-256 digests of the generated block rows and texels. When the optional `texture2ddecoder` package is installed, the test additionally compares 16,897 decoded blocks with that decoder. This comparison is skipped otherwise. The command above writes the fixture in the `BC7C` case layout. The `bc7_draw_warp` and `bc7_draw_hardware` checks also draw the 512-block fixture. WARP 10.0.26100.9818 and an RTX 5080 on driver 32.0.16.1692 each matched the CPU reference across 8,192 texels and 32,768 channels. Flipping the rotation bit of a mode-4 block while retaining the original reference produced the first mismatch at (16, 0).

The optional capture contains verified, top-down RGBA8 pixels converted from the GPU readback. The mip test binds both levels in one texture and renders to a half-size target so implicit derivatives select the lower mip. An original mode-6 fixture varies endpoints by block and indices by texel. The negative control shifts only the lower level's compressed blocks and requires a pixel mismatch. [Microsoft BC7 mode reference](https://learn.microsoft.com/en-us/windows/win32/direct3d11/bc7-format-mode-reference)

### Decoding MDU rANS symbol streams

`tools/bcpack_symbols.py` decodes an MDU entropy stream into per-lane integer
symbols, consumed byte count, clock count and per-lane fragment/final-state
diagnostics. Supply independently established symbol widths and a symbol count
per lane. The implementation uses the public cycle-level entropy reference from
[bcpack-rs by Ahmet Dedeler](https://github.com/Ahmet-Dedeler/bcpack-rs/tree/5bf5cc56ed56f1ababf1f9592300bd182ff90bd8),
with Microsoft's MIT-licensed model tables and per-lane decoder semantics.
[Source revision, extraction and licenses](../tools/_bcpack_rans/README.md).

```powershell
python -B tools/bcpack_symbols.py stream.bin --alphabet-bits 8 8 5 6 5 5 6 5 8 8 8 8 --symbols 64
ctest --preset dev --no-tests=error -R '^bcpack_rans_symbols$' -V
```

The widths above describe the reference's twelve BC1 lanes; they are actual bit
widths, not zero-based hardware alphabet indices. The wrapper accepts 1–16 lanes,
widths of 1–9 bits, at most 1,048,576 total symbols and a nonempty input of at most
2 MiB. It reads a bounded stream, handles changing model headers, alphabet
narrowing, fragment flushes (restoring at most two omitted low zero bytes), long
fragment lengths and shared-stream refills with the reference's eight-byte feed window and lockstep symbol outputs. All lanes
must terminate legally and produce exactly the requested count. Zero padding
may follow a completed stream; non-zero trailing bytes fail. Truncated streams,
undefined alphabet selections and illegal terminal states also fail.

Run `python -B tests/bcpack_symbols_check.py` on Linux or Windows without extra
packages. The controls compare all 9,168 symbols of two unchanged, Rust-generated
synthetic streams (12 × 64 and 12 × 700), including multiple fragments and more
lanes than the feed window. A separate reverse-equation encoder checks all 144
alphabet/model combinations, long headers, narrowed alphabets and flushed
fragments. Table checks pin the upstream canonical digest and test inverse
interval boundaries. Rejection controls cover truncation, trailing bytes, wrong
counts, invalid headers, terminal states and argument limits. The existing
mode-6 packet from [#442](https://github.com/R0Y00/Xbox2Windows/issues/442) also verifies all 32 symbols across 16 lanes and consumes its full 104 bytes.
Those expected symbols were cross-checked with
[Leonida revision 6a6704d](https://github.com/timelery/Leonida/tree/6a6704df1592e1dc4a3215ddf1423a78adb52045),
which reconstructs the known BC7
block exactly without provisional entries. Our test checks the entropy symbols
only. That case requires the bounded flush-zero extension to the vendored
reference; it fails without it. The CLI has a successful JSON output case and
a nonzero-exit diagnostic case.

Observed on Linux with Python 3.14.4: all eight entropy test methods passed.
On Windows 11 build 26300.9974 with Python 3.12.8, the CTest selection
`^bcpack_(rans_symbols|prefix_and_mip_tail)$` passed 2/2 tests after configuring
with `INTAKE_PYTHON` pointing to that interpreter. These checks use no GPU.

This adds entropy decoding. BC1 endpoint/codebook reconstruction, BC7 lane-to-field
mapping, archive stream discovery and rendered pixels remain separate work.
The existing prefix probe keeps its diagnostic behavior. No local archive or
texture payload is used by these checks.

### Reading logical ranges and probing structured resources

`tools/paged_resource.py` reads a logical byte range from an open archive and a supplied segment map. Each segment gives a physical offset, stored byte count excluding padding, decoded byte count, and codec. Raw and zlib segments may be mixed. The reader verifies the complete compressed stream and decoded size before returning bytes from it. Reads that touch an unknown codec fail. Segments after it remain independently addressable.

Run `python -B tools/texture_descriptors.py <archive.ttpf>` to group descriptors in the first raw ETPF table by width, height, DXGI format number and mip-marker byte. The reader checks stored and logical bounds and reads up to 1 MiB of descriptors in batches of at most 4 KiB. It leaves texture payloads unread. The marker alone does not identify a resident mip. [Published descriptor layout](https://github.com/rpatty-dev/PercyStar/blob/1b710c5349ef1911d94092f19173e9813dfb300c/src/tex.rs#L748-L785)

Add `--raw-page-map INDEX` to report the logical and file offsets of a zero-based descriptor's first 4 KiB page. The interval must be raw. Equal stored and logical lengths require no padding check. For unequal lengths, the excess stored bytes must match the 12-bit trailing-byte count from the starting boundary. The command reads this suffix (up to 4,095 bytes) and verifies that each byte is zero. The command checks that boundary positions increase strictly, reads the boundary table in batches of at most 4 KiB, and accepts tables up to 1 MiB. It reads no texture payload.

Use `--zlib-segment INDEX` for a descriptor in a codec-8 interval. This mode reads and decompresses the selected interval, limiting both stored and decoded sizes to 1 MiB. It verifies the zlib checksum and end of stream, checks the decoded length against the declared extent, and requires any trailing bytes to be zero. The report contains the fields for a `Segment`, its logical start, and the descriptor page's offset within it. Decoded bytes are not included in the report.

Run `python -B tools/texture_descriptors.py --lookup-key 0x0123456789abcdef first.ttpf second.ttpf` to search the parallel little-endian 64-bit key tables in ETPF version 0x27a. Each match lists the archive path and zero-based descriptor index, in input order and then index order. Repeated keys produce separate matches. An absent key produces an empty list. The reader scans at most 512 KiB of keys per archive in batches of up to 4 KiB and leaves descriptor bodies and texture payloads unread. [Key table layout](https://github.com/holdmysocks/SixNG/blob/2f672555819f96993adeed3273653ad53eeb1d81/crates/sixng-core/src/ttpf.rs#L112-L123)

Use `python -B tools/texture_descriptors.py --descriptor INDEX archive.ttpf` to inspect an individual entry, for example after `--lookup-key`. The JSON gives the zero-based index, width, height, numeric DXGI format, mip-marker byte and logical offset of the first 4 KiB page. The command reads the bounded headers and the selected 16-byte record. It leaves other descriptors, key tables and texture payloads unread.

`tools/section_probe.py` samples up to 8 MiB at the start, middle and end of each input. It looks for 16-byte-aligned zlib streams, with stored and decoded sizes capped at 256 KiB. A section record must start and end with the requested tags, and its big-endian section sizes must exactly cover the decoded buffer. Streams cut off by a window or size cap are omitted. Counts establish record boundaries within sampled regions. Interpreting payload fields or validating a checksum inside a section requires its format.

```powershell
python -B tools/section_probe.py --first PSIN --last CHKS "game-files/<build-directory>/levels.rpf"
ctest --preset dev -R resource_range_controls -V
```

### AWC sound-effect format metadata

Run `python -B tools/awc_metadata.py <archive> --offset <byte-offset> --size <byte-count>` with a known ADAT or TADA bank range. The offset and size accept decimal or `0x` hexadecimal integers. The tool reads the stream table and each stream's tag descriptors, checks their extents against the supplied range, and reads the first 32 bytes of each `0x76` format tag or the first 20 bytes of each `0xFA` format tag. Its JSON groups streams by numeric codec ID and sample rate. For sound-effect banks, the sample count in each group is the sum of the counts in its format tags. Container discovery and playback require separate evidence.

The reader supports version 1 sound-effect banks in little-endian (`ADAT`) or big-endian (`TADA`) byte order with flag words `0xff000001` or `0xff020001`, at most 4,096 streams, and nonoverlapping tags. Each stream must have one nonempty data tag (`0x55`) and exactly one format tag, either `0x76` or `0xFA`. Both give the sample count at offset 0 and the sample rate at offset 8; the codec ID is at offset 28 in `0x76` and at offset 19 in `0xFA` ([lines 488–527](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L488-L527)). The public reader keeps the last format tag of a sound; this tool rejects a sound with both. The stream's high three bits give its tag count. Each 64-bit tag descriptor encodes its type, byte count and bank-relative offset. [Public AWC format reader](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c)

Three locally inspected banks contained 563, 603 and 176 streams. All 1,342 format tags recorded 48,000 Hz and codec ID 11. The source-built fixture uses two rates and two codec IDs, verifies grouping when format and audio tags are out of physical order, and fails on any read outside the metadata. Run `ctest --preset dev --no-tests=error -R '^audio_awc_metadata$' -V`.

The same command also reads streamed (music) banks. The public reader treats a bank as streamed when the first stream ID is 0 ([`awc.c` line 379](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L379)). That stream's music header tag (`0x48`) gives the block count, a `block_chunk` value and one 16-byte record per channel with its ID, sample count, sample rate and codec ID ([lines 434–486](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L434-L486)). The JSON adds `"layout": "music"` and a `music` object with `channels`, `block_count` and `block_chunk`. Here `stream_count` counts stream-table entries, including the ID-0 stream. Each format group counts channels with the same rate and codec, and its `samples` field is the largest channel sample count in that group. The public reader keeps one maximum over all channels instead ([lines 480–481](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L480-L481)). The public reader assigns `block_chunk` to its `full_block_size` (lines [92](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L92) and [195](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L195)). Its meaning was not measured here, and the tool does not compare it with the data tag. The tool reads the header, stream table, the ID-0 stream's tag descriptors and the music header. It does not bound or read the other streams' tags. The public reader takes the music metadata only from the ID-0 stream's tags ([lines 414–422](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L414-L422)). For Vorbis music banks (codec ID 8) it also reads the other streams' `0x7F` setup tags to decode audio ([lines 564–595](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L564-L595)), which an inventory does not need. It notes that some banks carry a useless sound-effect header or a separate cloned data chunk on the other streams ([lines 349–377](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L349-L377)).

Music banks must use the flag words `0xff000001` or `0xff020001` and have at most 32 stream-table entries, the limit the public reader applies to streamed banks ([lines 384–387](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L384-L387), [`AWC_MAX_MUSIC_CHANNELS`](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc_streamfile.h#L6)). The ID-0 stream needs exactly one music header and one nonempty data tag, its tags, including empty ones, must lie between the declared header size and the end of the supplied range, and they must not overlap. The bank must declare 1 to 32 channels, a music header of at least 12 bytes plus 16 bytes per channel, a nonzero block count and a nonzero `block_chunk`, and all channels must share one codec ID. These rules are stricter than the public reader in the following ways: its header parser ignores the declared header size and does not check for a tag that extends past the supplied range, more than one music header or data tag (it keeps the last one), a repeated ID 0, an empty data tag (it rejects only a missing data offset, [lines 597–600](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L597-L600)), overlapping tags, a channel count of 0, a music header shorter than 12 bytes or than its channel records, a zero block count or a zero `block_chunk`, and skips the codec comparison for a codec ID of 0 ([line 475](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L475)). The public reader also accepts the flag bits `0x00010000`, `0x00040000` and `0x00080000` ([lines 328–346](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L328-L346)), which this tool rejects. In one respect the tool is more lenient: the public reader rejects `0x76` and `0x7F` tags on the ID-0 stream of a streamed bank ([lines 508–534](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L508-L534)), while this tool only bounds them and does not read them. Banks outside these rules fail with a specific message. No local music bank was inspected for this change. The source-built fixture has four channels at two rates, more channels than stream-table entries and ignored tags on other streams. The tests accept the 32-stream and 32-channel limits and tags at the range boundaries, and include a negative control for each rule and for each rejected flag bit. Run `ctest --preset dev --no-tests=error -R '^audio_awc_metadata$' -V`. Big-endian `TADA` banks are read with the same layout and rules; the public reader's header parser switches only its 16-, 32- and 64-bit reads ([lines 301–319](https://github.com/vgmstream/vgmstream/blob/24ffd6ce3e1f34fdebf9f5316d18ad2e162838ce/src/meta/awc.c#L301-L319)). The `TADA` path is derived from that source and the synthetic fixtures; no `TADA` bank was inspected. The flag bits the public reader also accepts stay rejected in both byte orders.

### Source-built binding, wave and texture checks

`dual_uav` dispatches through two root UAVs with a count supplied as a root constant. The CPU checks the input, active output and unwritten tail over seven counts. Decreasing the count from 129 to 128 leaves word 128 untouched. The wave tests compare a full-group mirror with a half-local mirror at lanes 31 and 32 and check an inclusive sum across both halves. With native wave64 support they also check a native mirror using the lane assignment returned by the GPU.

`texture_mips` writes an original RGBA8 checker pattern in padded 4×4 Morton tiles, decodes it into the device's copy footprints and point-samples every texel through a texture SRV. It checks all 148 texels of a 13×9 texture and its three smaller mips, plus a guard word after each level. Morton order is the synthetic input convention. Establishing a particular title's texture format and addressing mode requires separate evidence.

```powershell
ctest --preset dev -R 'dual_uav|texture_mips|bvh_waves|bvh_wave64' -V
```

### Optional compressed-record inventory

Run the following after building the native validator. The optional pass scans named records in the shader archive, decodes one raw-DEFLATE stream at a time and writes only aggregate metadata to the resource report. A candidate must fit the file, have matching stored sizes, reach the stream end, match the declared output length and contain a bounded DXBC section table with a DXIL program header. The pass caps stored and decoded spans at 8 MiB and 16 MiB. It records shader stages, section counts, LLVM bitcode framing, unused bytes at stream end and rejection reasons. The native validator samples the first accepted container per stage. These are content-based counts and depend on the recognized record format. [External record-format reference](https://github.com/ItsNotRexed/Leonida/blob/56f023a10a726da20702d4e121656ceda2391d93/tools/shader_carve.py)

Use `tools/extract_shaders.py --length-prefixed-names` with archives whose record names begin with a one-byte length that includes the NUL terminator. A printable length byte can also be a valid first character of a prefixless name. The extractor keeps the full printable run by default.

```powershell
python -B tools/resource_inventory.py "game-files/<build-directory>" --scene-records --scene-validator out/build/dev/root_probe.exe
ctest --preset dev -R 'compressed_scene_metadata|static_exe_intake|desktop_memory_query|era_heap_failure' -V
```

The synthetic intake test compares the candidate's direct unresolved imports against a DLL baseline and against a second baseline with a peer caller. Each `entry_import_gaps` item records ordinary or delayed binding and whether the same module/symbol pair was already unresolved. Transitive and conditional routes remain in `dependency_paths`. The memory tests check a real one-byte Windows query failure and unsupported heap attribute bits in combination with a supported alignment.

### Raw archive scan

After building, use the source-built pixel shader as a known DXIL control.

```powershell
python -B tools/resource_inventory.py "game-files/<build-directory>" --shader-control out/build/dev/triangle_ps.cso
```

`out/resources/<build-directory>.json` records install chunks, flags, byte counts and missing layout destinations. It records file-header signatures for the build and scans all raw bytes in `appdata0.rpf`, `common.rpf` and `shaders.rpf` for format markers. Candidate DXBC containers are checked for length, section boundaries and DXIL program headers. The synthetic `dxcontainer_part_offsets` regression verifies that part-table entries can be enumerated independently of the physical layout, while rejecting overlapping, out-of-bounds, or duplicate-FourCC part ranges. Both the part map and DXIL scanner reject repeated FourCC tags even when those parts are physically separate; the regression includes a valid framed DXIL control to distinguish duplicate tags from missing shader payloads. It checks structural framing, not shader execution. `sample_streams` covers at most eight qualifying zlib streams per file within the first 4 MiB. Plaintext stays in memory. The sample cannot characterize the rest of the archive. Installation order and runtime I/O need independent checks. [DXIL container format](https://github.com/microsoft/DirectXShaderCompiler/blob/main/docs/DXIL.rst)

### Ballot bit extraction in the named SPIR-V bridge

The HLSL bridge lowers a subgroup ballot read to a dynamic DWORD selection and a bit
test in that word. The source-built fixture supplies four distinguishable words and
reads all 128 positions in the resulting logical vector. This checks the mask layout
independently of the desktop device's wave size. On RTX 4060 Laptop and WARP, both
readbacks matched 128 output words and 192 guards. Exchanging expected positions
31 and 32 produced exactly two mismatches on each adapter.

With SPIRV-Tools and SPIRV-Cross available at configure time, run
`ctest --preset dev --no-tests=error -R spirv_ballot_extract -V`.

### Ordinary and comparison sampler views in the SPIR-V bridge

The named profile can use one indexed sampler table for ordinary samples and
depth-reference operations. In mixed modules SPIRV-Cross infers one comparison
type for that table. The bridge keeps ordinary accesses at set 0, binding 1.
For each depth-reference use it loads the same descriptor index from set 3,
binding 1. The host must populate both views from the guest sampler descriptors,
using ordinary filters in the first and comparison filters and the guest
comparison function in the second. Both operations retain their texture and
coordinates. Set 3 must be free. The supported path has a direct access chain,
sampler load and sampled image. Decorated sampler paths require separate support.

With SPIRV-Tools and SPIRV-Cross installed, run:

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^spirv_sampler_views_' -V
```

The fixture compiles both shaders at Shader Model 6.0 and dispatches the compute
shader for readback. The compiled pixel shader uses implicit LOD and the
compute shader uses explicit LOD. Both compare against a depth reference.

On Windows 10 build 19045.6456, the GTX 1060 (32.0.15.6094) and in-box WARP
(10.0.19041.5794) matched all four readback words in each of the three sampler
orders. Sampling returned 0.25 and 0.75, and comparison against 0.5 returned 0
and 1. Reversing either pair of wrap/clamp descriptors exchanged only that
pair's results. App-local WARP 1.0.22 produced identical results with the
SM 6.0 and SM 6.6 compute shaders. All these runs reported zero D3D12 warnings
and errors. The original SM 6.6 readback on RTX 4060 Laptop and WARP produced
the same values.

### Standalone comparison-sampler split in the SPIR-V bridge

When one standalone SPIR-V sampler variable feeds both depth-comparison and ordinary
sampling, SPIRV-Cross declares a `SamplerComparisonState` and passes it to
`SampleLevel`, which DXC rejects with
`no matching member function for call to 'SampleLevel'`. The indexed sampler table
above is handled by the sampler views. For a standalone sampler, the opt-in
`tools/spirv_sampler_split.py` keeps the comparison sampler at its binding and moves
only the ordinary uses to a new sampler at a binding the host supplies with
`--ordinary-binding`. That binding must be free in the same descriptor set. The pass
prints the `descriptor_set`, `comparison_binding` and `ordinary_binding` mapping for
the host.

The pass first looks for a directly loaded standalone sampler that feeds both kinds of
sampling. Modules without one are returned byte-for-byte unchanged. That includes the
indexed table above and unrelated samples on other samplers. For the sampler it splits,
the profile covers direct, non-array float32 `Texture2D` sampling in one compute entry
point at a constant LOD of zero. Other uses of that sampler, an occupied binding, a
nonzero LOD and more than one mixed sampler are rejected with a specific diagnostic.

The [source-built fixture](../tests/spirv_window/shared_sampler.spvasm) compares
references 0.25 and 0.75 against depth 0.5 with point-clamp `LESS_EQUAL`, and
samples an ordinary texel of 0.375. The test first checks that DXC rejects the
unsplit module. It then splits the module to ordinary binding 5, validates it for
Vulkan 1.2, cross-compiles for SM 6.0 and compiles it as `cs_6_0`. The original
`cs_6_6` readbacks on RX 9070 XT and WARP returned 1.0, 0.0 and 0.375 and left
all 13 guard words unchanged.
A reference claiming that the second comparison passed produced exactly one
mismatch, at word 1, on each adapter. Two descriptor controls change one static
sampler at a time. Switching s2 to `GREATER` exchanged only the two comparison
results. Clamping the ordinary sampler to `MinLOD` 1 changed only the ordinary
sample, to 0.625 from the value texture's second mip. The occupied-binding,
nonzero-LOD, already-separated and passthrough controls run without a GPU.

On Windows 10 build 19045.6456, the GTX 1060 (32.0.15.6094), in-box WARP
(10.0.19041.5794) and app-local WARP (1.0.22) matched all 16 words in each
SM 6.0 reference case. The wrong reference, flipped comparison and ordinary
mip clamp produced the expected differences at word 1, words 0–1 and word 2,
respectively. All runs reported zero D3D12 warnings and errors. App-local WARP
returned the same words with the original SM 6.6 build.

```powershell
ctest --preset dev --no-tests=error -R '^spirv_compare_sampler_' -V
```

### DXC compile time on a mixed-sampler compute shader

The replay profile in [#676](https://github.com/R0Y00/Xbox2Windows/issues/676) compiles
mixed-sampler compute shaders with DXC, and
[#775](https://github.com/R0Y00/Xbox2Windows/issues/775) recorded compile timeouts under
a 90 s policy. The [compile-time tool](../tools/dxc_compile_time.py) generates HLSL from
the shape of the named sampler fixture and times one DXC run per variant. It uses only
the Python standard library and an external DXC executable. It is not registered in CI
and holds no timeout or threshold of its own: `--timeout` is supplied by the caller and
is a local policy value.

The generated sources keep the fixture's declarations and its two control stores
verbatim, so words 0-3 of the UAV are identical in every variant. Each family changes
one property:

- `control`: the emitted fixture source unchanged (685 bytes with LF line ends, one
  texture, both sampler views and a raw UAV).
- `samples_N`: N additional independent `SampleLevel` stores with flat control flow.
- `branches_B`: B extra if/else regions with observable stores; sample count fixed.
- `samplebranch_N`: one `SampleLevel` inside each divergent region, the shape of the
  blocked compute records.

Measured on 2026-11-22 with DXC 1.9.2609.5 (`01b62ad47`), Windows 11 build 26300,
Ryzen 5 5500 (6C/12T, 15.9 GiB) and one compile at a time. Baseline command, with the
profile's SM 6.6 and native 16-bit settings (the fixture itself declares no 16-bit
types):

```powershell
# representative family with the 90 s replay policy
out\pe-env\Scripts\python.exe -B tools\dxc_compile_time.py --dxc <path-to-dxc.exe> --variants control,samples_1024,branches_1024,samplebranch_4096,samplebranch_8192 --repeat 3 --timeout 90 --out out\dxc-compile-time\measurements.csv
# pass attribution from a separate diagnostic run
out\pe-env\Scripts\python.exe -B tools\dxc_compile_time.py --dxc <path-to-dxc.exe> --variants samplebranch_4096,samplebranch_8192 --ftime-report --timeout 300 --out out\dxc-compile-time\passes.json
# optimized-off twin of the timeout case
out\pe-env\Scripts\python.exe -B tools\dxc_compile_time.py --dxc <path-to-dxc.exe> --variants samplebranch_16384 --extra-arg=-Od --timeout 300 --out out\dxc-compile-time\od.csv
```

`--repeat` records the median of the completed runs. A variant that times out, crashes
or reports a diagnostic stops repeating and is identified by its `outcome` column; the
wall time of a timeout row is the moment the monitor killed DXC, not a compile time.
`--out` writes CSV, or JSON when the file name ends in `.json`; the JSON record also
holds the per-run values, the exact command and the DXC version. `--work-dir` keeps the
generated HLSL and DXIL, and `--list-variants` prints the generated names and source
sizes without compiling.

Key measurements, wall medians:

| variant | HLSL bytes | DXC wall s | peak MiB | outcome |
|---|---:|---:|---:|---|
| control | 685 | 0.023 | 16.6 | DXIL |
| samples_1024 | 107,889 | 0.314 | 36.7 | DXIL |
| samples_16384 | 1,735,137 | 7.804 | 313.0 | DXIL |
| branches_1024 | 148,909 | 0.333 | 41.0 | DXIL |
| branches_16384 | 2,465,494 | 13.904 | 369.1 | DXIL |
| samplebranch_4096 | 968,577 | 8.083 | 171.3 | DXIL |
| samplebranch_8192 | 1,946,457 | 33.726 | 325.2 | DXIL |
| samplebranch_12288 | 2,929,337 | 90.044 | 438.3 | timeout (marginal) |
| samplebranch_16384 | 3,915,833 | 90.046 | 625.8 | timeout |
| samplebranch_16384, `-Od` | 3,915,833 | 9.604 | 442.6 | DXIL |

Log-log slopes of wall time against HLSL bytes are 1.10 for `samples_N`, 1.21 for
`branches_B` and 1.51 for `samplebranch_N` between 256 and 8192. The pair that separates
compile from timeout is `samplebranch_8192` against `samplebranch_16384` with the same
generator and command: the first compiled in every run (27.9-46.9 s across four runs,
33.73 s in the repetition batch), the second exceeded 90 s every time and needed
193.12 s with a raised limit. `samplebranch_12288` is the threshold bracket: 67.77 s in
one session, 90.04 s timeout and 97.68 s with a raised limit in another. Times on the
largest variants vary by 20-45 % across sessions, so an outcome near the threshold can
change in either direction. A verification run of the tool on the recording machine
regenerated the table's source sizes and the DXIL sizes of `control`,
`samplebranch_1024` and `samplebranch_4096` byte for byte, and their wall times were
0.024, 0.914 and 8.515 s.

Where the time goes. `-ftime-report` from a separate diagnostic run with the same flags
(its "Total" counts LLVM/DXIL passes only) puts 81.8 % of the pass time in the two
"Global Value Numbering" instances at `samplebranch_8192` (14.94 + 7.42 of 27.34 s);
the shares are 70.5 % at `samplebranch_4096` and 48.0 % at `branches_16384`, where "DXIL
simple GVN hoist" adds 9.5 %. `-Od` removes 25 % (`samples_16384`), 64 %
(`branches_16384`), 73 % (`samplebranch_4096`), 84 % (`samplebranch_8192`) and more
than 95 % (`samplebranch_16384`, 9.60 s instead of 193.12 s) of the wall time, and `-Vd`
saves 4 % at `samples_16384`; validation and the frontend are not the bottleneck. The
measured observation is that the cost grows with the coexistence of many sample
operations and many store-bearing control-flow regions in one function. Why the two GVN
instances do not scale is a hypothesis about DXC internals and is not established here.

Compiling and dispatching are separate checks; the tool only compiles. The existing
oracle `offscreen_check <adapter> --spirv-sampler-views <cso>` was run on the compiled
variants and compares their four control words with the fixture:

- the RTX 4060 (driver 32.0.16.2124) passed for control, `samples_1024`,
  `samples_16384`, `branches_1024`, `branches_16384` and
  `samplebranch_1024/4096/8192`;
- WARP (10.0.26100.10068) passed for the smaller subset: control, `samples_1024`,
  `branches_1024` and `samplebranch_4096`;
- a negative control with one UV constant changed failed on both adapters, and every
  emitted CSO was accepted by the DXC validator from the same package.

`samplebranch_12288` reached pipeline creation on the RTX 4060. The call did not return
within about 9 minutes for its 2.77 MB DXIL, so there was no dispatch. This separate
pipeline measurement is outside the DXC time above. The DXIL validates with `dxv`
and keeps the control stores checked by the smaller variants.

The archive-replay attribution in
[#775](https://github.com/R0Y00/Xbox2Windows/issues/775) remains open. This family
reaches the policy timeout only at multi-MB HLSL; the fixture-scale sources compile in
about 0.02 s and the worst measured case below 0.5 MB is 2.54 s. On a separate
17,188,611-byte archive-derived input, an instrumented default-optimization run last
entered `loop-rotate`. That trace locates the observed stall under instrumentation.
Uninstrumented pass-time totals and attribution across the replay cohort remain open.
The GPU check above covers the public fixture's four control words. [Archive trace](https://github.com/R0Y00/Xbox2Windows/issues/775)

#### Minimal pair: conditional UAV stores without sampling

Reducing `samplebranch_N` one element at a time showed that neither the sampler views
nor the sample operations are needed for the GVN cost. Removing the samples, the else
arm, the static `gl_WorkGroupID` reads or the shared condition operand left the
superlinear growth in place, and so did wrapping the regions in a loop or a
`for (;;) switch` state machine (the loop forms are slower again; they are not part of
the tool). Making the stores unconditional removed it. The tool has two families for
this pair, and the source of each differs from the other only by the branches:

- `condstore_N`: N stores `if (((gl_WorkGroupID.x + k) & 1u) != 0u) _4.Store(...)`
  into the raw UAV, with the samplebranch condition and no sampling beyond the two
  control stores;
- `stores_N`: the same N stores without their `if`.

Measured on 2026-11-23 with two pinned DXC builds, on a Ryzen 7 3700X (8C/16T, 32 GiB)
running Windows 10 build 19045, one compile at a time. The first build is DXC
1.7.2308.16 (`52da17e29`) from Windows SDK 10.0.26100. The second is DXC 1.9.0.5512
(`01b62ad47`) from LunarG Vulkan SDK 1.4.363.0. The maintainer's
1.8.2502.11 build was not available on this machine. The flags are the same as the
baseline above. Every row reports the compiler only. There was no dispatch or PSO
creation.

```powershell
out\pe-env\Scripts\python.exe -B tools\dxc_compile_time.py --dxc <path-to-dxc.exe> --variants control,stores_1024,stores_4096,stores_16384,condstore_1024,condstore_2048,condstore_4096 --repeat 3 --timeout 90 --out out\dxc-blowup\family.json
out\pe-env\Scripts\python.exe -B tools\dxc_compile_time.py --dxc <path-to-dxc.exe> --variants condstore_8192,condstore_16384 --timeout 90 --out out\dxc-blowup\policy.json
out\pe-env\Scripts\python.exe -B tools\dxc_compile_time.py --dxc <path-to-dxc.exe> --variants condstore_16384 --timeout 900 --out out\dxc-blowup\raised.json
out\pe-env\Scripts\python.exe -B tools\dxc_compile_time.py --dxc <path-to-dxc.exe> --variants condstore_4096,condstore_16384 --extra-arg=-opt-disable --extra-arg=gvn --timeout 300 --out out\dxc-blowup\nogvn.json
out\pe-env\Scripts\python.exe -B tools\dxc_compile_time.py --dxc <path-to-dxc.exe> --variants stores_4096,condstore_4096 --ftime-report --timeout 300 --out out\dxc-blowup\passes.json
```

Rows with three repetitions give the median wall time. Every other row is a single run.

| variant | setting | HLSL bytes | 1.7.2308 wall s | 1.7.2308 peak MiB | 1.7.2308 outcome | 1.9.0.5512 wall s | 1.9.0.5512 peak MiB | 1.9.0.5512 outcome |
|---|---|---:|---:|---:|---|---:|---:|---|
| control | default | 685 | 0.027 | 13.9 | DXIL | 0.022 | 15.1 | DXIL |
| stores_1024 | default | 41,645 | 0.170 | 18.9 | DXIL | 0.130 | 20.4 | DXIL |
| stores_4096 | default | 164,525 | 0.712 | 33.3 | DXIL | 0.583 | 35.2 | DXIL |
| stores_16384 | default | 663,813 | 2.598 | 90.2 | DXIL | 2.519 | 91.4 | DXIL |
| condstore_1024 | default | 107,095 | 0.948 | 51.5 | DXIL | 1.278 | 54.2 | DXIL |
| condstore_2048 | default | 214,615 | 3.259 | 141.2 | DXIL | 5.045 | 145.8 | DXIL |
| condstore_4096 | default | 429,655 | 12.993 | 480.1 | DXIL | 23.906 | 485.3 | DXIL |
| condstore_8192 | default, 90 s policy | 859,735 | 68.794 | 1,806.0 | DXIL | 90.093 | 1,807.8 | timeout |
| condstore_16384 | default, 90 s policy | 1,734,047 | 90.269 | 2,548.6 | timeout | 90.261 | 2,735.4 | timeout |
| condstore_16384 | default, 900 s limit | 1,734,047 | 382.221 | 6,917.9 | DXIL | 645.364 | 6,918.6 | DXIL |
| condstore_4096 | `-opt-disable gvn` | 429,655 | 1.394 | 68.5 | DXIL | 1.411 | 72.2 | DXIL |
| condstore_16384 | `-opt-disable gvn` | 1,734,047 | 7.138 | 221.3 | DXIL | 6.351 | 228.7 | DXIL |

Wall time and peak working set grow about fourfold per doubling of `condstore_N` from
2048 upwards: 3.5-4.7x in time and 3.4x in memory. `stores_N` grows linearly. The
policy timeout is reached at 0.86-1.73 MB of HLSL, against 2.9-3.9 MB for
`samplebranch_N` above. `condstore_16384` peaks at 6.9 GB. In the `-ftime-report`
diagnostic run of `condstore_4096`, Global Value Numbering takes 93.1 % of the
reported pass time on 1.7.2308 (12.84 of 13.80 s, one instance) and 95.3 % on
1.9.0.5512 (19.47 of 20.44 s, two instances). For `stores_4096` the largest pass is
instruction combining (34.9 % and 47.0 %), and GVN is not among the top two passes.
DXC's `-opt-disable gvn` toggle compiles `condstore_16384` in 6.4-7.1 s instead of
382-645 s and keeps peak memory near 225 MiB. This is a separately labelled
diagnostic setting. Its DXIL has not been checked on a GPU, and it is not proposed as
a replay flag.

The merged `samplebranch_N` variants were rerun on the same machine with the same two
builds, as an independent timing of the table above. The default rows are medians of
three runs under the 90 s policy. The `-opt-disable gvn` rows are single runs on the
same generated inputs, with a 300 s limit.

| variant | setting | HLSL bytes | 1.7.2308 wall s | 1.7.2308 peak MiB | 1.9.0.5512 wall s | 1.9.0.5512 peak MiB | outcome |
|---|---|---:|---:|---:|---:|---:|---|
| control | default | 685 | 0.021 | 14.0 | 0.023 | 15.1 | DXIL |
| samplebranch_4096 | default | 968,577 | 7.720 | 154.6 | 10.602 | 170.4 | DXIL |
| samplebranch_8192 | default | 1,946,457 | 25.481 | 290.2 | 25.283 | 322.3 | DXIL |
| samplebranch_4096 | `-opt-disable gvn` | 968,577 | 6.650 | 137.6 | 5.181 | 142.4 | DXIL |
| samplebranch_8192 | `-opt-disable gvn` | 1,946,457 | 13.247 | 257.2 | 7.883 | 266.0 | DXIL |
| samplebranch_16384 | `-opt-disable gvn` | 3,915,833 | 28.021 | 496.5 | 17.951 | 511.4 | DXIL |

Disabling GVN alone brings `samplebranch_16384`, which timed out above, to 18-28 s.
That is still slower than `-Od` above, so other passes contribute at this size.

`-print-before gvn` (1.9.0.5512 only; 1.7.2308 rejects the option) on `condstore_4`
shows the shape GVN receives. Each conditional block re-creates the UAV handle with
`dx.op.createHandleForLib` and `dx.op.annotateHandle` before its `rawBufferStore`,
and the join blocks between them hold no memory operation. A plausible mechanism is
that each handle call starts a non-local memory-dependence query that walks back
through every earlier join block, with the results cached. That would be quadratic in
time and memory. This mechanism is a hypothesis about DXC internals. The measured
facts are the GVN share, the `-opt-disable gvn` collapse and the growth rates.

`ctest --preset dev --no-tests=error -R '^dxc_compile_time_gvn$' -V` runs the
compile-only check (`tests/dxc_compile_time_check.py`) with the configured DXC. It
uses no wall-time threshold. It checks that the two families differ only by the
branches and keep the fixture's declarations and control stores. It pins the recorded
source sizes and checks that the `-ftime-report` parser also reads the three-column
table that LLVM prints when no pass records system time. It also checks the DXIL
outcome of control, `condstore_256` and `stores_256`, with two negative controls: a
forced 1 ms timeout must be recorded as `timeout` and an invalid profile as
`diagnostic`. Finally it checks the GVN share of the reported pass wall time. That
share must be at least 50 % for `condstore_1024`, below 25 % for `stores_1024`, and
absent with `-opt-disable gvn`. On the machine above, the shares were 76.9 % / 5.3 % /
0 on 1.7.2308 and 87.3 % / 7.7 % / 0 on 1.9.0.5512. A DXC that no longer shows this
profile makes the check exit 77 (skip), because the compiler has changed rather than
this repository.

This narrows the source-built cost to conditional UAV stores under default
optimization. It does not attribute the archive cohort. The archive trace's
`loop-rotate` stall and Jump Threading profile are different passes, and the 695
replay timeouts remain unattributed.

#### Output parity of `-opt-disable gvn` on the minimized case

`dxc_gvn_output_warp` and `dxc_gvn_output_hardware` (`tests/dxc_gvn_output_check.py`)
compile `condstore_256` twice, once with default optimization and once with
`-opt-disable gvn`. They dispatch both blobs with identical inputs and compare the
complete raw UAV bit for bit, with each other and with an expected buffer that the
script computes from the HLSL alone. The two containers are different programs. The
default build folds the alternating branch conditions into phis, and the `-opt-disable
gvn` build recomputes each condition. The script prints both SHA-256 values and fails
if they are equal.

- **Inputs:** the sampler fixture's bindings, with t0 a 2x1 R32_FLOAT texture
  (0.25, 0.75), the wrap/clamp point samplers and the LESS_EQUAL comparison samplers.
  u0 is a 70,656-byte raw buffer, pre-filled so that word i holds
  `((i + 1) * 0x9E3779B1) | 1`. Words the shader does not write therefore keep a
  distinct value.
- **Dispatches:** one with 1 group and one with 2 groups. `offscreen_check <adapter>
  --raw-uav-dump <cso> <groups> <bytes> <out.bin>` performs the dispatch and writes
  the whole UAV.
- **Expected buffer:** the control words 0-3 are the sampler fixture's values. For
  group x, word 16384+k becomes `gl_WorkGroupID.y` (0) when `(x + k) & 1` is set.
  Every other word keeps the fill, so 130 words change with 1 group and 260 with 2.
- **Wrong-output controls:**
  - A mutated source, with store k=101 redirected into the unused tail (word 16704)
    and compiled with default optimization, must mismatch at exactly words 16485 and
    16704 in both dispatches.
  - Flipping one bit of expected word 16385 must give exactly that one mismatch.
- **Target:** cs_6_0 without native 16-bit types. The compile-time measurements above
  use cs_6_6, but the Windows 10 in-box D3D12 runtime exposes SM 6.6 neither on WARP
  nor on the RTX 3060 (`offscreen_check` reports `skip: ... requires SM 6.6` on both).
  At cs_6_0, GVN still takes 81.8 % of the pass time of `condstore_1024` (DXC
  1.7.2308.16).

```powershell
ctest --preset dev --no-tests=error -R '^dxc_gvn_output_(warp|hardware)$' -V
# direct form, with the DXC under test
out\pe-env\Scripts\python.exe -B tests\dxc_gvn_output_check.py <path-to-dxc.exe> out\build\dev\offscreen_check.exe warp
```

Measured on 2026-11-23 on Windows 10 Pro build 19045 (Ryzen 7 3700X). WARP was the
in-box `d3d10warp.dll` 10.0.19041.5794 and the hardware adapter was an NVIDIA GeForce
RTX 3060 (driver 32.0.15.9636). No Agility SDK and no debug layer were used. With DXC
1.7.2308.16 (`52da17e29`, the configured CTest DXC) and with DXC 1.9.0.5512
(`01b62ad47`, LunarG Vulkan SDK 1.4.363.0), both adapters gave the same result:

- the default and `-opt-disable gvn` buffers matched each other and the expected
  buffer in all 17,664 words, for both dispatches;
- the mutated-store control mismatched at exactly [16485, 16704];
- the corrupted-expected control mismatched at exactly [16385].

There were no capability skips. The container hashes were `cc17e4dc...` (default) and
`649f66ed...` (`-opt-disable gvn`) for 1.7.2308.16, and `ac0ff6cb...` and `11042b5a...`
for 1.9.0.5512.

This supports output equivalence of the GVN option on this one minimized shader, for
these inputs, dispatch sizes and adapters. It says nothing about larger members of the
family, other shaders, cs_6_6 DXIL, production flags or the archive cohort.

### Mesh shader -Od pipeline creation across adapters

[#810](https://github.com/R0Y00/Xbox2Windows/issues/810) asks whether mesh shader DXIL
compiled with `-Od` reaches pipeline creation and the expected draw on more than one
adapter. The [mesh -Od tool](../tools/mesh_od_pipeline.py) generates a source-built mesh
shader with a known draw: threads 0-2 emit one triangle that covers the 32x32 target.
Every thread first runs a generated integer computation on a constant-buffer seed, and
the vertex depth stays inside the clip volume only when the result equals the value the
tool computes in Python. The draw therefore matches the green reference only if the
compiled computation is correct. The families grow that computation and keep the
expected image:

- `control`: no extra computation.
- `chain_N`: N dependent multiply/xor/add steps.
- `branches_N`: N divergent if/else regions that depend on the group thread.
- `loops_N`: N loops whose trip count is a constant-buffer word plus `(thread + k) & 3`.

The tool compiles each variant twice with the same DXC and target (`ms_6_5`, default
optimization and `-Od`) and records compile time, peak working set and outcome. Each
DXIL blob that compiles goes unchanged to `offscreen_check <adapter> --mesh-entry`, so
both blobs use the same root signature, pixel shader, pipeline stream and draw. The
renderer prints the duration of the mesh `CreatePipelineState` call as
`mesh_pipeline_ms` and flushes it, so the tool records pipeline time separately from
the process wall time and peak working set, and places a crash or timeout before or
after pipeline creation. Two negative controls run on every blob that draws the
reference: the seed with one bit flipped, and a reference image with the triangle
missing. Both must report a draw mismatch. Use a fresh `--seed` to vary the expected
constants and the DXIL between runs. Repeated draws use the same blob. The harness
records first and repeated pipeline times without inspecting driver cache state.

`ctest` runs the small variants (`control`, `chain_64`, `branches_64`, `loops_16`) with
`--check` on the default hardware adapter and on WARP. It fails if a blob does not
compile, does not draw the reference, misses either control, or the renderer prints no
pipeline time, and it exits 77 when the adapter has no mesh shader tier.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^mesh_od_pipeline_' -V
# measurement run; timeouts are local policy values, not defaults
$env:XBOX2WINDOWS_ADAPTER = 'RTX 5080'
out\pe-env\Scripts\python.exe -B tools\mesh_od_pipeline.py --dxc <path-to-dxc.exe> --renderer out\build\dev\offscreen_check.exe --adapter hardware --variants control,chain_4096,branches_4096,loops_1024,loops_4096,loops_8192 --repeat 3 --seed 0x5eed0201 --compile-timeout 300 --pipeline-timeout 300 --out out\mesh-od\nvidia.json
```

Measured on 2026-11-23 at `523d9ac` plus this change, Windows 11 build 26300.9974,
Ryzen 7 9800X3D, 31.1 GiB, DXC 1.9.2602.17 from Windows SDK 10.0.26100, debug layer
unavailable, one process at a time. Seeds were `0x5eed0201` (RTX 5080), `0x5eed0202`
(Radeon) and `0x5eed0303` (WARP, run after the flush was added). Adapters: NVIDIA
GeForce RTX 5080 (`10de:2c02`, 32.0.16.2124), the AMD Radeon integrated GPU of the same
CPU (`1002:13c0`, 32.0.11034.2) and WARP (`1414:008c`, 10.0.26100.9818). DXC times are
from the RTX 5080 run. In the other runs, times below 2 s differ by at most 0.25 s and
larger times by up to 18 %. Pipeline times are the first creation with a fresh seed; peak is the
largest renderer working set.

| variant | DXC default / -Od s | DXIL default / -Od B | RTX 5080 default / -Od | Radeon default / -Od | WARP default / -Od |
|---|---:|---:|---|---|---|
| control | 0.02 / 0.02 | 4,160 / 4,224 | 0.03 / 0.02 s | 0.05 / 0.01 s | 0.0005 / 0.0005 s |
| chain_4096 | 0.32 / 0.15 | 73,796 / 90,616 | 0.90 / 1.03 s | 0.30 / 0.32 s | 0.005 / 0.007 s |
| branches_4096 | 0.42 / 1.46 | 105,288 / 351,588 | 2.04 / 3.93 s | 0.36 / 3.25 s | 0.005 s / 3.63 s, crash |
| loops_1024 | 1.35 / 0.60 | 127,332 / 179,488 | 5.35 / 5.16 s | 8.67 / 1.28 s | 0.36 s, crash / 0.37 s, mismatch |
| loops_4096 | 19.79 / 8.07 | 525,848 / 762,564 | 83.9 / 79.0 s, 3.1 / 3.2 GiB | timeout, 3.1 GiB / 17.6 s, 4.7 GiB | 6.3 / 6.4 s, crash |
| loops_8192 | 79.53 / 34.32 | 1,073,872 / 1,563,332 | timeout, 11.6 / 11.6 GiB | timeout, 10.6 GiB / 75.2 s, 15.4 GiB | 28.5 / 28.4 s, crash, 6.6 GiB |

Every blob that drew the reference on an adapter also failed both controls there.
`timeout` is the 300 s policy expiring inside pipeline creation; the process was
killed. On WARP, `crash` is the renderer exiting with NTSTATUS `0xc0000005`
(`branches_4096 -Od` gave `0xc0000374` in two of three runs), after `CreatePipelineState` returned and before
the readback; `mismatch` means the draw completed with all 1024 pixels wrong. No
HRESULT failure, compiler diagnostic or unsupported mesh tier occurred.

What the measurements show:

- On the RTX 5080, `-Od` changed pipeline time by less than 15 % on the loop variants
  and doubled it for `branches_4096`. Both blobs of `loops_8192` exceeded 300 s with
  about 11.6 GiB of working set. Repeated creations of the same blob took 1-32 ms.
- On the Radeon, `-Od` turned the default timeouts of `loops_4096` and `loops_8192`
  into completed pipelines that drew the reference. It cost more for `branches_4096`
  (0.36 to 3.25 s).
- On WARP, pipeline creation returned for every blob (at most 28.5 s), and no blob
  from `branches_4096 -Od`, `loops_768` or larger drew the reference. `loops_768` gave a
  mismatch at both optimization levels in two runs, while `loops_512` and `loops_640`
  matched. `loops_1024` gave a mismatch in some runs and an access violation in
  others, so the outcome there is not deterministic.
  In an ad hoc run outside the tool, `loops_768` with trip-count base 0, 5 or 20 still
  mismatched and a single loop of 1,000,000 iterations matched, so the observed boundary
  follows the number of loops in the shader rather than the iteration count. The RTX
  5080 and the Radeon drew the reference with the same `loops_1024` blobs. The cause on
  WARP is not established here.

This covers source-built mesh shaders, one seed per run and the listed adapters. It
does not replay archive records, does not change any watchdog, memory or host stack
setting, and does not establish that a default and an `-Od` blob are equivalent for
other inputs. The archive-wide follow-up remains with the aggregate acceptance run.

### Minimum LOD in the SPIR-V bridge

The pinned Windows SPIRV-Cross build maps the SPIR-V `MinLod` operand to the
HLSL sampling clamp. The bridge supports non-projected `Sample`, `SampleBias`,
`SampleGrad` and implicit-LOD `SampleCmp` forms at Shader Model 5 and above.
It preserves a supplied texel offset and inserts zero when the HLSL overload
requires one. Cube samplers take the clamp without an offset.

```powershell
.\tools\setup-spirv-tools.ps1
cmake --preset dev
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^spirv_minlod_' -V
```

The source-built `spirv_minlod_hardware` and `spirv_minlod_warp` smoke draws
compile at `ps_6_0` and skip on adapters below Shader Model 6.0. They run
`Sample`, `SampleBias` with an offset,
and `SampleGrad` against distinct red and green mips. Each sample must reach
the green mip and match the 64-pixel reference. Removing the clamp selects
the red mip and must fail at pixel (0, 0). The GTX 1060 (32.0.15.6094),
Windows 10 in-box WARP (10.0.19041.5794), and app-local WARP 1.0.22 passed on
Windows 10 build 19045.6456 with zero D3D12 debug errors and warnings.

The level and fractional fixtures use a plain `Sample` over an 8x8 texture
with four solid-color mips. The coordinate is constant, so the LOD is the
clamp. Each clamp runs as translated SPIR-V and direct HLSL on the same adapter.

`spirv_minlod_levels_*` compiles at `ps_6_0`:

- With point mip filtering, each integer clamp from 0 through 3 selects its
  corresponding mip exactly across all 64 pixels.
- With `MIN_MAG_POINT_MIP_LINEAR`, nine clamps from 1 to 3.75 match the exact
  mip blend within 1/128. The translated and control bits must be identical
  on each adapter.
- Clamp 1 checked against mip 2, and clamp 1.25 checked against the 1.5 blend,
  must fail at pixel (0, 0).

`spirv_minlod_fractional_*` retains the `ps_6_6` capability gate. It checks
nearest-mip selection at 1.4375, 1.5625 and 3.75. At the 1.5 tie, either
neighbor is accepted when both shader sources select the same mip. The
stale-reference check at 1.5625 must reject mip 1 at pixel (0, 0).
[Issue #809](https://github.com/R0Y00/Xbox2Windows/issues/809) records why the
fractional point cases run separately.

In separate `ps_6_0` and `ps_6_2` probes, in-box WARP
10.0.19041.5794 selected green mip 1 for a per-sample clamp of 1.5625 under
point mip filtering. An explicit `SampleLevel` at the same LOD selected
blue mip 2 on that adapter. Linear mip filtering gave the expected blend
(G=0.4375, B=0.5625). The translated `ps_6_0` sample and a direct HLSL clamp
agreed. [Direct3D rules for per-sample clamping and mip selection](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm)

A `ps_6_0` probe of the point-filtered case (`MIN_MAG_MIP_POINT`, gate lowered
locally, `929e659`) compared the translated `Sample` with `MinLod`, a direct
HLSL clamp and a direct `SampleLevel` at the same value. Each cell is the mip
that filled all 64 pixels; clamps 1, 1.25 and 1.4375 read mip 1 everywhere.
Windows 10 build 19045, DXC 1.9.2609.

| Clamp | `SampleLevel`, all | In-box WARP 10.0.19041.7811, `ps_6_0` | RTX 3080 Ti 32.0.16.1047, in-box runtime, `ps_6_0` | WARP 1.0.22 and RTX, Agility SDK 1.620.2, `ps_6_0` and `ps_6_6` |
|---|---|---|---|---|
| 0.5625 | 1 | 0 | 1 | 1 |
| 1.5 | 2 | 1 | 2 | 2 |
| 1.5625 | 2 | 1 | 2 | 2 |
| 1.75 | 2 | 1 | 2 | 2 |
| 2.5625 | 3 | 2 | 3 | 3 |

Only the Windows 10 in-box WARP reads the mip below a `MinLod` clamp, so the
difference follows the WARP build, not the runtime or shader model. On Windows
11 build 26200.9974, in-box WARP 10.0.26100.9818 and an RX 7900 XTX
(32.0.32029.2010) read the nearest mip at 1.5 and 1.5625 (`ps_6_6`, no other
clamps). Translated and direct HLSL agreed in every case.
[Measurement](https://github.com/R0Y00/Xbox2Windows/issues/809#issuecomment-7097414235),
[Windows 11 report](https://github.com/R0Y00/Xbox2Windows/issues/809#issuecomment-7095165499)

An RDNA 4 adapter repeated the five-clamp sweep at `ps_6_0` with both forms
reported separately (`0f0c3eb`, Windows 11 build 26300.10075, DXC 1.9.2602.17,
debug layer unavailable). An RX 9060 XT (32.0.32029.2010) and in-box WARP
10.0.26100.10068 read mips 1, 2, 2, 2 and 3 at 0.5625, 1.5, 1.5625, 1.75 and
2.5625, for `MinLod` and for explicit `SampleLevel`. Exactly one mip matched
all 64 pixels at every clamp, and translated and direct HLSL agreed in every
case. Neither adapter shows the Windows 10 in-box WARP behavior.
[RX 9060 XT report](https://github.com/R0Y00/Xbox2Windows/issues/809#issuecomment-7111499476)

Mip-linear results (`MIN_MAG_POINT_MIP_LINEAR`, `ps_6_6`, debug layer): WARP
1.0.22 and the RTX on Windows 10 build 19045 with Agility SDK 1.620.2 and DXC
1.9.2609; the Radeon and in-box WARP on Windows 11 build 26200.9974 with DXC
1.8.2502.11.

| Clamp | Exact green, blue | WARP 1.0.22 | RTX 3080 Ti, driver 32.0.16.1047 | RX 7900 XTX and in-box WARP 10.0.26100.9818 |
|---|---|---|---|---|
| 1.25 | 0.75, 0.25 | exact | 0.7499962, 0.2500038 | exact |
| 1.3 | 0.7, 0.3 | 0.6992188, 0.3007813 | 0.7031205, 0.2968795 | 0.6992188, 0.3007813 |
| 1.001953125 | 0.9980469, 0.0019531 | 1, 0 | 1, 0 | 1, 0 |

All adapters quantize the blend weight to 1/256. At 1.3 the RTX truncates it
to 76/256 and the others give 77/256. Translated and control results were
bit-identical on each adapter, so the differences come from mip filtering. The
1/128 tolerance was chosen from these measurements, not from a specified
precision. Whether this explains the archive differences is not established.
[Issue](https://github.com/R0Y00/Xbox2Windows/issues/809)

A sweep of the blue weight from 76/256 to 77/256 in 1/4096 steps, plus the ties
at 75.5, 76.5 and 77.5 (`6cd0f15`), separates rounding from truncation. Each
cell is the observed weight × 256 and the blue word; all 64 pixels and both
shaders matched at every point.

| Requested weight × 256 | WARP 1.0.22 | RTX 3080 Ti, driver 32.0.16.1047 |
|---|---|---|
| 75.5 (tie) | 76 (`3e980000`) | 75 (`3e960096`) |
| 76.0 to 76.49976 | 76 (`3e980000`) | 76 (`3e980098`) |
| 76.5 (tie) | 76 (`3e980000`) | 76 (`3e980098`) |
| 76.50024 to 76.99976 | 77 (`3e9a0000`) | 76 (`3e980098`) |
| 77.0 | 77 (`3e9a0000`) | 77 (`3e9a009a`) |
| 77.5 (tie) | 78 (`3e9c0000`) | 77 (`3e9a009a`) |

WARP rounds to the nearest 1/256 with ties to even; the RTX truncates and
stores step k as `k·256/65535`, which gives the offset of up to 7.6e-6.
[Half-step sweep](https://github.com/R0Y00/Xbox2Windows/issues/809#issuecomment-7095572305)

The RX 9060 XT (32.0.32029.2010) and in-box WARP 10.0.26100.10068 gave the
WARP 1.0.22 column at `ps_6_0` (`0f0c3eb`, Windows 11 build 26300.10075, DXC
1.9.2602.17, debug layer unavailable). That sweep had 22 points: 76/256 to
77/256 in 1/16 steps, 76.5 ± 1/4096, 77 − 1/4096, and the ties at 75.5 and
77.5. The ties read 76, 76 and 78 on both, 76.50024 already reads 77, and the
blue words are exact `k/256` values with no RTX-style offset, so this Radeon
rounds to the nearest 1/256 with ties to even. The raw words are from pixel
(0, 0), where both shaders and both adapters matched at every point; the
other 63 pixels are covered by the fixture's 1/128 comparison, not by a
bit-for-bit one.
[RX 9060 XT sweep](https://github.com/R0Y00/Xbox2Windows/issues/809#issuecomment-7111499476)

## Quad-local DS swizzle in pixel shaders

The pixel profile lowers `ds_swizzle_b32` with the LLVM `QUAD_PERM` spelling.
Each of the four two-bit selectors picks the source lane for the corresponding
lane in the same quad. The profile requires a verified whole-quad execution
region, a VGPR defined for the whole quad, and an immediate
`s_waitcnt lgkmcnt(0)`. The generated HLSL reads the source as `uint` through
quad intrinsics before restoring the bits to the VGPR value. GDS, bit-mask
mode and DS operations other than the swizzle and no-op described here remain
outside this profile.
[AMD cross-lane instruction semantics](https://gpuopen.com/learn/amd-gcn-assembly-cross-lane-operations/)

The source-built fixture assembles for `gfx1030` with wave64 in the LLVM 15
binary round trip. On Windows it compares 256 `R32G32B32A32_UINT` channel words
from an 8×8 draw to a CPU reference derived from pixel centers. The four
exports exercise a mixed permutation on X and Y, a horizontal swap, and a
broadcast. Changing the swap to identity must report a mismatch at (0, 0) in
blue (`0x3e400000` expected, `0x3d800000` observed). The lowering controls reject
missing or insufficient waits, non-quad execution, GDS, an out-of-range
selector, an undefined source, an out-of-range VGPR and a cross-quad mode.

Measured on the patch based on `e97090a` with Windows 11 25H2 build 26200.9974,
Windows SDK DXC 1.8.2502.11 and the D3D12 debug layer enabled. Microsoft Basic
Render Driver (WARP 10.0.26100.9818) and Radeon RX 7900 XTX (32.0.32029.2010)
both matched every channel bit and caught the negative control. Neither test
skipped. This measures the desktop translation against the reference.

```powershell
cmake --build --preset dev --target offscreen_check
ctest --preset dev --no-tests=error -R '^pixel_aot_ds_swizzle_(warp|hardware)$' -V
```

The same fixture now also draws a variant with operandless `ds_nop` inserted
in the entry and whole-quad execution regions. The two variants are checked
against the same 256-word CPU oracle. Replacing those two instructions with
`s_nop 0` in place produces identical HLSL. The profile rejects `ds_nop` with
operands or modifiers, after the color export or program end, and between a
DS swizzle and its required immediate wait. The LLVM 15 gfx1030 wave64
round trip verifies the leading eight-byte encoding and both decoded no-ops.
The coverage record for the passing draw includes `ds_nop`.

```sh
python3 -B tests/pixel_aot_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

## Independent bit and byte alignment readback

The `pixel_aot_align_warp` and `pixel_aot_align_hardware` fixtures run dedicated
`v_alignbit_b32` and `v_alignbyte_b32` programs. Both are included in the
source-built binary round trip. Each exports four observable UINT channels; the
variable-count instruction aliases its second source and destination.

Each operation has 24 input/reference cases from three asymmetric word pairs
and eight counts, including zero, bit counts 31 and 32, byte counts 3 and 4,
and wrapped counts. References use Python's 64-bit concatenation and shift.
Input and output words avoid NaN and subnormal encodings so float register
transport cannot obscure the instruction comparison. A wrong expected word is
surrounded by correct cases; a separately compiled shader with reversed inputs
must fail every reference.

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_align_(warp|hardware)$' -V
```

Python-only controls check explicit reference words and translator acceptance.
Those controls do not establish GPU execution; the command above must pass on
the named backend before recording readback evidence.

## Independent VOP3 ternary integer ALU readback

The `pixel_aot_vop3_ternary_int_warp` and `pixel_aot_vop3_ternary_int_hardware` fixtures run
dedicated 3-operand integer ALU programs covering 8 instructions:
- `v_add3_u32`: `(src0 + src1 + src2)`
- `v_or3_b32`: `(src0 | src1 | src2)`
- `v_xor3_b32`: `(src0 ^ src1 ^ src2)`
- `v_and_or_b32`: `((src0 & src1) | src2)`
- `v_xad_u32`: `((src0 ^ src1) + src2)`
- `v_lshl_or_b32`: `((src0 << (src1 & 31)) | src2)`
- `v_lshl_add_u32`: `((src0 << (src1 & 31)) + src2)`
- `v_add_lshl_u32`: `((src0 + src1) << (src2 & 31))`

All 8 instructions are included in the source-built binary round trip. Each exports four
observable UINT channels; the fourth instruction in each program aliases its second source
and destination register.

Each group has 30 input/reference cases from three asymmetric word pairs and ten shift/add
counts (0, 1, 2, 3, 4, 8, 16, 24, 31, 32). References use 32-bit integer arithmetic.
Input and output words avoid NaN and subnormal encodings so float register transport
cannot obscure the instruction comparison. A wrong expected word is surrounded by correct
cases; a separately compiled shader with mutated operations must fail every reference.

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_vop3_ternary_int_(warp|hardware)$' -V
```

Python-only controls check explicit reference words and translator acceptance.
Those controls do not establish GPU execution; the command above must pass on
the named backend before recording readback evidence.

### Two shadow frames from the EXE entry

The native, mapped and AOT runners use the existing translated shadow scene. The
second frame shifts the caster four pixels while retaining the device, pipeline
states, descriptor heap, camera depth and color target. Two light depth maps
remain live so the second camera pass can deliberately select the first one.
The EXE selects each frame and map slot and calls the test provider to record,
submit and wait for each frame. One queue signals fence values 1 and 2. Frame
data is published after its wait and contains 4,096 color pixels followed by
4,096 light-depth texels. The check compares every byte to the CPU reference,
checks the mesh export traces, verifies the ordered calls and four owned surface
releases, and expects exit code 42. The stale binding changes only the second
color frame, by 170 pixels, and is checked against its own reference.

For the submission control the EXE records the second frame, then supplies an
invalid frame index to submit. The fixture rejects that request with
`E_INVALIDARG` before the queue is called. Only fence 1 and the first frame are
published. The EXE takes its submit-error exit 24 after the unsubmitted list,
four surfaces and device are released. The fixture provider implements the
record, submit, wait and close imports for this source-built scene.

Run `ctest --preset release --no-tests=error -R '^cpu_shim_shadow_(warp|hardware)$' -V`.

The host selects an adapter, and the EXE calls the
`d3d12_xs.dll!D3D12XboxCreateDevice` shim through its own PE import. The mapper
resolves that import to the same shim, and the AOT binding passes the four
arguments and 32-bit HRESULT from the lifted call site. The shim creates a
device at feature level 12.0. The fixture uses a null parameter block and the desktop
`ID3D12Device` interface. A non-null Xbox parameter block or unsupported
interface is rejected. The negative case supplies an unsupported IID after
adapter selection. It checks the cleared device output, release of the adapter
and factory references, zero frame bytes and exit code 20 on all three runners.

## First-active-lane vector broadcast readback

The `pixel_aot_warp` and `pixel_aot_hardware` fixtures include a dedicated
`v_readfirstlane_b32` program, which is also included in the source-built
binary round trip. `v_readfirstlane_b32` copies the value of a VGPR from the
first active lane in the wavefront into an SGPR destination, lowered to HLSL SM 6.0
`WaveReadLaneFirst()`. The destination scalar register is immediately tracked
for downstream SALU arithmetic, VALU operands, and constant-buffer offset indexing.

The lowerer enforces uniform-source extraction, where participating lanes
hold the same value (as when broadcasting or scalarizing values copied from
SGPRs, constant buffers, or immediate bit patterns). The draw fixture verifies
broadcast scalar extraction across zero, normal, and high integer inputs against
an exact bit pattern across 64 pixels, with a deliberate oracle mismatch case
ensuring incorrect readbacks fail. Divergent per-lane source readback and
non-zero first participating lane behavior remain unmeasured. Dedicated controls
verify rejection of varying VGPR inputs (such as interpolated coordinates and
barycentrics), non-entry execution masks (during whole-quad mode), VGPR destinations,
out-of-range SGPR indices, unresolved scalar memory dependencies, and invalid modifiers,
while uniform self-moves (`v_mov_b32_e32 v18, v18`) retain uniform provenance.

```powershell
ctest --preset dev --no-tests=error -R '^pixel_aot_(warp|hardware)$' -V
```

Python-only controls check HLSL wave-intrinsic translation, register tracking,
and scanner recognition without requiring a GPU.

## Vector float math division FMA and unverified prescaling

`pixel_aot` rejects `v_div_scale_f32` and `v_fma_f16` (including explicit `_e64`
spellings). Both instructions remain recognized by the scanner, but are not
reported as supported lowerings until verified by source-built native RDNA
readbacks. For `v_div_scale_f32`, a denominator-only threshold cannot reproduce its
numerator-dependent result and VCC flag (e.g., denominator 1 with numerator 2^100
takes the RDNA 2 opcode 365 exponent-gap branch scaling by 2^64 with VCC set).
For `v_fma_f16`, the halfway case `0x3c69 * 0x3faa + 0x89d1` distinguishes single
rounding to binary16 (`0x4039`) from intermediate rounding through float32 (`0x403a`),
and destination high-half preservation remains unmeasured without a source-built
native RDNA readback.

`v_div_fmas_f32` evaluates `div_fmas32(src0, src1, src2, vcc)` using double precision
with Knuth 2Sum remainder tracking for finite inputs. Scaling by 2^32 when VCC is true
happens before conversion to binary32. The helper is emitted only for shaders using this
operation and requires double-precision shader arithmetic. Its double comparisons use
only `<`, `>=`, `==` and `!=`: the Windows 10 in-box WARP (10.0.19041) faults at the
first dispatch of a double `>` or `<=` (DXIL `fcmp ogt`/`ole`) ([#876](https://github.com/R0Y00/Xbox2Windows/issues/876)). The integer-readback fixture
(`pixel_aot_div_fmas_warp` and `pixel_aot_div_fmas_hardware`) checks both VCC conditions
and clamping with runtime inputs under the preserve denormal mode:
- Midpoint single-rounding: `(0x3f800001, 0x3fc00000, 0x8d800000)` produces exact sum
  1.5 + 3*2^-24 - 2^-100 strictly below the midpoint, rounding down to `0x3fc00001`
  (`0x4fc00001` scaled by 2^32), whereas uncorrected double rounding rounds to even
  (`0x3fc00002` / `0x4fc00002`);
- Subnormal midpoints: adding an exact product of 2^-150 minus 2^-196 to
  `0x00400001` or `0x00040001` must keep the original unscaled word. The guard bit
  lies in different binary64 words in these two cases.
- Cancellation: `(0x3f800001, 0x3f7ffffe, 0xbf800000)` produces exact product + addend
  -2^-46 (`0xa8800000` unscaled, `0xb8800000` scaled by 2^32), where unfused float32 `mad`
  rounds `1 - 2^-46` prematurely to 1.0 yielding zero;
- Pre-scale underflow: `(0x00800000, 0x33000000, 0)` produces 2^-151 before scale, which
  would underflow float32 to zero if rounded early, but scales to 2^-119 (`0x04000000`);
- Denormal scale boundary: 2^-126 * 0.5 = 2^-127 (`0x00400000`) before scaling by 2^32 to
  2^-95 (`0x10000000`);
- Normal arithmetic: 2.0 * 0.5 + 1.0 = 2.0 (`0x40000000` unscaled, `0x50000000` scaled,
  `0x3f800000` clamped).
Four negative controls verify that omitting the 2^32 scale, substituting unfused `mad`,
omitting the 2Sum midpoint correction (double-rounding), or flushing intermediate
subnormals (`ftz`) report pixel mismatches under `--check-pixel-case`.


## Offline package PE import closure

`tools/pe_package_preflight.py` reads PE import and export tables from a
**source-built** package on Linux, macOS or Windows. It reuses the strict
`pe_inventory.parse_image` reader and the existing `resolve_desktop` forwarder
resolver, but **does not query host Windows DLLs** or load/execute any image.
The JSON output records direct versus delay imports, resolved package exports,
forwarder chains, ambiguous package basenames, and unresolved local exports.
An I386 importer cannot bind an AMD64 package DLL, or the reverse, even when its
export name matches. Such an edge is `bundled_machine_mismatch`; its final chain
step records the importer and provider machine tags, and traversal of that
forwarder chain stops there. The check applies to direct and delayed imports and
to intermediate forwarders. It does not reject an unrelated DLL that is not
reachable. Other machine combinations, including ARM64EC/ARM64X hybrids, remain
unverified rather than being classified by tag equality.
This follows the [Windows in-process 32/64-bit DLL restriction](https://learn.microsoft.com/en-us/windows/win32/winprog64/process-interoperability).
It follows static package providers reachable from the chosen EXE, preserving
intermediate forwarders and preferring unconditional paths. Images not reached by that analysis are listed
separately. External providers (including Windows system DLLs) are recorded as
`external_unverified`, **not as missing**, because no host exports or API-set
mapping were consulted. Forwarders to external providers are likewise marked
unverified. This is not a prediction that the image will start or run correctly.

```sh
python -B tools/pe_package_preflight.py out/source-built-package app.exe \
    --output out/pe-package-report.json
python -B tests/pe_package_preflight_check.py -v
```

The package root is scanned only for `.exe`/`.dll` files. Symlinked PE candidates,
images over 512 MiB, packages with more than 1,024 PE images or over 2 GiB of
PE images are refused, as are entries outside the package root. The tool does
not inspect non-PE assets, dynamic `LoadLibrary`/`GetProcAddress` calls, Windows
KnownDLLs, API-set redirections, shim ABI semantics, TLS execution or GPU state.
Reports are written outside the inspected package. Replacing an existing report
does not change any inspected input linked to the same file. Reports should remain
under `out/` and must not contain private package data in a public PR.

On a separate, source-built Win64 diagnostic executable, a Linux smoke test
reported 46 static imports as external/unverified and zero bundled failures:
no DLLs were included in that diagnostic directory. The self-contained eleven-test
suite checks forwarded exports and ordinals, missing symbols, forwarder cycles,
ambiguous local basenames, delay-import reachability, deterministic JSON output,
path escapes, symlinks, hard-linked outputs, I386/AMD64 import and forwarder mismatches,
and malformed PE CLI rejection. All tests use synthetic
metadata and fake local files; no title files or binary fixtures are checked in.

An additional source-built smoke comparison used Clang/LLD 21.1.8 on Linux: an
AMD64 EXE imports `Start` from `shim.dll`, built once as AMD64 and once as I386
from the same C function. The matching pair reports one bundled export and zero
failures; replacing only the DLL with the I386 build reports one machine mismatch.
Before this check, both packages reported zero failures. Neither image was executed.


### Starting source-built packages with ordinal-only DLL exports

`pe_run_package.py` can start a synthetic EXE importing `#77` from a bundled
DLL whose export address table contains only an unnamed ordinal. The offline
preflight resolves the package dependency as before. The mapper now exposes
`pe_map_export_ordinal(mapped, ordinal)`, accepting an ordinal in 1..65535
for a live mapping returned by `pe_map_image` with a non-forwarded export at
that exact ordinal. A null mapping returns null. The runner requires this
export from its mapper DLL only when a package actually needs an ordinal.
It refuses holes, absent ordinals, out-of-range inputs and forwarder RVAs, rather
than mistaking an export name-table index for an address-table index. Packages
with a forwarded ordinal target are resolved by the existing package graph
before looking up the terminal export in its mapped module.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_package_ordinal_startup$' -V
ctest --preset release --no-tests=error -R '^cpu_native_package_ordinal_startup$' -V
python -B tests/pe_package_ordinal_plan_check.py -v
```

The CRT-free fixture's import table must contain exactly one ordinal import,
`#77`, and its provider must export `mix_args @77 NONAME` (no export name).
Under the native loader and the mapped package runner, the original provider
returns 13 and the altered provider returns 14 after writing the same three
words. A provider that instead exports `#78` must be refused before the guest
entry, as must a missing provider; the direct mapper lookup also rejects six
invalid ordinal values. The accompanying platform-independent tests check
direct and forwarded ordinal routes and missing-symbol errors.

This addresses **only static packaged ordinal binding**, not console ABI,
DLL cycles, unknown runtime provider contracts, thread-local slot zero, or
execution of any retail executable. The public tests contain no private binaries.


## Legacy SSE FP compares and partial/non-temporal moves

[The SSE FP leaf translator](../tools/cpu_sse_fp_aot.py) lifts bounded straight-line
Windows x64 leaf functions covering five uncovered forms from `docs/coverage/x64_zen2.tsv`
group `SSE`:

- Packed float compare: `cmpps` (all eight `imm8` predicates `0..7`, `xmm` and 16-byte aligned `m128` sources)
- Scalar float compare: `cmpss` (all eight `imm8` predicates `0..7`, `xmm` and unaligned `m32` sources, preserving bits `127:32` of the destination)
- Half-qword float moves: `movlps` and `movhps` (`m64` load-merge preserving the other 64 bits, and `m64` store)
- Non-temporal store: `movntps` (16-byte aligned `m128` store)

Each lifted export takes `(word rcx, word rdx, word r8)` pointing to caller-owned
flat buffers (`rcx` and `rdx` inputs, `r8` output) with 16-byte-aligned bases and
returns `void`. Register state covers `xmm0`..`xmm5` modeled as `Vec128` (`std::uint64_t q[2]`).
For transport and ABI glue, the translator accepts `movaps`, `movdqa`, `movdqu` and
terminal `ret`. Memory stores (`movlps`, `movhps`, `movntps`, `movaps`, `movdqa`, `movdqu`)
must target an `r8`-rooted flat `[r8 + disp]` address; full 128-bit aligned operands
(`cmpps` `m128`, `movntps`, `movaps` `m128`, `movdqa` `m128`) require displacements
divisible by 16.

The translator explicitly rejects VEX/EVEX encodings, `cmpps`/`cmpss` predicates
`imm8 >= 8`, nonvolatile vector registers (`xmm6`..`xmm15`), nonvolatile or non-argument
memory base registers, non-`r8` store destinations, unaligned 16-byte memory operands,
undefined register reads/merges, `lock`/`rep`/segment prefixes, unsupported instructions,
missing terminal `ret`, and instructions after `ret`.

```powershell
cmake --build --preset dev --target cpu_sse_fp_original cpu_sse_fp_original_lifted cpu_sse_fp_altered cpu_sse_fp_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_sse_fp$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/sse_fp.asm) exports 34 functions covering all five
forms: all eight `cmpps` predicates plus aligned `m128`, displaced `m128` and self-compare
cases; all eight `cmpss` predicates plus `m32`, unaligned `+1` `m32` and self-compare
cases; `movlps`/`movhps` loads and stores including unaligned `+1` addresses; `movntps`
aligned stores at `+0` and `+16`; and a chained `op_pipeline` export. [The differential check](../tests/cpu/sse_fp_check.py)
verified 720 cases across the 34 exports bit-for-bit against both the native MASM DLL
and an independent Python IEEE-754 single-precision reference model under default MXCSR
controls (including signed zeros, infinities, quiet and signaling NaNs with distinct
payloads, subnormals, and 32-byte pre/post buffer guards).

A separate leaf stores one vector, executes a 16-byte load whose result is unused, and then stores
another vector. Both native and translated code perform both stores when the source is readable.
An inaccessible source must fault after the first store and leave the second vector unchanged,
including in the Release preset.

An `ALTER_IMM` variant flips predicates on `op_cmpps_1` (`1` to `5`), `op_cmpps_3`
(`3` to `7`), `op_cmpps_6` (`6` to `2`), `op_cmpss_1` (`1` to `5`), `op_cmpss_3` (`3` to `7`),
and `op_cmpss_6` (`6` to `2`); the altered lifted DLL matched the altered reference and
diverged from the original on 27 cases per export. An output-mutation control confirmed
the comparator detects single-bit output mismatches. Byte-decoded and synthetic `iced_x86`
controls passed 7 alternate-encoding lift checks, 60 unit rejection controls (9 VEX/EVEX,
4 `imm8 >= 8`, 15 unsupported instructions, 32 ABI/state/addressing/prefix/stream violations),
and 10 end-to-end PE-patched DLL rejection controls.

Not covered by this experiment: non-default MXCSR modes (FTZ/DAZ or unmasked exceptions),
non-temporal cache-bypass hints on `movntps`, control flow, nonvolatile `xmm6`..`xmm15`
prologues, and title CPU code execution.


## Legacy SSE/MMX conversion, shuffle, and non-temporal store

[The SSE/MMX leaf translator](../tools/cpu_sse_mmx_aot.py) lifts bounded straight-line
Windows x64 leaf functions covering five uncovered forms from `docs/coverage/x64_zen2.tsv`
group `SSE`:

- Integer-to-float conversion: `cvtpi2ps` (`mm` and `m64` sources, preserving `xmm[127:64]`)
- Float-to-integer conversions: `cvtps2pi` (round-to-nearest-even) and `cvttps2pi` (truncate toward zero) on `xmm` and `m64` sources, returning `0x80000000` on NaN, `+/-Inf`, or out-of-`int32_t` range after rounding
- Word permutation: `pshufw` (`imm8` 16-bit word shuffle on `mm` and `m64` sources)
- Non-temporal store: `movntq` (`m64` store to `[r8 + disp]`, tested at aligned and unaligned addresses)

Call with round-to-nearest-even, masked MXCSR exceptions, DAZ and FTZ off, and
an empty x87 stack with no pending unmasked exceptions or live MMX values.
The caller owns the buffers and reads the output after return. The `movntq`
lowering preserves the eight output bytes. Caching hints, inter-thread
visibility, floating-point status flags and physical x87 tags are outside
the observed contract.

Each lifted export uses the leaf ABI `void fn(word rcx, word rdx, word r8)` (`rcx`/`rdx`
inputs, `r8` output), modeling `xmm0`..`xmm5` (`Vec128`), `mm0`..`mm7` (`std::uint64_t mm[8]`),
and volatile scratch GPRs `rax`/`r9`/`r10`/`r11` for `movq` bridges alongside `movaps`/`movdqa`
(16-byte aligned), `movdqu`, `emms`, and terminal `ret`. Touching any `mm` register sets
`mmx_active`; `emms` clears it and invalidates `mm` definitions unconditionally, and `ret`
rejects while `mmx_active` is true. Dead-read fault ordering uses `std::atomic_signal_fence`
with volatile loads. Memory stores are restricted to `r8`-rooted flat `[r8 + disp]` operands,
and VEX/EVEX encodings, nonvolatile registers, undefined reads/merges, prefixes, and
unsupported instructions reject.

```powershell
cmake --build --preset dev --target cpu_sse_mmx_original cpu_sse_mmx_original_lifted cpu_sse_mmx_altered cpu_sse_mmx_altered_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_sse_mmx$' -V
```

### Measured results and open scope

The [MASM fixture](../tests/cpu/sse_mmx.asm) exports 19 functions covering register and
displaced/unaligned `m64` forms of all five instructions, `movq` GPR bridges, `emms`
re-entry, a discarded `cvtps2pi` fault-ordering read, and a chained pipeline.
[The differential check](../tests/cpu/sse_mmx_check.py) verified 237 cases across the 18
differential exports bit-for-bit against both the native MASM DLL and an independent Python
reference model (including `+/-0.0`, subnormals, ties-to-even, `2147483520.0`, `2147483648.0`,
`-2147483648.0`, `-2147483904.0`, `+/-Inf`, qNaN/sNaN, `xmm[127:64]` preservation, and
`pshufw` `imm8` edge patterns), plus the discarded-read fault-ordering test on both `dev`
and `release` (`/O2`) presets. An `ALTER_IMM` variant swaps `cvtps2pi`/`cvttps2pi` and
changes `pshufw` `imm8` on four exports (diverging on 3, 3, 7, and 7 cases); an
output-mutation control confirmed single-bit mismatch detection; and rejection controls
passed 7 alternate-encoding lift checks, 59 unit rejections (7 VEX/EVEX, 12 unsupported
instructions, 40 ABI/state/addressing/prefix/stream), and 10 PE-patched DLL rejections.

Not covered by this experiment: non-default MXCSR rounding modes, non-temporal cache-bypass
hints on `movntq`, x87/MMX aliasing across calls, control flow, and title CPU code execution.
