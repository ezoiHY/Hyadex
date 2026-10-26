"""Shared real-PTY driver for explicit instance-management prompts."""
import os
import pty
import select
import subprocess
import time
import unittest

class TerminalFixture(unittest.TestCase):
    def run_terminal(self, script, answer="yes\n", environment=None, prompt_answers=None):
        master, slave = pty.openpty()
        process = None
        output = b""
        try:
            process = subprocess.Popen(["/bin/bash", "-c", script], stdin=slave, stdout=slave, stderr=slave, env=environment)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 5
            pending = list(prompt_answers) if prompt_answers is not None else [(b"[y/N]", answer)]
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                    if pending and pending[0][0] in output:
                        _, reply = pending.pop(0)
                        os.write(master, reply.encode())
                elif process.poll() is not None:
                    break
            # PTY EOF can arrive just before waitpid observes process exit.
            self.assertEqual(process.wait(timeout=1), 0, output.decode(errors="replace"))
            return output.decode().replace("\r\n", "\n")
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=2)
            os.close(master)
            if slave is not None:
                os.close(slave)
