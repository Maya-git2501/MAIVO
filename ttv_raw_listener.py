# ttv_raw_listener.py
import argparse
import socket
import sys

HANDSHAKE_CLIENT = "XtraLib.Stream.0\nTacview.RealTimeTelemetry.0\nClient OpenRadar\n{password}\0"
SERVER_PREFIX = "XtraLib.Stream.0\nTacview.RealTimeTelemetry.0\n"

def recv_until(sock, terminator: bytes) -> bytes:
    buf = b""
    while terminator not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Socket closed before receiving terminator")
        buf += chunk
    return buf

def connect_and_handshake(host: str, port: int, password: str) -> socket.socket:
    s = socket.create_connection((host, port), timeout=10)
    # send client handshake (null-terminated)
    payload = HANDSHAKE_CLIENT.format(password=password).encode("utf-8")
    s.sendall(payload)

    # read server handshake (null-terminated)
    server_hs = recv_until(s, b"\0").decode("utf-8", errors="replace")
    if not server_hs.startswith(SERVER_PREFIX):
        s.close()
        raise RuntimeError(f"Unexpected server handshake:\n{server_hs}")
    return s

def iter_lines(sock: socket.socket):
    """Yield newline-delimited UTF-8 lines from the socket."""
    buf = b""
    while True:
        data = sock.recv(8192)
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, _, buf = buf.partition(b"\n")
            yield line.decode("utf-8", errors="replace").rstrip("\r")

def main():
    ap = argparse.ArgumentParser(description="Tacview RT raw listener")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--password", default="")
    args = ap.parse_args()

    try:
        sock = connect_and_handshake(args.host, args.port, args.password)
        print("[connected] printing raw lines; Ctrl+C to stop")
        for line in iter_lines(sock):
            if line == "":  # skip keepalives if any
                continue
            print(line)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            sock.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()