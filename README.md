# Poolex Control

Capture et contrôle d'une pompe à chaleur de piscine via son bus RS485,
en utilisant un Raspberry Pi 4 et un adaptateur USB-RS485.

---

## Table des matières

1. [Matériel requis](#1-matériel-requis)
2. [Protocole RS485 — ce qu'on a appris](#2-protocole-rs485--ce-quon-a-appris)
3. [Architecture logicielle](#3-architecture-logicielle)
4. [Installation sur le Raspberry Pi](#4-installation-sur-le-raspberry-pi)
5. [Mise en place GitHub Actions](#5-mise-en-place-github-actions)
6. [Utilisation de l'API](#6-utilisation-de-lapi)
7. [Dépannage](#7-dépannage)
8. [Protocole de test guidé](#8-protocole-de-test-guidé)

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

Switch 120Ω → OFF  (branchement en parallèle, pas en terminaison)
```

> ⚠️ Le module se branche **en parallèle** sur les fils de la télécommande filaire.
> Ne pas couper le câble existant. Ne pas activer la résistance de terminaison 120Ω.

---

## 2. Protocole RS485 — ce qu'on a appris

### Paramètres physiques

| Paramètre | Valeur |
|-----------|--------|
| Vitesse | 9600 baud |
| Format | 8N1 (8 bits, pas de parité, 1 stop) |
| Taille de trame | **80 octets fixes** |
| Cadence | ~1 trame/seconde par type |

### Types de trames

| Header | Hex | Émetteur | Fréquence | Rôle |
|--------|-----|----------|-----------|------|
| `DD` | 0xDD | PAC → télécommande | ~1/s | Données capteurs temps réel |
| `D2` | 0xD2 | Télécommande → PAC | ~1/s | Configuration / consignes |
| `CC` | 0xCC | Télécommande → PAC | ~1/s | Configuration (contenu identique à D2) |
| `CD` | 0xCD | Télécommande → PAC | Rare | Trame de commande (changement consigne) |

**Marqueur de fin** : `byte[79]` = valeur du header pour D2/CC/CD. Pour DD, `byte[79]` est un compteur roulant.

**Valeur "non disponible"** : `0x7F` (127) marque les octets sans donnée valide.

### Décodage trame DD (statut temps réel)

| Byte | Décodage | Exemple |
|------|----------|---------|
| `[0]` | Header = 0xDD | `DD` |
| `[22]` | **Température eau** = valeur ÷ 2 (°C) | 56 → 28.0°C |
| `[29]` | **Température air extérieur** (°C) | 25 → 25°C |
| `[3]` | Mode de fonctionnement (flags, à décoder) | |
| `[79]` | Compteur roulant | |

### Décodage trame CD (commande)

| Byte | Décodage |
|------|----------|
| `[0]` | Header = 0xCD |
| `[11]` | **Consigne température** (°C) |
| `[79]` | 0xCD ou 0xCE |

### Stratégie de contrôle

1. Écouter le bus et mémoriser la dernière trame `CD` reçue (template)
2. Pour changer la consigne : copier le template, modifier `byte[11]`, réinjecter sur le bus
3. L'adaptateur Waveshare gère le basculement DE/RE automatiquement

---

## 3. Architecture logicielle

```
poolex-control/
├── poolex/
│   ├── decoder.py      # Décodage des trames (Frame, DDFrame, CDFrame, diff)
│   ├── capture.py      # Lecture série en thread, retry automatique si port absent
│   ├── storage.py      # Stockage SQLite (schéma BLOB, 1 ligne par trame)
│   ├── controller.py   # Injection de trames CD modifiées
│   ├── api.py          # API REST Flask
│   ├── analyzer.py     # Analyseur CLI temps réel (diff coloré)
│   └── test_protocol.py # Interface web de test guidé
├── tests/
│   ├── test_decoder.py
│   └── test_controller.py
├── scripts/
│   ├── install.sh      # Installation initiale sur le RPi
│   └── poolex.service  # Unit systemd
└── .github/workflows/
    ├── ci.yml          # Lint + tests (GitHub cloud)
    └── deploy.yml      # Déploiement automatique (self-hosted runner sur RPi)
```

### Flux de données

```
Bus RS485
   │
   ▼
/dev/ttyUSB0  (Waveshare FT232RNL)
   │
   ▼
capture.py  ──► storage.py  ──► SQLite /var/lib/poolex/poolex.db
   │
   ├──► controller.py  (mémorise les trames CD)
   │
   ▼
api.py  (Flask :5000)
   │
   ├── GET  /status
   ├── GET  /frames
   ├── GET  /frames/stats
   └── POST /control/setpoint  ──► controller.py ──► Bus RS485
```

---

## 4. Installation sur le Raspberry Pi

### Prérequis

- Raspberry Pi OS ou Debian ≥ 12 (64 bits)
- Python ≥ 3.11
- Accès SSH et `sudo`

### Étape 1 — Mettre à jour le système

```bash
sudo apt update && sudo apt upgrade -y
```

### Étape 2 — Cloner le repo

```bash
git clone https://github.com/eldiablotin/poolex-control.git
cd poolex-control
```

Si le repo est privé, configurer le PAT d'abord :

```bash
git config --global credential.helper store
git remote set-url origin https://<USERNAME>:<PAT>@github.com/eldiablotin/poolex-control.git
```

### Étape 3 — Lancer le script d'installation

```bash
bash scripts/install.sh
```

Ce script effectue automatiquement :

| Étape | Action |
|-------|--------|
| 1 | Installation des paquets système (`python3-venv`, `git`) |
| 2 | Ajout de l'utilisateur au groupe `dialout` (accès `/dev/ttyUSB0`) |
| 3 | Création de `/opt/poolex-control/` et `/var/lib/poolex/` |
| 4 | Création du virtualenv Python + installation des dépendances |
| 5 | Installation et activation du service systemd `poolex` |
| 6 | Configuration sudoers (restart service sans mot de passe) |

> ⚠️ **Se déconnecter et reconnecter en SSH** après l'installation pour que le groupe `dialout` prenne effet.

### Étape 4 — Brancher l'adaptateur USB-RS485

Brancher l'adaptateur sur un port USB du RPi. Vérifier qu'il est reconnu :

```bash
ls /dev/ttyUSB*
# doit afficher : /dev/ttyUSB0

lsusb | grep -i ftdi
# doit afficher : Future Technology Devices International
```

### Étape 5 — Démarrer le service

```bash
sudo systemctl start poolex
journalctl -u poolex -f
```

Sortie attendue :
```
DB initialisée : /var/lib/poolex/poolex.db
Thread de capture démarré (port cible : /dev/ttyUSB0)
Port /dev/ttyUSB0 ouvert à 9600 baud
API démarrée sur le port 5000
```

### Vérifier que tout fonctionne

```bash
# Statut du service
systemctl status poolex

# Tester l'API
curl http://raspberrypi4:5000/status
curl http://raspberrypi4:5000/frames/stats
```

### Accès réseau — hostname mDNS

Le RPi est accessible via son nom d'hôte sur tout réseau local, **sans configuration IP** :

```
ssh pi@raspberrypi4     # SSH direct (mDNS géré par avahi-daemon)
```

> `avahi-daemon` est actif par défaut sur Debian. Le hostname `raspberrypi4` fonctionne
> sur Windows 10/11, macOS et Linux sans configuration IP.
> L'IP DHCP peut changer, le hostname reste stable.

> ℹ️ L'API Flask écoute sur `0.0.0.0:5000` — accessible depuis le PC via `http://raspberrypi4:5000/status`.
> La route `/` (racine) retourne la liste des endpoints disponibles.

### Variables d'environnement (optionnel)

Le service peut être configuré via des variables dans `/etc/systemd/system/poolex.service` :

| Variable | Défaut | Description |
|----------|--------|-------------|
| `POOLEX_SERIAL_PORT` | `/dev/ttyUSB0` | Port série de l'adaptateur |
| `POOLEX_DB_PATH` | `/var/lib/poolex/poolex.db` | Chemin de la base de données |
| `POOLEX_API_PORT` | `5000` | Port de l'API REST |

---

## 5. Mise en place GitHub Actions

Le déploiement automatique utilise un **runner self-hosted** sur le RPi :
à chaque push sur `main`, le code est déployé et le service redémarré.

### Étape 1 — Créer un Personal Access Token (PAT) GitHub

Sur GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens :
- Repo : `poolex-control`
- Permissions : `Contents: Read`, `Actions: Read and Write`

### Étape 2 — Ajouter le PAT comme secret du repo

Sur GitHub → repo `poolex-control` → Settings → Secrets and variables → Actions :
- Nom : `GH_PAT`
- Valeur : le PAT créé à l'étape précédente

### Étape 3 — Installer le runner sur le RPi

Sur GitHub → repo → Settings → Actions → Runners → **New self-hosted runner** :
- Sélectionner : **Linux / ARM64**
- Copier le token affiché (valable 1 heure)

Sur le RPi :

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner

# Télécharger le runner (vérifier la dernière version sur la page GitHub)
curl -o runner.tar.gz -L \
  https://github.com/actions/runner/releases/latest/download/actions-runner-linux-arm64.tar.gz
tar xzf runner.tar.gz

# Configurer avec le token obtenu sur GitHub
./config.sh --url https://github.com/eldiablotin/poolex-control --token <TOKEN>

# Installer et démarrer comme service systemd
sudo ./svc.sh install
sudo ./svc.sh start
```

Vérifier que le runner est actif :

```bash
systemctl status "actions.runner.*"
```

### Résultat

À chaque `git push` sur `main` depuis n'importe quelle machine :

```
Push → CI (lint + tests) → Deploy sur RPi → Restart service
```

---

## 6. Utilisation de l'API

L'API écoute sur `0.0.0.0:5000` — accessible depuis tout le réseau local.

```
http://raspberrypi4:5000/       → liste des endpoints
http://raspberrypi4:5000/status → état de la PAC
```

### GET /status

Retourne le dernier état décodé de la PAC.

```bash
curl http://raspberrypi4:5000/status
```

```json
{
  "water_temp": 28.0,
  "air_temp": 25,
  "mode": 128,
  "setpoint": 28,
  "controller_ready": true
}
```

| Champ | Description |
|-------|-------------|
| `water_temp` | Température eau piscine (°C) |
| `air_temp` | Température air extérieur (°C) |
| `mode` | Byte de mode brut (décodage en cours) |
| `setpoint` | Consigne température courante (°C) |
| `controller_ready` | `true` si une trame CD a été reçue (prêt à envoyer des commandes) |

### GET /frames

```bash
# 20 dernières trames de tous types
curl http://raspberrypi4:5000/frames

# Filtrer par type
curl "http://raspberrypi4:5000/frames?header=DD&limit=5"
```

### GET /frames/stats

```bash
curl http://raspberrypi4:5000/frames/stats
```

```json
{"CC": 1240, "CD": 12, "D2": 1356, "DD": 1298}
```

### POST /control/setpoint

Envoie une nouvelle consigne de température (10–40°C).

> ⚠️ Nécessite que `controller_ready` soit `true` (une trame CD doit avoir été reçue).

```bash
curl -X POST http://raspberrypi4:5000/control/setpoint \
     -H "Content-Type: application/json" \
     -d '{"temperature": 28}'
```

```json
{"status": "ok", "temperature": 28}
```

---

## 7. Dépannage

### Diagnostic complet — port /dev/ttyUSB0

Suivre les étapes dans l'ordre. S'arrêter dès qu'une étape révèle le problème.

#### Étape 1 — L'adaptateur est-il vu par le système USB ?

```bash
lsusb | grep -i ftdi
# Attendu : Bus 00X Device 00Y: ID 0403:6001 Future Technology Devices International, Ltd FT232 Serial (UART) IC
```

Si rien n'apparaît : vérifier le câble USB, essayer un autre port USB du RPi.

#### Étape 2 — Le kernel a-t-il créé le nœud série ?

```bash
ls /dev/ttyUSB* 2>/dev/null || echo "Aucun ttyUSB présent"
dmesg | grep -E "ttyUSB|ftdi|FTDI|FT232" | tail -20
```

Sortie normale au branchement :
```
usb 1-1.2: new full-speed USB device number 3 using xhci_hcd
usb 1-1.2: New USB device found, idVendor=0403, idProduct=6001
ftdi_sio 1-1.2:1.0: FTDI USB Serial Device converter detected
usb 1-1.2: FTDI USB Serial Device converter now attached to ttyUSB0
```

Si `attached to ttyUSB0` est suivi de `disconnected` : voir **Étape 3 (brltty)**.

#### Étape 3 — brltty vole-t-il le port ? (cause #1 sur Debian)

`brltty` (daemon braille) reconnaît les puces FTDI et les débranche immédiatement.

```bash
# Détecter la présence de brltty
systemctl status brltty 2>/dev/null || echo "brltty absent"
dmesg | grep -i brltty | tail -5
```

Si brltty est actif ou visible dans dmesg → le supprimer définitivement :

```bash
sudo systemctl stop brltty
sudo systemctl disable brltty
sudo apt remove --purge brltty -y

# Débrancher puis rebrancher l'adaptateur USB
ls /dev/ttyUSB*
# Doit afficher : /dev/ttyUSB0
```

#### Étape 4 — Permissions du nœud

```bash
ls -la /dev/ttyUSB0
# Attendu : crw-rw---- 1 root dialout ... /dev/ttyUSB0

groups pi
# Doit contenir : dialout
```

Si `pi` n'est pas dans `dialout` :

```bash
sudo usermod -a -G dialout pi
# Déconnecter / reconnecter la session SSH pour que le groupe prenne effet
```

#### Étape 5 — Le port est-il déjà utilisé par un autre process ?

```bash
lsof /dev/ttyUSB0 2>/dev/null
# Si une ligne apparaît : un autre process tient le port
```

Cas courant : le service `poolex` est déjà en cours et bloque le port pour un test manuel.

```bash
sudo systemctl stop poolex
# Puis relancer le test manuel
```

#### Étape 6 — Test de lecture brute (validation end-to-end)

Vérifie que des octets arrivent réellement depuis le bus RS485 :

```bash
# Lecture brute pendant 5 secondes (9600 baud 8N1)
timeout 5 cat /dev/ttyUSB0 | xxd | head -20
```

Si des octets apparaissent : la capture fonctionne, le problème est logiciel.
Si rien n'apparaît mais que le port existe : vérifier le branchement A+/B- et le switch 120Ω (doit être OFF).

#### Étape 7 — Test avec pyserial en direct

```bash
/opt/poolex-control/venv/bin/python3 - <<'EOF'
import serial, time
s = serial.Serial('/dev/ttyUSB0', 9600, timeout=2)
print("Port ouvert :", s.name)
data = s.read(80)
print(f"Octets reçus : {len(data)}")
if data:
    print("Premier octet (header) :", hex(data[0]))
s.close()
EOF
```

#### Récapitulatif des causes fréquentes

| Symptôme | Cause probable | Solution |
|----------|---------------|----------|
| `lsusb` vide | Câble ou port USB défaillant | Changer câble / port |
| `ttyUSB0` crée puis disparaît | `brltty` vole l'adaptateur | `apt remove brltty` |
| `Permission denied` | `pi` hors groupe `dialout` | `usermod -a -G dialout pi` |
| `lsof` montre un process | Port déjà ouvert | Arrêter le service `poolex` |
| Port OK, 0 octets reçus | Branchement A+/B- inversé ou switch 120Ω ON | Vérifier câblage |
| Octets reçus, trames invalides | Baud rate ou câblage bruit | Vérifier 9600 baud, longueur câble |

### Le service ne démarre pas

```bash
journalctl -u poolex -n 50 --no-pager
```

| Erreur | Cause | Solution |
|--------|-------|----------|
| `No such file or directory: '/dev/ttyUSB0'` | Adaptateur non branché | Normal, le service réessaie toutes les 10s |
| `Permission denied: '/dev/ttyUSB0'` | Utilisateur hors du groupe dialout | `sudo usermod -a -G dialout $USER` puis reconnexion SSH |
| `ModuleNotFoundError` | Venv absent ou incomplet | `python3 -m venv /opt/poolex-control/venv && /opt/poolex-control/venv/bin/pip install -r /opt/poolex-control/requirements.txt` |

### Le deploy GitHub Actions échoue

```bash
# Vérifier le runner
systemctl status "actions.runner.*"

# Vérifier les permissions du répertoire de déploiement
ls -la /opt/ | grep poolex
# doit afficher : drwxr-xr-x ... pi pi ... poolex-control

# Corriger si nécessaire
sudo chown -R pi:pi /opt/poolex-control
```

### sudo est cassé (erreur syntax dans sudoers)

```bash
# Utiliser su pour passer root
su -

# Supprimer le fichier cassé
rm /etc/sudoers.d/poolex

# Recréer correctement (remplacer 'pi' par ton utilisateur)
printf 'pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl daemon-reload\n' > /etc/sudoers.d/poolex
printf 'pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart poolex\n' >> /etc/sudoers.d/poolex
chmod 440 /etc/sudoers.d/poolex
exit
```

> ⚠️ Ne jamais mettre `user:group` dans sudoers — le caractère `:` est réservé et provoque une erreur de syntaxe.

---

## 8. Protocole de test guidé

Interface web permettant de conduire des tests provoqués pour valider le décodage
des trames RS485 par corrélation action physique → changement de bytes.

### Architecture de la session

```
Claude (SSH)          Opérateur (téléphone/PC près de la PAC)
─────────────         ──────────────────────────────────────
POST /test/api/start  →  Interface affiche l'étape 0 (relevé initial)
                         Opérateur saisit les valeurs et confirme
POST /test/api/next_step → Interface affiche : "Appuyez 1x sur ▲"
                           Opérateur appuie sur la télécommande PAC
                           Opérateur appuie sur FAIT
                           → Timestamp précis enregistré
                           → Capture RS485 pendant 2 min
                           → Analyse des bytes changés
POST /test/api/next_step → Étape suivante...
GET  /test/api/report    → Rapport JSON complet
```

### Modèle de timing

| Événement | Timestamp enregistré |
|-----------|----------------------|
| Étape présentée à l'opérateur | `step_presented_at` |
| Opérateur appuie sur FAIT | `operator_confirmed_at` |
| Début de capture des trames post-action | `capture_start_at` |
| Fin de capture (fenêtre 2 min) | `capture_end_at` |

> L'opérateur confirme **après avoir terminé** les pressions de boutons.
> La fenêtre de capture de 2 minutes démarre à ce moment pour laisser le temps
> à la PAC de propager le changement sur le bus RS485.

### Protocole de test initial (consigne de chauffe)

| Étape | Action demandée | Appuis bouton |
|-------|----------------|---------------|
| 0 | Relevé baseline (temp ext, eau, consigne affichés) | — |
| 1 | Consigne +1°C | 1× bouton ▲ |
| 2 | Consigne +2°C supplémentaires | 2× bouton ▲ |
| 3 | Consigne −2°C | 2× bouton ▼ |
| 4 | Consigne −1°C (retour initial) | 1× bouton ▼ |

### Lancer une session

**Opérateur** : ouvrir `http://raspberrypi4:5000/test` sur téléphone ou PC.

**Claude (via SSH)** :

```bash
# 1. Démarrer la session
ssh poolex-rpi "curl -s -X POST http://localhost:5000/test/api/start | python3 -m json.tool"

# 2. Avancer après confirmation de l'opérateur (répéter pour chaque étape)
ssh poolex-rpi "curl -s -X POST http://localhost:5000/test/api/next_step | python3 -m json.tool"

# 3. Lire le rapport final (corrélations action → bytes RS485)
ssh poolex-rpi "curl -s http://localhost:5000/test/api/report | python3 -m json.tool"
```

### Format du rapport

```json
{
  "started_at": "2025-08-17T17:43:00Z",
  "baseline": {
    "temp_ext_display": 25,
    "temp_eau_display": 28,
    "consigne_display": 27
  },
  "events": [
    {
      "step_id": 1,
      "label": "Consigne +1°C",
      "delta": 1,
      "step_presented_at": "2025-08-17T17:43:15Z",
      "operator_confirmed_at": "2025-08-17T17:43:28Z",
      "operator_delay_s": 13.2,
      "capture_window_s": 120,
      "frames_collected": {"DD": 20, "CD": 3, "D2": 20},
      "analysis": {
        "CD": {
          "11": {"before": 27, "after": 28, "hex_before": "0x1B", "hex_after": "0x1C"}
        }
      }
    }
  ]
}
```
