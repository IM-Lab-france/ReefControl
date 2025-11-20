#!/bin/bash
set -e

# ==============================================================================
# Script d'installation pour InfluxDB et Grafana avec Docker sur Ubuntu
# ==============================================================================

echo "--- Début de l'installation de la stack de monitoring (InfluxDB + Grafana) ---"

# --- 1. Installation de Docker et Docker Compose ---
écho -e "\n--- Étape 1: Installation de Docker et Docker Compose ---"
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update

sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "Ajout de l'utilisateur courant au groupe docker..."
sudo usermod -aG docker $USER
echo "NOTE : Vous devrez vous déconnecter et vous reconnecter pour que les changements de groupe prennent effet."


# --- 2. Création de la structure des répertoires ---
écho -e "\n--- Étape 2: Création des répertoires de données persistantes ---"
DATA_DIR="/srv/reef-controller-data"
INFLUX_DIR="$DATA_DIR/influxdb2"
GRAFANA_DIR="$DATA_DIR/grafana"

sudo mkdir -p $INFLUX_DIR
sudo mkdir -p $GRAFANA_DIR
sudo chown -R $USER:$USER $DATA_DIR

echo "Répertoires créés dans $DATA_DIR"


# --- 3. Génération des identifiants sécurisés ---
écho -e "\n--- Étape 3: Génération des identifiants pour InfluxDB ---"
INFLUXDB_USER="admin"
# Génère un mot de passe de 16 caractères alphanumériques
INFLUXDB_PASSWORD=$(openssl rand -base64 12)
INFLUXDB_ORG="reef-controller"
INFLUXDB_BUCKET="reef-data"
# Génère un token d'API long et sécurisé
INFLUXDB_TOKEN=$(openssl rand -hex 32)

echo "Identifiants générés."


# --- 4. Création du fichier docker-compose.yml ---
écho -e "\n--- Étape 4: Création du fichier docker-compose.yml ---"
cat <<EOF > docker-compose.yml
version: '3.8'

services:
  influxdb:
    image: influxdb:2.7
    container_name: influxdb_reef
    restart: unless-stopped
    ports:
      - "8086:8086"
    volumes:
      - '$INFLUX_DIR:/var/lib/influxdb2'
    environment:
      - DOCKER_INFLUXDB_INIT_MODE=setup
      - DOCKER_INFLUXDB_INIT_USERNAME=${INFLUXDB_USER}
      - DOCKER_INFLUXDB_INIT_PASSWORD=${INFLUXDB_PASSWORD}
      - DOCKER_INFLUXDB_INIT_ORG=${INFLUXDB_ORG}
      - DOCKER_INFLUXDB_INIT_BUCKET=${INFLUXDB_BUCKET}
      - DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=${INFLUXDB_TOKEN}

  grafana:
    image: grafana/grafana-oss:latest
    container_name: grafana_reef
    restart: unless-stopped
    ports:
      - "3000:3000"
    volumes:
      - '$GRAFANA_DIR:/var/lib/grafana'
    depends_on:
      - influxdb

networks:
  default:
    name: reef-net
EOF

echo "Fichier docker-compose.yml créé."


# --- 5. Lancement des conteneurs ---
écho -e "\n--- Étape 5: Lancement des conteneurs Docker ---"
echo "Cela peut prendre quelques minutes pour le premier téléchargement des images..."
# Il faut utiliser `newgrp docker` pour exécuter la commande dans un shell avec le nouveau groupe
# si l'utilisateur ne s'est pas encore déconnecté/reconnecté.
newgrp docker << END
docker compose up -d
END

echo "Conteneurs lancés avec succès."


# --- 6. Affichage des informations de connexion ---
écho -e "\n\n=============================================================================="
echo "    🚀 Installation terminée ! Sauvegardez précieusement ces informations. 🚀"
echo "=============================================================================="
echo ""
echo "--- Grafana ---"
echo "URL:          http://<IP_DE_VOTRE_VM>:3000"
echo "Utilisateur:  admin"
echo "Mot de passe:   admin (il vous sera demandé de le changer à la première connexion)"
echo ""
echo "--- InfluxDB ---"
echo "URL:          http://<IP_DE_VOTRE_VM>:8086"
echo "Organisation: ${INFLUXDB_ORG}"
echo "Bucket:       ${INFLUXDB_BUCKET}"
echo "Utilisateur:  ${INFLUXDB_USER}"
echo "Mot de passe:   ${INFLUXDB_PASSWORD}"
echo ""
echo "--- Token d'API (pour Python et Grafana) ---"
echo "Token:        ${INFLUXDB_TOKEN}"
echo ""
echo "=============================================================================="
echo "IMPORTANT : Pour utiliser Docker sans 'sudo', vous devez vous déconnecter"
echo "et vous reconnecter à votre session Ubuntu."
echo "=============================================================================="
echo -e "\nProchaines étapes recommandées :"
echo "1. Accédez à Grafana et changez le mot de passe."
echo "2. Dans Grafana, ajoutez une source de données de type 'InfluxDB' en utilisant le Token ci-dessus."
echo "3. Modifiez votre script 'controller.py' pour envoyer les données à InfluxDB."
