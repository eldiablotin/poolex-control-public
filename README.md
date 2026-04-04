# Poolex Control

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

À chaque `git push` sur `main` : CI (lint + tests) puis déploiement automatique sur le RPi.

### Runner self-hosted

Sur GitHub → repo → Settings → Actions → Runners → **New self-hosted runner** (Linux / ARM64).

Sur le RPi :

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o runner.tar.gz -L \
  https://github.com/actions/runner/releases/latest/download/actions-runner-linux-arm64.tar.gz
tar xzf runner.tar.gz
./config.sh --url https://github.com/eldiablotin/poolex-control --token <TOKEN>
```

Créer le service systemd **en tant que `pi`** (le runner refuse de tourner en root) :

```ini
# /etc/systemd/system/runner-poolex.service
[Unit]
Description=GitHub Actions Runner - poolex-control
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/actions-runner
ExecStart=/home/pi/actions-runner/run.sh
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now runner-poolex
```

> La directive `concurrency: cancel-in-progress: true` dans `deploy.yml` annule les
> anciens jobs quand un nouveau push arrive — évite qu'un déploiement antérieur
> écrase une version plus récente si le runner était offline.

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
