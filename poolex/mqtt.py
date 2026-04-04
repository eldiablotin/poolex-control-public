"""
Intégration MQTT avec Home Assistant autodiscovery.

Variables d'environnement :
  POOLEX_MQTT_HOST     : broker MQTT (défaut: localhost)
  POOLEX_MQTT_PORT     : port (défaut: 1883)
  POOLEX_MQTT_USER     : utilisateur (optionnel)
  POOLEX_MQTT_PASSWORD : mot de passe (optionnel)
  POOLEX_self._prefix   : préfixe des topics (défaut: poolex)

Entités Home Assistant créées :
  - climate   : contrôle on/off + consigne + température courante
  - select    : mode de chauffe (inverter/fix/sun/cooling)
  - sensor    : température eau, température air
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt
from paho.mqtt.client import CallbackAPIVersion

from .controller import MODES

if TYPE_CHECKING:
    from .controller import Controller
    from .storage import Storage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (lue au démarrage pour prendre en compte les variables
# injectées par systemd EnvironmentFile)
# ---------------------------------------------------------------------------

HA_PREFIX   = "homeassistant"
DEVICE_ID   = "poolex_pac"
DEVICE_NAME = "Poolex PAC"

_PUBLISH_INTERVAL = 15   # secondes entre deux publications d'état


class MQTTClient:
    """
    Publie l'état de la PAC sur MQTT et s'abonne aux topics de commande.
    Génère les payloads de découverte Home Assistant au démarrage.
    """

    def __init__(self, controller: "Controller", storage: "Storage") -> None:
        self._controller = controller
        self._storage    = storage

        # Configuration lue à l'instanciation (env vars disponibles au runtime)
        self._host   = os.environ.get("POOLEX_MQTT_HOST", "localhost")
        self._port   = int(os.environ.get("POOLEX_MQTT_PORT", "1883"))
        self._user   = os.environ.get("POOLEX_MQTT_USER", "")
        self._pass   = os.environ.get("POOLEX_MQTT_PASSWORD", "")
        self._prefix = os.environ.get("POOLEX_self._prefix", "poolex")

        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION1,
            client_id="poolex-control",
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        if self._user:
            self._client.username_pw_set(self._user, self._pass)

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    #  Cycle de vie                                                         #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._running = True
        try:
            self._client.connect(self._host, self._port, keepalive=60)
        except Exception:
            logger.exception("Impossible de se connecter au broker MQTT %s:%d", self._host, self._port)
            return
        self._client.loop_start()
        self._thread = threading.Thread(
            target=self._publish_loop, daemon=True, name="mqtt-publisher"
        )
        self._thread.start()
        logger.info("MQTT démarré → %s:%d (prefix=%s)", self._host, self._port, self._prefix)

    def stop(self) -> None:
        self._running = False
        self._client.loop_stop()
        self._client.disconnect()

    # ------------------------------------------------------------------ #
    #  Callbacks MQTT                                                       #
    # ------------------------------------------------------------------ #

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            logger.error("Connexion MQTT refusée (rc=%d)", rc)
            return
        logger.info("Connecté au broker MQTT")
        self._publish_discovery()
        # Abonnements aux commandes
        client.subscribe([
            (f"{self._prefix}/control/setpoint",    0),
            (f"{self._prefix}/control/power",       0),
            (f"{self._prefix}/control/mode",        0),
            # Topics climate HA
            (f"{self._prefix}/climate/temperature/set", 0),
            (f"{self._prefix}/climate/mode/set",    0),
        ])

    def _on_disconnect(self, client, userdata, rc) -> None:
        if rc != 0:
            logger.warning("Déconnexion MQTT inattendue (rc=%d), reconnexion automatique…", rc)

    def _on_message(self, client, userdata, msg) -> None:
        topic   = msg.topic
        payload = msg.payload.decode(errors="replace").strip()
        logger.debug("MQTT reçu %s = %r", topic, payload)

        try:
            if topic in (f"{self._prefix}/control/setpoint",
                         f"{self._prefix}/climate/temperature/set"):
                temp = int(float(payload))
                ok   = self._controller.set_temperature(temp)
                if ok:
                    self.publish_status()

            elif topic in (f"{self._prefix}/control/power",
                           f"{self._prefix}/climate/mode/set"):
                # climate mode: "off" → power off ; tout autre → power on
                if payload.lower() == "off":
                    ok = self._controller.set_power(False)
                elif payload.lower() == "heat":
                    ok = self._controller.set_power(True)
                elif payload.lower() in ("on", "true", "1"):
                    ok = self._controller.set_power(True)
                else:
                    logger.warning("Valeur power inconnue: %r", payload)
                    return
                if ok:
                    self.publish_status()

            elif topic == f"{self._prefix}/control/mode":
                if payload not in MODES:
                    logger.warning("Mode inconnu: %r", payload)
                    return
                ok = self._controller.set_mode(payload)
                if ok:
                    self.publish_status()

        except Exception:
            logger.exception("Erreur traitement message MQTT %s = %r", topic, payload)

    # ------------------------------------------------------------------ #
    #  Publication état                                                     #
    # ------------------------------------------------------------------ #

    def publish_status(self) -> None:
        """Publie l'état courant sur MQTT."""
        from .decoder import DDFrame
        last_dd    = self._storage.last("DD")
        water_temp = last_dd.water_temp if isinstance(last_dd, DDFrame) else None
        air_temp   = last_dd.air_temp   if isinstance(last_dd, DDFrame) else None

        power   = self._controller.power
        mode    = self._controller.mode
        setpoint = self._controller.setpoint

        # Topic de statut agrégé
        self._publish(f"{self._prefix}/status", json.dumps({
            "water_temp":  water_temp,
            "air_temp":    air_temp,
            "setpoint":    setpoint,
            "power":       power,
            "mode":        mode,
            "controller_ready": self._controller.ready,
        }))

        # Topics individuels (pour HA)
        if water_temp is not None:
            self._publish(f"{self._prefix}/water_temp", str(water_temp))
        if air_temp is not None:
            self._publish(f"{self._prefix}/air_temp", str(air_temp))
        if setpoint is not None:
            self._publish(f"{self._prefix}/setpoint", str(setpoint))
        if power is not None:
            self._publish(f"{self._prefix}/power", "on" if power else "off")
        if mode is not None:
            self._publish(f"{self._prefix}/mode", mode)

        # État climate HA : "heat" si allumé, "off" si éteint
        ha_mode = "off"
        if power:
            ha_mode = "heat"
        self._publish(f"{self._prefix}/climate/mode/state", ha_mode)
        if setpoint is not None:
            self._publish(f"{self._prefix}/climate/temperature/state", str(setpoint))
        if water_temp is not None:
            self._publish(f"{self._prefix}/climate/current_temperature", str(water_temp))

    def _publish_loop(self) -> None:
        while self._running:
            try:
                self.publish_status()
            except Exception:
                logger.exception("Erreur publication MQTT")
            time.sleep(_PUBLISH_INTERVAL)

    def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self._client.publish(topic, payload, qos=0, retain=retain)

    # ------------------------------------------------------------------ #
    #  Home Assistant autodiscovery                                        #
    # ------------------------------------------------------------------ #

    def _publish_discovery(self) -> None:
        device = {
            "identifiers":    [DEVICE_ID],
            "name":           DEVICE_NAME,
            "model":          "Poolex PAC RS485",
            "manufacturer":   "Poolex",
        }

        # --- Climate (on/off + consigne + temp courante) -----------------
        self._publish(
            f"{HA_PREFIX}/climate/{DEVICE_ID}/config",
            json.dumps({
                "unique_id":    f"{DEVICE_ID}_climate",
                "name":         DEVICE_NAME,
                "device":       device,

                "modes":        ["off", "heat"],
                "mode_state_topic":   f"{self._prefix}/climate/mode/state",
                "mode_command_topic": f"{self._prefix}/climate/mode/set",

                "temperature_state_topic":   f"{self._prefix}/climate/temperature/state",
                "temperature_command_topic": f"{self._prefix}/climate/temperature/set",
                "temperature_unit":  "C",
                "min_temp":          8,
                "max_temp":          40,
                "temp_step":         1,

                "current_temperature_topic": f"{self._prefix}/climate/current_temperature",

                "availability_topic": f"{self._prefix}/status",
                "availability_template": "{{ 'online' if value_json.controller_ready else 'offline' }}",
            }),
            retain=True,
        )

        # --- Select : mode de chauffe ------------------------------------
        self._publish(
            f"{HA_PREFIX}/select/{DEVICE_ID}_mode/config",
            json.dumps({
                "unique_id":    f"{DEVICE_ID}_mode",
                "name":         f"{DEVICE_NAME} Mode",
                "device":       device,
                "icon":         "mdi:heat-wave",

                "options":      list(MODES.keys()),
                "state_topic":  f"{self._prefix}/mode",
                "command_topic": f"{self._prefix}/control/mode",

                "availability_topic": f"{self._prefix}/status",
                "availability_template": "{{ 'online' if value_json.controller_ready else 'offline' }}",
            }),
            retain=True,
        )

        # --- Sensor : température eau ------------------------------------
        self._publish(
            f"{HA_PREFIX}/sensor/{DEVICE_ID}_water_temp/config",
            json.dumps({
                "unique_id":           f"{DEVICE_ID}_water_temp",
                "name":                f"{DEVICE_NAME} Température eau",
                "device":              device,
                "device_class":        "temperature",
                "unit_of_measurement": "°C",
                "state_topic":         f"{self._prefix}/water_temp",
                "state_class":         "measurement",
            }),
            retain=True,
        )

        # --- Sensor : température air ------------------------------------
        self._publish(
            f"{HA_PREFIX}/sensor/{DEVICE_ID}_air_temp/config",
            json.dumps({
                "unique_id":           f"{DEVICE_ID}_air_temp",
                "name":                f"{DEVICE_NAME} Température air",
                "device":              device,
                "device_class":        "temperature",
                "unit_of_measurement": "°C",
                "state_topic":         f"{self._prefix}/air_temp",
                "state_class":         "measurement",
            }),
            retain=True,
        )

        logger.info("Home Assistant autodiscovery publié")
