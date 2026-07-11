MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE web_sessions (
            token_hash TEXT PRIMARY KEY,
            csrf_token TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE run_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        );
        CREATE INDEX run_events_aggregate_idx
            ON run_events(aggregate_type, aggregate_id, sequence);
        """,
    ),
    (
        2,
        """
        CREATE TABLE provider_profiles (
            id TEXT PRIMARY KEY,
            binary_name TEXT NOT NULL CHECK(binary_name IN ('tmux', 'claude', 'codex')),
            executable_path TEXT NOT NULL,
            version TEXT,
            device INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            discovered_at INTEGER NOT NULL,
            UNIQUE(binary_name, executable_path)
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL UNIQUE,
            device INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE managed_sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
            provider_id TEXT NOT NULL REFERENCES provider_profiles(id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            tmux_session TEXT NOT NULL,
            tmux_window INTEGER NOT NULL CHECK(tmux_window >= 0),
            tmux_pane INTEGER NOT NULL CHECK(tmux_pane >= 0),
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(tmux_session, tmux_window, tmux_pane)
        );
        CREATE TABLE probe_results (
            id TEXT PRIMARY KEY,
            provider_id TEXT NOT NULL REFERENCES provider_profiles(id) ON DELETE CASCADE,
            success INTEGER NOT NULL CHECK(success IN (0, 1)),
            version TEXT,
            error_code TEXT,
            exit_code INTEGER,
            duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
            output_truncated INTEGER NOT NULL CHECK(output_truncated IN (0, 1)),
            observed_at INTEGER NOT NULL
        );
        CREATE INDEX probe_results_provider_idx
            ON probe_results(provider_id, observed_at DESC);
        CREATE INDEX managed_sessions_project_idx
            ON managed_sessions(project_id, created_at);
        """,
    ),
)
