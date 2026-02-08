"""
Standalone API wrapper for Opus 4.6 via the Rovo Dev CLI.

Wraps `acli rovodev run` as a subprocess, routing through the mitmproxy
so intercept.py can modify system prompts / max_tokens as configured.
you have to do max_tokens = 8192

Requirements:
  - `acli` installed and authenticated (`acli rovodev auth login`)
  - mitmproxy running with intercept.py (`mitmdump -s intercept.py -p 8080`)

Usage:
    from rovodev_opus import RovoDevOpus

    client = RovoDevOpus()
    response = client.send("Explain Python generators in 2 sentences.")
    print(response)

    # Streaming (yields lines as they appear in the output file)
    for chunk in client.stream("Write a haiku about coding"):
        print(chunk, end="", flush=True)
"""

import os
import subprocess
import tempfile
import time
from typing import Generator, Optional


class RovoDevOpus:
    """Client for Opus 4.6 via the Rovo Dev CLI subprocess."""

    def __init__(
        self,
        proxy: str = "http://127.0.0.1:8080",
        working_dir: Optional[str] = None,
        poll_interval: float = 0.3,
        timeout: float = 300,
        yolo: bool = False,
    ):
        """
        Args:
            proxy: mitmproxy URL for request interception.
            working_dir: Working directory for the CLI. Defaults to cwd.
            poll_interval: How often to poll the output file (seconds).
            timeout: Max time to wait for a response (seconds).
            yolo: If True, run with --yolo flag (no tool confirmations).
        """
        self._proxy = proxy
        self._working_dir = working_dir or os.getcwd()
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._yolo = yolo

    def send(self, prompt: str) -> str:
        """
        Send a prompt to Opus 4.6 and return the full text response.

        Args:
            prompt: The message to send.

        Returns:
            The assistant's text response.
        """
        output_path = self._make_output_path()
        try:
            proc = self._start_cli(prompt, output_path)
            try:
                self._wait_for_output(proc, output_path)
                with open(output_path, "r") as f:
                    return f.read().strip()
            finally:
                self._kill(proc)
        finally:
            self._cleanup(output_path)

    def stream(self, prompt: str) -> Generator[str, None, None]:
        """
        Send a prompt and yield the response as it's written to the output file.

        Note: The CLI writes the full response at once, so this yields the
        complete response in one chunk. For true token-level streaming,
        the output would need to come from the SSE stream directly.

        Args:
            prompt: The message to send.

        Yields:
            Text content from the response.
        """
        output_path = self._make_output_path()
        try:
            proc = self._start_cli(prompt, output_path)
            try:
                self._wait_for_output(proc, output_path)
                with open(output_path, "r") as f:
                    content = f.read().strip()
                if content:
                    yield content
            finally:
                self._kill(proc)
        finally:
            self._cleanup(output_path)

    @staticmethod
    def _make_output_path() -> str:
        """Create a unique output file path without creating the file."""
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="rovodev_")
        os.close(fd)
        os.unlink(path)
        return path

    def _start_cli(self, prompt: str, output_path: str) -> subprocess.Popen:
        """Start the CLI subprocess."""
        cmd = ["acli", "rovodev", "run", prompt, "--output-file", output_path]
        if self._yolo:
            cmd.append("--yolo")

        env = os.environ.copy()
        env["HTTPS_PROXY"] = self._proxy

        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=self._working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
        )
        return proc

    def _wait_for_output(self, proc: subprocess.Popen, output_path: str) -> None:
        """Poll until the output file has content or timeout/process exit."""
        elapsed = 0.0
        while elapsed < self._timeout:
            # Check if process crashed
            if proc.poll() is not None:
                if not (os.path.exists(output_path) and os.path.getsize(output_path) > 0):
                    stderr = proc.stderr.read().decode() if proc.stderr else ""
                    raise RuntimeError(
                        f"CLI exited with code {proc.returncode}. stderr: {stderr}"
                    )
                return

            # Check if output file has content
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                # Give it a moment to finish writing
                time.sleep(0.5)
                return

            time.sleep(self._poll_interval)
            elapsed += self._poll_interval

        raise TimeoutError(f"No response after {self._timeout}s")

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        """Kill the CLI subprocess (it doesn't exit on its own)."""
        if proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _cleanup(path: str) -> None:
        """Remove the temporary output file."""
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    client = RovoDevOpus()
    print("Sending prompt to Opus 4.6...")
    response = client.send("Say hello in 3 languages, one sentence each.")
    print(f"\nResponse:\n{response}")
