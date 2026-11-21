"""One read-only loopback origin. It serves exclusively the verified display projection."""
from __future__ import annotations

from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import http.client
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit, parse_qs
from uuid import uuid4

from .article_source import KEY
from .article_workspace import PROTOCOL, SERVICE_VERSION, path_for, project
from .errors import PipelineError, ValidationError
from .util import atomic_write_json, bytes_sha256, exclusive_lock, load_json

UI = Path(__file__).with_name("article_web")
HEALTH = "/english/health"
ROUTE = "/english/a/{entity_key}"
REVISION = ROUTE + "/revision"
MIME = {".css":"text/css", ".js":"text/javascript", ".html":"text/html"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
FIGURE_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
              "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")


@lru_cache(maxsize=2)
def asset_version(stamps):
    return bytes_sha256(b"".join((UI / name).read_bytes() for name, _, _ in stamps))


def writing_version():
    names=("writing.html","writing.js","writing.css")
    return asset_version(tuple((n,(UI/n).stat().st_mtime_ns,(UI/n).stat().st_size) for n in names))

def page_version():
    stamps = []
    for name in ("index.html","reader.js","reader.css"):
        stat = (UI / name).stat()
        stamps.append((name, stat.st_mtime_ns, stat.st_size))
    return asset_version(tuple(stamps))


def context(repo, root, key):
    if key.startswith("SYN-"):
        return root / "synthetic" / "repo", root / "synthetic"
    return repo, root


