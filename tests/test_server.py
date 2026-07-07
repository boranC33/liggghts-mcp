import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

import server


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.old_runs = server.RUNS
        self.old_bin = server.LIGGGHTS_BIN
        self.old_path = os.environ.get("PATH", "")
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "LIGGGHTS_READ_ONLY",
                "LIGGGHTS_ALLOW_OVERWRITE",
                "LIGGGHTS_MAX_RANKS",
                "LIGGGHTS_MAX_CONCURRENT_RUNS",
                "LIGGGHTS_ALLOWED_CASE_ROOTS",
                "LIGGGHTS_MAX_SWEEP_CASES",
                "LIGGGHTS_MAX_READ_BYTES",
                "LIGGGHTS_ALLOW_DECK_SHELL",
                "LIGGGHTS_VALIDATE_TIMEOUT_MAX",
                "LIGGGHTS_VIS_TIMEOUT",
                "OVITO_BIN",
                "OVITO_PYTHON",
                "PARAVIEW_BIN",
                "PVPYTHON_BIN",
                "PVBATCH_BIN",
            )
        }
        server.RUNS = self.tmp_path / "runs"
        server.RUNS.mkdir()
        server._PROCS.clear()

    def tearDown(self):
        for proc in list(server._PROCS.values()):
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=5)
        server._PROCS.clear()
        server.RUNS = self.old_runs
        server.LIGGGHTS_BIN = self.old_bin
        os.environ["PATH"] = self.old_path
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _run_dir(self, run_id: str = "run1") -> Path:
        wd = server.RUNS / run_id
        wd.mkdir()
        return wd

    def _fake_liggghts(self) -> Path:
        fake = self.tmp_path / "fake_liggghts.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import sys\n"
            "print('fake LIGGGHTS')\n"
            "deck = sys.argv[sys.argv.index('-in') + 1] if '-in' in sys.argv else None\n"
            "if deck:\n"
            "    print(Path(deck).read_text())\n"
            "Path('particles.dump').write_text('ITEM: TIMESTEP\\n0\\n')\n"
            "print('Step Atoms Temp')\n"
            "print('0 10 0.0')\n"
            "print('Loop time of 0.1 on 1 procs for 0 steps with 10 atoms')\n"
        )
        fake.chmod(0o755)
        server.LIGGGHTS_BIN = str(fake)
        return fake

    def _minimal_deck(self) -> str:
        return "units si\natom_style granular\nrun 0\n"

    def _fake_ovito_python(self) -> Path:
        fake = self.tmp_path / "fake_ovito_python.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import sys\n"
            "if '-c' in sys.argv:\n"
            "    print('fake ovito python')\n"
            "    raise SystemExit(0)\n"
            "out = Path(sys.argv[3])\n"
            "out.parent.mkdir(parents=True, exist_ok=True)\n"
            "out.write_text('fake ovito output')\n"
            "print('rendered', out)\n"
        )
        fake.chmod(0o755)
        os.environ["OVITO_PYTHON"] = str(fake)
        return fake

    def _fake_paraview_python(self) -> Path:
        fake = self.tmp_path / "fake_pvpython.py"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "from pathlib import Path\n"
            "import sys\n"
            "if '-c' in sys.argv:\n"
            "    print('paraview.simple')\n"
            "    raise SystemExit(0)\n"
            "if '--version' in sys.argv:\n"
            "    print('fake paraview 1.0')\n"
            "    raise SystemExit(0)\n"
            "out = Path(sys.argv[3])\n"
            "out.parent.mkdir(parents=True, exist_ok=True)\n"
            "out.write_text('fake paraview output')\n"
            "print('rendered', out)\n"
        )
        fake.chmod(0o755)
        os.environ["PVPYTHON_BIN"] = str(fake)
        os.environ["PVBATCH_BIN"] = str(fake)
        return fake

    def test_liggghts_bin_can_resolve_bare_path_command(self):
        bin_dir = self.tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "liggghts"
        fake.write_text("#!/bin/sh\necho fake-liggghts\n")
        fake.chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}:{self.old_path}"
        server.LIGGGHTS_BIN = "liggghts"

        self.assertEqual(server._liggghts_cmd(["-help"]), ["liggghts", "-help"])
        self.assertEqual(server._resolve_liggghts_executable(), fake)
        info = server.validate_liggghts_bin()
        self.assertTrue(info["exists"])
        self.assertTrue(info["executable"])
        self.assertEqual(info["liggghts_executable"], "liggghts")
        self.assertEqual(info["liggghts_resolved_path"], str(fake))
        self.assertEqual(info["version_line"], "fake-liggghts")

    def test_liggghts_bin_expands_user_paths_for_launch(self):
        home = self.tmp_path / "home"
        home.mkdir()
        old_home = os.environ.get("HOME", "")
        os.environ["HOME"] = str(home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home))
        server.LIGGGHTS_BIN = "~/bin/liggghts"

        self.assertEqual(
            server._liggghts_cmd(["-in", "input.in"]),
            [str(home / "bin" / "liggghts"), "-in", "input.in"],
        )

    def test_liggghts_bin_resolves_relative_paths_for_run_cwd(self):
        server.LIGGGHTS_BIN = "./liggghts"

        self.assertEqual(
            server._liggghts_cmd([]),
            [str((Path.cwd() / "liggghts").resolve())],
        )

    def test_list_outputs_rejects_patterns_that_escape_run_dir(self):
        wd = self._run_dir()
        (wd / "particles.dump").write_text("inside\n")
        (server.RUNS / "outside.dump").write_text("outside\n")

        self.assertEqual(
            server.list_outputs("run1", patterns=["*.dump"]),
            [str(wd / "particles.dump")],
        )
        with self.assertRaises(ValueError):
            server.list_outputs("run1", patterns=["../*"])
        with self.assertRaises(ValueError):
            server.list_outputs("run1", patterns=[str(server.RUNS / "*.dump")])

    def test_list_outputs_empty_pattern_list_means_no_outputs(self):
        wd = self._run_dir()
        (wd / "particles.dump").write_text("inside\n")

        self.assertEqual(server.list_outputs("run1", patterns=[]), [])

    def test_list_outputs_does_not_follow_symlink_dirs_outside_run(self):
        wd = self._run_dir()
        outside = self.tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.dump").write_text("outside\n")
        (wd / "linkdir").symlink_to(outside, target_is_directory=True)

        self.assertEqual(
            server.list_outputs("run1", patterns=["linkdir/*"], include_bookkeeping=True),
            [],
        )

    def test_read_log_tail_zero_and_negative_tail(self):
        wd = self._run_dir()
        (wd / "log.run").write_text("a\nb\nc\n")

        self.assertEqual(server.read_log("run1", tail=0), "")
        self.assertEqual(server.read_log("run1", tail=2), "b\nc")
        with self.assertRaises(ValueError):
            server.read_log("run1", tail=-1)

    def test_read_output_and_summary_parse_log(self):
        wd = self._run_dir()
        (wd / "status.json").write_text(
            '{"run_id":"run1","status":"finished","exit_code":0,'
            '"started_at":"2026-01-01T00:00:00+00:00",'
            '"finished_at":"2026-01-01T00:00:02+00:00"}'
        )
        (wd / "log.run").write_text(
            "fake LIGGGHTS\n"
            "WARNING: minor issue\n"
            "Step Atoms Temp\n"
            "0 12 0.5\n"
            "Loop time of 0.2 on 1 procs for 0 steps with 12 atoms\n"
        )
        (wd / "particles.dump").write_text("abcdef")

        chunk = server.read_output("run1", "particles.dump", offset=1, max_bytes=3)
        self.assertEqual(chunk["content"], "bcd")
        self.assertTrue(chunk["truncated"])

        parsed = server.parse_log("run1")
        self.assertEqual(parsed["warning_count"], 1)
        self.assertEqual(parsed["last_thermo"]["Atoms"], 12)
        self.assertEqual(parsed["loop_summary"]["atoms"], 12)

        summary = server.summarize_run("run1")
        self.assertEqual(summary["status"], "finished")
        self.assertEqual(summary["duration_seconds"], 2.0)
        self.assertEqual(summary["output_count"], 1)

    def test_deck_safety_blocks_shell_by_default(self):
        deck = self._minimal_deck() + "shell rm -rf /tmp/nope\n"

        result = server.validate_deck(deck)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        with self.assertRaises(PermissionError):
            server.start_simulation(deck, name="unsafe")

    def test_read_only_disables_mutating_tools(self):
        os.environ["LIGGGHTS_READ_ONLY"] = "1"
        self._fake_liggghts()

        with self.assertRaises(PermissionError):
            server.start_simulation(self._minimal_deck(), name="blocked")

    def test_overwrite_requires_environment_opt_in(self):
        self._fake_liggghts()
        server.start_simulation(self._minimal_deck(), name="same")
        deadline = time.monotonic() + 5
        while server.check_status("same")["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)

        with self.assertRaises(PermissionError):
            server.start_simulation(self._minimal_deck(), name="same", overwrite=True)
        os.environ["LIGGGHTS_ALLOW_OVERWRITE"] = "1"
        status = server.start_simulation(self._minimal_deck(), name="same", overwrite=True)
        self.assertEqual(status["run_id"], "same")

    def test_case_roots_limit_start_from_file(self):
        self._fake_liggghts()
        allowed = self.tmp_path / "allowed"
        blocked = self.tmp_path / "blocked"
        allowed.mkdir()
        blocked.mkdir()
        good = allowed / "in.good"
        bad = blocked / "in.bad"
        good.write_text(self._minimal_deck())
        bad.write_text(self._minimal_deck())
        os.environ["LIGGGHTS_ALLOWED_CASE_ROOTS"] = str(allowed)

        server.start_from_file(str(good), name="good")
        with self.assertRaises(PermissionError):
            server.start_from_file(str(bad), name="bad")

    def test_parameter_sweep_starts_rendered_cases(self):
        self._fake_liggghts()
        result = server.start_parameter_sweep(
            "units si\natom_style granular\nvariable f equal {{friction}}\nrun 0\n",
            {"friction": [0.1, 0.2]},
            name_prefix="fric",
        )

        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["runs"][0]["parameters"], {"friction": 0.1})
        self.assertIn("0.1", (server.RUNS / "fric_001" / "input.in").read_text())

    def test_clone_run_edits_deck_and_starts_clone(self):
        self._fake_liggghts()
        src = self._run_dir("base")
        (src / "input.in").write_text("units si\natom_style granular\nvariable f equal 0.1\nrun 0\n")
        (src / "old.dump").write_text("old output")

        status = server.clone_run(
            "base",
            "clone",
            replacements={"0.1": "0.3"},
        )

        self.assertEqual(status["kind"], "clone_run")
        self.assertIn("0.3", (server.RUNS / "clone" / "input.in").read_text())
        self.assertFalse((server.RUNS / "clone" / "old.dump").exists())

    def test_resources_and_prompts_are_registered(self):
        self.assertIn("liggghts://config", {str(uri) for uri in server.mcp._resource_manager._resources})
        self.assertIn(
            "angle_of_repose_case",
            set(server.mcp._prompt_manager._prompts),
        )

    def test_ensure_standard_outputs_returns_and_writes_deck(self):
        deck = self._minimal_deck()
        result = server.ensure_standard_outputs(deck, dump_every=50, thermo_every=25)

        self.assertTrue(result["changed"])
        self.assertIn("thermo          25", result["updated_input_script"])
        self.assertIn("dump            mcp_dump all custom 50", result["updated_input_script"])
        self.assertLess(
            result["updated_input_script"].index("dump            mcp_dump"),
            result["updated_input_script"].index("run 0"),
        )

        wd = self._run_dir("deckrun")
        (wd / "input.in").write_text(deck)
        write_result = server.ensure_standard_outputs(
            run_id="deckrun",
            write_back=True,
            dump_filename="particles.dump",
        )
        self.assertTrue(write_result["changed"])
        self.assertIn("particles.dump", (wd / "input.in").read_text())

    def test_collect_run_artifacts_copies_outputs_and_manifest(self):
        wd = self._run_dir()
        (wd / "particles.dump").write_text("dump")
        (wd / "visualization").mkdir()
        (wd / "visualization" / "preview.png").write_text("png")

        result = server.collect_run_artifacts("run1")

        self.assertEqual(result["collected_count"], 2)
        self.assertTrue((wd / "artifacts" / "particles.dump").exists())
        self.assertTrue((wd / "artifacts" / "visualization" / "preview.png").exists())
        self.assertTrue((wd / "artifacts" / "manifest.json").exists())

    def test_ovito_conversion_and_render_use_run_local_outputs(self):
        self._fake_ovito_python()
        wd = self._run_dir()
        (wd / "particles.dump").write_text("ITEM: TIMESTEP\n0\n")

        converted = server.convert_dump_with_ovito(
            "run1",
            "particles.dump",
            output_relpath="converted/particles.xyz",
            overwrite=True,
        )
        self.assertTrue(converted["ok"])
        self.assertEqual(converted["export_format"], "xyz")
        self.assertTrue((wd / "converted" / "particles.xyz").exists())

        rendered = server.render_with_ovito(
            "run1",
            "particles.dump",
            output_relpath="visualization/ovito.png",
            overwrite=True,
        )
        self.assertTrue(rendered["ok"])
        self.assertTrue((wd / "visualization" / "ovito.png").exists())
        with self.assertRaises(ValueError):
            server.render_with_ovito("run1", "../outside.dump")

    def test_paraview_render_and_visual_summary(self):
        self._fake_paraview_python()
        wd = self._run_dir()
        (wd / "particles.vtk").write_text("# vtk DataFile Version 3.0\n")

        rendered = server.render_with_paraview(
            "run1",
            "particles.vtk",
            output_relpath="visualization/paraview.png",
            overwrite=True,
        )

        self.assertTrue(rendered["ok"])
        self.assertTrue((wd / "visualization" / "paraview.png").exists())
        summary = server.summarize_visual_outputs("run1")
        self.assertGreaterEqual(summary["visual_output_count"], 1)
        self.assertIn("image", summary["by_kind"])

    def test_validate_visualization_tools_reports_configured_commands(self):
        ovito = self._fake_ovito_python()
        pv = self._fake_paraview_python()

        result = server.validate_visualization_tools()

        self.assertEqual(result["ovito_python"]["bin"], str(ovito))
        self.assertTrue(result["ovito_python"]["probe"]["ok"])
        self.assertEqual(result["pvpython"]["bin"], str(pv))
        self.assertTrue(result["pvpython"]["probe"]["ok"])

    def test_stop_simulation_uses_sigkill_for_stubborn_process_group(self):
        wd = self._run_dir("stubborn")
        server._launch(
            wd,
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import signal,time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "Path('ready').write_text('1'); "
                    "time.sleep(60)"
                ),
            ],
            run_id="stubborn",
        )
        deadline = time.monotonic() + 5
        while not (wd / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue((wd / "ready").exists())

        result = server.stop_simulation("stubborn")
        self.assertEqual(result["run_id"], "stubborn")
        self.assertEqual(result["status"], "terminated")
        self.assertEqual(result["termination_signal"], "SIGKILL")
        self.assertFalse(server._process_group_alive(result["pgid"]))

        status = server.check_status("stubborn")
        self.assertEqual(status["status"], "finished")
        self.assertTrue(status["terminated_by_mcp"])
        self.assertEqual(status["termination_signal"], "SIGKILL")
        self.assertEqual(status["exit_code"], -signal.SIGKILL)
        self.assertIn("pid", status)
        self.assertEqual(status["pid"], result["pid"])


if __name__ == "__main__":
    unittest.main()
