"""Prints this computer's address on the local network (used so a phone on the same Wi-Fi can open scanned QR codes)."""
import socket

try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("10.255.255.255", 1))
    print(s.getsockname()[0])
except OSError:
    print("127.0.0.1")
