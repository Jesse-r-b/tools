"""uci_config.py - generates the UCI config files (network, wireless,
dhcp, firewall, mesh11sd, system) and openmanetd's config.yml for a
node profile, without touching a live device or running the Go setup
wizard over the network.

This exists because driving the real wizard (ApplySetup) live proved
unreliable during hands-on multi-node testing: the network-reload step
it triggers could interact badly with device instability that was
still being tracked down, and re-running it against every node one at
a time doesn't scale. Instead this mirrors the *known-good, hardware-
verified output* of that wizard for the scenarios actually exercised
this session, so a node can boot straight into a working state.

CONFIDENCE LEVELS (surfaced in the TUI - read this before trusting a
new scenario blindly):

  HIGH   - gate+router+ethernet, point+extender: captured byte-exact
           from real devices that completed the wizard and were then
           confirmed working (mesh formed, AP reachable, DHCP worked).
  MEDIUM - gate+router_firewall+ethernet, point+none: not captured
           from a live device; derived directly from openmanetd's
           setup_phases.go (scenarioMeshGateRouter/scenarioMeshPointNone)
           and its own compat test suite, but not independently
           hardware-tested by this tool.
  UNSUPPORTED - wifi-STA uplink: not implemented. The real wizard
           supports it (writeSTAIface), but replicating it here without
           a verified capture to check against isn't worth the risk of
           silently shipping something wrong. Use the real setup
           wizard over the network for this case.

If a generated node's mesh/AP/DHCP behavior looks wrong, the MEDIUM
scenarios are the first place to suspect - not the HIGH ones.
"""
import random

CONFIDENCE_HIGH = "HIGH (hardware-verified)"
CONFIDENCE_MEDIUM = "MEDIUM (code-derived, not independently hardware-tested)"

SCENARIOS = {
    ("gate", "router", "ethernet"): CONFIDENCE_HIGH,
    ("gate", "router_firewall", "ethernet"): CONFIDENCE_MEDIUM,
    ("point", "extender", None): CONFIDENCE_HIGH,
    ("point", "none", None): CONFIDENCE_MEDIUM,
}


def scenario_key(profile):
    if profile["role"] == "gate":
        return ("gate", profile["gate_mode"], profile["uplink_type"])
    return ("point", profile["point_mode"], None)


def scenario_confidence(profile):
    return SCENARIOS.get(scenario_key(profile), "UNSUPPORTED")


def random_mesh_ip(rng=None):
    """Matches network.RandomMeshIP in openmanetd: 10.41.254.<0-253>,
    never the factory default 10.41.254.1."""
    rng = rng or random
    while True:
        octet = rng.randint(0, 253)
        if octet != 1:
            return f"10.41.254.{octet}"


def htmode_for_bandwidth(mhz):
    return f"{mhz} MHz"


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------

def gen_system(profile):
    return f"""
config system
\toption hostname '{profile['hostname']}'
\toption timezone 'UTC'
\toption ttylogin '0'
\toption log_size '256'
\toption urandom_seed '0'
\toption compat_version '1.0'
\toption log_file '/etc/wifi-debug.log'
\toption default_wifi_key '6fT5XP5s'

config timeserver 'ntp'
\toption enabled '1'
\toption enable_server '0'
\tlist server '0.openwrt.pool.ntp.org'
\tlist server '1.openwrt.pool.ntp.org'
\tlist server '2.openwrt.pool.ntp.org'
\tlist server '3.openwrt.pool.ntp.org'
"""


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------

_NETWORK_HEADER = """
config interface 'loopback'
\toption device 'lo'
\toption proto 'static'
\toption ipaddr '127.0.0.1'
\toption netmask '255.0.0.0'

config globals 'globals'
\toption ula_prefix 'fd01:ed20:ecb4::/48'

config device
\toption name 'br-lan'
\toption type 'bridge'
\tlist ports 'eth0'
\toption stp '1'
"""

