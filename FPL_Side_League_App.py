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
    "captain_tiebreak_mode": "effective",
    "groups": {},
    "group_fixture_mode": "auto",
    "group_fixtures": {},
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

            cap = next(
                (p for p in picks if p.get("is_captain")),
                None
            )

            vc = next(
                (p for p in picks if p.get("is_vice_captain")),
                None
            )

            cap_base = (
                live_cache[gw].get(cap["element"], 0)
                if cap else 0
            )

            vc_base = (
                live_cache[gw].get(vc["element"], 0)
                if vc else 0
            )

            # Final FPL multiplier:
            # Normal captain = x2
            # Triple Captain = x3
            # Captain DNP = 0
            # Vice inherits x2/x3 if FPL transfers captaincy
            cap_eff = (
                cap_base * int(cap.get("multiplier", 0))
                if cap else 0
            )

            vc_eff = (
                vc_base * int(vc.get("multiplier", 0))
                if vc else 0
            )

            event_points = int(h.get("points", 0))
            hit = int(h.get("event_transfers_cost", 0))

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
                "league_total_at_pull": int(
                    h.get("total_points", 0)
                ),
                "league_rank_at_pull": int(
                    h.get("overall_rank", 0) or 0
                ),
            })

    return (
        league_meta,
        pd.DataFrame(records),
        pd.DataFrame(members),
        gw_now
    )


def tiebreak_columns(config):
    if config.get(
        "captain_tiebreak_mode",
        "effective"
    ) == "base":
        return "captain_base", "vice_base"

    return "captain_effective", "vice_effective"


# -----------------------------
# Last Man Standing
# -----------------------------
def calculate_lms(gw_df, start_gw, cap_col, vc_col):
    alive = set(gw_df["entry_id"].unique())
    eliminations = []

    for gw in sorted(gw_df["gw"].unique()):

        if gw < start_gw or len(alive) <= 1:
            continue

        d = gw_df[
            (gw_df["gw"] == gw)
            & (gw_df["entry_id"].isin(alive))
        ].copy()

        if d.empty:
            continue

        d = d.sort_values(
            [
                "net_points",
                cap_col,
                vc_col,
                "league_total_at_pull",
                "entry_id"
            ],
            ascending=[
                True,
                True,
                True,
                True,
                True
            ]
        )

        loser = d.iloc[0]

        alive.remove(
            int(loser["entry_id"])
        )

        eliminations.append({
            "GW": gw,
            "Eliminated": loser["team"],
            "Manager": loser["manager"],
            "Net": int(loser["net_points"]),
            "Hit": int(loser["hit_cost"]),
            "Captain": int(loser[cap_col]),
            "Vice": int(loser[vc_col]),
            "League Total": int(
                loser["league_total_at_pull"]
            ),
        })

    return alive, pd.DataFrame(eliminations)


# -----------------------------
# Champions League scoring
# -----------------------------
def fixture_result(
    gw_df,
    a,
    b,
    gws,
    cap_col="captain_effective",
    vc_col="vice_effective"
):
    """
    Knockout tiebreak:
    1. Aggregate net FPL points
    2. Effective captain points
    3. Effective vice-captain points
    """

    arows = gw_df[
        (gw_df.entry_id == a)
        & (gw_df.gw.isin(gws))
    ]

    brows = gw_df[
        (gw_df.entry_id == b)
        & (gw_df.gw.isin(gws))
    ]

    a_score = int(
        arows.net_points.sum()
    )

    b_score = int(
        brows.net_points.sum()
    )

    if a_score != b_score:
        return (
            a if a_score > b_score else b,
            a_score,
            b_score,
            "aggregate points"
        )

    a_cap = int(
        arows["captain_effective"].sum()
    )

    b_cap = int(
        brows["captain_effective"].sum()
    )

    if a_cap != b_cap:
        return (
            a if a_cap > b_cap else b,
            a_score,
            b_score,
            "effective captain"
        )

    a_vc = int(
        arows["vice_effective"].sum()
    )

    b_vc = int(
        brows["vice_effective"].sum()
    )

    if a_vc != b_vc:
        return (
            a if a_vc > b_vc else b,
            a_score,
            b_score,
            "effective vice-captain"
        )

    return (
        None,
        a_score,
        b_score,
        "unresolved"
    )


# -----------------------------
# Group fixtures
# -----------------------------
def round_robin_four(ids, gws):

    if len(ids) != 4:
        return []

    a, b, c, d = ids

    pairings = [
        [
            (a, d),
            (b, c)
        ],
        [
            (a, c),
            (d, b)
        ],
        [
            (a, b),
            (c, d)
        ],
    ]

    fixtures = []

    for gw, round_pairs in zip(
        gws,
        pairings
    ):
        for x, y in round_pairs:
            fixtures.append(
                (gw, x, y)
            )

    return fixtures


def group_fixtures_for(
    group_name,
    ids,
    gws
):
    """
    Auto mode:
    use standard round robin.

    Manual mode:
    use commissioner fixtures.
    """

    if cfg.get(
        "group_fixture_mode",
        "auto"
    ) != "manual":

        return round_robin_four(
            ids,
            gws
        )

    saved = (
        cfg
        .get("group_fixtures", {})
        .get(group_name, [])
    )

    fixtures = []

    valid_ids = {
        int(x)
        for x in ids
    }

    valid_gws = {
        int(x)
        for x in gws
    }

    for item in saved:

        try:
            gw = int(item[0])
            a = int(item[1])
            b = int(item[2])

        except Exception:
            continue

        if (
            gw in valid_gws
            and a in valid_ids
            and b in valid_ids
            and a != b
        ):
            fixtures.append(
                (gw, a, b)
            )

    ok, _ = (
        validate_group_fixture_schedule(
            fixtures,
            ids,
            gws
        )
    )

    if ok:
        return fixtures

    return round_robin_four(
        ids,
        gws
    )


