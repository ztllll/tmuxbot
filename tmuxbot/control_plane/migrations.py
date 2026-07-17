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
    (
        3,
        """
        CREATE TABLE team_runs (
            run_id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('draft', 'running', 'paused', 'completed', 'failed',
                          'operator_required', 'stopped')
            ),
            max_retries INTEGER NOT NULL CHECK (max_retries >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE team_agents (
            agent_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES team_runs(run_id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('coordinator', 'implementer', 'reviewer')),
            managed_session_id TEXT NOT NULL,
            UNIQUE(run_id, role),
            UNIQUE(run_id, managed_session_id)
        );
        CREATE TABLE team_tasks (
            task_id TEXT NOT NULL,
            run_id TEXT NOT NULL REFERENCES team_runs(run_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            goal TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('coordinator', 'implementer', 'reviewer')),
            state TEXT NOT NULL CHECK (
                state IN ('pending', 'ready', 'assigned', 'working', 'review',
                          'accepted', 'blocked', 'failed', 'retrying', 'operator_required')
            ),
            dependencies_json TEXT NOT NULL,
            requires_write INTEGER NOT NULL CHECK (requires_write IN (0, 1)),
            max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
            attempt INTEGER NOT NULL CHECK (attempt >= 0),
            assignee_agent_id TEXT REFERENCES team_agents(agent_id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(run_id, task_id)
        );
        CREATE INDEX team_tasks_run_state_idx ON team_tasks(run_id, state);
        CREATE TABLE mailbox_messages (
            message_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES team_runs(run_id) ON DELETE CASCADE,
            task_id TEXT,
            sender_agent_id TEXT REFERENCES team_agents(agent_id),
            recipient_agent_id TEXT REFERENCES team_agents(agent_id),
            kind TEXT NOT NULL,
            body_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            UNIQUE(run_id, idempotency_key),
            FOREIGN KEY(run_id, task_id) REFERENCES team_tasks(run_id, task_id)
                ON DELETE CASCADE
        );
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES team_runs(run_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            producer_agent_id TEXT NOT NULL REFERENCES team_agents(agent_id),
            kind TEXT NOT NULL,
            uri TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, idempotency_key),
            FOREIGN KEY(run_id, task_id) REFERENCES team_tasks(run_id, task_id)
                ON DELETE CASCADE
        );
        CREATE TABLE write_leases (
            lease_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES team_runs(run_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            released_at TEXT,
            FOREIGN KEY(run_id, task_id) REFERENCES team_tasks(run_id, task_id)
                ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX write_leases_one_active_per_run
            ON write_leases(run_id) WHERE released_at IS NULL;
        """,
    ),
    (
        4,
        """
        CREATE TABLE dispatch_commands (
            command_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES team_runs(run_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK(attempt >= 1),
            managed_session_id TEXT NOT NULL,
            envelope_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending', 'tmux_written', 'uncertain')),
            created_at TEXT NOT NULL,
            tmux_written_at TEXT,
            last_error TEXT,
            UNIQUE(run_id, task_id, attempt),
            FOREIGN KEY(run_id, task_id) REFERENCES team_tasks(run_id, task_id)
                ON DELETE CASCADE
        );
        CREATE INDEX dispatch_commands_pending_idx
            ON dispatch_commands(run_id, state, created_at);
        """,
    ),
)