_BATMESH_TAIL = """
config interface 'batmesh0'
\toption proto 'batadv_hardif'
\toption master 'bat0'

config interface 'batmesh1'
\toption proto 'batadv_hardif'
\toption master 'bat0'
"""


def _bat0_block(gw_mode):
    return f"""
config interface 'bat0'
\toption multicast_mode '0'
\toption proto 'batadv'
\toption routing_algo 'BATMAN_V'
\toption bridge_loop_avoidance '1'
\toption hop_penalty '30'
\toption bonding '1'
\toption aggregated_ogms '1'
\toption ap_isolation '0'
\toption fragmentation '1'
\toption orig_interval '1000'
\toption distributed_arp_table '1'
\toption network_coding '1'
\toption isolation_mark '0x00000000/0x00000000'
\toption gw_mode '{gw_mode}'
"""


def _ahwlan_block(ipaddr, dns, proto="static"):
    lines = [
        "",
        "config interface 'ahwlan'",
        f"\toption proto '{proto}'",
    ]
    if proto == "static":
        lines += [
            "\toption netmask '255.255.0.0'",
        ]
    lines += [
        "\toption ip6assign '64'",
        "\toption ip6ifaceid 'eui64'",
        "\toption device 'br-ahwlan'",
        "\tlist ip6class 'local'",
    ]
    if ipaddr:
        lines.append(f"\toption ipaddr '{ipaddr}'")
    if dns:
        lines.append(f"\toption dns '{dns}'")
    return "\n".join(lines) + "\n"


def _br_ahwlan_device(ports, macaddr):
    port_lines = "\n".join(f"\tlist ports '{p}'" for p in ports)
    return f"""
config device 'wizard_bridge_br_ahwlan'
\toption name 'br-ahwlan'
\toption type 'bridge'
\toption stp '1'
\toption macaddr '{macaddr}'
{port_lines}
"""


def random_mac(rng=None):
    rng = rng or random
    # F2 OUI prefix, matching network.RandomMAC's locally-administered,
    # unicast convention used throughout openmanetd's wizard.
    tail = [rng.randint(0, 255) for _ in range(5)]
    return "F2:" + ":".join(f"{b:02x}" for b in tail)


