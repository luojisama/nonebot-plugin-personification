FORMAT = "personification-data-package"
VERSION = 3
SUPPORTED_VERSIONS = frozenset({1, 2, 3})

V1_DATASETS = (
    "group_messages",
    "session_messages",
    "conversation_threads",
    "group_relation_edges",
    "group_style_snapshots",
    "group_state",
    "local_user_profiles",
    "group_memories",
)

V2_DATASETS = (
    *V1_DATASETS,
    "avatar_relation_evidence",
)

DATASETS = (
    *V2_DATASETS,
    "meme_dictionary",
)

DEFAULT_DATASETS = (
    "conversation_threads",
    "group_relation_edges",
    "group_style_snapshots",
    "group_state",
    "local_user_profiles",
    "group_memories",
    "avatar_relation_evidence",
    "meme_dictionary",
)

TABLE_FIELDS = {
    "group_messages": ("id", "group_id", "user_id", "nickname", "content", "image_count", "visual_summary", "is_bot", "reply_to_msg_id", "reply_to_user_id", "mentioned_ids", "is_at_bot", "message_id", "thread_id", "source_kind", "sender_role", "timestamp"),
    "session_messages": ("id", "session_id", "role", "content", "is_summary", "timestamp", "metadata"),
    "conversation_threads": ("thread_id", "group_id", "topic_summary", "participants", "created_at", "last_active_at"),
    "group_relation_edges": ("group_id", "src_user_id", "dst_user_id", "edge_kind", "weight", "last_seen_at", "sample_msg_id"),
    "group_style_snapshots": ("id", "group_id", "style_text", "style_json", "created_at"),
    # Avatar hashes bind evidence to source-side assets and therefore never cross
    # a transfer boundary. Imported visual priors remain short-lived and can be
    # refreshed locally from current avatars.
    "avatar_relation_evidence": (
        "group_id", "left_user_id", "right_user_id", "relation", "confidence",
        "evidence_tags", "asset_kinds", "schema_version", "observed_at", "expires_at",
    ),
}

PRIMARY_KEYS = {
    "group_messages": ("id",),
    "session_messages": ("id",),
    "conversation_threads": ("thread_id",),
    "group_relation_edges": ("group_id", "src_user_id", "dst_user_id", "edge_kind"),
    "group_style_snapshots": ("id",),
    "avatar_relation_evidence": ("group_id", "left_user_id", "right_user_id"),
}

GROUP_CONFIG_FIELDS = frozenset({
    "enabled", "sticker_enabled", "tts_enabled", "schedule_enabled",
    "schedule_prompt", "custom_prompt", "allow_group_admin_config",
})

GROUP_KV_NAMESPACES = frozenset({
    "group_member_aliases", "group_mute_state", "group_style_last_run",
    "group_style_daily_count", "group_knowledge_last_run",
    "group_knowledge_daily_count",
})

EXCLUDED_CATEGORIES = (
    "credentials", "auth", "log", "audit", "trace", "token",
    "provider_health", "tasks", "proactive", "qzone", "user_policy",
)

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 32
MAX_COMPRESSION_RATIO = 100

PLAN_TOKEN_TTL_SECONDS = 15 * 60
ARTIFACT_RETENTION_SECONDS = 7 * 24 * 60 * 60
BACKUP_RETENTION_SECONDS = 30 * 24 * 60 * 60

# Only stable, scope-local memory fields cross the package boundary. Derived
# indexes and fields that can point at another memory are rebuilt locally.
MEMORY_PAYLOAD_FIELDS = frozenset({
    "memory_id", "memory_type", "palace_zone", "summary", "aliases",
    "topic_tags", "entity_tags", "snippets", "user_id", "group_id",
    "thread_id", "time_created", "last_accessed_at", "access_count",
    "confidence", "salience", "stability", "emotional_weight",
    "privacy_level", "permission_type", "expires_at", "supports_recall",
    "supports_autofill", "revision", "tone_risk", "irony_risk",
    "time_sensitivity", "reinforcement_count", "tier",
})

# Logical slang data is transferable, but platform author fingerprints and all
# browser/login state deliberately have no field in this package schema.
MEME_DICTIONARY_ROOT_FIELDS = (
    "term", "aliases", "meaning", "tone", "risk_level", "examples",
    "scope", "group_id", "confidence", "evidence_message_ids", "safe_usage",
    "managed_by", "updated_at",
)

MEME_SENSE_FIELDS = (
    "sense_id", "scope", "group_id", "term", "meaning", "aliases_json",
    "game_context_json", "version_context", "usage_context", "safe_usage",
    "risk_level", "status", "confidence", "source_count", "platform_count",
    "auto_managed", "first_seen_at", "last_verified_at", "reverify_after",
    "expires_at", "manual_locked", "revision", "updated_at",
)

MEME_EVIDENCE_FIELDS = (
    "claim_id", "sense_id", "packet_id", "platform", "content_id",
    "canonical_url", "content_type", "discussion_id", "evidence_type", "quote",
    "content_fingerprint", "media_fingerprint", "source_cluster_id",
    "extractor_version", "model_route", "confidence", "source_quality",
    "published_at", "retrieved_at", "created_at",
)

MEME_EVENT_FIELDS = (
    "event_id", "sense_id", "event_type", "old_status", "new_status", "actor",
    "detail_json", "created_at",
)