def validate_group_fixture_schedule(
    fixtures,
    ids,
    gws
):
    """
    Valid group schedule:
    - four teams
    - two matches per GW
    - every team plays once each GW
    - six unique matches total
    """

    ids = [
        int(x)
        for x in ids
    ]

    gws = [
        int(x)
        for x in gws
    ]

    if len(ids) != 4:
        return (
            False,
            "Each group must contain exactly four teams."
        )

    if len(fixtures) != 6:
        return (
            False,
            "Each group must have exactly six matches."
        )

    seen_pairs = set()

    for gw in gws:

        round_matches = [
            (int(a), int(b))
            for fixture_gw, a, b in fixtures
            if int(fixture_gw) == gw
        ]

        if len(round_matches) != 2:
            return (
                False,
                f"GW{gw} must contain exactly two matches."
            )

        used = []

        for a, b in round_matches:

            if a == b:
                return (
                    False,
                    f"A team cannot play itself in GW{gw}."
                )

            if (
                a not in ids
                or b not in ids
            ):
                return (
                    False,
                    f"GW{gw} contains a team outside this group."
                )

            used.extend(
                [a, b]
            )

            pair = tuple(
                sorted(
                    (a, b)
                )
            )

            if pair in seen_pairs:
                return (
                    False,
                    "The same matchup cannot be repeated during the group stage."
                )

            seen_pairs.add(
                pair
            )

        if sorted(used) != sorted(ids):
            return (
                False,
                f"Every team must play exactly once in GW{gw}."
            )

    if len(seen_pairs) != 6:
        return (
            False,
            "All six unique group matchups must occur exactly once."
        )

    return True, ""


def manual_group_fixture_editor(
    group_name,
    ids,
    gws,
    entry_to_team
):
    """
    Commissioner picks Match 1 each GW.
    Remaining two teams become Match 2.
    """

    ids = [
        int(x)
        for x in ids
    ]

    labels = {
        i: entry_to_team.get(
            i,
            str(i)
        )
        for i in ids
    }

    fixtures = []

    st.markdown(
        f"**Group {group_name}**"
    )

    for gw in gws:

        c1, c2 = st.columns(2)

        with c1:

            team1 = st.selectbox(
                f"GW{gw} — Match 1, Team 1",
                options=ids,
                format_func=lambda x:
                    labels[int(x)],
                key=(
                    f"group_{group_name}_"
                    f"gw_{gw}_team1"
                )
            )

        with c2:

            opponent_options = [
                x
                for x in ids
                if int(x) != int(team1)
            ]

            team2 = st.selectbox(
                f"GW{gw} — Match 1, Team 2",
                options=opponent_options,
                format_func=lambda x:
                    labels[int(x)],
                key=(
                    f"group_{group_name}_"
                    f"gw_{gw}_team2"
                )
            )

        remaining = [
            x
            for x in ids
            if int(x)
            not in {
                int(team1),
                int(team2)
            }
        ]

        fixtures.append(
            (
                int(gw),
                int(team1),
                int(team2)
            )
        )

        fixtures.append(
            (
                int(gw),
                int(remaining[0]),
                int(remaining[1])
            )
        )

        st.caption(
            f"GW{gw}: "
            f"{labels[int(team1)]} "
            f"vs {labels[int(team2)]}"
            f"  |  "
            f"{labels[int(remaining[0])]} "
            f"vs {labels[int(remaining[1])]}"
        )

    return fixtures


# -----------------------------
# Group table
# -----------------------------
def build_group_table(
    group_name,
    ids,
    gw_df,
    group_gws,
    gw_now,
    entry_to_team,
    entry_to_league_rank
):

    rows = {
        i: {
            "entry_id": i,
            "team": entry_to_team.get(
                i,
                str(i)
            ),
            "Group": group_name,
            "League Rank":
                int(
                    entry_to_league_rank
                    .get(i, 999999)
                ),
            "P": 0,
            "W": 0,
            "D": 0,
            "L": 0,
            "PF": 0,
            "PA": 0,
            "Pts": 0
        }
        for i in ids
    }

    fixture_rows = []

    for gw, a, b in group_fixtures_for(
        group_name,
        ids,
        group_gws
    ):

        if gw > gw_now:

            fixture_rows.append({
                "GW": gw,
                "Home":
                    entry_to_team.get(
                        a,
                        str(a)
                    ),
                "Away":
                    entry_to_team.get(
                        b,
                        str(b)
                    ),
                "Score": "—",
                "Result": "Upcoming"
            })

            continue

        winner, sa, sb, reason = (
            fixture_result(
                gw_df,
                a,
                b,
                [gw]
            )
        )

        rows[a]["P"] += 1
        rows[b]["P"] += 1

        rows[a]["PF"] += sa
        rows[a]["PA"] += sb

        rows[b]["PF"] += sb
        rows[b]["PA"] += sa

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

            result = (
                f"{entry_to_team.get(a)} won"
            )

        else:

            rows[b]["W"] += 1
            rows[a]["L"] += 1

            rows[b]["Pts"] += 3

            result = (
                f"{entry_to_team.get(b)} won"
            )

        fixture_rows.append({
            "GW": gw,
            "Home":
                entry_to_team.get(
                    a,
                    str(a)
                ),
            "Away":
                entry_to_team.get(
                    b,
                    str(b)
                ),
            "Score":
                f"{sa}-{sb}",
            "Result":
                result
        })

    table = pd.DataFrame(
        rows.values()
    )

    table["GD"] = (
        table["PF"]
        - table["PA"]
    )

    # Group-stage tiebreak:
    # 1) Group points
    # 2) Total FPL points scored
    # 3) Current main-league position
    table = table.sort_values(
        [
            "Pts",
            "PF",
            "League Rank",
            "entry_id"
        ],
        ascending=[
            False,
            False,
            True,
            True
        ]
    ).reset_index(
        drop=True
    )

    table["Pos"] = (
        table.index + 1
    )

    return (
        table,
        pd.DataFrame(
            fixture_rows
        )
    )


