import json
import os
import time
import requests
import pandas as pd
import streamlit as st
from google import genai

st.set_page_config(page_title="Dynasty Command Center", layout="wide")

SLEEPER_BASE = "https://api.sleeper.app/v1"
PLAYER_CACHE = "nfl_players_cache.json"


def get_secret(key):
    """Safely read from Streamlit Secrets without crashing if no secrets file exists."""
    try:
        return st.secrets.get(key, "")
    except Exception:
        return ""


def fetch_json(url):
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=86400)
def get_nfl_players():
    """Cache Sleeper's ~15MB NFL player database locally for 7 days."""
    if os.path.exists(PLAYER_CACHE):
        age_days = (time.time() - os.path.getmtime(PLAYER_CACHE)) / 86400
        if age_days < 7:
            with open(PLAYER_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
    players = fetch_json(f"{SLEEPER_BASE}/players/nfl")
    with open(PLAYER_CACHE, "w", encoding="utf-8") as f:
        json.dump(players, f)
    return players


def normalize(series):
    if series.max() == series.min():
        return pd.Series([50.0] * len(series), index=series.index)
    return ((series - series.min()) / (series.max() - series.min())) * 100


@st.cache_data(ttl=600)
def load_league_data(league_id, selected_week):
    league = fetch_json(f"{SLEEPER_BASE}/league/{league_id}")
    users = fetch_json(f"{SLEEPER_BASE}/league/{league_id}/users")
    rosters = fetch_json(f"{SLEEPER_BASE}/league/{league_id}/rosters")
    traded_picks = fetch_json(f"{SLEEPER_BASE}/league/{league_id}/traded_picks")
    players_db = get_nfl_players()

    user_map = {}
    for u in users:
        team_name = u.get("metadata", {}).get("team_name") or u.get("display_name")
        user_map[u["user_id"]] = {
            "team_name": team_name,
            "owner": u.get("display_name", "Unknown"),
        }

    # Pull matchups for weeks 1..selected_week for All-Play & Momentum
    weekly_scores = {r["roster_id"]: [] for r in rosters}
    all_play_w = {r["roster_id"]: 0 for r in rosters}
    all_play_l = {r["roster_id"]: 0 for r in rosters}
    selected_week_raw = []

    for wk in range(1, selected_week + 1):
        matchups = fetch_json(f"{SLEEPER_BASE}/league/{league_id}/matchups/{wk}")
        if wk == selected_week:
            selected_week_raw = matchups
        valid = [m for m in matchups if m.get("points") is not None]
        for m in valid:
            rid = m["roster_id"]
            pts = m["points"]
            weekly_scores[rid].append(pts)
            wins_wk = sum(1 for o in valid if o["roster_id"] != rid and pts > o["points"])
            losses_wk = sum(1 for o in valid if o["roster_id"] != rid and pts < o["points"])
            all_play_w[rid] += wins_wk
            all_play_l[rid] += losses_wk

    # Track future 1st, 2nd, 3rd round picks for next 3 seasons
    current_season = int(league.get("season", 2026))
    future_years = [str(current_season + i) for i in (1, 2, 3)]
    pick_ledger = {}
    for r in rosters:
        rid = r["roster_id"]
        for yr in future_years:
            for rd in (1, 2, 3):
                pick_ledger[(yr, rd, rid)] = rid  # default owner = original team

    for tp in traded_picks:
        key = (str(tp["season"]), int(tp["round"]), tp["roster_id"])
        if key in pick_ledger:
            pick_ledger[key] = tp["owner_id"]

    # Assemble per-team metrics
    rows = []
    roster_name_map = {}
    for r in rosters:
        rid = r["roster_id"]
        owner_info = user_map.get(r.get("owner_id"), {"team_name": f"Team {rid}", "owner": "Open"})
        tname = owner_info["team_name"]
        roster_name_map[rid] = tname

        s = r.get("settings", {})
        wins = s.get("wins", 0)
        losses = s.get("losses", 0)
        ties = s.get("ties", 0)
        games = max(wins + losses + ties, 1)
        win_pct = (wins + 0.5 * ties) / games

        pf = s.get("fpts", 0) + s.get("fpts_decimal", 0) / 100
        pa = s.get("fpts_against", 0) + s.get("fpts_against_decimal", 0) / 100
        max_pf = s.get("ppts", 0) + s.get("ppts_decimal", 0) / 100
        eff = (pf / max_pf * 100) if max_pf > 0 else 0.0

        ap_w, ap_l = all_play_w[rid], all_play_l[rid]
        ap_pct = ap_w / max(ap_w + ap_l, 1)

        recent = weekly_scores[rid][-3:]
        recent_avg = sum(recent) / len(recent) if recent else 0.0

        # Dynasty metrics
        pids = r.get("players") or []
        ages = [
            players_db[pid]["age"]
            for pid in pids
            if pid in players_db and isinstance(players_db[pid].get("age"), (int, float))
        ]
        avg_age = sum(ages) / len(ages) if ages else 0.0
        youth_pct = (sum(1 for a in ages if a <= 25) / len(ages) * 100) if ages else 0.0

        firsts = sum(1 for (yr, rd, _), owner in pick_ledger.items() if owner == rid and rd == 1)
        seconds = sum(1 for (yr, rd, _), owner in pick_ledger.items() if owner == rid and rd == 2)
        thirds = sum(1 for (yr, rd, _), owner in pick_ledger.items() if owner == rid and rd == 3)

        rows.append({
            "roster_id": rid,
            "Team": tname,
            "Owner": owner_info["owner"],
            "Wins": wins,
            "Losses": losses,
            "Record": f"{wins}-{losses}" + (f"-{ties}" if ties else ""),
            "Win %": round(win_pct * 100, 1),
            "All-Play": f"{ap_w}-{ap_l}",
            "All-Play %": round(ap_pct * 100, 1),
            "PF": round(pf, 2),
            "PA": round(pa, 2),
            "Max PF": round(max_pf, 2),
            "Efficiency %": round(eff, 1),
            "Last 3 Avg": round(recent_avg, 2),
            "Luck Index": round((win_pct - ap_pct) * 100, 1),
            "Avg Age": round(avg_age, 2),
            "Youth % (<=25)": round(youth_pct, 1),
            "1sts (Next 3Y)": firsts,
            "2nds (Next 3Y)": seconds,
            "3rds (Next 3Y)": thirds,
        })

    df = pd.DataFrame(rows)

    # Calculate Power Score (0-100)
    df["Power Score"] = (
        normalize(df["All-Play %"]) * 0.30
        + normalize(df["Win %"]) * 0.25
        + normalize(df["PF"]) * 0.25
        + normalize(df["Last 3 Avg"]) * 0.15
        + normalize(df["Efficiency %"]) * 0.05
    ).round(1)

    df = df.sort_values("Power Score", ascending=False).reset_index(drop=True)
    df.insert(0, "Rank", range(1, len(df) + 1))

    # Playoff Picture
    playoff_spots = league.get("settings", {}).get("playoff_teams", 6)
    playoff_df = df.sort_values(["Wins", "PF"], ascending=[False, False]).reset_index(drop=True)
    playoff_df.insert(0, "Seed", range(1, len(playoff_df) + 1))
    cutoff_wins = playoff_df.loc[playoff_spots - 1, "Wins"]

    def playoff_status(row):
        if row["Seed"] <= 2 and playoff_spots >= 6:
            return "BYE"
        if row["Seed"] <= playoff_spots:
            return "IN"
        if cutoff_wins - row["Wins"] <= 2:
            return "IN THE HUNT"
        return "ON LIFE SUPPORT"

    playoff_df["Status"] = playoff_df.apply(playoff_status, axis=1)
    playoff_df["GB"] = (cutoff_wins - playoff_df["Wins"]).clip(lower=0)

    # Dynasty Archetypes
    med_age = df["Avg Age"].median()
    med_power = df["Power Score"].median()

    def archetype(row):
        young = row["Avg Age"] <= med_age
        strong = row["Power Score"] >= med_power
        if strong and young:
            return "Dynasty Juggernaut"
        if strong and not young:
            return "Win-Now Contender"
        if not strong and (young or row["1sts (Next 3Y)"] >= 4):
            return "Full Rebuild"
        return "Fringe / Retooling"

    df["Archetype"] = df.apply(archetype, axis=1)

    # Build Selected Week Matchup Summary
    matchup_groups = {}
    for m in selected_week_raw:
        mid = m.get("matchup_id")
        if mid is None:
            continue
        matchup_groups.setdefault(mid, []).append(m)

    matchup_lines = []
    for mid, pair in matchup_groups.items():
        if len(pair) == 2:
            a, b = sorted(pair, key=lambda x: x.get("points", 0), reverse=True)
            ta = roster_name_map.get(a["roster_id"], "Team A")
            tb = roster_name_map.get(b["roster_id"], "Team B")
            margin = round(a["points"] - b["points"], 2)
            matchup_lines.append(f"{ta} ({a['points']:.2f}) def. {tb} ({b['points']:.2f}) [Margin: {margin}]")

    return league, df, playoff_df, matchup_lines


def generate_recap(api_key, league_name, week, matchups, power_df, playoff_df):
    client = genai.Client(api_key=api_key)
    prompt = f"""
    You are a witty, sharp ESPN-style fantasy football analyst writing the official Week {week}
    recap for the '{league_name}' dynasty league. Be punchy, entertaining, and lightly roast
    underperformers or lucky teams using the real numbers below. Never invent stats.

    WEEK {week} MATCHUPS:
    {chr(10).join(matchups)}

    POWER RANKINGS (with All-Play %, Luck Index, Last 3 Avg, Dynasty Archetype):
    {power_df[['Rank', 'Team', 'Record', 'Power Score', 'All-Play %', 'PF', 'Last 3 Avg', 'Luck Index', 'Archetype']].to_string(index=False)}

    PLAYOFF PICTURE:
    {playoff_df[['Seed', 'Team', 'Record', 'PF', 'Status', 'GB']].to_string(index=False)}

    Write the recap with these exact sections:
    1. **Headline & Opening Monologue** (ESPN SportsCenter desk vibe)
    2. **Week {week} Superlatives** (Matchup of the Week, Biggest Blowout, Luckiest Fraud, Unluckiest Loss)
    3. **Power Rankings Breakdown** (Quick witty blurbs for the top contenders, biggest movers, and cellar dwellers)
    4. **Playoff Picture Watch** (Who's cruising to a bye, who's sweating on the bubble)
    5. **Dynasty Outlook Corner** (Highlight 1-2 Dynasty Juggernauts vs. teams needing to pick a lane)
    """
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
    )
    return response.text


