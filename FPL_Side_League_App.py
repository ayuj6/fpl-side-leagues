
import json
import random
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

LEAGUE_ID = 55439
FPL = "https://fantasy.premierleague.com/api"
CONFIG_FILE = Path("competition_config.json")

st.set_page_config(page_title="FPL Side League Tracker", page_icon="⚽", layout="wide")

# -----------------------------
# API helpers
# -----------------------------
@st.cache_data(ttl=300, show_spinner=False)
def api_get(path: str):
    r = requests.get(f"{FPL}{path}", timeout=30)
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=300, show_spinner=False)
def bootstrap():
    return api_get("/bootstrap-static/")

def current_gw():
    events = bootstrap()["events"]
    finished = [e["id"] for e in events if e.get("finished")]
    current = [e["id"] for e in events if e.get("is_current")]
    if current:
        return current[0]
    if finished:
        return max(finished)
    return 1

@st.cache_data(ttl=300, show_spinner=False)
def league_members(league_id: int):
    members = []
    page = 1
    league_meta = None
    while True:
        payload = api_get(f"/leagues-classic/{league_id}/standings/?page_standings={page}")
        league_meta = payload.get("league", league_meta)
        rows = payload["standings"]["results"]
        members.extend(rows)
        if not payload["standings"].get("has_next"):
            break
        page += 1
    return league_meta or {}, members

@st.cache_data(ttl=300, show_spinner=False)
def entry_history(entry_id: int):
    return api_get(f"/entry/{entry_id}/history/")

@st.cache_data(ttl=300, show_spinner=False)
def entry_picks(entry_id: int, gw: int):
    return api_get(f"/entry/{entry_id}/event/{gw}/picks/")

@st.cache_data(ttl=300, show_spinner=False)
def gw_live(gw: int):
    payload = api_get(f"/event/{gw}/live/")
    return {x["id"]: x["stats"]["total_points"] for x in payload["elements"]}

