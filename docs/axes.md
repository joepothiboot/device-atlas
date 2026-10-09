# Seven questions to ask about any new device

When a vendor hands you a new chip, you do not need to memorize it. You need to
place it in the hierarchy by answering these questions from its manual. The answers
decide the tile shape, the loop structure and the compiler passes you need.

| # | Question | Where the manual answers it | What it does to tiling |
|---|----------|-----------------------------|------------------------|
| 1 | **Who moves data between memories?** Hardware caches, a thread scheduler plus software-staged shared memory, or software-issued DMA? | memory chapter, "DMA", "cache coherence" | Cache: tiles are a *hint* (too big = slower). Shared memory: tiles are a *limit* per block. Scratchpad: tiles are a *contract* (too big = does not run). |
| 2 | **What is the fastest memory you control, and how big is it?** (registers, smem, VTCM, L1 buffer, VMEM) | memory map, per-unit capacities | Upper bound on the inner tile. Remember double buffering halves it. |
| 3 | **What is the native compute shape?** (vector width, mma 16x8x16, wgmma m64, HMX 32x32, MXU 128x128) | ISA chapter, matrix unit chapter | Tile sides must be multiples of it. Everything else is padding waste. |
| 4 | **How many independent units, and who schedules work onto them?** (SMs, cores, tiles) | execution model, launch model | Tile count vs unit count = wave quantization (GPU) or idle units (scratchpad). Few tiles means split-K. |
| 5 | **What does moving a tile cost?** (bandwidth, latency/setup, minimum burst, alignment) | DMA / load-store chapter | Sets the lower bound on tile size and the benefit of double buffering. |
| 6 | **What layouts does the unit want?** (VNNI, NZ fractal, swizzled smem, 2 KB HMX tiles) | operand format tables | Often a *layout transform* is the real cost of using the fast unit, not the tile size. |
| 7 | **Who is the compiler?** (vendor offline, LLVM backend, XLA, hand-written DSL kernel) | toolchain docs | Decides whether you tune a heuristic, write intrinsics, or write the cost model yourself. |

## The three archetypes in this atlas

| Archetype | Examples | Tile = | If you get it wrong |
|-----------|----------|--------|---------------------|
| `cache_cpu` | AVX2, AVX-512 + AMX, NEON | register micro-kernel inside cache-sized panels | slower (cache misses, conflicts) |
| `simt_gpu` | A100, H100, MI300X, Orin | block tile in shared memory, accumulators in registers | lower occupancy, wasted waves, spills |
| `scratchpad_dma` | Hexagon, Ascend, AIE, Ethos, TPU, MN1 | tile + double buffers inside the scratchpad | does not compile or run |

The planner in `atlas/planners.py` uses the same time model for all three and
swaps only the **constraints**. That is the point: the *algorithm* (tile the output,
stream K) stays the same; the device changes which constraint binds.

## What to look at when the numbers look the same

If two devices produce similar tiles for one problem, change the problem:

- `--m 16` (decode-like): H100 pads M to 64 (75% of the matrix work is padding), Hexagon HMX pads to 32
  (50%), A100 and Ascend pad to 16 (none). Every device is memory-bound, so the padding waste costs
  less than it looks, but the small-M efficiency gap between devices is real.
- `--dtype i8`: only the software-managed accelerators and a few others have a native path.
- Shrink the scratchpad (edit `kb` in `data/devices.json`): the scratchpad device picks smaller
  tiles and moves more data, and at zero capacity the planner refuses (a scratchpad overflow is a
  failure, not a slowdown).

## From here to a real job

1. Pick a device card, read its vendor manual, and fix every field you can verify.
   Set `"verified": true` only after you have checked a number against a document.
2. Add the missing devices you actually meet (an NPU from your employer, a DSP).
3. Where the model disagrees with a measurement on real hardware, that disagreement is the
   interesting finding: write it into the device's `gotchas`.
