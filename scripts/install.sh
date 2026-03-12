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
echo "[1/6] Mise à jour et installation des paquets système..."
sudo apt-get update -q
sudo apt-get install -y python3 python3-venv git

# 2. Groupe dialout (accès /dev/ttyUSB0 sans sudo)
echo ""
echo "[2/6] Ajout de $SERVICE_USER au groupe dialout..."
sudo usermod -a -G dialout "$SERVICE_USER"
echo "      (reconnexion SSH nécessaire pour que ce changement prenne effet)"

# 3. Répertoires de déploiement et de données
echo ""
echo "[3/6] Création des répertoires..."
sudo mkdir -p "$DEPLOY_DIR" "$DATA_DIR"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$DEPLOY_DIR" "$DATA_DIR"

# 4. Environnement virtuel Python
echo ""
echo "[4/6] Création du virtualenv et installation des dépendances..."
python3 -m venv "$DEPLOY_DIR/venv"
"$DEPLOY_DIR/venv/bin/pip" install --upgrade pip -q
"$DEPLOY_DIR/venv/bin/pip" install -r "$REPO_DIR/requirements.txt" -q

# Déploiement initial des fichiers
cp -r "$REPO_DIR/." "$DEPLOY_DIR/"

# 5. Service systemd
echo ""
echo "[5/6] Installation du service systemd..."
sed "s/__SERVICE_USER__/$SERVICE_USER/" "$REPO_DIR/scripts/poolex.service" \
    | sudo tee /etc/systemd/system/poolex.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable poolex

# 6. Sudoers : uniquement le restart du service (sans mot de passe pour le runner)
echo ""
echo "[6/6] Configuration sudoers (systemctl restart poolex)..."
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