def get_group_qualifiers(
    groups,
    gw_df,
    group_gws,
    gw_now,
    entry_to_team,
    entry_to_league_rank
):

    if (
        not groups
        or not group_gws
        or max(group_gws) > gw_now
    ):
        return (
            pd.DataFrame(),
            {}
        )

    qualifier_rows = []
    tables = {}

    for group_name in sorted(groups):

        ids = [
            int(x)
            for x in groups[group_name]
        ]

        if len(ids) != 4:
            continue

        table, _ = build_group_table(
            group_name,
            ids,
            gw_df,
            group_gws,
            gw_now,
            entry_to_team,
            entry_to_league_rank
        )

        tables[group_name] = table

        for _, r in (
            table
            .head(2)
            .iterrows()
        ):

            qualifier_rows.append({
                "Group":
                    group_name,
                "Position":
                    int(r["Pos"]),
                "entry_id":
                    int(r["entry_id"]),
                "team":
                    r["team"],
                "Group Pts":
                    int(r["Pts"]),
                "PF":
                    int(r["PF"]),
                "GD":
                    int(r["GD"]),
                "League Rank":
                    int(r["League Rank"]),
            })

    return (
        pd.DataFrame(
            qualifier_rows
        ),
        tables
    )


# -----------------------------
# Knockout draw helpers
# -----------------------------
def draw_round_of_16(
    qualifiers_df
):
    """
    Auto R16 draw:
    - winner vs runner-up
    - no same-group rematch
    """

    if (
        qualifiers_df is None
        or qualifiers_df.empty
        or len(qualifiers_df) != 16
    ):
        return None

    winners = (
        qualifiers_df[
            qualifiers_df["Position"] == 1
        ][
            ["Group", "entry_id"]
        ]
        .to_dict("records")
    )

    runners = (
        qualifiers_df[
            qualifiers_df["Position"] == 2
        ][
            ["Group", "entry_id"]
        ]
        .to_dict("records")
    )

    random.shuffle(
        winners
    )

    random.shuffle(
        runners
    )

    def backtrack(
        i,
        remaining,
        pairs
    ):

        if i == len(winners):
            return pairs

        w = winners[i]

        candidates = [
            r
            for r in remaining
            if r["Group"]
            != w["Group"]
        ]

        random.shuffle(
            candidates
        )

        for r in candidates:

            next_remaining = [
                x
                for x in remaining
                if x["entry_id"]
                != r["entry_id"]
            ]

            result = backtrack(
                i + 1,
                next_remaining,
                pairs + [[
                    int(w["entry_id"]),
                    int(r["entry_id"])
                ]]
            )

            if result is not None:
                return result

        return None

    return backtrack(
        0,
        runners,
        []
    )


def round_winners(
    pairs,
    gws,
    gw_df,
    gw_now
):

    if (
        not pairs
        or not gws
        or max(gws) > gw_now
    ):
        return None

    winners = []

    for a, b in pairs:

        winner, _, _, reason = (
            fixture_result(
                gw_df,
                int(a),
                int(b),
                gws
            )
        )

        if winner is None:
            return None

        winners.append(
            int(winner)
        )

    return winners


def random_open_draw(
    entry_ids
):

    ids = [
        int(x)
        for x in entry_ids
    ]

    if (
        len(ids) % 2 != 0
        or len(ids) < 2
    ):
        return None

    random.shuffle(ids)

    return [
        [
            ids[i],
            ids[i + 1]
        ]
        for i in range(
            0,
            len(ids),
            2
        )
    ]


def validate_manual_pairings(
    pairs,
    eligible_ids
):

    eligible = [
        int(x)
        for x in eligible_ids
    ]

    flat = [
        int(x)
        for pair in pairs
        for x in pair
    ]

    if any(
        len(pair) != 2
        for pair in pairs
    ):
        return (
            False,
            "Every matchup must contain exactly two teams."
        )

    if any(
        int(a) == int(b)
        for a, b in pairs
    ):
        return (
            False,
            "A team cannot be matched against itself."
        )

    if len(flat) != len(eligible):
        return (
            False,
            "The number of selected teams does not match the number of qualifiers."
        )

    if set(flat) != set(eligible):
        return (
            False,
            "Every qualifying team must appear exactly once."
        )

    if len(flat) != len(set(flat)):
        return (
            False,
            "A team has been selected more than once."
        )

    return True, ""


def manual_pairing_editor(
    round_key,
    eligible_ids,
    entry_to_team
):

    ids = [
        int(x)
        for x in eligible_ids
    ]

    team_options = {
        int(i):
            entry_to_team.get(
                int(i),
                str(i)
            )
        for i in ids
    }

    st.markdown(
        f"#### Manual {round_key} draw"
    )

    st.caption(
        "Select each matchup. "
        "Every qualifying team must be used exactly once."
    )

    pairs = []

    for i in range(
        len(ids) // 2
    ):

        c1, c2 = st.columns(2)

        with c1:

            a = st.selectbox(
                f"Match {i+1} — Team 1",
                options=ids,
                format_func=lambda x:
                    team_options[int(x)],
                key=(
                    f"{round_key}_"
                    f"manual_a_{i}"
                )
            )

        with c2:

            b = st.selectbox(
                f"Match {i+1} — Team 2",
                options=ids,
                format_func=lambda x:
                    team_options[int(x)],
                key=(
                    f"{round_key}_"
                    f"manual_b_{i}"
                )
            )

        pairs.append([
            int(a),
            int(b)
        ])

    return pairs


# -----------------------------
# UI
# -----------------------------
st.title(
    "⚽ FPL Side-League Tracker"
)

st.caption(
    f"Classic League ID: "
    f"{cfg['league_id']} "
    f"• CL manual fixtures v5.2"
)

try:

    (
        league_meta,
        gw_df,
        members_df,
        gw_now
    ) = build_gw_data(
        int(cfg["league_id"])
    )

