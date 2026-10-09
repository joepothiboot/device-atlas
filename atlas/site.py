"""Build the static site: web/ assets + data.json with every plan precomputed in Python.

The browser never re-implements the planner; it only renders what this module computed.
"""
import json
import os
import shutil

from . import devices as D
from . import planners as P

WEB = os.path.join(D.HERE, "..", "web")

PRESETS = [
    ("square-4096", "Large square, 4096 x 4096 x 4096", 4096, 4096, 4096),
    ("square-1024", "Medium square, 1024 x 1024 x 1024", 1024, 1024, 1024),
    ("skinny-16", "Skinny, M=16 (decode-like)", 16, 4096, 4096),
    ("single-row", "Single row, M=1", 1, 4096, 4096),
    ("odd-100", "Odd sizes, 100 x 100 x 100", 100, 100, 100),
]

GROUP_NOTES = {
    "General-purpose processors": "Hardware caches move the data. You tile so panels stay resident in each cache level, "
                                  "and the innermost kernel is a register-allocation problem.",
    "Parallel accelerators": "Thousands of hardware threads. The hardware schedules thread blocks onto units; "
                             "you stage each block's tile through shared memory and registers.",
    "Software-managed accelerators": "No cache to fall back on. The tile and its DMA buffers must fit the on-chip memory "
                                     "exactly, and software decides what moves when.",
    "Simulated": "Devices that exist only as models, built to practice against.",
    "CPU with SIMD": "Wide vector registers, a few cores, three cache levels. Register count caps the micro-kernel.",
    "CPU with matrix extension": "A SIMD CPU with a tile-matrix unit. The unit's tile shape fixes the micro-kernel.",
    "GPU: SIMT + tensor cores": "Block tile in shared memory, accumulators in registers, tensor cores consume fixed-shape fragments. "
                                "Tile count versus unit count decides how full the machine is.",
    "DSP: vector + scratchpad": "Long vector registers and a small software-managed memory filled by DMA.",
    "NPU: matrix engine + scratchpad": "A fixed-shape matrix engine fed through several explicit buffer levels.",
    "NPU: spatial array of tiles": "Many small cores, each with its own local memory, joined by streams and DMA. "
                                   "Tiling is a placement problem as much as a size problem.",
    "NPU: microcontroller class": "Small NPUs next to a microcontroller. The network is compiled ahead of time and weights stream from flash.",
    "Systolic array": "A large fixed grid of multiply-accumulate cells. Data flows through the grid, so shapes pad to the grid.",
    "Teaching": "Simulators with a written manual, for practicing bring-up.",
}

ARCHETYPE_TEXT = {
    "cache_cpu": {"short": "Hardware caches move the data", "tile": "a hint: oversized tiles run slower"},
    "simt_gpu": {"short": "Hardware schedules blocks, you stage tiles in shared memory", "tile": "a per-block limit: oversized tiles cost occupancy"},
    "scratchpad_dma": {"short": "Software owns the memory and the DMA", "tile": "a contract: oversized tiles do not run"},
}


def slim(plan):
    keep = ["archetype", "dtype", "M", "N", "K", "Mp", "Np", "Kp", "outer", "inner", "resident", "tiles_list", "notes", "flops",
            "flops_padded", "traffic", "ai", "balance", "t_comp", "t_dma", "t", "bound", "pct_peak", "tiles", "units"]
    return {k: plan[k] for k in keep}


def build_data():
    devs = D.load()
    plans = {}
    for d in devs:
        for pid, _label, M, N, K in PRESETS:
            for dt in d["dtypes"]:
                try:
                    plans.setdefault(d["id"], {}).setdefault(pid, {})[dt] = slim(P.plan(d, dt, M, N, K))
                except P.NoFit:
                    pass
    return {
        "devices": devs,
        "plans": plans,
        "presets": [{"id": i, "label": l, "M": m, "N": n, "K": k} for i, l, m, n, k in PRESETS],
        "group_notes": GROUP_NOTES,
        "archetypes": ARCHETYPE_TEXT,
    }


def build(outdir):
    os.makedirs(outdir, exist_ok=True)
    for name in os.listdir(WEB):
        src = os.path.join(WEB, name)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(outdir, name))
    with open(os.path.join(outdir, "data.json"), "w") as f:
        json.dump(build_data(), f, separators=(",", ":"))
    open(os.path.join(outdir, ".nojekyll"), "w").close()
    return outdir
