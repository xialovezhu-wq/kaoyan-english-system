"""Maintain one sandboxed Visualize localhost preview of newly rendered content per chat."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import runpy
import select
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

IDLE_SECONDS = 3600


def _renderer():
    from .runtime_paths import codex_home
    cache = codex_home() / "plugins" / "cache"
    for vendor in ("openai-curated-remote", "openai-bundled"):
        choices = []
        for script in (cache / vendor / "visualize").glob("*/skills/visualize/scripts/render.py"):
            version = script.parents[3].name
            if re.fullmatch(r"\d+(?:\.\d+)*", version):
                choices.append((tuple(map(int, version.split("."))), script))
        if choices:
            return max(choices)[1].resolve()
    raise ValueError("the bundled Visualize preview renderer is not installed")


def _worker(slot, renderer, port):
    # The supported renderer only serves / and /index.html on a loopback address.
    # Its sandboxed iframe has no filesystem origin or local directory listing.
    def stop_if_idle(*_):
        if not slot.is_file() or time.time() - slot.stat().st_mtime > IDLE_SECONDS:
            raise KeyboardInterrupt
    signal.signal(signal.SIGALRM, stop_if_idle)
    signal.setitimer(signal.ITIMER_REAL, 60, 60)
    sys.argv = [str(renderer), str(slot), "--serve", "--port", str(port), "--title", "英语讲解"]
    sys.path.insert(0, str(renderer.parent))
    runpy.run_path(str(renderer), run_name="__main__")


def _owned_server(record, slot, renderer):
    pid = record.get("pid")
    url = record.get("url")
    if (type(pid) is not int or pid <= 0 or not isinstance(url, str)
            or not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}/", url)):
        return False
    # A stale PID or a recycled port must never transfer the tab to an unrelated service.
    result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                            capture_output=True, text=True, timeout=2)
    command = f"{Path(__file__).resolve()} --worker {slot} {renderer} {record.get('requested_port', 0)}"
    return result.returncode == 0 and result.stdout.strip().endswith(command)


def _start(slot, renderer, port=0):
    cmd = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker", str(slot), str(renderer), str(port)]
    worker = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        if not select.select([worker.stdout], [], [], 6)[0]:
            raise ValueError("the local preview did not report readiness")
        url = worker.stdout.readline(160).decode().strip()
        if not re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}/", url):
            raise ValueError("the local renderer declined the fragment or did not open a loopback port")
    except BaseException:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=2)
        raise
    finally:
        worker.stdout.close()
    return {"pid": worker.pid, "url": url, "requested_port": port}


def lesson_url(page, root, *, fragment=None):
    """Publish fresh teaching output, or resume that exact previously published lesson."""
    from .util import atomic_write_bytes, atomic_write_json, exclusive_lock, file_sha256
    if (not root.is_absolute() or not page.is_absolute() or page.is_symlink() or
            not page.resolve().is_relative_to(root.resolve())):
        raise ValueError("local lesson preview outside its chat directory")
    page = page.resolve(strict=True)
    folder = root.resolve() / ".english-lesson-panel"
    slot, state = folder / "current.html", folder / "server.json"
    if folder.is_symlink() or page.is_relative_to(folder) or slot.is_symlink() or state.is_symlink():
        raise ValueError("invalid lesson preview slot")
    renderer = _renderer()
    with exclusive_lock(folder / "preview.lock"):
        previous = json.loads(state.read_text()) if state.is_file() else {}
        if not isinstance(previous, dict):
            raise ValueError("invalid local lesson preview receipt")
        digest = file_sha256(page)
        same_page = (previous.get("page") == str(page) and previous.get("sha256") == digest
                     and slot.is_file() and file_sha256(slot) == digest)
        if fragment is None:
            if not same_page:
                raise ValueError("this lesson must be rendered for the current scope first")
        else:
            if not fragment or len(fragment) > 1_000_000 or hashlib.sha256(fragment).hexdigest() != digest:
                raise ValueError("lesson preview requires the exact freshly rendered inline content")
            if not same_page:
                atomic_write_bytes(slot, fragment)
        os.utime(slot, None)
        existing = _owned_server(previous, slot, renderer)
        if existing:
            ready = previous
        else:
            prior_url = previous.get("url", "")
            port = urlsplit(prior_url).port if re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}/", prior_url) else 0
            try:
                ready = _start(slot, renderer, port or 0)
            except ValueError:
                if not port:
                    raise
                ready = _start(slot, renderer)
        binding = {"page": str(page), "sha256": digest, "pid": ready["pid"], "url": ready["url"],
                   "requested_port": ready.get("requested_port", 0)}
        if not existing or not same_page or not previous.get("verified"):
            atomic_write_json(state, {**binding, "verified": False})
            connection = http.client.HTTPConnection("127.0.0.1", urlsplit(ready["url"]).port, timeout=4)
            try:
                connection.request("GET", "/")
                reply = connection.getresponse()
                if reply.status != 200 or reply.getheader("Content-Type", "").split(";")[0] != "text/html":
                    raise ValueError("the sandboxed lesson preview could not render the new page")
                reply.read()
            finally:
                connection.close()
            atomic_write_json(state, {**binding, "verified": True})
        return ready["url"], digest


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--worker":
        raise SystemExit("this worker is started by the local English lesson renderer")
    _worker(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]))
