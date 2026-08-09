-- Journey Onward PoC schema for SQLite. Applied by scripts/init_db.py as a one-off step, never from application code.
-- Provenance model: raw source values, machine proposals and reviewer decisions stay separate records.
-- Column types stay close to the PostgreSQL target so the same shape can be promoted later.

create table datasets (
    id                  text primary key,
    dataset_version     text not null,
    source_root         text not null,
    manifest_sha256     text not null,
    created_utc         text not null
) strict;

create table media_assets (
    id                  integer primary key autoincrement,
    dataset_id          text not null references datasets (id),
    relative_path       text not null,
    media_type          text not null,
    size_bytes          integer not null,
    sha256              text not null,
    modified_utc        text,
    unique (dataset_id, relative_path)
) strict;

create index media_assets_sha256_idx on media_assets (sha256);

create table processing_runs (
    id                  integer primary key autoincrement,
    dataset_id          text not null references datasets (id),
    dataset_version     text not null,
    schema_version      text not null,
    prompt_version      text not null,
    model_id            text,
    status              text not null check (status in ('running', 'complete', 'failed')),
    started_utc         text not null,
    finished_utc        text,
    failure_detail      text
) strict;

create table metadata_observations (
    id                  integer primary key autoincrement,
    media_asset_id      integer not null references media_assets (id),
    processing_run_id   integer not null references processing_runs (id),
    category            text not null check (category in ('photo', 'fetched', 'detected', 'computed')),
    field               text not null,
    value               text,
    raw_value           text,
    source              text not null,
    method_version      text not null,
    confidence          real,
    evidence            text,
    unknown_reason      text,
    recorded_utc        text not null
) strict;

create index metadata_observations_asset_field_idx on metadata_observations (media_asset_id, field);

create table model_calls (
    id                  integer primary key autoincrement,
    processing_run_id   integer not null references processing_runs (id),
    media_asset_id      integer references media_assets (id),
    provider            text not null,
    model_id            text not null,
    prompt_version      text not null,
    schema_version      text not null,
    attempt             integer not null,
    validation_status   text not null check (validation_status in ('valid', 'invalid', 'error')),
    request_bytes       integer,
    latency_ms          integer,
    prompt_tokens       integer,
    completion_tokens   integer,
    cost_usd            real,
    raw_response        text,
    parsed_output       text,
    failure_detail      text,
    called_utc          text not null
) strict;

create table group_proposals (
    id                  integer primary key autoincrement,
    processing_run_id   integer not null references processing_runs (id),
    dataset_id          text not null references datasets (id),
    label               text,
    start_utc           text,
    end_utc             text,
    place_label         text,
    score               real,
    method_version      text not null,
    evidence            text
) strict;

create table group_members (
    group_proposal_id   integer not null references group_proposals (id),
    media_asset_id      integer not null references media_assets (id),
    membership          text not null check (membership in ('member', 'representative', 'duplicate', 'burst')),
    evidence            text,
    primary key (group_proposal_id, media_asset_id)
) strict;

create table review_decisions (
    id                  integer primary key autoincrement,
    group_proposal_id   integer not null references group_proposals (id),
    media_asset_id      integer references media_assets (id),
    decision            text not null check (decision in ('accept', 'split', 'merge', 'reject', 'correct')),
    reviewer            text not null,
    notes               text,
    payload             text,
    decided_utc         text not null
) strict;
