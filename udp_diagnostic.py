"""
udp_diagnostic.py

One-off diagnostic: sends a real BitTorrent UDP tracker 'connect' packet
(BEP 15 protocol) to a known public tracker and checks for a reply.

A reply means outbound UDP genuinely works end-to-end on this runner.
No reply/timeout strongly suggests UDP is being blocked or dropped
somewhere on the network path.
"""

import socket
import struct

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(6)

# BEP 15: UDP tracker protocol "connect" request
# protocol_id (magic constant) + action=0 (connect) + random transaction_id
protocol_id = 0x41727101980
transaction_id = 12345
packet = struct.pack(">QII", protocol_id, 0, transaction_id)

try:
    s.sendto(packet, ("tracker.opentrackr.org", 1337))
    data, addr = s.recvfrom(1024)
    action, recv_txn, connection_id = struct.unpack(">IIQ", data[:16])
    if recv_txn == transaction_id and action == 0:
        print(f"SUCCESS: got a valid UDP tracker reply from {addr}. UDP works fine end-to-end.")
    else:
        print(f"Got a reply from {addr} but it didn't match the expected format — unusual, but still means UDP round-tripped.")
except socket.timeout:
    print("TIMEOUT: no reply within 6 seconds. This strongly suggests outbound UDP is being blocked or silently dropped on this runner.")
except Exception as e:
    print(f"UDP socket error: {e}")
