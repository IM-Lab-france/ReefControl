#pragma once

#include <pgmspace.h>

const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Fish Feeder</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: sans-serif; margin: 20px; max-width: 720px; }
    h2 { margin-bottom: 0.1em; }
    form { margin-bottom: 1.5em; }
    label { display: block; font-weight: 600; margin-top: 0.5em; }
    input, button { padding: 0.6em; width: 100%; max-width: 420px; box-sizing: border-box; margin-top: 0.25em; }
    button { cursor: pointer; }
    .actions { display: flex; gap: 0.75em; flex-wrap: wrap; }
    .actions form { margin-bottom: 0; }
    .hint { font-size: 0.9em; color: #666; margin-top: 0.3em; }
    .modal { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45); display: flex; align-items: center; justify-content: center; padding: 1em; }
    .modal.hidden { display: none; }
    .modal-box { background: #fff; color: #000; padding: 1.5em; border-radius: 8px; max-width: 420px; width: 100%; box-shadow: 0 10px 40px rgba(0,0,0,0.35); }
    .modal-box.error { border-left: 4px solid #c0392b; }
    .modal-box p { margin: 0 0 1em 0; white-space: pre-wrap; }
    .modal-box button { width: auto; }
  </style>
</head>
<body>
  <h2>ESP32-C3 Fish Feeder</h2>
  <p>Utilisez cette interface pour nourrir vos poissons, configurer le Wi-Fi et le broker MQTT.</p>

  <div class="actions">
    <form action="/feed" method="POST" class="ajax-form">
      <button type="submit">Nourrir maintenant</button>
    </form>
    <form action="/restart" method="POST" class="ajax-form" onsubmit="return confirm('Redemarrer l\'ESP32-C3 ?');">
      <button type="submit">Redemarrer</button>
    </form>
  </div>

  <hr>
  <form action="/saveWifi" method="POST" class="ajax-form">
    <h3>Configuration Wi-Fi</h3>
    <label>SSID</label>
    <input name="ssid" placeholder="Nom du reseau">
    <label>Mot de passe</label>
    <input name="pass" type="password" placeholder="********">
    <button type="submit">Enregistrer le Wi-Fi</button>
    <p class="hint">L'ESP redemarre automatiquement apres l'enregistrement.</p>
  </form>

  <hr>
  <form action="/saveMqtt" method="POST" class="ajax-form">
    <h3>Configuration MQTT</h3>
    <label>MQTT Host</label>
    <input name="host" placeholder="192.168.1.10">
    <label>Port</label>
    <input name="port" value="1883">
    <label>Base topic</label>
    <input name="base" placeholder="aquarium/feeder">
    <label>Utilisateur</label>
    <input name="user" placeholder="username">
    <label>Mot de passe</label>
    <input name="pwd" type="password" placeholder="********">
    <button type="submit">Enregistrer MQTT</button>
  </form>

  <hr>
  <form action="/saveServo" method="POST" class="ajax-form">
    <h3>Parametres Servo</h3>
    <label>Angle d'ouverture (degres)</label>
    <input name="openAngle" type="number" min="-720" max="720" placeholder="90" data-autosubmit="true">
    <label>Angle de fermeture (degres)</label>
    <input name="closeAngle" type="number" min="-720" max="720" placeholder="0" data-autosubmit="true">
    <label>Temps ouvert (ms)</label>
    <input name="openDelay" type="number" min="0" placeholder="600">
    <label>Vitesse d'ouverture (%)</label>
    <input name="speed" type="number" min="1" max="100" placeholder="100">
    <button type="submit">Enregistrer Servo</button>
  </form>

  <hr>
  <section>
    <h3>Informations</h3>
    <p>Un appui court sur le bouton physique declenche le nourrissage.</p>
    <p>Un appui long (&gt; 3 s) force le mode point d'acces pour reconfigurer le reseau.</p>
  </section>

  <div id="modal" class="modal hidden" role="dialog" aria-modal="true">
    <div id="modalBox" class="modal-box">
      <p id="modalMessage"></p>
      <button type="button" id="modalClose">Fermer</button>
    </div>
  </div>

  <script>
    function setFieldValue(name, value) {
      var input = document.querySelector('[name="' + name + '"]');
      if (!input) {
        return;
      }
      if (value === undefined || value === null) {
        input.value = '';
      } else {
        input.value = value;
      }
    }

    async function loadConfig() {
      try {
        var response = await fetch('/status', { cache: 'no-store' });
        if (!response.ok) {
          return;
        }
        var data = await response.json();
        setFieldValue('ssid', data.wifiSsid);
        setFieldValue('pass', data.wifiPass);
        setFieldValue('host', data.mqttHost);
        setFieldValue('port', data.mqttPort);
        setFieldValue('base', data.mqttBase);
        setFieldValue('user', data.mqttUser);
        setFieldValue('pwd', data.mqttPass);
        setFieldValue('openAngle', data.servoOpenAngle);
        setFieldValue('closeAngle', data.servoCloseAngle);
        setFieldValue('openDelay', data.servoOpenDelayMs);
        setFieldValue('speed', data.servoSpeedPercent);
        var minAngle = (typeof data.servoMinAngle !== 'undefined') ? data.servoMinAngle : -720;
        var maxAngle = (typeof data.servoMaxAngle !== 'undefined') ? data.servoMaxAngle : 720;
        ['openAngle', 'closeAngle'].forEach(function(field) {
          var el = document.querySelector('[name="' + field + '"]');
          if (el) {
            el.min = minAngle;
            el.max = maxAngle;
          }
        });
      } catch (error) {
        console.warn('Config load failed', error);
      }
    }

    async function submitAjaxForm(form, opts) {
      opts = opts || {};
      var silent = !!opts.silent;
      var formData = new FormData(form);
      var changedField = opts.changedField || '';
      var method = (form.method || 'POST').toUpperCase();
      var options = { method: method };
      if (method !== 'GET') {
        if (changedField) {
          formData.set('changedField', changedField);
        }
        options.body = formData;
      }
      try {
        var response = await fetch(form.action, options);
        var text = await response.text();
        if (response.ok) {
          if (!silent) {
            showModal(text || 'Operation terminee.', false);
          }
          await loadConfig();
        } else {
          if (!silent) {
            showModal(text || 'Une erreur est survenue.', true);
          } else {
            console.warn('Form submission error:', text);
          }
        }
      } catch (err) {
        if (!silent) {
          showModal('Erreur: ' + err.message, true);
        } else {
          console.warn('Form submission error:', err);
        }
      }
    }

    function showModal(message, isError) {
      var modal = document.getElementById('modal');
      var box = document.getElementById('modalBox');
      var textNode = document.getElementById('modalMessage');
      textNode.textContent = message;
      if (isError) {
        box.classList.add('error');
      } else {
        box.classList.remove('error');
      }
      modal.classList.remove('hidden');
    }

    function hideModal() {
      document.getElementById('modal').classList.add('hidden');
    }

    document.addEventListener('DOMContentLoaded', function() {
      loadConfig();
      document.addEventListener('submit', function(event) {
        var form = event.target;
        if (form.classList && form.classList.contains('ajax-form')) {
          event.preventDefault();
          submitAjaxForm(form);
        }
      });
      document.querySelectorAll('[data-autosubmit="true"]').forEach(function(input) {
        var handler = function() {
          var form = input.form;
          if (form && form.classList.contains('ajax-form')) {
            submitAjaxForm(form, { silent: true, changedField: input.name });
          }
        };
        input.addEventListener('change', handler);
        input.addEventListener('input', handler);
      });
      document.getElementById('modalClose').addEventListener('click', hideModal);
      document.getElementById('modal').addEventListener('click', function(event) {
        if (event.target === event.currentTarget) {
          hideModal();
        }
      });
    });
  </script>
</body>
</html>
)rawliteral";