except Exception as e:

    st.error(
        "Could not reach the FPL API. "
        "Check your internet connection and try again."
    )

    st.exception(e)
    st.stop()


league_name = (
    league_meta.get(
        "name",
        f"League {cfg['league_id']}"
    )
)

st.subheader(
    league_name
)

cap_col, vc_col = (
    tiebreak_columns(cfg)
)

team_lookup = (
    members_df[
        [
            "entry",
            "entry_name",
            "player_name",
            "rank",
            "total"
        ]
    ]
    .rename(
        columns={
            "entry":
                "entry_id",
            "entry_name":
                "team",
            "player_name":
                "manager"
        }
    )
)

entry_to_team = dict(
    zip(
        team_lookup
        .entry_id
        .astype(int),

        team_lookup
        .team
    )
)

entry_to_league_rank = dict(
    zip(
        team_lookup
        .entry_id
        .astype(int),

        team_lookup[
            "rank"
        ].astype(int)
    )
)


tab1, tab2, tab3, tab4, tab5 = (
    st.tabs([
        "🏆 Regular League",
        "💀 Last Man Standing",
        "⭐ Champions League",
        "📊 GW Audit",
        "⚙️ Admin"
    ])
)


# -----------------------------
# Regular League
# -----------------------------
with tab1:

    s = team_lookup.sort_values(
        ["rank", "total"],
        ascending=[
            True,
            False
        ]
    ).copy()

    s["entry_id"] = (
        s["entry_id"]
        .astype(int)
    )

    st.dataframe(
        s[
            [
                "rank",
                "team",
                "manager",
                "total"
            ]
        ],
        use_container_width=True,
        hide_index=True
    )


# -----------------------------
# LMS
# -----------------------------
with tab2:

    alive, elim = (
        calculate_lms(
            gw_df,
            int(
                cfg["lms_start_gw"]
            ),
            cap_col,
            vc_col
        )
    )

    c1, c2, c3 = (
        st.columns(3)
    )

    c1.metric(
        "Current GW",
        gw_now
    )

    c2.metric(
        "Still alive",
        len(alive)
    )

    c3.metric(
        "Eliminated",
        len(elim)
    )

    if not elim.empty:

        latest = (
            elim
            .sort_values(
                "GW",
                ascending=False
            )
            .iloc[0]
        )

        st.warning(
            f"Latest elimination — "
            f"GW{latest['GW']}: "
            f"**{latest['Eliminated']}** "
            f"({latest['Net']} net pts)"
        )

        st.dataframe(
            elim.sort_values(
                "GW",
                ascending=False
            ),
            use_container_width=True,
            hide_index=True
        )

    survivors = (
        team_lookup[
            team_lookup
            .entry_id
            .astype(int)
            .isin(alive)
        ]
        .sort_values("rank")
    )

    st.markdown(
        "#### Survivors"
    )

    st.dataframe(
        survivors[
            [
                "rank",
                "team",
                "manager",
                "total"
            ]
        ],
        use_container_width=True,
        hide_index=True
    )