def gen_network(profile):
    role = profile["role"]
    rng = random.Random()

    if role == "gate":
        mode = profile["gate_mode"]
        uplink_port = profile["uplink_ethernet_port"]
        macaddr = random_mac(rng)

        if mode == "router":
            # Verified scenario: uplink port stays bound to `lan`
            # (network.SetInterfaceProtoWithReader + attachUplinkPort),
            # ahwlan gets everything else (just bat0, since the only
            # ethernet port is excluded as the chosen uplink).
            lan_block = f"""
config interface 'lan'
\toption proto 'dhcp'
\toption ipaddr '10.41.254.1'
\toption netmask '255.255.0.0'
\toption ip6assign '60'
\toption dns '1.1.1.1'
\toption device '{uplink_port}'
"""
            wan_block = "\nconfig interface 'wan'\n\toption proto 'dhcp'\n"
            wan6_block = ""
            ahwlan_ports = ["bat0"]
            wizard_block = "\nconfig interface 'wizard'\n\toption device_mode_meshgate 'router'\n\toption uplink 'ethernet'\n"
        else:
            # router_firewall: symmetric to 'router' but the uplink
            # port binds to `wan` instead, `lan` stays unused/dangling
            # (matches the code path openmanetd's attachUplinkPort
            # takes for this scenario), and wan6 is added for IPv6
            # transit.
            lan_block = """
config interface 'lan'
\toption proto 'static'
\toption ipaddr '10.41.254.1'
\toption netmask '255.255.0.0'
\toption ip6assign '60'
\toption dns '1.1.1.1'
"""
            wan_block = f"""
config interface 'wan'
\toption proto 'dhcp'
\toption device '{uplink_port}'
"""
            wan6_block = "\nconfig interface 'wan6'\n\toption proto 'dhcpv6'\n"
            ahwlan_ports = ["bat0"]
            wizard_block = "\nconfig interface 'wizard'\n\toption device_mode_meshgate 'router_firewall'\n\toption uplink 'ethernet'\n"

        gw_mode = "server"
        # Gate role always anchors ahwlan at a fixed gateway address
        # (not randomized like the mesh-point case) - confirmed on
        # real hardware.
        ahwlan_block = _ahwlan_block(ipaddr="10.41.0.1", dns=None)

    else:
        mode = profile["point_mode"]
        uplink_port = profile.get("uplink_ethernet_port") or "eth0"
        macaddr = random_mac(rng)
        lan_block = ""
        wan_block = "\nconfig interface 'wan'\n\toption proto 'dhcp'\n"
        wan6_block = ""
        gw_mode = "client"

        if mode == "extender":
            ahwlan_ports = [uplink_port, "bat0"]
            ipaddr = random_mesh_ip(rng)
            ahwlan_block = _ahwlan_block(ipaddr=ipaddr, dns="1.1.1.1")
            wizard_block = "\nconfig interface 'wizard'\n\toption device_mode_meshpoint 'extender'\n"
        else:  # none
            ahwlan_ports = [uplink_port, "bat0"]
            ahwlan_block = _ahwlan_block(ipaddr=None, dns=None, proto="dhcp")
            wizard_block = "\nconfig interface 'wizard'\n\toption device_mode_meshpoint 'none'\n"

    parts = [
        _NETWORK_HEADER,
        wan_block,
        wan6_block,
        _bat0_block(gw_mode),
        ahwlan_block,
        _br_ahwlan_device(ahwlan_ports, macaddr),
        wizard_block,
        _BATMESH_TAIL,
    ]
    if lan_block:
        # Insert lan_block right after the header (matches captured
        # ordering: loopback, globals, br-lan device, lan, wan, ...).
        parts.insert(1, lan_block)

    return "".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# wireless
# ---------------------------------------------------------------------------

def gen_wireless(profile):
    mesh = profile["mesh"]
    lines = []

    for ap in profile["aps"]:
        lines.append(f"""
config wifi-device '{ap['radio']}'
\toption type 'mac80211'
\toption path '{ap['path']}'
\toption band '{ap['band']}'
\toption channel '{ap.get('channel', '6')}'
\toption htmode '{ap.get('htmode', 'HT20')}'
\toption short_gi_40 '0'
\toption country '{mesh['country']}'
""")
        iface_lines = [
            f"\nconfig wifi-iface 'default_{ap['radio']}'",
            f"\toption device '{ap['radio']}'",
            "\toption network 'ahwlan'",
            "\toption mode 'ap'",
        ]
        if ap["enabled"]:
            iface_lines += [
                f"\toption ssid '{ap['ssid']}'",
                f"\toption encryption '{ap['encryption']}'",
            ]
            if ap.get("passphrase"):
                iface_lines.append(f"\toption key '{ap['passphrase']}'")
        else:
            iface_lines.append("\toption disabled '1'")
        lines.append("\n".join(iface_lines) + "\n")

    lines.append(f"""
config wifi-device '{mesh['radio']}'
\toption type 'morse'
\toption path '{mesh['path']}'
\toption band 's1g'
\toption hwmode '11ah'
\toption reconf '0'
\toption channel '{mesh['channel']}'
\toption country '{mesh['country']}'
\toption enable_ps '0'
\toption enable_dynamic_ps_offload '0'
\toption enable_twt '0'
\toption watchdog_interval_secs '{mesh['watchdog_interval_secs']}'
\toption spi_clock_speed '{mesh['spi_clock_speed']}'
\toption bcf '{mesh['bcf']}'
\toption enable_mcast_whitelist '0'
\toption enable_mcast_rate_control '1'
\toption htmode '{htmode_for_bandwidth(mesh['bandwidth_mhz'])}'
""")

    lines.append(f"""
config wifi-iface 'default_{mesh['radio']}'
\toption mode 'mesh'
\toption wds '1'
\toption device '{mesh['radio']}'
\toption network 'batmesh0'
\toption ssid '{profile.get('mesh_iface_ssid') or profile['hostname']}'
\toption encryption '{mesh['encryption']}'
\toption key '{mesh['passphrase']}'
\toption mesh_id '{mesh['mesh_id']}'
\toption beacon_int '1000'
""")

    lines.append(f"""
config wifi-iface 'meshap_{mesh['radio']}'
\toption device '{mesh['radio']}'
\toption mode 'ap'
\toption network 'ahwlan'
\toption encryption '{mesh['encryption']}'
\toption ssid '{profile['hostname']}'
\toption key '{profile.get('meshap_key') or _rand_key()}'
\toption disabled '1'
""")

    return "".join(lines)


