-- Editable station backoffice.
--
-- Source records remain in the unified catalog/NHTSA/admin tables. The views
-- below are compatibility adapters for the ten entity types used by the
-- original Flask/Jinja backoffice. Human changes are overlays and append-only
-- events; they never UPDATE or DELETE the crawler source rows.

CREATE TABLE IF NOT EXISTS admin_override_heads (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    entity_type VARCHAR(64) NOT NULL,
    identity_key VARCHAR(96) NOT NULL,
    source_record_id BIGINT UNSIGNED NULL,
    manual_uuid CHAR(36) NULL,
    payload_json JSON NOT NULL,
    status VARCHAR(16) NOT NULL,
    revision INT UNSIGNED NOT NULL,
    base_sha256 CHAR(64) NOT NULL,
    actor VARCHAR(191) NOT NULL,
    reason TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_admin_override_identity (entity_type, identity_key),
    UNIQUE KEY uq_admin_override_source (entity_type, source_record_id),
    UNIQUE KEY uq_admin_override_manual (entity_type, manual_uuid),
    INDEX idx_admin_override_list (entity_type, status, source_record_id, id),
    CONSTRAINT chk_admin_override_entity CHECK (
        entity_type IN (
            'vehicle_configurations', 'taxonomy_nodes', 'diagrams', 'part_numbers',
            'part_occurrences', 'fitments', 'part_term_mappings',
            'vin_vehicle_mappings', 'vin_part_fitments', 'reconciliation_cases'
        )
    ),
    CONSTRAINT chk_admin_override_identity CHECK (
        (source_record_id IS NOT NULL AND manual_uuid IS NULL)
        OR (source_record_id IS NULL AND manual_uuid IS NOT NULL)
    ),
    CONSTRAINT chk_admin_override_status CHECK (status IN ('active', 'retired')),
    CONSTRAINT chk_admin_override_revision CHECK (revision >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_override_events (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    head_id BIGINT UNSIGNED NOT NULL,
    entity_type VARCHAR(64) NOT NULL,
    identity_key VARCHAR(96) NOT NULL,
    source_record_id BIGINT UNSIGNED NULL,
    manual_uuid CHAR(36) NULL,
    action VARCHAR(16) NOT NULL,
    revision INT UNSIGNED NOT NULL,
    base_sha256 CHAR(64) NOT NULL,
    before_json JSON NULL,
    after_json JSON NULL,
    actor VARCHAR(191) NOT NULL,
    reason TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_admin_override_event_revision (head_id, revision),
    INDEX idx_admin_override_event_identity (entity_type, identity_key, revision),
    INDEX idx_admin_override_event_source (entity_type, source_record_id, id),
    CONSTRAINT fk_admin_override_event_head
        FOREIGN KEY (head_id) REFERENCES admin_override_heads(id) ON DELETE RESTRICT,
    CONSTRAINT chk_admin_override_event_action
        CHECK (action IN ('create', 'update', 'retire', 'restore')),
    CONSTRAINT chk_admin_override_event_revision CHECK (revision >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_crawl_request_audits (
    request_id BIGINT UNSIGNED PRIMARY KEY,
    actor VARCHAR(191) NOT NULL,
    reason TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    CONSTRAINT fk_admin_crawl_request_audit
        FOREIGN KEY (request_id) REFERENCES admin_crawl_requests(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE OR REPLACE VIEW station_admin_vehicle_configurations AS
SELECT
    v.id,
    b.id AS catalog_brand_id,
    m.id AS catalog_model_id,
    v.id AS vehicle_configuration_id,
    b.name AS catalog_brand,
    b.name AS brand_raw,
    UPPER(TRIM(b.name)) AS brand_normalized,
    v.name AS name_raw,
    m.name AS model_raw,
    v.description AS description_raw,
    v.options AS options_raw,
    v.prod_period AS prod_period_raw,
    v.production_from,
    v.production_to,
    CASE
        WHEN v.production_from IS NOT NULL OR v.production_to IS NOT NULL THEN 'month'
        ELSE NULL
    END AS production_precision,
    v.model_code AS catalog_code,
    v.vid AS vehicle_external_id,
    JSON_OBJECT(
        'brand_id', b.id,
        'model_id', m.id,
        'identity_hash', v.identity_hash,
        'grade', v.grade,
        'market', v.market,
        'engine', v.engine,
        'transmission', v.transmission,
        'body_style', v.body_style
    ) AS metadata_json,
    v.url AS source_url,
    v.fetched_at AS created_at,
    v.fetched_at AS updated_at
FROM vehicles AS v
JOIN models AS m ON m.id = v.model_id
JOIN brands AS b ON b.id = m.brand_id;

CREATE OR REPLACE VIEW station_admin_taxonomy_nodes AS
SELECT
    CAST(c.id * 2 AS UNSIGNED) AS id,
    c.vehicle_id AS vehicle_configuration_id,
    CAST(NULL AS UNSIGNED) AS parent_id,
    1 AS depth,
    COALESCE(c.cid, '') AS code_raw,
    c.name AS name_raw,
    c.name AS path_raw,
    (
        SELECT MIN(g.url)
        FROM groups_t AS g
        WHERE g.category_id = c.id AND g.url IS NOT NULL
    ) AS source_url
FROM categories AS c
UNION ALL
SELECT
    CAST(g.id * 2 + 1 AS UNSIGNED) AS id,
    c.vehicle_id AS vehicle_configuration_id,
    CAST(c.id * 2 AS UNSIGNED) AS parent_id,
    2 AS depth,
    g.code AS code_raw,
    COALESCE(g.name, '') AS name_raw,
    CONCAT(c.name, ' / ', COALESCE(g.name, g.code)) AS path_raw,
    g.url AS source_url
FROM groups_t AS g
JOIN categories AS c ON c.id = g.category_id;

CREATE OR REPLACE VIEW station_admin_diagrams AS
SELECT
    g.id,
    c.vehicle_id AS vehicle_configuration_id,
    CAST(g.id * 2 + 1 AS UNSIGNED) AS taxonomy_node_id,
    g.code AS diagram_code_raw,
    COALESCE(g.name, '') AS diagram_name_raw,
    CAST(NULL AS CHAR(64)) AS diagram_range_raw,
    CAST(NULL AS CHAR(7)) AS diagram_from,
    CAST(NULL AS CHAR(7)) AS diagram_to,
    JSON_OBJECT(
        'category_id', c.id,
        'category_cid', c.cid,
        'group_uid', g.uid
    ) AS metadata_json,
    g.url AS source_url
FROM groups_t AS g
JOIN categories AS c ON c.id = g.category_id;

CREATE OR REPLACE VIEW station_admin_part_numbers AS
SELECT
    p.id,
    m.id AS catalog_model_id,
    v.id AS vehicle_configuration_id,
    p.code AS source_part_code,
    b.name AS part_brand_raw,
    p.part_number AS number_raw,
    UPPER(REGEXP_REPLACE(p.part_number, '[[:space:]-]+', '')) AS number_normalized,
    p.name AS name_en_raw,
    0 AS is_assembly_inferred,
    CAST(NULL AS CHAR(255)) AS assembly_inference_reason,
    p.url AS source_url,
    p.created_at,
    p.updated_at
FROM parts AS p
JOIN groups_t AS g ON g.id = p.group_id
JOIN categories AS c ON c.id = g.category_id
JOIN vehicles AS v ON v.id = c.vehicle_id
JOIN models AS m ON m.id = v.model_id
JOIN brands AS b ON b.id = m.brand_id;

-- Part override projections for API consumers. Dataset-specific queries decide
-- whether the immutable published snapshot or current normalized row is the
-- fallback, so a failed/partial crawl cannot leak into published responses.
CREATE OR REPLACE VIEW station_admin_effective_parts AS
SELECT
    h.source_record_id AS part_id,
    CASE
        WHEN JSON_CONTAINS_PATH(h.payload_json, 'one', '$.number_raw') = 0 THEN NULL
        ELSE JSON_UNQUOTE(JSON_EXTRACT(h.payload_json, '$.number_raw'))
    END AS part_number_override,
    CASE
        WHEN JSON_CONTAINS_PATH(h.payload_json, 'one', '$.number_raw') = 0 THEN NULL
        ELSE UPPER(REGEXP_REPLACE(
            JSON_UNQUOTE(JSON_EXTRACT(h.payload_json, '$.number_raw')),
            '[[:space:]-]+',
            ''
        ))
    END AS number_normalized_override,
    CASE
        WHEN JSON_CONTAINS_PATH(h.payload_json, 'one', '$.name_en_raw') = 0 THEN NULL
        ELSE JSON_UNQUOTE(JSON_EXTRACT(h.payload_json, '$.name_en_raw'))
    END AS part_name_override,
    h.status AS override_status,
    h.revision AS override_revision
FROM admin_override_heads AS h
WHERE h.entity_type = 'part_numbers' AND h.source_record_id IS NOT NULL;

CREATE OR REPLACE VIEW station_admin_part_occurrences AS
SELECT
    p.id,
    p.id AS part_number_id,
    g.id AS diagram_id,
    c.vehicle_id AS vehicle_configuration_id,
    p.code AS callout_raw,
    p.quantity AS quantity_raw,
    p.range_str AS part_range_raw,
    p.part_from,
    p.part_to,
    CAST(NULL AS CHAR(255)) AS part_condition_raw,
    p.note AS note_raw,
    JSON_OBJECT(
        'category_id', c.id,
        'category_cid', c.cid,
        'group_code', g.code,
        'group_uid', g.uid
    ) AS row_metadata_json,
    COALESCE(p.url, g.url) AS source_url
FROM parts AS p
JOIN groups_t AS g ON g.id = p.group_id
JOIN categories AS c ON c.id = g.category_id;

CREATE OR REPLACE VIEW station_admin_fitments AS
SELECT
    source.id,
    source.part_occurrence_id,
    source.part_number_id,
    source.vehicle_configuration_id,
    source.diagram_id,
    CASE
        WHEN source.effective_from IS NOT NULL
         AND source.effective_to IS NOT NULL
         AND source.effective_from > source.effective_to THEN 0
        WHEN published.part_id IS NOT NULL THEN 1
        ELSE 0
    END AS is_verified,
    CASE
        WHEN source.effective_from IS NOT NULL
         AND source.effective_to IS NOT NULL
         AND source.effective_from > source.effective_to
            THEN 'invalid_date_intersection'
        WHEN published.part_id IS NOT NULL THEN 'partsouq_published_snapshot'
        ELSE 'partsouq_normalized_unpublished'
    END AS derivation,
    CASE
        WHEN source.effective_from IS NOT NULL
         AND source.effective_to IS NOT NULL
         AND source.effective_from > source.effective_to THEN CAST(0.0 AS DECIMAL(4, 3))
        WHEN published.part_id IS NOT NULL THEN CAST(1.0 AS DECIMAL(4, 3))
        ELSE CAST(0.5 AS DECIMAL(4, 3))
    END AS confidence,
    source.effective_from,
    source.effective_to,
    source.source_url
FROM (
    SELECT
        p.id,
        p.id AS part_occurrence_id,
        p.id AS part_number_id,
        c.vehicle_id AS vehicle_configuration_id,
        g.id AS diagram_id,
        CASE
            WHEN p.part_from IS NULL THEN v.production_from
            WHEN v.production_from IS NULL THEN p.part_from
            ELSE GREATEST(p.part_from, v.production_from)
        END AS effective_from,
        CASE
            WHEN p.part_to IS NULL THEN v.production_to
            WHEN v.production_to IS NULL THEN p.part_to
            ELSE LEAST(p.part_to, v.production_to)
        END AS effective_to,
        COALESCE(p.url, g.url, v.url) AS source_url
    FROM parts AS p
    JOIN groups_t AS g ON g.id = p.group_id
    JOIN categories AS c ON c.id = g.category_id
    JOIN vehicles AS v ON v.id = c.vehicle_id
) AS source
LEFT JOIN published_parts AS published ON published.part_id = source.id;

CREATE OR REPLACE VIEW station_admin_part_term_mappings AS
SELECT
    t.id,
    (
        SELECT MIN(p.id)
        FROM parts AS p
        WHERE p.name = t.english_name
    ) AS part_number_id,
    t.english_name AS name_en_raw,
    UPPER(TRIM(t.english_name)) AS name_en_normalized,
    t.chinese_name AS name_zh_tw,
    CASE
        WHEN NULLIF(TRIM(t.common_chinese_name), '') IS NULL THEN JSON_ARRAY()
        ELSE JSON_ARRAY(t.common_chinese_name)
    END AS common_names_zh_tw,
    'confirmed' AS mapping_status,
    t.source_name AS source_kind,
    CAST(1.0 AS DECIMAL(4, 3)) AS confidence,
    t.source_reference AS source_url,
    t.updated_at AS observed_at,
    t.created_at,
    t.updated_at
FROM admin_part_translations AS t;

CREATE OR REPLACE VIEW station_admin_vin_vehicle_mappings AS
SELECT
    CAST(CONV(SUBSTRING(SHA2(d.vin, 256), 1, 15), 16, 10) AS UNSIGNED) AS id,
    d.vin,
    d.make_name,
    d.model_name,
    d.series_name,
    JSON_UNQUOTE(JSON_EXTRACT(d.payload_json, '$.BodyClass')) AS body_class,
    JSON_UNQUOTE(JSON_EXTRACT(d.payload_json, '$.VehicleType')) AS vehicle_type,
    d.model_year,
    JSON_UNQUOTE(JSON_EXTRACT(d.payload_json, '$.Manufacturer')) AS manufacturer_name,
    d.trim_name,
    d.engine_configuration,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(d.payload_json, '$.EngineCylinders')) AS UNSIGNED)
        AS engine_cylinders,
    CAST(d.displacement_l AS CHAR) AS displacement_l_raw,
    d.engine_model,
    JSON_UNQUOTE(JSON_EXTRACT(d.payload_json, '$.EngineManufacturer'))
        AS engine_manufacturer,
    JSON_UNQUOTE(JSON_EXTRACT(d.payload_json, '$.FuelTypePrimary')) AS fuel_type_primary,
    JSON_UNQUOTE(JSON_EXTRACT(d.payload_json, '$.DriveType')) AS drive_type,
    JSON_UNQUOTE(JSON_EXTRACT(d.payload_json, '$.TransmissionStyle'))
        AS transmission_style,
    JSON_UNQUOTE(JSON_EXTRACT(d.payload_json, '$.PlantCountry')) AS plant_country,
    m.partsouq_vehicle_id AS partsouq_vehicle_configuration_id,
    CASE WHEN d.error_code = '0' THEN 'decoded' ELSE 'error' END AS decode_status,
    d.error_code,
    d.error_text,
    'nhtsa_vpic' AS source_kind,
    d.source_artifact_id AS response_id,
    d.decoded_at,
    d.decoded_at AS created_at,
    COALESCE(m.updated_at, d.decoded_at) AS updated_at
FROM nhtsa_vin_decodes AS d
LEFT JOIN admin_vehicle_mappings AS m ON m.vin = d.vin;

CREATE OR REPLACE VIEW station_admin_vin_part_fitments AS
SELECT
    CAST(f.mapping_id * 4294967296 + f.part_id AS UNSIGNED) AS id,
    f.mapping_id AS vin_vehicle_mapping_id,
    f.part_id AS part_number_id,
    f.vehicle_id AS vehicle_configuration_id,
    1 AS is_verified,
    f.fitment_status AS derivation,
    CAST(1.0 AS DECIMAL(4, 3)) AS confidence,
    f.source_url,
    m.updated_at AS observed_at,
    m.created_at,
    m.updated_at
FROM v_vin_part_fitments AS f
JOIN admin_vehicle_mappings AS m ON m.id = f.mapping_id;

CREATE OR REPLACE VIEW station_admin_reconciliation_cases AS
SELECT
    r.id,
    r.channel AS case_type,
    'mapping' AS subject_type,
    r.subject_key,
    'normal' AS severity,
    r.status,
    r.left_value AS current_json,
    r.right_value AS candidate_json,
    JSON_OBJECT('channel', r.channel) AS evidence_json,
    CASE
        WHEN NULLIF(TRIM(r.resolution_note), '') IS NULL THEN JSON_ARRAY()
        ELSE JSON_ARRAY(r.resolution_note)
    END AS comments_json,
    CAST(NULL AS CHAR(191)) AS assigned_to,
    r.resolution_note AS resolution,
    CAST(NULL AS CHAR(191)) AS source_run_key,
    r.created_at AS opened_at,
    r.updated_at,
    r.resolved_at
FROM admin_reconciliation_items AS r;
