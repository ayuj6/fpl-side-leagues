
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


def build_group_table(group_name, ids, gw_df, group_gws, gw_now, entry_to_team, entry_to_league_rank):
    """Build one group table + fixture list using completed group-stage GWs only."""
    rows = {
        i: {
            "entry_id": i,
            "team": entry_to_team.get(i, str(i)),
            "Group": group_name,
            "League Rank": int(entry_to_league_rank.get(i, 999999)),
            "P": 0, "W": 0, "D": 0, "L": 0,
            "PF": 0, "PA": 0, "Pts": 0
        }
        for i in ids
    }
    fixture_rows = []

    for gw, a, b in round_robin_four(ids, group_gws):
        if gw > gw_now:
            fixture_rows.append({
                "GW": gw,
                "Home": entry_to_team.get(a, str(a)),
                "Away": entry_to_team.get(b, str(b)),
                "Score": "—",
                "Result": "Upcoming"
            })
            continue

        winner, sa, sb, reason = fixture_result(gw_df, a, b, [gw])
        rows[a]["P"] += 1
        rows[b]["P"] += 1
        rows[a]["PF"] += sa
        rows[a]["PA"] += sb
        rows[b]["PF"] += sb
        rows[b]["PA"] += sa

        # Group-stage matches remain draws on equal net points.
        # Captain/vice tiebreakers are used for knockout ties.
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

    # Group-stage tiebreak:
    # 1) group points
    # 2) total FPL points scored across all group games (PF)
    # 3) current position in the main mini-league (lower rank number is better)
    table = table.sort_values(
        ["Pts", "PF", "League Rank", "entry_id"],
        ascending=[False, False, True, True]
    ).reset_index(drop=True)
    table["Pos"] = table.index + 1
    return table, pd.DataFrame(fixture_rows)


def get_group_qualifiers(groups, gw_df, group_gws, gw_now, entry_to_team, entry_to_league_rank):
    """
    Return the top two from every completed group.
    Qualification is only considered final once ALL configured group GWs are completed.
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
        for _, r in table.head(2).iterrows():
            qualifier_rows.append({
                "Group": group_name,
                "Position": int(r["Pos"]),
                "entry_id": int(r["entry_id"]),
                "team": r["team"],
                "Group Pts": int(r["Pts"]),
                "PF": int(r["PF"]),
                "GD": int(r["GD"]),
                "League Rank": int(r["League Rank"]),
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

# -----------------------------
# UI
# -----------------------------
st.title("⚽ FPL Side-League Tracker")
st.caption(f"Classic League ID: {cfg['league_id']} • CL rules v5")

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
        "Group tiebreak: group points → total FPL points scored across group games → current main-league position. "
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

            display_table = table.copy()
            display_table["Status"] = display_table["Pos"].apply(
                lambda x: "Qualified" if max(cfg["cl_group_gws"]) <= gw_now and x <= 2 else ""
            )

            st.dataframe(
                display_table[["Pos","team","P","W","D","L","PF","PA","Pts","League Rank","Status"]],
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
        if cl_qualifiers.empty:
            st.caption(
                f"Final qualifiers will appear automatically after GW{max(cfg['cl_group_gws'])} is complete."
            )
        else:
            qdisplay = cl_qualifiers.copy()
            qdisplay["Seed Type"] = qdisplay["Position"].map({1:"Group winner", 2:"Runner-up"})
            st.dataframe(
                qdisplay[["Group","Seed Type","team","Group Pts","PF","League Rank"]],
                use_container_width=True,
                hide_index=True
            )

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
    st.info("Cloud note: after saving groups or knockout pairings, download the competition config backup and replace competition_config.json in GitHub. This makes the draw persist across Streamlit restarts.")
    st.caption("Use top-32 qualifiers. You can auto-draw once, or edit the JSON below after your live draw.")
    if st.button("Auto-draw Groups A–H from current top 32"):
        ids = q["entry_id"].astype(int).tolist()
        random.shuffle(ids)
        cfg["groups"] = {chr(65+i): ids[i*4:(i+1)*4] for i in range(8)}
        save_config(cfg)
        st.success("Groups saved.")

    group_json = st.text_area("Group assignments (entry IDs)", value=json.dumps(cfg.get("groups",{}), indent=2), height=300)
    if st.button("Save group JSON"):
        try:
            cfg["groups"] = json.loads(group_json)
            save_config(cfg)
            st.success("Group draw saved.")
        except Exception as e:
            st.error(f"Invalid JSON: {e}")

    st.markdown("### Champions League knockout draw")
    st.caption(
        "R16: group winners are drawn against runners-up, with no same-group rematch. "
        "QF/SF/Final: open draw among the winners of the previous round. "
        "Knockout tiebreak: aggregate points → effective captain → effective vice-captain."
    )

    knockout = cfg.setdefault("knockout_pairings", {})

    # Recompute final R16 qualifiers here so Admin works independently of what is visible above.
    admin_qualifiers, _ = get_group_qualifiers(
        cfg.get("groups", {}),
        gw_df,
        cfg["cl_group_gws"],
        gw_now,
        entry_to_team,
        entry_to_league_rank
    )

    if admin_qualifiers.empty:
        st.info(
            f"Round of 16 draw becomes available automatically after GW{max(cfg['cl_group_gws'])} "
            "once Groups A–H have been saved and completed."
        )
    else:
        if not knockout.get("Round of 16"):
            if st.button("🎲 Draw Round of 16"):
                pairs = draw_round_of_16(admin_qualifiers)
                if pairs:
                    knockout["Round of 16"] = pairs
                    cfg["knockout_pairings"] = knockout
                    save_config(cfg)
                    st.success("Round of 16 draw saved. Download the config backup below.")
                    st.rerun()
                else:
                    st.error("Could not produce a valid Round of 16 draw.")
        else:
            st.success("Round of 16 draw is saved.")

    # Quarterfinal draw becomes available once R16 is complete.
    r16_winners = round_winners(
        knockout.get("Round of 16", []),
        cfg["cl_round_of_16_gws"],
        gw_df, gw_now
    )
    if r16_winners and not knockout.get("Quarterfinal"):
        if st.button("🎲 Draw Quarterfinals"):
            knockout["Quarterfinal"] = random_open_draw(r16_winners)
            cfg["knockout_pairings"] = knockout
            save_config(cfg)
            st.success("Quarterfinal draw saved.")
            st.rerun()

    qf_winners = round_winners(
        knockout.get("Quarterfinal", []),
        cfg["cl_quarterfinal_gws"],
        gw_df, gw_now
    )
    if qf_winners and not knockout.get("Semifinal"):
        if st.button("🎲 Draw Semifinals"):
            knockout["Semifinal"] = random_open_draw(qf_winners)
            cfg["knockout_pairings"] = knockout
            save_config(cfg)
            st.success("Semifinal draw saved.")
            st.rerun()

    sf_winners = round_winners(
        knockout.get("Semifinal", []),
        cfg["cl_semifinal_gws"],
        gw_df, gw_now
    )
    if sf_winners and not knockout.get("Final"):
        if st.button("🏆 Create Final"):
            knockout["Final"] = random_open_draw(sf_winners)
            cfg["knockout_pairings"] = knockout
            save_config(cfg)
            st.success("Final pairing saved.")
            st.rerun()

    # Current knockout draw summary.
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
