"""
agent.py — run this project's pipeline on your own machine, driven by the website.

    SETTINGS_WEB_APP_URL='https://script.google.com/macros/s/…/exec' \
    PROJECT_PASSWORD='…' \
    python agent.py

Leave it running. It watches the project for work the dashboard has queued and
carries it out here, then reports back. No Google credentials, no GitHub, no
inbound connection.

── WHY IT POLLS ─────────────────────────────────────────────────────────────
The obvious design is the website calling the machine, and it cannot work: a
laptop sits behind a router, sleeps, and has a different address every week.
Nothing on the internet can reach it. So the direction is reversed — the
machine asks, repeatedly, whether there is anything to do. Both ends only ever
make outbound requests, which is why this works from any network without a
tunnel, a port forward or a fixed address.

The queue is a Runs tab in the project's own spreadsheet. The dashboard appends
a row; this claims it. Claiming happens under a lock on the far side and
re-checks the row inside that lock, so two machines polling the same project
cannot both take the same job.

── WHAT IT COSTS ────────────────────────────────────────────────────────────
One request every POLL_SEC, which doubles as the heartbeat that tells the
dashboard a machine is listening. Nothing is written to the sheet by a poll
that finds no work.

── STOPPING ─────────────────────────────────────────────────────────────────
Ctrl-C. A run in progress is interrupted and marked failed rather than left
looking alive — a row stuck on "running" would block everything queued behind
it. Should the machine die without the chance to say so, the far side marks the
run lost once the heartbeat has been silent long enough.
"""

import argparse
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import remote_store
from logger_setup import setup_logging_from_config

logger = logging.getLogger(__name__)

VERSION = "1"

# Seconds between polls. Short enough that pressing Run feels immediate, long
# enough that an idle agent is nearly free: at 15s this is 5,760 requests a day.
POLL_SEC = 15

# After a failure, back off rather than hammering a service that is down —
# doubling up to this ceiling, then steady.
BACKOFF_MAX_SEC = 300

# How often a running job tells the far side it is still going. Also how it
# learns it has been cancelled, so it bounds how long Cancel takes to bite.
PROGRESS_SEC = 20

# A cancelled run gets SIGINT first, which Python raises as KeyboardInterrupt so
# the pipeline can close what it is holding. Only a process that ignores that is
# killed outright.
GRACE_SEC = 30

# The tail kept for the dashboard. The full output is always on this machine, in
# logs/agent/, so this only has to be enough to see what happened.
SUMMARY_CHARS = 3000
TAIL_LINES = 40

# Every mode the dashboard offers, and what it actually runs here. The same list
# the CI workflow dispatches, so a run means the same thing wherever it happens.
MODES = {
    "full": [],
    "scrape-only": ["main.py"],
    "validate-only": ["job_validator.py"],
    "enrich-only": ["company_enricher.py"],
    "mismatch-only": ["company_validator.py"],
    "classify-only": ["company_classifier.py"],
    "pagination-only": ["pagination_analyzer.py"],
    "career-pages-only": ["automation_pipeline.py", "--skip-enrichment",
                          "--skip-keyword-scraping", "--skip-validation"],
    "cleanup-rows": ["cleanup_validation.py",
                     "--config", "cleanup_validation_config.json"],
    "publish-only": ["publish_projects.py", "--out", "site/data"],
}
MODES["full"] = ["automation_pipeline.py"]


class Stopping(Exception):
    """Ctrl-C, on its way out."""


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    kept = [line for line in text.splitlines() if line.strip()][-lines:]
    joined = "\n".join(kept)
    return joined[-SUMMARY_CHARS:]


