# Option B: Dynamic Page Server Implementation

You're right — I was conflating two things. Let me restate the architecture correctly:

**A guild is a single entity that is BOTH:**
1. An **LXMF propagation node** (store-and-forward for channel messages)
2. A **NomadNet page server** (the Micron frontend where users browse channels)

Channels are discovered by browsing a guild's Micron page. The page server dynamically renders channel views from the SQLite store of received LXMF messages.

The cleanest way to achieve true dynamism in NomadNet is to use its **executable page mechanism**: Python scripts placed in the pages directory that receive request context via environment variables and emit Micron to stdout.

## File Structure

```
reticord_storage/
├── identity                    # Local Reticulum identity
├── reticord.db                 # SQLite store
├── images/                     # Received images
├── lxmf/                       # LXMF router storage
└── pages/                      # Symlinked into ~/.nomadnetwork/storage/pages/reticord/
    ├── index.py                # Guild landing page (dynamic)
    ├── channel.py              # Channel view (dynamic)
    ├── settings.py             # User settings form
    ├── update.py               # Message submission handler
    └── save_settings.py        # Settings save handler
```

## The Implementation (`reticord.py`)

```python
#!/usr/bin/env python3
import RNS
import LXMF
import sqlite3
import time
import os
import hashlib
import stat
import threading
import shutil

# ==========================================
# 1. PATHS & DATABASE
# ==========================================
STORAGE_PATH = os.path.abspath("reticord_storage")
IMAGES_PATH = os.path.join(STORAGE_PATH, "images")
PAGES_PATH = os.path.join(STORAGE_PATH, "pages")
DB_PATH = os.path.join(STORAGE_PATH, "reticord.db")

os.makedirs(IMAGES_PATH, exist_ok=True)
os.makedirs(PAGES_PATH, exist_ok=True)

# Link our pages dir into NomadNet's pages directory so it serves them
NOMADNET_PAGES = os.path.expanduser("~/.nomadnetwork/storage/pages")
os.makedirs(NOMADNET_PAGES, exist_ok=True)
RETICORD_LINK = os.path.join(NOMADNET_PAGES, "reticord")
if os.path.islink(RETICORD_LINK):
    os.unlink(RETICORD_LINK)
if not os.path.exists(RETICORD_LINK):
    os.symlink(PAGES_PATH, RETICORD_LINK)

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS guilds 
                 (id TEXT PRIMARY KEY, name TEXT, prop_dest_hash TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels 
                 (addr TEXT PRIMARY KEY, name TEXT, guild_id TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_addr TEXT, 
                  timestamp REAL, sender_hash TEXT, content TEXT, image_file TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (hash TEXT PRIMARY KEY, display_name TEXT, node_address TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS local_settings 
                 (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    return conn

db_conn = init_db()

def get_setting(key, default=""):
    c = db_conn.cursor()
    c.execute("SELECT value FROM local_settings WHERE key=?", (key,))
    res = c.fetchone()
    return res[0] if res else default

def set_setting(key, value):
    c = db_conn.cursor()
    c.execute("INSERT OR REPLACE INTO local_settings (key, value) VALUES (?, ?)", (key, value))
    db_conn.commit()

# ==========================================
# 2. RETICULUM & LXMF SETUP
# ==========================================
RNS.log("Initializing Reticulum...")
RNS.initialize()

identity_path = os.path.join(STORAGE_PATH, "identity")
if os.path.exists(identity_path):
    identity = RNS.Identity.from_file(identity_path)
else:
    identity = RNS.Identity()
    identity.to_file(identity_path)

# The guild's propagation destination — this is the "guild" itself
guild_id = "main_guild"
guild_name = "Reticord Hub"
guild_dest = RNS.Destination(
    identity, RNS.Destination.IN, RNS.Destination.SINGLE,
    "lxmf", "propagation", "guild", guild_id
)
guild_dest_hash = RNS.hexrep(guild_dest.hash, delimit=False)

# Register as propagation node (store-and-forward for channel messages)
router = LXMF.LXMRouter(storagepath=os.path.join(STORAGE_PATH, "lxmf"))
local_user = router.register_delivery_identity(identity, display_name=guild_name)
router.register_propagation_node(guild_dest)

# Announce the guild so the mesh can discover it
RNS.Transport.announce(guild_dest)
RNS.log(f"Guild announced. Destination: {guild_dest_hash}")

# Local user identity (separate from guild, for sending messages)
user_identity_path = os.path.join(STORAGE_PATH, "user_identity")
if os.path.exists(user_identity_path):
    user_identity = RNS.Identity.from_file(user_identity_path)
else:
    user_identity = RNS.Identity()
    user_identity.to_file(user_identity_path)
user_lxm_identity = router.register_delivery_identity(user_identity, display_name="Reticord User")
local_hash = RNS.hexrep(user_lxm_identity.hash, delimit=False)

# Default local settings
local_display_name = get_setting("display_name", "Anon")
local_node_address = get_setting("node_address", "")

c = db_conn.cursor()
c.execute("INSERT OR REPLACE INTO users (hash, display_name, node_address) VALUES (?, ?, ?)",
          (local_hash, local_display_name, local_node_address))
c.execute("INSERT OR IGNORE INTO guilds (id, name, prop_dest_hash) VALUES (?, ?, ?)",
          (guild_id, guild_name, guild_dest_hash))
db_conn.commit()

# Channel destination registry
channel_destinations = {}

def ensure_channel(name):
    """A channel is a unique LXMF delivery destination derived from its name."""
    addr_hash = RNS.hexrep(RNS.Identity.full_hash(f"{guild_id}:{name}".encode()), delimit=False)
    c = db_conn.cursor()
    c.execute("SELECT addr FROM channels WHERE addr=?", (addr_hash,))
    if not c.fetchone():
        c.execute("INSERT INTO channels (addr, name, guild_id) VALUES (?, ?, ?)",
                  (addr_hash, name, guild_id))
        db_conn.commit()
    if addr_hash not in channel_destinations:
        chan_dest = RNS.Destination(
            identity, RNS.Destination.IN, RNS.Destination.SINGLE,
            "lxmf", "delivery", "channel", addr_hash
        )
        channel_destinations[addr_hash] = chan_dest
    return addr_hash

general_addr = ensure_channel("general")
images_addr = ensure_channel("images")

def handle_incoming_lxmf(message, channel_addr):
    """Store a received LXMF message in the guild's SQLite ledger."""
    c = db_conn.cursor()
    sender_hash = RNS.hexrep(message.source_hash, delimit=False)
    content = message.content.decode('utf-8') if message.content else ""
    
    display_name = ""
    node_address = ""
    if hasattr(message, 'fields'):
        display_name = message.fields.get('display_name', b'').decode('utf-8', 'ignore')
        node_address = message.fields.get('node_address', b'').decode('utf-8', 'ignore')
    if display_name:
        c.execute("INSERT OR REPLACE INTO users (hash, display_name, node_address) VALUES (?, ?, ?)",
                  (sender_hash, display_name, node_address))
    
    image_file = None
    if hasattr(message, 'fields') and 'image' in message.fields:
        img_data = message.fields['image']
        filename = f"{int(time.time())}_{hashlib.md5(img_data).hexdigest()}.png"
        with open(os.path.join(IMAGES_PATH, filename), 'wb') as f:
            f.write(img_data)
        image_file = filename
    
    c.execute("INSERT INTO messages (channel_addr, timestamp, sender_hash, content, image_file) VALUES (?, ?, ?, ?, ?)",
              (channel_addr, time.time(), sender_hash, content, image_file))
    db_conn.commit()

# Route inbound LXMF: channel messages go to our store, private messages fall through to standard handling
original_handle_inbound = router.handle_inbound
def custom_handle_inbound(message):
    dest = message.destination
    aspects = dest.aspects
    if len(aspects) >= 4 and aspects[0] == "lxmf" and aspects[1] == "delivery" and aspects[2] == "channel":
        channel_addr = aspects[3]
        c = db_conn.cursor()
        c.execute("SELECT addr FROM channels WHERE addr=?", (channel_addr,))
        if c.fetchone():
            handle_incoming_lxmf(message, channel_addr)
            return True
    return original_handle_inbound(message)

router.handle_inbound = custom_handle_inbound

def send_channel_message(channel_addr, content, image_data=None):
    """Send an LXMF message to a channel destination on this guild."""
    if channel_addr not in channel_destinations:
        return False
    dest = channel_destinations[channel_addr]
    
    img_file = None
    if image_data:
        filename = f"{int(time.time())}_{hashlib.md5(image_data).hexdigest()}.png"
        with open(os.path.join(IMAGES_PATH, filename), 'wb') as f:
            f.write(image_data)
        img_file = filename
    
    c = db_conn.cursor()
    c.execute("INSERT INTO messages (channel_addr, timestamp, sender_hash, content, image_file) VALUES (?, ?, ?, ?, ?)",
              (channel_addr, time.time(), local_hash, content, img_file))
    db_conn.commit()
    
    lxm = LXMF.LXMessage(dest, user_lxm_identity, content.encode('utf-8'), title="Channel Message")
    lxm.fields['display_name'] = local_display_name.encode('utf-8')
    lxm.fields['node_address'] = local_node_address.encode('utf-8')
    if image_data:
        lxm.fields['image'] = image_data
    router.handle_outbound(lxm)
    return True

# ==========================================
# 3. DYNAMIC PAGE SCRIPTS (NomadNet executable pages)
# ==========================================
# NomadNet executes these scripts when requested. They receive:
#   REMOTE_PEER       - hex hash of requesting peer
#   REQUEST_PATH      - e.g. /reticord/channel.py
#   QUERY_STRING      - form data
#   REQUEST_METHOD    - GET/POST
# They emit Micron markup to stdout.

PYTHON_SHEBANG = "#!/usr/bin/env python3\n"
EXEC_MODE = 0o755

def write_script(filename, content):
    path = os.path.join(PAGES_PATH, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(PYTHON_SHEBANG + content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

# ---------- index.py : Guild landing page with channel list ----------
write_script("index.py", r'''
import os, sqlite3, urllib.parse

db_path = os.environ.get("RETICORD_DB")
guild_dest = os.environ.get("RETICORD_GUILD_DEST")
guild_name = os.environ.get("RETICORD_GUILD_NAME", "Guild")
query = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
active = query.get("channel", [None])[0]

conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("SELECT addr, name FROM channels ORDER BY name ASC")
channels = c.fetchall()

print("#!fg=ffffff")
print("#!bg=2f3136")
print("")
print(f"> {guild_name}")
print(f"r[Settings](/reticord/settings.py)")
print(f"r[My Node: {guild_dest[:16]}...]()")
print("")
print("c!bold!F=58fChannels!f")
for addr, name in channels:
    if addr == active:
        print(f"l!bold!F=58f> #{name}!f")
    else:
        print(f"l[{name}](/reticord/index.py?channel={addr})")
print("")
print("-")
if active:
    # Micron partial: asynchronously loads channel view (the "in-frame")
    print(f"{{/reticord/channel.py?channel={active}}}")
else:
    print("cSelect a channel.")
conn.close()
''')

# ---------- channel.py : Renders messages for a channel ----------
write_script("channel.py", r'''
import os, sqlite3, time, urllib.parse

db_path = os.environ.get("RETICORD_DB")
images_dir = os.environ.get("RETICORD_IMAGES")
query = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
channel_addr = query.get("channel", [None])[0]

if not channel_addr:
    print("c!F=f00No channel specified.!f")
    exit(0)

conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("SELECT name FROM channels WHERE addr=?", (channel_addr,))
res = c.fetchone()
chan_name = res[0] if res else "unknown"

print(f"c!bold!F=58f#{chan_name}!f")
print("")

c.execute("""
    SELECT m.sender_hash, m.timestamp, m.content, m.image_file,
           u.display_name, u.node_address
    FROM messages m LEFT JOIN users u ON m.sender_hash = u.hash
    WHERE m.channel_addr=? ORDER BY m.timestamp ASC
""", (channel_addr,))
for sender_hash, ts, content, img_file, display_name, node_address in c.fetchall():
    d_name = display_name if display_name else sender_hash[:8]
    # Clickable username links to user's self-hosted profile node
    if node_address and len(node_address) >= 32:
        user_link = f"[{d_name}]({node_address}:/profile.mu)"
    else:
        user_link = d_name
    time_str = time.strftime('%H:%M', time.localtime(ts))
    print(f"l{user_link} !italic!{time_str}!i")
    if content:
        print(f"l{content}")
    if img_file:
        # NomadNet 1.4 whole-line image syntax
        print(f"(/reticord/images/{img_file}|w=100%|h=auto|a=left|s=scale|k=keep)")
    print("")

print("-")
print("lMessage:")
print("<user_input>")
print(f"[Send](/reticord/update.py?channel={channel_addr}&content=<user_input>)")
conn.close()
''')

# ---------- update.py : Receives message submission ----------
write_script("update.py", r'''
import os, sqlite3, urllib.parse, time, hashlib, RNS, LXMF

db_path = os.environ.get("RETICORD_DB")
images_dir = os.environ.get("RETICORD_IMAGES")
storage_path = os.environ.get("RETICORD_STORAGE")
query = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
channel_addr = query.get("channel", [None])[0]
content = query.get("content", [""])[0]

if channel_addr and content:
    # Local echo: write directly to DB
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    # Get local user hash from settings
    c.execute("SELECT value FROM local_settings WHERE key='local_hash'")
    res = c.fetchone()
    local_hash = res[0] if res else "unknown"
    c.execute("INSERT INTO messages (channel_addr, timestamp, sender_hash, content, image_file) VALUES (?, ?, ?, ?, ?)",
              (channel_addr, time.time(), local_hash, content, None))
    conn.commit()
    conn.close()
    
    # Also dispatch via LXMF to the propagation node (ourselves + peers)
    try:
        RNS.initialize()
        router = LXMF.LXMRouter(storagepath=os.path.join(storage_path, "lxmf"))
        user_identity = RNS.Identity.from_file(os.path.join(storage_path, "user_identity"))
        user_lxm = router.register_delivery_identity(user_identity)
        
        # Reconstruct channel destination
        chan_dest = RNS.Destination(
            user_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
            "lxmf", "delivery", "channel", channel_addr
        )
        lxm = LXMF.LXMessage(chan_dest, user_lxm, content.encode('utf-8'), title="Channel Message")
        # Attach identity fields
        c2 = sqlite3.connect(db_path).cursor()
        c2.execute("SELECT value FROM local_settings WHERE key='display_name'")
        dn = c2.fetchone()
        c2.execute("SELECT value FROM local_settings WHERE key='node_address'")
        na = c2.fetchone()
        if dn: lxm.fields['display_name'] = dn[0].encode('utf-8')
        if na: lxm.fields['node_address'] = na[0].encode('utf-8')
        router.handle_outbound(lxm)
    except Exception as e:
        pass  # Local echo already succeeded

# Redirect back to channel view
print(f"!/reticord/index.py?channel={channel_addr}")
''')

# ---------- settings.py : User settings form ----------
write_script("settings.py", r'''
import os, sqlite3, urllib.parse

db_path = os.environ.get("RETICORD_DB")
conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("SELECT value FROM local_settings WHERE key='display_name'")
dn = c.fetchone()
display_name = dn[0] if dn else "Anon"
c.execute("SELECT value FROM local_settings WHERE key='node_address'")
na = c.fetchone()
node_address = na[0] if na else ""
conn.close()

print("> Settings")
print("")
print("lYour Display Name:")
print(f"<display_name>{display_name}")
print("")
print("lYour Profile Node Destination Hash:")
print("l(32-char hex hash of your personal NomadNet node)")
print(f"<node_address>{node_address}")
print("")
print("[Save](/reticord/save_settings.py?name=<display_name>&node=<node_address>)")
print("")
print("[Back to Guild](/reticord/index.py)")
''')

# ---------- save_settings.py : Persist settings ----------
write_script("save_settings.py", r'''
import os, sqlite3, urllib.parse

db_path = os.environ.get("RETICORD_DB")
query = urllib.parse.parse_qs(os.environ.get("QUERY_STRING", ""))
name = query.get("name", ["Anon"])[0]
node = query.get("node", [""])[0]

conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("INSERT OR REPLACE INTO local_settings (key, value) VALUES (?, ?)", ("display_name", name))
c.execute("INSERT OR REPLACE INTO local_settings (key, value) VALUES (?, ?)", ("node_address", node))
conn.commit()
conn.close()

print("> Saved")
print("")
print(f"lDisplay name: !bold!{name}!f")
print(f"lNode hash: {node[:32] if node else '(none)'}...")
print("")
print("[Back to Guild](/reticord/index.py)")
''')

# Inject environment variables into each script so they can find the DB
ENV_INJECTION = f'''
import os
os.environ.setdefault("RETICORD_DB", {repr(DB_PATH)})
os.environ.setdefault("RETICORD_IMAGES", {repr(IMAGES_PATH)})
os.environ.setdefault("RETICORD_STORAGE", {repr(STORAGE_PATH)})
os.environ.setdefault("RETICORD_GUILD_DEST", {repr(guild_dest_hash)})
os.environ.setdefault("RETICORD_GUILD_NAME", {repr(guild_name)})
'''

for fname in ["index.py", "channel.py", "update.py", "settings.py", "save_settings.py"]:
    path = os.path.join(PAGES_PATH, fname)
    with open(path, 'r') as f:
        content = f.read()
    # Insert env injection after shebang
    lines = content.split('\n', 1)
    new_content = lines[0] + '\n' + ENV_INJECTION + (lines[1] if len(lines) > 1 else '')
    with open(path, 'w') as f:
        f.write(new_content)

# Persist local_hash so update.py can read it
set_setting("local_hash", local_hash)

# ==========================================
# 4. IMAGE SERVING
# ==========================================
# NomadNet can serve files from a directory. We symlink images into the pages dir
IMAGES_LINK = os.path.join(PAGES_PATH, "images")
if os.path.islink(IMAGES_LINK):
    os.unlink(IMAGES_LINK)
if not os.path.exists(IMAGES_LINK):
    os.symlink(IMAGES_PATH, IMAGES_LINK)

# ==========================================
# 5. MAIN LOOP
# ==========================================
def run_node():
    RNS.log(f"Reticord Guild Node Active.")
    RNS.log(f"Guild destination: {guild_dest_hash}")
    RNS.log(f"Browse in NomadNet: <{guild_dest_hash}> or /reticord/index.py")
    RNS.log(f"Channels registered: general={general_addr}, images={images_addr}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        RNS.log("Shutting down...")
        db_conn.close()

if __name__ == "__main__":
    run_node()
```

