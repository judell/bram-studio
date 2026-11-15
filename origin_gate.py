"""Who may talk to Studio's server (server-origin-host-gate).

Pure functions, so tests can import them without starting the server
(importing serve_media.py runs it). serve_media.py's Handler.refuse_foreign
calls both before any handler.

Pages may talk to the server only if they're local: Studio's page comes from
Bram, served on whatever port Bram chose, so any loopback origin passes, plus
Bram's embedded preview pane. A request with no Origin (curl, the agent,
start.sh, a <video> or <img> load, whose response a page can't read) passes
too. The Host must name loopback and the port the server bound, which stops a
DNS-rebinding page reaching it under its own hostname.
"""
import urllib.parse

LOOPBACK_NAMES = ("127.0.0.1", "localhost", "::1")
PANE_ORIGIN = "bramapp://localhost"


def origin_allowed(origin):
    # An Origin header is scheme://host[:port] and nothing more.
    if origin == PANE_ORIGIN:
        return True
    try:
        u = urllib.parse.urlsplit(origin)
        u.port  # raises on a malformed port
    except ValueError:
        return False
    return (u.scheme == "http" and u.hostname in LOOPBACK_NAMES and not u.username
            and not u.password and not u.path and not u.query and not u.fragment)


def host_allowed(host, port):
    # No Host header (HTTP/1.0 clients) passes: a browser always sends one.
    if not host:
        return True
    try:
        u = urllib.parse.urlsplit("//" + host)
        return u.hostname in LOOPBACK_NAMES and u.port == port and not u.username and not u.path
    except ValueError:
        return False
