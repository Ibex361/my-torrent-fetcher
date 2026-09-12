"""
udp_diagnostic.py

Diagnostic: tests connectivity to every tracker used in your torrents,
both UDP (BEP 15 'connect' packet) and HTTP/HTTPS (a real GET request
against the announce URL), and reports a clear per-tracker verdict.

Purpose: distinguish between two different possible causes of torrents
stalling on GitHub Actions runners:
  1. Outbound UDP is blocked/dropped at the network level (would show
     ALL UDP trackers timing out, including ones known to work fine
     from home networks).
  2. IP-reputation blocking — some trackers/swarms refuse or silently
     drop traffic from known datacenter/cloud IP ranges (which GitHub
     Actions runners are), while others don't care. This would show a
     MIX of some trackers responding and others not.
"""

import socket
import struct
import urllib.request
import urllib.error

# Trackers actually seen in your magnet links (the UDP ones from the
# failing torrent) plus the HTTP/HTTPS fallbacks this project already adds.
UDP_TRACKERS = [
    ("tracker.bittor.pw", 1337),
    ("tracker.opentrackr.org", 1337),
    ("tracker.dler.org", 6969),
    ("open.stealth.si", 80),
    ("tracker.torrent.eu.org", 451),
    ("exodus.desync.com", 6969),
    ("open.demonii.com", 1337),
]

HTTP_TRACKERS = [
    "https://tracker.opentrackr.org:443/announce",
    "https://tracker.gbitt.info/announce",
    "https://tracker.tamersunion.org:443/announce",
    "http://open.acgnxtracker.com:80/announce",
    "http://tracker.files.fm:6969/announce",
]


def test_udp_tracker(host: str, port: int) -> str:
    """Send a real BEP-15 'connect' packet, return a short verdict string."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(6)
        protocol_id = 0x41727101980
        transaction_id = 12345
        packet = struct.pack(">QII", protocol_id, 0, transaction_id)
        s.sendto(packet, (host, port))
        data, addr = s.recvfrom(1024)
        action, recv_txn, connection_id = struct.unpack(">IIQ", data[:16])
        if recv_txn == transaction_id and action == 0:
            return f"✅ RESPONDED (from {addr[0]})"
        return "⚠️  replied but format unexpected (still means UDP worked)"
    except socket.timeout:
        return "❌ TIMEOUT (no reply)"
    except socket.gaierror as e:
        return f"❌ DNS/RESOLVE ERROR: {e}"
    except Exception as e:
        return f"❌ ERROR: {e}"


def test_http_tracker(url: str) -> str:
    """
    Do a minimal GET against the tracker's announce endpoint. Real trackers
    will respond (often with an error about missing params, which is FINE -
    it proves the tracker is reachable and answering). A timeout or
    connection refusal suggests blocking.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aria2/1.36.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            return f"✅ RESPONDED (HTTP {resp.status})"
    except urllib.error.HTTPError as e:
        # Trackers commonly return 4xx for a GET with no torrent info_hash -
        # that's still a successful connection, just an expected protocol error.
        return f"✅ RESPONDED (HTTP {e.code} — expected error for a bare GET, means it's reachable)"
    except urllib.error.URLError as e:
        return f"❌ UNREACHABLE: {e.reason}"
    except Exception as e:
        return f"❌ ERROR: {e}"


def main():
    print("=" * 70)
    print("UDP TRACKER TESTS (real BitTorrent BEP-15 connect packets)")
    print("=" * 70)
    udp_success = 0
    for host, port in UDP_TRACKERS:
        verdict = test_udp_tracker(host, port)
        print(f"  udp://{host}:{port:<6} -> {verdict}")
        if "✅" in verdict:
            udp_success += 1

    print()
    print("=" * 70)
    print("HTTP/HTTPS TRACKER TESTS")
    print("=" * 70)
    http_success = 0
    for url in HTTP_TRACKERS:
        verdict = test_http_tracker(url)
        print(f"  {url:<55} -> {verdict}")
        if "✅" in verdict:
            http_success += 1

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"UDP trackers responding:  {udp_success}/{len(UDP_TRACKERS)}")
    print(f"HTTP trackers responding: {http_success}/{len(HTTP_TRACKERS)}")
    print()
    if udp_success == 0:
        print("→ ALL UDP trackers failed. This looks like outbound UDP is broadly")
        print("  blocked/dropped on this runner's network, not an IP-reputation issue.")
    elif udp_success < len(UDP_TRACKERS):
        print("→ SOME UDP trackers responded, others didn't. This looks like")
        print("  per-tracker/IP-reputation blocking rather than a blanket UDP block.")
        print("  Trackers that specifically serve a given torrent's swarm may still")
        print("  reject GitHub's IP even though UDP itself works fine in general.")
    else:
        print("→ All UDP trackers responded fine here. If a specific torrent still")
        print("  stalls, the issue is likely that torrent's specific swarm/trackers")
        print("  rejecting this runner's IP, or a DHT-only swarm not reachable here.")


if __name__ == "__main__":
    main()
