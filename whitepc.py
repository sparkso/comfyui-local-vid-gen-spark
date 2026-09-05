#!/usr/bin/env python3
"""Sleep / wake / status for White-PC, run from tigerclaw (same LAN).

  python3 whitepc.py status   # ComfyUI reachable? ssh reachable?
  python3 whitepc.py sleep    # S3 standby via PowerShell over ssh (Tailscale address)
  python3 whitepc.py wake     # Wake-on-LAN magic packet to the Realtek NIC (armed, verified 2026-09-05)
"""
import socket
import subprocess
import sys
import time
import urllib.request

MAC = "18:c0:4d:2a:fc:a4"
LAN_IP = "10.0.0.20"
BROADCAST = "10.0.0.255"
SSH_HOST = "spark@100.103.198.60"      # ssh only answers on the Tailscale address
SSH_KEY = "/Users/tigerclaw/.ssh/id_ed25519"
COMFY = f"http://{LAN_IP}:8000/system_stats"


def comfy_up(timeout=3):
    try:
        urllib.request.urlopen(COMFY, timeout=timeout)
        return True
    except Exception:
        return False


def wake():
    payload = b"\xff" * 6 + bytes.fromhex(MAC.replace(":", "")) * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for port in (9, 7):
        for _ in range(3):
            for dest in (BROADCAST, "255.255.255.255", LAN_IP):
                try:
                    s.sendto(payload, (dest, port))
                except OSError:
                    pass  # unicast to a sleeping host has no ARP entry; broadcast is what matters
    s.close()
    print("magic packets sent; waiting for ComfyUI on White-PC ...")
    for i in range(36):
        if comfy_up():
            print(f"ComfyUI up after {i * 5}s")
            return 0
        time.sleep(5)
    print("no answer after 3 minutes (ComfyUI may still be starting, or WoL did not fire)")
    return 1


def sleep():
    cmd = [
        "ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", SSH_HOST,
        'powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; '
        "[System.Windows.Forms.Application]::SetSuspendState([System.Windows.Forms.PowerState]::Suspend, $false, $false)\"",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        err = "\n".join(l for l in r.stderr.splitlines() if "post-quantum" not in l and "store now" not in l and "pq.html" not in l)
        print("ssh exit", r.returncode, err.strip())
    except subprocess.TimeoutExpired:
        print("ssh session hung (expected once the host suspends)")
    time.sleep(10)
    print("asleep" if not comfy_up() else "still answering; sleep may not have taken")
    return 0


def status():
    print("ComfyUI:", "up" if comfy_up() else "down")
    r = subprocess.run(["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", SSH_HOST, "hostname"],
                       capture_output=True, text=True)
    print("ssh:", r.stdout.strip() or "unreachable")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit({"wake": wake, "sleep": sleep, "status": status}.get(action, status)())
