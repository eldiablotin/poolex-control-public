"""
API REST Flask — point d'entrée principal de l'application.

Endpoints :
  GET  /status                → état courant décodé (temp eau, air, consigne)
  GET  /frames?header=DD&limit=20  → dernières trames brutes
  GET  /frames/stats          → comptage par type
  POST /control/setpoint  {"temperature": 28}  → envoie une nouvelle consigne
"""
from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, request

from .capture import RS485Capture
from .controller import Controller
from .decoder import CDFrame, DDFrame
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
controller = Controller(capture)   # enveloppe _on_frame et intercepte les CD


# ---------------------------------------------------------------------------
#  Routes
# ---------------------------------------------------------------------------

@app.get("/status")
def status():
    last_dd = storage.last("DD")
    last_cd = storage.last("CD")
    return jsonify({
        "water_temp":          last_dd.water_temp if isinstance(last_dd, DDFrame) else None,
        "air_temp":            last_dd.air_temp   if isinstance(last_dd, DDFrame) else None,
        "mode":                last_dd.mode_byte  if isinstance(last_dd, DDFrame) else None,
        "setpoint":            last_cd.setpoint   if isinstance(last_cd, CDFrame) else None,
        "controller_ready":    controller.has_template,
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
        sent = controller.set_temperature(temp)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not sent:
        return jsonify({"error": "Pas encore de trame CD disponible comme template"}), 503

    return jsonify({"status": "ok", "temperature": temp})


# ---------------------------------------------------------------------------
#  Démarrage
# ---------------------------------------------------------------------------

def run() -> None:
    capture.start()
    logger.info("API démarrée sur le port %d", API_PORT)
    app.run(host="0.0.0.0", port=API_PORT, debug=False, use_reloader=False)
