# ESP32-CAM Eye Firmware

Firmware complet pour ESP32-CAM (modele AI Thinker) jouant le role d'une camera connectee autonome avec configuration WiFi et reglages capteur via HTTP.

## Fonctionnalites principales
- Demarrage en STA avec identifiants stockes dans NVS (`CONFIG`). Retour en AP apres 15 s si la connexion echoue.
- Point d'acces `ESP32-CAM-SETUP-XXXX` (XXXX = 4 derniers hex du MAC) avec mot de passe par defaut `esp32setup`.
- Serveur HTTP local (port 80) fournissant les pages `/`, `/wifi`, `/image` et l'endpoint de capture `/capture`. En AP, l'interface est accessible sur `http://192.168.4.1/`.
- API JSON pour lire et ecrire les parametres WiFi et camera (`/api/config/all`, `/api/settings`, `/api/wifi`).
- Reglages persistants: luminosite, contraste, saturation (-2..+2) et framesize (QVGA/VGA/SVGA/XGA/UXGA).
- Sauvegarde instantanee dans NVS puis reboot automatique apres changement WiFi.
- Mise a jour OTA via ArduinoOTA (port 3232) des que l'ESP32 est connecte en STA.
- Portail captif: le firmware fournit un DNS interne redirigeant toutes les requetes vers `192.168.4.1` lorsque l'ESP tourne en AP.

## Compilation et flash
1. Installer l'ESP32 board package dans l'IDE Arduino (gestionnaire de cartes Espressif 2.x ou plus).
2. Selectionner la carte **AI Thinker ESP32-CAM** et regler la partition `Huge APP` pour laisser de la place au binaire.
3. Copier `esp32_cam_eye.ino` dans un nouveau sketch Arduino ou ouvrir directement ce fichier dans l'IDE.
4. Renseigner le port serie de l'ESP32-CAM branchee en mode flash (IO0 a GND) et televerser.
5. Deconnecter IO0 de GND pour redemarrer sur le nouveau firmware.

## Mise a jour OTA
Lorsque l'ESP32-CAM est connecte en mode station, le service OTA s'active automatiquement:
- L'hote apparait sur le reseau sous la forme `ESP32-CAM-EYE-XXXX` (XXXX = 4 derniers hex du MAC).
- Dans l'IDE Arduino, choisissez ce port reseau (menu Outils > Port > Port reseau) puis televersez comme d'habitude. Aucune manipulation des broches n'est necessaire.
- En ligne de commande, vous pouvez aussi utiliser `espota.py -i <ip_du_module> -p 3232 -f firmware.bin`.
- En cas de bascule en mode AP, l'OTA est desactive jusqu'a la prochaine connexion STA reussie.

## Premier demarrage
- Sans configuration WiFi, l'appareil passe automatiquement en AP et diffuse `ESP32-CAM-SETUP-XXXX`.
- Connectez-vous au point d'acces, assurez-vous que votre machine recoit une IP 192.168.4.x/24 puis ouvrez explicitement `http://192.168.4.1/` (pas HTTPS) pour configurer le reseau dans la page **Config WiFi**. Le portail captif redirige aussi toute URL tapee vers cette page.
- Si aucune page ne s'affiche, verifiez que `ping 192.168.4.1` repond, desactivez les VPN/donnees mobiles automatiques et relancez le navigateur.
- Apres sauvegarde, l'ESP redemarre et tente une connexion STA. En cas d'echec, il revient en AP pour permettre une nouvelle configuration.

## API HTTP
| Methode | URL | Description |
| --- | --- | --- |
| GET | `/` | Page d'accueil (etat WiFi + liens rapides). |
| GET | `/wifi` | Formulaire pour enregistrer SSID/mot de passe. POST JSON vers `/api/wifi`. |
| GET | `/image` | Reglages camera (sliders + select framesize). POST JSON vers `/api/settings`. |
| GET | `/capture` | Capture JPEG immediate avec `Content-Type: image/jpeg`. |
| GET | `/api/config/all` | Retourne le JSON complet (wifi_ssid + parametres image). |
| GET | `/api/settings` | Retourne les parametres image + SSID. |
| POST | `/api/settings` | Applique et sauvegarde les reglages image (JSON). |
| POST | `/api/wifi` | Sauvegarde SSID/mot de passe puis redemarre. |

### Exemple de charge utile `/api/settings`
```json
{
  "brightness": 1,
  "contrast": 0,
  "saturation": -1,
  "framesize": "VGA"
}
```

### Exemple de charge utile `/api/wifi`
```json
{
  "ssid": "MonReseau",
  "password": "MonSecret"
}
```

## Parametres sauvegardes (namespace `CONFIG`)
- `wifi_ssid`
- `wifi_password`
- `img_brightness`
- `img_contrast`
- `img_saturation`
- `img_framesize`

Aucun autre fichier de configuration n'est necessaire: toutes les pages web et valeurs par defaut sont integrees au firmware.
