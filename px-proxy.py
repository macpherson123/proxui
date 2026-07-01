#!/usr/bin/env python3
"""
Proxmox alternate-UI server.

Run this ON the Proxmox host. It:
  * serves proxmox-ui.html at /
  * forwards /api2/... (and other pveproxy paths) to the local pveproxy
  * relays the noVNC/xterm.js WebSocket so the in-app console works
  * exposes host-level helpers under /x/ that the API doesn't cover:
      /x/disks         block-device detail (lsblk)
      /x/wifi/scan     wifi scan          (nmcli)
      /x/wifi/status   wifi adapter state (nmcli)
      /x/wifi/connect  join a network     (nmcli)  [POST]
      /x/wifi/disconnect  drop the wifi client link (nmcli) [POST]
      /x/wifi/hotspot         create/start an AP    (nmcli) [POST]
      /x/wifi/hotspot/status  active AP details     (nmcli)
      /x/wifi/hotspot/clients connected devices     (iw/leases/neigh)
      /x/wifi/hotspot/stop    stop the AP           (nmcli) [POST]
      /x/exec          run a command in a VM (qm guest exec), an LXC
                       (pct exec) or on the host (sh -c)            [POST]
      /x/shortcut      download a .desktop launcher for a console

Because the page and the API share one origin, the browser handles auth
cookies normally — no CORS or cert workarounds needed in the browser.

Install as a service:   sudo ./install.sh        (see install.sh / proxui.service)
Manual run:             python3 px-proxy.py
"""
import http.server, socketserver, urllib.request, urllib.parse, urllib.error
import ssl, os, sys, json, socket, select, subprocess, shutil, re

# ── configuration ──────────────────────────────────────────────────────────
TARGET   = 'https://127.0.0.1:8006'      # local pveproxy on this host
PORT     = 8090
USE_TLS  = True                          # serve over https using Proxmox's cert
TLS_CERT = '/etc/pve/local/pve-ssl.pem'  # falls back to plain http if missing
TLS_KEY  = '/etc/pve/local/pve-ssl.key'
ALLOW_HOST_EXEC = True                   # allow /x/exec kind=host (root shell on the box)
EXEC_TIMEOUT    = 30                      # default seconds for /x/exec
# ─────────────────────────────────────────────────────────────────────────────

HERE     = os.path.dirname(os.path.abspath(__file__))
UI_FILE  = os.path.join(HERE, 'proxmox-ui.html')
SCHEME   = 'http'                         # set to https at startup if TLS is on

_up      = urllib.parse.urlparse(TARGET)
UP_HOST  = _up.hostname
UP_PORT  = _up.port or 443


