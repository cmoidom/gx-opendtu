"""Built-in web page for editing config.json (incl. adding/removing inverters).

Stdlib only (http.server) so it runs unmodified on Venus OS (Cerbo GX) and on
a plain VM, no extra dependency. Started in a background thread from
src/main.py, on config.web.port.

Deliberately does NOT reload the running control loop or restart the
service on save -- it only writes config.json. A restart is required for
edits to take effect (see README.md). This keeps a bad edit in the web form
from immediately disrupting a live zero-export control loop, and keeps this
module fully decoupled from the control loop's in-memory state.

No authentication (matches the OpenDTU API's own default) -- anyone on the
LAN that can reach this port can change the controller's configuration.
"""

from __future__ import annotations

import html
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from src.config import parse_config
from src.opendtu_client import OpenDTUClient, OpenDTUError

_FIELDS = [
    ("opendtu.base_url", "text", "http://192.168.1.50"),
    ("grid.export_setpoint_w", "number", "30"),
    ("grid.read_interval_s", "number", "2"),
    ("grid.ema_alpha", "number", "0.5"),
    ("control.kp", "number", "0.4"),
    ("control.ki", "number", "0.05"),
    ("control.decision_interval_s", "number", "5"),
    ("control.step_absolute_w", "number", "100"),
    ("control.step_relative_pct", "number", "10"),
    ("control.min_change_w", "number", "5"),
    ("capacity_probe.step_w", "number", "10"),
    ("capacity_probe.interval_s", "number", "30"),
    ("battery.activate_at_pct", "number", "100"),
    ("battery.deactivate_below_pct", "number", "98"),
    ("web.port", "number", "8080"),
]


def _dig(raw: dict, dotted_path: str, default=""):
    node = raw
    for key in dotted_path.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return default if node is None else node


def _load_raw(config_path: str) -> dict:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _inverter_rows_html(inverters: list) -> str:
    rows = []
    for inv in inverters:
        row = (
            '<tr class="inv-row">'
            '<td><input type="text" name="inverter_serial" value="{serial}" required></td>'
            '<td><input type="number" name="inverter_nominal_power_w" value="{power}" '
            'step="1" min="1" required></td>'
            '<td><button type="button" class="remove-btn" onclick="this.closest(\'tr\').remove()">'
            "&times;</button></td></tr>"
        ).format(
            serial=html.escape(str(inv.get("serial", ""))),
            power=html.escape(str(inv.get("nominal_power_w", ""))),
        )
        rows.append(row)
    return "\n".join(rows)


