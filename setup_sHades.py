#!/usr/bin/env python3
"""sHades setup / deployment script.

Two deployment modes:

  fresh    Bootstrap a brand-new sHades community hub (guild server):
           picks a storage dir, writes a standalone RNS config, generates the
           guild identity, optionally seeds moderators.txt, launches sHades.py
           and prints the three destination hashes (node / delivery / propagation).

  join     Configure this machine to connect to a pre-existing community hub:
           prompts for the hub's node_hash and delivery_hash, generates a local
           client identity, and writes a client config so you (or the Kivy client)
           can post to and browse the hub.

Run:  /usr/bin/python3 setup_sHades.py
"""
import os
import json
import time
import subprocess
import re

RETI_DIR = os.path.dirname(os.path.abspath(__file__))
SHades_PY = os.path.join(RETI_DIR, "sHades.py")

DEFAULT_STORAGE = os.path.expanduser("~/.sHades")
DEFAULT_GUILD_NAME = "sHades Hub"
CLIENT_CONFIG = os.path.expanduser("~/.sHades_client.json")

HUB_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
ADDRESS_RE = re.compile(r"^([^:]+):(\d+)$")


def prompt(text, default=None):
    if default is not None:
        text = f"{text} [{default}] "
    else:
        text = f"{text}: "
    val = input(text).strip()
    return val if val else (default or "")


def yes_no(text, default=True):
    suffix = "Y/n" if default else "y/N"
    while True:
        val = input(f"{text} [{suffix}] ").strip().lower()
        if not val:
            return default
        if val in ("y", "yes"):
            return True
        if val in ("n", "no"):
            return False
        print("  please answer y or n")


def hash_prompt(text):
    while True:
        h = prompt(text).strip().lower()
        if HUB_HASH_RE.match(h):
            return h
        print("  not a valid 32-hex destination hash, try again")


def address_prompt(text):
    while True:
        a = prompt(text).strip()
        if not a:
            print("  hub address is required, try again")
            continue
        m = ADDRESS_RE.match(a)
        if m:
            return m.group(1), int(m.group(2))
        print("  not a valid ip:port address (e.g. 192.168.1.50:29221), try again")


def rns_config_text():
    return (
        "[reticulum]\n"
        "  enable_transport = no\n"
        "  share_instance = no\n\n"
        "[interfaces]\n"
        "  [[AutoInterface]]\n"
        "    type = AutoInterface\n"
        "    enabled = yes\n"
    )


def fresh_hub():
    """Bootstrap a new sHades community hub."""
    print("\n=== Fresh sHades community hub ===")
    storage = prompt("Storage directory", DEFAULT_STORAGE) or DEFAULT_STORAGE
    name = prompt("Guild name", DEFAULT_GUILD_NAME) or DEFAULT_GUILD_NAME

    os.makedirs(storage, exist_ok=True)
    os.makedirs(os.path.join(storage, "attachments"), exist_ok=True)
    os.makedirs(os.path.join(storage, "avatars"), exist_ok=True)

    # RNS config (standalone, share_instance=no, AutoInterface for LAN discovery)
    rns_dir = os.path.join(storage, "rns")
    cfgfile = os.path.join(rns_dir, "config")
    if not os.path.exists(cfgfile):
        os.makedirs(rns_dir, exist_ok=True)
        with open(cfgfile, "w") as f:
            f.write(rns_config_text())
        print(f"  wrote RNS config: {cfgfile}")

    # Guild identity
    ipath = os.path.join(storage, "identity")
    if not os.path.exists(ipath):
        import RNS
        ident = RNS.Identity()
        ident.to_file(ipath)
        print(f"  generated guild identity: {ipath}")

    # moderators.txt (optional seed)
    mod_path = os.path.join(storage, "moderators.txt")
    if yes_no("Seed moderators.txt with your client identity now?", False):
        client_hash = prompt("Your client lxmf.delivery hash (32 hex)", "")
        if HUB_HASH_RE.match(client_hash):
            with open(mod_path, "w") as f:
                f.write(client_hash + "\n")
            print(f"  wrote moderators.txt with {client_hash[:12]}...")
        else:
            print("  skipped (invalid hash)")

    print(f"\nStorage ready: {storage}")
    print("  guild db   :", os.path.join(storage, "sHades.db"))
    print("  rns config :", cfgfile)
    print("  identity   :", ipath)

    if yes_no("\nLaunch the sHades guild server now?", True):
        delay = prompt("Announce delay (seconds)", "2") or "2"
        listen = prompt("Listen address:port for direct client connections "
                        "(blank = LAN discovery only)", "")
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        server_args = ["/usr/bin/python3", SHades_PY, "--storage", storage,
                       "--name", name, "--announce-delay", delay]
        if listen:
            server_args.extend(["--listen", listen])
        proc = subprocess.Popen(
            server_args,
            cwd=RETI_DIR, env=env,
            stdout=open(os.path.join(storage, "sHades.log"), "ab"),
            stderr=subprocess.STDOUT)
        print(f"  sHades server started (PID {proc.pid}), waiting for boot...")
        info_path = os.path.join(storage, "guild_info.json")
        info = None
        for _ in range(300):
            if os.path.isfile(info_path):
                info = json.load(open(info_path))
                break
            if proc.poll() is not None:
                print(open(os.path.join(storage, "sHades.log")).read())
                print("  server exited early; check the log above")
                return
            time.sleep(0.25)
        if info:
            print("\n=== sHades community hub is live ===")
            print(f"  name         : {info['name']}")
            print(f"  node_hash    : {info['node_hash']}   (NomadNet: add as node to browse)")
            print(f"  delivery_hash: {info['delivery_hash']}  (LXMF: #channel first token)")
            print(f"  propagation  : {info['prop_hash']}")
            print(f"  storage      : {info['storage']}")
            print("\nThe server is running in the background. Stop it with: pkill -f sHades.py")
            print("Or run it in the foreground: /usr/bin/python3 sHades.py --storage", storage)
        else:
            print("  timed out waiting for guild_info.json; check the log")


