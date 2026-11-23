"""Run these on Linux to verify actual owned process-group cleanup."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import psutil

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="kk-cleanup-tests-")
os.environ["KKGO_DATA_DIR"] = str(Path(TEMP.name) / "results")
sys.path.insert(0, str(ROOT))
from kkagent_bridge.python_mcp import MediaWorker, _ACTIVE_PIDS


def exited(pid):
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


class CleanupTests(unittest.TestCase):
    def test_owned_worker_and_child_exit(self):
        async def run():
            marker = Path(TEMP.name) / "child.pid"
            child = "import os,time,pathlib;pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid()));time.sleep(60)"
            parent = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);time.sleep(60)"
            worker = MediaWorker({})
            options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {"start_new_session": True}
            worker.process = await asyncio.create_subprocess_exec(sys.executable, "-c", parent, **options)
            process = worker.process
            _ACTIVE_PIDS.add(process.pid)
            try:
                for _ in range(100):
                    if marker.is_file():
                        break
                    await asyncio.sleep(.02)
                self.assertTrue(marker.is_file())
                child_pid = int(marker.read_text())
            finally:
                await worker.close()
            self.assertTrue(exited(process.pid))
            self.assertTrue(exited(child_pid))
            self.assertNotIn(process.pid, _ACTIVE_PIDS)
        asyncio.run(run())

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires a Linux kernel")
    def test_children_cleaned_after_group_leader_exits(self):
        async def run():
            marker = Path(TEMP.name) / "orphan.pid"
            child = "import os,time,pathlib;pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid()));time.sleep(60)"
            parent = "import subprocess,sys;subprocess.Popen([sys.executable,'-c'," + repr(child) + "])"
            worker = MediaWorker({})
            worker.process = await asyncio.create_subprocess_exec(sys.executable, "-c", parent, start_new_session=True)
            process = worker.process
            try:
                await asyncio.wait_for(process.wait(), 3)
                for _ in range(100):
                    if marker.is_file():
                        break
                    await asyncio.sleep(.02)
                self.assertTrue(marker.is_file())
                child_pid = int(marker.read_text())
            finally:
                await worker.close()
            for _ in range(100):
                if exited(child_pid):
                    break
                await asyncio.sleep(.02)
            self.assertTrue(exited(child_pid))
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