class Agent:
    """Polls one project for work and carries it out."""

    def __init__(self, exec_url: str, password: str, name: str,
                 config_path: str, poll_sec: int = POLL_SEC,
                 python: str = None, workdir: str = None):
        self.store = remote_store.RemoteSheetsStore(exec_url, password)
        self.name = name
        self.config_path = config_path
        self.poll_sec = poll_sec
        # The interpreter running this one, so a virtualenv is inherited
        # without the caller having to think about it.
        self.python = python or sys.executable
        self.workdir = workdir or os.path.dirname(os.path.abspath(__file__))
        self.stopping = False
        self.current = None                  # the run id in progress, if any

    # ── Talking to the project ────────────────────────────────────────────────

    def _post(self, action: str, **fields):
        body = {"action": action, "password": self.store.password}
        body.update(fields)
        return self.store._request("POST", data=json.dumps(body))

    def claim(self) -> dict:
        """Ask for work. Doubles as the heartbeat, so it is never skipped."""
        reply = self._post("claimRun", agent=self.name, version=VERSION)
        if reply.get("reaped"):
            logger.info(f"{reply['reaped']} abandoned run(s) were cleared")
        return reply.get("run")

    def report(self, run_id: str, **fields) -> bool:
        """Send progress. Returns True when a cancellation is waiting."""
        try:
            reply = self._post("updateRun", id=run_id, **fields)
            return bool(reply.get("cancelRequested"))
        except remote_store.RemoteStoreError as exc:
            # Never let a failed report end a run that is otherwise fine.
            logger.warning(f"could not report on {run_id}: {exc}")
            return False

    # ── Doing the work ────────────────────────────────────────────────────────

    def _config_for_run(self, tmpdir: str) -> str:
        """config.yaml with this project's Settings tab applied over it.

        CI does this by rewriting config.yaml on a throwaway checkout. Here the
        checkout is somebody's actual working copy, so the overlay goes to a
        copy instead — a run must not leave the repository modified.
        """
        target = os.path.join(tmpdir, "config.yaml")
        shutil.copyfile(self.config_path, target)
        result = subprocess.run(
            [self.python, "settings_sheet.py", "--config", self.config_path,
             "--apply-to", target],
            cwd=self.workdir, env=self._env(), capture_output=True, text=True,
            timeout=300)
        if result.returncode != 0:
            # Not fatal: the run can proceed on config.yaml as it stands, which
            # is what happened before settings lived in the sheet at all.
            logger.warning("could not apply the Settings tab; using config.yaml "
                           f"as it is ({_tail(result.stderr, 3)})")
            shutil.copyfile(self.config_path, target)
        return target

    def _env(self) -> dict:
        env = dict(os.environ)
        env["SETTINGS_WEB_APP_URL"] = self.store.exec_url
        env["PROJECT_PASSWORD"] = self.store.password
        # Unbuffered, or a long run's output arrives only when it ends.
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def execute(self, run: dict) -> None:
        run_id, mode = run["id"], run["mode"]
        argv = MODES.get(mode)
        if argv is None:
            self.report(run_id, status="failed",
                        summary=f"this agent does not know the mode '{mode}'. "
                                "It is probably older than the dashboard.")
            return

        self.current = run_id
        logger.info(f"── run {run_id}: {mode} ──")
        os.makedirs(os.path.join(self.workdir, "logs", "agent"), exist_ok=True)
        log_path = os.path.join(self.workdir, "logs", "agent", f"{run_id}.log")

        with tempfile.TemporaryDirectory(prefix="agent-run-") as tmpdir:
            try:
                config = self._config_for_run(tmpdir)
            except Exception as exc:
                self.report(run_id, status="failed",
                            summary=f"could not prepare the config: {exc}")
                self.current = None
                return

            command = [self.python, "-u"] + argv
            if "--config" not in argv:
                command += ["--config", config]

            started = time.time()
            collected = []
            cancelled = False
            with open(log_path, "w", encoding="utf-8") as log:
                log.write(f"# {mode}  run {run_id}\n# {' '.join(command)}\n\n")
                log.flush()
                process = subprocess.Popen(
                    command, cwd=self.workdir, env=self._env(),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    bufsize=1)
                last_ping = time.time()
                try:
                    for line in process.stdout:
                        log.write(line)
                        collected.append(line)
                        # Bounded: a full run prints far more than is worth
                        # holding, and the file has all of it anyway.
                        if len(collected) > 400:
                            del collected[:200]
                        if time.time() - last_ping >= PROGRESS_SEC:
                            last_ping = time.time()
                            elapsed = int(time.time() - started)
                            if self.report(run_id, status="running",
                                           summary=f"running for {elapsed}s\n"
                                                   + _tail("".join(collected), 6)):
                                cancelled = True
                                logger.info("cancellation asked for; interrupting")
                                self._interrupt(process)
                                break
                        if self.stopping:
                            self._interrupt(process)
                            break
                finally:
                    code = process.wait()

            elapsed = int(time.time() - started)
            tail = _tail("".join(collected))
            if cancelled:
                self.report(run_id, status="cancelled", exitCode=code,
                            summary=f"cancelled after {elapsed}s\n\n{tail}")
                logger.info(f"run {run_id} cancelled")
            elif self.stopping:
                self.report(run_id, status="failed", exitCode=code,
                            summary=f"the agent was stopped after {elapsed}s\n\n{tail}")
            elif code == 0:
                self.report(run_id, status="done", exitCode=0,
                            summary=f"finished in {elapsed}s\n\n{tail}")
                logger.info(f"run {run_id} finished in {elapsed}s")
            else:
                self.report(run_id, status="failed", exitCode=code,
                            summary=f"exit {code} after {elapsed}s\n\n{tail}")
                logger.error(f"run {run_id} failed with exit {code}")
        self.current = None

    def _interrupt(self, process) -> None:
        """Ask the run to stop, and insist only if it will not.

        SIGINT rather than SIGKILL: Python raises it as KeyboardInterrupt, so
        the pipeline unwinds instead of dying mid-write to a spreadsheet.
        """
        try:
            process.send_signal(signal.SIGINT)
        except Exception:
            return
        deadline = time.time() + GRACE_SEC
        while time.time() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.5)
        logger.warning("the run ignored the interrupt; killing it")
        try:
            process.kill()
        except Exception:
            pass

    # ── The loop ──────────────────────────────────────────────────────────────

    def poll_once(self) -> bool:
        """One claim. True when something was run."""
        run = self.claim()
        if not run:
            return False
        self.execute(run)
        return True

    def serve(self, once: bool = False) -> None:
        logger.info(f"agent '{self.name}' watching for work every {self.poll_sec}s "
                    "(Ctrl-C to stop)")
        backoff = 0
        idle_logged = False
        while not self.stopping:
            try:
                did = self.poll_once()
                backoff = 0
                if did:
                    idle_logged = False
                elif not idle_logged:
                    logger.info("connected, nothing queued")
                    idle_logged = True
            except remote_store.RemoteStoreError as exc:
                backoff = min(BACKOFF_MAX_SEC, max(self.poll_sec, backoff * 2))
                logger.warning(f"{exc}; retrying in {backoff}s")
            except Stopping:
                break
            if once:
                return
            self._sleep(backoff or self.poll_sec)

    def _sleep(self, seconds: float) -> None:
        """Sleep in slices, so Ctrl-C is felt immediately rather than at the end."""
        deadline = time.time() + seconds
        while time.time() < deadline and not self.stopping:
            time.sleep(min(0.5, max(0.0, deadline - time.time())))

    def stop(self) -> None:
        self.stopping = True


