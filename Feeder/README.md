# Feeder

Projet Arduino pour ESP32-C3 Super Mini qui controle un distributeur de nourriture pour poissons via un servo MF90, un serveur web embarque et une integration MQTT compatible Home Assistant.

## Fichiers principaux

- `Feeder.ino` : logique principale (Wi-Fi, AP, bouton, servo, MQTT, serveur web).
- `config.h` : constantes materielle et parametres par defaut.
- `webpage.h` : page HTML de configuration et de controle (stockee en PROGMEM).

## Fonctionnalites

- Connexion Wi-Fi STA avec bascule automatique en mode point d'acces `FishFeeder-XXXX` si aucun reseau n'est configure ou disponible.
- Portail web simple pour declencher le nourrissage et configurer Wi-Fi / MQTT.
- Gestion du bouton physique (appui court = nourrir, appui long = mode AP).
- Servo MF90 pilote (90 degres aller/retour) avec temporisation ajustable.
- Publication MQTT (`<baseTopic>/state`, `<baseTopic>/availability`) et ecoute des commandes (`<baseTopic>/command` avec payload `FEED`).
- Sauvegarde des parametres dans la NVS via `Preferences`.

## Bibliotheques requises

- ESP32 Servo (disponible dans l'ESP32 Arduino core)
- `WiFi.h`, `WebServer.h`, `DNSServer.h` (ESP32 Arduino core)
- `Preferences.h` (ESP32 Arduino core)
- `PubSubClient`

## Compilation

1. Installer le core ESP32 pour Arduino IDE.
2. Selectionner la carte **ESP32C3 Dev Module** ou **ESP32-C3 SuperMini**.
3. Ajouter les bibliotheques listees.
4. Ouvrir `Feeder.ino`, verifier les parametres dans `config.h`, televerser.

