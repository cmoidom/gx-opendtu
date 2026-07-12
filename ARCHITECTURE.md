# Architecture

## Contexte et contraintes

- Installation **monophasée** : une seule valeur de puissance réseau à
  surveiller (`/Ac/Power`), pas de logique par phase.
- Deux modes de déploiement supportés (sélectionnés par `grid.source` en
  config, `src/main._make_grid_reader`) :
  - **Sur le Cerbo GX lui-même** (Venus OS), lecture D-Bus locale — pas de
    matériel intermédiaire.
  - **Sur une VM Linux séparée** (ex. Proxmox), lecture de la puissance
    réseau via **Modbus TCP** à distance — pas de contrainte flash/no-deps
    de Venus OS sur cette VM, donc `pymodbus` y est une dépendance acceptable
    (`requirements.txt`), contrairement au reste du projet.
- OpenDTU est piloté **exclusivement en HTTP REST**, jamais en MQTT.
- L'environnement Venus OS est contraint : uniquement `/data` est persistant
  (le reste du système de fichiers est en lecture seule/overlay), pas d'accès
  internet garanti sur l'appareil, dépendances Python à minimiser. D'où le
  choix de `urllib` (stdlib) plutôt que `requests` pour le client OpenDTU.
- Le compteur réseau Victron VM-3P75CT s'enregistre nativement sur le D-Bus
  comme n'importe quel `com.victronenergy.grid.*`.

## Composants

```
gx-opendtu/
├── src/
│   ├── main.py             boucle principale (lecture rapide / décision lente)
│   ├── config.py           chargement + validation config JSON (dataclasses)
│   ├── grid_meter.py       DbusGridMeter: D-Bus com.victronenergy.grid.*  /Ac/Power
│   ├── grid_meter_modbus.py ModbusGridMeter: Modbus TCP, unit ID 100, registre 820
│   ├── battery_soc.py      DbusBatterySoc: D-Bus com.victronenergy.system /Dc/Battery/Soc
│   ├── battery_soc_modbus.py ModbusBatterySoc: Modbus TCP, unit ID 100, registre 843
│   ├── opendtu_client.py   client HTTP OpenDTU (urllib stdlib, zéro dépendance)
│   ├── controller.py       PI + lissage + quantification + rampe + capacité + hystérésis batterie
│   ├── allocator.py        répartition water-filling multi-onduleurs (pure)
│   ├── live_state.py       LiveState: buffer circulaire en mémoire pour le tableau de bord
│   └── webui.py            pages web config ("/") et tableau de bord ("/dashboard")
├── config/config.example.json           déploiement Cerbo GX (grid.source=dbus)
├── config/config.example.vm-modbus.json déploiement VM (grid.source=modbus)
├── deploy/systemd/gx-opendtu-zero-export.service   service pour VM Linux
├── requirements.txt        pymodbus (VM uniquement, jamais sur Venus OS)
├── tests/                  tests de la logique pure (controller, allocator, ...)
├── services/gx-opendtu-zero-export/   service daemontools (run, log/run) - Cerbo GX
├── version / setup / gitHubInfo        packaging SetupHelper - Cerbo GX
```

