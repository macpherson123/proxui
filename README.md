works

# proxui — a fast alternate UI for Proxmox VE

**proxui** is a single-file, self-hosted alternate web interface for [Proxmox VE](https://www.proxmox.com/).
It runs *on* the Proxmox host as a tiny Python service (standard library only — no pip installs),
serves one HTML page, and transparently forwards the Proxmox API and the noVNC/xterm.js
console over the same origin. On top of the stock API it adds host-level helpers the native
UI doesn't expose: block-device/SMART detail, and full **Wi-Fi + hotspot management** via
NetworkManager — including turning the host into an access point that shares its uplink.

> One file to run (`px-proxy.py`), one file to serve (`proxmox-ui.html`). No Node, no build step, no database.

## Features

- **Dashboard** — node CPU/RAM/disk gauges, running/total VM & LXC counts, one resource table.
- **VMs & LXC** — start/stop/shutdown, right-click actions, live status.
- **In-browser console** — noVNC (KVM) and xterm.js (LXC/shell) relayed through the same port, so cookies just work.
- **Storage / Disks / Network / Nodes** — datastore usage + volumes, physical disks with SMART, host interfaces, cluster nodes.
- **Wi-Fi client** — scan and join networks on the host through `nmcli`.
- **Hotspot / Access Point** — create a WPA2 AP in a couple of clicks. NAT-shares the host's
  uplink so clients get **internet plus access to the Proxmox subnet and VM bridges**, and it
  **auto-starts on boot**. A live **Connected Devices** list shows hostname, IP, MAC, signal and uptime.
- **Task history**, **console shortcut** `.desktop` launchers, and a **Settings** panel (accent colour, auto-refresh, timeouts).
- **Zero external JS/CSS deps** beyond Google Fonts; ~1 MB of static HTML.

## Requirements

- A **Proxmox VE** host (tested on PVE 7.x/8.x) with `pveproxy` running on `:8006`.
- `python3` (3.9+) — already present on Proxmox.
- For Wi-Fi/hotspot features (all optional): **NetworkManager** (`nmcli`), `iw`, and `dnsmasq-base`:
  ```bash
  apt install network-manager iw dnsmasq-base
  ```
  and a wireless adapter that supports AP mode.

## Install

Run as **root on the Proxmox host**:

```bash
git clone https://github.com/macpherson123/proxui.git
cd proxui
sudo ./install.sh
```

The installer copies the two files to `/opt/proxui`, installs a systemd unit, and enables it on boot.
Then open:

```
https://<host-ip>:8090/
```

Your browser will warn once about Proxmox's self-signed certificate — accept it (it's the same
cert the native UI on `:8006` uses). Log in with your normal Proxmox credentials
(e.g. `root@pam`).

### Manual run (no service)

```bash
python3 px-proxy.py      # serves https://0.0.0.0:8090/
```

### Updating

Pull the latest and re-run the installer — it copies the new files and restarts the service:

```bash
git pull && sudo ./install.sh
```

### Uninstall

```bash
systemctl disable --now proxui
rm -rf /opt/proxui /etc/systemd/system/proxui.service
systemctl daemon-reload
```

## Setting up the always-on hotspot

Open the **Wi-Fi** tab → **Hotspot** panel, enter a name and password (≥ 8 chars), and hit
**Start / Update**. The AP is created with `nmcli` as a shared/NAT connection
(`192.168.99.1/24` by default), runs its own DHCP/DNS via NetworkManager's dnsmasq, and is set
to `autoconnect` so it returns after a reboot. Connected clients are NAT'd out through the
host's uplink and can reach the Proxmox subnet and VM bridges. The **Connected Devices** list
updates whenever you open the tab.

## Configuration

Edit the constants at the top of `px-proxy.py`:

| Setting | Default | Purpose |
|---|---|---|
| `PORT` | `8090` | Port proxui listens on |
| `TARGET` | `https://127.0.0.1:8006` | Local pveproxy to forward to |
| `USE_TLS` | `True` | Serve HTTPS using Proxmox's cert (falls back to HTTP if missing) |
| `ALLOW_HOST_EXEC` | `True` | Allow the `/x/exec` host-shell helper |

## Security notes

proxui is meant for a **trusted management LAN**. It forwards Proxmox authentication as-is
(no accounts of its own) and, with `ALLOW_HOST_EXEC=True`, exposes a root-shell helper to
authenticated users. Don't expose `:8090` to the internet; put it behind a VPN/Tailscale or
firewall it to your admin network. Set `ALLOW_HOST_EXEC=False` to disable the host-shell path.

## How it works

```
browser ──https──▶ px-proxy.py (:8090) ──https──▶ pveproxy (:8006)
                        │
                        ├── serves proxmox-ui.html at /
                        ├── relays /api2/... (REST + console WebSocket)
                        └── adds /x/... host helpers (disks, wifi, hotspot, exec)
```

Because the page and the API share one origin, the browser handles the `PVEAuthCookie`
normally — no CORS or certificate workarounds in the client.

## License

MIT — see [LICENSE](LICENSE).
