"""Loading, validating and classifying the device database."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data", "devices.json")

ARCHETYPES = {
    "cache_cpu": "hardware caches move data; software tiles for them",
    "simt_gpu": "many threads + software-managed shared memory; hardware schedules blocks",
    "scratchpad_dma": "software owns all on-chip memory and the DMA that fills it",
}
DTYPES = {"f32", "f16", "bf16", "i8"}
SEGMENTS = ["Datacenter", "Client and mobile", "Edge and embedded", "Microcontroller", "Teaching"]
REQUIRED = ["id", "name", "path", "archetype", "units", "dtypes", "peak_gflops", "dram", "memory", "verified"]
ROLES_NEEDED = {"cache_cpu": ["l1", "l2"], "simt_gpu": ["smem"], "scratchpad_dma": ["spm"]}


def load(path=DATA):
    with open(path) as f:
        return json.load(f)["devices"]


def by_id(devs, dev_id):
    for d in devs:
        if d["id"] == dev_id:
            return d
    raise KeyError(f"unknown device '{dev_id}'. known: {', '.join(d['id'] for d in devs)}")


def validate(dev):
    """Return a list of problems (empty means the entry is well-formed)."""
    errs = []
    for k in REQUIRED:
        if k not in dev:
            errs.append(f"missing '{k}'")
    if errs:
        return errs
    if dev.get("segment") not in SEGMENTS:
        errs.append(f"segment must be one of {SEGMENTS}")
    if not dev.get("used_for"):
        errs.append("used_for must list at least one purpose")
    if len(dev["path"]) != 3:
        errs.append("path must have 3 levels (class, subclass, family)")
    if dev["archetype"] not in ARCHETYPES:
        errs.append(f"unknown archetype '{dev['archetype']}'")
        return errs
    for t in dev["dtypes"]:
        if t not in DTYPES:
            errs.append(f"unknown dtype '{t}'")
    for t in dev["peak_gflops"]:
        if t not in dev["dtypes"]:
            errs.append(f"peak given for unsupported dtype '{t}'")
    roles = {m["role"] for m in dev["memory"]}
    for r in ROLES_NEEDED[dev["archetype"]]:
        if r not in roles:
            errs.append(f"archetype {dev['archetype']} needs a memory level with role '{r}'")
    if dev["archetype"] == "simt_gpu" and not (dev.get("matrix_unit") and dev.get("gpu")):
        errs.append("simt_gpu needs 'matrix_unit' and 'gpu'")
    if dev["archetype"] == "cache_cpu" and not dev.get("vector"):
        errs.append("cache_cpu needs 'vector'")
    if dev["archetype"] == "scratchpad_dma" and not (dev.get("matrix_unit") or dev.get("vector")):
        errs.append("scratchpad_dma needs 'matrix_unit' or 'vector'")
    return errs


def tree(devs):
    root = {}
    for d in devs:
        node = root
        for p in d["path"]:
            node = node.setdefault(p, {})
        node.setdefault("_devices", []).append(d)
    return root


def render_tree(devs):
    lines = []

    def walk(node, prefix):
        keys = [k for k in node if k != "_devices"]
        items = [(k, node[k]) for k in keys] + [("_devices", d) for d in node.get("_devices", [])]
        for i, (k, v) in enumerate(items):
            last = i == len(items) - 1
            branch = "└── " if last else "├── "
            if k == "_devices":
                tag = "" if v["verified"] else "  (unverified)"
                lines.append(f"{prefix}{branch}{v['id']}: {v['name']}{tag}")
            else:
                lines.append(f"{prefix}{branch}{k}")
                walk(v, prefix + ("    " if last else "│   "))

    lines.append("Devices")
    walk(tree(devs), "")
    return "\n".join(lines)


def audit(devs):
    out = []
    for d in devs:
        problems = validate(d)
        missing = []
        if not d["peak_gflops"]:
            missing.append("no peak numbers (roofline unavailable)")
        if d["dram"].get("gbps") is None:
            missing.append("no DRAM bandwidth")
        if not d["verified"]:
            missing.append("not verified against: " + ", ".join(d.get("sources_to_check", ["?"])))
        out.append((d["id"], problems, missing))
    return out
