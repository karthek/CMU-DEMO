from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from travel_agent.itinerary.locking import MonitorLock, MonitorBusy


class LockTests(unittest.TestCase):
    def test_existence_is_not_ownership(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "db.sqlite3"
            with MonitorLock(path):
                with self.assertRaises(MonitorBusy):
                    with MonitorLock(path):
                        pass
            self.assertTrue(Path(str(path) + ".lock").exists())
            with MonitorLock(path):
                pass

    def test_other_process_blocked(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "db.sqlite3"
            script = "from travel_agent.itinerary.locking import MonitorLock, MonitorBusy\nimport sys\ntry:\n with MonitorLock(sys.argv[1]): pass\nexcept MonitorBusy:\n sys.exit(7)\n"
            with MonitorLock(path):
                result = subprocess.run([sys.executable, "-B", "-c", script, str(path)], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr)
