"""A made-up SMTP server on 127.0.0.1 for the endings of an approved mail (docs/SENDGATE.md, package 6a). It speaks enough of
RFC 5321 for smtplib (EHLO, AUTH PLAIN, MAIL, RCPT, DATA, NOOP, RSET, QUIT), keeps the bytes of every message it got after DATA,
counts how often a message reached DATA, and can refuse or drop the connection at each step:

    server.fault = ("mail", 553)        answer MAIL FROM with 553
    server.fault = ("rcpt", "drop")     close the connection instead of answering RCPT
    server.fault = ("data", 451)        answer the DATA command itself with 451 (the message is never handed over)
    server.fault = ("after", "hang")    take the message, then never answer (the client's read times out)
    server.fault = ("auth", 535)        refuse the sign-in
    server.refuse = {"tom@example.net"} answer RCPT for these addresses with 550

No TLS: the tests set SMTP_SECURITY=none. Everything sent to it is made up.
"""
from __future__ import annotations

import base64
import socket
import threading
import time


class Server:
    def __init__(self, password: str = "", user: str = "", starttls: bool = False):
        self.user, self.password, self.starttls = user, password, starttls
        self.fault: tuple[str, object] | None = None
        self.refuse: set[str] = set()
        self.messages: list[bytes] = []          # the bytes after DATA, as they came over the wire (dot-stuffing undone)
        self.envelopes: list[tuple[str, list[str]]] = []
        self.data_reached = 0                    # how often a client sent the DATA command
        self.connections = 0
        self.hang_seconds = 5.0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass

    # -- the protocol ---------------------------------------------------------------------------------------------------------
    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _fault(self, step: str):
        if self.fault and self.fault[0] == step:
            return self.fault[1]
        return None

    def _serve(self, conn: socket.socket) -> None:
        f = conn.makefile("rb")
        out = conn.makefile("wb")

        def say(line: str) -> None:
            out.write(line.encode() + b"\r\n")
            out.flush()

        def cut(step: str) -> None:
            """The fault for this step drops the connection, or hangs until the client gives up."""
            if self._fault(step) == "hang":
                time.sleep(self.hang_seconds)
            conn.close()

        try:
            say("220 smtp.example.org ESMTP made up")
            sender, rcpts = "", []
            while True:
                line = f.readline()
                if not line:
                    return
                cmd = line.decode(errors="replace").strip()
                verb = cmd.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    caps = ["smtp.example.org", "AUTH PLAIN", "SIZE 20000000", "8BITMIME"] + (["STARTTLS"] if self.starttls else [])
                    for c in caps[:-1]:
                        say(f"250-{c}")
                    say(f"250 {caps[-1]}")
                elif verb == "AUTH":
                    parts = cmd.split()
                    blob = base64.b64decode(parts[2]).split(b"\0") if len(parts) > 2 else []
                    if self._fault("auth") is not None or (self.password and (len(blob) < 3 or blob[2].decode() != self.password)):
                        say("535 Authentication failed")
                    else:
                        say("235 Authentication succeeded")
                elif verb == "MAIL":
                    sender, rcpts = cmd.split(":", 1)[1].strip().split(" ")[0].strip("<>"), []
                    what = self._fault("mail")
                    if what == "drop" or what == "hang":
                        cut("mail")
                        return
                    say(f"{what} Sender refused" if isinstance(what, int) else "250 OK")
                elif verb == "RCPT":
                    addr = cmd.split(":", 1)[1].strip().strip("<>")
                    what = self._fault("rcpt")
                    if what in ("drop", "hang"):
                        cut("rcpt")
                        return
                    if addr in self.refuse:
                        say("550 No such user here")
                    else:
                        rcpts.append(addr)
                        say("250 OK")
                elif verb == "DATA":
                    self.data_reached += 1
                    what = self._fault("data")
                    if what in ("drop", "hang"):
                        cut("data")
                        return
                    if isinstance(what, int):
                        say(f"{what} Not now")
                        continue
                    say("354 End data with <CR><LF>.<CR><LF>")
                    body = []
                    while True:
                        chunk = f.readline()
                        if not chunk or chunk == b".\r\n":
                            break
                        body.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                    self.messages.append(b"".join(body))
                    self.envelopes.append((sender, list(rcpts)))
                    what = self._fault("after")
                    if what in ("drop", "hang"):
                        cut("after")
                        return
                    say(f"{what} Message refused" if isinstance(what, int) else "250 OK: queued")
                elif verb == "NOOP":
                    say("250 OK")
                elif verb == "RSET":
                    sender, rcpts = "", []
                    say("250 OK")
                elif verb == "QUIT":
                    say("221 Bye")
                    return
                else:
                    say("502 Command not implemented")
        except OSError:
            return
        finally:
            for x in (f, out, conn):
                try:
                    x.close()
                except OSError:
                    pass