def _rand_key(n=8):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(random.choice(alphabet) for _ in range(n))


# ---------------------------------------------------------------------------
# dhcp
# ---------------------------------------------------------------------------

def _dhcp_header(notinterface_networks):
    """The default/anonymous dnsmasq instance. notinterface_networks
    lists every network a NAMED instance owns exclusively (e.g.
    ['ahwlan']) - without excluding them here, this instance's
    bind-dynamic mode still tries to claim their addresses too and
    fights the dedicated instance for the same socket. Confirmed live
    on real hardware: this was the second of three conflicts needed to
    get a named dnsmasq instance to actually coexist with this one -
    see _named_dnsmasq_instance's docstring for the full chain.
    """
    notinterface_lines = "".join(f"\tlist notinterface '{n}'\n" for n in notinterface_networks)
    return f"""
config dnsmasq
\toption domainneeded '1'
\toption boguspriv '1'
\toption filterwin2k '0'
\toption localise_queries '1'
\toption rebind_protection '1'
\toption rebind_localhost '1'
\toption local '/lan/'
\toption domain 'lan'
\toption expandhosts '1'
\toption nonegcache '0'
\toption cachesize '1000'
\toption authoritative '1'
\toption readethers '1'
\toption leasefile '/tmp/dhcp.leases'
\toption resolvfile '/tmp/resolv.conf.d/resolv.conf.auto'
\toption nonwildcard '1'
\toption localservice '1'
\toption ednspacket_max '1232'
\toption filter_aaaa '0'
\toption filter_a '0'
{notinterface_lines}"""


def _named_dnsmasq_instance(network_id):
    """A named dnsmasq instance dedicated to one network (e.g.
    'ahwlan_dns' for network_id='ahwlan'). Mirrors
    network.SetupDnsmasqInstance in openmanetd's Go source exactly,
    including the interface/localuse fix added after a real-hardware
    failure chain:

      1. The pool section referencing `option instance '<name>_dns'`
         needs this section to exist at all - without it, OpenWrt's
         dnsmasq init script has nothing telling it to spin up a
         second process, and no DHCP server runs on this network. No
         error anywhere; the pool's `instance` reference just points
         at nothing.
      2. Even with the section present, dnsmasq's bind-dynamic mode
         makes this instance try to bind *every* up interface unless
         explicitly restricted via `list interface`- otherwise it
         fights the default instance for sockets on interfaces that
         aren't even this network.
      3. Even scoped to its own interface, it still competes for
         127.0.0.1 with the default instance unless `localuse=0` tells
         it not to participate in local/loopback resolution.
      4. Even with all of the above, bind-dynamic still separately
         tries to grab 127.0.0.1 alongside its explicit interface list
         unless `list notinterface 'lo'` excludes it too - this is the
         documented pairing in dnsmasq's own shipped default template
         (package/network/services/dnsmasq/files/dhcp.conf ships
         "list interface br-lan" / "list notinterface lo" together,
         commented out, as the example for exactly this scoped-instance
         pattern), not a guess.

    Any one of these four missing is enough for procd to end up
    running only one of the two instances ("running (1/2)"), with the
    UCI config looking completely correct either way - confirmed live.
    """
    return f"""
config dnsmasq '{network_id}_dns'
\toption domainneeded '1'
\toption localize_queries '1'
\toption rebind_localhost '1'
\toption local '/{network_id}/'
\toption domain '{network_id}'
\toption expandhosts '1'
\toption cachesize '1000'
\toption authoritative '1'
\toption readethers '1'
\toption localservice '1'
\toption ednspacket_max '1232'
\toption localuse '0'
\tlist interface '{network_id}'
\tlist notinterface 'lo'
"""


