"""
APP WEB DEPLOYABILE — MONITOR MULTI-PARTITA IN TEMPO REALE
==============================================================
Dashboard che mostra TUTTE le partite live delle leghe configurate,
con statistiche di dominio calcolate automaticamente, aggiornata
in automatico ogni 20 secondi. Pensata per girare 24/7 su un
servizio di hosting (Render, Railway, PythonAnywhere).

QUESTA APP NON PIAZZA SCOMMESSE. Solo lettura + visualizzazione.

DATI: usa API-Football (v3.football.api-sports.io) per fixture live
e statistiche. La API key va messa come variabile d'ambiente
(mai scritta nel codice), per sicurezza durante il deploy.

REQUISITI (vedi requirements.txt):
    flask, requests, gunicorn

AVVIO IN LOCALE (per test prima del deploy):
    export API_FOOTBALL_KEY="la_tua_key"
    python app.py
    poi apri http://localhost:5000

DEPLOY: vedi il file DEPLOY.md incluso per le istruzioni passo-passo.
"""

import os
import threading
import time
import requests
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# ============== CONFIG ==============

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"

# ID delle leghe API-Football da monitorare (Serie A, Premier League,
# La Liga, Bundesliga, Ligue 1, Champions League)
LEAGUE_IDS = {
    135: "Serie A",
    39: "Premier League",
    140: "La Liga",
    78: "Bundesliga",
    61: "Ligue 1",
    2: "Champions League",
}

DOMINANCE_MIN_POSSESSION = 60
DOMINANCE_MIN_SHOTS_DIFF = 2
DOMINANCE_MIN_SHOTS_ON_TARGET = 1
ENTRY_DEADLINE_MINUTE = 35

POLL_SECONDS = 20

# =====================================

state_lock = threading.Lock()
state = {"matches": [], "last_update": None, "error": None}


