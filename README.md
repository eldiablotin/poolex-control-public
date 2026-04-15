# Poolex Control
# French version 

Contrôle complet d'une pompe à chaleur de piscine via son bus RS485,
en utilisant un Raspberry Pi 4 et un adaptateur USB-RS485.

---

## Table des matières

1. [Matériel requis](#1-matériel-requis)
2. [Protocole RS485 — modèle complet](#2-protocole-rs485--modèle-complet)
3. [Méthodologie de reverse engineering](#3-méthodologie-de-reverse-engineering)
4. [Architecture logicielle](#4-architecture-logicielle)
5. [Installation sur le Raspberry Pi](#5-installation-sur-le-raspberry-pi)
6. [Intégration MQTT et Home Assistant](#6-intégration-mqtt-et-home-assistant)
7. [Mise en place GitHub Actions](#7-mise-en-place-github-actions)
8. [Utilisation de l'API](#8-utilisation-de-lapi)
9. [Dépannage](#9-dépannage)

---

## 1. Matériel requis

| Composant | Modèle | Notes |
|-----------|--------|-------|
| Ordinateur mono-carte | Raspberry Pi 4 Rev B | Debian 13 (Trixie), 64 bits |
| Adaptateur USB-RS485 | Waveshare USB to RS232/485 | Puce FTDI FT232RNL |
| Câble USB | USB-A → USB-A (fourni) | |
| Accès au bus RS485 | Bornes A+/B- de la PAC | En parallèle sur la télécommande filaire |

### Branchement

```
Adaptateur Waveshare          Bus RS485 PAC
────────────────────          ─────────────
TX_A  ─────────────────────►  Borne A+
RX_B  ◄─────────────────────  Borne B-
GND   ─────────────────────── Masse (optionnel)
```

**Switch 120Ω :**
- `OFF` si la télécommande filaire est présente (branchement en parallèle)
- `ON` si la télécommande est débranchée (RPi seul sur le bus — terminaison requise)

> Le Waveshare (FT232RNL) gère le basculement DE/RE automatiquement.
> Il **ne renvoie pas l'écho local** en émission : les trames envoyées par le RPi
> n'apparaissent pas dans le flux de réception.

---

## 2. Protocole RS485 — modèle complet

### Paramètres physiques

| Paramètre | Valeur |
|-----------|--------|
| Vitesse | 9600 baud |
| Format | 8N1 (8 bits, pas de parité, 1 stop) |
| Taille de trame | **80 octets fixes** |
| Cadence | ~1 trame/seconde par type |

### Rôles des trames — modèle confirmé avril 2026

> ⚠️ Contrairement à ce qu'on pourrait attendre, **c'est la PAC qui est maître du bus**.
> Elle émet D2 en broadcast avec sa configuration courante.
> La télécommande répond en miroir avec CC (keepalive) et envoie CD pour commander.

| Header | Hex  | Émetteur | Fréquence | Rôle |
|--------|------|----------|-----------|------|
| `DD`   | 0xDD | PAC      | ~1/s      | Statut live (températures, état compresseur) |
| `D2`   | 0xD2 | **PAC**  | ~1/s      | Configuration courante broadcast (consigne, mode, power) |
| `CC`   | 0xCC | Remote   | ~1/s      | Keepalive — miroir de D2, confirme réception |
| `CD`   | 0xCD | Remote   | Sur commande | Commande : change consigne, power, mode |

### Checksum byte[79]

Toutes les trames D2, CC et CD ont un **checksum en byte[79]** :

```
byte[79] = (sum(bytes[0..78]) + 0xAF) & 0xFF
```

> Toute trame modifiée doit recalculer ce checksum. La PAC rejette silencieusement
> les trames avec un checksum incorrect.

Pour DD, byte[79] est un compteur roulant (pas un checksum applicatif).

### Décodage trame DD (statut temps réel PAC → remote)

| Byte   | Décodage                                  | Exemple          | Statut       |
|--------|-------------------------------------------|------------------|--------------|
| `[0]`  | Header = 0xDD                             | —                | ✓            |
| `[20]` | Température air extérieur = valeur ÷ 2 °C | 26 → 13.0°C      | ✓ avr 2026   |
| `[29]` | Température eau piscine = valeur ÷ 10 °C  | 114 → 11.4°C     | ✓ avr 2026   |
| `[3]`  | État compresseur (voir tableau ci-dessous)| 0xa1 → chauffe   | ✓ avr 2026   |
| `[79]` | Compteur roulant                          | —                | ✓            |

**Valeurs DD byte[3] :**

| Valeur | Hex   | Signification       |
|--------|-------|---------------------|
| 161    | 0xa1  | Chauffe active      |
| 33     | 0x21  | Marche / standby    |
| 32     | 0x20  | Arrêt en cours      |
| 0      | 0x00  | Éteint              |

### Décodage trame D2 (configuration courante PAC → remote)

La PAC émet D2 à ~1/s avec sa configuration interne. Quand le RPi modifie
un paramètre (via CD), D2 se met à jour pour refléter le nouvel état.

| Byte   | Décodage                                    |
|--------|---------------------------------------------|
| `[0]`  | Header = 0xD2                               |
| `[1]`  | État + mode (voir tableau modes)             |
| `[4]`  | Sous-mode (0x01 = normal, 0x02 = cooling)   |
| `[11]` | Consigne température (°C)                   |
| `[79]` | Checksum = (sum[0..78] + 0xAF) & 0xFF       |

### Modes de chauffe — byte[1] + byte[4]

| byte[1] | byte[4] | Mode              | bit 0 de byte[1] |
|---------|---------|-------------------|-----------------|
| 0x5B    | 0x01    | **inverter** (on) | 1 = allumé      |
| 0x3B    | 0x01    | **fix** (on)      | 1 = allumé      |
| 0x1B    | 0x01    | **sun** (on)      | 1 = allumé      |
| 0x1B    | 0x02    | **cooling** (on)  | 1 = allumé      |
| 0x5A    | 0x01    | inverter (off)    | 0 = éteint      |
| 0x3A    | 0x01    | fix (off)         | 0 = éteint      |
| …       | …       | …                 | …               |

**Règle :** `byte[1] bit 0 = 1` → allumé, `bit 0 = 0` → éteint.
Les bits 1-7 encodent le mode. Les modes diffèrent par pas de `0x20`.

### Trame CC (keepalive remote → PAC)

CC est le miroir exact de D2 : mêmes octets [1..78], seul le header change.

```
CC[0]  = 0xCC  (à la place de 0xD2)
CC[79] = (sum(CC[0..78]) + 0xAF) & 0xFF
```

La PAC s'attend à recevoir CC après chaque D2 (environ 50–100 ms après).
Sans CC régulier, la PAC peut considérer la télécommande absente.

### Trame CD (commande remote → PAC)

Pour modifier la configuration, le remote envoie CD avec les nouveaux paramètres.
Le RPi répète CD sur **~8 cycles D2 consécutifs** pour garantir la réception.

```
CD[0]  = 0xCD
CD[1]  = byte[1] avec le mode et le bit on/off voulus
CD[4]  = byte[4] du mode voulu (0x01 ou 0x02)
CD[11] = nouvelle consigne (°C)
CD[79] = (sum(CD[0..78]) + 0xAF) & 0xFF
```

> La PAC met à jour son D2 broadcast dès qu'elle accepte un CD.
> Surveiller D2 byte[1] et byte[11] pour confirmer la prise en compte.

---

## 3. Méthodologie de reverse engineering

Cette section documente **comment analyser les trames et tester des commandes**
avec la télécommande filaire en place. C'est la méthode utilisée pour établir
le protocole décrit ci-dessus.

### Principe général

Le bus RS485 est un média partagé : tout ce qui circule est visible par tous.
En branchant le Waveshare en parallèle (switch 120Ω OFF), on observe passivement
toutes les trames sans perturber le bus.

La méthode est : **provoquer une action connue sur la télécommande** → **corréler
avec les changements de bytes dans les trames capturées** → **en déduire l'encodage**.

### Étape 1 — Capture passive

Arrêter le service poolex (libère le port), puis lancer une capture en CSV :

```bash
sudo systemctl stop poolex

nohup /opt/poolex-control/venv/bin/python3 -c "
import sqlite3, time, sys
sys.path.insert(0, '/opt/poolex-control')
from poolex.capture import RS485Capture
from poolex.decoder import Frame

log = open('/tmp/capture.log', 'w')
log.write('timestamp,header,b1,b4,b11,b79,raw\n')
log.flush()

def on_frame(f):
    log.write(f'{time.time():.3f},{f.name},'
              f'0x{f.raw[1]:02x},0x{f.raw[4]:02x},{f.raw[11]},'
              f'0x{f.raw[79]:02x},{f.raw.hex()}\n')
    log.flush()

c = RS485Capture(port='/dev/ttyUSB0', on_frame=on_frame)
c.start()
time.sleep(300)   # 5 minutes
c.stop()
log.close()
" > /tmp/capture.out 2>&1 &

echo "Capture démarrée (PID $!)"
```

Redémarrer le service après :
```bash
sudo systemctl start poolex
```

### Étape 2 — Provoquer des actions et corréler

Effectuer des actions sur la télécommande filaire (changer la consigne, le mode,
allumer/éteindre) **une par une**, en notant l'heure approximative.

Après chaque action, vérifier les trames CD capturées :

```bash
# Voir tous les CD distincts par b[1] et b[11]
grep ',CD,' /tmp/capture.log | cut -d',' -f1,3,4,5 | sort -u

# Voir les transitions de D2 (reflète l'état interne de la PAC)
grep ',D2,' /tmp/capture.log | cut -d',' -f1,3,4,5 \
  | awk -F',' '{ if ($2 != prev2 || $3 != prev3 || $4 != prev4)
      { print; prev2=$2; prev3=$3; prev4=$4 } }'
```

### Étape 3 — Analyser les bytes variables

Pour identifier quel byte change lors d'une action :

```python
# Comparer deux trames hexadécimales (ex: avant/après changement de mode)
a = bytes.fromhex("d25b0001012d...")
b = bytes.fromhex("d23b0001012d...")
diffs = [(i, f'0x{a[i]:02x}→0x{b[i]:02x}') for i in range(80) if a[i] != b[i]]
print(diffs)
```

Ou utiliser l'endpoint `/frames` pour récupérer les trames brutes :

```bash
# Comparer les 2 dernières trames D2
curl "http://raspberrypi4:5000/frames?header=D2&limit=2"
```

### Étape 4 — Valider le checksum

Vérifier que byte[79] correspond à la formule pour les trames CD capturées :

```python
raw = bytes.fromhex("cd5b0001...")
expected = (sum(raw[:79]) + 0xAF) & 0xFF
assert expected == raw[79], f"Checksum: attendu 0x{expected:02x}, reçu 0x{raw[79]:02x}"
```

### Étape 5 — Tester une commande en live

Avec le service poolex actif (et la télécommande branchée ou non) :

```bash
# Changer la consigne
curl -X POST http://raspberrypi4:5000/control/setpoint \
     -H "Content-Type: application/json" -d '{"temperature": 25}'

# Surveiller D2 pour confirmer que la PAC a accepté
watch -n1 "curl -s http://raspberrypi4:5000/status | python3 -m json.tool"
```

La PAC confirme en mettant à jour D2 byte[11]. Un changement non pris en compte
indique un checksum incorrect ou un timing de bus inadapté.

### Règles importantes pour l'analyse

| Règle | Explication |
|-------|-------------|
| Modifier **un seul paramètre à la fois** | Sinon impossible de corréler |
| **Toujours noter l'heure** de chaque action | Les timestamps du log permettent de filtrer |
| Surveiller **D2 en retour** après une commande | La PAC confirme en mettant à jour son D2 |
| Le Waveshare **ne s'écho pas** en RX | Les trames envoyées par le RPi ne reviennent pas dans le log |
| Le service poolex et une capture manuelle **ne peuvent pas coexister** sur le même port | Arrêter l'un avant de lancer l'autre |
| byte[79] doit toujours être **recalculé** après modification | `(sum(bytes[0..78]) + 0xAF) & 0xFF` |

### Cohabitation télécommande filaire + RPi

La télécommande filaire et le RPi **peuvent coexister sur le bus** simultanément.
Le RPi envoie CC (keepalive) et CD (commandes), la télécommande fait de même.
Le RPi lit D2 de la PAC et met à jour son état interne — la télécommande reste
utilisable pour observer l'effet des commandes RPi sur l'afficheur.

Pour des captures propres (sans CC/CD parasites du RPi) :
```bash
sudo systemctl stop poolex
# ... capture manuelle ...
sudo systemctl start poolex
```

---

## 4. Architecture logicielle

```
poolex-control/
├── poolex/
│   ├── decoder.py       # Décodage des trames (Frame, DDFrame, CDFrame, diff)
│   ├── capture.py       # Lecture série en thread, retry si port absent
│   ├── storage.py       # Stockage SQLite (schéma BLOB, 1 ligne par trame)
│   ├── controller.py    # Protocole réactif : CC keepalive + CD commandes
│   ├── mqtt.py          # Client MQTT + autodiscovery Home Assistant
│   ├── api.py           # API REST Flask
│   ├── analyzer.py      # Outil CLI interactif pour reverse engineering
│   └── test_protocol.py # Blueprint Flask de test guidé pour reverse engineering
├── tests/
│   ├── test_decoder.py
│   └── test_controller.py
├── scripts/
│   ├── install.sh       # Installation initiale sur le RPi
│   └── poolex.service   # Unit systemd
└── .github/workflows/
    ├── ci.yml           # Lint + tests (ubuntu-latest)
    └── deploy.yml       # Déploiement automatique (self-hosted runner RPi)
```

### Flux de données

```
Bus RS485
   │
   ├─── PAC envoie DD (~1/s) ────────────────────►  capture.py
   ├─── PAC envoie D2 (~1/s) ────────────────────►  capture.py
   │                                                      │
   │                                               storage.py → SQLite
   │                                                      │
   │                                               controller.py
   │                                               ├─ lit D2 → met à jour état interne
   │                                               ├─ envoie CC (~50ms après D2)
   │                                               └─ envoie CD (sur commande API)
   │
   ◄─── RPi envoie CC ──────────────────────────── controller.py
   ◄─── RPi envoie CD (commandes) ──────────────── controller.py
   │
api.py (Flask :5000)
   ├── GET  /status          → état courant (températures, setpoint, power, mode)
   ├── GET  /frames          → trames brutes récentes
   ├── GET  /frames/stats    → comptage par type
   ├── POST /control/setpoint → CD avec nouveau byte[11]
   ├── POST /control/power    → CD avec byte[1] bit 0 modifié
   └── POST /control/mode     → CD avec byte[1] + byte[4] du mode voulu
```

### Logique du contrôleur

```
Démarrage
   │
   ├─ Charger template D2 depuis SQLite (dernier D2 connu)
   │  → controller_ready = True immédiatement (pas besoin du bus au démarrage)
   │
   └─ Boucle : attendre D2 de la PAC
         │
         ├─ Mettre à jour template (setpoint, mode, power reflets de la PAC)
         ├─ 50ms après → envoyer CC (miroir de D2, checksum recalculé)
         └─ Si commande en attente → envoyer CD (8 cycles pour fiabilité)
                                     → annuler après 8 cycles
```

---

## 5. Installation sur le Raspberry Pi

### Prérequis

- Raspberry Pi OS ou Debian ≥ 12 (64 bits)
- Python ≥ 3.11
- Accès SSH et `sudo`

### Étape 1 — Cloner et installer

```bash
git clone https://github.com/eldiablotin/poolex-control.git
cd poolex-control
bash scripts/install.sh
```

Le script effectue automatiquement :

| Étape | Action |
|-------|--------|
| 1 | Installation des paquets système (`python3-venv`, `git`, `mosquitto`) |
| 2 | Création du broker Mosquitto avec un user `poolex` dédié + génération du mot de passe |
| 3 | Ajout de l'utilisateur au groupe `dialout` (accès `/dev/ttyUSB0`) |
| 4 | Création de `/opt/poolex-control/` et `/var/lib/poolex/` |
| 5 | Virtualenv Python + installation des dépendances |
| 6 | Service systemd `poolex` installé et activé |
| 7 | Règle sudoers (restart sans mot de passe pour le runner CI) |

À la fin du script, **le mot de passe MQTT généré est affiché**. Il doit être ajouté comme secret GitHub (`POOLEX_MQTT_PASSWORD`) pour que le deploy l'injecte automatiquement.

> ⚠️ Se déconnecter et reconnecter en SSH après l'installation (groupe `dialout`).

### Étape 2 — Vérifier

```bash
# Port USB-RS485
ls /dev/ttyUSB*              # → /dev/ttyUSB0

# Service
systemctl status poolex
curl http://localhost:5000/status
```

### Variables d'environnement

| Variable | Défaut | Description |
|----------|--------|-------------|
| `POOLEX_SERIAL_PORT` | `/dev/ttyUSB0` | Port série |
| `POOLEX_DB_PATH` | `/var/lib/poolex/poolex.db` | Base de données |
| `POOLEX_API_PORT` | `5000` | Port de l'API |
| `POOLEX_MQTT_HOST` | `localhost` | Broker MQTT |
| `POOLEX_MQTT_PORT` | `1883` | Port MQTT |
| `POOLEX_MQTT_USER` | `poolex` | Utilisateur MQTT |
| `POOLEX_MQTT_PASSWORD` | *(secret)* | Mot de passe MQTT — via `poolex.env` |
| `POOLEX_MQTT_PREFIX` | `poolex` | Préfixe des topics |

---

## 6. Intégration MQTT et Home Assistant

### Broker MQTT

Le script `install.sh` installe **Mosquitto** sur le RPi et crée un utilisateur `poolex` dédié.
Si vous utilisez un broker existant (ex: le Mosquitto add-on Home Assistant), configurez les variables d'environnement en conséquence.

### Topics publiés (~toutes les 15s et après chaque commande)

| Topic | Contenu |
|-------|---------|
| `poolex/status` | JSON complet (water_temp, air_temp, setpoint, power, mode, controller_ready) |
| `poolex/water_temp` | Température eau piscine (°C) |
| `poolex/air_temp` | Température air extérieur (°C) |
| `poolex/setpoint` | Consigne courante (°C) |
| `poolex/power` | `on` ou `off` |
| `poolex/mode` | `inverter`, `fix`, `sun` ou `cooling` |

### Topics de commande (subscribe)

| Topic | Valeur attendue |
|-------|----------------|
| `poolex/control/setpoint` | Entier entre 8 et 40 |
| `poolex/control/power` | `on` ou `off` |
| `poolex/control/mode` | `inverter`, `fix`, `sun` ou `cooling` |

### Home Assistant — autodiscovery

Au démarrage, le service publie automatiquement les payloads de découverte MQTT.
Home Assistant crée les entités suivantes sans configuration manuelle :

| Entité | Type HA | Fonctionnalité |
|--------|---------|---------------|
| Poolex PAC | `climate` | On/off + consigne (8–40°C) + température courante |
| Poolex PAC Mode | `select` | Sélection mode : inverter / fix / sun / cooling |
| Poolex PAC Température eau | `sensor` | Température eau piscine (°C) |
| Poolex PAC Température air | `sensor` | Température air extérieur (°C) |

### Configuration avec le Mosquitto add-on HA

Si Home Assistant utilise son propre broker Mosquitto (add-on), configurez poolex-control
pour s'y connecter plutôt qu'au broker local :

1. Dans HA → Settings → People → créer un utilisateur `poolex` avec un mot de passe
2. Dans `scripts/poolex.service`, mettre `POOLEX_MQTT_HOST=<IP de HA>`
3. Mettre à jour le secret GitHub `POOLEX_MQTT_PASSWORD` avec ce mot de passe
4. L'intégration MQTT HA existante découvrira automatiquement les entités

---

## 7. Mise en place GitHub Actions

Deux workflows sont fournis :

| Workflow | Déclencheur | Runner | Rôle |
|----------|------------|--------|------|
| `ci.yml` | Tout push | `ubuntu-latest` (GitHub) | Lint ruff + tests pytest |
| `deploy.yml` | Push sur `main` | Self-hosted (RPi) | Déploiement automatique |

Le CI fonctionne sans configuration. Le deploy nécessite un runner self-hosted sur le RPi.

### Secrets GitHub requis

| Secret | Description |
|--------|-------------|
| `GH_PAT` | Personal Access Token avec scope `repo` (pour le checkout self-hosted) |
| `POOLEX_MQTT_PASSWORD` | Mot de passe MQTT — généré par `install.sh` |

### Installer le runner self-hosted sur le RPi

Sur GitHub → repo → Settings → Actions → Runners → **New self-hosted runner** (Linux / ARM64).

Sur le RPi (en tant que `pi`, **jamais root**) :

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o runner.tar.gz -L \
  https://github.com/actions/runner/releases/latest/download/actions-runner-linux-arm64.tar.gz
tar xzf runner.tar.gz
./config.sh --url https://github.com/<vous>/<repo> --token <TOKEN>

# Installer et activer comme service systemd
sudo ./svc.sh install pi
sudo ./svc.sh start
```

> `concurrency: cancel-in-progress: true` dans `deploy.yml` annule les jobs en attente
> si un nouveau push arrive — évite qu'un déploiement obsolète écrase une version plus récente.

### Déploiement manuel (sans CI/CD)

```bash
# Sur le RPi
cd ~/poolex-control
git pull
find . -maxdepth 1 -mindepth 1 ! -name '.git' -exec cp -r {} /opt/poolex-control/ \;
/opt/poolex-control/venv/bin/pip install -r /opt/poolex-control/requirements.txt -q
sudo systemctl restart poolex
```

---

## 8. Utilisation de l'API

L'API écoute sur `0.0.0.0:5000`.

### GET /status

```bash
curl http://raspberrypi4:5000/status
```

```json
{
  "water_temp": 11.7,
  "air_temp": 16.5,
  "pac_mode": 161,
  "setpoint": 26,
  "power": true,
  "mode": "inverter",
  "controller_ready": true
}
```

| Champ | Description |
|-------|-------------|
| `water_temp` | Température eau piscine (°C) — DD byte[29] ÷ 10 |
| `air_temp` | Température air extérieur (°C) — DD byte[20] ÷ 2 |
| `pac_mode` | État compresseur brut — DD byte[3] (161=chauffe, 33=marche, 0=éteint) |
| `setpoint` | Consigne courante (°C) — reflète le D2 de la PAC |
| `power` | État on/off — D2 byte[1] bit 0 |
| `mode` | Mode de chauffe — inverter / fix / sun / cooling |
| `controller_ready` | `true` si un template D2 est disponible |

### POST /control/setpoint

```bash
curl -X POST http://raspberrypi4:5000/control/setpoint \
     -H "Content-Type: application/json" \
     -d '{"temperature": 28}'
```

Plage : 8–40°C. La PAC confirme en mettant à jour D2 byte[11].

### POST /control/power

```bash
curl -X POST http://raspberrypi4:5000/control/power \
     -H "Content-Type: application/json" \
     -d '{"state": "off"}'   # ou "on"
```

### POST /control/mode

```bash
curl -X POST http://raspberrypi4:5000/control/mode \
     -H "Content-Type: application/json" \
     -d '{"mode": "fix"}'    # inverter | fix | sun | cooling
```

### GET /frames

```bash
# 20 dernières trames
curl http://raspberrypi4:5000/frames

# Filtrer par type
curl "http://raspberrypi4:5000/frames?header=DD&limit=10"
```

### GET /frames/stats

```bash
curl http://raspberrypi4:5000/frames/stats
# {"CC": 2353, "CD": 235, "D2": 8668, "DD": 10475}
```

---

## 9. Dépannage

### Port /dev/ttyUSB0 absent ou instable

```bash
lsusb | grep -i ftdi            # vérifier détection USB
dmesg | grep -E "ttyUSB|ftdi"   # voir les événements kernel

# Si ttyUSB0 apparaît puis disparaît immédiatement → brltty
sudo apt remove --purge brltty -y
# Débrancher / rebrancher l'adaptateur
```

### Pas de trames sur le bus

```bash
# Test de lecture brute
timeout 5 cat /dev/ttyUSB0 | xxd | head -20
```

- Aucun octet → vérifier câblage A+/B-, switch 120Ω adapté (ON si remote débranchée)
- Octets présents mais trames invalides → vérifier 9600 baud, longueur de câble

### Service ne démarre pas

```bash
journalctl -u poolex -n 30 --no-pager
```

| Erreur | Solution |
|--------|----------|
| `No such file: /dev/ttyUSB0` | Normal, retry automatique toutes les 10s |
| `Permission denied: /dev/ttyUSB0` | `sudo usermod -a -G dialout pi` + reconnexion |
| `ModuleNotFoundError` | `pip install -r /opt/poolex-control/requirements.txt` dans le venv |

### Runner GitHub Actions ne démarre pas

```bash
systemctl status runner-poolex
journalctl -u runner-poolex -n 20
```

Cause fréquente : service configuré `Type=oneshot` ou `User=root`.
Le runner refuse de tourner en root — voir [section 6](#6-mise-en-place-github-actions).

### sudo cassé (erreur syntaxe sudoers)

```bash
su -                             # passer root sans sudo
rm /etc/sudoers.d/poolex
printf 'pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl daemon-reload\n' \
       > /etc/sudoers.d/poolex
printf 'pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart poolex\n' \
       >> /etc/sudoers.d/poolex
chmod 440 /etc/sudoers.d/poolex
```

> ⚠️ Ne jamais mettre `user:group` dans sudoers — le `:` est réservé.


# ENGLISH VERSION
# Poolex Control

Full control of a swimming pool heat pump via its RS485 bus, using a Raspberry Pi 4 and a USB-RS485 adapter.

---

## Table of Contents

1. [Required Hardware](#1-required-hardware)
2. [RS485 Protocol — Full Model](#2-rs485-protocol--full-model)
3. [Reverse Engineering Methodology](#3-reverse-engineering-methodology)
4. [Software Architecture](#4-software-architecture)
5. [Installation on Raspberry Pi](#5-installation-on-raspberry-pi)
6. [MQTT and Home Assistant Integration](#6-mqtt-and-home-assistant-integration)
7. [Setting Up GitHub Actions](#7-setting-up-github-actions)
8. [Using the API](#8-using-the-api)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Required Hardware

| Component | Model | Notes |
|-----------|-------|-------|
| Single-board computer | Raspberry Pi 4 Rev B | Debian 13 (Trixie), 64-bit |
| USB-RS485 adapter | Waveshare USB to RS232/485 | FTDI FT232RNL chip |
| USB cable | USB-A → USB-A (provided) | |
| Access to RS485 bus | A+/B- terminals of the heat pump | In parallel with the wired remote control |

### Wiring

```
Waveshare Adapter          Heat Pump RS485 Bus
────────────────────          ─────────────────
TX_A  ─────────────────────►  A+ Terminal
RX_B  ◄─────────────────────  B- Terminal
GND   ─────────────────────── Ground (optional)
```

**120Ω Switch:**
- `OFF` if the wired remote control is present (parallel connection)
- `ON` if the remote control is disconnected (RPi alone on the bus — termination required)

> The Waveshare (FT232RNL) automatically handles DE/RE switching.
> It **does not return local echo** during transmission: frames sent by the RPi do not appear in the receive stream.

---

## 2. RS485 Protocol — Full Model

### Physical Parameters

| Parameter | Value |
|-----------|-------|
| Baud rate | 9600 baud |
| Format | 8N1 (8 bits, no parity, 1 stop) |
| Frame size | **80 bytes fixed** |
| Rate | ~1 frame/second per type |

### Frame Roles — Model Confirmed April 2026

> ⚠️ Contrary to expectations, **the heat pump is the bus master**.
> It broadcasts D2 with its current configuration.
> The remote responds with CC (keepalive) and sends CD to issue commands.

| Header | Hex | Sender | Frequency | Role |
|--------|-----|--------|-----------|------|
| `DD` | 0xDD | Heat Pump | ~1/s | Live status (temperatures, compressor state) |
| `D2` | 0xD2 | **Heat Pump** | ~1/s | Current configuration broadcast (setpoint, mode, power) |
| `CC` | 0xCC | Remote | ~1/s | Keepalive — mirror of D2, confirms reception |
| `CD` | 0xCD | Remote | On command | Command: change setpoint, power, mode |

### Checksum byte[79]

All D2, CC, and CD frames have a **checksum in byte[79]**:

```
byte[79] = (sum(bytes[0..78]) + 0xAF) & 0xFF
```

> Any modified frame must recalculate this checksum. The heat pump silently rejects frames with an incorrect checksum.

For DD, byte[79] is a rolling counter (not an application checksum).

### DD Frame Decoding (Real-Time Status: Heat Pump → Remote)

| Byte | Decoding | Example | Status |
|------|----------|---------|--------|
| `[0]` | Header = 0xDD | — | ✓ |
| `[20]` | Outdoor air temperature = value ÷ 2 °C | 26 → 13.0°C | ✓ Apr 2026 |
| `[29]` | Pool water temperature = value ÷ 10 °C | 114 → 11.4°C | ✓ Apr 2026 |
| `[3]` | Compressor state (see table below) | 0xa1 → heating | ✓ Apr 2026 |
| `[79]` | Rolling counter | — | ✓ |

**DD byte[3] Values:**

| Value | Hex | Meaning |
|-------|-----|---------|
| 161 | 0xa1 | Heating active |
| 33 | 0x21 | Running / standby |
| 32 | 0x20 | Stopping |
| 0 | 0x00 | Off |

### D2 Frame Decoding (Current Configuration: Heat Pump → Remote)

The heat pump emits D2 at ~1/s with its internal configuration. When the RPi modifies a parameter (via CD), D2 updates to reflect the new state.

| Byte | Decoding |
|------|----------|
| `[0]` | Header = 0xD2 |
| `[1]` | State + mode (see modes table) |
| `[4]` | Sub-mode (0x01 = normal, 0x02 = cooling) |
| `[11]` | Setpoint temperature (°C) |
| `[79]` | Checksum = (sum[0..78] + 0xAF) & 0xFF |

### Heating Modes — byte[1] + byte[4]

| byte[1] | byte[4] | Mode | bit 0 of byte[1] |
|---------|---------|------|------------------|
| 0x5B | 0x01 | **inverter** (on) | 1 = on |
| 0x3B | 0x01 | **fix** (on) | 1 = on |
| 0x1B | 0x01 | **sun** (on) | 1 = on |
| 0x1B | 0x02 | **cooling** (on) | 1 = on |
| 0x5A | 0x01 | inverter (off) | 0 = off |
| 0x3A | 0x01 | fix (off) | 0 = off |
| … | … | … | … |

**Rule:** `byte[1] bit 0 = 1` → on, `bit 0 = 0` → off.
Bits 1-7 encode the mode. Modes differ by steps of `0x20`.

### CC Frame (Keepalive: Remote → Heat Pump)

CC is an exact mirror of D2: same bytes [1..78], only the header changes.

```
CC[0]  = 0xCC  (instead of 0xD2)
CC[79] = (sum(CC[0..78]) + 0xAF) & 0xFF
```

The heat pump expects to receive CC after each D2 (approximately 50–100 ms later).
Without regular CC, the heat pump may consider the remote absent.

### CD Frame (Command: Remote → Heat Pump)

To modify the configuration, the remote sends CD with the new parameters.
The RPi repeats CD on **~8 consecutive D2 cycles** to ensure reception.

```
CD[0]  = 0xCD
CD[1]  = byte[1] with the desired mode and on/off bit
CD[4]  = byte[4] of the desired mode (0x01 or 0x02)
CD[11] = new setpoint (°C)
CD[79] = (sum(CD[0..78]) + 0xAF) & 0xFF
```

> The heat pump updates its D2 broadcast as soon as it accepts a CD.
> Monitor D2 byte[1] and byte[11] to confirm acceptance.

---
---
## 3. Reverse Engineering Methodology

This section documents **how to analyze frames and test commands** with the wired remote control in place. This is the method used to establish the protocol described above.

### General Principle

The RS485 bus is a shared medium: everything that circulates is visible to all.
By connecting the Waveshare adapter in parallel (120Ω switch OFF), you can passively observe all frames without disrupting the bus.

The method is: **trigger a known action on the remote control** → **correlate** with the changes in bytes in the captured frames → **deduce the encoding**.

### Step 1 — Passive Capture

Stop the poolex service (frees the port), then start a capture in CSV:

```bash
sudo systemctl stop poolex

nohup /opt/poolex-control/venv/bin/python3 -c "
import sqlite3, time, sys
sys.path.insert(0, '/opt/poolex-control')
from poolex.capture import RS485Capture
from poolex.decoder import Frame

log = open('/tmp/capture.log', 'w')
log.write('timestamp,header,b1,b4,b11,b79,raw\n')
log.flush()

def on_frame(f):
    log.write(f'{time.time():.3f},{f.name},'
              f'0x{f.raw[1]:02x},0x{f.raw[4]:02x},{f.raw[11]},'
              f'0x{f.raw[79]:02x},{f.raw.hex()}\n')
    log.flush()

c = RS485Capture(port='/dev/ttyUSB0', on_frame=on_frame)
c.start()
time.sleep(300)   # 5 minutes
c.stop()
log.close()
" > /tmp/capture.out 2>&1 &

echo "Capture started (PID $!)"
```

Restart the service afterward:

```bash
sudo systemctl start poolex
```

### Step 2 — Trigger Actions and Correlate

Perform actions on the wired remote control (change setpoint, mode, turn on/off) **one at a time**, noting the approximate time.

After each action, check the captured CD frames:

```bash
# See all distinct CD frames by b[1] and b[11]
grep ',CD,' /tmp/capture.log | cut -d',' -f1,3,4,5 | sort -u

# See D2 transitions (reflects the heat pump's internal state)
grep ',D2,' /tmp/capture.log | cut -d',' -f1,3,4,5 \
  | awk -F',' '{ if ($2 != prev2 || $3 != prev3 || $4 != prev4)
      { print; prev2=$2; prev3=$3; prev4=$4 } }'
```

### Step 3 — Analyze Variable Bytes

To identify which byte changes during an action:

```bash
# Compare two hexadecimal frames (e.g., before/after mode change)
a = bytes.fromhex("d25b0001012d...")
b = bytes.fromhex("d23b0001012d...")
diffs = [(i, f'0x{a[i]:02x}→0x{b[i]:02x}') for i in range(80) if a[i] != b[i]]
print(diffs)
```

Or use the `/frames` endpoint to retrieve raw frames:

```bash
# Compare the last 2 D2 frames
curl "http://raspberrypi4:5000/frames?header=D2&limit=2"
```

### Step 4 — Validate the Checksum

Check that byte[79] matches the formula for captured CD frames:

```bash
raw = bytes.fromhex("cd5b0001...")
expected = (sum(raw[:79]) + 0xAF) & 0xFF
assert expected == raw[79], f"Checksum: expected 0x{expected:02x}, received 0x{raw[79]:02x}"
```

### Step 5 — Test a Command Live

With the poolex service active (and the remote connected or not):

```bash
# Change the setpoint
curl -X POST http://raspberrypi4:5000/control/setpoint \
     -H "Content-Type: application/json" -d '{"temperature": 25}'

# Monitor D2 to confirm the heat pump has accepted
watch -n1 "curl -s http://raspberrypi4:5000/status | python3 -m json.tool"
```

The heat pump confirms by updating D2 byte[11]. A change not taken into account indicates an incorrect checksum or inappropriate bus timing.

### Important Rules for Analysis

| Rule | Explanation |
|------|-------------|
| Modify **one parameter at a time** | Otherwise, correlation is impossible |
| **Always note the time** of each action | Log timestamps allow filtering |
| Monitor **D2 in return** after a command | The heat pump confirms by updating its D2 |
| The Waveshare **does not echo** in RX | Frames sent by the RPi do not return in the log |
| The poolex service and manual capture **cannot coexist** on the same port | Stop one before starting the other |
| byte[79] must always be **recalculated** after modification | `(sum(bytes[0..78]) + 0xAF) & 0xFF` |

### Coexistence of Wired Remote + RPi

The wired remote and the RPi **can coexist on the bus** simultaneously.
The RPi sends CC (keepalive) and CD (commands), and the remote does the same.
The RPi reads D2 from the heat pump and updates its internal state — the remote remains usable to observe the effect of RPi commands on the display.

For clean captures (without RPi's CC/CD interference):

```bash
sudo systemctl stop poolex
# ... manual capture ...
sudo systemctl start poolex
```

---
---
## 4. Software Architecture

```
poolex-control/
├── poolex/
│   ├── decoder.py       # Frame decoding (Frame, DDFrame, CDFrame, diff)
│   ├── capture.py       # Serial reading in thread, retry if port is missing
│   ├── storage.py       # SQLite storage (BLOB schema, 1 row per frame)
│   ├── controller.py    # Reactive protocol: CC keepalive + CD commands
│   ├── mqtt.py          # MQTT client + Home Assistant autodiscovery
│   ├── api.py           # Flask REST API
│   ├── analyzer.py      # Interactive CLI tool for reverse engineering
│   └── test_protocol.py # Flask blueprint for guided reverse engineering testing
├── tests/
│   ├── test_decoder.py
│   └── test_controller.py
├── scripts/
│   ├── install.sh       # Initial installation on RPi
│   └── poolex.service   # Systemd unit
└── .github/workflows/
    ├── ci.yml           # Lint + tests (ubuntu-latest)
    └── deploy.yml       # Automatic deployment (self-hosted runner RPi)
```

### Data Flow

```
RS485 Bus
   │
   ├─── Heat Pump sends DD (~1/s) ────────────────────► capture.py
   ├─── Heat Pump sends D2 (~1/s) ────────────────────► capture.py
   │                                              │
   │                                       storage.py → SQLite
   │                                              │
   │                                       controller.py
   │                                       ├─ reads D2 → updates internal state
   │                                       ├─ sends CC (~50ms after D2)
   │                                       └─ sends CD (on API command)
   │
   ◄─── RPi sends CC ──────────────────────────── controller.py
   ◄─── RPi sends CD (commands) ──────────────── controller.py

api.py (Flask :5000)
   ├── GET  /status          → current state (temperatures, setpoint, power, mode)
   ├── GET  /frames          → recent raw frames
   ├── GET  /frames/stats    → count by type
   ├── POST /control/setpoint → CD with new byte[11]
   ├── POST /control/power    → CD with byte[1] bit 0 modified
   └── POST /control/mode     → CD with byte[1] + byte[4] of desired mode
```

### Controller Logic

```
Startup
   │
   ├─ Load D2 template from SQLite (last known D2)
   │  → controller_ready = True immediately (no need for bus at startup)
   │
   └─ Loop: wait for D2 from the heat pump
         │
         ├─ Update template (setpoint, mode, power reflecting the heat pump)
         ├─ 50ms later → send CC (mirror of D2, checksum recalculated)
         └─ If command pending → send CD (8 cycles for reliability)
                                     → cancel after 8 cycles
```

---
---
## 5. Installation on Raspberry Pi

### Prerequisites

- Raspberry Pi OS or Debian ≥ 12 (64-bit)
- Python ≥ 3.11
- SSH access and `sudo`

### Step 1 — Clone and Install

```bash
git clone https://github.com/eldiablotin/poolex-control.git
cd poolex-control
bash scripts/install.sh
```

The script automatically performs:

| Step | Action |
|------|--------|
| 1 | Install system packages (`python3-venv`, `git`, `mosquitto`) |
| 2 | Create Mosquitto broker with a dedicated `poolex` user + password generation |
| 3 | Add user to `dialout` group (access `/dev/ttyUSB0`) |
| 4 | Create `/opt/poolex-control/` and `/var/lib/poolex/` |
| 5 | Python virtualenv + install dependencies |
| 6 | Install and enable `poolex` systemd service |
| 7 | Sudoers rule (restart without password for CI runner) |

At the end of the script, **the generated MQTT password is displayed**. It must be added as a GitHub secret (`POOLEX_MQTT_PASSWORD`) for the deploy to inject it automatically.

> ⚠️ Log out and log back in via SSH after installation (to apply `dialout` group).

### Step 2 — Verify

```bash
# USB-RS485 port
ls /dev/ttyUSB*              # → /dev/ttyUSB0

# Service
systemctl status poolex
curl http://localhost:5000/status
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `POOLEX_SERIAL_PORT` | `/dev/ttyUSB0` | Serial port |
| `POOLEX_DB_PATH` | `/var/lib/poolex/poolex.db` | Database |
| `POOLEX_API_PORT` | `5000` | API port |
| `POOLEX_MQTT_HOST` | `localhost` | MQTT broker |
| `POOLEX_MQTT_PORT` | `1883` | MQTT port |
| `POOLEX_MQTT_USER` | `poolex` | MQTT user |
| `POOLEX_MQTT_PASSWORD` | _(secret)_ | MQTT password — via `poolex.env` |
| `POOLEX_MQTT_PREFIX` | `poolex` | Topic prefix |

---
---
## 6. MQTT and Home Assistant Integration

### MQTT Broker

The `install.sh` script installs **Mosquitto** on the RPi and creates a dedicated `poolex` user.
If you use an existing broker (e.g., the Home Assistant Mosquitto add-on), configure the environment variables accordingly.

### Published Topics (~every 15s and after each command)

| Topic | Content |
|-------|---------|
| `poolex/status` | Full JSON (water_temp, air_temp, setpoint, power, mode, controller_ready) |
| `poolex/water_temp` | Pool water temperature (°C) |
| `poolex/air_temp` | Outdoor air temperature (°C) |
| `poolex/setpoint` | Current setpoint (°C) |
| `poolex/power` | `on` or `off` |
| `poolex/mode` | `inverter`, `fix`, `sun`, or `cooling` |

### Command Topics (Subscribe)

| Topic | Expected Value |
|-------|----------------|
| `poolex/control/setpoint` | Integer between 8 and 40 |
| `poolex/control/power` | `on` or `off` |
| `poolex/control/mode` | `inverter`, `fix`, `sun`, or `cooling` |

### Home Assistant — Autodiscovery

At startup, the service automatically publishes MQTT discovery payloads.
Home Assistant creates the following entities without manual configuration:

| Entity | HA Type | Functionality |
|--------|---------|---------------|
| Poolex PAC | `climate` | On/off + setpoint (8–40°C) + current temperature |
| Poolex PAC Mode | `select` | Mode selection: inverter / fix / sun / cooling |
| Poolex PAC Water Temperature | `sensor` | Pool water temperature (°C) |
| Poolex PAC Air Temperature | `sensor` | Outdoor air temperature (°C) |

### Configuration with Home Assistant Mosquitto Add-on

If Home Assistant uses its own Mosquitto broker (add-on), configure poolex-control to connect to it instead of the local broker:

1. In HA → Settings → People → create a user `poolex` with a password
2. In `scripts/poolex.service`, set `POOLEX_MQTT_HOST=<HA IP>`
3. Update the GitHub secret `POOLEX_MQTT_PASSWORD` with this password
4. The existing MQTT integration in HA will automatically discover the entities

---
---
## 7. Setting Up GitHub Actions

Two workflows are provided:

| Workflow | Trigger | Runner | Role |
|----------|---------|--------|------|
| `ci.yml` | Any push | `ubuntu-latest` (GitHub) | Ruff lint + pytest tests |
| `deploy.yml` | Push to `main` | Self-hosted (RPi) | Automatic deployment |

The CI works without configuration. Deployment requires a self-hosted runner on the RPi.

### Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `GH_PAT` | Personal Access Token with `repo` scope (for self-hosted checkout) |
| `POOLEX_MQTT_PASSWORD` | MQTT password — generated by `install.sh` |

### Install the Self-Hosted Runner on the RPi

On GitHub → repo → Settings → Actions → Runners → **New self-hosted runner** (Linux / ARM64).

On the RPi (as `pi`, **never root**):

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o runner.tar.gz -L \
  https://github.com/actions/runner/releases/latest/download/actions-runner-linux-arm64.tar.gz
tar xzf runner.tar.gz
./config.sh --url https://github.com/<you>/<repo> --token <TOKEN>

# Install and activate as a systemd service
sudo ./svc.sh install pi
sudo ./svc.sh start
```

> `concurrency: cancel-in-progress: true` in `deploy.yml` cancels pending jobs if a new push arrives — prevents an outdated deployment from overwriting a newer version.

### Manual Deployment (Without CI/CD)

```bash
# On the RPi
cd ~/poolex-control
git pull
find . -maxdepth 1 -mindepth 1 ! -name '.git' -exec cp -r {} /opt/poolex-control/ \;
/opt/poolex-control/venv/bin/pip install -r /opt/poolex-control/requirements.txt -q
sudo systemctl restart poolex
```

---
---
## 8. Using the API

The API listens on `0.0.0.0:5000`.

### GET /status

```bash
curl http://raspberrypi4:5000/status
```

```json
{
  "water_temp": 11.7,
  "air_temp": 16.5,
  "pac_mode": 161,
  "setpoint": 26,
  "power": true,
  "mode": "inverter",
  "controller_ready": true
}
```

| Field | Description |
|-------|-------------|
| `water_temp` | Pool water temperature (°C) — DD byte[29] ÷ 10 |
| `air_temp` | Outdoor air temperature (°C) — DD byte[20] ÷ 2 |
| `pac_mode` | Raw compressor state — DD byte[3] (161=heating, 33=running, 0=off) |
| `setpoint` | Current setpoint (°C) — reflects the heat pump's D2 |
| `power` | On/off state — D2 byte[1] bit 0 |
| `mode` | Heating mode — inverter / fix / sun / cooling |
| `controller_ready` | `true` if a D2 template is available |

### POST /control/setpoint

```bash
curl -X POST http://raspberrypi4:5000/control/setpoint \
     -H "Content-Type: application/json" \
     -d '{"temperature": 28}'
```
Range: 8–40°C. The heat pump confirms by updating D2 byte[11].

### POST /control/power

```bash
curl -X POST http://raspberrypi4:5000/control/power \
     -H "Content-Type: application/json" \
     -d '{"state": "off"}'   # or "on"
```

### POST /control/mode

```bash
curl -X POST http://raspberrypi4:5000/control/mode \
     -H "Content-Type: application/json" \
     -d '{"mode": "fix"}'    # inverter | fix | sun | cooling
```

### GET /frames

```bash
# Last 20 frames
curl http://raspberrypi4:5000/frames

# Filter by type
curl "http://raspberrypi4:5000/frames?header=DD&limit=10"
```

### GET /frames/stats

```bash
curl http://raspberrypi4:5000/frames/stats
# {"CC": 2353, "CD": 235, "D2": 8668, "DD": 10475}
```

---
---
## 9. Troubleshooting

### /dev/ttyUSB0 Missing or Unstable

```bash
lsusb | grep -i ftdi            # Check USB detection
dmesg | grep -E "ttyUSB|ftdi"   # View kernel events

# If ttyUSB0 appears then disappears immediately → brltty
sudo apt remove --purge brltty -y
# Unplug / replug the adapter
```

### No Frames on the Bus

```bash
# Raw read test
timeout 5 cat /dev/ttyUSB0 | xxd | head -20
```

- No bytes → Check A+/B- wiring, 120Ω switch setting (ON if remote is disconnected)
- Bytes present but invalid frames → Check 9600 baud, cable length

### Service Fails to Start

```bash
journalctl -u poolex -n 30 --no-pager
```

| Error | Solution |
|-------|----------|
| `No such file: /dev/ttyUSB0` | Normal, automatic retry every 10s |
| `Permission denied: /dev/ttyUSB0` | `sudo usermod -a -G dialout pi` + reconnect |
| `ModuleNotFoundError` | `pip install -r /opt/poolex-control/requirements.txt` in the venv |

### GitHub Actions Runner Fails to Start

```bash
systemctl status runner-poolex
journalctl -u runner-poolex -n 20
```

Common cause: service configured as `Type=oneshot` or `User=root`.
The runner refuses to run as root — see [Section 6](#6-mqtt-and-home-assistant-integration).

### sudo Broken (sudoers Syntax Error)

```bash
su -                             # Switch to root without sudo
rm /etc/sudoers.d/poolex
printf 'pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl daemon-reload\n' \
       > /etc/sudoers.d/poolex
printf 'pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart poolex\n' \
       >> /etc/sudoers.d/poolex
chmod 440 /etc/sudoers.d/poolex
```

> ⚠️ Never use `user:group` in sudoers — the `:` is reserved.