def _render_page(raw: dict, error: str = "", message: str = "") -> str:
    banner = ""
    if error:
        banner = f'<div class="banner error">{html.escape(error)}</div>'
    elif message:
        banner = f'<div class="banner ok">{html.escape(message)}</div>'

    def val(path: str, default: str = "") -> str:
        return html.escape(str(_dig(raw, path, default)))

    grid_source = _dig(raw, "grid.source", "dbus")
    modbus_display = "" if grid_source == "modbus" else ' style="display:none"'

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>gx-opendtu - configuration</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ font-size: 1.3rem; }}
  fieldset {{ margin-bottom: 1.2rem; border: 1px solid #ccc; border-radius: 6px; }}
  legend {{ font-weight: 600; padding: 0 0.4rem; }}
  label {{ display: block; margin: 0.5rem 0 0.15rem; font-size: 0.9rem; }}
  input[type=text], input[type=number] {{ width: 100%; padding: 0.35rem; box-sizing: border-box; }}
  input[type=checkbox] {{ margin-right: 0.4rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: 0.25rem; }}
  .remove-btn {{ color: #b00; border: none; background: none; font-size: 1.2rem; cursor: pointer; }}
  .banner {{ padding: 0.6rem 1rem; border-radius: 6px; margin-bottom: 1rem; }}
  .banner.error {{ background: #fde2e2; color: #7a1212; }}
  .banner.ok {{ background: #e2f6e2; color: #1a5c1a; }}
  button.primary {{ padding: 0.6rem 1.2rem; background: #2563eb; color: white; border: none;
                    border-radius: 6px; cursor: pointer; font-size: 1rem; }}
  #add-inv-btn {{ margin-top: 0.5rem; }}
  .hint {{ color: #666; font-size: 0.82rem; margin: 0.2rem 0 0; }}
</style>
</head>
<body>
<h1>gx-opendtu - configuration</h1>
{banner}
<form method="post" action="/save">

  <fieldset>
    <legend>OpenDTU</legend>
    <label>URL de base OpenDTU</label>
    <input type="text" name="opendtu.base_url" value="{val('opendtu.base_url')}" required>
  </fieldset>

  <fieldset>
    <legend>Reseau (grid)</legend>
    <label>Consigne d'export (W)</label>
    <input type="number" step="any" name="grid.export_setpoint_w" value="{val('grid.export_setpoint_w', '30')}" required>
    <label>Intervalle de lecture (s)</label>
    <input type="number" step="any" name="grid.read_interval_s" value="{val('grid.read_interval_s', '2')}" required>
    <label>Coefficient EMA (0-1)</label>
    <input type="number" step="any" min="0" max="1" name="grid.ema_alpha" value="{val('grid.ema_alpha', '0.5')}" required>
    <label>Source</label>
    <select name="grid.source" id="grid-source" onchange="document.getElementById('modbus-fields').style.display = this.value === 'modbus' ? '' : 'none'">
      <option value="dbus" {"selected" if grid_source != "modbus" else ""}>dbus (local, sur le Cerbo GX)</option>
      <option value="modbus" {"selected" if grid_source == "modbus" else ""}>modbus (TCP distant, depuis une VM)</option>
    </select>
    <div id="modbus-fields"{modbus_display}>
      <label>Hote Modbus (IP Cerbo GX)</label>
      <input type="text" name="grid.modbus.host" value="{val('grid.modbus.host')}">
      <label>Port Modbus</label>
      <input type="number" name="grid.modbus.port" value="{val('grid.modbus.port', '502')}">
      <label>Unit ID</label>
      <input type="number" name="grid.modbus.unit_id" value="{val('grid.modbus.unit_id', '100')}">
    </div>
  </fieldset>

  <fieldset>
    <legend>Asservissement (control)</legend>
    <label>kp</label>
    <input type="number" step="any" name="control.kp" value="{val('control.kp', '0.4')}" required>
    <label>ki</label>
    <input type="number" step="any" name="control.ki" value="{val('control.ki', '0.05')}" required>
    <label>Intervalle de decision (s)</label>
    <input type="number" step="any" name="control.decision_interval_s" value="{val('control.decision_interval_s', '5')}" required>
    <label>Palier absolu (W)</label>
    <input type="number" step="any" name="control.step_absolute_w" value="{val('control.step_absolute_w', '100')}" required>
    <label>Palier relatif (%)</label>
    <input type="number" step="any" name="control.step_relative_pct" value="{val('control.step_relative_pct', '10')}" required>
    <label>Changement minimal (W)</label>
    <input type="number" step="any" name="control.min_change_w" value="{val('control.min_change_w', '5')}" required>
  </fieldset>

  <fieldset>
    <legend>Sonde de capacite (capacity_probe)</legend>
    <label>Palier de sonde (W)</label>
    <input type="number" step="any" name="capacity_probe.step_w" value="{val('capacity_probe.step_w', '10')}" required>
    <label>Intervalle de sonde (s)</label>
    <input type="number" step="any" name="capacity_probe.interval_s" value="{val('capacity_probe.interval_s', '30')}" required>
  </fieldset>

  <fieldset>
    <legend>Batterie (priorite charge)</legend>
    <label><input type="checkbox" name="battery.enabled" {"checked" if _dig(raw, "battery.enabled", False) else ""}> Activer</label>
    <label>Seuil d'activation SOC (%)</label>
    <input type="number" step="any" name="battery.activate_at_pct" value="{val('battery.activate_at_pct', '100')}" required>
    <label>Seuil de desactivation SOC (%)</label>
    <input type="number" step="any" name="battery.deactivate_below_pct" value="{val('battery.deactivate_below_pct', '98')}" required>
  </fieldset>

  <fieldset>
    <legend>Onduleurs</legend>
    <table id="inv-table">
      <tbody id="inv-tbody">
{_inverter_rows_html(_dig(raw, "inverters", []))}
      </tbody>
    </table>
    <button type="button" id="add-inv-btn" onclick="addInverterRow()">+ Ajouter un onduleur (manuel)</button>

    <div style="margin-top:0.8rem">
      <button type="button" onclick="fetchInverters()">Charger la liste depuis OpenDTU</button>
      <p class="hint" id="fetch-status"></p>
      <div id="discovered-list"></div>
      <p class="hint">Cocher un onduleur decouvert l'ajoute a la liste ci-dessus (puissance
      pre-remplie depuis OpenDTU, modifiable) -- decocher ne retire rien, utilisez le
      bouton &times; sur la ligne pour retirer un onduleur deja ajoute.</p>
    </div>
  </fieldset>

  <fieldset>
    <legend>Page de configuration (web)</legend>
    <label><input type="checkbox" name="web.enabled" {"checked" if _dig(raw, "web.enabled", True) else ""}> Activer cette page</label>
    <label>Port</label>
    <input type="number" name="web.port" value="{val('web.port', '8080')}" required>
    <p class="hint">Necessite un redemarrage du service pour prendre effet.</p>
  </fieldset>

  <button type="submit" class="primary">Enregistrer</button>
  <p class="hint">Ecrit config.json uniquement -- redemarrez le service pour appliquer les changements.</p>
</form>

<script>
function addInverterRow(serial, power) {{
  const tbody = document.getElementById('inv-tbody');
  const tr = document.createElement('tr');
  tr.className = 'inv-row';
  tr.innerHTML = '<td><input type="text" name="inverter_serial" value="' + (serial || '') +
    '" required></td>' +
    '<td><input type="number" name="inverter_nominal_power_w" value="' + (power || '') +
    '" step="1" min="1" required></td>' +
    '<td><button type="button" class="remove-btn" onclick="this.closest(\\'tr\\').remove()">&times;</button></td>';
  tbody.appendChild(tr);
}}

function existingSerials() {{
  return Array.from(document.querySelectorAll('input[name="inverter_serial"]')).map(i => i.value.trim());
}}

function fetchInverters() {{
  const baseUrl = document.querySelector('input[name="opendtu.base_url"]').value.trim();
  const status = document.getElementById('fetch-status');
  const list = document.getElementById('discovered-list');
  status.textContent = 'Chargement...';
  list.innerHTML = '';
  fetch('/fetch-inverters?base_url=' + encodeURIComponent(baseUrl))
    .then(r => r.json())
    .then(data => {{
      if (data.error) {{ status.textContent = 'Erreur: ' + data.error; return; }}
      if (!data.inverters.length) {{ status.textContent = 'Aucun onduleur trouve sur cet OpenDTU.'; return; }}
      status.textContent = data.inverters.length + ' onduleur(s) trouve(s) sur OpenDTU :';
      const known = existingSerials();
      data.inverters.forEach(inv => {{
        const already = known.includes(inv.serial);
        const row = document.createElement('div');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = already;
        cb.disabled = already;
        cb.dataset.serial = inv.serial;
        cb.dataset.power = inv.max_power_w;
        cb.onchange = function() {{
          if (this.checked && !existingSerials().includes(this.dataset.serial)) {{
            addInverterRow(this.dataset.serial, this.dataset.power);
            this.disabled = true;
          }}
        }};
        const label = document.createElement('label');
        label.appendChild(cb);
        label.appendChild(document.createTextNode(
          ' ' + (inv.name || '(sans nom)') + ' (' + inv.serial + ') - ' + inv.max_power_w + ' W' +
          (already ? ' [deja gere]' : '')
        ));
        row.appendChild(label);
        list.appendChild(row);
      }});
    }})
    .catch(err => {{ status.textContent = 'Erreur: ' + err; }});
}}
</script>
</body>
</html>
"""


def _form_to_raw(form: dict) -> dict:
    def first(key: str, default: str = "") -> str:
        values = form.get(key)
        return values[0] if values else default

    serials = form.get("inverter_serial", [])
    powers = form.get("inverter_nominal_power_w", [])
    inverters = []
    for serial, power in zip(serials, powers):
        serial = serial.strip()
        if not serial:
            continue
        inverters.append({"serial": serial, "nominal_power_w": float(power)})

    raw = {
        "opendtu": {"base_url": first("opendtu.base_url").strip()},
        "grid": {
            "export_setpoint_w": float(first("grid.export_setpoint_w", "30")),
            "read_interval_s": float(first("grid.read_interval_s", "2")),
            "ema_alpha": float(first("grid.ema_alpha", "0.5")),
            "source": first("grid.source", "dbus"),
        },
        "control": {
            "kp": float(first("control.kp", "0.4")),
            "ki": float(first("control.ki", "0.05")),
            "decision_interval_s": float(first("control.decision_interval_s", "5")),
            "step_absolute_w": float(first("control.step_absolute_w", "100")),
            "step_relative_pct": float(first("control.step_relative_pct", "10")),
            "min_change_w": float(first("control.min_change_w", "5")),
        },
        "capacity_probe": {
            "step_w": float(first("capacity_probe.step_w", "10")),
            "interval_s": float(first("capacity_probe.interval_s", "30")),
        },
        "battery": {
            "enabled": "battery.enabled" in form,
            "activate_at_pct": float(first("battery.activate_at_pct", "100")),
            "deactivate_below_pct": float(first("battery.deactivate_below_pct", "98")),
        },
        "web": {
            "enabled": "web.enabled" in form,
            "port": int(float(first("web.port", "8080"))),
        },
        "inverters": inverters,
    }
    if raw["grid"]["source"] == "modbus":
        raw["grid"]["modbus"] = {
            "host": first("grid.modbus.host").strip(),
            "port": int(float(first("grid.modbus.port", "502"))),
            "unit_id": int(float(first("grid.modbus.unit_id", "100"))),
        }
    return raw


def _write_raw(config_path: str, raw: dict) -> None:
    tmp_path = f"{config_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, config_path)


def _make_handler(config_path: str):
    class ConfigHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003 - quiet down default per-request stderr logging
            pass

        def _send_html(self, body: str, status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, obj: dict, status: int = 200) -> None:
            encoded = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802 - required BaseHTTPRequestHandler method name
            parsed = urlsplit(self.path)
            if parsed.path == "/fetch-inverters":
                self._handle_fetch_inverters(parse_qs(parsed.query))
                return
            if parsed.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            self._send_html(_render_page(_load_raw(config_path)))

        def _handle_fetch_inverters(self, query: dict) -> None:
            base_url = (query.get("base_url") or [""])[0].strip()
            if not base_url:
                self._send_json({"error": "URL OpenDTU manquante"}, status=400)
                return
            client = OpenDTUClient(base_url, timeout_s=5.0)
            try:
                inverters = client.list_inverters()
            except OpenDTUError as exc:
                self._send_json({"error": str(exc)}, status=502)
                return
            self._send_json(
                {
                    "inverters": [
                        {"serial": inv.serial, "name": inv.name, "max_power_w": inv.max_power_w}
                        for inv in inverters
                    ]
                }
            )

        def do_POST(self) -> None:  # noqa: N802 - required BaseHTTPRequestHandler method name
            if self.path != "/save":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            form = parse_qs(body, keep_blank_values=True)

            raw: dict = {}
            try:
                raw = _form_to_raw(form)
                parse_config(raw)  # validate before writing
                _write_raw(config_path, raw)
            except (ValueError, TypeError) as exc:
                self._send_html(_render_page(raw, error=str(exc)), status=400)
                return

            self._send_html(
                _render_page(raw, message="Configuration enregistree. Redemarrez le service pour l'appliquer.")
            )

    return ConfigHandler


def start_webui_server(config_path: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(config_path))
    thread = threading.Thread(target=server.serve_forever, name="gx-opendtu-webui", daemon=True)
    thread.start()
    return server