def api_football_get(endpoint, params):
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    resp = requests.get(f"{API_FOOTBALL_BASE_URL}/{endpoint}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()["response"]


def get_live_fixtures():
    """Recupera tutte le partite attualmente live nelle leghe configurate."""
    data = api_football_get("fixtures", {"live": "all"})
    league_ids_str = set(LEAGUE_IDS.keys())
    return [f for f in data if f["league"]["id"] in league_ids_str]


def extract_stat(stats_list, stat_type):
    for s in stats_list:
        if s["type"] == stat_type:
            val = s["value"]
            if val is None:
                return 0
            if isinstance(val, (int, float)):
                return int(val)
            return int(str(val).replace("%", ""))
    return 0


def analyze_fixture(fixture):
    fixture_id = fixture["fixture"]["id"]
    minute = fixture["fixture"]["status"]["elapsed"] or 0
    home_name = fixture["teams"]["home"]["name"]
    away_name = fixture["teams"]["away"]["name"]
    home_score = fixture["goals"]["home"] or 0
    away_score = fixture["goals"]["away"] or 0
    league_name = LEAGUE_IDS.get(fixture["league"]["id"], fixture["league"]["name"])

    result = {
        "fixture_id": fixture_id,
        "league": league_name,
        "home": home_name,
        "away": away_name,
        "score": f"{home_score}-{away_score}",
        "minute": minute,
        "home_dominance": False,
        "away_dominance": False,
        "stats_available": False,
        "window_open": minute <= ENTRY_DEADLINE_MINUTE,
    }

    try:
        stats_data = api_football_get("fixtures/statistics", {"fixture": fixture_id})
        if len(stats_data) >= 2:
            home_stats = stats_data[0]["statistics"]
            away_stats = stats_data[1]["statistics"]

            home_poss = extract_stat(home_stats, "Ball Possession")
            away_poss = extract_stat(away_stats, "Ball Possession")
            home_shots = extract_stat(home_stats, "Total Shots")
            away_shots = extract_stat(away_stats, "Total Shots")
            home_sot = extract_stat(home_stats, "Shots on Goal")
            away_sot = extract_stat(away_stats, "Shots on Goal")

            result["stats_available"] = True
            result["home_possession"] = home_poss
            result["away_possession"] = away_poss
            result["home_shots"] = home_shots
            result["away_shots"] = away_shots
            result["home_sot"] = home_sot
            result["away_sot"] = away_sot

            result["home_dominance"] = (
                home_poss >= DOMINANCE_MIN_POSSESSION
                and (home_shots - away_shots) >= DOMINANCE_MIN_SHOTS_DIFF
                and home_sot >= DOMINANCE_MIN_SHOTS_ON_TARGET
            )
            result["away_dominance"] = (
                away_poss >= DOMINANCE_MIN_POSSESSION
                and (away_shots - home_shots) >= DOMINANCE_MIN_SHOTS_DIFF
                and away_sot >= DOMINANCE_MIN_SHOTS_ON_TARGET
            )
    except Exception:
        pass

    return result


def background_poller():
    while True:
        try:
            fixtures = get_live_fixtures()
            analyzed = [analyze_fixture(f) for f in fixtures]
            # Ordina mettendo prima le partite con dominio rilevato
            analyzed.sort(key=lambda m: (not (m["home_dominance"] or m["away_dominance"]), m["minute"]))
            with state_lock:
                state["matches"] = analyzed
                state["last_update"] = time.strftime("%H:%M:%S")
                state["error"] = None
        except Exception as e:
            with state_lock:
                state["error"] = str(e)
        time.sleep(POLL_SECONDS)


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/matches")
def api_matches():
    with state_lock:
        return jsonify(state)


_poller_started = False
_poller_lock = threading.Lock()


def ensure_poller_started():
    """Avvia il thread di aggiornamento in sottofondo alla prima richiesta
    ricevuta dal server. Farlo qui (invece che al caricamento del modulo)
    evita problemi di compatibilità con come alcuni servizi di hosting
    (come Render) avviano il processo internamente."""
    global _poller_started
    with _poller_lock:
        if not _poller_started:
            t = threading.Thread(target=background_poller, daemon=True)
            t.start()
            _poller_started = True


@app.before_request
def _start_poller_before_request():
    ensure_poller_started()


@app.route("/api/debug")
def api_debug():
    """Pagina di debug temporanea: mostra la risposta grezza di API-Football
    e se la chiave è stata letta correttamente, per capire dove sta il problema."""
    key_status = "MANCANTE (vuota)" if not API_FOOTBALL_KEY else f"presente, lunga {len(API_FOOTBALL_KEY)} caratteri"
    result = {"api_key_status": key_status}
    try:
        headers = {"x-apisports-key": API_FOOTBALL_KEY}
        resp = requests.get(f"{API_FOOTBALL_BASE_URL}/status", headers=headers, timeout=15)
        result["http_status_code"] = resp.status_code
        result["raw_response"] = resp.json()
    except Exception as e:
        result["exception"] = str(e)

    # Controlla se il thread in background è vivo
    result["poller_thread_alive"] = _poller_started
    result["current_state_snapshot"] = state

    # Prova a chiamare manualmente get_live_fixtures per isolare eventuali errori
    try:
        fixtures = get_live_fixtures()
        result["manual_fixtures_call"] = {"success": True, "count": len(fixtures)}
    except Exception as e:
        result["manual_fixtures_call"] = {"success": False, "exception": str(e)}

    return jsonify(result)


# Il thread di aggiornamento in background si avvia automaticamente
# alla prima richiesta ricevuta (vedi ensure_poller_started sopra),
# non al caricamento del modulo — più affidabile su servizi come Render.


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Match Monitor Live</title>
<style>
    :root { --pitch:#0b1f16; --line:#1e3a2a; --panel:#0f2a1d; --amber:#e8a33d; --text:#eef2ee; --dim:#7fa08c; --green:#5fd88f; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--pitch); color:var(--text); font-family:'Courier New',monospace; padding:20px 14px 60px; max-width:480px; margin:0 auto; }
    h1 { font-size:15px; color:var(--dim); font-weight:normal; margin:0 0 4px; }
    .status { font-size:12px; color:var(--amber); margin-bottom:18px; }
    .match { background:var(--panel); border:1px solid var(--line); border-radius:4px; padding:14px; margin-bottom:10px; }
    .row { display:flex; justify-content:space-between; align-items:baseline; }
    .teams { font-size:15px; font-weight:bold; }
    .score { font-size:18px; color:var(--amber); font-weight:bold; }
    .meta { font-size:11px; color:var(--dim); margin:4px 0 8px; }
    .stats { font-size:12px; color:var(--dim); margin-bottom:6px; }
    .badge { display:inline-block; padding:3px 8px; border-radius:3px; font-size:11px; font-weight:bold; }
    .badge.dom { background:rgba(95,216,143,0.15); color:var(--green); }
    .badge.none { background:rgba(127,160,140,0.1); color:var(--dim); }
    .empty { text-align:center; color:var(--dim); padding:40px 20px; font-size:13px; }
</style>
</head>
<body>
<h1>MATCH MONITOR LIVE</h1>
<div class="status" id="status">Caricamento...</div>
<div id="matches"></div>

<script>
async function refresh() {
    const res = await fetch('/api/matches');
    const data = await res.json();
    document.getElementById('status').innerText = data.error
        ? 'Errore: ' + data.error
        : 'Ultimo aggiornamento: ' + (data.last_update || '—') + ' · aggiorna ogni 20s';

    const container = document.getElementById('matches');
    if (!data.matches || data.matches.length === 0) {
        container.innerHTML = '<div class="empty">Nessuna partita live al momento nelle leghe monitorate.</div>';
        return;
    }

    container.innerHTML = data.matches.map(m => {
        let statsHtml = '';
        if (m.stats_available) {
            statsHtml = `<div class="stats">Possesso: ${m.home_possession}% - ${m.away_possession}% | Tiri: ${m.home_shots}(${m.home_sot}) - ${m.away_shots}(${m.away_sot})</div>`;
        }
        let badge = '<span class="badge none">Nessun dominio netto</span>';
        if (m.home_dominance) badge = `<span class="badge dom">${m.home} in dominio</span>`;
        if (m.away_dominance) badge = `<span class="badge dom">${m.away} in dominio</span>`;

        return `<div class="match">
            <div class="row"><span class="teams">${m.home} - ${m.away}</span><span class="score">${m.score}</span></div>
            <div class="meta">${m.league} · ${m.minute}' ${m.window_open ? '(entro finestra 35\\')' : '(oltre finestra)'}</div>
            ${statsHtml}
            ${badge}
        </div>`;
    }).join('');
}

setInterval(refresh, 20000);
refresh();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