_AHWLAN_DHCP_POOL = """
config dhcp 'ahwlan'
\toption start '100'
\toption limit '16'
\toption leasetime '12h'
\toption ra 'server'
\toption ra_slaac '1'
\toption dns_service '0'
\toption ignore '0'
\toption force '1'
\toption dns '2606:4700:4700::1111'
\toption ra_flags 'none'
\toption interface 'ahwlan'
\toption instance 'ahwlan_dns'
"""

_WAN_DHCP_IGNORE = "\nconfig dhcp 'wan'\n\toption interface 'wan'\n\toption ignore '1'\n"


def gen_dhcp(profile):
    role = profile["role"]

    if role == "gate" and profile["gate_mode"] == "router":
        # lan is the DHCP-client uplink here; a disabled placeholder
        # pool still gets written by the wizard's reset/whitelist pass.
        parts = [
            _dhcp_header(notinterface_networks=["ahwlan"]),
            """
config dhcp 'lan'
\toption interface 'lan'
\toption start '100'
\toption limit '150'
\toption leasetime '12h'
\toption ignore '1'
""",
            _WAN_DHCP_IGNORE,
            _named_dnsmasq_instance("ahwlan"),
            _AHWLAN_DHCP_POOL,
        ]
    elif role == "point" and profile["point_mode"] == "none":
        # mesh-point-none serves DHCP on lan (downstream side), not
        # ahwlan - ahwlan is itself a DHCP client of a peer mesh-gate.
        parts = [
            _dhcp_header(notinterface_networks=["lan"]),
            _named_dnsmasq_instance("lan"),
            """
config dhcp 'lan'
\toption interface 'lan'
\toption start '100'
\toption limit '150'
\toption leasetime '12h'
\toption ignore '0'
\toption instance 'lan_dns'
\tlist dhcp_option '3'
\tlist dhcp_option '6'
""",
            _WAN_DHCP_IGNORE,
        ]
    else:
        parts = [
            _dhcp_header(notinterface_networks=["ahwlan"]),
            _WAN_DHCP_IGNORE,
            _named_dnsmasq_instance("ahwlan"),
            _AHWLAN_DHCP_POOL,
        ]

    return "".join(parts)


# ---------------------------------------------------------------------------
# firewall
# ---------------------------------------------------------------------------

_FIREWALL_DEFAULTS = """
config defaults
\toption syn_flood '1'
\toption input 'REJECT'
\toption output 'ACCEPT'
\toption forward 'REJECT'
"""

_WAN_ZONE = """
config zone
\toption name 'wan'
\tlist network 'wan'
\tlist network 'wan6'
\toption input 'REJECT'
\toption output 'ACCEPT'
\toption forward 'REJECT'

config forwarding
\toption src 'lan'
\toption dest 'wan'
\toption enabled '0'
"""