# -----------------------------
# Champions League
# -----------------------------
with tab3:

    qual_gw = int(
        cfg["cl_qualification_gw"]
    )

    q = (
        gw_df[
            gw_df.gw <= qual_gw
        ]
        .groupby(
            [
                "entry_id",
                "team",
                "manager"
            ],
            as_index=False
        )[
            "net_points"
        ]
        .sum()
    )

    q = (
        q.sort_values(
            [
                "net_points",
                "entry_id"
            ],
            ascending=[
                False,
                True
            ]
        )
        .head(32)
        .reset_index(
            drop=True
        )
    )

    q.index = (
        q.index + 1
    )

    q["Seed"] = q.index

    st.markdown(
        f"#### Qualification snapshot "
        f"— top 32 through GW{qual_gw}"
    )

    st.dataframe(
        q[
            [
                "Seed",
                "team",
                "manager",
                "net_points"
            ]
        ],
        use_container_width=True,
        hide_index=True
    )

    st.caption(
        "Group tiebreak: "
        "group points → "
        "total FPL points scored across group games → "
        "current main-league position. "
        "Knockout tiebreak: "
        "aggregate net points → "
        "effective captain total → "
        "effective vice-captain total."
    )

    groups = cfg.get(
        "groups",
        {}
    )

    cl_qualifiers = (
        pd.DataFrame()
    )

    cl_group_tables = {}

    if not groups:

        st.info(
            "No group draw saved yet. "
            "Use the Admin tab to create or edit Groups A–H."
        )

    else:

        for group_name in sorted(
            groups
        ):

            ids = [
                int(x)
                for x in groups[
                    group_name
                ]
            ]

            st.markdown(
                f"### Group {group_name}"
            )

            table, fixture_df = (
                build_group_table(
                    group_name,
                    ids,
                    gw_df,
                    cfg["cl_group_gws"],
                    gw_now,
                    entry_to_team,
                    entry_to_league_rank
                )
            )

            display_table = (
                table.copy()
            )

            display_table[
                "Status"
            ] = (
                display_table[
                    "Pos"
                ]
                .apply(
                    lambda x:
                    (
                        "Qualified"
                        if (
                            max(
                                cfg[
                                    "cl_group_gws"
                                ]
                            )
                            <= gw_now
                            and x <= 2
                        )
                        else ""
                    )
                )
            )

            st.dataframe(
                display_table[
                    [
                        "Pos",
                        "team",
                        "P",
                        "W",
                        "D",
                        "L",
                        "PF",
                        "PA",
                        "Pts",
                        "League Rank",
                        "Status"
                    ]
                ],
                use_container_width=True,
                hide_index=True
            )

            st.dataframe(
                fixture_df,
                use_container_width=True,
                hide_index=True
            )

        (
            cl_qualifiers,
            cl_group_tables
        ) = get_group_qualifiers(
            groups,
            gw_df,
            cfg["cl_group_gws"],
            gw_now,
            entry_to_team,
            entry_to_league_rank
        )

        st.markdown(
            "## Qualified for the Round of 16"
        )

        if cl_qualifiers.empty:

            st.caption(
                f"Final qualifiers will appear "
                f"automatically after "
                f"GW{max(cfg['cl_group_gws'])} "
                f"is complete."
            )

        else:

            qdisplay = (
                cl_qualifiers.copy()
            )

            qdisplay[
                "Seed Type"
            ] = (
                qdisplay[
                    "Position"
                ]
                .map({
                    1:
                        "Group winner",
                    2:
                        "Runner-up"
                })
            )

            st.dataframe(
                qdisplay[
                    [
                        "Group",
                        "Seed Type",
                        "team",
                        "Group Pts",
                        "PF",
                        "League Rank"
                    ]
                ],
                use_container_width=True,
                hide_index=True
            )

        st.markdown(
            "## Knockout rounds"
        )

        knockout = (
            cfg.get(
                "knockout_pairings",
                {}
            )
        )

        round_gws = {
            "Round of 16":
                cfg[
                    "cl_round_of_16_gws"
                ],
            "Quarterfinal":
                cfg[
                    "cl_quarterfinal_gws"
                ],
            "Semifinal":
                cfg[
                    "cl_semifinal_gws"
                ],
            "Final":
                cfg[
                    "cl_final_gws"
                ],
        }

        for round_name in [
            "Round of 16",
            "Quarterfinal",
            "Semifinal",
            "Final"
        ]:

            pairs = (
                knockout.get(
                    round_name,
                    []
                )
            )

            if not pairs:
                continue

            st.markdown(
                f"### {round_name}"
            )

            rrows = []

            gws = round_gws.get(
                round_name,
                []
            )

            for a, b in pairs:

                a = int(a)
                b = int(b)

                played_gws = [
                    gw
                    for gw in gws
                    if gw <= gw_now
                ]

                if not played_gws:

                    rrows.append({
                        "Team 1":
                            entry_to_team.get(
                                a,
                                str(a)
                            ),
                        "Team 2":
                            entry_to_team.get(
                                b,
                                str(b)
                            ),
                        "Aggregate":
                            "—",
                        "Winner":
                            "Pending",
                        "Decided by":
                            ""
                    })

                    continue

                (
                    winner,
                    sa,
                    sb,
                    reason
                ) = fixture_result(
                    gw_df,
                    a,
                    b,
                    played_gws
                )

                round_complete = (
                    bool(gws)
                    and max(gws) <= gw_now
                )

                if round_complete:

                    winner_label = (
                        entry_to_team.get(
                            winner,
                            "Unresolved"
                        )
                        if winner
                        else "Unresolved"
                    )

                    decided = reason

                else:

                    winner_label = (
                        "In progress"
                    )

                    decided = ""

                rrows.append({
                    "Team 1":
                        entry_to_team.get(
                            a,
                            str(a)
                        ),
                    "Team 2":
                        entry_to_team.get(
                            b,
                            str(b)
                        ),
                    "Aggregate":
                        f"{sa}-{sb}",
                    "Winner":
                        winner_label,
                    "Decided by":
                        decided
                })

            st.dataframe(
                pd.DataFrame(
                    rrows
                ),
                use_container_width=True,
                hide_index=True
            )


# -----------------------------
# GW Audit
# -----------------------------
with tab4:

    gws = sorted(
        gw_df.gw.unique(),
        reverse=True
    )

    selected_gw = (
        st.selectbox(
            "Gameweek",
            gws,
            index=0
        )
    )

    audit = (
        gw_df[
            gw_df.gw
            == selected_gw
        ]
        .copy()
    )

    audit = (
        audit.sort_values(
            [
                "net_points",
                cap_col,
                vc_col
            ],
            ascending=[
                False,
                False,
                False
            ]
        )
    )

    st.dataframe(
        audit[
            [
                "team",
                "manager",
                "gross_points",
                "hit_cost",
                "net_points",
                cap_col,
                vc_col,
                "league_total_at_pull"
            ]
        ],
        use_container_width=True,
        hide_index=True
    )

    st.download_button(
        "Download full GW audit CSV",
        gw_df
        .to_csv(index=False)
        .encode(),
        file_name="fpl_gw_audit.csv",
        mime="text/csv"
    )


