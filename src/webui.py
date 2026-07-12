"""Built-in web pages: config editor ("/") and live dashboard ("/dashboard").

Stdlib only (http.server) so it runs unmodified on Venus OS (Cerbo GX) and on
a plain VM, no extra dependency. Started in a background thread from
src/main.py, on config.web.port.

The dashboard polls GET /status.json (incremental via ?since=<epoch>) and
draws its charts with hand-rolled <canvas> code -- no charting library, kept
inline, since Venus OS has no guaranteed internet access to fetch a CDN
script from. Data comes from a shared src.live_state.LiveState instance the
control loop (src/main.py) writes into every cycle; this module only reads
it, never touches it otherwise.

Saving ("Enregistrer") only writes config.json -- it deliberately does NOT
reload the running control loop or restart the service, keeping a bad edit
in the web form from immediately disrupting a live zero-export control
loop, and keeping this module fully decoupled from the control loop's
in-memory state. Applying ("Enregistrer et appliquer") is a separate,
explicit action: it validates and writes the same as save, then exits the
whole process (os._exit) so the service supervisor restarts it and it picks
up the new config on the next load_config() call -- there is no in-process
hot-reload. Exit code 1 is used so this also works under systemd's
`Restart=on-failure` (see deploy/systemd/*.service), not just daemontools
(which restarts unconditionally on any exit, see services/*).

No authentication (matches the OpenDTU API's own default) -- anyone on the
LAN that can reach this port can change the controller's configuration.
"""

from __future__ import annotations

import html
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from src.config import parse_config
from src.manual_override import DEFAULT_DURATION_S
from src.opendtu_client import OpenDTUClient, OpenDTUError