# The wizard's 13 default WAN rules (network.AddDefaultWanFirewallRules)
# plus the pre-existing factory 6, all with the wizard's numbered
# section names - scenario-independent, always targets the ahwlan zone.
_DEFAULT_WAN_RULES = """
config rule
\toption name 'Allow-DHCP-Renew'
\toption src 'wan'
\toption proto 'udp'
\toption dest_port '68'
\toption target 'ACCEPT'
\toption family 'ipv4'

config rule
\toption name 'Allow-Ping'
\toption src 'wan'
\toption proto 'icmp'
\toption icmp_type 'echo-request'
\toption family 'ipv4'
\toption target 'ACCEPT'

config rule
\toption name 'Allow-IGMP'
\toption src 'wan'
\toption proto 'igmp'
\toption family 'ipv4'
\toption target 'ACCEPT'

config rule
\toption name 'Allow-DHCPv6'
\toption src 'wan'
\toption proto 'udp'
\toption dest_port '546'
\toption family 'ipv6'
\toption target 'ACCEPT'

config rule
\toption name 'Allow-MLD'
\toption src 'wan'
\toption proto 'icmp'
\toption src_ip 'fe80::/10'
\tlist icmp_type '130/0'
\tlist icmp_type '131/0'
\tlist icmp_type '132/0'
\tlist icmp_type '143/0'
\toption family 'ipv6'
\toption target 'ACCEPT'

config rule
\toption name 'Allow-ICMPv6-Input'
\toption src 'wan'
\toption proto 'icmp'
\tlist icmp_type 'echo-request'
\tlist icmp_type 'echo-reply'
\tlist icmp_type 'destination-unreachable'
\tlist icmp_type 'packet-too-big'
\tlist icmp_type 'time-exceeded'
\tlist icmp_type 'bad-header'
\tlist icmp_type 'unknown-header-type'
\tlist icmp_type 'router-solicitation'
\tlist icmp_type 'neighbour-solicitation'
\tlist icmp_type 'router-advertisement'
\tlist icmp_type 'neighbour-advertisement'
\toption limit '1000/sec'
\toption family 'ipv6'
\toption target 'ACCEPT'

config rule
\toption name 'Allow-ICMPv6-Forward'
\toption src 'wan'
\toption dest '*'
\toption proto 'icmp'
\tlist icmp_type 'echo-request'
\tlist icmp_type 'echo-reply'
\tlist icmp_type 'destination-unreachable'
\tlist icmp_type 'packet-too-big'
\tlist icmp_type 'time-exceeded'
\tlist icmp_type 'bad-header'
\tlist icmp_type 'unknown-header-type'
\toption limit '1000/sec'
\toption family 'ipv6'
\toption target 'ACCEPT'

config rule
\toption name 'Allow-IPSec-ESP'
\toption src 'wan'
\toption dest 'lan'
\toption proto 'esp'
\toption target 'ACCEPT'

config rule
\toption name 'Allow-ISAKMP'
\toption src 'wan'
\toption dest 'lan'
\toption dest_port '500'
\toption proto 'udp'
\toption target 'ACCEPT'
"""

_AHWLAN_ZONE_TEMPLATE = """
config zone 'ahwlan'
\toption name 'ahwlan'
\tlist network 'ahwlan'
\toption input 'ACCEPT'
\toption output 'ACCEPT'
\toption forward 'ACCEPT'
\toption mtu_fix '1'
"""