# -----------------------------
# Admin
# -----------------------------
with tab5:

    st.markdown(
        "### Competition settings"
    )

    with st.form(
        "settings"
    ):

        lms_start = (
            st.number_input(
                "Last Man Standing start GW",
                1,
                38,
                int(
                    cfg[
                        "lms_start_gw"
                    ]
                )
            )
        )

        qual = (
            st.number_input(
                "Champions League qualification GW",
                1,
                38,
                int(
                    cfg[
                        "cl_qualification_gw"
                    ]
                )
            )
        )

        cap_mode = (
            st.selectbox(
                "Captain/vice tiebreak points",
                [
                    "effective",
                    "base"
                ],
                index=(
                    0
                    if cfg.get(
                        "captain_tiebreak_mode",
                        "effective"
                    ) == "effective"
                    else 1
                ),
                help=(
                    "Use Effective for this league: "
                    "it applies the final FPL multiplier, "
                    "including Triple Captain and "
                    "vice-captain inheritance when "
                    "the captain does not play."
                )
            )
        )

        if st.form_submit_button(
            "Save settings"
        ):

            cfg[
                "lms_start_gw"
            ] = int(
                lms_start
            )

            cfg[
                "cl_qualification_gw"
            ] = int(
                qual
            )

            cfg[
                "captain_tiebreak_mode"
            ] = cap_mode

            save_config(cfg)

            st.success(
                "Saved. Refresh the page."
            )


    # -----------------------------
    # CL Group Draw
    # -----------------------------
    st.markdown(
        "### Champions League group draw"
    )

    st.info(
        "Cloud note: after saving groups, group fixtures, "
        "or knockout pairings, download the competition "
        "config backup and replace competition_config.json "
        "in GitHub. This makes the setup persist across "
        "Streamlit restarts."
    )

    st.caption(
        "Set Groups A–H first. "
        "After that, choose automatic or manual "
        "group-stage fixtures."
    )


    if st.button(
        "Auto-draw Groups A–H from current top 32"
    ):

        ids = (
            q[
                "entry_id"
            ]
            .astype(int)
            .tolist()
        )

        random.shuffle(
            ids
        )

        cfg[
            "groups"
        ] = {
            chr(65 + i):
                ids[
                    i * 4:
                    (i + 1) * 4
                ]
            for i in range(8)
        }

        cfg[
            "group_fixtures"
        ] = {}

        save_config(cfg)

        st.success(
            "Groups saved."
        )

        st.rerun()


    group_json = (
        st.text_area(
            "Group assignments (entry IDs)",
            value=json.dumps(
                cfg.get(
                    "groups",
                    {}
                ),
                indent=2
            ),
            height=300
        )
    )


    if st.button(
        "Save group JSON"
    ):

        try:

            parsed_groups = (
                json.loads(
                    group_json
                )
            )

            cfg[
                "groups"
            ] = parsed_groups

            cfg[
                "group_fixtures"
            ] = {}

            save_config(cfg)

            st.success(
                "Group draw saved."
            )

            st.rerun()

        except Exception as e:

            st.error(
                f"Invalid JSON: {e}"
            )


    groups_for_fixtures = (
        cfg.get(
            "groups",
            {}
        )
    )

    valid_groups = (
        isinstance(
            groups_for_fixtures,
            dict
        )
        and len(
            groups_for_fixtures
        ) == 8
        and all(
            isinstance(v, list)
            and len(v) == 4
            for v in
            groups_for_fixtures.values()
        )
    )


    # -----------------------------
    # Group Fixture Setup
    # -----------------------------
    if valid_groups:

        st.markdown(
            "### Group-stage fixture setup"
        )

        fixture_mode = (
            st.radio(
                "How should GW9–GW11 fixtures be created?",
                [
                    "Auto fixtures",
                    "Manual fixtures"
                ],
                index=(
                    0
                    if cfg.get(
                        "group_fixture_mode",
                        "auto"
                    ) == "auto"
                    else 1
                ),
                horizontal=True,
                key=(
                    "group_fixture_mode_selector"
                )
            )
        )


        # -----------------------------
        # AUTO GROUP FIXTURES
        # -----------------------------
        if (
            fixture_mode
            == "Auto fixtures"
        ):

            st.caption(
                "The app will automatically create "
                "a full three-week round robin: "
                "each team plays every other team once."
            )

            if st.button(
                "Save automatic group fixtures"
            ):

                cfg[
                    "group_fixture_mode"
                ] = "auto"

                cfg[
                    "group_fixtures"
                ] = {}

                save_config(cfg)

                st.success(
                    "Automatic group fixtures saved."
                )

                st.rerun()


            preview_rows = []

            for group_name in sorted(
                groups_for_fixtures
            ):

                ids = [
                    int(x)
                    for x in
                    groups_for_fixtures[
                        group_name
                    ]
                ]

                for gw, a, b in (
                    round_robin_four(
                        ids,
                        cfg[
                            "cl_group_gws"
                        ]
                    )
                ):

                    preview_rows.append({
                        "Group":
                            group_name,
                        "GW":
                            gw,
                        "Team 1":
                            entry_to_team.get(
                                a,
                                str(a)
                            ),
                        "Team 2":
                            entry_to_team.get(
                                b,
                                str(b)
                            )
                    })

            if preview_rows:

                st.dataframe(
                    pd.DataFrame(
                        preview_rows
                    ),
                    use_container_width=True,
                    hide_index=True
                )


        # -----------------------------
        # MANUAL GROUP FIXTURES
        # -----------------------------
        else:

            st.caption(
                "Choose one matchup in each group for each GW. "
                "The other two teams are paired automatically. "
                "The app validates that all six unique matchups "
                "occur exactly once."
            )

            all_manual = {}
            all_valid = True

            for group_name in sorted(
                groups_for_fixtures
            ):

                ids = [
                    int(x)
                    for x in
                    groups_for_fixtures[
                        group_name
                    ]
                ]

                fixtures = (
                    manual_group_fixture_editor(
                        group_name,
                        ids,
                        cfg[
                            "cl_group_gws"
                        ],
                        entry_to_team
                    )
                )

                ok, msg = (
                    validate_group_fixture_schedule(
                        fixtures,
                        ids,
                        cfg[
                            "cl_group_gws"
                        ]
                    )
                )

                all_manual[
                    group_name
                ] = [
                    [
                        gw,
                        a,
                        b
                    ]
                    for gw, a, b
                    in fixtures
                ]

                if not ok:

                    all_valid = False

                    st.warning(
                        f"Group {group_name}: "
                        f"{msg}"
                    )


            if st.button(
                "Save manual group fixtures"
            ):

                if all_valid:

                    cfg[
                        "group_fixture_mode"
                    ] = "manual"

                    cfg[
                        "group_fixtures"
                    ] = all_manual

                    save_config(cfg)

                    st.success(
                        "Manual group fixtures saved."
                    )

                    st.rerun()

                else:

                    st.error(
                        "Please fix the highlighted "
                        "group schedules before saving."
                    )


        if (
            cfg.get(
                "group_fixture_mode"
            ) == "manual"
            and cfg.get(
                "group_fixtures"
            )
        ):

            st.success(
                "Current group fixture mode: Manual"
            )

        elif cfg.get(
            "group_fixture_mode",
            "auto"
        ) == "auto":

            st.success(
                "Current group fixture mode: Automatic"
            )

    else:

        st.caption(
            "Save all eight groups with four teams each "
            "before setting the group-stage fixtures."
        )


    # -----------------------------
    # Knockout Draw
    # -----------------------------
    st.markdown(
        "### Champions League knockout draw"
    )

    st.caption(
        "For R16, QF and SF you can either conduct your "
        "own live/manual draw or use the auto-draw button. "
        "Knockout tiebreak remains aggregate points → "
        "effective captain → effective vice-captain."
    )


    knockout = (
        cfg.setdefault(
            "knockout_pairings",
            {}
        )
    )


    admin_qualifiers, _ = (
        get_group_qualifiers(
            cfg.get(
                "groups",
                {}
            ),
            gw_df,
            cfg[
                "cl_group_gws"
            ],
            gw_now,
            entry_to_team,
            entry_to_league_rank
        )
    )


    # -----------------------------
    # ROUND OF 16
    # -----------------------------
    if admin_qualifiers.empty:

        st.info(
            f"Round of 16 setup becomes available "
            f"after GW{max(cfg['cl_group_gws'])} "
            f"once the groups are complete."
        )

    else:

        st.markdown(
            "#### Round of 16 qualifiers"
        )

        r16_display = (
            admin_qualifiers.copy()
        )

        r16_display[
            "Qualifier"
        ] = (
            r16_display[
                "Group"
            ].astype(str)
            +
            r16_display[
                "Position"
            ]
            .astype(int)
            .astype(str)
        )

        st.dataframe(
            r16_display[
                [
                    "Qualifier",
                    "team",
                    "Group Pts",
                    "PF",
                    "League Rank"
                ]
            ],
            use_container_width=True,
            hide_index=True
        )


        r16_ids = (
            admin_qualifiers[
                "entry_id"
            ]
            .astype(int)
            .tolist()
        )


        if not knockout.get(
            "Round of 16"
        ):

            r16_mode = (
                st.radio(
                    "Round of 16 draw method",
                    [
                        "Manual live draw",
                        "Auto-draw"
                    ],
                    horizontal=True,
                    key=(
                        "r16_draw_method"
                    )
                )
            )


            if (
                r16_mode
                == "Auto-draw"
            ):

                if st.button(
                    "🎲 Auto-draw Round of 16"
                ):

                    pairs = (
                        draw_round_of_16(
                            admin_qualifiers
                        )
                    )

                    if pairs:

                        knockout[
                            "Round of 16"
                        ] = pairs

                        cfg[
                            "knockout_pairings"
                        ] = knockout

                        save_config(cfg)

                        st.success(
                            "Round of 16 draw saved."
                        )

                        st.rerun()

                    else:

                        st.error(
                            "Could not produce a valid Round of 16 draw."
                        )


            else:

                r16_pairs = (
                    manual_pairing_editor(
                        "Round of 16",
                        r16_ids,
                        entry_to_team
                    )
                )

                if st.button(
                    "Save manual Round of 16 draw"
                ):

                    ok, msg = (
                        validate_manual_pairings(
                            r16_pairs,
                            r16_ids
                        )
                    )

                    if ok:

                        qmeta = {
                            int(
                                r[
                                    "entry_id"
                                ]
                            ):
                            {
                                "Group":
                                    str(
                                        r[
                                            "Group"
                                        ]
                                    ),
                                "Position":
                                    int(
                                        r[
                                            "Position"
                                        ]
                                    )
                            }
                            for _, r
                            in admin_qualifiers
                            .iterrows()
                        }

                        for a, b in r16_pairs:

                            if (
                                qmeta[a][
                                    "Group"
                                ]
                                ==
                                qmeta[b][
                                    "Group"
                                ]
                            ):

                                ok = False

                                msg = (
                                    "Same-group R16 matchup "
                                    "is not allowed: "
                                    f"{entry_to_team.get(a)} "
                                    "vs "
                                    f"{entry_to_team.get(b)}."
                                )

                                break


                            if (
                                qmeta[a][
                                    "Position"
                                ]
                                ==
                                qmeta[b][
                                    "Position"
                                ]
                            ):

                                ok = False

                                msg = (
                                    "Each R16 matchup must be "
                                    "a group winner vs a group runner-up."
                                )

                                break


                    if ok:

                        knockout[
                            "Round of 16"
                        ] = r16_pairs

                        cfg[
                            "knockout_pairings"
                        ] = knockout

                        save_config(cfg)

                        st.success(
                            "Manual Round of 16 draw saved."
                        )

                        st.rerun()

                    else:

                        st.error(
                            msg
                        )

        else:

            st.success(
                "Round of 16 draw is saved."
            )


    # -----------------------------
    # QUARTERFINALS
    # -----------------------------
    r16_winners = (
        round_winners(
            knockout.get(
                "Round of 16",
                []
            ),
            cfg[
                "cl_round_of_16_gws"
            ],
            gw_df,
            gw_now
        )
    )


    if r16_winners:

        st.markdown(
            "#### Quarterfinal qualifiers"
        )

        st.dataframe(
            pd.DataFrame([
                {
                    "Team":
                        entry_to_team.get(
                            int(x),
                            str(x)
                        ),
                    "Entry ID":
                        int(x)
                }
                for x
                in r16_winners
            ]),
            use_container_width=True,
            hide_index=True
        )


        if not knockout.get(
            "Quarterfinal"
        ):

            qf_mode = (
                st.radio(
                    "Quarterfinal draw method",
                    [
                        "Manual live draw",
                        "Auto-draw"
                    ],
                    horizontal=True,
                    key=(
                        "qf_draw_method"
                    )
                )
            )


            if (
                qf_mode
                == "Auto-draw"
            ):

                if st.button(
                    "🎲 Auto-draw Quarterfinals"
                ):

                    knockout[
                        "Quarterfinal"
                    ] = random_open_draw(
                        r16_winners
                    )

                    cfg[
                        "knockout_pairings"
                    ] = knockout

                    save_config(cfg)

                    st.success(
                        "Quarterfinal draw saved."
                    )

                    st.rerun()


            else:

                qf_pairs = (
                    manual_pairing_editor(
                        "Quarterfinal",
                        r16_winners,
                        entry_to_team
                    )
                )

                if st.button(
                    "Save manual Quarterfinal draw"
                ):

                    ok, msg = (
                        validate_manual_pairings(
                            qf_pairs,
                            r16_winners
                        )
                    )

                    if ok:

                        knockout[
                            "Quarterfinal"
                        ] = qf_pairs

                        cfg[
                            "knockout_pairings"
                        ] = knockout

                        save_config(cfg)

                        st.success(
                            "Manual Quarterfinal draw saved."
                        )

                        st.rerun()

                    else:

                        st.error(
                            msg
                        )


    # -----------------------------
    # SEMIFINALS
    # -----------------------------
    qf_winners = (
        round_winners(
            knockout.get(
                "Quarterfinal",
                []
            ),
            cfg[
                "cl_quarterfinal_gws"
            ],
            gw_df,
            gw_now
        )
    )


    if qf_winners:

        st.markdown(
            "#### Semifinal qualifiers"
        )

        st.dataframe(
            pd.DataFrame([
                {
                    "Team":
                        entry_to_team.get(
                            int(x),
                            str(x)
                        ),
                    "Entry ID":
                        int(x)
                }
                for x
                in qf_winners
            ]),
            use_container_width=True,
            hide_index=True
        )


        if not knockout.get(
            "Semifinal"
        ):

            sf_mode = (
                st.radio(
                    "Semifinal draw method",
                    [
                        "Manual live draw",
                        "Auto-draw"
                    ],
                    horizontal=True,
                    key=(
                        "sf_draw_method"
                    )
                )
            )


            if (
                sf_mode
                == "Auto-draw"
            ):

                if st.button(
                    "🎲 Auto-draw Semifinals"
                ):

                    knockout[
                        "Semifinal"
                    ] = random_open_draw(
                        qf_winners
                    )

                    cfg[
                        "knockout_pairings"
                    ] = knockout

                    save_config(cfg)

                    st.success(
                        "Semifinal draw saved."
                    )

                    st.rerun()


            else:

                sf_pairs = (
                    manual_pairing_editor(
                        "Semifinal",
                        qf_winners,
                        entry_to_team
                    )
                )

                if st.button(
                    "Save manual Semifinal draw"
                ):

                    ok, msg = (
                        validate_manual_pairings(
                            sf_pairs,
                            qf_winners
                        )
                    )

                    if ok:

                        knockout[
                            "Semifinal"
                        ] = sf_pairs

                        cfg[
                            "knockout_pairings"
                        ] = knockout

                        save_config(cfg)

                        st.success(
                            "Manual Semifinal draw saved."
                        )

                        st.rerun()

                    else:

                        st.error(
                            msg
                        )


    # -----------------------------
    # FINAL
    # -----------------------------
    sf_winners = (
        round_winners(
            knockout.get(
                "Semifinal",
                []
            ),
            cfg[
                "cl_semifinal_gws"
            ],
            gw_df,
            gw_now
        )
    )


    if (
        sf_winners
        and not knockout.get(
            "Final"
        )
    ):

        st.markdown(
            "#### Finalists"
        )

        st.dataframe(
            pd.DataFrame([
                {
                    "Team":
                        entry_to_team.get(
                            int(x),
                            str(x)
                        ),
                    "Entry ID":
                        int(x)
                }
                for x
                in sf_winners
            ]),
            use_container_width=True,
            hide_index=True
        )


        if st.button(
            "🏆 Create Final"
        ):

            knockout[
                "Final"
            ] = random_open_draw(
                sf_winners
            )

            cfg[
                "knockout_pairings"
            ] = knockout

            save_config(cfg)

            st.success(
                "Final pairing saved."
            )

            st.rerun()


    # -----------------------------
    # Saved Draw Summary
    # -----------------------------
    if knockout:

        st.markdown(
            "#### Saved knockout pairings"
        )

        for rn in [
            "Round of 16",
            "Quarterfinal",
            "Semifinal",
            "Final"
        ]:

            pairs = (
                knockout.get(
                    rn,
                    []
                )
            )

            if pairs:

                st.markdown(
                    f"**{rn}**"
                )

                summary_rows = []

                for a, b in pairs:

                    summary_rows.append({
                        "Team 1":
                            entry_to_team.get(
                                int(a),
                                str(a)
                            ),
                        "Team 2":
                            entry_to_team.get(
                                int(b),
                                str(b)
                            )
                    })

                st.dataframe(
                    pd.DataFrame(
                        summary_rows
                    ),
                    use_container_width=True,
                    hide_index=True
                )


    # -----------------------------
    # Advanced JSON override
    # -----------------------------
    with st.expander(
        "Advanced: manually edit knockout JSON"
    ):

        st.caption(
            "Use this only if you want to override a draw."
        )

        ko_json = (
            st.text_area(
                "Knockout pairings (entry IDs)",
                value=json.dumps(
                    cfg.get(
                        "knockout_pairings",
                        {}
                    ),
                    indent=2
                ),
                height=250
            )
        )


        if st.button(
            "Save knockout JSON"
        ):

            try:

                cfg[
                    "knockout_pairings"
                ] = json.loads(
                    ko_json
                )

                save_config(cfg)

                st.success(
                    "Knockout pairings saved."
                )

                st.rerun()

            except Exception as e:

                st.error(
                    f"Invalid JSON: {e}"
                )


    if st.button(
        "Clear all knockout draws"
    ):

        cfg[
            "knockout_pairings"
        ] = {}

        save_config(cfg)

        st.success(
            "All knockout draws cleared."
        )

        st.rerun()


    # -----------------------------
    # Config Backup
    # -----------------------------
    st.download_button(
        "Download competition config backup",
        json.dumps(
            cfg,
            indent=2
        ).encode(),
        file_name=(
            "competition_config.json"
        ),
        mime=(
            "application/json"
        )
    )


st.divider()

st.caption(
    "Data source: Official Fantasy Premier League "
    "public JSON endpoints. Refresh the browser "
    "after a gameweek is finalized."
)
