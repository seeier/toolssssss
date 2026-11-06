"""check_system.py 的测试。只使用标准库 unittest，直接运行即可。"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

# 确保无论从哪个目录启动，都能找到同一文件夹里的 check_system.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_system as cs  # noqa: E402


class HumanSizeTests(unittest.TestCase):
    def test_small_numbers_stay_in_bytes(self):
        self.assertEqual(cs.human_size(0), "0.0 B")
        self.assertEqual(cs.human_size(1023), "1023.0 B")

    def test_units_step_up_by_1024(self):
        self.assertEqual(cs.human_size(1024), "1.0 KB")
        self.assertEqual(cs.human_size(1024 ** 3), "1.0 GB")

    def test_very_large_values_use_tb(self):
        self.assertEqual(cs.human_size(1024 ** 5), "1024.0 TB")


class HumanDurationTests(unittest.TestCase):
    def test_formats_days_hours_minutes(self):
        seconds = 2 * 86400 + 3 * 3600 + 5 * 60
        self.assertEqual(cs.human_duration(seconds), "2 天 3 小时 5 分钟")

    def test_zero_shows_minutes(self):
        self.assertEqual(cs.human_duration(0), "0 分钟")


class NetstatParserTests(unittest.TestCase):
    WINDOWS_SAMPLE = (
        "Active Connections\n\n"
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1412\n"
        "  TCP    192.168.0.5:2241       100.1.2.3:443          ESTABLISHED     2000\n"
        "  UDP    0.0.0.0:123            *:*                                    7828\n"
    )

    def setUp(self):
        # 不真正调用 tasklist，避免测试依赖本机进程
        self._original = cs.windows_pid_names
        cs.windows_pid_names = lambda: {"1412": "svchost"}

    def tearDown(self):
        cs.windows_pid_names = self._original

    def test_listening_keeps_only_listening_tcp_and_all_udp(self):
        rows = cs.parse_netstat_windows(self.WINDOWS_SAMPLE, listening=True)
        addresses = [row["监听地址"] for row in rows]
        self.assertIn("0.0.0.0:135", addresses)
        self.assertIn("0.0.0.0:123", addresses)
        self.assertNotIn("192.168.0.5:2241", addresses)

    def test_known_pid_shows_name_unknown_shows_pid_only(self):
        rows = cs.parse_netstat_windows(self.WINDOWS_SAMPLE, listening=True)
        by_address = {row["监听地址"]: row["进程"] for row in rows}
        self.assertEqual(by_address["0.0.0.0:135"], "svchost（PID 1412）")
        self.assertEqual(by_address["0.0.0.0:123"], "PID 7828")

    def test_connections_keep_only_established(self):
        rows = cs.parse_netstat_windows(self.WINDOWS_SAMPLE, listening=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["对方"], "100.1.2.3:443")

    def test_unix_parser_reads_ss_output(self):
        sample = (
            "Netid State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
            'tcp   LISTEN 0      128    0.0.0.0:22          0.0.0.0:*     users:(("sshd",pid=900,fd=3))\n'
        )
        rows = cs.parse_netstat_unix(sample)
        self.assertEqual(rows[0]["监听地址"], "0.0.0.0:22")
        self.assertEqual(rows[0]["进程"], "sshd（PID 900）")


class FileScanTests(unittest.TestCase):
    def setUp(self):
        # 放在项目目录下，避免系统临时目录的权限限制
        base = Path(__file__).resolve().parent / ".test_tmp"
        base.mkdir(exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self._tmp.name)
        (self.root / "small.txt").write_bytes(b"a" * 10)
        (self.root / "big.bin").write_bytes(b"b" * 5000)
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "copy_of_small.txt").write_bytes(b"a" * 10)
        (sub / "other.txt").write_bytes(b"c" * 300)
        ignored = self.root / ".git"
        ignored.mkdir()
        (ignored / "config").write_bytes(b"x" * 99999)

    def tearDown(self):
        self._tmp.cleanup()

    def test_large_files_sorted_and_skip_git(self):
        rows = cs.collect_large_files(self.root, top=10)
        self.assertTrue(rows[0]["文件"].endswith("big.bin"))
        self.assertFalse(any(".git" in row["文件"] for row in rows))

    def test_duplicates_found_by_content(self):
        groups = cs.collect_duplicates(self.root, top=10)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["份数"], 2)
        self.assertEqual(groups[0]["可省空间"], cs.human_size(10))

    def test_folder_sizes_group_top_level(self):
        rows = cs.collect_folder_sizes(self.root, top=10)
        names = [row["名称"] for row in rows]
        self.assertIn("sub", names)
        self.assertIn("（直接放在这一层的文件）", names)

    def test_duplicates_do_not_delete_anything(self):
        before = sorted(p.name for p in self.root.rglob("*") if p.is_file())
        cs.collect_duplicates(self.root, top=10)
        after = sorted(p.name for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(before, after)


class CliTests(unittest.TestCase):
    def run_main(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = cs.main(argv)
        return code, buffer.getvalue()

    def test_missing_directory_returns_2(self):
        code, _ = self.run_main(["--large-files", str(Path(tempfile.gettempdir()) / "no-such-dir-xyz")])
        self.assertEqual(code, 2)

    def test_json_output_is_valid_and_has_sections(self):
        cs.warnings.clear()
        cs.skipped.clear()
        code, text = self.run_main(["--json", "--quiet"])
        self.assertIn(code, (0, 1))
        # --quiet 只影响文字输出，JSON 仍应是完整的
        data = json.loads(text)
        for key in ("system", "cpu", "memory", "disks", "network", "warnings", "skipped"):
            self.assertIn(key, data)

    def test_output_writes_only_to_requested_file(self):
        cs.warnings.clear()
        cs.skipped.clear()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "report.txt"
            code, _ = self.run_main(["--output", str(target)])
            self.assertIn(code, (0, 1))
            self.assertTrue(target.is_file())
            self.assertIn("系统检查报告", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