# --- STREAMLIT UI ---
st.title("Dynasty League Command Center")

with st.sidebar:
    st.header("League Settings")
    league_id = st.text_input(
        "Sleeper League ID",
        value=get_secret("SLEEPER_LEAGUE_ID"),
        placeholder="e.g. 123456789012345678",
    )
    api_key = st.text_input(
        "Gemini API Key",
        value=get_secret("GEMINI_API_KEY"),
        type="password",
        placeholder="Paste your AI Studio key",
    )
    week = st.number_input("Week to Analyze", min_value=1, max_value=18, value=11, step=1)
    load_btn = st.button("Load League Data", type="primary", use_container_width=True)

if load_btn and league_id:
    st.session_state["loaded"] = True

if st.session_state.get("loaded") and league_id:
    with st.spinner("Pulling live Sleeper data and crunching metrics..."):
        league, df, playoff_df, matchups = load_league_data(league_id, int(week))

    st.subheader(f"{league.get('name', 'Dynasty League')} — Week {week}")

    tab1, tab2, tab3, tab4 = st.tabs([
        "ESPN Weekly Recap",
        "Power Rankings & Metrics",
        "Playoff Picture",
        "Dynasty Outlook",
    ])

    with tab1:
        st.markdown("### AI-Generated Weekly Recap")
        if st.button("Generate ESPN-Style Recap", type="primary"):
            if not api_key:
                st.error("Paste your Gemini API key in the left sidebar first.")
            else:
                with st.spinner("Writing your Week " + str(week) + " broadcast..."):
                    recap_text = generate_recap(
                        api_key, league.get("name", "Dynasty League"), int(week), matchups, df, playoff_df
                    )
                    st.session_state["recap"] = recap_text

        if "recap" in st.session_state:
            st.markdown(st.session_state["recap"])
            st.divider()
            st.caption("Copy-paste version for Sleeper chat:")
            st.code(st.session_state["recap"], language="markdown")

    with tab2:
        st.markdown("### Season Power Rankings (Full Metrics)")
        cols = [
            "Rank", "Team", "Record", "Power Score", "All-Play",
            "All-Play %", "PF", "PA", "Max PF", "Efficiency %",
            "Last 3 Avg", "Luck Index"
        ]
        st.dataframe(df[cols], use_container_width=True, hide_index=True)
        with st.expander("How Power Score is calculated"):
            st.write(
                "Each metric is normalized 0–100 across the league: "
                "**All-Play % (30%)** + **Win % (25%)** + **Points For (25%)** + "
                "**Last 3-Week Avg (15%)** + **Lineup Efficiency (5%)**."
            )

    with tab3:
        st.markdown("### Current Playoff Picture")
        p_cols = ["Seed", "Team", "Record", "PF", "Status", "GB"]
        st.dataframe(playoff_df[p_cols], use_container_width=True, hide_index=True)

    with tab4:
        st.markdown("### Dynasty Outlook & Future Draft Capital")
        d_cols = [
            "Team", "Archetype", "Avg Age", "Youth % (<=25)",
            "1sts (Next 3Y)", "2nds (Next 3Y)", "3rds (Next 3Y)", "Power Score"
        ]
        st.dataframe(
            df[d_cols].sort_values(["Avg Age", "1sts (Next 3Y)"], ascending=[True, False]),
            use_container_width=True,
            hide_index=True,
        )
else:
    st.info("Enter your Sleeper League ID in the left sidebar and click **Load League Data** to start.")