def join_hub():
    """Configure this machine to join a pre-existing community hub."""
    print("\n=== Join a pre-existing sHades community hub ===")
    print("Connect to the hub by its address (ip:port).")
    print("Get this from the hub admin, or from the hub's startup output.")
    hub_host, hub_port = address_prompt("Hub address (ip:port)")

    print("\nOptional: guild destination hashes from the hub's guild_info.json.")
    print("Leave blank to skip — you'll still reach the hub by address.")
    node_hash = prompt("Hub node_hash (32 hex, optional)", "").strip().lower() or None
    delivery_hash = prompt("Hub delivery_hash (32 hex, optional)", "").strip().lower() or None
    prop_hash = prompt("Hub propagation_hash (32 hex, optional)", "").strip().lower() or None

    if node_hash and not HUB_HASH_RE.match(node_hash):
        print("  invalid node_hash, clearing")
        node_hash = None
    if delivery_hash and not HUB_HASH_RE.match(delivery_hash):
        print("  invalid delivery_hash, clearing")
        delivery_hash = None
    if prop_hash and not HUB_HASH_RE.match(prop_hash):
        print("  invalid propagation_hash, clearing")
        prop_hash = None

    # Generate a local client identity
    client_dir = os.path.expanduser("~/.sHades_client")
    os.makedirs(client_dir, exist_ok=True)
    import RNS
    ident = RNS.Identity()
    ipath = os.path.join(client_dir, "identity")
    ident.to_file(ipath)
    client_delivery = RNS.Destination(
        ident, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
    client_hash = RNS.hexrep(client_delivery.hash, delimit=False)

    cfg = {
        "hub": {
            "address": "%s:%d" % (hub_host, hub_port),
            "host": hub_host,
            "port": hub_port,
            "node_hash": node_hash,
            "delivery_hash": delivery_hash,
            "propagation_hash": prop_hash,
        },
        "client": {
            "identity": ipath,
            "delivery_hash": client_hash,
        },
    }
    with open(CLIENT_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)

    print(f"\nClient config written: {CLIENT_CONFIG}")
    print(f"  hub address : {hub_host}:{hub_port}")
    print(f"  client delivery hash: {client_hash}")
    if node_hash:
        print(f"  hub node_hash : {node_hash}")
    print("\nTo connect, run the sHades client and enter the hub address above.")
    print("The RNS config (~/.sHades_client/rns/config) now routes to the hub directly.")
    print("\nIf you run the sHades server on this machine too, point it at a *different* "
          "storage dir and set share_instance=yes in both RNS configs to share the instance.")


def main():
    print("sHades setup — Reticulum-native community hub")
    print("1) fresh  — bootstrap a new sHades guild server (community hub)")
    print("2) join   — configure this machine to connect to a pre-existing hub")
    choice = ""
    while choice not in ("1", "2", "fresh", "join"):
        choice = prompt("Choose deployment mode (1/fresh or 2/join)", "1").strip().lower()
    if choice in ("1", "fresh"):
        fresh_hub()
    else:
        join_hub()


if __name__ == "__main__":
    main()