class Reader(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log page URLs or learner material.

    def respond(self, status, data, mime="application/json", policy=CSP):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, separators=(",",":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self.read_route()
        except (BrokenPipeError, ConnectionResetError):
            return
        except (ValueError, KeyError, PipelineError, OSError, TypeError) as exc:
            code = str(exc) if str(exc) in ("scope_expired","source_changed","article_unavailable") else "projection_unavailable"
            self.respond(409 if code in ("scope_expired","source_changed") else 404, {"error":code})

    def read_route(self):
        host = f"127.0.0.1:{self.server.server_port}"
        if (self.headers.get("Host") != host or self.headers.get("Origin") not in (None, "http://"+host)
                or self.headers.get("Sec-Fetch-Site") in ("same-site","cross-site")):
            return self.respond(403, {"error":"origin_rejected"})
        url = urlsplit(self.path)
        if url.netloc or url.scheme:
            return self.respond(400, {"error":"invalid_route"})
        if url.path == HEALTH and not url.query:
            return self.respond(200, self.server.health)
        if url.path in ("/english/ui/reader.js", "/english/ui/reader.css") and not url.query:
            asset = UI / url.path.rsplit("/", 1)[1]
            return self.respond(200, asset.read_bytes(), MIME[asset.suffix])
        if url.path in ("/english/ui/writing.css", "/english/ui/writing.js") and not url.query:
            asset = UI / url.path.rsplit("/", 1)[1]
            return self.respond(200, asset.read_bytes(), MIME[asset.suffix])
        parts = url.path.split("/")
        if len(parts) < 4 or parts[:3] != ["","english","a"] or not KEY.fullmatch(parts[3]):
            return self.respond(404, {"error":"unknown_route"})
        key = parts[3]
        repo, root = context(self.server.repo, self.server.root, key)
        end = parts[4:]
        params = parse_qs(url.query, strict_parsing=True, max_num_fields=3)
        if any(len(v) != 1 for v in params.values()):
            raise ValueError("ambiguous request")
        from . import writing_workspace as writing
        mirror=key.removesuffix("-DRAFT")
        if mirror!=key and writing.KEY.fullmatch(mirror):
            s=writing.current(root,mirror)
            if s["status"]!="active" or not s.get("micro"):
                raise ValidationError("scope_expired")
            if not end and not params:
                return self.respond(200,(UI/"writing.html").read_bytes().replace(b"__WRITING_RELEASE__",writing_version().encode()),"text/html",CSP)
            if end in (["revision"],["projection"]) and not params:
                m=s["micro"]; drafts=m["drafts"]
                draft=next((d for d in drafts if d["id"]==m.get("selected_draft")),drafts[0] if drafts else None)
                meta={**writing.metadata(s),"entity_key":key,"presentation_profile":"english_draft_reference","focus_anchors":[]}
                if end==["revision"]:
                    return self.respond(200,meta)
                item={k:draft[k] for k in ("id","text","sha256")} if draft else None
                number=next((i+1 for i,d in enumerate(drafts) if draft and d["id"]==draft["id"]),None)
                return self.respond(200,{**meta,"stage":"reference","stage_label":"稿件对照","course":"考研英语一",
                    "section":"首稿" if number==1 else ("第 "+str(number)+" 版" if number else "原稿"),
                    "goal":"","cards":[],"reference":{"draft":item,"anchor":m.get("focus_anchor")}})
            return self.respond(404,{"error":"unknown_route"})
        if writing.KEY.fullmatch(key):
            if not end and not params:
                return self.respond(200, (UI / "writing.html").read_bytes().replace(b"__WRITING_RELEASE__",writing_version().encode()), MIME[".html"], CSP+"; img-src 'self'")
            if end in (["revision"], ["projection"]) and not params:
                return self.respond(200, {**writing.projection(repo,root,key,meta_only=end==["revision"]), "reader_template":writing_version()})
            if len(end)==2 and end[0]=="page" and set(params)=={"epoch"}:
                return self.respond(200,writing.page_bytes(root,key,params["epoch"][0],end[1]),"image/webp")
            if len(end)==2 and end[0]=="figure" and set(params)=={"epoch"}:
                safe=writing.projection(repo,root,key)
                if safe["access_epoch"]!=params["epoch"][0] or not any(c.get("figure")==end[1] for c in safe["cards"]):
                    raise ValidationError("scope_expired")
                from .article_figures import read_figure
                return self.respond(200,read_figure(root,end[1]),MIME[".html"],FIGURE_CSP)
            if end == ["actions"] and not params:
                return self.respond(200, writing.actions(root,key))
            return self.respond(404, {"error":"unknown_route"})
        if not end and not params:
            # A cold/back navigation only receives a static shell, even offline.
            return self.respond(200, (UI / "index.html").read_bytes().replace(b"__PAGE_VERSION__", page_version().encode()), MIME[".html"])
        if end == ["changes"] and not params:
            return self.change_stream(repo, root, key)
        if end == ["revision"] and not params:
            return self.respond(200, {**project(repo, root, key, meta_only=True), "page_version":page_version()})
        if end == ["projection"] and set(params) <= {"epoch","since"}:
            since = int(params["since"][0]) if "since" in params else None
            return self.respond(200, {**project(repo, root, key, since=since, epoch=params.get("epoch",[None])[0]), "page_version":page_version()})
        if len(end) == 3 and end[0] == "figure" and re.fullmatch("[a-f0-9]{64}", end[2]) and set(params) == {"epoch"}:
            safe = project(repo, root, key)
            if safe["access_epoch"] != params["epoch"][0] or not any(
                    b.get("figure") == end[2] for s in safe["sections"] if s["id"] == end[1] for b in s["blocks"]):
                raise ValidationError("scope_expired")
            from .article_figures import read_figure
            data = read_figure(root, end[2])
            return self.respond(200, data, MIME[".html"], FIGURE_CSP)
        self.respond(404, {"error":"unknown_route"})

    def change_stream(self, repo, root, key):
        # The CLI and HTTP server are separate processes. Watch this article's
        # SQLite inode, then read its *committed* scope. No HTTP write channel.
        path = path_for(root,key)
        with path.open("rb") as source:
            watcher = select.kqueue() if hasattr(select, "kqueue") else None
            try:
                if watcher:
                    event = select.kevent(source.fileno(), filter=select.KQ_FILTER_VNODE,
                                          flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                          fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE)
                    watcher.control([event], 0, 0)
                initial = project(repo, root, key, meta_only=True)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Connection", "close")
                self.end_headers()
                self.connection.settimeout(5)
                self.wfile.write(b"retry: 700\n\n")
                last = None
                until = time.monotonic() + 45
                while time.monotonic() < until:
                    current = initial
                    if last is not None:
                        try:
                            current = project(repo,root,key,meta_only=True)
                        except (PipelineError, ValueError, KeyError, OSError):
                            self.wfile.write(b"event: invalidated\ndata: {}\n\n")
                            self.wfile.flush()
                            return
                    current["page_version"] = page_version()
                    token = (current["content_hash"], current["page_version"])
                    if token != last:
                        payload = json.dumps({k:current[k] for k in ("revision","access_epoch","content_hash","page_version")})
                        self.wfile.write(("event: revision\ndata: "+payload+"\n\n").encode())
                        last = token
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    if watcher:
                        watcher.control(None,1,1.5)
                    else:
                        time.sleep(.25)
            finally:
                if watcher:
                    watcher.close()

    def do_POST(self):
        from . import writing_workspace as writing
        parts = self.path.split("/")
        if len(parts) != 5 or parts[:3] != ["","english","a"] or parts[4] not in {"actions","micro-actions"} or not writing.KEY.fullmatch(parts[3]):
            return self.respond(405, {"error":"read_only"})
        host = f"127.0.0.1:{self.server.server_port}"
        if (self.headers.get("Host") != host or self.headers.get("Origin") != "http://"+host
                or self.headers.get("Sec-Fetch-Site") not in (None,"same-origin")
                or self.headers.get("Content-Type") != "application/json"):
            return self.respond(403, {"error":"origin_rejected"})
        try:
            length = int(self.headers.get("Content-Length","0"))
            if not 0 < length <= (96000 if parts[4]=="micro-actions" else 2048):
                return self.respond(413, {"error":"invalid_action_size"})
            repo, root = context(self.server.repo, self.server.root, parts[3])
            if parts[4]=="micro-actions":
                from .writing_practice import operate
                return self.respond(200, operate(repo,root,parts[3],json.loads(self.rfile.read(length))))
            return self.respond(200, writing.actions(root,parts[3],posted=json.loads(self.rfile.read(length))))
        except (KeyError, ValueError, OSError, TypeError, PipelineError):
            return self.respond(409, {"error":"stale_or_invalid_action"})


def health_fields(instance):
    return dict(protocol=PROTOCOL, subject="english", service_version=SERVICE_VERSION, instance_id=instance)


def serve(repo, root):
    config = root / "service.json"
    old = load_json(config) if config.exists() else {}
    port = old.get("port", 0)
    with ThreadingHTTPServer(("127.0.0.1", port), Reader) as server:
        server.repo, server.root = repo, root
        server.health = health_fields(uuid4().hex)
        rec = {**server.health, "port":server.server_port, "pid":os.getpid()}
        atomic_write_json(config, rec)
        print(json.dumps(rec), flush=True)
        server.serve_forever()


def read_http(rec, path):
    connection = http.client.HTTPConnection("127.0.0.1", rec["port"], timeout=3)
    try:
        connection.request("GET",path)
        response = connection.getresponse()
        if response.status != 200:
            raise ValidationError("service_unavailable")
        return json.loads(response.read(1_000_000))
    finally:
        connection.close()


def origin(rec):
    return f"http://127.0.0.1:{rec['port']}"


def stop_worker(rec, repo, root):
    """Only stop the exact registered English worker, including an older release."""
    try:
        health = read_http(rec, HEALTH)
    except ConnectionRefusedError:
        return {"status":"already_stopped"}
    expected = {k:rec.get(k) for k in ("protocol","subject","instance_id","service_version")}
    script = Path(__file__).resolve().parents[1] / "scripts/english_article.py"
    command = subprocess.run(["ps","-ww","-p",str(rec["pid"]),"-o","command="],
                             capture_output=True,text=True,check=True).stdout.strip()
    if (expected.get("protocol") != PROTOCOL or expected.get("subject") != "english" or health != expected
            or not command.endswith(f"{script} --runtime {root} --repo {repo} serve")):
        raise ValidationError("service_identity_mismatch")
    os.kill(rec["pid"],signal.SIGTERM)
    for _ in range(30):
        try:
            read_http(rec,HEALTH)
        except (ConnectionResetError,ConnectionRefusedError):
            return {"status":"stopped","port_reserved_for_restart":rec["port"]}
        time.sleep(.05)
    raise ValidationError("service_stop_unconfirmed")


def ensure(repo, root):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with exclusive_lock(root / "service.lock"):
        config = root / "service.json"
        if config.exists():
            rec = load_json(config)
            try:
                health = read_http(rec, HEALTH)
            except (ConnectionRefusedError, ConnectionResetError):
                pass
            else:
                if health == health_fields(rec["instance_id"]):
                    return rec
                stop_worker(rec, repo, root)
        script = Path(__file__).resolve().parents[1] / "scripts/english_article.py"
        argv = [sys.executable, "-B", str(script), "--runtime", str(root), "--repo", str(repo), "serve"]
        worker = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            if not select.select([worker.stdout], [], [], 8)[0]:
                raise ValidationError("service_unavailable")
            line = worker.stdout.readline(2048)
            if not line:
                raise ValidationError("stable_port_unavailable")
            rec = json.loads(line)
            if rec["pid"] != worker.pid or read_http(rec, HEALTH) != health_fields(rec["instance_id"]):
                raise ValidationError("service_identity_mismatch")
        except BaseException:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=3)
            raise
        finally:
            worker.stdout.close()
        return rec


def descriptor(rec):
    return {**health_fields(rec["instance_id"]), "origin":origin(rec), "entity_route_template":ROUTE,
            "health_path":HEALTH, "revision_path_template":REVISION}