log = logging.getLogger("gx-opendtu-zero-export")

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
            '<td><input type="text" name="inverter_name" value="{name}" placeholder="(optionnel)"></td>'
            '<td><input type="number" name="inverter_nominal_power_w" value="{power}" '
            'step="1" min="1" required></td>'
            '<td><button type="button" class="remove-btn" onclick="this.closest(\'tr\').remove()">'
            "&times;</button></td></tr>"
        ).format(
            serial=html.escape(str(inv.get("serial", ""))),
            name=html.escape(str(inv.get("name") or "")),
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
                    border-radius: 6px; cursor: pointer; font-size: 1rem; margin-right: 0.5rem; }}
  button.apply-btn {{ background: #b45309; }}
  #add-inv-btn {{ margin-top: 0.5rem; }}
  .hint {{ color: #666; font-size: 0.82rem; margin: 0.2rem 0 0; }}
  nav {{ margin-bottom: 1rem; font-size: 0.9rem; }}
  nav a {{ color: #2563eb; text-decoration: none; }}
  nav a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<nav><a href="/">Configuration</a> &middot; <a href="/dashboard">Tableau de bord</a></nav>
<h1>gx-opendtu - configuration</h1>
{banner}
<form method="post" action="/save">

  <fieldset>
    <legend>OpenDTU</legend>
    <label>URL de base OpenDTU</label>
    <input type="text" name="opendtu.base_url" value="{val('opendtu.base_url')}" required>
    <label>Nom d'utilisateur (si Basic Auth activee sur OpenDTU)</label>
    <input type="text" name="opendtu.username" value="{val('opendtu.username')}" autocomplete="off">
    <label>Mot de passe</label>
    <input type="password" name="opendtu.password" value="{val('opendtu.password')}" autocomplete="off">
    <p class="hint">Necessaire uniquement si OpenDTU renvoie "401 Unauthorized" (visible dans les
    logs) -- laisser vide sinon. Sans authentification, le controleur ne peut pas limiter les
    onduleurs, y compris le repli fail-safe.</p>
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
    <label>Seuil mini onduleur (% de sa puissance nominale)</label>
    <input type="number" step="any" min="0" max="100" name="control.min_inverter_pct" value="{val('control.min_inverter_pct', '10')}" required>
    <p class="hint">Un onduleur qui produit n'est jamais commande sous ce seuil (certains
    micro-onduleurs ne regulent pas de façon fiable pres de 0). Mettre 0 pour desactiver.
    Un arret complet (fail-safe, charge batterie) n'est jamais concerne.</p>
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
    <label>Export confirmant la batterie pleine (W)</label>
    <input type="number" step="any" min="0" name="battery.export_confirms_full_w" value="{val('battery.export_confirms_full_w', '50')}" required>
    <p class="hint">Passe en regulation ON dès qu'un export reseau reel d'au moins cette puissance
    est observe alors que le SOC est deja au-dessus du seuil de desactivation -- evite d'attendre
    que le SOC atteigne pile le seuil d'activation. Mettre 0 pour desactiver.</p>
  </fieldset>

  <fieldset>
    <legend>Onduleurs</legend>
    <table id="inv-table">
      <thead><tr><th>Serie</th><th>Nom</th><th>Puissance nominale (W)</th><th></th></tr></thead>
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

  <fieldset>
    <legend>Journalisation (logging)</legend>
    <label><input type="checkbox" name="logging.verbose_traces"
      {"checked" if _dig(raw, "logging.verbose_traces", True) else ""}> Tracer l'etat complet a chaque cycle</label>
    <p class="hint">Ligne "grid_meter=... injection_control=..." loggee a chaque cycle de decision,
    changement ou non. Desactiver si le <a href="/dashboard">tableau de bord</a> suffit --
    les erreurs et actions (fail-safe, deblocage charge batterie, redemarrage) restent
    tracees dans tous les cas.</p>
  </fieldset>

  <button type="submit" formaction="/save" class="primary">Enregistrer</button>
  <button type="submit" formaction="/apply" class="primary apply-btn"
          onclick="return confirm('Enregistrer et redemarrer le service maintenant ? Le pilotage sera brievement interrompu.');">
    Enregistrer et appliquer (redemarre le service)
  </button>
  <p class="hint">"Enregistrer" ecrit config.json sans redemarrer -- utile pour preparer des
  changements sans interrompre le pilotage. "Enregistrer et appliquer" redemarre le service
  tout de suite pour prendre en compte la nouvelle config.</p>
</form>

<script>
function addInverterRow(serial, power, name) {{
  const tbody = document.getElementById('inv-tbody');
  const tr = document.createElement('tr');
  tr.className = 'inv-row';
  tr.innerHTML = '<td><input type="text" name="inverter_serial" value="' + (serial || '') +
    '" required></td>' +
    '<td><input type="text" name="inverter_name" value="' + (name || '') + '" placeholder="(optionnel)"></td>' +
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
  const username = document.querySelector('input[name="opendtu.username"]').value.trim();
  const password = document.querySelector('input[name="opendtu.password"]').value;
  const status = document.getElementById('fetch-status');
  const list = document.getElementById('discovered-list');
  status.textContent = 'Chargement...';
  list.innerHTML = '';
  const params = new URLSearchParams({{ base_url: baseUrl, username: username, password: password }});
  fetch('/fetch-inverters?' + params.toString())
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
        cb.dataset.name = inv.name || '';
        cb.onchange = function() {{
          if (this.checked && !existingSerials().includes(this.dataset.serial)) {{
            addInverterRow(this.dataset.serial, this.dataset.power, this.dataset.name);
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


def _render_dashboard_page() -> str:
    return """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>gx-opendtu - tableau de bord</title>
<style>
  :root {
    --surface-1: #fcfcfb; --page: #f9f9f7; --text-primary: #0b0b0b; --text-secondary: #52514e;
    --muted: #898781; --gridline: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6; --series-2: #1baf7a; --series-3: #eda100; --series-4: #4a3aa7;
    --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --surface-1: #1a1a19; --page: #0d0d0d; --text-primary: #ffffff; --text-secondary: #c3c2b7;
      --muted: #898781; --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
      --series-1: #3987e5; --series-2: #199e70; --series-3: #c98500; --series-4: #9085e9;
      --good: #0ca30c; --warning: #fab219; --critical: #e66767;
    }
  }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 960px; margin: 2rem auto;
         padding: 0 1rem; color: var(--text-primary); background: var(--page); }
  nav { margin-bottom: 1rem; font-size: 0.9rem; }
  nav a { color: var(--series-1); text-decoration: none; }
  nav a:hover { text-decoration: underline; }
  h1 { font-size: 1.3rem; }
  h2 { font-size: 1rem; margin: 1.6rem 0 0.5rem; }
  .tiles { display: flex; flex-wrap: wrap; gap: 0.6rem; margin-bottom: 0.5rem; }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
          padding: 0.6rem 0.9rem; min-width: 130px; }
  .tile .label { color: var(--text-secondary); font-size: 0.78rem; }
  .tile .value { font-size: 1.3rem; font-variant-numeric: tabular-nums; }
  .tile .value.on { color: var(--good); }
  .tile .value.off { color: var(--warning); }
  .banner { padding: 0.6rem 0.9rem; border-radius: 8px; margin-bottom: 0.8rem; font-size: 0.88rem; }
  .banner.warning { background: #fdf0d5; border: 1px solid var(--warning); color: #0b0b0b; }
  @media (prefers-color-scheme: dark) {
    .banner.warning { background: #3a2f14; color: #ffffff; }
  }
  .chart-box { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
               padding: 0.8rem; margin-bottom: 1rem; position: relative; }
  .control-row { display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; margin: 0.3rem 0; }
  .control-label { font-size: 0.85rem; color: var(--text-secondary); margin-right: 0.3rem; }
  .mode-btn, .force-btn, #cancel-force-btn { padding: 0.3rem 0.7rem; border-radius: 6px; cursor: pointer;
    border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); font-size: 0.85rem; }
  .mode-btn.active { background: var(--series-1); color: white; border-color: var(--series-1); }
  .force-btn { background: var(--series-4); color: white; border-color: var(--series-4); }
  #cancel-force-btn { background: var(--critical); color: white; border-color: var(--critical); }
  .chart-box canvas { width: 100%; height: 200px; display: block; }
  .legend { display: flex; flex-wrap: wrap; gap: 1rem; font-size: 0.82rem; color: var(--text-secondary);
            margin-top: 0.4rem; }
  .legend .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                 margin-right: 0.35rem; vertical-align: middle; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid var(--gridline); }
  th { color: var(--text-secondary); font-weight: 600; }
  th.num, td.num, th.center, td.center { text-align: center; }
  td.num { font-variant-numeric: tabular-nums; }
  .hint { color: var(--muted); font-size: 0.82rem; }
  #tooltip { position: fixed; display: none; background: var(--text-primary); color: var(--surface-1);
             font-size: 0.78rem; padding: 0.35rem 0.55rem; border-radius: 6px; pointer-events: none;
             z-index: 10; white-space: nowrap; }
  .chart-box canvas { cursor: grab; }
  .chart-box canvas.dragging { cursor: grabbing; }
  #reset-zoom { font-size: 0.8rem; padding: 0.25rem 0.6rem; margin-left: 0.6rem; cursor: pointer; }
</style>
</head>
<body>
<nav><a href="/">Configuration</a> &middot; <a href="/dashboard">Tableau de bord</a></nav>
<h1>gx-opendtu - tableau de bord</h1>
<p class="hint"><span id="conn-status">Connexion...</span>
  <button type="button" id="reset-zoom" onclick="resetZoom()" style="display:none">Reinitialiser le zoom</button>
</p>
<p class="hint">Molette pour zoomer, glisser pour deplacer, double-clic pour reinitialiser --
synchronise sur les trois graphiques temporels.</p>

<div id="floor-warning" class="banner warning" style="display:none"></div>

<div class="chart-box" id="manual-controls">
  <div class="control-row">
    <span class="control-label">Mode regulation :</span>
    <button type="button" class="mode-btn" data-mode="AUTO" onclick="setInjectionMode('AUTO')">AUTO</button>
    <button type="button" class="mode-btn" data-mode="ON" onclick="setInjectionMode('ON')">ON</button>
    <button type="button" class="mode-btn" data-mode="OFF" onclick="setInjectionMode('OFF')">OFF</button>
    <span class="hint" id="mode-status"></span>
  </div>
  <p class="hint">AUTO = hysterese normale basee sur le SOC. ON/OFF force le mode
  jusqu'a revenir en AUTO -- utile juste apres un redemarrage si la regulation
  reste bloquee en OFF alors que la batterie est en realite pleine.</p>
  <div class="control-row">
    <span class="control-label">Forcer tous les onduleurs a :</span>
    <button type="button" class="force-btn" onclick="setForcePct(25)">25%</button>
    <button type="button" class="force-btn" onclick="setForcePct(50)">50%</button>
    <button type="button" class="force-btn" onclick="setForcePct(75)">75%</button>
    <button type="button" class="force-btn" onclick="setForcePct(100)">100%</button>
    <button type="button" id="cancel-force-btn" onclick="cancelForcePct()" style="display:none">Annuler le forcage</button>
    <span class="hint" id="force-status"></span>
  </div>
  <p class="hint">Contournement direct du regulateur PI pendant 5 minutes (test/diagnostic),
  puis retour automatique au pilotage normal. Sans effet si le mode regulation est OFF
  (deblocage a 100% pour la charge batterie reste prioritaire).</p>
</div>

<div class="tiles" id="tiles"></div>

<h2>SOC batterie</h2>
<div class="chart-box"><canvas id="chart-soc"></canvas></div>

<h2>Puissance reseau (brut / EMA) et batterie</h2>
<div class="chart-box">
  <canvas id="chart-grid"></canvas>
  <div class="legend">
    <span><span class="dot" style="background:var(--series-1)"></span>Reseau (brut)</span>
    <span><span class="dot" style="background:var(--series-2)"></span>Reseau (EMA, utilisee par le regulateur)</span>
    <span><span class="dot" style="background:var(--series-3)"></span>Batterie</span>
  </div>
  <p class="hint">Batterie : positif = charge, negatif = decharge.</p>
</div>

<h2>Puissance par onduleur</h2>
<div class="chart-box">
  <canvas id="chart-inverters"></canvas>
  <div class="legend" id="inverters-legend"></div>
</div>

<h2>Detail par onduleur</h2>
<table id="inverters-table">
  <thead><tr><th>Serie</th><th class="num">Puissance</th><th class="num">Limite</th><th class="num">Nominale</th><th class="center">Etat</th></tr></thead>
  <tbody></tbody>
</table>
<p class="hint">Pendant la charge batterie prioritaire (regulation OFF), "Limite" affiche
100% (debride) et "Puissance" la production reelle mesuree -- ce n'est pas une commande active.</p>

<h2>Energie reseau par heure</h2>
<div class="chart-box">
  <canvas id="chart-energy"></canvas>
  <div class="legend">
    <span><span class="dot" style="background:var(--series-1)"></span>Soutiree du reseau</span>
    <span><span class="dot" style="background:var(--series-2)"></span>Injectee au reseau</span>
  </div>
  <p class="hint">Compteurs cumulatifs du compteur reseau (pas de la boucle de pilotage) --
  l'heure en cours est partielle. Pas de zoom/deplacement sur ce graphique (deja agrege a l'heure).</p>
</div>

<div id="tooltip"></div>

<script>
const SERIES_COLORS = ['--series-1', '--series-2', '--series-3', '--series-4'];
const root = getComputedStyle(document.documentElement);
function cssVar(name) { return root.getPropertyValue(name).trim(); }

let lastT = 0;
let history = [];
const MAX_POINTS = 900;
let inverterOrder = [];  // stable color assignment, first-seen order
let inverterNames = {};  // serial -> name, from config (src/config.py InverterConfig.name)
let hourlyEnergy = [];

function inverterLabel(serial) { return inverterNames[serial] || serial; }

// Shared zoom/pan window, synchronized across all time-series charts. null
// means "auto, full extent" -- both bounds are epoch seconds so a zoomed-in
// window stays anchored to real time as new samples arrive, rather than
// drifting as history grows.
let viewTMin = null;
let viewTMax = null;
let isDragging = false;

function resetZoom() { viewTMin = null; viewTMax = null; renderCharts(); }

function setInjectionMode(mode) {
  fetch('/override/mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'mode=' + encodeURIComponent(mode),
  }).then(() => poll());
}

function setForcePct(pct) {
  fetch('/override/pct', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'pct=' + pct,
  }).then(() => poll());
}

function cancelForcePct() {
  fetch('/override/pct/clear', { method: 'POST' }).then(() => poll());
}

function renderControls(data) {
  const mode = data.injection_mode || 'AUTO';
  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
  document.getElementById('mode-status').textContent = mode === 'AUTO' ? '' : '(force manuellement)';

  const override = data.manual_override;
  const cancelBtn = document.getElementById('cancel-force-btn');
  const status = document.getElementById('force-status');
  if (override) {
    cancelBtn.style.display = '';
    status.textContent = 'Actif : ' + override.pct + '% (encore ' + Math.round(override.remaining_s) + 's)';
  } else {
    cancelBtn.style.display = 'none';
    status.textContent = '';
  }
}

function fullHistoryRange() {
  if (!history.length) return [0, 1];
  return [history[0].t, history[history.length - 1].t];
}

function clampView(tMin, tMax) {
  const [fMin, fMax] = fullHistoryRange();
  const span = Math.min(tMax - tMin, fMax - fMin);
  if (span <= 0) return [fMin, fMax];
  let newMin = tMin, newMax = tMin + span;
  if (newMin < fMin) { newMin = fMin; newMax = newMin + span; }
  if (newMax > fMax) { newMax = fMax; newMin = newMax - span; }
  return [newMin, newMax];
}

function fmtTime(t) { return new Date(t * 1000).toLocaleTimeString(); }
function fmtW(v) { return (v === null || v === undefined) ? '-' : Math.round(v) + ' W'; }
function fmtPct(v) { return (v === null || v === undefined) ? '-' : Math.round(v) + ' %'; }
function fmtKwh(v) { return (v === null || v === undefined) ? '-' : v.toFixed(2) + ' kWh'; }
function fmtHour(h) { return new Date(h * 1000).toLocaleTimeString([], { hour: '2-digit' }); }

// "Nice numbers" axis rounding (Heckbert's algorithm): picks bounds/step
// that are always a round 1/2/5 x 10^n, so gridlines land on 50/100/200/500
// rather than whatever the raw data range happens to divide into (49, 189).
function niceNum(range, round) {
  if (range <= 0) return 1;
  const exponent = Math.floor(Math.log10(range));
  const fraction = range / Math.pow(10, exponent);
  let niceFraction;
  if (round) {
    if (fraction < 1.5) niceFraction = 1;
    else if (fraction < 3) niceFraction = 2;
    else if (fraction < 7) niceFraction = 5;
    else niceFraction = 10;
  } else {
    if (fraction <= 1) niceFraction = 1;
    else if (fraction <= 2) niceFraction = 2;
    else if (fraction <= 5) niceFraction = 5;
    else niceFraction = 10;
  }
  return niceFraction * Math.pow(10, exponent);
}

function niceScale(min, max, targetTicks) {
  if (min === max) { min -= 1; max += 1; }
  const step = niceNum((max - min) / Math.max(1, targetTicks - 1), true);
  return { min: Math.floor(min / step) * step, max: Math.ceil(max / step) * step, step: step };
}

function drawChart(canvas, seriesList, opts) {
  opts = opts || {};
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (w === 0 || h === 0) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const padding = { left: 46, right: 8, top: 8, bottom: 20 };
  const plotW = w - padding.left - padding.right;
  const plotH = h - padding.top - padding.bottom;
  const allPoints = seriesList.flatMap(s => s.points);

  ctx.font = '10px system-ui';
  if (!allPoints.length) {
    ctx.fillStyle = cssVar('--muted');
    ctx.fillText('en attente de donnees...', padding.left, h / 2);
    canvas._chartData = null;
    return;
  }

  const dataTMin = Math.min.apply(null, allPoints.map(p => p.t));
  const dataTMax = Math.max.apply(null, allPoints.map(p => p.t));
  const tMin = (opts.viewTMin !== null && opts.viewTMin !== undefined) ? opts.viewTMin : dataTMin;
  const tMax = (opts.viewTMax !== null && opts.viewTMax !== undefined) ? opts.viewTMax : dataTMax;

  // Y-axis auto-scales to whatever is actually visible in the current
  // zoom/pan window, not the full history -- falls back to the full set if
  // the window happens to contain no points for a series (e.g. panned past
  // the data, or an all-null series like battery power on a no-battery install).
  const visiblePoints = allPoints.filter(p => p.t >= tMin && p.t <= tMax);
  const pointsForYRange = visiblePoints.length ? visiblePoints : allPoints;
  let dataMin = opts.yMin !== undefined ? opts.yMin : Math.min.apply(null, pointsForYRange.map(p => p.v));
  let dataMax = opts.yMax !== undefined ? opts.yMax : Math.max.apply(null, pointsForYRange.map(p => p.v));
  if (opts.includeZero) { dataMin = Math.min(dataMin, 0); dataMax = Math.max(dataMax, 0); }
  const scale = niceScale(dataMin, dataMax, 5);
  const yMin = scale.min, yMax = scale.max;

  function xPix(t) { return padding.left + (t - tMin) / ((tMax - tMin) || 1) * plotW; }
  function yPix(v) { return padding.top + (1 - (v - yMin) / ((yMax - yMin) || 1)) * plotH; }

  ctx.strokeStyle = cssVar('--gridline');
  ctx.fillStyle = cssVar('--muted');
  ctx.lineWidth = 1;

  // X axis (time) ticks -- one label per ~90px of plot width, 2 to 6 ticks.
  const xSteps = Math.min(6, Math.max(2, Math.round(plotW / 90)));
  for (let i = 0; i <= xSteps; i++) {
    const t = tMin + (tMax - tMin) * i / xSteps;
    const x = xPix(t);
    ctx.beginPath(); ctx.moveTo(x, padding.top); ctx.lineTo(x, h - padding.bottom); ctx.stroke();
    const label = new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const textWidth = ctx.measureText(label).width;
    const lx = Math.max(padding.left, Math.min(w - padding.right - textWidth, x - textWidth / 2));
    ctx.fillText(label, lx, h - 5);
  }

  const tickEpsilon = scale.step * 1e-6;
  for (let v = yMin; v <= yMax + tickEpsilon; v += scale.step) {
    const y = yPix(v);
    ctx.beginPath(); ctx.moveTo(padding.left, y); ctx.lineTo(w - padding.right, y); ctx.stroke();
    ctx.fillText(opts.yFormat ? opts.yFormat(v) : Math.round(v).toString(), 2, y + 3);
  }

  if (yMin < 0 && yMax > 0) {
    ctx.strokeStyle = cssVar('--baseline');
    const y0 = yPix(0);
    ctx.beginPath(); ctx.moveTo(padding.left, y0); ctx.lineTo(w - padding.right, y0); ctx.stroke();
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(padding.left, padding.top, plotW, plotH);
  ctx.clip();
  seriesList.forEach(s => {
    if (!s.points.length) return;
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    let prevT = null;
    s.points.forEach(p => {
      const gap = prevT !== null && (p.t - prevT) > 60;  // don't connect across long OFF-state gaps
      const x = xPix(p.t), y = yPix(p.v);
      if (!started || gap) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
      prevT = p.t;
    });
    ctx.stroke();
  });
  ctx.restore();

  canvas._chartData = {
    seriesList, tMin, tMax, dataTMin, dataTMax, yMin, yMax, padding, plotW, plotH, yFormat: opts.yFormat,
  };
}

// Grouped bar chart (two bars per hour: from-net / to-net) -- no zoom/pan,
// the data is already aggregated to hourly buckets so there's nothing finer
// to zoom into. Hover shows the exact value for the bar under the cursor
// (per-mark tooltip, as opposed to drawChart's continuous crosshair).
function drawBarChart(canvas, buckets, seriesKeys, colors, labels, opts) {
  opts = opts || {};
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (w === 0 || h === 0) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const padding = { left: 46, right: 8, top: 8, bottom: 20 };
  const plotW = w - padding.left - padding.right;
  const plotH = h - padding.top - padding.bottom;

  ctx.font = '10px system-ui';
  if (!buckets.length) {
    ctx.fillStyle = cssVar('--muted');
    ctx.fillText('en attente de donnees...', padding.left, h / 2);
    canvas._barData = null;
    return;
  }

  const allValues = buckets.flatMap(b => seriesKeys.map(k => b[k]));
  const scale = niceScale(0, Math.max.apply(null, allValues.concat([0.01])), 5);
  const yMin = 0, yMax = scale.max;
  function yPix(v) { return padding.top + (1 - (v - yMin) / ((yMax - yMin) || 1)) * plotH; }

  ctx.strokeStyle = cssVar('--gridline');
  ctx.fillStyle = cssVar('--muted');
  ctx.lineWidth = 1;
  const tickEpsilon = scale.step * 1e-6;
  for (let v = yMin; v <= yMax + tickEpsilon; v += scale.step) {
    const y = yPix(v);
    ctx.beginPath(); ctx.moveTo(padding.left, y); ctx.lineTo(w - padding.right, y); ctx.stroke();
    ctx.fillText(opts.yFormat ? opts.yFormat(v) : Math.round(v).toString(), 2, y + 3);
  }

  const slotW = plotW / buckets.length;
  const groupPad = slotW * 0.15;
  const barW = (slotW - groupPad * 2) / seriesKeys.length;
  const bars = [];
  buckets.forEach((b, i) => {
    const slotX = padding.left + i * slotW;
    seriesKeys.forEach((key, k) => {
      const v = b[key] || 0;
      const x = slotX + groupPad + k * barW;
      const y = yPix(v);
      const barH = (padding.top + plotH) - y;
      ctx.fillStyle = colors[k];
      ctx.fillRect(x, y, Math.max(1, barW - 2), barH);
      bars.push({ x, y, w: barW - 2, h: barH, value: v, label: labels[k], hour: b.hour });
    });
  });

  // one label per hour slot (skip some if too narrow to avoid overlap)
  const labelEvery = Math.max(1, Math.ceil(60 / slotW));
  ctx.fillStyle = cssVar('--muted');
  buckets.forEach((b, i) => {
    if (i % labelEvery !== 0) return;
    const label = fmtHour(b.hour);
    const x = padding.left + i * slotW + slotW / 2 - ctx.measureText(label).width / 2;
    ctx.fillText(label, x, h - 5);
  });

  canvas._barData = { bars, padding, plotW, plotH };
}

function attachBarHover(canvas) {
  const tooltip = document.getElementById('tooltip');
  canvas.addEventListener('mousemove', (ev) => {
    const data = canvas._barData;
    if (!data) return;
    const rect = canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
    const hit = data.bars.find(b => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h);
    if (!hit) { tooltip.style.display = 'none'; return; }
    tooltip.style.display = 'block';
    tooltip.style.left = (ev.clientX + 12) + 'px';
    tooltip.style.top = (ev.clientY - 10) + 'px';
    tooltip.innerHTML = '<div>' + fmtHour(hit.hour) + '-' + fmtHour(hit.hour + 3600) + '</div>' +
      '<div>' + hit.label + ': ' + fmtKwh(hit.value) + '</div>';
  });
  canvas.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
}

function attachHover(canvas, opts) {
  const tooltip = document.getElementById('tooltip');
  canvas.addEventListener('mousemove', (ev) => {
    if (isDragging) { tooltip.style.display = 'none'; return; }
    const data = canvas._chartData;
    if (!data) return;
    const rect = canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    if (x < data.padding.left) { tooltip.style.display = 'none'; return; }
    const t = data.tMin + (x - data.padding.left) / data.plotW * (data.tMax - data.tMin);
    const lines = [];
    data.seriesList.forEach(s => {
      if (!s.points.length) return;
      let nearest = s.points[0], best = Math.abs(nearest.t - t);
      s.points.forEach(p => { const d = Math.abs(p.t - t); if (d < best) { best = d; nearest = p; } });
      if (Math.abs(nearest.t - t) < (data.tMax - data.tMin) / 20 || s.points.length < 5) {
        const val = data.yFormat ? data.yFormat(nearest.v) : Math.round(nearest.v);
        lines.push(s.label + ': ' + val);
      }
    });
    if (!lines.length) { tooltip.style.display = 'none'; return; }
    tooltip.style.display = 'block';
    tooltip.style.left = (ev.clientX + 12) + 'px';
    tooltip.style.top = (ev.clientY - 10) + 'px';
    tooltip.innerHTML = '<div>' + fmtTime(t) + '</div>' + lines.map(l => '<div>' + l + '</div>').join('');
  });
  canvas.addEventListener('mouseleave', () => { tooltip.style.display = 'none'; });
}

let dragStartX = 0, dragStartTMin = 0, dragStartTMax = 0, dragPlotW = 0;

function attachZoomPan(canvas) {
  canvas.addEventListener('wheel', (ev) => {
    const data = canvas._chartData;
    if (!data) return;
    ev.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const curTMin = viewTMin !== null ? viewTMin : data.dataTMin;
    const curTMax = viewTMax !== null ? viewTMax : data.dataTMax;
    const span = curTMax - curTMin;
    const cursorT = curTMin + (x - data.padding.left) / data.plotW * span;
    const fullSpan = data.dataTMax - data.dataTMin;
    let newSpan = Math.min(span * (ev.deltaY > 0 ? 1.2 : 1 / 1.2), fullSpan);
    if (newSpan < 10) newSpan = 10;  // floor so scrolling in can't collapse to an unusable sliver
    const ratio = span > 0 ? (cursorT - curTMin) / span : 0.5;
    const [newTMin, newTMax] = clampView(cursorT - ratio * newSpan, cursorT - ratio * newSpan + newSpan);
    if (newSpan >= fullSpan - 1e-6) { viewTMin = null; viewTMax = null; }
    else { viewTMin = newTMin; viewTMax = newTMax; }
    renderCharts();
  }, { passive: false });

  canvas.addEventListener('mousedown', (ev) => {
    const data = canvas._chartData;
    if (!data) return;
    isDragging = true;
    document.querySelectorAll('.chart-box canvas').forEach(c => c.classList.add('dragging'));
    dragStartX = ev.clientX;
    dragStartTMin = viewTMin !== null ? viewTMin : data.dataTMin;
    dragStartTMax = viewTMax !== null ? viewTMax : data.dataTMax;
    dragPlotW = data.plotW;
  });

  canvas.addEventListener('dblclick', () => resetZoom());
}

document.addEventListener('mousemove', (ev) => {
  if (!isDragging || dragPlotW === 0) return;
  const dt = -(ev.clientX - dragStartX) / dragPlotW * (dragStartTMax - dragStartTMin);
  const [newTMin, newTMax] = clampView(dragStartTMin + dt, dragStartTMax + dt);
  viewTMin = newTMin; viewTMax = newTMax;
  renderCharts();
});
document.addEventListener('mouseup', () => {
  if (isDragging) {
    isDragging = false;
    document.querySelectorAll('.chart-box canvas').forEach(c => c.classList.remove('dragging'));
  }
});

const chartSoc = document.getElementById('chart-soc');
const chartGrid = document.getElementById('chart-grid');
const chartInverters = document.getElementById('chart-inverters');
const chartEnergy = document.getElementById('chart-energy');
attachHover(chartSoc, { yFormat: fmtPct });
attachHover(chartGrid, { yFormat: fmtW });
attachHover(chartInverters, { yFormat: fmtW });
attachBarHover(chartEnergy);
attachZoomPan(chartSoc);
attachZoomPan(chartGrid);
attachZoomPan(chartInverters);

function renderTiles(latest) {
  const tiles = document.getElementById('tiles');
  if (!latest) { tiles.innerHTML = ''; return; }
  const on = latest.injection_control === 'ON';
  tiles.innerHTML =
    '<div class="tile"><div class="label">Reseau (brut)</div><div class="value">' + fmtW(latest.grid_raw_w) + '</div></div>' +
    '<div class="tile"><div class="label">Reseau (EMA)</div><div class="value">' + fmtW(latest.grid_ema_w) + '</div></div>' +
    (latest.soc_pct !== null ? '<div class="tile"><div class="label">SOC batterie</div><div class="value">' + fmtPct(latest.soc_pct) + '</div></div>' : '') +
    (latest.battery_power_w !== null && latest.battery_power_w !== undefined ? '<div class="tile"><div class="label">Puissance batterie</div><div class="value">' + fmtW(latest.battery_power_w) + '</div></div>' : '') +
    ((latest.inverters && latest.inverters.length) ? '<div class="tile"><div class="label">Puissance solaire (total)</div><div class="value">' + fmtW(latest.inverters.reduce((sum, inv) => sum + (inv.actual_w || 0), 0)) + '</div></div>' : '') +
    '<div class="tile"><div class="label">Regulation</div><div class="value ' + (on ? 'on' : 'off') + '">' + (latest.injection_control || '-') + '</div></div>' +
    (on ? '<div class="tile"><div class="label">Consigne totale</div><div class="value">' + fmtW(latest.consigne_w) + '</div></div>' : '');

  const warningBox = document.getElementById('floor-warning');
  if (latest.min_inverter_floor_warning) {
    warningBox.style.display = '';
    const rec = latest.recommended_min_inverter_pct;
    warningBox.innerHTML = '&#9888; Le seuil mini onduleur provoque une injection reseau ce cycle. ' +
      (rec !== null && rec !== undefined
        ? 'Valeur qui aurait evite cela maintenant : <strong>' + rec + ' %</strong> (calcul instantane, va fluctuer -- baisser progressivement si ca persiste).'
        : 'Envisager de le reduire dans la configuration.');
  } else {
    warningBox.style.display = 'none';
  }
}

function renderInverterTable(latest) {
  const tbody = document.querySelector('#inverters-table tbody');
  const inverters = (latest && latest.inverters) || [];
  if (!inverters.length) { tbody.innerHTML = '<tr><td colspan="5" class="hint">aucune donnee</td></tr>'; return; }
  tbody.innerHTML = inverters.map(inv => {
    if (inv.name) inverterNames[inv.serial] = inv.name;
    return '<tr><td>' + (inv.name ? inv.name + ' <span class="hint">(' + inv.serial + ')</span>' : inv.serial) + '</td>' +
    '<td class="num">' + fmtW(inv.actual_w) + '</td>' +
    '<td class="num">' + fmtPct(inv.limit_relative_pct) + '</td>' +
    '<td class="num">' + fmtW(inv.max_power_w) + '</td>' +
    '<td class="center">' + (inv.acknowledged === false ? 'en attente (RF)' :
               inv.acknowledged === null ? 'debride (charge batterie)' : 'ok') + '</td></tr>';
  }).join('');
}

function renderInvertersLegend() {
  const legend = document.getElementById('inverters-legend');
  legend.innerHTML = inverterOrder.map((serial, i) =>
    '<span><span class="dot" style="background:' + cssVar(SERIES_COLORS[i % SERIES_COLORS.length]) + '"></span>' + inverterLabel(serial) + '</span>'
  ).join('');
}

function renderCharts() {
  const viewOpts = { viewTMin: viewTMin, viewTMax: viewTMax };
  const resetBtn = document.getElementById('reset-zoom');
  resetBtn.style.display = (viewTMin !== null || viewTMax !== null) ? '' : 'none';

  const socPoints = history.filter(s => s.soc_pct !== null).map(s => ({ t: s.t, v: s.soc_pct }));
  drawChart(chartSoc, [{ label: 'SOC', color: cssVar('--series-1'), points: socPoints }],
    Object.assign({ yMin: 0, yMax: 100, yFormat: fmtPct }, viewOpts));

  const batteryPoints = history.filter(s => s.battery_power_w !== null && s.battery_power_w !== undefined)
                                .map(s => ({ t: s.t, v: s.battery_power_w }));
  drawChart(chartGrid, [
    { label: 'Reseau (brut)', color: cssVar('--series-1'), points: history.map(s => ({ t: s.t, v: s.grid_raw_w })) },
    { label: 'Reseau (EMA)', color: cssVar('--series-2'), points: history.map(s => ({ t: s.t, v: s.grid_ema_w })) },
    { label: 'Batterie', color: cssVar('--series-3'), points: batteryPoints },
  ], Object.assign({ yFormat: fmtW, includeZero: true }, viewOpts));

  history.forEach(s => (s.inverters || []).forEach(inv => {
    if (!inverterOrder.includes(inv.serial)) inverterOrder.push(inv.serial);
    if (inv.name) inverterNames[inv.serial] = inv.name;
  }));
  const invSeries = inverterOrder.map((serial, i) => ({
    label: inverterLabel(serial),
    color: cssVar(SERIES_COLORS[i % SERIES_COLORS.length]),
    points: history.filter(s => (s.inverters || []).some(inv => inv.serial === serial))
                   .map(s => ({ t: s.t, v: s.inverters.find(inv => inv.serial === serial).actual_w })),
  }));
  drawChart(chartInverters, invSeries, Object.assign({ yFormat: fmtW }, viewOpts));
  renderInvertersLegend();

  drawBarChart(
    chartEnergy, hourlyEnergy, ['from_kwh', 'to_kwh'],
    [cssVar('--series-1'), cssVar('--series-2')], ['Soutiree', 'Injectee'],
    { yFormat: fmtKwh }
  );
}

function poll() {
  fetch('/status.json?since=' + lastT)
    .then(r => r.json())
    .then(data => {
      document.getElementById('conn-status').textContent = 'Connecte -- mise a jour toutes les 2s';
      if (data.history.length) {
        history.push.apply(history, data.history);
        if (history.length > MAX_POINTS) history.splice(0, history.length - MAX_POINTS);
        lastT = data.history[data.history.length - 1].t;
      }
      if (data.hourly_energy) hourlyEnergy = data.hourly_energy;
      renderTiles(data.latest);
      renderInverterTable(data.latest);
      renderCharts();
      renderControls(data);
    })
    .catch(() => { document.getElementById('conn-status').textContent = 'Connexion perdue, nouvel essai...'; });
}

window.addEventListener('resize', renderCharts);
poll();
setInterval(poll, 2000);
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
    names = form.get("inverter_name", [])
    inverters = []
    for i, serial in enumerate(serials):
        serial = serial.strip()
        if not serial:
            continue
        name = names[i].strip() if i < len(names) else ""
        inverters.append(
            {"serial": serial, "nominal_power_w": float(powers[i]), "name": name or None}
        )

    raw = {
        "opendtu": {
            "base_url": first("opendtu.base_url").strip(),
            "username": first("opendtu.username").strip() or None,
            "password": first("opendtu.password") or None,
        },
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
            "min_inverter_pct": float(first("control.min_inverter_pct", "10")),
        },
        "capacity_probe": {
            "step_w": float(first("capacity_probe.step_w", "10")),
            "interval_s": float(first("capacity_probe.interval_s", "30")),
        },
        "battery": {
            "enabled": "battery.enabled" in form,
            "activate_at_pct": float(first("battery.activate_at_pct", "100")),
            "deactivate_below_pct": float(first("battery.deactivate_below_pct", "98")),
            "export_confirms_full_w": float(first("battery.export_confirms_full_w", "50")),
        },
        "web": {
            "enabled": "web.enabled" in form,
            "port": int(float(first("web.port", "8080"))),
        },
        "logging": {
            "verbose_traces": "logging.verbose_traces" in form,
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


def _make_handler(config_path: str, live_state, energy_history, manual_override, injection_mode):
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
            if parsed.path == "/status.json":
                self._handle_status(parse_qs(parsed.query))
                return
            if parsed.path == "/dashboard":
                self._send_html(_render_dashboard_page())
                return
            if parsed.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            self._send_html(_render_page(_load_raw(config_path)))

        def _handle_status(self, query: dict) -> None:
            try:
                since = float((query.get("since") or ["0"])[0])
            except ValueError:
                since = 0.0
            payload = live_state.snapshot_since(since)
            payload["hourly_energy"] = energy_history.snapshot()
            payload["manual_override"] = manual_override.snapshot()
            payload["injection_mode"] = injection_mode.get_mode()
            self._send_json(payload)

        def _handle_fetch_inverters(self, query: dict) -> None:
            base_url = (query.get("base_url") or [""])[0].strip()
            if not base_url:
                self._send_json({"error": "URL OpenDTU manquante"}, status=400)
                return
            username = (query.get("username") or [""])[0].strip() or None
            password = (query.get("password") or [""])[0] or None
            client = OpenDTUClient(base_url, timeout_s=5.0, username=username, password=password)
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

        def _handle_set_override_pct(self, form: dict) -> None:
            try:
                pct = float((form.get("pct") or ["0"])[0])
            except ValueError:
                self._send_json({"error": "pct invalide"}, status=400)
                return
            if pct not in (25.0, 50.0, 75.0, 100.0):
                self._send_json({"error": "pct doit etre 25, 50, 75 ou 100"}, status=400)
                return
            manual_override.set(pct)
            log.warning(
                "forcage manuel demande via la page de config: %.0f%% pendant %d min",
                pct,
                int(DEFAULT_DURATION_S // 60),
            )
            self._send_json({"ok": True, "override": manual_override.snapshot()})

        def _handle_set_injection_mode(self, form: dict) -> None:
            mode = (form.get("mode") or ["AUTO"])[0]
            try:
                injection_mode.set_mode(mode)
            except ValueError:
                self._send_json({"error": "mode invalide"}, status=400)
                return
            log.warning("mode de regulation change via la page de config: %s", mode)
            self._send_json({"ok": True, "mode": injection_mode.get_mode()})

        def do_POST(self) -> None:  # noqa: N802 - required BaseHTTPRequestHandler method name
            if self.path not in ("/save", "/apply", "/override/pct", "/override/pct/clear", "/override/mode"):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            form = parse_qs(body, keep_blank_values=True)

            if self.path == "/override/pct":
                self._handle_set_override_pct(form)
                return
            if self.path == "/override/pct/clear":
                manual_override.clear()
                self._send_json({"ok": True})
                return
            if self.path == "/override/mode":
                self._handle_set_injection_mode(form)
                return

            raw: dict = {}
            try:
                raw = _form_to_raw(form)
                parse_config(raw)  # validate before writing
                _write_raw(config_path, raw)
            except (ValueError, TypeError) as exc:
                self._send_html(_render_page(raw, error=str(exc)), status=400)
                return

            if self.path == "/apply":
                self._send_html(
                    _render_page(
                        raw, message="Configuration enregistree, redemarrage du service en cours..."
                    )
                )
                log.warning(
                    "redemarrage demande via la page de configuration (bouton appliquer) -- "
                    "le superviseur du service va le relancer"
                )
                # Delayed so the response above has time to flush to the client's
                # socket before the process exits; os._exit skips normal Python
                # cleanup/socket shutdown, which could otherwise truncate it.
                threading.Timer(0.5, os._exit, args=(1,)).start()
                return

            self._send_html(
                _render_page(raw, message="Configuration enregistree. Redemarrez le service pour l'appliquer.")
            )

    return ConfigHandler


def start_webui_server(
    config_path: str, port: int, live_state, energy_history, manual_override, injection_mode
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(
        ("0.0.0.0", port), _make_handler(config_path, live_state, energy_history, manual_override, injection_mode)
    )
    thread = threading.Thread(target=server.serve_forever, name="gx-opendtu-webui", daemon=True)
    thread.start()
    return server
