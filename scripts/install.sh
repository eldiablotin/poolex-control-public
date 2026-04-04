#!/usr/bin/env bash
# =============================================================================
# install.sh — Installation initiale de poolex-control sur le Raspberry Pi
# À exécuter UNE SEULE FOIS depuis le répertoire du repo cloné.
# =============================================================================
set -euo pipefail

# Répertoire racine du repo, indépendamment d'où le script est lancé
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
sudo apt-get install -y python3 python3-venv git mosquitto mosquitto-clients

# 2. Utilisateur MQTT mosquitto
echo ""
echo "[2/7] Création de l'utilisateur MQTT 'poolex'..."
MQTT_PASS="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
sudo mosquitto_passwd -c -b /etc/mosquitto/passwd poolex "${MQTT_PASS}"
# Activer l'authentification dans mosquitto
cat <<MCONF | sudo tee /etc/mosquitto/conf.d/auth.conf > /dev/null
allow_anonymous false
password_file /etc/mosquitto/passwd
MCONF
sudo systemctl restart mosquitto
echo ""
echo "  *** MOT DE PASSE MQTT GÉNÉRÉ (À COPIER DANS GITHUB SECRETS) ***"
echo "      Nom du secret : POOLEX_MQTT_PASSWORD"
echo "      Valeur        : ${MQTT_PASS}"
echo "  → https://github.com/eldiablotin/poolex-control/settings/secrets/actions"
echo ""
# Créer l'env file local immédiatement pour le premier démarrage
sudo mkdir -p "$DEPLOY_DIR"
printf 'POOLEX_MQTT_PASSWORD=%s\n' "${MQTT_PASS}" | sudo tee /opt/poolex-control/poolex.env > /dev/null
sudo chmod 600 /opt/poolex-control/poolex.env

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
# poolex.env déjà créé à l'étape 2, ajuster l'ownership
sudo chown root:"$SERVICE_USER" /opt/poolex-control/poolex.env

# 5. Environnement virtuel Python
echo ""
echo "[5/7] Création du virtualenv et installation des dépendances..."
python3 -m venv "$DEPLOY_DIR/venv"
"$DEPLOY_DIR/venv/bin/pip" install --upgrade pip -q
"$DEPLOY_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt" -q

# Déploiement initial des fichiers
cp -r "$REPO_DIR/." "$DEPLOY_DIR/"

# 6. Service systemd
echo ""
echo "[6/7] Installation du service systemd..."
sed "s/__SERVICE_USER__/$SERVICE_USER/" "$REPO_DIR/scripts/poolex.service" \
    | sudo tee /etc/systemd/system/poolex.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable poolex

# 7. Sudoers : uniquement le restart du service (sans mot de passe pour le runner)
echo ""
echo "[7/7] Configuration sudoers (systemctl restart poolex)..."
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
echo " Prochaine étape : enregistrer le runner GitHub Actions"
echo ""
echo "   1. Aller sur :"
echo "      https://github.com/eldiablotin/poolex-control/settings/actions/runners/new"
echo "      Sélectionner : Linux / ARM64"
echo ""
echo "   2. Copier le TOKEN affiché, puis exécuter :"
echo ""
echo "      mkdir -p $RUNNER_DIR && cd $RUNNER_DIR"
echo "      curl -o runner.tar.gz -L \\"
echo "        https://github.com/actions/runner/releases/latest/download/actions-runner-linux-arm64.tar.gz"
echo "      tar xzf runner.tar.gz"
echo "      ./config.sh --url $REPO_URL --token <TOKEN>"
echo "      sudo ./svc.sh install && sudo ./svc.sh start"
echo ""
echo " Démarrer le service manuellement pour tester :"
echo "   sudo systemctl start poolex"
echo "   journalctl -u poolex -f"
echo ""
echo " ⚠️  Rebrancher la session SSH pour activer le groupe dialout."
