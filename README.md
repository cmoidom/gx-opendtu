# gx-opendtu — zero-injection PV controller

Empêche toute injection réseau ("zero export") sur une installation
photovoltaïque monophasée équipée de micro-onduleurs Hoymiles pilotés par
[OpenDTU](https://github.com/tbnobody/OpenDTU), en s'appuyant sur un Victron
Cerbo GX (Venus OS) et son compteur réseau **VM-3P75CT**.

Deux modes de déploiement possibles : **directement sur le Cerbo GX**
(lecture D-Bus locale), ou **sur une VM Linux séparée** sur le même réseau
(lecture de la puissance réseau via Modbus TCP). Dans les deux cas, la
communication avec OpenDTU se fait exclusivement en HTTP (pas de MQTT).

Voir [`ARCHITECTURE.md`](ARCHITECTURE.md) pour le détail de la conception, et
[`AGENTS.md`](AGENTS.md) pour les conventions à respecter en cas de reprise du
code par un agent IA.

## Fonctionnement en un coup d'œil

1. **Lecture** de la puissance réseau instantanée (positif = soutirage,
   négatif = injection), par l'une des deux voies (`grid.source` en config) :
   - `dbus` (`src/grid_meter.py`) : D-Bus local `com.victronenergy.grid.*` →
     `/Ac/Power` — uniquement si le service tourne sur le Cerbo GX lui-même.
   - `modbus` (`src/grid_meter_modbus.py`) : Modbus TCP à distance, unit ID
     100 (`com.victronenergy.system`, agrégat toujours disponible), registre
     820 = puissance active Grid L1 — pour un déploiement sur une VM séparée.
2. **Décision** (`src/controller.py`) : boucle PI, quantifiée par paliers
   (100 W ou 10 % du parc, la plus grande des deux), limitée en rampe à un
   palier par cycle de décision — asservissement doux, peu de requêtes HTTP.
3. **Répartition** (`src/allocator.py`) : la puissance cible totale est
   répartie de façon égalitaire entre les onduleurs, avec redistribution
   automatique (water-filling) quand un onduleur ne peut pas suivre sa part
   (ombre, sous-production).
4. **Commande** (`src/opendtu_client.py`) : écriture des limites via
   `POST /api/limit/config` (types non-persistants uniquement, pas d'usure
   flash), lecture via `GET /api/livedata/status` et `GET /api/limit/status`.
5. **Repli sécurité** (`src/main.py`) : en cas de perte du compteur réseau ou
   d'OpenDTU injoignable, tous les onduleurs sont ramenés à 0 % en attendant le
   rétablissement de la communication.
6. **Priorité charge batterie** (optionnel, `battery.enabled`) : tant que le
   SOC batterie n'a pas atteint 100 %, le contrôle d'injection est désactivé
   (onduleurs débloqués à 100 %) pour laisser l'ESS Victron charger la
   batterie avec le surplus PV. Une fois 100 % atteint, le contrôle
   d'injection reste actif jusqu'à ce que le SOC repasse sous 98 % — avec
   hystérésis pour éviter les allers-retours. Voir `ARCHITECTURE.md`.

## Prérequis

- Compteur réseau Victron reconnu nativement (`com.victronenergy.grid.*` sur
  D-Bus) — installation **monophasée**.
- OpenDTU déjà flashé et configuré, joignable en HTTP sur le réseau local,
  API sans authentification (ou à adapter si Basic Auth activée — non
  implémenté actuellement, voir `src/opendtu_client.py`).

Selon le mode de déploiement choisi :

- **Sur le Cerbo GX** (`grid.source = "dbus"`) : Venus OS ≥ v2.80 (Python3 +
  `dbus-python` préinstallés), [SetupHelper](https://github.com/kwindrem/SetupHelper)
  installé pour un déploiement persistant (recommandé), ou installation
  manuelle (voir plus bas).
- **Sur une VM Linux séparée** (`grid.source = "modbus"`) : Modbus TCP activé
  sur le Cerbo GX (Settings > Services > Modbus/TCP, port 502 par défaut),
  Python3 + `pymodbus` (`pip install -r requirements.txt`) sur la VM, réseau
  IP entre la VM et le Cerbo GX/OpenDTU.

## Configuration

Deux exemples selon le mode de déploiement :
- `config/config.example.json` — sur le Cerbo GX (`grid.source = "dbus"`).
- `config/config.example.vm-modbus.json` — sur une VM séparée
  (`grid.source = "modbus"`, avec `grid.modbus.host` = IP du Cerbo GX).

Copier celui qui correspond, puis l'adapter (URL OpenDTU, numéros de série et
puissance nominale de chaque onduleur, gains PI, paliers) — voir
`ARCHITECTURE.md` pour la signification de chaque paramètre.

Pour activer la priorité de charge batterie, passer `battery.enabled` à
`true` (désactivé par défaut, comportement inchangé sinon) :
```json
"battery": { "enabled": true, "activate_at_pct": 100, "deactivate_below_pct": 98 }
```

## Installation

### Via SetupHelper (recommandé, persiste après mise à jour firmware)

```sh
# Sur le Cerbo GX, une fois SetupHelper installé :
cd /data
git clone https://github.com/cmoidom/gx-opendtu.git gx-opendtu-zero-export
cd gx-opendtu-zero-export
./setup
```

Le script `setup` copie `config/config.example.json` vers
`/data/gx-opendtu-zero-export/config/config.json` s'il n'existe pas encore —
**pensez à l'éditer avant de démarrer le service**.

> Le script `setup` suit la convention documentée de SetupHelper mais n'a pas
> été validé sur un Cerbo GX réel dans le cadre de ce projet — vérifiez-le
> contre `PackageDevelopmentGuidelines.md` du dépôt SetupHelper avant un
> déploiement sans supervision.

### Installation manuelle (test rapide, ne survit pas à une mise à jour firmware)

```sh
mkdir -p /data/gx-opendtu-zero-export
cp -r . /data/gx-opendtu-zero-export
cp config/config.example.json /data/gx-opendtu-zero-export/config/config.json
# éditer la config, puis :
ln -s /data/gx-opendtu-zero-export/services/gx-opendtu-zero-export /service/gx-opendtu-zero-export
```
daemontools (déjà actif sur Venus OS) prend le service en charge en quelques
secondes et le redémarre automatiquement en cas de crash.

### Sur une VM Linux séparée (Debian/Ubuntu + systemd)

Sur le Cerbo GX : activer **Settings > Services > Modbus/TCP**.

Sur la VM :

```sh
sudo useradd --system --home /opt/gx-opendtu --shell /usr/sbin/nologin gx-opendtu
sudo mkdir -p /opt/gx-opendtu /etc/gx-opendtu
sudo git clone https://github.com/cmoidom/gx-opendtu.git /opt/gx-opendtu
cd /opt/gx-opendtu
sudo python3 -m venv .venv
sudo ./.venv/bin/pip install -r requirements.txt
sudo cp config/config.example.vm-modbus.json /etc/gx-opendtu/config.json
# éditer /etc/gx-opendtu/config.json : IP du Cerbo GX (grid.modbus.host),
# URL OpenDTU, numéros de série et puissances nominales des onduleurs
sudo chown -R gx-opendtu:gx-opendtu /opt/gx-opendtu /etc/gx-opendtu

sudo cp deploy/systemd/gx-opendtu-zero-export.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gx-opendtu-zero-export
journalctl -u gx-opendtu-zero-export -f
```

## Mode test (`--dry-run`)

Depuis la racine du projet (important : lancer via `-m src.main`, pas
`python3 src/main.py`, sinon les imports internes `from src...` échouent
avec `ModuleNotFoundError: No module named 'src'`) :

```sh
python3 -m src.main --config config/config.json --dry-run
```

Le service tourne normalement (lecture du compteur réseau, lecture SOC
batterie si activé, lecture OpenDTU) mais **n'envoie jamais rien à OpenDTU**
(ni limite, ni repli sécurité, ni déblocage charge batterie). Chaque cycle de
décision trace l'état complet, que ça change ou non :

```
[DRY-RUN] grid_meter=+120W opendtu_actual=380W soc=87% injection_control=ON consigne=400W allocation={'114181801234': 240, '114181805678': 160} changed=True (rien envoye)
```

ou, si `battery.enabled` et batterie pas encore pleine :

```
[DRY-RUN] soc=94% grid_meter=+45W injection_control=OFF (charge batterie prioritaire) (rien envoye)
```

- `grid_meter` : valeur lue (moyennée) sur le compteur réseau Victron.
- `opendtu_actual` : puissance AC actuellement mesurée par OpenDTU sur
  l'ensemble des onduleurs.
- `soc` : SOC batterie (uniquement si `battery.enabled`).
- `injection_control` : `ON` (asservissement zero-export actif) ou `OFF`
  (batterie pas encore pleine, onduleurs débloqués à 100 %).
- `consigne` : la puissance totale que le contrôleur enverrait, avec le
  détail de répartition par onduleur (`allocation`).

Ces mêmes informations sont tracées en mode normal (sans `--dry-run`), à
chaque cycle de décision — seule l'écriture vers OpenDTU reste conditionnée
à un changement réel (`changed=True`), pas le log.

Utile pour valider l'asservissement sur une installation réelle avant de le
laisser piloter effectivement les onduleurs.

## Tests

Logique pure (PI, quantification, rampe, water-filling) et logique de la
boucle de décision (via un faux client OpenDTU, sans HTTP réel), testables
sans matériel Victron/OpenDTU :

```sh
python -m pytest tests -q
```

Seuls les appels réseau réels (D-Bus dans `src/grid_meter.py`, Modbus TCP
dans `src/grid_meter_modbus.py`, HTTP dans `src/opendtu_client.py`) restent
non couverts par ces tests — ils nécessitent soit le matériel réel, soit un
harnais de simulation dédié. Le mode `--dry-run` ci-dessus est le moyen
recommandé de valider le comportement sur l'installation réelle sans risque.

## Dépannage

- Logs du service : `svlogd` écrit sous `/var/log/gx-opendtu-zero-export/`
  (Cerbo GX) ou `journalctl -u gx-opendtu-zero-export` (VM/systemd).
- `limit_set_status` reste sur `Pending` : latence RF normale (secondes), si
  ça persiste vérifier la portée radio entre le récepteur OpenDTU et les
  onduleurs.
- La puissance réseau ne converge pas vers le setpoint : vérifier le signe
  (`/Ac/Power` doit être positif en soutirage) et les numéros de série/puissances
  nominales déclarés dans la config.
- Déploiement VM/Modbus : `Connection refused` → Modbus/TCP pas activé sur le
  Cerbo GX (Settings > Services) ou pare-feu bloquant le port 502 ; valeur
  toujours à 0 ou aberrante → vérifier `grid.modbus.unit_id` (100 = agrégat
  système, ne pas confondre avec l'instance VRM du compteur lui-même).
- `injection_control=OFF` qui ne repasse jamais à `ON` : le SOC n'a pas
  encore atteint `battery.activate_at_pct` (100 % par défaut) — c'est le
  comportement voulu (priorité charge batterie), pas un bug. Vérifier le
  SOC tracé dans les logs.