def _sslctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'proxui/3.0'

    # ── dispatch ──
    def do_GET(self):
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            return self.relay_websocket()
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path.startswith('/x/'):
            return self.handle_ext('GET')
        if path in ('/', '/index.html', '/ui') and 'console' not in query:
            return self.serve_ui()
        self.proxy()

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path.startswith('/x/'):
            return self.handle_ext('POST')
        self.proxy()

    def do_PUT(self):    self.proxy()
    def do_DELETE(self): self.proxy()
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Content-Length', '0')
        self.end_headers()

    # ── static UI ──
    def serve_ui(self):
        try:
            with open(UI_FILE, 'rb') as f:
                data = f.read()
        except OSError as e:
            return self.send_error(500, f'Cannot read UI file: {e}')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── generic HTTP proxy to pveproxy ──
    def proxy(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(TARGET + self.path, data=body, method=self.command)
        skip = {'host', 'content-length', 'connection', 'x-pve-auth'}
        for h, v in self.headers.items():
            if h.lower() not in skip:
                req.add_header(h, v)
        ticket = self.headers.get('X-PVE-Auth')
        if ticket:
            req.add_header('Cookie', f'PVEAuthCookie={ticket}')
        try:
            r = urllib.request.urlopen(req, context=_sslctx())
            self._relay_response(r.status, r.headers, r.read())
        except urllib.error.HTTPError as e:
            self._relay_response(e.code, e.headers, e.read())
        except urllib.error.URLError as e:
            self.send_error(502, f'Upstream error: {e.reason}')

    def _relay_response(self, status, headers, body):
        self.send_response(status)
        for h, v in headers.items():
            if h.lower() not in ('transfer-encoding', 'connection', 'content-length'):
                self.send_header(h, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── WebSocket relay (noVNC / xterm.js console) ──
    def relay_websocket(self):
        try:
            raw = socket.create_connection((UP_HOST, UP_PORT))
            upstream = _sslctx().wrap_socket(raw, server_hostname=UP_HOST)
        except OSError as e:
            return self.send_error(502, f'ws upstream: {e}')

        lines = [f'GET {self.path} HTTP/1.1', f'Host: {UP_HOST}:{UP_PORT}']
        for h, v in self.headers.items():
            lh = h.lower()
            if lh == 'host':
                continue
            if lh == 'x-pve-auth':
                lines.append(f'Cookie: PVEAuthCookie={v}')
                continue
            lines.append(f'{h}: {v}')
        upstream.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())

        client = self.connection
        client.setblocking(False)
        upstream.setblocking(False)
        socks = [client, upstream]
        try:
            while True:
                r, _, x = select.select(socks, [], socks, 120)
                if x:
                    break
                if not r:
                    continue
                for s in r:
                    if not self._pump(s, upstream if s is client else client):
                        return
        finally:
            try: upstream.close()
            except OSError: pass

    @staticmethod
    def _pump(src, dst):
        # Move all currently-available bytes from src to dst. Returns False on close.
        try:
            data = src.recv(65536)
        except (ssl.SSLWantReadError, BlockingIOError):
            return True
        except OSError:
            return False
        if not data:
            return False
        try:
            dst.sendall(data)
            # SSL buffers data the kernel select() can't see — drain it.
            pending = getattr(src, 'pending', None)
            if pending:
                while src.pending():
                    more = src.recv(65536)
                    if not more:
                        return False
                    dst.sendall(more)
        except OSError:
            return False
        return True

    # ── host-level helpers under /x/ ──
    def handle_ext(self, method):
        parsed = urllib.parse.urlparse(self.path)
        route, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if route == '/x/ping':
                return self.send_json({'ok': True, 'host': socket.gethostname(), 'scheme': SCHEME,
                                       'host_exec': ALLOW_HOST_EXEC})
            if route == '/x/disks':
                return self.send_json(self.ext_disks())
            if route == '/x/wifi/status':
                return self.send_json(self.ext_wifi_status())
            if route == '/x/wifi/scan':
                return self.send_json(self.ext_wifi_scan())
            if route == '/x/wifi/connect' and method == 'POST':
                return self.send_json(self.ext_wifi_connect())
            if route == '/x/wifi/disconnect' and method == 'POST':
                return self.send_json(self.ext_wifi_disconnect())
            if route == '/x/wifi/hotspot' and method == 'POST':
                return self.send_json(self.ext_wifi_hotspot())
            if route == '/x/wifi/hotspot/status':
                return self.send_json(self.ext_wifi_hotspot_status())
            if route == '/x/wifi/hotspot/clients':
                return self.send_json(self.ext_wifi_hotspot_clients())
            if route == '/x/wifi/hotspot/stop' and method == 'POST':
                return self.send_json(self.ext_wifi_hotspot_stop())
            if route == '/x/exec' and method == 'POST':
                return self.send_json(self.ext_exec())
            if route == '/x/shortcut':
                return self.ext_shortcut(qs)
        except Exception as e:  # noqa: BLE001 - surface helper failures as JSON
            return self.send_json({'error': str(e)}, status=500)
        self.send_json({'error': 'unknown endpoint'}, status=404)

    def send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, TypeError):
            return {}

    def ext_disks(self):
        if not shutil.which('lsblk'):
            return {'error': 'lsblk not found'}
        cols = 'NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,FSUSED,FSAVAIL,MOUNTPOINT,MODEL,SERIAL,VENDOR,TRAN,ROTA,STATE,LABEL'
        out = subprocess.run(['lsblk', '-J', '-b', '-o', cols], capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return {'error': out.stderr.strip() or 'lsblk failed'}
        return json.loads(out.stdout)

    def _wifi_dev(self):
        try:
            out = subprocess.run(['nmcli', '-t', '-f', 'DEVICE,TYPE', 'device'],
                                 capture_output=True, text=True, timeout=10)
            for line in out.stdout.splitlines():
                dev, _, typ = line.partition(':')
                if typ == 'wifi':
                    return dev
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    def ext_wifi_status(self):
        if not shutil.which('nmcli'):
            return {'available': False, 'error': 'nmcli not installed on host'}
        out = subprocess.run(['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION', 'device'],
                             capture_output=True, text=True, timeout=10)
        devices = []
        for line in out.stdout.splitlines():
            p = _nmcli_split(line)
            if len(p) >= 4 and p[1] == 'wifi':
                devices.append({'device': p[0], 'state': p[2], 'connection': p[3]})
        return {'available': True, 'wifi': devices}

    def ext_wifi_scan(self):
        if not shutil.which('nmcli'):
            return {'available': False, 'error': 'nmcli not installed on host'}
        out = subprocess.run(
            ['nmcli', '-t', '-f', 'IN-USE,SSID,SIGNAL,SECURITY,FREQ', 'device', 'wifi', 'list', '--rescan', 'yes'],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return {'available': True, 'error': out.stderr.strip() or 'scan failed', 'networks': []}
        nets, seen = [], set()
        for line in out.stdout.splitlines():
            p = _nmcli_split(line)
            if len(p) < 4 or not p[1] or p[1] in seen:
                continue
            seen.add(p[1])
            nets.append({'active': p[0] == '*', 'ssid': p[1],
                         'signal': int(p[2]) if p[2].isdigit() else 0,
                         'security': p[3] or 'open', 'freq': p[4] if len(p) > 4 else ''})
        nets.sort(key=lambda n: n['signal'], reverse=True)
        return {'available': True, 'networks': nets}

    def ext_wifi_connect(self):
        if not shutil.which('nmcli'):
            return {'ok': False, 'error': 'nmcli not installed on host'}
        body = self._read_json_body()
        ssid = (body.get('ssid') or '').strip()
        psk = body.get('password') or ''
        if not ssid:
            return {'ok': False, 'error': 'ssid required'}
        dev = self._wifi_dev()
        if not dev:
            return {'ok': False, 'error': 'no wifi adapter found'}

        # If a saved connection for this SSID already exists, update the password
        # and activate it. This avoids "Connection already exists" failures and
        # lets the user re-enter a corrected password.
        con_name = self._nm_conn_for_ssid(ssid)
        if con_name:
            if psk:
                subprocess.run(['nmcli', 'connection', 'modify', con_name,
                                '802-11-wireless-security.psk', psk],
                               capture_output=True, text=True, timeout=15)
            out = subprocess.run(['nmcli', 'connection', 'up', con_name],
                                 capture_output=True, text=True, timeout=45)
            if out.returncode == 0:
                return {'ok': True, 'message': f'Connected to {ssid}'}
            # Fall through to a fresh device connect if activating the saved
            # profile failed (e.g. password still wrong, hidden SSID, etc.).

        cmd = ['nmcli', 'device', 'wifi', 'connect', ssid]
        if psk:
            cmd += ['password', psk]
        cmd += ['ifname', dev]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {'ok': out.returncode == 0, 'message': (out.stdout or out.stderr).strip()}

    def _nm_conn_for_ssid(self, ssid):
        """Return the connection profile name for a given SSID, if one exists."""
        try:
            out = subprocess.run(['nmcli', '-t', '-f', 'NAME,TYPE,802-11-WIRELESS.SSID',
                                  'connection', 'show'],
                                 capture_output=True, text=True, timeout=10)
            for line in out.stdout.splitlines():
                parts = _nmcli_split(line)
                if len(parts) >= 3 and parts[1] == '802-11-wireless' and parts[2] == ssid:
                    return parts[0]
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    def ext_wifi_hotspot(self):
        if not shutil.which('nmcli'):
            return {'ok': False, 'error': 'nmcli not installed on host'}
        body = self._read_json_body()
        ssid = (body.get('ssid') or '').strip()
        psk = body.get('password') or ''
        band = (body.get('band') or 'bg').strip().lower()
        # Gateway/host address on the AP subnet. NM's shared mode runs dnsmasq
        # (DHCP+DNS) and NAT masquerade bound to this address; without an explicit
        # address the profile can come up carrier-only with no gateway for clients.
        gateway = (body.get('gateway') or '192.168.99.1/24').strip()
        try:
            channel = int(body.get('channel') or 0)
        except (TypeError, ValueError):
            channel = 0
        if not ssid:
            return {'ok': False, 'error': 'ssid required'}
        if len(psk) < 8:
            return {'ok': False, 'error': 'password must be at least 8 characters'}
        dev = self._wifi_dev()
        if not dev:
            return {'ok': False, 'error': 'no wifi adapter found'}

        con_name = ssid
        # Remove any stale profile with the same name so we can recreate it cleanly.
        subprocess.run(['nmcli', 'connection', 'delete', con_name],
                       capture_output=True, text=True, timeout=15)

        cmd = ['nmcli', 'connection', 'add', 'type', 'wifi',
               'ifname', dev, 'con-name', con_name, 'autoconnect', 'yes',
               '802-11-wireless.mode', 'ap',
               '802-11-wireless.ssid', ssid,
               '802-11-wireless.band', band]
        # channel 0 means "auto" — nmcli rejects it, so only pin a real channel.
        if channel > 0:
            cmd += ['802-11-wireless.channel', str(channel)]
        cmd += ['802-11-wireless-security.key-mgmt', 'wpa-psk',
                '802-11-wireless-security.psk', psk,
                'ipv4.method', 'shared',
                'ipv4.addresses', gateway]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return {'ok': False, 'message': (out.stdout or out.stderr).strip()}

        up = subprocess.run(['nmcli', 'connection', 'up', con_name],
                            capture_output=True, text=True, timeout=30)
        return {'ok': up.returncode == 0,
                'message': (up.stdout or up.stderr).strip() or f'Hotspot {ssid} started'}

    def ext_wifi_hotspot_status(self):
        if not shutil.which('nmcli'):
            return {'available': False, 'error': 'nmcli not installed on host'}
        try:
            out = subprocess.run(['nmcli', '-t', '-f', 'NAME,DEVICE,TYPE,ACTIVE',
                                  'connection', 'show', '--active'],
                                 capture_output=True, text=True, timeout=10)
            for line in out.stdout.splitlines():
                parts = _nmcli_split(line)
                if len(parts) >= 4 and parts[2] == '802-11-wireless':
                    detail = subprocess.run(
                        ['nmcli', '-t', '-f', '802-11-wireless.ssid,IP4.ADDRESS',
                         'connection', 'show', parts[0]],
                        capture_output=True, text=True, timeout=10)
                    info = dict(p.split(':', 1) for p in _nmcli_split(detail.stdout) if ':' in p)
                    return {'active': True, 'name': parts[0], 'device': parts[1],
                            'ssid': info.get('802-11-wireless.ssid', parts[0]),
                            'ip4': info.get('IP4.ADDRESS', ''),
                            'mode': 'ap' if self._conn_is_ap(parts[0]) else 'sta'}
            return {'active': False}
        except (OSError, subprocess.SubprocessError) as e:
            return {'active': False, 'error': str(e)}

    def ext_wifi_hotspot_clients(self):
        """List devices connected to the AP: associated stations (iw) enriched
        with DHCP lease (IP/hostname) and neighbour-table reachability."""
        import glob, time
        dev = self._wifi_dev()
        # DHCP leases NM's dnsmasq writes, keyed by MAC.
        leases = {}
        for lf in glob.glob('/var/lib/NetworkManager/dnsmasq-*.leases'):
            try:
                with open(lf) as f:
                    for line in f:
                        p = line.split()
                        if len(p) >= 4:
                            leases[p[1].lower()] = {
                                'ip': p[2], 'hostname': '' if p[3] == '*' else p[3],
                                'expires': int(p[0]) if p[0].isdigit() else 0}
            except OSError:
                pass
        # Neighbour table gives IP + reachability for any MAC on the subnet.
        neigh = {}
        try:
            out = subprocess.run(['ip', 'neigh'], capture_output=True, text=True, timeout=10)
            for line in out.stdout.splitlines():
                m = re.match(r'(\S+).*\blladdr (\S+)\s+(\S+)', line)
                if m:
                    neigh[m.group(2).lower()] = {'ip': m.group(1), 'state': m.group(3)}
        except (OSError, subprocess.SubprocessError):
            pass
        # Associated wifi stations (authoritative "connected") + signal, if iw exists.
        stations = {}
        if dev and shutil.which('iw'):
            try:
                out = subprocess.run(['iw', 'dev', dev, 'station', 'dump'],
                                     capture_output=True, text=True, timeout=10)
                cur = None
                for line in out.stdout.splitlines():
                    ms = re.match(r'Station ([0-9a-fA-F:]{17})', line.strip())
                    if ms:
                        cur = ms.group(1).lower(); stations[cur] = {}
                    elif cur and 'signal:' in line and 'avg' not in line:
                        sm = re.search(r'signal:\s*(-?\d+)', line)
                        if sm: stations[cur]['signal'] = int(sm.group(1))
                    elif cur and 'connected time:' in line:
                        cm = re.search(r'connected time:\s*(\d+)', line)
                        if cm: stations[cur]['connected'] = int(cm.group(1))
            except (OSError, subprocess.SubprocessError):
                pass

        macs = set(stations) | set(leases) | {m for m, n in neigh.items()
                                              if n['ip'].startswith('192.168.99.')}
        online_states = ('REACHABLE', 'STALE', 'DELAY', 'PROBE')
        clients = []
        for mac in macs:
            lease, n, st = leases.get(mac, {}), neigh.get(mac, {}), stations.get(mac)
            clients.append({
                'mac': mac,
                'ip': lease.get('ip') or n.get('ip', ''),
                'hostname': lease.get('hostname', ''),
                'signal': st.get('signal') if st else None,
                'connected_s': st.get('connected') if st else None,
                'associated': st is not None,
                'online': st is not None or n.get('state', '') in online_states,
            })
        clients.sort(key=lambda c: [int(x) for x in c['ip'].split('.')] if c['ip'] else [999])
        return {'ok': True, 'count': len(clients), 'clients': clients}

    def _conn_is_ap(self, con_name):
        try:
            out = subprocess.run(['nmcli', '-t', '-f', '802-11-wireless.mode',
                                  'connection', 'show', con_name],
                                 capture_output=True, text=True, timeout=5)
            return 'ap' in out.stdout.lower()
        except (OSError, subprocess.SubprocessError):
            return False

    def ext_wifi_hotspot_stop(self):
        if not shutil.which('nmcli'):
            return {'ok': False, 'error': 'nmcli not installed on host'}
        body = self._read_json_body()
        con_name = (body.get('name') or '').strip()
        if not con_name:
            # Find the active AP connection.
            st = self.ext_wifi_hotspot_status()
            if not st.get('active') or st.get('mode') != 'ap':
                return {'ok': False, 'error': 'no active hotspot found'}
            con_name = st['name']
        out = subprocess.run(['nmcli', 'connection', 'down', con_name],
                             capture_output=True, text=True, timeout=15)
        return {'ok': out.returncode == 0, 'message': (out.stdout or out.stderr).strip()}

    def ext_wifi_disconnect(self):
        if not shutil.which('nmcli'):
            return {'ok': False, 'error': 'nmcli not installed on host'}
        dev = self._wifi_dev()
        if not dev:
            return {'ok': False, 'error': 'no wifi adapter found'}
        out = subprocess.run(['nmcli', 'device', 'disconnect', dev],
                             capture_output=True, text=True, timeout=15)
        return {'ok': out.returncode == 0, 'message': (out.stdout or out.stderr).strip()}

    def ext_exec(self):
        body = self._read_json_body()
        kind = body.get('kind', 'host')
        vmid = str(body.get('vmid', '') or '')
        cmd = body.get('command', '')
        timeout = int(body.get('timeout', EXEC_TIMEOUT) or EXEC_TIMEOUT)
        if not cmd:
            return {'ok': False, 'error': 'command required'}
        if kind == 'lxc':
            if not vmid:
                return {'ok': False, 'error': 'vmid required'}
            argv = ['pct', 'exec', vmid, '--', 'sh', '-c', cmd]
        elif kind == 'vm':
            if not vmid:
                return {'ok': False, 'error': 'vmid required'}
            argv = ['qm', 'guest', 'exec', vmid, '--', 'sh', '-c', cmd]
        else:
            if not ALLOW_HOST_EXEC:
                return {'ok': False, 'error': 'host exec disabled (ALLOW_HOST_EXEC=False)'}
            argv = ['sh', '-c', cmd]
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {'ok': False, 'error': f'timed out after {timeout}s'}
        except FileNotFoundError as e:
            return {'ok': False, 'error': str(e)}
        # `qm guest exec` prints a JSON envelope to stdout.
        if kind == 'vm' and out.stdout.strip().startswith('{'):
            try:
                j = json.loads(out.stdout)
                return {'ok': j.get('exitcode', 0) == 0, 'code': j.get('exitcode'),
                        'stdout': j.get('out-data', ''), 'stderr': j.get('err-data', '')}
            except ValueError:
                pass
        return {'ok': out.returncode == 0, 'code': out.returncode,
                'stdout': out.stdout, 'stderr': out.stderr}

    def ext_shortcut(self, qs):
        def g(k, d=''):
            return (qs.get(k, [d])[0] or d)
        vmid, node, name = g('vmid'), g('node'), g('name', f"vm-{g('vmid')}")
        kind = g('kind', 'kvm')
        host = self.headers.get('Host', f'localhost:{PORT}')
        console = 'lxc' if kind == 'lxc' else ('shell' if kind == 'shell' else 'kvm')
        url = f'{SCHEME}://{host}/?console={console}&novnc=1&vmid={vmid}&node={node}&resize=scale'
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', name)
        desktop = ('[Desktop Entry]\nType=Application\n'
                   f'Name=Console: {name}\nComment=Open the Proxmox console for {name}\n'
                   f'Exec=xdg-open "{url}"\nTerminal=false\nIcon=utilities-terminal\nCategories=System;\n')
        data = desktop.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-desktop')
        self.send_header('Content-Disposition', f'attachment; filename="console-{safe}.desktop"')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path[:80]}", flush=True)


def _nmcli_split(line):
    return [f.replace('\\:', ':') for f in re.split(r'(?<!\\):', line)]


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == '__main__':
    if not os.path.exists(UI_FILE):
        sys.exit(f"  proxmox-ui.html not found next to this script ({UI_FILE})")
    server = ThreadingServer(('', PORT), Handler)
    if USE_TLS and os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        SCHEME = 'https'
    elif USE_TLS:
        print(f"  TLS requested but cert not found ({TLS_CERT}); serving plain HTTP")
    print(f"\n  Proxmox alternate UI")
    print(f"  Serving {UI_FILE}")
    print(f"  {SCHEME}://0.0.0.0:{PORT}/   →  API + console forwarded to {TARGET}")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