# -----------------------------
# Config
# -----------------------------
DEFAULT_CONFIG = {
    "league_id": LEAGUE_ID,
    "lms_start_gw": 1,
    "cl_qualification_gw": 8,
    "cl_group_gws": [9, 10, 11],
    "cl_round_of_16_gws": [12, 13],
    "cl_quarterfinal_gws": [14, 15],
    "cl_semifinal_gws": [16, 17],
    "cl_final_gws": [18],
    "captain_tiebreak_mode": "effective",  # final FPL multiplier applied
    "groups": {},
    "group_fixture_mode": "auto",
    "group_fixtures": {},
    "group_manual_qualifiers": {},
    "knockout_pairings": {}
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(json.loads(CONFIG_FILE.read_text()))
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

cfg = load_config()

# -----------------------------
# Build normalized GW data
# -----------------------------
@st.cache_data(ttl=300, show_spinner=True)
def build_gw_data(league_id: int):
    league_meta, members = league_members(league_id)
    gw_now = current_gw()
    live_cache = {}
    records = []
    for idx, m in enumerate(members):
        entry = int(m["entry"])
        hist = entry_history(entry)
        hist_by_gw = {int(x["event"]): x for x in hist.get("current", [])}
        for gw, h in hist_by_gw.items():
            if gw not in live_cache:
                live_cache[gw] = gw_live(gw)
            picks_payload = entry_picks(entry, gw)
            picks = picks_payload.get("picks", [])
            cap = next((p for p in picks if p.get("is_captain")), None)
            vc = next((p for p in picks if p.get("is_vice_captain")), None)

            cap_base = live_cache[gw].get(cap["element"], 0) if cap else 0
            vc_base = live_cache[gw].get(vc["element"], 0) if vc else 0

            # picks[].multiplier is the manager's FINAL scoring multiplier.
            # Normal captain = x2, Triple Captain = x3.
            # If captain does not play, captain multiplier becomes 0 and
            # the vice-captain inherits x2/x3 when FPL applies the substitution.
            cap_eff = cap_base * int(cap.get("multiplier", 0)) if cap else 0
            vc_eff = vc_base * int(vc.get("multiplier", 0)) if vc else 0

            event_points = int(h.get("points", 0))
            hit = int(h.get("event_transfers_cost", 0))
            # FPL history "points" is the GW score before transfer-hit deductions.
            # Official season total accounts for the hit separately.
            gross = event_points
            net = event_points - hit

            records.append({
                "entry_id": entry,
                "manager": m.get("player_name", ""),
                "team": m.get("entry_name", ""),
                "gw": gw,
                "gross_points": gross,
                "hit_cost": hit,
                "net_points": net,
                "captain_base": cap_base,
                "captain_effective": cap_eff,
                "vice_base": vc_base,
                "vice_effective": vc_eff,
                "league_total_at_pull": int(h.get("total_points", 0)),
                "league_rank_at_pull": int(h.get("overall_rank", 0) or 0),
            })
    return league_meta, pd.DataFrame(records), pd.DataFrame(members), gw_now

def tiebreak_columns(config):
    # Use final/post-multiplier FPL scores for this league's tiebreak rules.
    # This captures x2 captain, x3 Triple Captain, and vice-captain inheritance.
    if config.get("captain_tiebreak_mode", "effective") == "base":
        return "captain_base", "vice_base"
    return "captain_effective", "vice_effective"

def calculate_lms(gw_df, start_gw, cap_col, vc_col):
    alive = set(gw_df["entry_id"].unique())
    eliminations = []
    for gw in sorted(gw_df["gw"].unique()):
        if gw < start_gw or len(alive) <= 1:
            continue
        d = gw_df[(gw_df["gw"] == gw) & (gw_df["entry_id"].isin(alive))].copy()
        if d.empty:
            continue
        # Lowest net is worst; on a tie, highest captain survives, then highest vice,
        # then highest current league total survives. The eliminated manager is therefore
        # lowest net, then lowest captain, lowest vice, lowest total.
        d = d.sort_values(
            ["net_points", cap_col, vc_col, "league_total_at_pull", "entry_id"],
            ascending=[True, True, True, True, True]
        )
        loser = d.iloc[0]
        alive.remove(int(loser["entry_id"]))
        eliminations.append({
            "GW": gw,
            "Eliminated": loser["team"],
            "Manager": loser["manager"],
            "Net": int(loser["net_points"]),
            "Hit": int(loser["hit_cost"]),
            "Captain": int(loser[cap_col]),
            "Vice": int(loser[vc_col]),
            "League Total": int(loser["league_total_at_pull"]),
        })
    return alive, pd.DataFrame(eliminations)

def fixture_result(gw_df, a, b, gws, cap_col="captain_effective", vc_col="vice_effective"):
    """
    Champions League knockout tiebreak:
      1) aggregate net FPL points across the round
      2) aggregate EFFECTIVE captain points across the round
      3) aggregate EFFECTIVE vice-captain points across the round
    If all three are tied, the tie remains unresolved for manual handling.
    """
    arows = gw_df[(gw_df.entry_id == a) & (gw_df.gw.isin(gws))]
    brows = gw_df[(gw_df.entry_id == b) & (gw_df.gw.isin(gws))]

    a_score = int(arows.net_points.sum())
    b_score = int(brows.net_points.sum())
    if a_score != b_score:
        return a if a_score > b_score else b, a_score, b_score, "aggregate points"

    a_cap = int(arows["captain_effective"].sum())
    b_cap = int(brows["captain_effective"].sum())
    if a_cap != b_cap:
        return a if a_cap > b_cap else b, a_score, b_score, "effective captain"

    a_vc = int(arows["vice_effective"].sum())
    b_vc = int(brows["vice_effective"].sum())
    if a_vc != b_vc:
        return a if a_vc > b_vc else b, a_score, b_score, "effective vice-captain"

    return None, a_score, b_score, "unresolved"

def round_robin_four(ids, gws):
    # Three rounds for a group of four
    if len(ids) != 4:
        return []
    a, b, c, d = ids
    pairings = [
        [(a, d), (b, c)],
        [(a, c), (d, b)],
        [(a, b), (c, d)],
    ]
    fixtures = []
    for gw, round_pairs in zip(gws, pairings):
        for x, y in round_pairs:
            fixtures.append((gw, x, y))
    return fixtures


def group_fixtures_for(group_name, ids, gws):
    """
    Return fixtures for a group.
    Auto mode uses the standard round robin.
    Manual mode uses the commissioner-saved fixtures from config.
    """
    if cfg.get("group_fixture_mode", "auto") != "manual":
        return round_robin_four(ids, gws)

    saved = cfg.get("group_fixtures", {}).get(group_name, [])
    fixtures = []
    valid_ids = {int(x) for x in ids}
    valid_gws = {int(x) for x in gws}

    for item in saved:
        try:
            gw, a, b = int(item[0]), int(item[1]), int(item[2])
        except Exception:
            continue
        if gw in valid_gws and a in valid_ids and b in valid_ids and a != b:
            fixtures.append((gw, a, b))

    # Only use manual fixtures if the full six-match schedule is valid.
    ok, _ = validate_group_fixture_schedule(fixtures, ids, gws)
    return fixtures if ok else round_robin_four(ids, gws)


def validate_group_fixture_schedule(fixtures, ids, gws):
    """
    A valid four-team / three-GW group schedule must have:
      - exactly two matches per GW
      - every team playing exactly once per GW
      - all six possible pairings occurring exactly once
    """
    ids = [int(x) for x in ids]
    gws = [int(x) for x in gws]

    if len(ids) != 4:
        return False, "Each group must contain exactly four teams."
    if len(fixtures) != 6:
        return False, "Each group must have exactly six matches."

    seen_pairs = set()
    for gw in gws:
        round_matches = [(int(a), int(b)) for fgw, a, b in fixtures if int(fgw) == gw]
        if len(round_matches) != 2:
            return False, f"GW{gw} must contain exactly two matches."

        used = []
        for a, b in round_matches:
            if a == b:
                return False, f"A team cannot play itself in GW{gw}."
            if a not in ids or b not in ids:
                return False, f"GW{gw} contains a team outside this group."
            used.extend([a, b])
            pair = tuple(sorted((a, b)))
            if pair in seen_pairs:
                return False, "The same matchup cannot be repeated during the group stage."
            seen_pairs.add(pair)

        if sorted(used) != sorted(ids):
            return False, f"Every team must play exactly once in GW{gw}."

    if len(seen_pairs) != 6:
        return False, "All six unique group matchups must occur exactly once."

    return True, ""


def manual_group_fixture_editor(group_name, ids, gws, entry_to_team):
    """
    For each GW, commissioner selects one matchup.
    The remaining two teams are automatically paired for match 2.
    """
    ids = [int(x) for x in ids]
    labels = {i: entry_to_team.get(i, str(i)) for i in ids}
    fixtures = []

    st.markdown(f"**Group {group_name}**")
    for gw in gws:
        c1, c2 = st.columns(2)
        with c1:
            team1 = st.selectbox(
                f"GW{gw} — Match 1, Team 1",
                options=ids,
                format_func=lambda x: labels[int(x)],
                key=f"group_{group_name}_gw_{gw}_team1"
            )
        with c2:
            opponent_options = [x for x in ids if int(x) != int(team1)]
            team2 = st.selectbox(
                f"GW{gw} — Match 1, Team 2",
                options=opponent_options,
                format_func=lambda x: labels[int(x)],
                key=f"group_{group_name}_gw_{gw}_team2"
            )

        remaining = [x for x in ids if int(x) not in {int(team1), int(team2)}]
        fixtures.append((int(gw), int(team1), int(team2)))
        fixtures.append((int(gw), int(remaining[0]), int(remaining[1])))
        st.caption(
            f"GW{gw}: {labels[int(team1)]} vs {labels[int(team2)]}  |  "
            f"{labels[int(remaining[0])]} vs {labels[int(remaining[1])]}"
        )

    return fixtures


def build_group_table(group_name, ids, gw_df, group_gws, gw_now, entry_to_team, entry_to_league_rank):
    """
    Build one Champions League group table + fixture list.

    Group-stage match scoring:
      - Net FPL points after transfer hits
      - Win = 3 group points
      - Draw = 1 group point
      - Loss = 0 group points

    Group ranking / qualification tiebreak:
      1) Group points
      2) Aggregate net FPL points scored across the three group games (PF)
      3) Aggregate EFFECTIVE captain points across the three group games
      4) Aggregate EFFECTIVE vice-captain points across the three group games
      5) Manual commissioner ruling if still tied and the tie affects the top two
    """
    rows = {
        i: {
            "entry_id": i,
            "team": entry_to_team.get(i, str(i)),
            "Group": group_name,
            "League Rank": int(entry_to_league_rank.get(i, 999999)),  # informational only
            "P": 0, "W": 0, "D": 0, "L": 0,
            "PF": 0, "PA": 0, "Pts": 0,
            "Eff Cap": 0, "Eff VC": 0,
        }
        for i in ids
    }
    fixture_rows = []

    for gw, a, b in group_fixtures_for(group_name, ids, group_gws):
        if gw > gw_now:
            fixture_rows.append({
                "GW": gw,
                "Home": entry_to_team.get(a, str(a)),
                "Away": entry_to_team.get(b, str(b)),
                "Score": "—",
                "Result": "Upcoming"
            })
            continue

        # Individual group matches are decided ONLY by net FPL points after hits.
        # If net points are equal, the match is a draw. Captain / vice-captain
        # values are NOT used to decide an individual group match.
        arow = gw_df[(gw_df.entry_id == a) & (gw_df.gw == gw)]
        brow = gw_df[(gw_df.entry_id == b) & (gw_df.gw == gw)]

        sa = int(arow["net_points"].sum())
        sb = int(brow["net_points"].sum())

        rows[a]["P"] += 1
        rows[b]["P"] += 1
        rows[a]["PF"] += sa
        rows[a]["PA"] += sb
        rows[b]["PF"] += sb
        rows[b]["PA"] += sa

        # Each team plays once per group GW, so these aggregate to the team's
        # effective captain / vice-captain totals across all three group games.
        rows[a]["Eff Cap"] += int(arow["captain_effective"].sum())
        rows[a]["Eff VC"] += int(arow["vice_effective"].sum())
        rows[b]["Eff Cap"] += int(brow["captain_effective"].sum())
        rows[b]["Eff VC"] += int(brow["vice_effective"].sum())

        if sa == sb:
            rows[a]["D"] += 1
            rows[b]["D"] += 1
            rows[a]["Pts"] += 1
            rows[b]["Pts"] += 1
            result = "Draw"
        elif sa > sb:
            rows[a]["W"] += 1
            rows[b]["L"] += 1
            rows[a]["Pts"] += 3
            result = f"{entry_to_team.get(a)} won"
        else:
            rows[b]["W"] += 1
            rows[a]["L"] += 1
            rows[b]["Pts"] += 3
            result = f"{entry_to_team.get(b)} won"

        fixture_rows.append({
            "GW": gw,
            "Home": entry_to_team.get(a, str(a)),
            "Away": entry_to_team.get(b, str(b)),
            "Score": f"{sa}-{sb}",
            "Result": result
        })

    table = pd.DataFrame(rows.values())
    table["GD"] = table["PF"] - table["PA"]

    # Entry ID is used ONLY as a stable display-order fallback while an exact
    # four-level tie awaits a manual ruling. It is NOT a competition tiebreak.
    table = table.sort_values(
        ["Pts", "PF", "Eff Cap", "Eff VC", "entry_id"],
        ascending=[False, False, False, False, True]
    ).reset_index(drop=True)
    table["Pos"] = table.index + 1
    return table, pd.DataFrame(fixture_rows)


def group_qualification_state(table):
    """
    Determine whether the top two can be resolved automatically.

    Returns:
      fixed_top: already-resolved entries above the unresolved tie
      tie_candidates: entries still exactly tied on all four group criteria
      slots_needed: how many top-two places must be selected from tie_candidates

    If tie_candidates is empty, the top two are fully resolved automatically.
    """
    if table is None or table.empty:
        return [], [], 0

    ranked = table.copy()
    criteria = ["Pts", "PF", "Eff Cap", "Eff VC"]

    fixed_top = []
    i = 0

    while i < len(ranked) and len(fixed_top) < 2:
        row = ranked.iloc[i]
        key = tuple(int(row[c]) for c in criteria)

        tied_ids = []
        j = i
        while j < len(ranked):
            other = ranked.iloc[j]
            other_key = tuple(int(other[c]) for c in criteria)
            if other_key != key:
                break
            tied_ids.append(int(other["entry_id"]))
            j += 1

        slots_needed = 2 - len(fixed_top)

        if len(tied_ids) == 1:
            fixed_top.append(tied_ids[0])
            i = j
            continue

        # An exact tie intersects one or both qualification positions.
        # Stop here and require a manual ruling for the remaining slot(s).
        return fixed_top, tied_ids, slots_needed

    return fixed_top[:2], [], 0


def valid_group_manual_override(table, override_ids):
    """
    Validate a saved manual ruling.

    The override is an ordered [group winner, runner-up] list. It may only
    resolve the exact tie identified by the four official group criteria.
    """
    fixed_top, tie_candidates, slots_needed = group_qualification_state(table)

    if not tie_candidates:
        return False

    try:
        override = [int(x) for x in override_ids]
    except Exception:
        return False

    if len(override) != 2 or len(set(override)) != 2:
        return False

    if override[:len(fixed_top)] != fixed_top:
        return False

    chosen_from_tie = override[len(fixed_top):]
    if len(chosen_from_tie) != slots_needed:
        return False

    if any(x not in tie_candidates for x in chosen_from_tie):
        return False

    return True


def resolved_group_top_two(group_name, table, config):
    """
    Resolve the ordered [group winner, runner-up].

    Automatic resolution uses:
      Pts -> PF -> Eff Cap -> Eff VC

    If still tied, a valid commissioner override in group_manual_qualifiers
    is required. Otherwise returns None.
    """
    fixed_top, tie_candidates, slots_needed = group_qualification_state(table)

    if not tie_candidates:
        return [int(x) for x in table.head(2)["entry_id"].tolist()]

    override = config.get("group_manual_qualifiers", {}).get(group_name, [])
    if valid_group_manual_override(table, override):
        return [int(x) for x in override]

    return None


def display_group_table_with_manual_order(group_name, table, config):
    """
    If a valid manual ruling exists, display the manually resolved group winner
    and runner-up in positions 1 and 2. Otherwise retain the automatic display order.
    """
    resolved = resolved_group_top_two(group_name, table, config)
    fixed_top, tie_candidates, _ = group_qualification_state(table)

    if not tie_candidates or not resolved:
        return table.copy()

    remaining = table[~table["entry_id"].astype(int).isin(resolved)].copy()
    selected_rows = []
    for entry_id in resolved:
        selected_rows.append(table[table["entry_id"].astype(int) == int(entry_id)])

    ordered = pd.concat(selected_rows + [remaining], ignore_index=True)
    ordered["Pos"] = ordered.index + 1
    return ordered


def get_group_qualifiers(groups, gw_df, group_gws, gw_now, entry_to_team, entry_to_league_rank):
    """
    Return the ordered top two from every completed group.

    No league-position or Entry-ID fallback is used for qualification.
    If an exact tie remains after Pts -> PF -> Eff Cap -> Eff VC and it affects
    the top two, that group contributes no qualifiers until a manual ruling is saved.
    """
    if not groups or not group_gws or max(group_gws) > gw_now:
        return pd.DataFrame(), {}

    qualifier_rows = []
    tables = {}

    for group_name in sorted(groups):
        ids = [int(x) for x in groups[group_name]]
        if len(ids) != 4:
            continue

        table, _ = build_group_table(
            group_name, ids, gw_df, group_gws, gw_now, entry_to_team, entry_to_league_rank
        )
        tables[group_name] = table

        resolved_ids = resolved_group_top_two(group_name, table, cfg)
        if not resolved_ids:
            continue

        for position, entry_id in enumerate(resolved_ids, start=1):
            r = table[table["entry_id"].astype(int) == int(entry_id)].iloc[0]
            qualifier_rows.append({
                "Group": group_name,
                "Position": position,
                "entry_id": int(r["entry_id"]),
                "team": r["team"],
                "Group Pts": int(r["Pts"]),
                "PF": int(r["PF"]),
                "Eff Cap": int(r["Eff Cap"]),
                "Eff VC": int(r["Eff VC"]),
                "GD": int(r["GD"]),
            })

    return pd.DataFrame(qualifier_rows), tables

def draw_round_of_16(qualifiers_df):
    """
    Draw 8 R16 ties:
      - group winner vs group runner-up
      - no rematch against a team from the same group
    Uses a small backtracking search so the draw is always valid when possible.
    """
    if qualifiers_df is None or qualifiers_df.empty or len(qualifiers_df) != 16:
        return None

    winners = qualifiers_df[qualifiers_df["Position"] == 1][["Group","entry_id"]].to_dict("records")
    runners = qualifiers_df[qualifiers_df["Position"] == 2][["Group","entry_id"]].to_dict("records")
    random.shuffle(winners)
    random.shuffle(runners)

    def backtrack(i, remaining, pairs):
        if i == len(winners):
            return pairs
        w = winners[i]
        candidates = [r for r in remaining if r["Group"] != w["Group"]]
        random.shuffle(candidates)
        for r in candidates:
            next_remaining = [x for x in remaining if x["entry_id"] != r["entry_id"]]
            result = backtrack(i + 1, next_remaining, pairs + [[int(w["entry_id"]), int(r["entry_id"])]])
            if result is not None:
                return result
        return None

    return backtrack(0, runners, [])


def round_winners(pairs, gws, gw_df, gw_now):
    """Return completed winners for a knockout round, or None until the round is fully decided."""
    if not pairs or not gws or max(gws) > gw_now:
        return None

    winners = []
    for a, b in pairs:
        winner, _, _, reason = fixture_result(
            gw_df, int(a), int(b), gws
        )
        if winner is None:
            return None
        winners.append(int(winner))
    return winners


def random_open_draw(entry_ids):
    """Create random open-draw pairings with no restrictions."""
    ids = [int(x) for x in entry_ids]
    if len(ids) % 2 != 0 or len(ids) < 2:
        return None
    random.shuffle(ids)
    return [[ids[i], ids[i+1]] for i in range(0, len(ids), 2)]


def validate_manual_pairings(pairs, eligible_ids):
    eligible = [int(x) for x in eligible_ids]
    flat = [int(x) for pair in pairs for x in pair]
    if any(len(pair) != 2 for pair in pairs):
        return False, "Every matchup must contain exactly two teams."
    if any(int(a) == int(b) for a, b in pairs):
        return False, "A team cannot be matched against itself."
    if len(flat) != len(eligible):
        return False, "The number of selected teams does not match the number of qualifiers."
    if set(flat) != set(eligible):
        return False, "Every qualifying team must appear exactly once."
    if len(flat) != len(set(flat)):
        return False, "A team has been selected more than once."
    return True, ""


def manual_pairing_editor(round_key, eligible_ids, entry_to_team):
    ids = [int(x) for x in eligible_ids]
    team_options = {int(i): entry_to_team.get(int(i), str(i)) for i in ids}
    st.markdown(f"#### Manual {round_key} draw")
    st.caption("Select each matchup. Every qualifying team must be used exactly once.")
    pairs = []
    for i in range(len(ids) // 2):
        c1, c2 = st.columns(2)
        with c1:
            a = st.selectbox(
                f"Match {i+1} — Team 1",
                options=ids,
                format_func=lambda x: team_options[int(x)],
                key=f"{round_key}_manual_a_{i}"
            )
        with c2:
            b = st.selectbox(
                f"Match {i+1} — Team 2",
                options=ids,
                format_func=lambda x: team_options[int(x)],
                key=f"{round_key}_manual_b_{i}"
            )
        pairs.append([int(a), int(b)])
    return pairs

# -----------------------------
# UI
# -----------------------------
st.title("⚽ FPL Side-League Tracker")
st.caption(f"Classic League ID: {cfg['league_id']} • CL manual fixtures v5.3")

try:
    league_meta, gw_df, members_df, gw_now = build_gw_data(int(cfg["league_id"]))
except Exception as e:
    st.error("Could not reach the FPL API. Check your internet connection and try again.")
    st.exception(e)
    st.stop()

league_name = league_meta.get("name", f"League {cfg['league_id']}")
st.subheader(league_name)
cap_col, vc_col = tiebreak_columns(cfg)

team_lookup = (
    members_df[["entry", "entry_name", "player_name", "rank", "total"]]
    .rename(columns={"entry":"entry_id","entry_name":"team","player_name":"manager"})
)
entry_to_team = dict(zip(team_lookup.entry_id.astype(int), team_lookup.team))
entry_to_league_rank = dict(zip(team_lookup.entry_id.astype(int), team_lookup["rank"].astype(int)))

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["🏆 Regular League", "💀 Last Man Standing", "⭐ Champions League", "📊 GW Audit", "⚙️ Admin"]
)

with tab1:
    s = team_lookup.sort_values(["rank", "total"], ascending=[True, False]).copy()
    s["entry_id"] = s["entry_id"].astype(int)
    st.dataframe(
        s[["rank","team","manager","total"]],
        use_container_width=True,
        hide_index=True
    )

with tab2:
    alive, elim = calculate_lms(gw_df, int(cfg["lms_start_gw"]), cap_col, vc_col)
    c1, c2, c3 = st.columns(3)
    c1.metric("Current GW", gw_now)
    c2.metric("Still alive", len(alive))
    c3.metric("Eliminated", len(elim))
    if not elim.empty:
        latest = elim.sort_values("GW", ascending=False).iloc[0]
        st.warning(f"Latest elimination — GW{latest['GW']}: **{latest['Eliminated']}** ({latest['Net']} net pts)")
        st.dataframe(elim.sort_values("GW", ascending=False), use_container_width=True, hide_index=True)
    survivors = team_lookup[team_lookup.entry_id.astype(int).isin(alive)].sort_values("rank")
    st.markdown("#### Survivors")
    st.dataframe(survivors[["rank","team","manager","total"]], use_container_width=True, hide_index=True)

with tab3:
    qual_gw = int(cfg["cl_qualification_gw"])

    # Qualification snapshot reconstructed from cumulative net points through qualification GW.
    q = gw_df[gw_df.gw <= qual_gw].groupby(
        ["entry_id","team","manager"], as_index=False
    )["net_points"].sum()
    q = q.sort_values(["net_points","entry_id"], ascending=[False, True]).head(32).reset_index(drop=True)
    q.index = q.index + 1
    q["Seed"] = q.index

    st.markdown(f"#### Qualification snapshot — top 32 through GW{qual_gw}")
    st.dataframe(
        q[["Seed","team","manager","net_points"]],
        use_container_width=True,
        hide_index=True
    )

    st.caption(
        "Group tiebreak: group points → aggregate net FPL points across the 3 group games "
        "→ aggregate effective captain points → aggregate effective vice-captain points "
        "→ manual ruling if still tied. "
        "Knockout tiebreak: aggregate net points → effective captain total → effective vice-captain total."
    )

    groups = cfg.get("groups", {})
    cl_qualifiers = pd.DataFrame()
    cl_group_tables = {}

    if not groups:
        st.info("No group draw saved yet. Use the Admin tab to create or edit Groups A–H.")
    else:
        for group_name in sorted(groups):
            ids = [int(x) for x in groups[group_name]]
            st.markdown(f"### Group {group_name}")

            table, fixture_df = build_group_table(
                group_name,
                ids,
                gw_df,
                cfg["cl_group_gws"],
                gw_now,
                entry_to_team,
                entry_to_league_rank
            )

            display_table = display_group_table_with_manual_order(
                group_name, table, cfg
            )

            display_table["Status"] = ""
            if max(cfg["cl_group_gws"]) <= gw_now:
                resolved_ids = resolved_group_top_two(group_name, table, cfg)
                fixed_top, tie_candidates, _ = group_qualification_state(table)

                if resolved_ids:
                    manual_used = bool(tie_candidates)
                    display_table["Status"] = display_table["entry_id"].astype(int).apply(
                        lambda x: (
                            "Qualified (manual)" if manual_used and x in resolved_ids
                            else "Qualified" if x in resolved_ids
                            else ""
                        )
                    )
                elif tie_candidates:
                    display_table["Status"] = display_table["entry_id"].astype(int).apply(
                        lambda x: "Manual ruling needed" if x in tie_candidates else ""
                    )

            st.dataframe(
                display_table[[
                    "Pos","team","P","W","D","L","PF","PA","Pts",
                    "Eff Cap","Eff VC","Status"
                ]],
                use_container_width=True,
                hide_index=True
            )
            st.dataframe(fixture_df, use_container_width=True, hide_index=True)

        # Automatically determine final group-stage qualifiers once all group GWs are complete.
        cl_qualifiers, cl_group_tables = get_group_qualifiers(
            groups,
            gw_df,
            cfg["cl_group_gws"],
            gw_now,
            entry_to_team,
            entry_to_league_rank
        )

        st.markdown("## Qualified for the Round of 16")
        group_stage_complete = max(cfg["cl_group_gws"]) <= gw_now

        if not group_stage_complete:
            st.caption(
                f"Final qualifiers will appear automatically after GW{max(cfg['cl_group_gws'])} is complete."
            )
        else:
            unresolved_groups = []
            for group_name, table in cl_group_tables.items():
                if resolved_group_top_two(group_name, table, cfg) is None:
                    unresolved_groups.append(group_name)

            if not cl_qualifiers.empty:
                qdisplay = cl_qualifiers.copy()
                qdisplay["Seed Type"] = qdisplay["Position"].map({1:"Group winner", 2:"Runner-up"})
                st.dataframe(
                    qdisplay[[
                        "Group","Seed Type","team","Group Pts","PF","Eff Cap","Eff VC"
                    ]],
                    use_container_width=True,
                    hide_index=True
                )

            if unresolved_groups:
                st.warning(
                    "Manual group-stage tiebreak ruling required for Group(s): "
                    + ", ".join(unresolved_groups)
                    + ". Save the ruling in the Admin tab before the Round of 16 draw."
                )
            elif cl_qualifiers.empty:
                st.caption("No group qualifiers are available yet.")

        st.markdown("## Knockout rounds")
        knockout = cfg.get("knockout_pairings", {})
        round_gws = {
            "Round of 16": cfg["cl_round_of_16_gws"],
            "Quarterfinal": cfg["cl_quarterfinal_gws"],
            "Semifinal": cfg["cl_semifinal_gws"],
            "Final": cfg["cl_final_gws"],
        }

        for round_name in ["Round of 16", "Quarterfinal", "Semifinal", "Final"]:
            pairs = knockout.get(round_name, [])
            if not pairs:
                continue

            st.markdown(f"### {round_name}")
            rrows = []
            gws = round_gws.get(round_name, [])

            for a, b in pairs:
                a, b = int(a), int(b)

                # Show live/current aggregate once at least one round GW has started.
                played_gws = [gw for gw in gws if gw <= gw_now]
                if not played_gws:
                    rrows.append({
                        "Team 1": entry_to_team.get(a, str(a)),
                        "Team 2": entry_to_team.get(b, str(b)),
                        "Aggregate": "—",
                        "Winner": "Pending",
                        "Decided by": ""
                    })
                    continue

                winner, sa, sb, reason = fixture_result(
                    gw_df, a, b, played_gws
                )

                round_complete = bool(gws) and max(gws) <= gw_now
                if round_complete:
                    winner_label = entry_to_team.get(winner, "Unresolved") if winner else "Unresolved"
                    decided = reason
                else:
                    winner_label = "In progress"
                    decided = ""

                rrows.append({
                    "Team 1": entry_to_team.get(a, str(a)),
                    "Team 2": entry_to_team.get(b, str(b)),
                    "Aggregate": f"{sa}-{sb}",
                    "Winner": winner_label,
                    "Decided by": decided
                })

            st.dataframe(pd.DataFrame(rrows), use_container_width=True, hide_index=True)

with tab4:
    gws = sorted(gw_df.gw.unique(), reverse=True)
    selected_gw = st.selectbox("Gameweek", gws, index=0)
    audit = gw_df[gw_df.gw == selected_gw].copy()
    audit = audit.sort_values(["net_points", cap_col, vc_col], ascending=[False,False,False])
    st.dataframe(
        audit[["team","manager","gross_points","hit_cost","net_points",cap_col,vc_col,"league_total_at_pull"]],
        use_container_width=True,
        hide_index=True
    )
    st.download_button(
        "Download full GW audit CSV",
        gw_df.to_csv(index=False).encode(),
        file_name="fpl_gw_audit.csv",
        mime="text/csv"
    )

with tab5:
    st.markdown("### Competition settings")
    with st.form("settings"):
        lms_start = st.number_input("Last Man Standing start GW", 1, 38, int(cfg["lms_start_gw"]))
        qual = st.number_input("Champions League qualification GW", 1, 38, int(cfg["cl_qualification_gw"]))
        cap_mode = st.selectbox(
            "Captain/vice tiebreak points",
            ["effective", "base"],
            index=0 if cfg.get("captain_tiebreak_mode","effective") == "effective" else 1,
            help="Use Effective for this league: it applies the final FPL multiplier, including Triple Captain and vice-captain inheritance when the captain does not play."
        )
        if st.form_submit_button("Save settings"):
            cfg["lms_start_gw"] = int(lms_start)
            cfg["cl_qualification_gw"] = int(qual)
            cfg["captain_tiebreak_mode"] = cap_mode
            save_config(cfg)
            st.success("Saved. Refresh the page.")

    st.markdown("### Champions League group draw")
    st.info(
        "Cloud note: after saving groups, group fixtures, or knockout pairings, "
        "download the competition config backup and replace competition_config.json in GitHub. "
        "This makes the setup persist across Streamlit restarts."
    )
    st.caption("Set Groups A–H first. After that, choose automatic or manual group-stage fixtures.")

    if st.button("Auto-draw Groups A–H from current top 32"):
        ids = q["entry_id"].astype(int).tolist()
        random.shuffle(ids)
        cfg["groups"] = {chr(65+i): ids[i*4:(i+1)*4] for i in range(8)}
        # Reset old manual fixtures because group membership changed.
        cfg["group_fixtures"] = {}
        save_config(cfg)
        st.success("Groups saved.")
        st.rerun()

    group_json = st.text_area(
        "Group assignments (entry IDs)",
        value=json.dumps(cfg.get("groups",{}), indent=2),
        height=300
    )
    if st.button("Save group JSON"):
        try:
            parsed_groups = json.loads(group_json)
            cfg["groups"] = parsed_groups
            # Reset old manual fixtures because group membership may have changed.
            cfg["group_fixtures"] = {}
            save_config(cfg)
            st.success("Group draw saved.")
            st.rerun()
        except Exception as e:
            st.error(f"Invalid JSON: {e}")

    groups_for_fixtures = cfg.get("groups", {})
    valid_groups = (
        isinstance(groups_for_fixtures, dict)
        and len(groups_for_fixtures) == 8
        and all(isinstance(v, list) and len(v) == 4 for v in groups_for_fixtures.values())
    )

    if valid_groups:
        st.markdown("### Group-stage fixture setup")
        fixture_mode = st.radio(
            "How should GW9–GW11 fixtures be created?",
            ["Auto fixtures", "Manual fixtures"],
            index=0 if cfg.get("group_fixture_mode", "auto") == "auto" else 1,
            horizontal=True,
            key="group_fixture_mode_selector"
        )

        if fixture_mode == "Auto fixtures":
            st.caption(
                "The app will automatically create a full three-week round robin: "
                "each team plays every other team once."
            )
            if st.button("Save automatic group fixtures"):
                cfg["group_fixture_mode"] = "auto"
                cfg["group_fixtures"] = {}
                save_config(cfg)
                st.success("Automatic group fixtures saved.")
                st.rerun()

            # Preview the automatic schedule.
            preview_rows = []
            for group_name in sorted(groups_for_fixtures):
                ids = [int(x) for x in groups_for_fixtures[group_name]]
                for gw, a, b in round_robin_four(ids, cfg["cl_group_gws"]):
                    preview_rows.append({
                        "Group": group_name,
                        "GW": gw,
                        "Team 1": entry_to_team.get(a, str(a)),
                        "Team 2": entry_to_team.get(b, str(b))
                    })
            if preview_rows:
                st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)

        else:
            st.caption(
                "Choose one matchup in each group for each GW. "
                "The other two teams are paired automatically. "
                "The app validates that all six unique matchups occur exactly once."
            )

            all_manual = {}
            all_valid = True
            validation_messages = []

            for group_name in sorted(groups_for_fixtures):
                ids = [int(x) for x in groups_for_fixtures[group_name]]
                fixtures = manual_group_fixture_editor(
                    group_name,
                    ids,
                    cfg["cl_group_gws"],
                    entry_to_team
                )
                ok, msg = validate_group_fixture_schedule(
                    fixtures, ids, cfg["cl_group_gws"]
                )
                all_manual[group_name] = [[gw, a, b] for gw, a, b in fixtures]
                if not ok:
                    all_valid = False
                    validation_messages.append(f"Group {group_name}: {msg}")
                    st.warning(f"Group {group_name}: {msg}")

            if st.button("Save manual group fixtures"):
                if all_valid:
                    cfg["group_fixture_mode"] = "manual"
                    cfg["group_fixtures"] = all_manual
                    save_config(cfg)
                    st.success("Manual group fixtures saved.")
                    st.rerun()
                else:
                    st.error("Please fix the highlighted group schedules before saving.")

        if cfg.get("group_fixture_mode") == "manual" and cfg.get("group_fixtures"):
            st.success("Current group fixture mode: Manual")
        elif cfg.get("group_fixture_mode", "auto") == "auto":
            st.success("Current group fixture mode: Automatic")
    else:
        st.caption("Save all eight groups with four teams each before setting the group-stage fixtures.")

    # Manual group-stage tiebreak rulings are needed only in the rare case where
    # Pts, PF, effective captain and effective vice-captain are ALL tied.
    if valid_groups and max(cfg["cl_group_gws"]) <= gw_now:
        unresolved_manual_groups = []

        for group_name in sorted(groups_for_fixtures):
            ids = [int(x) for x in groups_for_fixtures[group_name]]
            table, _ = build_group_table(
                group_name,
                ids,
                gw_df,
                cfg["cl_group_gws"],
                gw_now,
                entry_to_team,
                entry_to_league_rank
            )
            fixed_top, tie_candidates, slots_needed = group_qualification_state(table)

            if tie_candidates and resolved_group_top_two(group_name, table, cfg) is None:
                unresolved_manual_groups.append(
                    (group_name, table, fixed_top, tie_candidates, slots_needed)
                )

        if unresolved_manual_groups:
            st.markdown("### Manual group-stage tiebreak rulings")
            st.warning(
                "These groups are still tied after group points, aggregate net score, "
                "effective captain points and effective vice-captain points. "
                "A commissioner ruling is required before the Round of 16 draw."
            )

            for group_name, table, fixed_top, tie_candidates, slots_needed in unresolved_manual_groups:
                st.markdown(f"#### Group {group_name}")
                tied_display = table[
                    table["entry_id"].astype(int).isin(tie_candidates)
                ][["team","Pts","PF","Eff Cap","Eff VC"]].copy()

                st.dataframe(
                    tied_display,
                    use_container_width=True,
                    hide_index=True
                )

                if len(fixed_top) == 1 and slots_needed == 1:
                    fixed_winner = int(fixed_top[0])
                    st.caption(
                        f"Group winner is already resolved: "
                        f"{entry_to_team.get(fixed_winner, str(fixed_winner))}. "
                        "Select the runner-up from the exact tie below."
                    )

                    runner = st.selectbox(
                        f"Group {group_name} — manual runner-up",
                        options=tie_candidates,
                        format_func=lambda x: entry_to_team.get(int(x), str(x)),
                        key=f"group_{group_name}_manual_runner"
                    )

                    if st.button(
                        f"Save Group {group_name} manual ruling",
                        key=f"save_group_{group_name}_manual_ruling"
                    ):
                        cfg.setdefault("group_manual_qualifiers", {})[group_name] = [
                            fixed_winner,
                            int(runner)
                        ]
                        save_config(cfg)
                        st.success(f"Group {group_name} manual ruling saved.")
                        st.rerun()

                else:
                    st.caption(
                        "Select the ordered group winner and runner-up from the exactly tied teams."
                    )
                    c1, c2 = st.columns(2)

                    with c1:
                        manual_winner = st.selectbox(
                            f"Group {group_name} — manual group winner",
                            options=tie_candidates,
                            format_func=lambda x: entry_to_team.get(int(x), str(x)),
                            key=f"group_{group_name}_manual_winner"
                        )

                    with c2:
                        runner_options = [
                            x for x in tie_candidates
                            if int(x) != int(manual_winner)
                        ]
                        manual_runner = st.selectbox(
                            f"Group {group_name} — manual runner-up",
                            options=runner_options,
                            format_func=lambda x: entry_to_team.get(int(x), str(x)),
                            key=f"group_{group_name}_manual_runner2"
                        )

                    if st.button(
                        f"Save Group {group_name} manual ruling",
                        key=f"save_group_{group_name}_manual_ruling"
                    ):
                        proposed = [int(manual_winner), int(manual_runner)]
                        if valid_group_manual_override(table, proposed):
                            cfg.setdefault("group_manual_qualifiers", {})[group_name] = proposed
                            save_config(cfg)
                            st.success(f"Group {group_name} manual ruling saved.")
                            st.rerun()
                        else:
                            st.error(
                                "That selection does not match the unresolved top-two tie."
                            )

        saved_manual = cfg.get("group_manual_qualifiers", {})
        if saved_manual:
            with st.expander("Saved manual group-stage rulings"):
                rows = []
                for group_name, selected in sorted(saved_manual.items()):
                    if isinstance(selected, list) and len(selected) == 2:
                        rows.append({
                            "Group": group_name,
                            "Group winner": entry_to_team.get(int(selected[0]), str(selected[0])),
                            "Runner-up": entry_to_team.get(int(selected[1]), str(selected[1]))
                        })
                if rows:
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("### Champions League knockout draw")
    st.caption(
        "For R16, QF and SF you can either conduct your own live/manual draw or use the auto-draw button. "
        "Knockout tiebreak remains aggregate points → effective captain → effective vice-captain."
    )

    knockout = cfg.setdefault("knockout_pairings", {})

    admin_qualifiers, _ = get_group_qualifiers(
        cfg.get("groups", {}),
        gw_df,
        cfg["cl_group_gws"],
        gw_now,
        entry_to_team,
        entry_to_league_rank
    )

    # ROUND OF 16
    if len(admin_qualifiers) != 16:
        if max(cfg["cl_group_gws"]) > gw_now:
            st.info(
                f"Round of 16 setup becomes available after GW{max(cfg['cl_group_gws'])} "
                "once the groups are complete."
            )
        else:
            st.info(
                "Round of 16 setup will unlock once all eight groups have two resolved qualifiers. "
                "Complete any manual group-stage tiebreak ruling shown above."
            )
    else:
        st.markdown("#### Round of 16 qualifiers")
        r16_display = admin_qualifiers.copy()
        r16_display["Qualifier"] = (
            r16_display["Group"].astype(str)
            + r16_display["Position"].astype(int).astype(str)
        )
        st.dataframe(
            r16_display[["Qualifier","team","Group Pts","PF","Eff Cap","Eff VC"]],
            use_container_width=True,
            hide_index=True
        )
        r16_ids = admin_qualifiers["entry_id"].astype(int).tolist()

        if not knockout.get("Round of 16"):
            r16_mode = st.radio(
                "Round of 16 draw method",
                ["Manual live draw", "Auto-draw"],
                horizontal=True,
                key="r16_draw_method"
            )

            if r16_mode == "Auto-draw":
                if st.button("🎲 Auto-draw Round of 16"):
                    pairs = draw_round_of_16(admin_qualifiers)
                    if pairs:
                        knockout["Round of 16"] = pairs
                        cfg["knockout_pairings"] = knockout
                        save_config(cfg)
                        st.success("Round of 16 draw saved.")
                        st.rerun()
                    else:
                        st.error("Could not produce a valid Round of 16 draw.")
            else:
                r16_pairs = manual_pairing_editor("Round of 16", r16_ids, entry_to_team)
                if st.button("Save manual Round of 16 draw"):
                    ok, msg = validate_manual_pairings(r16_pairs, r16_ids)
                    if ok:
                        qmeta = {
                            int(r["entry_id"]): {
                                "Group": str(r["Group"]),
                                "Position": int(r["Position"])
                            }
                            for _, r in admin_qualifiers.iterrows()
                        }
                        for a, b in r16_pairs:
                            if qmeta[a]["Group"] == qmeta[b]["Group"]:
                                ok = False
                                msg = f"Same-group R16 matchup is not allowed: {entry_to_team.get(a)} vs {entry_to_team.get(b)}."
                                break
                            if qmeta[a]["Position"] == qmeta[b]["Position"]:
                                ok = False
                                msg = "Each R16 matchup must be a group winner vs a group runner-up."
                                break

                    if ok:
                        knockout["Round of 16"] = r16_pairs
                        cfg["knockout_pairings"] = knockout
                        save_config(cfg)
                        st.success("Manual Round of 16 draw saved.")
                        st.rerun()
                    else:
                        st.error(msg)
        else:
            st.success("Round of 16 draw is saved.")

    # QUARTERFINALS
    r16_winners = round_winners(
        knockout.get("Round of 16", []),
        cfg["cl_round_of_16_gws"],
        gw_df, gw_now
    )

    if r16_winners:
        st.markdown("#### Quarterfinal qualifiers")
        st.dataframe(
            pd.DataFrame([
                {"Team": entry_to_team.get(int(x), str(x)), "Entry ID": int(x)}
                for x in r16_winners
            ]),
            use_container_width=True,
            hide_index=True
        )

        if not knockout.get("Quarterfinal"):
            qf_mode = st.radio(
                "Quarterfinal draw method",
                ["Manual live draw", "Auto-draw"],
                horizontal=True,
                key="qf_draw_method"
            )
            if qf_mode == "Auto-draw":
                if st.button("🎲 Auto-draw Quarterfinals"):
                    knockout["Quarterfinal"] = random_open_draw(r16_winners)
                    cfg["knockout_pairings"] = knockout
                    save_config(cfg)
                    st.success("Quarterfinal draw saved.")
                    st.rerun()
            else:
                qf_pairs = manual_pairing_editor("Quarterfinal", r16_winners, entry_to_team)
                if st.button("Save manual Quarterfinal draw"):
                    ok, msg = validate_manual_pairings(qf_pairs, r16_winners)
                    if ok:
                        knockout["Quarterfinal"] = qf_pairs
                        cfg["knockout_pairings"] = knockout
                        save_config(cfg)
                        st.success("Manual Quarterfinal draw saved.")
                        st.rerun()
                    else:
                        st.error(msg)

    # SEMIFINALS
    qf_winners = round_winners(
        knockout.get("Quarterfinal", []),
        cfg["cl_quarterfinal_gws"],
        gw_df, gw_now
    )

    if qf_winners:
        st.markdown("#### Semifinal qualifiers")
        st.dataframe(
            pd.DataFrame([
                {"Team": entry_to_team.get(int(x), str(x)), "Entry ID": int(x)}
                for x in qf_winners
            ]),
            use_container_width=True,
            hide_index=True
        )

        if not knockout.get("Semifinal"):
            sf_mode = st.radio(
                "Semifinal draw method",
                ["Manual live draw", "Auto-draw"],
                horizontal=True,
                key="sf_draw_method"
            )
            if sf_mode == "Auto-draw":
                if st.button("🎲 Auto-draw Semifinals"):
                    knockout["Semifinal"] = random_open_draw(qf_winners)
                    cfg["knockout_pairings"] = knockout
                    save_config(cfg)
                    st.success("Semifinal draw saved.")
                    st.rerun()
            else:
                sf_pairs = manual_pairing_editor("Semifinal", qf_winners, entry_to_team)
                if st.button("Save manual Semifinal draw"):
                    ok, msg = validate_manual_pairings(sf_pairs, qf_winners)
                    if ok:
                        knockout["Semifinal"] = sf_pairs
                        cfg["knockout_pairings"] = knockout
                        save_config(cfg)
                        st.success("Manual Semifinal draw saved.")
                        st.rerun()
                    else:
                        st.error(msg)

    # FINAL
    sf_winners = round_winners(
        knockout.get("Semifinal", []),
        cfg["cl_semifinal_gws"],
        gw_df, gw_now
    )
    if sf_winners and not knockout.get("Final"):
        st.markdown("#### Finalists")
        st.dataframe(
            pd.DataFrame([
                {"Team": entry_to_team.get(int(x), str(x)), "Entry ID": int(x)}
                for x in sf_winners
            ]),
            use_container_width=True,
            hide_index=True
        )
        if st.button("🏆 Create Final"):
            knockout["Final"] = random_open_draw(sf_winners)
            cfg["knockout_pairings"] = knockout
            save_config(cfg)
            st.success("Final pairing saved.")
            st.rerun()

    if knockout:
        st.markdown("#### Saved knockout pairings")
        for rn in ["Round of 16","Quarterfinal","Semifinal","Final"]:
            pairs = knockout.get(rn, [])
            if pairs:
                st.markdown(f"**{rn}**")
                summary_rows = []
                for a, b in pairs:
                    summary_rows.append({
                        "Team 1": entry_to_team.get(int(a), str(a)),
                        "Team 2": entry_to_team.get(int(b), str(b))
                    })
                st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    with st.expander("Advanced: manually edit knockout JSON"):
        st.caption(
            'Use this only if you want to override a draw. Example: '
            '{"Round of 16": [[123,456]], "Quarterfinal": []}'
        )
        ko_json = st.text_area(
            "Knockout pairings (entry IDs)",
            value=json.dumps(cfg.get("knockout_pairings",{}), indent=2),
            height=250
        )
        if st.button("Save knockout JSON"):
            try:
                cfg["knockout_pairings"] = json.loads(ko_json)
                save_config(cfg)
                st.success("Knockout pairings saved.")
                st.rerun()
            except Exception as e:
                st.error(f"Invalid JSON: {e}")

    if st.button("Clear all knockout draws"):
        cfg["knockout_pairings"] = {}
        save_config(cfg)
        st.success("All knockout draws cleared.")
        st.rerun()

    st.download_button(
        "Download competition config backup",
        json.dumps(cfg, indent=2).encode(),
        file_name="competition_config.json",
        mime="application/json"
    )

st.divider()
st.caption("Data source: Official Fantasy Premier League public JSON endpoints. Refresh the browser after a gameweek is finalized.")
