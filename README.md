# device-atlas

A classified catalog of the compute devices you are likely to meet in compiler and
kernel work, with a small planner that **tiles the same matrix multiply for each one** so
you can see why the answer differs.

The hierarchy is organized by the question that actually changes tiling:
**who moves data between memories?**

```
Devices
├── General-purpose processors      hardware caches move data
│   ├── CPU with SIMD               AVX2, NEON
│   └── CPU with matrix extension   AMX
├── Parallel accelerators           hardware schedules blocks; you stage tiles in shared memory
│   └── GPU: SIMT + tensor cores    A100, H100, Orin, MI300X
├── Software-managed accelerators   you (or the compiler) issue the DMA
│   ├── DSP: vector + scratchpad    Hexagon V68 / V73
│   ├── NPU: matrix engine + scratchpad   Ascend
│   ├── NPU: spatial array of tiles Versal AIE
│   ├── NPU: microcontroller class  Ethos-U55
│   └── Systolic array              TPU
└── Simulated                       MN1 from ../mock-npu-bench
```

(`python3 -m atlas tree` prints the full tree with device ids.)

## Use it

```bash
python3 -m atlas tree
python3 -m atlas show hexagon-v73-hmx
python3 -m atlas tile nvidia-a100 --dtype f16 --m 4096 --n 4096 --k 4096   # one device, with reasons
python3 -m atlas compare --dtype f16 --m 4096 --n 4096 --k 4096            # same GEMM everywhere
python3 -m atlas compare --dtype f16 --m 16 --n 4096 --k 4096              # skinny: very different story
python3 -m atlas audit        # which entries are unverified or missing data
python3 -m atlas docs         # regenerate docs/comparison.md
python3 -m unittest discover -s tests
```

Python 3, standard library only.

## Web UI

A static site: a collapsible device tree on the left, and on the right a device page (specs, memory
ladder with the tile's footprint, tile table, reasons, estimate), a group page, or the all-device
comparison. Presets are precomputed by the Python planner, so the browser never re-implements it.

```bash
python3 -m atlas site --out dist          # build web/ + data.json into dist/
python3 -m http.server -d dist 8000       # then open http://localhost:8000
```

It deploys to GitHub Pages from `.github/workflows/pages.yml` on every push to `main`
(tests, build, deploy, then a check that the live site serves the build). One-time setup:
repo Settings, Pages, Source: **GitHub Actions**. Expected address:
https://joepothiboot.github.io/device-atlas/

## What to read

- [docs/axes.md](docs/axes.md): seven questions to ask about any new device, and what each one does to tiling.
- [docs/comparison.md](docs/comparison.md): generated side-by-side tables for four problems.
- `data/devices.json`: the database. One entry per device: hierarchy path, units, memories, matrix unit, peaks, gotchas.
- `atlas/planners.py`: the three planners and the shared cost model.

## Read this before trusting a number

- **The device data is from public knowledge and is not verified.** Every entry except the simulator
  has `"verified": false` and lists the documents to check it against (`atlas audit`). Some fields
  are guesses: the TPU VMEM size, Ascend buffer sizes, the Ethos-U55 SRAM, Hexagon VTCM sizes,
  the AIE-ML array size, and L2 bandwidth (`llc_bw_factor`, an assumption).
- **The planner is a teaching model, not a tuner.** It reports a roofline-style *upper bound*: it assumes the
  micro-kernel reaches peak. Real kernels also lose time to instruction latency, bank conflicts, and layout
  conversions. For example, the planner calls MN1 matmul compute-bound at ~100% of peak, while the
  `mock-npu-bench` simulator measures about 17% for its best hand-written matmul. That gap is the
  micro-kernel (software pipelining), not the tile.
- Hexagon, Versal AIE and Ethos entries lack peak and/or bandwidth numbers, so those rows show
  arithmetic intensity but no bound or %-of-peak.

## Where this connects

- `../mock-npu-bench`: MN1 is a device here (`mn1-mock`), so `atlas tile mn1-mock --dtype f32` suggests
  a scratchpad tile you can try in `tools/gen_matmul.py` once it supports tiling.
- `../nano-dsp-mlir`: its `TargetModel` (vector width, registers, cache sizes) is a small version of one
  device entry. A natural next step is generating `TargetModel` entries from this database.
