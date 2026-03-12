"""
Interface de test guidé pour le reverse engineering du protocole RS485.

Architecture :
  - Ce module expose un Blueprint Flask monté sur /test
  - Claude (via SSH) contrôle l'avancement des étapes via l'API /test/api/*
  - L'opérateur voit les instructions sur l'interface web et confirme
    chaque action physique sur la télécommande de la PAC
  - À chaque confirmation, un snapshot des trames RS485 est pris et analysé

Flux :
  Claude (SSH) → POST /test/api/next_step → Interface web affiche instruction
  Opérateur → effectue action sur PAC → clique "FAIT"
  Interface → POST /test/api/confirm → analyse frames → résultat stocké
  Claude (SSH) → GET /test/api/report → lit les corrélations
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import threading
from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, jsonify, request

from .decoder import DDFrame, CDFrame, Frame, decode, diff

bp = Blueprint("test", __name__)

DB_PATH = os.environ.get("POOLEX_DB_PATH", "/var/lib/poolex/poolex.db")

# Durée de capture des trames APRÈS confirmation de l'opérateur.
# L'opérateur confirme quand il a fini d'appuyer sur les boutons,
# mais la PAC peut prendre quelques secondes à propager sur le bus RS485.
# On capture sur une fenêtre large pour ne rien rater.
CAPTURE_WINDOW_S = 20   # secondes d'observation après "FAIT"
CAPTURE_FRAMES_N = 10   # nombre de trames à collecter par type

# ---------------------------------------------------------------------------
#  Définition du protocole de test
# ---------------------------------------------------------------------------

TEST_STEPS: list[dict] = [
    {
        "id": 0,
        "label": "Relevé initial",
        "instruction": (
            "Notez les valeurs affichées sur la télécommande :\n"
            "température extérieure, température eau, consigne de chauffe.\n"
            "Saisissez-les dans les champs ci-dessous, puis confirmez."
        ),
        "requires_input": True,   # étape de saisie manuelle
        "delta": 0,
    },
    {
        "id": 1,
        "label": "Consigne +1°C",
        "instruction": (
            "Appuyez 1 fois sur le bouton ▲ de la télécommande pour augmenter\n"
            "la consigne de chauffe de 1°C.\n\n"
            "Attendez que la valeur soit affichée, puis appuyez sur FAIT."
        ),
        "requires_input": False,
        "delta": +1,
    },
    {
        "id": 2,
        "label": "Consigne +2°C",
        "instruction": (
            "Appuyez 2 fois sur le bouton ▲ pour augmenter la consigne de 2°C\n"
            "supplémentaires.\n\n"
            "Attendez que la valeur soit stable, puis appuyez sur FAIT."
        ),
        "requires_input": False,
        "delta": +2,
    },
    {
        "id": 3,
        "label": "Consigne −2°C",
        "instruction": (
            "Appuyez 2 fois sur le bouton ▼ pour redescendre la consigne de 2°C.\n\n"
            "Attendez que la valeur soit stable, puis appuyez sur FAIT."
        ),
        "requires_input": False,
        "delta": -2,
    },
    {
        "id": 4,
        "label": "Consigne −1°C",
        "instruction": (
            "Appuyez 1 fois sur le bouton ▼ pour redescendre la consigne de 1°C\n"
            "et revenir à la valeur de départ.\n\n"
            "Vérifiez que la consigne affichée est bien la valeur initiale,\n"
            "puis appuyez sur FAIT."
        ),
        "requires_input": False,
        "delta": -1,
    },
]

# ---------------------------------------------------------------------------
#  État de session (singleton, une seule session simultanée)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_session: dict = {
    "active": False,
    "step_index": -1,          # -1 = pas démarré
    "baseline": {},            # valeurs saisies par l'opérateur à l'étape 0
    "events": [],              # résultats horodatés par étape
    "started_at": None,
    "waiting_confirm": False,  # True = l'opérateur doit confirmer
    "snapshot_before": {},     # trames capturées avant l'action
}


# ---------------------------------------------------------------------------
#  Utilitaires DB
# ---------------------------------------------------------------------------

def _last_frames() -> dict[str, dict]:
    """Retourne le dernier frame par type avec son id DB."""
    result: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        for header in ("DD", "D2", "CC", "CD"):
            row = conn.execute(
                "SELECT id, raw FROM frames WHERE header=? ORDER BY id DESC LIMIT 1",
                (header,),
            ).fetchone()
            if row:
                frame = decode(row[1])
                if frame:
                    result[header] = {"db_id": row[0], "frame": frame}
        conn.close()
    except Exception:
        pass
    return result


def _frames_after(min_ids: dict[str, int],
                  wait_s: float = CAPTURE_WINDOW_S,
                  n: int = CAPTURE_FRAMES_N) -> dict[str, dict]:
    """
    Collecte les nouvelles trames pendant wait_s secondes après confirmation.

    On attend toute la fenêtre (pas de break anticipé) pour capturer
    l'évolution complète : la PAC peut mettre plusieurs secondes à propager
    le changement de consigne sur le bus RS485 après les pressions de boutons.

    Retourne pour chaque header :
      - first : première trame arrivée (réaction immédiate)
      - last  : dernière trame arrivée (état stabilisé)
      - count : nombre de trames reçues pendant la fenêtre
    """
    deadline = time.monotonic() + wait_s
    collected: dict[str, list[dict]] = {h: [] for h in ("DD", "D2", "CC", "CD")}

    while time.monotonic() < deadline:
        try:
            conn = sqlite3.connect(DB_PATH)
            for header in ("DD", "D2", "CC", "CD"):
                min_id = min_ids.get(header, 0)
                if collected[header]:
                    min_id = max(min_id, collected[header][-1]["db_id"])
                rows = conn.execute(
                    "SELECT id, raw, timestamp FROM frames "
                    "WHERE header=? AND id>? ORDER BY id LIMIT ?",
                    (header, min_id, n),
                ).fetchall()
                for row_id, raw, ts in rows:
                    frame = decode(raw)
                    if frame:
                        collected[header].append(
                            {"db_id": row_id, "frame": frame, "captured_at": ts}
                        )
            conn.close()
        except Exception:
            pass
        time.sleep(1.0)

    result: dict[str, dict] = {}
    for header, frames in collected.items():
        if frames:
            result[header] = {
                "db_id":  frames[-1]["db_id"],   # dernier = état stabilisé
                "frame":  frames[-1]["frame"],
                "first":  frames[0],
                "last":   frames[-1],
                "count":  len(frames),
            }
    return result


def _analyze(before: dict[str, dict], after: dict[str, dict]) -> dict:
    """Calcule les bytes significatifs qui ont changé entre avant et après."""
    analysis: dict[str, list] = {}
    for header in ("DD", "CD", "D2", "CC"):
        b = before.get(header)
        a = after.get(header)
        if not b or not a:
            continue
        changes = diff(b["frame"], a["frame"])
        # Exclure byte[0] (header) et byte[79] (marqueur/compteur)
        significant = {
            k: {"before": v[0], "after": v[1],
                "hex_before": f"0x{v[0]:02X}", "hex_after": f"0x{v[1]:02X}"}
            for k, v in changes.items()
            if k not in (0, 79)
        }
        if significant:
            analysis[header] = significant
    return analysis


def _current_readings() -> dict:
    """Décode les dernières valeurs connues depuis la DB."""
    frames = _last_frames()
    dd = frames.get("DD", {}).get("frame")
    cd = frames.get("CD", {}).get("frame")
    return {
        "water_temp": dd.water_temp if isinstance(dd, DDFrame) else None,
        "air_temp":   dd.air_temp   if isinstance(dd, DDFrame) else None,
        "setpoint":   cd.setpoint   if isinstance(cd, CDFrame) else None,
    }


# ---------------------------------------------------------------------------
#  Routes API (utilisées par Claude via SSH + par l'interface JS)
# ---------------------------------------------------------------------------

@bp.get("/test/api/state")
def api_state():
    """État complet de la session (interrogé par l'interface JS toutes les 2s)."""
    with _lock:
        step = TEST_STEPS[_session["step_index"]] if _session["step_index"] >= 0 else None
        return jsonify({
            "active":          _session["active"],
            "step_index":      _session["step_index"],
            "total_steps":     len(TEST_STEPS),
            "step":            step,
            "waiting_confirm": _session["waiting_confirm"],
            "baseline":        _session["baseline"],
            "events_count":    len(_session["events"]),
            "readings":        _current_readings(),
        })


@bp.post("/test/api/start")
def api_start():
    """Démarre une nouvelle session (appelé par Claude via SSH)."""
    with _lock:
        now = datetime.now(timezone.utc).isoformat()
        _session.update({
            "active": True,
            "step_index": 0,
            "baseline": {},
            "events": [],
            "started_at": now,
            "step_presented_at": now,
            "waiting_confirm": True,
            "snapshot_before": _last_frames(),
        })
    return jsonify({"status": "started", "step": TEST_STEPS[0]})


@bp.post("/test/api/confirm")
def api_confirm():
    """
    L'opérateur confirme qu'il a effectué l'action.
    Body JSON pour l'étape 0 : {"temp_ext": 22, "temp_eau": 28, "consigne": 27}
    """
    with _lock:
        if not _session["active"] or not _session["waiting_confirm"]:
            return jsonify({"error": "Aucune action en attente"}), 400

        ts = datetime.now(timezone.utc).isoformat()
        idx = _session["step_index"]
        step = TEST_STEPS[idx]

        step_presented_at = _session.get("step_presented_at", ts)
        event: dict = {
            "step_id":          step["id"],
            "label":            step["label"],
            "delta":            step["delta"],
            "step_presented_at": step_presented_at,   # quand Claude a avancé l'étape
            "operator_confirmed_at": ts,              # quand l'opérateur a appuyé sur FAIT
            "operator_delay_s": round(
                (datetime.fromisoformat(ts) -
                 datetime.fromisoformat(step_presented_at)).total_seconds(), 1
            ),
        }

        if step["requires_input"]:
            body = request.get_json(silent=True) or {}
            baseline = {
                "temp_ext_display": body.get("temp_ext"),
                "temp_eau_display": body.get("temp_eau"),
                "consigne_display": body.get("consigne"),
            }
            _session["baseline"] = baseline
            event["baseline"] = baseline
        else:
            before = _session["snapshot_before"]
            before_ids = {h: v["db_id"] for h, v in before.items()}

        _session["waiting_confirm"] = False

        # Analyse asynchrone pour les étapes avec action physique
        if not step["requires_input"]:
            capture_start = datetime.now(timezone.utc).isoformat()

            def _do_analysis():
                after = _frames_after(before_ids)
                analysis = _analyze(before, after)
                capture_end = datetime.now(timezone.utc).isoformat()
                with _lock:
                    event["analysis"]          = analysis
                    event["capture_start_at"]  = capture_start
                    event["capture_end_at"]    = capture_end
                    event["capture_window_s"]  = CAPTURE_WINDOW_S
                    event["frames_collected"]  = {
                        h: d["count"] for h, d in after.items()
                    }
                    _session["events"].append(event)

            threading.Thread(target=_do_analysis, daemon=True).start()
        else:
            _session["events"].append(event)

        return jsonify({"status": "confirmed", "step_label": step["label"]})


@bp.post("/test/api/next_step")
def api_next_step():
    """Avance à l'étape suivante (appelé par Claude via SSH)."""
    with _lock:
        if not _session["active"]:
            return jsonify({"error": "Session non démarrée"}), 400
        if _session["waiting_confirm"]:
            return jsonify({"error": "En attente de confirmation opérateur"}), 400

        next_idx = _session["step_index"] + 1
        if next_idx >= len(TEST_STEPS):
            _session["active"] = False
            return jsonify({"status": "completed"})

        _session["step_index"] = next_idx
        _session["snapshot_before"] = _last_frames()
        _session["waiting_confirm"] = True
        _session["step_presented_at"] = datetime.now(timezone.utc).isoformat()

        return jsonify({"status": "next", "step": TEST_STEPS[next_idx]})


@bp.get("/test/api/report")
def api_report():
    """Rapport complet de la session (appelé par Claude pour analyse)."""
    with _lock:
        return jsonify({
            "started_at": _session["started_at"],
            "baseline":   _session["baseline"],
            "events":     _session["events"],
        })


# ---------------------------------------------------------------------------
#  Interface HTML (servie à l'opérateur)
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Poolex — Test Protocol</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f172a; color: #f1f5f9; min-height: 100vh; padding: 16px; }
  .card { background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
  .header { text-align: center; margin-bottom: 20px; }
  .header h1 { font-size: 1.2rem; color: #94a3b8; letter-spacing: .1em; text-transform: uppercase; }
  .step-badge { display: inline-block; background: #334155; border-radius: 20px; padding: 4px 14px; font-size: .85rem; color: #94a3b8; margin-bottom: 12px; }
  .step-label { font-size: 1.4rem; font-weight: 700; color: #f8fafc; margin-bottom: 8px; }
  .instruction { font-size: 1.05rem; color: #cbd5e1; line-height: 1.6; white-space: pre-line; }

  .readings { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .reading-box { background: #0f172a; border-radius: 8px; padding: 12px; text-align: center; }
  .reading-label { font-size: .7rem; color: #64748b; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 4px; }
  .reading-value { font-size: 1.6rem; font-weight: 700; }
  .reading-value.water { color: #38bdf8; }
  .reading-value.air   { color: #a3e635; }
  .reading-value.setpoint { color: #fb923c; }

  .btn-confirm { width: 100%; padding: 20px; border: none; border-radius: 12px; font-size: 1.3rem; font-weight: 700; cursor: pointer; transition: all .15s; }
  .btn-confirm.ready { background: #16a34a; color: white; }
  .btn-confirm.ready:active { transform: scale(.97); background: #15803d; }
  .btn-confirm.waiting { background: #334155; color: #64748b; cursor: not-allowed; }
  .btn-confirm.done { background: #1d4ed8; color: white; cursor: not-allowed; }

  .input-group { margin-bottom: 12px; }
  .input-group label { display: block; font-size: .8rem; color: #94a3b8; margin-bottom: 4px; text-transform: uppercase; letter-spacing: .05em; }
  .input-group input { width: 100%; background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 12px; color: #f1f5f9; font-size: 1.1rem; }

  .status-bar { text-align: center; font-size: .8rem; color: #475569; margin-top: 8px; }
  .progress { display: flex; gap: 6px; justify-content: center; margin-bottom: 16px; }
  .progress-dot { width: 10px; height: 10px; border-radius: 50%; background: #334155; }
  .progress-dot.done { background: #16a34a; }
  .progress-dot.current { background: #f59e0b; }

  .waiting-indicator { text-align: center; color: #f59e0b; font-size: .9rem; padding: 10px; }
  .confirmed-msg { text-align: center; color: #4ade80; font-size: 1rem; padding: 12px; display: none; }
  .completed { text-align: center; padding: 40px 20px; }
  .completed h2 { color: #4ade80; font-size: 1.5rem; margin-bottom: 8px; }
  .completed p { color: #94a3b8; }
</style>
</head>
<body>

<div class="header">
  <h1>Poolex — Test Protocol</h1>
</div>

<div id="app">
  <div class="card" style="text-align:center; color:#64748b;">Chargement...</div>
</div>

<script>
let lastStepIndex = -1;
let confirming = false;

async function poll() {
  try {
    const r = await fetch('/test/api/state');
    const s = await r.json();
    render(s);
  } catch(e) {}
}

function render(s) {
  const app = document.getElementById('app');

  if (!s.active && s.step_index < 0) {
    app.innerHTML = `<div class="card" style="text-align:center; color:#64748b; padding:40px">
      En attente du démarrage de la session...</div>`;
    return;
  }

  if (!s.active && s.step_index >= 0) {
    app.innerHTML = `<div class="completed">
      <h2>✓ Session terminée</h2>
      <p>Merci. Les données ont été enregistrées.</p>
    </div>`;
    return;
  }

  const step = s.step;
  const readings = s.readings;

  // Progress dots
  let dots = '';
  for (let i = 0; i < s.total_steps; i++) {
    let cls = i < s.step_index ? 'done' : i === s.step_index ? 'current' : '';
    dots += `<div class="progress-dot ${cls}"></div>`;
  }

  // Readings
  const wt = readings.water_temp != null ? readings.water_temp.toFixed(1) + '°C' : '—';
  const at = readings.air_temp   != null ? readings.air_temp + '°C' : '—';
  const sp = readings.setpoint   != null ? readings.setpoint + '°C' : '—';

  // Baseline inputs or confirm button
  let actionArea = '';
  if (step.requires_input) {
    actionArea = `
      <div class="input-group">
        <label>Température extérieure affichée (°C)</label>
        <input type="number" id="inp_ext" step="0.5" placeholder="ex: 22">
      </div>
      <div class="input-group">
        <label>Température eau affichée (°C)</label>
        <input type="number" id="inp_eau" step="0.5" placeholder="ex: 28">
      </div>
      <div class="input-group">
        <label>Consigne de chauffe affichée (°C)</label>
        <input type="number" id="inp_consigne" step="1" placeholder="ex: 27">
      </div>
      <button class="btn-confirm ${s.waiting_confirm ? 'ready' : 'done'}"
              onclick="confirmBaseline()" id="btn">
        ✓ CONFIRMER LES VALEURS
      </button>`;
  } else {
    const btnClass = s.waiting_confirm ? 'ready' : 'done';
    const btnText  = s.waiting_confirm ? '✓ FAIT — Action effectuée' : '✓ Confirmé';
    actionArea = `
      <button class="btn-confirm ${btnClass}" onclick="confirmAction()" id="btn">
        ${btnText}
      </button>`;
  }

  const waitingMsg = !s.waiting_confirm
    ? `<div class="waiting-indicator">⏳ Analyse en cours...</div>` : '';

  app.innerHTML = `
    <div class="progress">${dots}</div>

    <div class="card">
      <div class="step-badge">Étape ${s.step_index + 1} / ${s.total_steps}</div>
      <div class="step-label">${step.label}</div>
      <div class="instruction">${step.instruction}</div>
    </div>

    <div class="card">
      <div class="reading-label" style="margin-bottom:10px">Valeurs RS485 temps réel</div>
      <div class="readings">
        <div class="reading-box">
          <div class="reading-label">Eau</div>
          <div class="reading-value water">${wt}</div>
        </div>
        <div class="reading-box">
          <div class="reading-label">Air ext.</div>
          <div class="reading-value air">${at}</div>
        </div>
        <div class="reading-box">
          <div class="reading-label">Consigne RS485</div>
          <div class="reading-value setpoint">${sp}</div>
        </div>
      </div>
    </div>

    <div class="card">
      ${actionArea}
      ${waitingMsg}
    </div>

    <div class="status-bar" id="status">Dernière mise à jour : ${new Date().toLocaleTimeString()}</div>
  `;
}

async function confirmBaseline() {
  if (confirming) return;
  const ext      = parseFloat(document.getElementById('inp_ext')?.value);
  const eau      = parseFloat(document.getElementById('inp_eau')?.value);
  const consigne = parseFloat(document.getElementById('inp_consigne')?.value);
  if (isNaN(ext) || isNaN(eau) || isNaN(consigne)) {
    alert('Veuillez saisir les 3 valeurs.');
    return;
  }
  confirming = true;
  await fetch('/test/api/confirm', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({temp_ext: ext, temp_eau: eau, consigne: consigne}),
  });
  confirming = false;
}

async function confirmAction() {
  if (confirming) return;
  const btn = document.getElementById('btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Enregistrement...'; }
  confirming = true;
  await fetch('/test/api/confirm', { method: 'POST' });
  confirming = false;
}

setInterval(poll, 2000);
poll();
</script>
</body>
</html>
"""


@bp.get("/test")
def test_ui():
    """Interface HTML pour l'opérateur."""
    from flask import Response
    return Response(_HTML, mimetype="text/html")