## How the Architecture Now Maps to Your Spec

| Spec Requirement | Implementation |
|---|---|
| **Guilds are LXMF store-and-propagate nodes** | `router.register_propagation_node(guild_dest)` — the node stores and forwards channel messages |
| **Guilds also display messages per channel** | Same node runs NomadNet executable page scripts that query the SQLite store |
| **Channels are unique LXMF addresses** | Each channel is a distinct `lxmf.delivery.channel.<hash>` destination |
| **Channels have redefinable names** | Name ↔ hash mapping lives in `channels` table |
| **Chats are Micron pages keyed by time received** | `channel.py` queries messages `ORDER BY timestamp ASC` |
| **Private messages are standard LXMF** | `custom_handle_inbound` falls through to `original_handle_inbound` for non-channel destinations |
| **Guild Micron page allows channel selection** | `index.py` lists channels; clicking reloads with `?channel=` |
| **Viewing utilizes in-frames** | `{/reticord/channel.py?channel=...}` is a Micron partial — NomadNet fetches only that block asynchronously |
| **Zero login** | Identity comes from local Reticulum identity file |
| **Usernames displayed as Discord does** | `channel.py` renders `[Name](node_hash:/profile.mu)` — clickable to self-hosted profile |
| **Clicking user opens their profile on their self-hosted node** | Link uses destination hash; NomadNet resolves it via Reticulum's cryptographic routing |
| **Python based** | Entire stack is Python using `rns`, `lxmf`, `sqlite3`, and NomadNet's executable page protocol |

## How to Use

1. `pip install rns lxmf` (ensure `rnsd` is running or let the script initialize)
2. `python reticord.py`
3. Open **NomadNet** → browse to `/reticord/index.py` (or your guild's destination hash)
4. Click a channel → partial loads the chat asynchronously
5. Type a message → `update.py` stores it locally AND dispatches it via LXMF to the propagation node (which then propagates to peers)
6. Set your display name and profile node hash in `/reticord/settings.py`

The guild node is now a proper dual-role entity: it's both the message bus (LXMF propagation) and the UI (dynamic Micron page server). Other users discover channels by browsing the guild's page, and the page is always live because NomadNet executes the Python scripts on every request.[H]