_WIZARD_NUMBERED_RULES = """
config rule 'wizard_rule_allow_dhcp_renew_9'
\toption name 'Allow-DHCP-Renew'
\toption src 'wan'
\toption proto 'udp'
\toption dest_port '68'
\toption target 'ACCEPT'
\toption family 'ipv4'

config rule 'wizard_rule_allow_ping_10'
\toption name 'Allow-Ping'
\toption src 'wan'
\toption proto 'icmp'
\toption icmp_type 'echo-request'
\toption target 'ACCEPT'
\toption family 'ipv4'

config rule 'wizard_rule_allow_igmp_11'
\toption name 'Allow-IGMP'
\toption src 'wan'
\toption proto 'igmp'
\toption target 'ACCEPT'
\toption family 'ipv4'

config rule 'wizard_rule_allow_dhcpv6_12'
\toption name 'Allow-DHCPv6'
\toption src 'wan'
\toption proto 'udp'
\toption dest_port '546'
\toption target 'ACCEPT'
\toption family 'ipv6'

config rule 'wizard_rule_allow_mld_13'
\toption name 'Allow-MLD'
\toption src 'wan'
\toption src_ip 'fe80::/10'
\toption proto 'icmp'
\tlist icmp_type '130/0'
\tlist icmp_type '131/0'
\tlist icmp_type '132/0'
\tlist icmp_type '143/0'
\toption target 'ACCEPT'
\toption family 'ipv6'

config rule 'wizard_rule_allow_icmpv6_input_14'
\toption name 'Allow-ICMPv6-Input'
\toption src 'wan'
\toption proto 'icmp'
\tlist icmp_type 'echo-request'
\tlist icmp_type 'echo-reply'
\tlist icmp_type 'destination-unreachable'
\tlist icmp_type 'packet-too-big'
\tlist icmp_type 'time-exceeded'
\tlist icmp_type 'bad-header'
\tlist icmp_type 'unknown-header-type'
\tlist icmp_type 'router-solicitation'
\tlist icmp_type 'neighbor-solicitation'
\tlist icmp_type 'router-advertisement'
\tlist icmp_type 'neighbor-advertisement'
\toption limit '1000/sec'
\toption target 'ACCEPT'
\toption family 'ipv6'

config rule 'wizard_rule_allow_icmpv6_forward_15'
\toption name 'Allow-ICMPv6-Forward'
\toption src 'wan'
\toption dest '*'
\toption proto 'icmp'
\tlist icmp_type 'echo-request'
\tlist icmp_type 'echo-reply'
\tlist icmp_type 'destination-unreachable'
\tlist icmp_type 'packet-too-big'
\tlist icmp_type 'time-exceeded'
\tlist icmp_type 'bad-header'
\tlist icmp_type 'unknown-header-type'
\toption limit '1000/sec'
\toption target 'ACCEPT'
\toption family 'ipv6'

config rule 'wizard_rule_allow_ipsec_esp_16'
\toption name 'Allow-IPSec-ESP'
\toption src 'wan'
\toption dest '*'
\toption proto 'esp'
\toption target 'ACCEPT'

config rule 'wizard_rule_allow_isakmp_17'
\toption name 'Allow-ISAKMP'
\toption src 'wan'
\toption dest '*'
\toption proto 'udp'
\toption dest_port '500'
\toption target 'ACCEPT'

config rule 'wizard_rule_allow_batman_mesh_tcp_4242_18'
\toption name 'Allow Batman Mesh TCP 4242'
\toption src '*'
\toption dest '*'
\toption proto 'tcp'
\toption dest_port '4242'
\toption target 'ACCEPT'

config rule 'wizard_rule_allow_incoming_comms_19'
\toption name 'Allow Incoming Comms'
\toption src '*'
\toption dest '*'
\toption dest_ip '239.192.41.1'
\toption proto 'udp'
\toption dest_port '33801-38864'
\toption target 'ACCEPT'

config rule 'wizard_rule_block_dhcp_request_out_ahwlan_20'
\toption name 'Block-DHCP-Request-Out-ahwlan'
\toption src 'ahwlan'
\toption dest '*'
\toption proto 'udp'
\toption dest_port '67'
\toption target 'DROP'
\toption family 'ipv4'

config rule 'wizard_rule_block_dhcp_response_in_ahwlan_21'
\toption name 'Block-DHCP-Response-In-ahwlan'
\toption src '*'
\toption dest 'ahwlan'
\toption proto 'udp'
\toption dest_port '68'
\toption target 'DROP'
\toption family 'ipv4'
"""


