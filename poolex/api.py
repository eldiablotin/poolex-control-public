"""
API REST Flask — point d'entrée principal de l'application.

Endpoints :
  GET  /status                          → état courant (temp eau, air, consigne, power)
  GET  /frames?header=DD&limit=20       → dernières trames brutes
  GET  /frames/stats                    → comptage par type
  POST /control/setpoint {"temperature": 28}   → change la consigne °C
  POST /control/power    {"state": "on"/"off"} → allume / éteint la PAC
  POST /control/mode     {"mode": "inverter"}  → change le mode de chauffe
"""
from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, request

from .capture import RS485Capture
from .controller import Controller, MODES
from .decoder import DDFrame
from .mqtt import MQTTClient
from .storage import Storage
from .test_protocol import bp as test_bp

logger = logging.getLogger(__name__)

SERIAL_PORT = os.environ.get("POOLEX_SERIAL_PORT", "/dev/ttyUSB0")
DB_PATH     = os.environ.get("POOLEX_DB_PATH",     "/var/lib/poolex/poolex.db")
API_PORT    = int(os.environ.get("POOLEX_API_PORT", "5000"))

app = Flask(__name__)
app.register_blueprint(test_bp)

# -- Initialisation des composants ------------------------------------------
# L'ordre est important : storage d'abord, puis capture avec callback,
# puis controller qui enveloppe le callback.

storage = Storage(DB_PATH)

def _on_frame(frame):
    storage.save(frame)

capture    = RS485Capture(port=SERIAL_PORT, on_frame=_on_frame)
controller = Controller(capture, storage=storage)   # réactif sur D2 de la PAC, commande via CD
mqtt_client = MQTTClient(controller, storage)


# ---------------------------------------------------------------------------
#  Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return jsonify({
        "service": "poolex-control",
        "endpoints": [
            "GET  /status",
            "GET  /frames?header=DD&limit=20",
            "GET  /frames/stats",
            "POST /control/setpoint  {temperature: int}",
            "POST /control/power     {state: on|off}",
            "POST /control/mode      {mode: inverter|fix|sun|cooling}",
        ],
    })


@app.get("/status")
def status():
    last_dd = storage.last("DD")
    return jsonify({
        "water_temp":       last_dd.water_temp if isinstance(last_dd, DDFrame) else None,
        "air_temp":         last_dd.air_temp   if isinstance(last_dd, DDFrame) else None,
        "pac_mode":         last_dd.mode_byte  if isinstance(last_dd, DDFrame) else None,
        "setpoint":         controller.setpoint,
        "power":            controller.power,
        "mode":             controller.mode,
        "controller_ready": controller.ready,
    })


@app.get("/frames")
def get_frames():
    header = (request.args.get("header", "") or "").upper() or None
    limit  = min(int(request.args.get("limit", 20)), 200)
    frames = storage.recent(header=header, limit=limit)
    return jsonify([{"header": f.name, "raw": f.raw.hex()} for f in frames])


@app.get("/frames/stats")
def frame_stats():
    return jsonify(storage.stats())


@app.post("/control/setpoint")
def set_setpoint():
    body = request.get_json(silent=True) or {}
    temp = body.get("temperature")

    if temp is None:
        return jsonify({"error": "Paramètre 'temperature' manquant"}), 400
    try:
        temp = int(temp)
    except (TypeError, ValueError):
        return jsonify({"error": "Valeur de température invalide"}), 400

    try:
        ok = controller.set_temperature(temp)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not ok:
        return jsonify({"error": "Contrôleur pas encore prêt (templates D2/CC non capturés)"}), 503

    mqtt_client.publish_status()
    return jsonify({"status": "ok", "temperature": temp})


@app.post("/control/mode")
def set_mode():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode")

    if mode not in MODES:
        return jsonify({"error": f"Mode invalide. Valeurs : {list(MODES)}"}), 400

    ok = controller.set_mode(mode)
    if not ok:
        return jsonify({"error": "Contrôleur pas encore prêt"}), 503

    mqtt_client.publish_status()
    return jsonify({"status": "ok", "mode": mode})


@app.post("/control/power")
def set_power():
    body = request.get_json(silent=True) or {}
    state = body.get("state")

    if state not in ("on", "off"):
        return jsonify({"error": "Paramètre 'state' doit être 'on' ou 'off'"}), 400

    ok = controller.set_power(state == "on")
    if not ok:
        return jsonify({"error": "Contrôleur pas encore prêt (templates D2/CC non capturés)"}), 503

    mqtt_client.publish_status()
    return jsonify({"status": "ok", "power": state})


# ---------------------------------------------------------------------------
#  Démarrage
# ---------------------------------------------------------------------------

def run() -> None:
    capture.start()
    controller.start()
    mqtt_client.start()
    logger.info("API démarrée sur le port %d", API_PORT)
    app.run(host="0.0.0.0", port=API_PORT, debug=False, use_reloader=False)
