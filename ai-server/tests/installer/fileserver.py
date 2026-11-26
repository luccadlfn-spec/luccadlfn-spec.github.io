#!/usr/bin/env python3
"""Small HTTP file server for the download tests.

    fileserver.py ROOT LOGFILE

Serves the files under ROOT on a free port of 127.0.0.1 and prints the port.
Unlike `python -m http.server` it understands Range requests, which is what
`curl -C -` sends to continue a download. Every request is appended to
LOGFILE as "GET <path> range=<header or ->".

A POST is answered with the file at its path, whatever the body was (a
stand-in for an API that answers with fixed JSON); its log line also holds
"x-api-key=<header or ->".

A marker file makes one path misbehave once:
    <file>.flaky   the next GET sends half of the bytes and then drops the
                   connection (the marker is removed, so a retry works)
    <file>.slow    every GET takes about two and a half seconds
"""
import http.server
import os
import sys
import time

ROOT = os.path.abspath(sys.argv[1])
LOG = sys.argv[2]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.do_GET()

    def do_GET(self, head=False):
        path = self.path.split("?", 1)[0]
        rng = self.headers.get("Range")
        extra = ""
        if self.command == "POST":
            extra = " x-api-key=%s" % (self.headers.get("x-api-key") or "-")
        with open(LOG, "a") as log:
            log.write("%s %s range=%s%s\n" % (self.command, path, rng or "-", extra))
        full = os.path.normpath(os.path.join(ROOT, path.lstrip("/")))
        if not full.startswith(ROOT + os.sep) or not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = os.path.getsize(full)
        start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-", 1)[0] or 0)
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(size - start))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if head:
            return
        with open(full, "rb") as handle:
            handle.seek(start)
            body = handle.read()
        flaky = full + ".flaky"
        if os.path.exists(flaky):
            os.remove(flaky)
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.close_connection = True
            self.connection.close()
            return
        if os.path.exists(full + ".slow"):
            step = max(1, len(body) // 5)
            for offset in range(0, len(body), step):
                self.wfile.write(body[offset:offset + step])
                self.wfile.flush()
                time.sleep(0.5)
            return
        self.wfile.write(body)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