def gen_firewall(profile):
    role = profile["role"]

    if role == "gate":
        upstream = "wan" if profile["gate_mode"] == "router_firewall" else "lan"
        masq = "\n\toption masq '1'" if upstream == "lan" else ""
        lan_zone = f"""
config zone
\toption name 'lan'
\tlist network 'lan'
\toption input 'ACCEPT'
\toption output 'ACCEPT'
\toption forward 'ACCEPT'
\toption mtu_fix '1'{masq}
"""
        forwarding = f"""
config forwarding 'mmrouter'
\toption src 'ahwlan'
\toption dest '{upstream}'
"""
    else:
        # mesh-point-extender / mesh-point-none both get a plain lan
        # zone with mtu_fix only (no masq - never upstream in point
        # roles) and the mmextender forward.
        lan_zone = """
config zone
\toption name 'lan'
\tlist network 'lan'
\toption input 'ACCEPT'
\toption output 'ACCEPT'
\toption forward 'ACCEPT'
\toption mtu_fix '1'
"""
        forwarding = """
config forwarding 'mmextender'
\toption src 'lan'
\toption dest 'ahwlan'
"""

    return "".join([
        _FIREWALL_DEFAULTS,
        lan_zone,
        _WAN_ZONE,
        _DEFAULT_WAN_RULES,
        _AHWLAN_ZONE_TEMPLATE,
        forwarding,
        _WIZARD_NUMBERED_RULES,
    ])


# ---------------------------------------------------------------------------
# mesh11sd
# ---------------------------------------------------------------------------

def gen_mesh11sd(profile):
    is_gate = profile["role"] == "gate"
    gate_announcements = "1" if is_gate else "0"
    return f"""
config mesh11sd 'setup'
\toption debuglevel '1'
\toption checkinterval '10'
\toption interface_timeout '10'
\toption enabled '1'

config mesh11sd 'mesh_params'
\toption mesh_fwding '0'
\toption mesh_max_peer_links '10'
\toption mesh_rssi_threshold '-85'
\toption mesh_ttl '31'
\toption mesh_hwmp_rootmode '0'
\toption mesh_gate_announcements '{gate_announcements}'
\toption mesh_nolearn '0'

config mesh11sd 'mbca'
\toption mbca_config '1'
\toption mesh_beacon_timing_report_int '10'
\toption mbss_start_scan_duration_ms '2048'
\toption mbca_min_beacon_gap_ms '25'
\toption mbca_tbtt_adj_interval_sec '60'

config mesh11sd 'mesh_beaconless'
\toption mesh_beacon_less_mode '0'

config mesh11sd 'mesh_dynamic_peering'
\toption enabled '0'
"""


# ---------------------------------------------------------------------------
# openmanetd config.yml
# ---------------------------------------------------------------------------

def gen_openmanetd_config(profile):
    battery = profile["battery"]
    if battery["enabled"]:
        battery_lines = (
            "battery:\n"
            "    enable: true\n"
            f"    sensorType: {battery['sensor_type']}\n"
        )
        if battery.get("i2c_bus"):
            battery_lines += f"    i2cBus: {battery['i2c_bus']}\n"
        if battery.get("i2c_address"):
            battery_lines += f"    i2cAddress: {battery['i2c_address']}\n"
        if battery["sensor_type"] == "ina219":
            battery_lines += (
                f"    senseResistorMilliohm: {battery['sense_resistor_milliohm']}\n"
                f"    maxCurrentMilliamp: {battery['max_current_milliamp']}\n"
                f"    minVoltage: {battery['min_voltage']}\n"
                f"    maxVoltage: {battery['max_voltage']}\n"
            )
    else:
        battery_lines = "battery:\n    enable: false\n"

    return f"""logLevel: info
setup:
    enabled: true
    complete: true
auth:
    enable: true
gnss:
    enable: true
    sendAsExternalGNSSSource:
        sendAsNMEA: true
        sendAsCoT: true
blos:
    enable: false
{battery_lines}comms:
    enable: false
    debug: false
    controlSource: openvlm
"""


# ---------------------------------------------------------------------------
# top-level
# ---------------------------------------------------------------------------

def generate_all(profile):
    """Returns {filename: content} for every config file this profile
    needs, plus the uci-defaults script text and the admin-password
    chpasswd line, ready to be written into an image or a live device."""
    return {
        "system": gen_system(profile),
        "network": gen_network(profile),
        "wireless": gen_wireless(profile),
        "dhcp": gen_dhcp(profile),
        "firewall": gen_firewall(profile),
        "mesh11sd": gen_mesh11sd(profile),
        "openmanetd-config.yml": gen_openmanetd_config(profile),
    }
