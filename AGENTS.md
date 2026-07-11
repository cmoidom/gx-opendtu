# AGENTS.md

Conventions pour tout agent IA (ou humain) qui reprend ce dépôt. Voir
[`ARCHITECTURE.md`](ARCHITECTURE.md) pour la conception détaillée avant de
modifier quoi que ce soit.

## Invariants à ne pas casser

- **Signe de la puissance réseau** : `/Ac/Power` positif = soutirage,
  négatif = injection (`src/grid_meter.py`, `src/controller.py`). Ne pas
  inverser sans mettre à jour toute la chaîne de signes dans
  `SoftTargetController.compute_target`.
- **Installation monophasée** : le code ne lit/n'écrit qu'une seule valeur de
  puissance réseau (`/Ac/Power` en D-Bus, registre 820 en Modbus). Si le
  projet évolue vers du triphasé, ça touche `grid_meter.py`/
  `grid_meter_modbus.py` (lecture par phase) et potentiellement
  `allocator.py` (éviter l'export sur une phase isolée) — ce n'est pas un
  simple ajout.
- **Deux modes de déploiement, deux régimes de dépendances** : sur le Cerbo
  GX (`grid.source = "dbus"`), zéro dépendance externe dans `src/`
  (`urllib`/`json`/`dbus` stdlib ou préinstallés sur Venus OS uniquement —
  accès internet non garanti, flash limité, ne pas introduire `requests`,
  `paho-mqtt`, etc. sans en discuter d'abord). Sur une VM séparée
  (`grid.source = "modbus"`), `pymodbus` est acceptable (`requirements.txt`,
  `src/grid_meter_modbus.py`) car cette contrainte ne s'applique pas — mais
  ne pas le laisser fuiter en import de niveau module dans un fichier
  partagé entre les deux modes (voir plus bas).
- **Modbus TCP : unit ID 100, registre 820, jamais l'instance VRM du
  compteur** (`src/grid_meter_modbus.py`). Unit ID 100 = service agrégat
  `com.victronenergy.system`, toujours disponible sans configuration par
  site ; ne pas remplacer par l'unit ID du compteur lui-même (variable par
  installation) sans une bonne raison. Les registres pymodbus sont non
  signés (`uint16`) — toute lecture doit repasser par `_to_signed_int16`
  pour voir correctement les valeurs négatives (export).
- **Types de limite OpenDTU non-persistants uniquement**
  (`LIMIT_TYPE_ABSOLUTE_NONPERSISTENT` / `..._RELATIVE_NONPERSISTENT` dans
  `src/opendtu_client.py`). Les variants persistants (256/257) écrivent en
  flash côté onduleur — ne pas les utiliser dans une boucle qui tourne toutes
  les quelques secondes.
- **Asservissement doux et peu bavard** : toute modification de
  `SoftTargetController` doit préserver la quantification par palier
  (`max(step_absolute_w, step_relative_pct%)`) et la rampe (1 palier par
  cycle de décision maximum). C'est une exigence explicite de l'utilisateur,
  pas un détail d'implémentation — ne pas revenir à un envoi de commande à
  chaque tick "pour plus de réactivité" sans validation explicite.
- **Fail-safe** : toute perte de communication (D-Bus ou OpenDTU) doit
  ramener les onduleurs à une limite basse et sûre plutôt que de laisser le
  service "en roue libre" (`_apply_failsafe` dans `src/main.py`).

## Frontière testable / non testable

- `src/allocator.py` et `src/controller.py` sont **purs** (pas d'I/O) : tout
  ajout de logique doit rester testable unitairement, sans mock de D-Bus ou
  de réseau. C'est ce qui permet à `tests/` de tourner sans matériel Victron
  ni OpenDTU réel.
- `src/main._decision_cycle` / `_apply_failsafe` sont testés via un **faux
  client OpenDTU** (duck-typing, `tests/test_dry_run.py`) — pas de vrai HTTP.
  C'est pour ça que `src/grid_meter.py` importe `dbus` à l'intérieur des
  fonctions et non au niveau module : si cet import redevient un import de
  module (`import dbus` en tête de fichier), `src.main` redevient
  impossible à importer sur une machine sans `dbus-python`, et
  `test_dry_run.py` casse à la collecte. Ne pas "remonter" cet import sans
  garder ce test en tête.
- Seuls les appels **réellement réseau** (le vrai D-Bus dans
  `grid_meter.read_grid_power_w`, le vrai Modbus TCP dans
  `grid_meter_modbus.ModbusGridMeter._connected_client`, le vrai `urllib`
  dans `opendtu_client.OpenDTUClient`) restent non couverts par les tests
  actuels — ils nécessitent soit le matériel réel, soit un harnais de
  simulation dédié, soit le mode `--dry-run` sur le matériel réel. La
  conversion `_to_signed_int16` de `grid_meter_modbus.py` est en revanche
  pure et testée (`tests/test_grid_meter_modbus.py`) — tout nouveau bout de
  logique pure ajouté à ce fichier doit suivre le même principe plutôt que
  d'être mélangé avec l'appel réseau. Ne pas prétendre qu'un changement dans
  les trois modules réseau est "vérifié" sans l'un des trois moyens
  ci-dessus.
- `src/grid_meter_modbus.py` importe `pymodbus` à l'intérieur des méthodes
  (jamais au niveau module), pour la même raison que `dbus` dans
  `grid_meter.py` : garder `src.main` et `src.grid_meter_modbus` importables
  sur une machine sans `pymodbus` installé (utile en mode `dbus`/Cerbo GX,
  et pour que `tests/test_grid_meter_modbus.py` puisse tourner sans lui).
- Avant d'affirmer qu'un détail de l'API OpenDTU ou de packaging Venus
  OS/SetupHelper est correct, vérifier contre la version réellement installée
  sur le Cerbo GX cible : ce projet a été conçu à partir de documentation
  publique (voir sources citées dans `ARCHITECTURE.md`) mais **jamais testé
  sur du matériel réel**. Le script `setup` en particulier est un
  best-effort suivant la convention documentée de SetupHelper, non validé en
  conditions réelles.

## Style

- Pas de commentaires expliquant le "quoi" ; uniquement le "pourquoi" quand
  ce n'est pas évident (voir les commentaires existants dans
  `controller.py`/`opendtu_client.py` pour le ton attendu).
- Fins de ligne **LF forcées** via `.gitattributes` — ne pas désactiver, les
  scripts shell (`setup`, `services/**/run`) doivent rester exécutables tels
  quels une fois copiés sur Venus OS (Linux).
