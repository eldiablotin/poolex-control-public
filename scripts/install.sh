#!/usr/bin/env bash
# =============================================================================
# install.sh — Installation initiale de poolex-control sur le Raspberry Pi
# Idempotent : peut être relancé sans risque.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REPO_URL="https://github.com/eldiablotin/poolex-control"
DEPLOY_DIR="/opt/poolex-control"
DATA_DIR="/var/lib/poolex"
SERVICE_USER="$(whoami)"
RUNNER_DIR="$HOME/actions-runner"

echo "======================================================"
echo " Installation Poolex Control"
echo " Utilisateur : $SERVICE_USER"
echo "======================================================"

# 1. Paquets système
echo ""
echo "[1/7] Mise à jour et installation des paquets système..."
sudo apt-get update -q
sudo apt-get install -y python3 python3-venv git openssl mosquitto mosquitto-clients

# 2. Utilisateur MQTT mosquitto (mosquitto 2.x : listener obligatoire)
echo ""
echo "[2/7] Configuration mosquitto + utilisateur MQTT 'poolex'..."

MQTT_PASS="$(openssl rand -hex 20)"
sudo mosquitto_passwd -c -b /etc/mosquitto/passwd poolex "${MQTT_PASS}"
sudo chown mosquitto:mosquitto /etc/mosquitto/passwd

# Remplacer entièrement mosquitto.conf par une config minimale mosquitto 2.x
# (évite tout conflit avec conf.d ou les valeurs par défaut Debian)
sudo rm -f /etc/mosquitto/conf.d/*.conf
sudo tee /etc/mosquitto/mosquitto.conf > /dev/null <<'MCONF'
pid_file /run/mosquitto/mosquitto.pid
persistence true
persistence_location /var/lib/mosquitto/
log_dest stderr
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
MCONF

sudo systemctl restart mosquitto
sudo systemctl is-active mosquitto || { sudo journalctl -u mosquitto -n 20 --no-pager; exit 1; }

echo ""
echo "  *** MOT DE PASSE MQTT GÉNÉRÉ — À COPIER DANS GITHUB SECRETS ***"
echo "      Nom du secret : POOLEX_MQTT_PASSWORD"
echo "      Valeur        : ${MQTT_PASS}"
echo "  → https://github.com/eldiablotin/poolex-control/settings/secrets/actions"
echo ""

# 3. Groupe dialout (accès /dev/ttyUSB0 sans sudo)
echo ""
echo "[3/7] Ajout de $SERVICE_USER au groupe dialout..."
sudo usermod -a -G dialout "$SERVICE_USER"
echo "      (reconnexion SSH nécessaire pour que ce changement prenne effet)"

# 4. Répertoires de déploiement et de données
echo ""
echo "[4/7] Création des répertoires..."
sudo mkdir -p "$DEPLOY_DIR" "$DATA_DIR"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$DEPLOY_DIR" "$DATA_DIR"

# Env file pour le secret MQTT (ne sera écrasé que par le deploy CI)
printf 'POOLEX_MQTT_PASSWORD=%s\n' "${MQTT_PASS}" | sudo tee "$DEPLOY_DIR/poolex.env" > /dev/null
sudo chmod 600 "$DEPLOY_DIR/poolex.env"
sudo chown root:"$SERVICE_USER" "$DEPLOY_DIR/poolex.env"

# 5. Environnement virtuel Python
echo ""
echo "[5/7] Création du virtualenv et installation des dépendances..."
python3 -m venv "$DEPLOY_DIR/venv"
"$DEPLOY_DIR/venv/bin/pip" install --upgrade pip -q
"$DEPLOY_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt" -q

# Déploiement initial des fichiers (le CI prendra le relais ensuite)
find "$REPO_DIR" -maxdepth 1 -mindepth 1 ! -name '.git' \
    -exec cp -r {} "$DEPLOY_DIR/" \;

# 6. Service systemd
echo ""
echo "[6/7] Installation du service systemd..."
sed "s/__SERVICE_USER__/$SERVICE_USER/" "$REPO_DIR/scripts/poolex.service" \
    | sudo tee /etc/systemd/system/poolex.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable poolex

# 7. Sudoers pour le runner CI (systemctl sans mot de passe)
echo ""
echo "[7/7] Configuration sudoers..."
printf '%s ALL=(ALL) NOPASSWD: /usr/bin/systemctl daemon-reload\n' "$SERVICE_USER" \
    | sudo tee    /etc/sudoers.d/poolex > /dev/null
printf '%s ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart poolex\n' "$SERVICE_USER" \
    | sudo tee -a /etc/sudoers.d/poolex > /dev/null
sudo chmod 440 /etc/sudoers.d/poolex

# =============================================================================
echo ""
echo "======================================================"
echo " Installation terminée !"
echo "======================================================"
echo ""
echo " Étapes suivantes :"
echo ""
echo " 1. Ajouter le secret MQTT dans GitHub (affiché ci-dessus)"
echo "    → https://github.com/eldiablotin/poolex-control/settings/secrets/actions"
echo ""
echo " 2. Démarrer le service :"
echo "    sudo systemctl start poolex"
echo "    journalctl -u poolex -f"
echo ""
echo " 3. Enregistrer le runner GitHub Actions (si pas encore fait) :"
echo "    → https://github.com/eldiablotin/poolex-control/settings/actions/runners/new"
echo "    mkdir -p $RUNNER_DIR && cd $RUNNER_DIR"
echo "    curl -o runner.tar.gz -L \\"
echo "      https://github.com/actions/runner/releases/latest/download/actions-runner-linux-arm64.tar.gz"
echo "    tar xzf runner.tar.gz"
echo "    ./config.sh --url $REPO_URL --token <TOKEN>"
echo "    sudo ./svc.sh install pi && sudo ./svc.sh start"
echo ""
echo " ⚠️  Rebrancher la session SSH pour activer le groupe dialout."
