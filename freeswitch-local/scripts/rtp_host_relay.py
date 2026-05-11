#!/usr/bin/env python3

import argparse
import selectors
import signal
import socket
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Relay host RTP ports to an alternate Docker-published RTP range. "
            "This keeps the source port seen by a local SIP client equal to the "
            "port advertised in SDP, avoiding Docker Desktop UDP source-port NAT."
        )
    )
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--listen-start", type=int, default=16384)
    parser.add_argument("--listen-end", type=int, default=16484)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-start", type=int, default=26384)
    parser.add_argument("--log-interval", type=float, default=5.0)
    return parser.parse_args()


class RtpHostRelay:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.selector = selectors.DefaultSelector()
        self.phone_peers: dict[int, tuple[str, int]] = {}
        self.phone_to_fs = 0
        self.fs_to_phone = 0
        self.drops = 0
        self.running = True

    def target_for(self, listen_port: int) -> tuple[str, int]:
        return (
            self.args.target_host,
            self.args.target_start + listen_port - self.args.listen_start,
        )

    def open(self) -> None:
        for port in range(self.args.listen_start, self.args.listen_end + 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(False)
            sock.bind((self.args.bind, port))
            self.selector.register(sock, selectors.EVENT_READ, port)
        count = self.args.listen_end - self.args.listen_start + 1
        print(
            f"rtp_host_relay listening {self.args.bind}:"
            f"{self.args.listen_start}-{self.args.listen_end} -> "
            f"{self.args.target_host}:{self.args.target_start}-"
            f"{self.args.target_start + count - 1}",
            flush=True,
        )

    def stop(self, *_args: object) -> None:
        self.running = False

    def is_target_packet(self, listen_port: int, addr: tuple[str, int]) -> bool:
        target_host, target_port = self.target_for(listen_port)
        return addr[1] == target_port and (
            addr[0] == target_host or addr[0].startswith("127.")
        )

    def handle_packet(
        self, sock: socket.socket, listen_port: int, data: bytes, addr: tuple[str, int]
    ) -> None:
        target = self.target_for(listen_port)
        if self.is_target_packet(listen_port, addr):
            phone = self.phone_peers.get(listen_port)
            if not phone:
                self.drops += 1
                return
            sock.sendto(data, phone)
            self.fs_to_phone += 1
            return

        if self.phone_peers.get(listen_port) != addr:
            self.phone_peers[listen_port] = addr
            print(f"port {listen_port}: phone peer {addr[0]}:{addr[1]}", flush=True)
        sock.sendto(data, target)
        self.phone_to_fs += 1

    def log_stats(self) -> None:
        active = ",".join(str(port) for port in sorted(self.phone_peers)) or "-"
        print(
            "stats "
            f"phone_to_fs={self.phone_to_fs} fs_to_phone={self.fs_to_phone} "
            f"drops={self.drops} active_ports={active}",
            flush=True,
        )

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        next_log = time.monotonic() + self.args.log_interval
        while self.running:
            for key, _events in self.selector.select(timeout=0.5):
                sock = key.fileobj
                listen_port = key.data
                data, addr = sock.recvfrom(4096)
                self.handle_packet(sock, listen_port, data, addr)

            now = time.monotonic()
            if now >= next_log:
                self.log_stats()
                next_log = now + self.args.log_interval

        self.selector.close()


def main() -> int:
    args = parse_args()
    relay = RtpHostRelay(args)
    try:
        relay.open()
        relay.run()
    except OSError as exc:
        print(f"rtp_host_relay error: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
