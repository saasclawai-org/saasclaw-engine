"""
Pi bridge — runs Pi in RPC mode as a persistent subprocess.

Pi's --mode rpc uses JSON lines over stdin/stdout. We keep Pi alive
across messages so it can do full agentic loops (read, write, edit, bash)
per prompt. Session continuity is built-in.

Protocol:
  stdin  → {"type": "prompt", "message": "...", "id": "1"}
  stdout ← {type: "response", command: "prompt", success: true}
  stdout ← {type: "message_update", assistantMessageEvent: {type: "text_delta", delta: "..."}}
  stdout ← {type: "agent_end", messages: [...]}
"""

import json
import logging
import os
import subprocess
import sys
import threading
from typing import Generator, Optional

logger = logging.getLogger("pi_bridge")

def _log(msg, *args):
    """Log to both logger and stderr (gunicorn captures stderr)."""
    text = msg % args if args else str(msg)
    print(f"[pi_bridge] {text}", file=sys.stderr, flush=True)


class PiTimeoutError(Exception):
    """Raised when Pi produces no output for too long."""
    pass


class PiBridge:
    """Manages a persistent Pi RPC subprocess for the wizard."""

    # If no event from Pi for this many seconds, consider it hung
    EVENT_TIMEOUT = 300  # 5 minutes

    def __init__(
        self,
        working_dir: str,
        provider: str = "zai",
        model: str = "glm-5.2",
        session_dir: str = "/tmp/pi-sessions",
        session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        thinking: str = "off",
    ):
        self.working_dir = working_dir
        self.provider = provider
        self.model = model
        self.session_dir = session_dir
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.thinking = thinking
        self._process: Optional[subprocess.Popen] = None
        self._stderr_lines: list[bytes] = []
        self._stderr_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()  # guards stdin writes
        self._started = False
        self._watchdog_timer: Optional[threading.Timer] = None
        self._watchdog_event = threading.Event()

    def _ensure_started(self):
        """Start the Pi RPC process if not already running."""
        if self._process and self._process.poll() is None:
            return

        pi_cmd = [
            "pi",
            "--mode", "rpc",
            "--session-dir", self.session_dir,
            "--provider", self.provider,
            "--model", self.model,
            "--thinking", self.thinking,
        ]

        if self.session_id:
            pi_cmd.extend(["--session-id", self.session_id])

        if self.system_prompt:
            pi_cmd.extend(["--system-prompt", self.system_prompt])

        env = os.environ.copy()
        from django.conf import settings
        if self.provider == "zai":
            zai_key = env.get("ZAI_API_KEY") or getattr(settings, "ZAI_API_KEY", "")
            if zai_key:
                env["ZAI_API_KEY"] = zai_key
            zai_url = env.get("ZAI_BASE_URL") or getattr(settings, "ZAI_LLM_BASE_URL", "")
            if zai_url:
                env["ZAI_BASE_URL"] = zai_url
        elif self.provider == "openai":
            from saasclaw_engine.studio_models.models import ProviderKey
            try:
                pk = ProviderKey.objects.filter(provider="openai", is_active=True).latest("updated_at")
                if pk.api_key:
                    env["OPENAI_API_KEY"] = pk.api_key
            except ProviderKey.DoesNotExist:
                pass
        elif self.provider == "anthropic":
            from saasclaw_engine.studio_models.models import ProviderKey
            try:
                pk = ProviderKey.objects.filter(provider="anthropic", is_active=True).latest("updated_at")
                if pk.api_key:
                    env["ANTHROPIC_API_KEY"] = pk.api_key
            except ProviderKey.DoesNotExist:
                pass

        # Wrap Pi in bwrap for filesystem isolation.
        # Pi can read/write only its workspace (mounted at its real path as CWD)
        # and /tmp for Pi sessions. Cannot access other projects.
        ws_real = os.path.realpath(self.working_dir)
        bwrap_cmd = [
            "bwrap",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--bind", ws_real, ws_real,
            "--bind", "/tmp", "/tmp",
            "--ro-bind", "/etc/ssl", "/etc/ssl",
            "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
            "--ro-bind", "/etc/hosts", "/etc/hosts",
            "--ro-bind", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
            "--dev", "/dev",
            "--proc", "/proc",
            "--ro-bind-try", "/home/saasclaw/.pi", "/home/saasclaw/.pi",
            "--ro-bind-try", "/etc/machine-id", "/etc/machine-id",
        ] + pi_cmd

        self._process = subprocess.Popen(
            bwrap_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.working_dir,
            env=env,
        )

        # Drain stderr in background so it doesn't block stdout
        self._stderr_lines = []
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

        self._started = True
        _log("Pi RPC process started (pid=%s, cwd=%s)", self._process.pid, self.working_dir)

        # Give Pi a moment to initialize its session
        import time
        time.sleep(0.5)
        if self._process.poll() is not None:
            stderr_text = b"".join(self._stderr_lines).decode(errors="replace")[:500]
            _log("Pi process exited immediately (rc=%d): %s", self._process.returncode, stderr_text)

    def _read_stderr(self):
        """Background reader for stderr to prevent pipe blocking."""
        assert self._process and self._process.stderr
        for line in self._process.stderr:
            self._stderr_lines.append(line)
            text = line.decode(errors="replace").strip()
            if text:
                _log("Pi stderr: %s", text)

    def _reset_watchdog(self):
        """Reset the inactivity watchdog timer."""
        if self._watchdog_timer:
            self._watchdog_timer.cancel()
        self._watchdog_event.clear()
        self._watchdog_timer = threading.Timer(self.EVENT_TIMEOUT, self._watchdog_fired)
        self._watchdog_timer.daemon = True
        self._watchdog_timer.start()

    def _stop_watchdog(self):
        """Cancel the watchdog."""
        if self._watchdog_timer:
            self._watchdog_timer.cancel()
            self._watchdog_timer = None

    def _watchdog_fired(self):
        """Called when no event received for EVENT_TIMEOUT seconds."""
        self._watchdog_event.set()
        _log("Watchdog fired: no Pi output for %ds — killing process", self.EVENT_TIMEOUT)
        if self._process and self._process.poll() is None:
            self._process.kill()
            self._process.wait()

    def _send_command(self, command: dict) -> None:
        """Send a JSON command to Pi's stdin."""
        assert self._process and self._process.stdin
        data = json.dumps(command) + "\n"
        with self._lock:
            self._process.stdin.write(data.encode("utf-8"))
            self._process.stdin.flush()

    def run(self, message: str) -> Generator[dict, None, None]:
        """Send a prompt to Pi and yield all events until the agent finishes.

        Pi stays alive across calls. Each prompt triggers a full agentic
        loop (tool calls, reads, writes, etc.) with events streamed in
        real-time.

        Raises PiTimeoutError if Pi produces no output for EVENT_TIMEOUT seconds.
        """
        import select

        self._ensure_started()
        if not self._process or self._process.poll() is not None:
            yield {"type": "error", "content": "Pi process failed to start."}
            return

        prompt_id = "prompt_1"
        self._send_command({
            "type": "prompt",
            "message": message,
            "id": prompt_id,
        })

        # Start watchdog
        self._reset_watchdog()

        # Stream events from stdout until we see agent_end
        assert self._process.stdout
        while True:
            # Check if watchdog killed the process
            if self._watchdog_event.is_set():
                self._stop_watchdog()
                yield {
                    "type": "error",
                    "content": f"Pi timed out after {self.EVENT_TIMEOUT}s of inactivity — process killed.",
                }
                break

            # Check if process died
            if self._process.poll() is not None:
                self._stop_watchdog()
                stderr_text = b"".join(self._stderr_lines).decode(errors="replace")[-300:]
                yield {
                    "type": "error",
                    "content": f"Pi process exited unexpectedly (code {self._process.returncode}): {stderr_text}",
                }
                break

            # Non-blocking read with 1-second timeout
            ready, _, _ = select.select([self._process.stdout], [], [], 1.0)
            if not ready:
                continue

            raw = self._process.stdout.readline().strip()
            if not raw:
                continue

            # Got data — reset watchdog
            self._reset_watchdog()

            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                yield {"type": "raw", "text": raw[:200]}
                continue

            yield event

            # Stop reading after agent_end — Pi is idle again
            if event.get("type") == "agent_end":
                self._stop_watchdog()
                break

    def steer(self, message: str) -> None:
        """Steer the running agent with additional guidance."""
        if not self._process or self._process.poll() is not None:
            return
        self._send_command({"type": "steer", "message": message})

    def abort(self) -> None:
        """Abort the current agent turn."""
        if not self._process or self._process.poll() is not None:
            return
        self._send_command({"type": "abort"})

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self):
        """Kill the Pi process."""
        self._stop_watchdog()
        if self._process and self._process.poll() is None:
            self._process.stdin.close()
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            if self._stderr_lines:
                stderr_text = b"".join(self._stderr_lines).decode(errors="replace")[:500]
                _log("Pi stderr: %s", stderr_text)
        self._process = None
        self._started = False

    def get_state(self) -> Optional[dict]:
        """Query Pi's current session state."""
        if not self._process or self._process.poll() is not None:
            return None
        # This is synchronous — we'd need to read one response from stdout.
        # For now, skip; the SSE view doesn't need it.
        return None

    def __del__(self):
        self.stop()


def run_pi_message(
    message: str,
    working_dir: str,
    provider: str = "zai",
    model: str = "glm-5.2",
    session_dir: str = "/tmp/pi-sessions",
    session_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
    thinking: str = "off",
) -> Generator[dict, None, None]:
    """Convenience function: run Pi and yield events."""
    bridge = PiBridge(
        working_dir=working_dir,
        provider=provider,
        model=model,
        session_dir=session_dir,
        session_id=session_id,
        system_prompt=system_prompt,
        thinking=thinking,
    )
    yield from bridge.run(message)
    bridge.stop()
