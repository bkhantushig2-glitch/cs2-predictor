CREATE TABLE news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE,
            url TEXT,
            published TEXT,
            category TEXT,
            fetched_at TEXT
        , content TEXT, teams_mentioned TEXT, players_mentioned TEXT);
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE roster_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE,
            url TEXT,
            player TEXT,
            from_team TEXT,
            to_team TEXT,
            change_type TEXT,
            published TEXT,
            fetched_at TEXT
        , content TEXT);
CREATE TABLE matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team1 TEXT,
            team2 TEXT,
            score1 INTEGER,
            score2 INTEGER,
            winner TEXT,
            event TEXT,
            match_date TEXT,
            fetched_at TEXT, event_tier TEXT, is_lan INTEGER DEFAULT 0,
            UNIQUE(team1, team2, match_date, event)
        );
CREATE TABLE team_form (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT,
            wins_last_10 INTEGER,
            losses_last_10 INTEGER,
            win_rate REAL,
            updated_at TEXT, recent_changes TEXT,
            UNIQUE(team)
        );
CREATE TABLE upcoming_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team1 TEXT,
            team2 TEXT,
            match_time TEXT,
            event TEXT,
            status TEXT,
            fetched_at TEXT, match_url TEXT,
            UNIQUE(team1, team2, event, match_time)
        );
CREATE TABLE team_maps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT,
            map_name TEXT,
            times_played INTEGER,
            wins INTEGER,
            win_rate REAL,
            updated_at TEXT,
            UNIQUE(team, map_name)
        );
CREATE TABLE team_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT UNIQUE,
            world_rank INTEGER,
            weeks_in_top30 INTEGER,
            avg_player_age REAL,
            roster TEXT,
            coach TEXT,
            total_maps INTEGER,
            total_wins INTEGER,
            total_win_rate REAL,
            lan_wins INTEGER,
            lan_maps INTEGER,
            lan_win_rate REAL,
            online_wins INTEGER,
            online_maps INTEGER,
            online_win_rate REAL,
            last_roster_change TEXT,
            updated_at TEXT
        );
CREATE TABLE match_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT,
            team1 TEXT,
            team2 TEXT,
            map_name TEXT,
            team1_score INTEGER,
            team2_score INTEGER,
            team1_ct_score INTEGER,
            team1_t_score INTEGER,
            team2_ct_score INTEGER,
            team2_t_score INTEGER,
            event TEXT,
            event_type TEXT,
            date TEXT,
            UNIQUE(match_id, map_name)
        );
CREATE TABLE player_match_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id TEXT,
            player_name TEXT,
            team TEXT,
            map_name TEXT,
            kills INTEGER,
            deaths INTEGER,
            adr REAL,
            kast REAL,
            rating REAL,
            UNIQUE(match_id, player_name, map_name)
        );
CREATE TABLE team_rankings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team TEXT UNIQUE,
        world_rank INTEGER,
        points INTEGER,
        roster TEXT,
        team_id TEXT,
        updated_at TEXT
    );
CREATE TABLE players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        team TEXT,
        role TEXT,
        rating REAL,
        country TEXT
    , adr REAL, kast REAL, impact REAL, dpr REAL, kpr REAL, maps_played INTEGER, updated_at TEXT);
CREATE TABLE team_elo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT UNIQUE,
            elo_rating REAL DEFAULT 1500,
            peak_elo REAL DEFAULT 1500,
            matches_played INTEGER DEFAULT 0,
            last_updated TEXT
        );
CREATE TABLE map_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT,
            map_name TEXT,
            times_played INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0.5,
            ct_rounds_won INTEGER DEFAULT 0,
            t_rounds_won INTEGER DEFAULT 0,
            updated_at TEXT,
            UNIQUE(team, map_name)
        );
CREATE TABLE roster_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT,
            player_in TEXT,
            player_out TEXT,
            change_date TEXT,
            change_type TEXT
        );
CREATE TABLE match_extended (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team1 TEXT,
            team2 TEXT,
            team1_score INTEGER,
            team2_score INTEGER,
            winner TEXT,
            match_format TEXT,
            event TEXT,
            event_type TEXT,
            maps_played TEXT,
            match_date TEXT,
            UNIQUE(team1, team2, match_date, event)
        );
CREATE TABLE betting_odds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team1 TEXT,
            team2 TEXT,
            team1_odds REAL,
            team2_odds REAL,
            bookmaker TEXT,
            match_date TEXT,
            fetched_at TEXT
        );
CREATE TABLE player_props (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player TEXT,
            match_team1 TEXT,
            match_team2 TEXT,
            market TEXT,
            line REAL,
            over_odds REAL,
            under_odds REAL,
            bookmaker TEXT,
            fetched_at TEXT,
            UNIQUE(player, match_team1, match_team2, market, line, bookmaker)
        );
