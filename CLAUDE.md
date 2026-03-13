# poolex-control — Instructions pour Claude

## Accès SSH au Raspberry Pi

Le RPi est directement accessible depuis cette machine via SSH :

```bash
ssh poolex-rpi          # alias défini dans ~/.ssh/config
# ou
ssh pi@raspberrypi4     # connexion directe (mDNS, clé RSA configurée)
```

Clé privée : `~/.ssh/poolex_rpi`
Config SSH : `~/.ssh/config` (entrée `poolex-rpi`)

**Utiliser `ssh poolex-rpi` pour toutes les commandes distantes** (restart service, curl, dmesg, journalctl…).

## Infrastructure

- **Service systemd** : `poolex` — déployé dans `/opt/poolex-control/`
- **Venv Python** : `/opt/poolex-control/venv/`
- **Base de données** : `/var/lib/poolex/poolex.db`
- **Port série RS485** : `/dev/ttyUSB0` (Waveshare FT232RNL)
- **API Flask** : `http://raspberrypi4:5000/` (écoute sur `0.0.0.0:5000`)

## Commandes courantes

```bash
# Statut du service
ssh poolex-rpi "systemctl status poolex --no-pager"

# Logs temps réel
ssh poolex-rpi "journalctl -u poolex -f"

# Redémarrer le service
ssh poolex-rpi "sudo systemctl restart poolex"

# Tester l'API
curl http://raspberrypi4:5000/status
curl http://raspberrypi4:5000/frames/stats

# Vérifier le port série
ssh poolex-rpi "ls /dev/ttyUSB* && lsusb | grep -i ftdi"
```

## Déploiement

Un `git push` sur `main` déclenche automatiquement via GitHub Actions :
1. CI (lint + tests) sur GitHub cloud
2. Deploy sur le RPi (runner self-hosted) + restart service

## Protocole RS485

- 80 octets fixes, 9600 baud 8N1
- Headers : `DD` (PAC→télécommande), `D2`/`CC` (télécommande→PAC), `CD` (commande consigne)
- Consigne température : trame CD, `byte[11]`
- Température eau : trame DD, `byte[22] / 2`
- Température air : trame DD, `byte[29]`