def default_name() -> str:
    return f"{platform.node() or 'machine'}"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Run this project's pipeline here, driven by the dashboard")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--name", default=default_name(),
                    help="how this machine appears in the dashboard")
    ap.add_argument("--interval", type=int, default=POLL_SEC,
                    help=f"seconds between polls (default {POLL_SEC})")
    ap.add_argument("--once", action="store_true",
                    help="claim at most one run, then exit")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    exec_url = os.environ.get("SETTINGS_WEB_APP_URL", "").strip()
    password = os.environ.get("PROJECT_PASSWORD", "").strip()
    if not exec_url or not password:
        print("agent.py needs two things in the environment:\n"
              "  SETTINGS_WEB_APP_URL   the project's /exec URL\n"
              "  PROJECT_PASSWORD       the password you sign in with\n\n"
              "Both are on the dashboard, under Run here.", file=sys.stderr)
        sys.exit(2)

    try:
        from config_loader import load_config
        setup_logging_from_config(load_config(args.config), name="agent")
    except Exception:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")

    agent = Agent(exec_url, password, args.name, args.config,
                  poll_sec=max(5, args.interval))

    def bail(signum, frame):
        if agent.stopping:                   # a second Ctrl-C means now
            raise KeyboardInterrupt
        logger.info("stopping after the current step…")
        agent.stop()

    signal.signal(signal.SIGINT, bail)
    signal.signal(signal.SIGTERM, bail)

    try:
        agent.serve(once=args.once)
    except KeyboardInterrupt:
        pass
    logger.info("agent stopped")


if __name__ == "__main__":
    main()
