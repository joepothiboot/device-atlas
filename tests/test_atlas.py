import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from atlas import devices as D  # noqa: E402
from atlas import planners as P  # noqa: E402
from atlas import cli  # noqa: E402

DEVS = D.load()
SIZES = [(1024, 1024, 1024), (16, 4096, 4096), (100, 100, 100), (4096, 4096, 4096), (1, 1024, 1024)]


class Database(unittest.TestCase):
    def test_entries_valid(self):
        for d in DEVS:
            self.assertEqual(D.validate(d), [], d["id"])

    def test_every_device_says_what_it_is_used_for(self):
        for d in DEVS:
            self.assertIn(d["segment"], D.SEGMENTS, d["id"])
            self.assertGreaterEqual(len(d["used_for"]), 1, d["id"])
            for u in d["used_for"]:
                self.assertGreater(len(u), 20, f"{d['id']}: purpose too terse: {u!r}")

    def test_unique_ids(self):
        ids = [d["id"] for d in DEVS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_device_in_tree(self):
        text = D.render_tree(DEVS)
        for d in DEVS:
            self.assertIn(d["id"], text)

    def test_unverified_entries_are_flagged(self):
        # Honesty check: only the simulator we wrote ourselves may claim verified=True.
        for d in DEVS:
            if d["verified"]:
                self.assertEqual(d["archetype"], "scratchpad_dma")
                self.assertEqual(d["id"], "mn1-mock")

    def test_validate_catches_bad_entries(self):
        bad = dict(DEVS[0]); bad["archetype"] = "magic"
        self.assertTrue(D.validate(bad))
        bad = dict(DEVS[0]); del bad["memory"]
        self.assertTrue(D.validate(bad))
        bad = dict(DEVS[0]); bad["used_for"] = []
        self.assertTrue(D.validate(bad))
        bad = dict(DEVS[0]); bad["segment"] = "Moon"
        self.assertTrue(D.validate(bad))


class Planners(unittest.TestCase):
    def all_plans(self):
        for d in DEVS:
            for dt in d["dtypes"]:
                for (M, N, K) in SIZES:
                    yield d, dt, (M, N, K), P.plan(d, dt, M, N, K)

    def test_capacities_respected(self):
        for d, dt, mnk, p in self.all_plans():
            for t in p["tiles_list"]:
                if t["use"] is not None and t["cap"]:
                    self.assertLessEqual(t["use"], t["cap"], f"{d['id']} {dt} {mnk} {t['level']}")

    def test_never_exceeds_peak(self):
        for d, dt, mnk, p in self.all_plans():
            if p["pct_peak"] is not None:
                self.assertLessEqual(p["pct_peak"], 100.0001, f"{d['id']} {dt} {mnk}")
            self.assertGreaterEqual(p["flops_padded"], p["flops"])

    def test_cpu_register_budget(self):
        for d in DEVS:
            if d["archetype"] != "cache_cpu":
                continue
            p = P.plan(d, "f32", 1024, 1024, 1024)
            regs = p["tiles_list"][0]
            self.assertLessEqual(regs["use"], regs["cap"])
            self.assertIn("registers", regs["level"])

    def test_matrix_unit_alignment_padding(self):
        tpu = D.by_id(DEVS, "google-tpu-v5e")
        p = P.plan(tpu, "bf16", 100, 100, 100)
        self.assertEqual((p["Mp"], p["Np"], p["Kp"]), (104, 128, 128))  # (8,128,128) grid
        self.assertGreater(p["flops_padded"], p["flops"])

    def test_gpu_tile_quantization_waste(self):
        h100 = D.by_id(DEVS, "nvidia-h100")
        p = P.plan(h100, "f16", 16, 4096, 4096)
        self.assertGreaterEqual(p["Mp"], 64)  # wgmma needs M in multiples of 64
        self.assertGreater(p["flops_padded"], p["flops"])

    def test_skinny_gemm_is_memory_bound_everywhere(self):
        for d in DEVS:
            if d["id"] == "mn1-mock" or not d["peak_gflops"] or d["dram"].get("gbps") is None:
                continue
            for dt in d["dtypes"]:
                if dt in d["peak_gflops"]:
                    self.assertEqual(P.plan(d, dt, 1, 4096, 4096)["bound"], "memory", f"{d['id']} {dt}")

    def test_same_problem_gives_different_plans(self):
        rows, _ = cli.compare_rows(DEVS, "f16", 4096, 4096, 4096)
        self.assertGreaterEqual(len({r[4] for r in rows}), 2)   # shared memory vs software scratchpad
        self.assertGreaterEqual(len({r[2] for r in rows}), 4)   # different outer tiles
        rows, _ = cli.compare_rows(DEVS, "f32", 1024, 1024, 1024)
        self.assertIn("hardware caches", {r[4] for r in rows})

    def test_unsupported_dtype(self):
        with self.assertRaises(P.NoFit):
            P.plan(D.by_id(DEVS, "x86-avx2"), "f16", 64, 64, 64)

    def test_scratchpad_overflow_is_a_failure_not_a_slowdown(self):
        tiny = dict(D.by_id(DEVS, "mn1-mock"))
        tiny["memory"] = [{"name": "SPM", "role": "spm", "kb": 0, "scope": "per_unit"}]
        with self.assertRaises(P.NoFit):
            P.plan(tiny, "f32", 64, 64, 64)

    def test_deterministic(self):
        d = D.by_id(DEVS, "nvidia-a100")
        self.assertEqual(P.plan(d, "f16", 2048, 2048, 2048)["outer"], P.plan(d, "f16", 2048, 2048, 2048)["outer"])


class Site(unittest.TestCase):
    def test_data_covers_every_device_and_preset(self):
        from atlas import site
        data = site.build_data()
        self.assertEqual([d["id"] for d in data["devices"]], [d["id"] for d in DEVS])
        for d in DEVS:
            self.assertIn(d["id"], data["plans"], d["id"])
            for pr in data["presets"]:
                self.assertIn(pr["id"], data["plans"][d["id"]])
                self.assertEqual(set(data["plans"][d["id"]][pr["id"]]), set(d["dtypes"]))
        for d in DEVS:
            for node in d["path"][:2]:  # class and subclass carry a note; families do not
                self.assertIn(node, data["group_notes"], f"no group note for '{node}'")

    def test_data_is_plain_json(self):
        import json
        from atlas import site
        json.dumps(site.build_data(), allow_nan=False)  # no NaN/inf leaking into the page

    def test_tile_roles_name_real_memory_levels(self):
        from atlas import site
        data = site.build_data()
        roles = {d["id"]: {m["role"] for m in d["memory"]} for d in DEVS}
        for did, presets in data["plans"].items():
            for by_dt in presets.values():
                for plan in by_dt.values():
                    for t in plan["tiles_list"]:
                        if t.get("role"):
                            self.assertIn(t["role"], roles[did], f"{did}: {t['level']}")

    def test_build_writes_assets(self):
        import tempfile
        from atlas import site
        with tempfile.TemporaryDirectory() as tmp:
            site.build(tmp)
            self.assertTrue(os.path.exists(os.path.join(tmp, "data.json")))
            self.assertTrue(os.path.exists(os.path.join(tmp, ".nojekyll")))


class Web(unittest.TestCase):
    """Pages serves the site under /device-atlas/, so every asset path must be relative."""
    WEB = os.path.join(os.path.dirname(__file__), "..", "web")

    def read(self, name):
        with open(os.path.join(self.WEB, name)) as f:
            return f.read()

    def test_index_links_to_existing_local_files(self):
        html = self.read("index.html")
        for ref in ("style.css", "app.js"):
            self.assertIn(ref, html)
            self.assertTrue(os.path.exists(os.path.join(self.WEB, ref)), ref)

    def test_no_root_absolute_paths(self):
        html, js = self.read("index.html"), self.read("app.js")
        for needle in ('href="/', 'src="/', "fetch(\"/"):
            self.assertNotIn(needle, html + js)

    def test_ui_only_reads_fields_the_data_provides(self):
        # app.js reads these plan keys; they must exist in every generated plan
        from atlas import site
        data = site.build_data()
        needed = {"outer", "inner", "resident", "tiles_list", "notes", "flops", "flops_padded", "ai", "balance",
                  "traffic", "t", "t_comp", "t_dma", "bound", "pct_peak"}
        for presets in data["plans"].values():
            for by_dt in presets.values():
                for plan in by_dt.values():
                    self.assertTrue(needed <= set(plan), needed - set(plan))
        for k in ("devices", "plans", "presets", "group_notes", "archetypes"):
            self.assertIn(k, data)


class Cli(unittest.TestCase):
    def test_commands_run(self):
        for argv in (["tree"], ["show", "nvidia-a100"], ["tile", "nvidia-a100"], ["compare"], ["audit"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(argv), 0, argv)

    def test_unknown_device(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["show", "nope"]), 2)


if __name__ == "__main__":
    unittest.main()
