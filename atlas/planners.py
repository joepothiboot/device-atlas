"""Teaching tiling planners for C[M,N] = A[M,K] @ B[K,N].

Three constraint sets share one cost model:
  cache_cpu       hardware caches hold the tiles; we size panels so they stay resident
  simt_gpu        thread-block tile in shared memory, accumulators in registers
  scratchpad_dma  tile + double buffers must fit a software-managed scratchpad

These are models for building intuition, not measurements. See README for limits.
"""
import math

DT = {"f32": (4, 4), "f16": (2, 4), "bf16": (2, 4), "i8": (1, 4)}  # (input bytes, accumulator/output bytes)


def cdiv(a, b):
    return -(-a // b)


def rup(a, b):
    return cdiv(a, b) * b


def role(dev, r):
    for m in dev["memory"]:
        if m["role"] == r:
            return m
    return None


def role_bytes(dev, r):
    m = role(dev, r)
    return m["kb"] * 1024 if m else 0


def llc_bytes(dev):
    """Largest shared cache (last-level) used for tile-group reuse on GPUs."""
    best = 0
    for m in dev["memory"]:
        if m["role"] in ("l2", "l3") and m["scope"] == "shared":
            best = max(best, m["kb"] * 1024)
    return best


def peak_flops(dev, dt):
    g = dev["peak_gflops"].get(dt)
    return g * 1e9 if g else None


def dram_bw(dev):
    g = dev["dram"].get("gbps")
    return g * 1e9 if g else None


def cands(align, padded):
    s = {align * i for i in range(1, 17)} | {align * 2 ** j for j in range(0, 14)} | {padded}
    return sorted(x for x in s if x <= padded)


def cost(dev, dt, Mp, Np, Kp, tm, tn, tk, use_llc):
    """Time model for an output-stationary tile loop spread over the device's units.

    Two data-movement terms: DRAM traffic (after tile-group reuse in a shared cache, if any) and, for
    cache-backed GPUs, the tile-level traffic the SMs pull from that cache (assumed `llc_bw_factor` x DRAM
    bandwidth). Without the second term nothing would punish tiny tiles. For scratchpad devices, DMA rows
    shorter than the burst size run below full bandwidth.
    """
    in_b, out_b = DT[dt]
    units = dev["units"]["count"]
    n_m, n_n = cdiv(Mp, tm), cdiv(Np, tn)
    tiles = n_m * n_n
    llc = llc_bytes(dev) if use_llc else 0
    g = 1
    if llc:
        for cand in range(max(n_m, n_n), 0, -1):
            if cand * (tm + tn) * Kp * in_b <= 0.5 * llc:
                g = cand
                break
    gm, gn = min(g, n_m), min(g, n_n)
    a_passes, b_passes = cdiv(n_n, gn), cdiv(n_m, gm)
    traffic = Mp * Kp * in_b * a_passes + Kp * Np * in_b * b_passes + Mp * Np * out_b
    rounds = cdiv(tiles, units)
    peak, bw = peak_flops(dev, dt), dram_bw(dev)
    t_comp = rounds * 2 * tm * tn * Kp / (peak / units) if peak else None
    lat = ((dev.get("dma") or {}).get("latency_ns")) or 0
    t_lat = rounds * (2 * cdiv(Kp, tk) + 1) * lat * 1e-9
    dma = dev.get("dma") or {}
    eff = min(1.0, min(tk, tn) * in_b / dma.get("burst_bytes", 256)) if not use_llc else 1.0
    traffic_eff = traffic / eff
    t_dma = traffic_eff / bw + t_lat if bw else None
    if t_dma is not None and llc:
        tile_traffic = Mp * Kp * in_b * n_n + Kp * Np * in_b * n_m + Mp * Np * out_b
        t_dma = max(t_dma, tile_traffic / (dev.get("llc_bw_factor", 3) * bw))
    t = max(t_comp, t_dma) if (t_comp is not None and t_dma is not None) else None
    return dict(n_m=n_m, n_n=n_n, tiles=tiles, rounds=rounds, group=g, gm=gm, gn=gn, traffic=traffic, traffic_eff=traffic_eff,
                t_comp=t_comp, t_dma=t_dma, t=t)


def objective(c, tm, tn):
    return (c["t"] if c["t"] is not None else c["traffic_eff"], c["traffic_eff"], -tm * tn)


def finish(dev, dt, M, N, K, Mp, Np, Kp, c, plan):
    peak, bw = peak_flops(dev, dt), dram_bw(dev)
    flops = 2 * M * N * K
    plan.update(dict(
        dtype=dt, M=M, N=N, K=K, Mp=Mp, Np=Np, Kp=Kp, flops=flops, flops_padded=2 * Mp * Np * Kp,
        traffic=c["traffic"], ai=flops / c["traffic"], balance=(peak / bw if peak and bw else None),
        t_comp=c["t_comp"], t_dma=c["t_dma"], t=c["t"], tiles=c["tiles"], units=dev["units"]["count"],
    ))
    if c["t"] is not None:
        plan["bound"] = "compute" if c["t_comp"] >= c["t_dma"] else "memory"
        plan["pct_peak"] = 100.0 * flops / (c["t"] * peak)
    else:
        plan["bound"] = "n/a"
        plan["pct_peak"] = None
    return plan


class NoFit(Exception):
    pass


# --------------------------------------------------------------------------- CPU
def plan_cpu(dev, dt, M, N, K):
    in_b, out_b = DT[dt]
    vec, mu = dev["vector"], dev.get("matrix_unit")
    if mu and dt not in mu["dtype"]:
        mu = None
    acc_lanes = vec["bits"] // (8 * out_b)
    notes = []
    if mu:
        mr, nr, k_align = 2 * mu["m"], 2 * mu["n"], mu["k"]
        reg_use, reg_cap = 8, 8
        inner = f"{mr}x{nr} = 2x2 {mu['m']}x{mu['n']} AMX tiles"
        notes.append(f"Micro-kernel is dictated by the matrix unit: 4 C tiles + 2 A + 2 B = 8 tile registers. "
                     f"Vector registers ({vec['regs']}) are not the limit here.")
    else:
        best = None
        for nrv in range(1, 5):
            mrv = (vec["regs"] - nrv - 1) // nrv
            if mrv < 1:
                continue
            key = (mrv * nrv / (mrv + nrv), -nrv)  # FMAs per load: maximize register reuse
            if best is None or key > best[0]:
                best = (key, mrv, nrv)
        _, mr, nrv = best
        nr, k_align = nrv * acc_lanes, 1
        reg_use, reg_cap = mr * nrv + nrv + 1, vec["regs"]
        inner = f"{mr}x{nr} in registers"
        notes.append(f"Registers set the micro-kernel: {mr}x{nrv} accumulators + {nrv} B vectors + 1 A broadcast = "
                     f"{reg_use} of {reg_cap} vector registers; each k step does {mr * nrv} FMAs for {mr + nrv} loads.")
    l1, l2, l3 = role_bytes(dev, "l1"), role_bytes(dev, "l2"), role_bytes(dev, "l3")
    kc = max(8, int(0.5 * l1 // ((mr + nr) * in_b)) // k_align * k_align)
    kc = min(kc, rup(K, k_align))
    mc = min(max(mr, int(0.5 * l2 // (kc * in_b)) // mr * mr), M)
    nc = min(max(nr, int(0.25 * l3 // (kc * in_b)) // nr * nr), N) if l3 else N
    c_passes = 1 if M * nc * out_b <= 0.25 * l3 else 2 * cdiv(K, kc) - 1
    traffic = M * K * in_b * cdiv(N, nc) + K * N * in_b + M * N * out_b * c_passes
    peak, bw = peak_flops(dev, dt), dram_bw(dev)
    t_comp = 2 * M * N * K / peak if peak else None
    t_dma = traffic / bw if bw else None
    t = max(t_comp, t_dma) if (t_comp and t_dma) else None
    c = dict(traffic=traffic, t_comp=t_comp, t_dma=t_dma, t=t, tiles=cdiv(M, mc) * cdiv(N, nc))
    tiles = [
        dict(level="registers", shape=f"{mr} x {nr}", use=reg_use, cap=reg_cap, unit="regs"),
        dict(level="L1 (micro-panels)", shape=f"({mr}+{nr}) x kc={kc}", use=(mr + nr) * kc * in_b, cap=l1),
        dict(level="L2 (A block)", shape=f"mc={mc} x kc={kc}", use=mc * kc * in_b, cap=l2),
        dict(level="L3 (B panel)", shape=f"kc={kc} x nc={nc}", use=kc * nc * in_b, cap=l3),
    ]
    notes += [
        "Nothing is explicitly moved: the loop order (BLIS: jc, pc, ic, jr, ir) is arranged so each panel is reused "
        "while it still sits in the cache. Sizes use about half of each level to leave room for the other operand.",
        f"Threads: {dev['units']['count']} cores split the ic/jr loops; each core streams its own L1/L2 blocks.",
    ]
    plan = dict(archetype="cache_cpu", tiles_list=tiles, outer=f"{mc}x{nc}x{kc}", inner=inner,
                resident="hardware caches", notes=notes)
    return finish(dev, dt, M, N, K, M, N, K, c, plan)


# --------------------------------------------------------------------------- GPU
def plan_gpu(dev, dt, M, N, K):
    in_b, out_b = DT[dt]
    mu, g = dev["matrix_unit"], dev["gpu"]
    smem_cap, stages = role_bytes(dev, "smem"), g["stages"]
    best = None
    for tm in (16, 32, 64, 128, 256):
        if tm % mu["m"]:
            continue
        for tn in (16, 32, 64, 128, 256):
            if tn % mu["n"]:
                continue
            for tk in (16, 32, 64, 128):
                if tk % mu["k"]:
                    continue
                threads = 256 if tm * tn >= 128 * 128 else (128 if tm * tn >= 64 * 64 else 64)
                acc = tm * tn // threads
                smem_blk = (tm * tk + tk * tn) * in_b * stages
                if acc > 160 or smem_blk > smem_cap:
                    continue
                Mp, Np, Kp = rup(M, tm), rup(N, tn), rup(K, tk)
                c = cost(dev, dt, Mp, Np, Kp, tm, tn, tk, use_llc=True)
                bps = max(1, min(smem_cap // smem_blk, g["regs_per_unit"] // (threads * (acc + 48)), 16))
                key = objective(c, tm, tn) + (-tk,)
                if best is None or key < best[0]:
                    best = (key, tm, tn, tk, threads, acc, smem_blk, bps, Mp, Np, Kp, c)
    if best is None:
        raise NoFit("no thread-block tile fits")
    _, tm, tn, tk, threads, acc, smem_blk, bps, Mp, Np, Kp, c = best
    llc = llc_bytes(dev)
    tiles = [
        dict(level="shared cache group", shape=f"{c['gm']}x{c['gn']} tiles = {c['gm'] * tm}x{c['gn'] * tn}",
             use=c["gm"] * tm * Kp * in_b + c["gn"] * tn * Kp * in_b, cap=llc),
        dict(level="shared memory (block tile)", shape=f"{tm} x {tn} x {tk}, {stages} stages", use=smem_blk, cap=smem_cap),
        dict(level="registers (accumulators)", shape=f"{acc} fp32/thread x {threads} threads", use=acc + 48,
             cap=g["max_regs_thread"], unit="regs/thread"),
        dict(level="matrix instruction", shape=f"{mu['m']}x{mu['n']}x{mu['k']} ({mu['name']})", use=None, cap=None),
    ]
    notes = [
        f"The block tile lives in shared memory ({stages} pipeline stages of A+B tiles): {smem_blk // 1024} KB of "
        f"{smem_cap // 1024} KB. Tile sides must be multiples of the matrix instruction ({mu['m']}x{mu['n']}x{mu['k']}).",
        f"{tm * tn} accumulators are spread over {threads} threads = {acc} registers each; {bps} block(s) fit per "
        f"{dev['units']['name']} (limited by shared memory and registers).",
        f"Hardware schedules blocks: {c['tiles']} blocks over {dev['units']['count']} {dev['units']['name']}s = "
        f"{c['rounds']} wave(s), utilization {100 * c['tiles'] / (c['rounds'] * dev['units']['count']):.0f}% "
        f"(wave quantization).",
        f"Grouping {c['gm']}x{c['gn']} neighboring blocks keeps their A/B panels in the shared cache (rasterization/"
        f"swizzle), which is what lets the tile be small without every block re-reading DRAM.",
    ]
    if (Mp, Np, Kp) != (M, N, K):
        notes.append(f"Edge blocks still run a full tile: padded to {Mp}x{Np}x{Kp}.")
    if c["tiles"] < dev["units"]["count"]:
        notes.append("Fewer tiles than units: real kernels switch to split-K / stream-K here.")
    plan = dict(archetype="simt_gpu", tiles_list=tiles, outer=f"{tm}x{tn}x{tk}", inner=f"{mu['m']}x{mu['n']}x{mu['k']} mma",
                resident="shared memory", notes=notes)
    return finish(dev, dt, M, N, K, Mp, Np, Kp, c, plan)


# --------------------------------------------------------------------------- scratchpad
def plan_scratchpad(dev, dt, M, N, K):
    in_b, out_b = DT[dt]
    mu, vec = dev.get("matrix_unit"), dev.get("vector")
    if mu and dt not in mu["dtype"]:
        mu = None
    lanes = vec["bits"] // (8 * in_b) if vec else 1
    align_bytes = (dev.get("dma") or {}).get("align_bytes", 1)
    align_m = dev.get("align_m") or (mu["m"] if mu else 1)
    align_n = mu["n"] if mu else max(lanes, align_bytes // in_b, 1)
    align_k = mu["k"] if mu else 1
    Mp, Np, Kp = rup(M, align_m), rup(N, align_n), rup(K, align_k)
    spm = int(role_bytes(dev, "spm") * 0.9)
    best = None
    for tm in cands(align_m, Mp):
        for tn in cands(align_n, Np):
            c_bytes = tm * tn * out_b
            if c_bytes >= spm:
                continue
            for tk in cands(align_k, Kp):
                foot = 2 * (tm * tk + tk * tn) * in_b + c_bytes
                if foot > spm:
                    break
                c = cost(dev, dt, Mp, Np, Kp, tm, tn, tk, use_llc=False)
                key = objective(c, tm, tn) + (-tk,)
                if best is None or key < best[0]:
                    best = (key, tm, tn, tk, foot, c)
    if best is None:
        raise NoFit("no tile fits the scratchpad")
    _, tm, tn, tk, foot, c = best
    unit = (f"{mu['name']}: {mu['m']}x{mu['n']}x{mu['k']}" if mu else f"{lanes}-lane vector ({vec['bits']}-bit)")
    tiles = [
        dict(level="scratchpad (all buffers)", shape=f"tile {tm} x {tn} x {tk}", use=foot, cap=role_bytes(dev, "spm")),
        dict(level=" A,B double buffers", shape=f"2 x ({tm}x{tk} + {tk}x{tn})", use=2 * (tm * tk + tk * tn) * in_b, cap=None),
        dict(level=" C accumulator tile", shape=f"{tm} x {tn} x {out_b}B", use=tm * tn * out_b, cap=None),
        dict(level="compute unit", shape=unit, use=None, cap=None),
    ]
    notes = [
        f"No cache: the tile and its double buffers must fit the scratchpad exactly "
        f"({foot / 1024:.0f} KB of {role_bytes(dev, 'spm') // 1024} KB, 10% reserved). Overflow is a compile/run failure, not a slowdown.",
        f"Alignment comes from the hardware: tm multiple of {align_m}, tn of {align_n}, tk of {align_k}. "
        f"Padding to that grid: {Mp}x{Np}x{Kp}.",
        "Software issues the DMA and decides what to overlap: while the unit computes on one buffer, the next A/B "
        "tiles stream into the other.",
    ]
    lat = (dev.get("dma") or {}).get("latency_ns")
    if lat:
        notes.append(f"Each DMA pays about {lat} ns setup, so fewer, larger transfers win: tk={tk} gives "
                     f"{2 * cdiv(Kp, tk) + 1} transfers per output tile.")
    if c["tiles"] < dev["units"]["count"]:
        notes.append(f"Only {c['tiles']} output tiles for {dev['units']['count']} units: the rest sit idle.")
    plan = dict(archetype="scratchpad_dma", tiles_list=tiles, outer=f"{tm}x{tn}x{tk}", inner=unit,
                resident="software-managed scratchpad", notes=notes)
    return finish(dev, dt, M, N, K, Mp, Np, Kp, c, plan)


PLANNERS = {"cache_cpu": plan_cpu, "simt_gpu": plan_gpu, "scratchpad_dma": plan_scratchpad}


def plan(dev, dt, M, N, K):
    if dt not in dev["dtypes"]:
        raise NoFit(f"{dev['id']} has no native {dt} path (supports {', '.join(dev['dtypes'])})")
    return PLANNERS[dev["archetype"]](dev, dt, M, N, K)