`allocator.py` et la partie logique de `controller.py` sont **purement
fonctionnels** (pas d'I/O) : c'est ce qui les rend testables sans matériel.
`grid_meter.py`, `grid_meter_modbus.py` et `opendtu_client.py` sont
volontairement de fins wrappers d'I/O, sans logique métier, pour garder
cette séparation. `main._make_grid_reader(config)` choisit `DbusGridMeter`
ou `ModbusGridMeter` selon `config.grid.source` ; les deux exposent la même
interface `read_grid_power_w() -> float`, donc `main.run()` ne connaît pas
la différence.

`webui.py` démarre un `http.server.ThreadingHTTPServer` (stdlib, pas de
dépendance) dans un thread daemon, sur `config.web.port` (8080 par défaut,
`config.web.enabled` pour désactiver). Il lit/écrit directement
`config.json` (y compris ajout/suppression d'onduleurs) mais ne touche pas à
l'état en mémoire de la boucle de contrôle : un enregistrement écrit le
fichier et affiche un message, sans redémarrer le service — cohérent avec le
choix de ne rien recharger à chaud (voir README, section configuration).
Pas d'authentification (comme l'API OpenDTU) : accessible à quiconque sur le
LAN.

"Enregistrer et appliquer" (`POST /apply`) valide et écrit la config comme
`/save`, puis appelle `os._exit(1)` (via un `threading.Timer` de 0.5 s pour
laisser la réponse HTTP partir avant que le process ne meure) : pas de
hot-reload en mémoire, on relance tout le process et on laisse le
superviseur (`services/` daemontools sur le Cerbo GX, `deploy/systemd/`
ailleurs) le redémarrer avec la nouvelle config au prochain `load_config()`.
Code de sortie 1 (pas 0) pour rester compatible avec `Restart=on-failure`
dans le fichier systemd fourni — daemontools redémarre de toute façon quel
que soit le code de sortie.

Le bouton de découverte des onduleurs appelle `GET /fetch-inverters?base_url=...`
côté serveur webui (pas d'appel direct navigateur → OpenDTU, donc pas de
souci CORS), qui délègue à `OpenDTUClient.list_inverters()`
(`src/opendtu_client.py`) : combine `/api/livedata/status` (serial, name) et
`/api/limit/status` (max_power) — il n'existe pas d'endpoint
`/api/inverter/list` dans le firmware OpenDTU standard. Identifiant/mot de
passe actuellement saisis dans le formulaire (section OpenDTU) sont passés
en query string à `GET /fetch-inverters` puis à `OpenDTUClient`, comme pour
`opendtu.base_url` -- avant sauvegarde, donc.

`OpenDTUClient` (`src/opendtu_client.py`) envoie un en-tête
`Authorization: Basic ...` sur **chaque** requête (GET et POST) dès que
`username` est renseigné (`password` optionnel, chaîne vide sinon) --
OpenDTU ignore simplement cet en-tête sur les endpoints qui n'en ont pas
besoin, donc pas de branchement par endpoint. Nécessaire dès que
`/api/limit/config` (écriture, "Settings API" côté OpenDTU) exige une
authentification -- ce qui est courant même quand les endpoints de lecture
(`/api/livedata/status`, `/api/limit/status`) n'en demandent pas. Sans ces
identifiants dans ce cas, toute écriture échoue en 401 -- y compris le
repli fail-safe, qui ne peut alors plus curtailer les onduleurs.

`live_state.py` (`LiveState`) est un buffer circulaire thread-safe
(`collections.deque`, ~900 échantillons par défaut, soit environ 30 min au
`grid.read_interval_s` par défaut de 2s) rempli par `main.run()` à chaque
tick de la boucle rapide (`record_grid`, toujours) et à chaque cycle de
décision (`update_decision`, soc/injection_control/consigne/onduleurs,
reporté sur chaque échantillon rapide jusqu'au prochain cycle de décision).
`webui.py` le lit seulement, jamais ne le modifie -- expose
`GET /status.json?since=<epoch>` (récupération incrémentale : historique
complet si `since` omis/0, sinon seulement les échantillons plus récents) et
la page `/dashboard`, qui interroge cet endpoint toutes les 2s et dessine
trois graphiques en `<canvas>` fait main (pas de librairie -- même
contrainte "pas d'accès internet garanti" que le reste du projet). L'état
est perdu à chaque redémarrage du service (y compris via "Enregistrer et
appliquer") : c'est une vue en direct, pas un historique persistant.

Quand `injection_control=OFF` (charge batterie prioritaire), `main.run()`
n'appelle pas `_decision_cycle` -- il n'y a donc pas d'allocation/limite à
reporter. Plutôt que de laisser `inverters=[]` (graphique et tableau vides
en permanence tout le temps que dure la charge, ce qui ressemble à une
panne), `_off_state_inverters_payload` (`src/main.py`) lit quand même
`client.get_live_power_w()` et construit une entrée par onduleur avec
`allocated_w=None` et `acknowledged=None` (pas de commande active à
rapporter) mais `actual_w` réel et `limit_relative_pct=100` (débridé) --
`webui.py` affiche ces `None` comme "débridé (charge batterie)" plutôt que
de les confondre avec `acknowledged=False` ("en attente RF") ou un état
"ok" normal. Un échec de cette lecture (`OpenDTUError`) redonne `[]` sans
perturber la boucle de déblocage à 100%, qui reste la priorité.

Les graduations de l'axe Y de chaque graphique utilisent l'algorithme
"nice numbers" de Heckbert (`niceNum`/`niceScale` dans `webui.py`) : le pas
et les bornes sont toujours arrondis à 1/2/5 x 10^n (50, 100, 200, 500...),
jamais des valeurs arbitraires issues de la division brute de la plage de
données. Le graphique "Puissance réseau" force en plus `includeZero` pour
que la ligne 0 W reste toujours visible, même quand toutes les valeurs de
la fenêtre affichée sont positives.

`config.logging.verbose_traces` (défaut `true`) ne gate que la ligne d'état
répétée à chaque cycle de décision dans `main._decision_cycle` et dans la
branche OFF de `main.run()` (`log.info("grid_meter=...")`) -- toujours
indépendante de `LiveState`, qui est mis à jour dans les deux cas quel que
soit ce réglage. Les logs d'erreur/avertissement et les actions ponctuelles
(fail-safe, déblocage charge batterie, redémarrage via "Enregistrer et
appliquer") ne sont jamais concernés par ce réglage.

## Convention de signe

`/Ac/Power` du compteur réseau : **positif = soutirage réseau (import),
négatif = injection réseau (export)**. La cible de régulation est de
maintenir cette valeur autour d'un petit seuil positif (`export_setpoint_w`,
défaut 30 W) — jamais négative — pour absorber le bruit de mesure et la
latence de la boucle sans jamais réellement basculer en export.

## Boucle de contrôle

Deux cadences découplées, exécutées dans une seule boucle Python
(`src/main.py`), pas de threads :

- **Lecture** (`grid.read_interval_s`, défaut 2 s) : lit `/Ac/Power`, alimente
  un filtre exponentiel (`GridPowerSmoother`, `filtered += ema_alpha *
  (raw - filtered)`, `grid.ema_alpha` défaut 0,5) pour amortir le bruit de
  mesure sans ajouter la discontinuité qu'une moyenne mobile à fenêtre fixe
  provoque quand un vieil échantillon en sort brutalement. `ema_alpha` plus
  haut = réaction plus rapide à un vrai à-coup de charge (au prix de plus de
  bruit résiduel) ; plus bas = plus lisse mais plus lent. Constante de temps
  approximative : `read_interval_s / ema_alpha` (≈4 s avec les défauts).
- **Décision** (`control.decision_interval_s`, défaut 5 s) : c'est le seul
  moment où des requêtes HTTP peuvent partir vers OpenDTU.

**Pourquoi 5 s, et pourquoi ne pas chercher à réagir plus vite en logiciel** :
le vrai facteur limitant n'est ni l'intervalle de décision, ni le palier
logiciel (`step_absolute_w`/`step_relative_pct`), c'est la **rampe de
puissance physique de l'onduleur Hoymiles lui-même** — un paramètre du profil
réseau (grid profile), réglable uniquement via l'appli/DTU officiels
Hoymiles, **pas via OpenDTU**. Deux sources communautaires indépendantes
convergent sur un ordre de grandeur d'environ **0,5 %Pn/s ≈ 3 W/s** pour un
onduleur 600 W (confiance moyenne — ça peut varier selon modèle/profil) ; un
swing complet 0→100 % prend alors de l'ordre de ~200 s, pas 5 s. Notre palier
logiciel par défaut (100 W ou 10 % toutes les 5 s, soit jusqu'à ~20 W/s
autorisés) est déjà bien au-dessus de ce que l'onduleur peut physiquement
suivre : le logiciel n'est donc jamais le goulot d'étranglement en pratique,
et l'accélérer n'aurait aucun effet réel sur la vitesse de réaction. La
marge `export_setpoint_w` (+30 W par défaut) absorbe sans risque le délai
d'ajustement de production lors d'un à-coup de charge — c'est son rôle.
Le `ema_alpha` par défaut (0,5, ≈4 s de constante de temps) est choisi pour
que le filtre lui-même n'ajoute pas de retard significatif au-dessus de ce
plancher matériel — il ne le fera pas disparaître pour autant.
Source: discussion communautaire OpenDTU-OnBattery #908 (deux mesures
indépendantes convergentes) ; un rapport isolé (issue OpenDTU #571)
évoquait une asymétrie montée/descente mais n'est pas corroboré et est
probablement spécifique à une version/config particulière — à ne pas
généraliser.

À chaque cycle de décision (`SoftTargetController.compute_target`,
`src/controller.py`) :

```
error            = grid_power_avg - export_setpoint_w
delta            = PI(error)                      # kp*error + intégrale (anti-windup clampée)
current_actual   = somme des puissances AC actuelles (GET /api/livedata/status)
raw_target       = clamp(current_actual + delta, 0, capacité_totale)

step             = max(step_absolute_w, step_relative_pct% * capacité_totale)
quantized        = round(raw_target / step) * step        # palier
next_target      = last_sent + clamp(quantized - last_sent, -step, +step)  # rampe: 1 palier / cycle max

si next_target n'a pas bougé d'au moins min_change_w depuis le dernier envoi :
    -> rien n'est envoyé ce cycle (zéro requête HTTP)
sinon :
    -> répartition + envoi (voir ci-dessous)
```

Le choix de `step = max(absolu, relatif)` garantit un palier minimal en watts
même sur une petite installation, et un palier proportionnellement raisonnable
sur une grosse installation (pas de spam à chaque petite variation de charge).

## Répartition multi-onduleurs (water-filling)

`allocator.water_fill_allocate(total_target_w, serials, capacity_estimates)` :
répartition égalitaire, plafonnée par la capacité connue de chaque onduleur,
avec redistribution itérative du surplus tant qu'il reste des onduleurs non
saturés. Complexité O(n²) dans le pire cas (n = nombre d'onduleurs, en
pratique très petit), un onduleur au moins sort de la boucle à chaque
itération donc pas de risque de boucle infinie.

`capacity_estimates` (`CapacityEstimator`, `src/controller.py`) démarre à la
puissance nominale déclarée en config. Si un onduleur reste durablement
sous sa part allouée alors qu'OpenDTU confirme que la limite est bien
appliquée (`limit_set_status == "Ok"`, donc ce n'est pas la limite qui le
bride), on suppose qu'il est limité par l'irradiance réelle et son plafond est
abaissé à sa production mesurée. Une sonde périodique
(`capacity_probe.step_w` / `interval_s`) relève ce plafond par petits pas vers
le nominal, pour redétecter une amélioration (nuage qui passe) sans jamais le
dépasser.

## API OpenDTU utilisée

- `GET /api/livedata/status` → puissance AC actuelle par `serial`.
- `GET /api/limit/status` → `{serial: {limit_relative, max_power,
  limit_set_status}}`. `limit_set_status` passe `Pending` → `Ok` après
  acquittement RF (latence de quelques secondes, normal pour du sub-GHz).
- `POST /api/limit/config`, form field `data=<json>` :
  `{"serial", "limit_type", "limit_value"}`. Seuls les types **non-persistants**
  sont utilisés (`0` = absolu, `1` = relatif) — les variants persistants
  écrivent en flash côté onduleur et useraient prématurément la mémoire avec
  un asservissement aussi fréquent.
- Pas d'authentification Basic dans cette installation (désactivée côté
  OpenDTU). Si elle venait à être activée, il faudrait étendre
  `OpenDTUClient` pour l'envoyer sur les requêtes GET/POST.
- ⚠️ Un changement de paramètres de `/api/limit/config` a été signalé sur
  certaines versions d'OpenDTU (2025-08-07) : vérifier le format exact contre
  la version réellement installée avant un déploiement.

## Lecture réseau à distance (Modbus TCP, déploiement VM)

`src/grid_meter_modbus.py` (`ModbusGridMeter`) lit la puissance réseau sans
accès D-Bus local, pour un service qui tourne sur une VM séparée :

- Nécessite **Settings > Services > Modbus/TCP** activé sur le Cerbo GX
  (port 502 par défaut).
- Utilise **unit ID 100** = `com.victronenergy.system`, le service agrégat
  système, toujours présent quel que soit le modèle de compteur connecté.
  Alternative volontairement écartée : lire directement le service Modbus du
  compteur (`com.victronenergy.grid.X`) demanderait de connaître son instance
  VRM, qui varie par installation — l'unit ID 100 évite cette configuration
  par site.
- Registre **820** = puissance active Grid L1 (`int16`, échelle 1, W, même
  convention de signe que le D-Bus : négatif = injection). Installation
  monophasée → seul ce registre est lu (pas L2/L3).
- `pymodbus` renvoie les registres en `uint16` non signé : conversion
  explicite en complément à deux (`_to_signed_int16`, testée dans
  `tests/test_grid_meter_modbus.py`) — oublier cette conversion est un bug
  classique qui masquerait silencieusement les valeurs d'export (négatives).
- L'API de `pymodbus` a changé **plusieurs fois** de mot-clé pour l'unit ID
  (`unit=` en 2.x, `slave=` en 3.0-3.7ish, `device_id=` confirmé sur 3.13.1) :
  `_read_holding_registers` essaie `device_id=`, puis `slave=`, puis `unit=`,
  au lieu de figer une version exacte — ce point a déjà cassé une fois en
  déploiement réel avant d'être élargi à trois variantes, voir
  `tests/test_grid_meter_modbus.py::test_read_holding_registers_falls_back_across_pymodbus_versions`.
- `pymodbus` n'est requis que pour ce mode (`requirements.txt`) ; l'import
  est différé à l'intérieur des méthodes, donc `grid_meter_modbus.py` reste
  importable même sans le paquet installé (utile en mode `dbus`/Cerbo GX).

## Sécurité / repli

- Échec de lecture D-Bus répété (`FAILSAFE_AFTER_CONSECUTIVE_FAILURES`,
  `src/main.py`) ou échec de communication OpenDTU : tous les onduleurs sont
  ramenés à 0 % (`_apply_failsafe`). Le service ne reste jamais "en roue
  libre" sans supervision active de la puissance réseau.
- La marge de sécurité `export_setpoint_w` (> 0) garantit que le point de
  fonctionnement visé reste toujours légèrement côté import, jamais export.

## Priorité charge batterie (hystérésis, optionnel)

Motivation : tant que la batterie n'est pas pleine, le surplus PV AC-couplé
(Hoymiles) peut être absorbé par le chargeur de batterie de l'ESS Victron
(Multiplus/Quattro) sans que ce projet ait besoin de brider quoi que ce soit
— seul le cas "batterie pleine" (plus de sink pour le surplus) exige une
vraie curtailment des micro-onduleurs pour ne jamais exporter.

- `src/battery_soc.py` (D-Bus) / `src/battery_soc_modbus.py` (Modbus TCP,
  registre 843, `uint16`, échelle 1, pas de conversion de signe) lisent le
  SOC agrégé système (`com.victronenergy.system` `/Dc/Battery/Soc`), même
  logique que pour la puissance réseau : correct quel que soit le nombre de
  packs/moniteurs batterie physiques, pas de lookup par installation.
- `controller.BatteryFullHysteresis` (pure, testée dans
  `tests/test_battery_hysteresis.py`) : verrou à deux seuils —
  `activate_at_pct` (défaut 100 %) pour passer en `ON`, `deactivate_below_pct`
  (défaut 98 %) pour repasser en `OFF`. La zone morte entre les deux évite le
  yoyo (ex. `SOC=99%` ne fait jamais rien basculer, qu'on vienne d'en dessous
  ou d'en dessus).
- Quand `injection_control=OFF` (SOC pas encore à `activate_at_pct`) :
  `main._release_for_charging` débloque tous les onduleurs à 100 % (limite
  relative non-persistante), une seule fois par transition (pas à chaque
  cycle) — puis le contrôleur zero-export normal (`_decision_cycle`) est
  entièrement sauté tant que l'état reste `OFF`.
- Quand `injection_control=ON` : comportement inchangé, `_decision_cycle`
  tourne normalement (PI + water-filling + quantification).
- **Repli si le SOC est illisible** : on suppose le pire cas pour la
  conformité zero-injection — `injection_control` reste `ON` (comme si la
  batterie était pleine) plutôt que de débloquer les onduleurs sans
  supervision. Ce repli n'altère pas l'état interne du verrou
  (`hysteresis.active`), seulement l'action de ce cycle : au prochain SOC lu
  avec succès, l'hystérésis reprend exactement où elle en était.
- Fonctionnalité **désactivée par défaut** (`battery.enabled = false`) :
  comportement du projet inchangé tant qu'elle n'est pas explicitement
  activée en config.
- Log : chaque cycle de décision trace le SOC (si activé) et l'état
  `injection_control=ON|OFF`, dans les deux modes (`--dry-run` et normal) —
  voir README "Mode test".

## Déploiement

**Sur le Cerbo GX (Venus OS)** : seul `/data` est persistant à travers les
mises à jour firmware. Le packaging suit la convention **SetupHelper**
(kwindrem) : `version` / `setup` / `gitHubInfo` à la racine, service
daemontools sous `services/gx-opendtu-zero-export/{run, log/run}`, auto-lié
dans `/service/` et supervisé par `svscan` (redémarrage automatique).
SetupHelper réinstalle le package automatiquement après une mise à jour
firmware via son hook `reinstallMods`.

**Sur une VM Linux séparée** : unité systemd classique
(`deploy/systemd/gx-opendtu-zero-export.service`), venv Python + `pymodbus`
sous `/opt/gx-opendtu`, config sous `/etc/gx-opendtu/config.json`,
`grid.source = "modbus"`. Pas de contrainte de persistance particulière
(filesystem normal, pas d'overlay Venus OS).

Voir le README pour les commandes détaillées des trois voies d'installation
(SetupHelper, manuelle sur Cerbo GX, VM/systemd).

## Mode dry-run

`main()` accepte `--dry-run` (voir README) : `run(config, dry_run=True)`
traverse `_decision_cycle`/`_apply_failsafe` normalement (lecture D-Bus,
lecture OpenDTU, calcul PI + water-filling) mais n'appelle jamais
`client.set_absolute_limit_w` / `set_relative_limit_pct`. Chaque cycle logue
la valeur du compteur réseau, la production actuelle vue par OpenDTU et la
consigne calculée. C'est le mécanisme de validation recommandé avant de
laisser le service piloter réellement les onduleurs sur une installation
neuve. Testé par `tests/test_dry_run.py` via un faux client OpenDTU (pas de
HTTP réel) qui vérifie qu'aucun appel d'écriture ne part en mode dry-run.

## Limites connues / non couvert

- `src/grid_meter.py` importe `dbus` en différé (à l'intérieur des fonctions,
  pas au niveau module) précisément pour que `src.main` reste importable sur
  une machine de dev sans `dbus-python`, ce qui permet de tester
  `_decision_cycle`/`_apply_failsafe` via un faux client OpenDTU
  (`tests/test_dry_run.py`). Les appels D-Bus réels et le client HTTP réel de
  `opendtu_client.py` restent non couverts par des tests automatisés — ils
  nécessitent le matériel réel ou un harnais de simulation dédié (voir
  README "Tests").
- Conçu pour une installation **monophasée** ; une extension triphasée
  nécessiterait de revoir la lecture du compteur (par phase) et
  potentiellement la logique de répartition (éviter l'export sur une phase
  isolée même si le total est nul).
- Le mode Modbus TCP n'a pas non plus été validé sur un Cerbo GX réel dans
  le cadre de ce projet — les valeurs de registre/unit ID viennent de la
  documentation officielle Victron (feuille de registres Modbus-TCP), à
  confirmer sur le matériel cible avant un déploiement sans supervision.
