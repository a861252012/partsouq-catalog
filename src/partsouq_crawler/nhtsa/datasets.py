from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    name: str
    delimiter: str
    has_header: bool
    field_names: tuple[str, ...]
    required_fields: tuple[str, ...]
    identity_fields: tuple[str, ...]
    external_id_field: str | None = None
    make_field: str | None = None
    model_field: str | None = None
    model_year_field: str | None = None
    campaign_field: str | None = None
    component_field: str | None = None
    summary_field: str | None = None
    encoding: str = "cp1252"


@dataclass(frozen=True, slots=True)
class BulkSource:
    key: str
    dataset_name: str
    url: str
    expected_member: str
    is_zip: bool = True


@dataclass(frozen=True, slots=True)
class ApiSource:
    key: str
    dataset_name: str
    url: str
    context: tuple[tuple[str, str], ...] = ()


RECALL_FIELDS = (
    "RECORD_ID",
    "CAMPNO",
    "MAKETXT",
    "MODELTXT",
    "YEARTXT",
    "MFGCAMPNO",
    "COMPNAME",
    "MFGNAME",
    "BGMAN",
    "ENDMAN",
    "RCLTYPECD",
    "POTAFF",
    "ODATE",
    "INFLUENCED_BY",
    "MFGTXT",
    "RCDATE",
    "DATEA",
    "RPNO",
    "FMVSS",
    "DESC_DEFECT",
    "CONEQUENCE_DEFECT",
    "CORRECTIVE_ACTION",
    "NOTES",
    "RCL_CMPT_ID",
    "MFR_COMP_NAME",
    "MFR_COMP_DESC",
    "MFR_COMP_PTNO",
    "DO_NOT_DRIVE",
    "PARK_OUTSIDE",
)

INVESTIGATION_FIELDS = (
    "NHTSA_ACTION_NUMBER",
    "MAKE",
    "MODEL",
    "YEAR",
    "COMPNAME",
    "MFR_NAME",
    "ODATE",
    "CDATE",
    "CAMPNO",
    "SUBJECT",
    "SUMMARY",
)

COMPLAINT_FIELDS = (
    "CMPLID",
    "ODINO",
    "MFR_NAME",
    "MAKETXT",
    "MODELTXT",
    "YEARTXT",
    "CRASH",
    "FAILDATE",
    "FIRE",
    "INJURED",
    "DEATHS",
    "COMPDESC",
    "CITY",
    "STATE",
    "VIN",
    "DATEA",
    "LDATE",
    "MILES",
    "OCCURENCES",
    "CDESCR",
    "CMPL_TYPE",
    "POLICE_RPT_YN",
    "PURCH_DT",
    "ORIG_OWNER_YN",
    "ANTI_BRAKES_YN",
    "CRUISE_CONT_YN",
    "NUM_CYLS",
    "DRIVE_TRAIN",
    "FUEL_SYS",
    "FUEL_TYPE",
    "TRANS_TYPE",
    "VEH_SPEED",
    "DOT",
    "TIRE_SIZE",
    "LOC_OF_TIRE",
    "TIRE_FAIL_TYPE",
    "ORIG_EQUIP_YN",
    "MANUF_DT",
    "SEAT_TYPE",
    "RESTRAINT_TYPE",
    "DEALER_NAME",
    "DEALER_TEL",
    "DEALER_CITY",
    "DEALER_STATE",
    "DEALER_ZIP",
    "PROD_TYPE",
    "REPAIRED_YN",
    "MEDICAL_ATTN",
    "VEHICLES_TOWED_YN",
    "STATE_OF_INCIDENT",
    "VEHICLE_OPERATOR",
)

MANUFACTURER_COMMUNICATION_FIELDS = (
    "NHTSA_ID_NUMBER",
    "REPLACEMENT_SERVICE_BULLETIN_NUMBER",
    "DATE_ADDED_TO_FILE",
    "TSB_DOCUMENT_ID",
    "MFR_COMMUNICATION_DATE",
    "MFR_INTERNAL_CAMPAIGN_ID_SOFTWARE_VERSION",
    "COMMUNICATION_TYPE",
    "MAKE",
    "MODEL",
    "MODEL_YEAR",
    "NHTSA_COMPONENTS",
    "MFR_COMPONENT_SYSTEM",
    "MFR_COMPONENT_SUBSYSTEM",
    "SUMMARY",
)

DATASET_SPECS = {
    "safety_ratings": DatasetSpec(
        name="safety_ratings",
        delimiter=",",
        has_header=True,
        field_names=(),
        required_fields=("MAKE", "MODEL", "MODEL_YR"),
        identity_fields=(
            "MAKE",
            "MODEL",
            "MODEL_YR",
            "BODY_STYLE",
            "VEHICLE_TYPE",
            "DRIVE_TRAIN",
            "PRODUCTION_RELEASE",
            "FRNT_TEST_NO",
            "SIDE_TEST_NO",
            "POLE_TEST_NO",
        ),
        make_field="MAKE",
        model_field="MODEL",
        model_year_field="MODEL_YR",
        summary_field="ROLL_SAFETY_CONCERN",
        encoding="utf-8-sig",
    ),
    "recalls": DatasetSpec(
        name="recalls",
        delimiter="\t",
        has_header=False,
        field_names=RECALL_FIELDS,
        required_fields=("RECORD_ID", "CAMPNO"),
        identity_fields=("RECORD_ID",),
        external_id_field="RECORD_ID",
        make_field="MAKETXT",
        model_field="MODELTXT",
        model_year_field="YEARTXT",
        campaign_field="CAMPNO",
        component_field="COMPNAME",
        summary_field="DESC_DEFECT",
    ),
    "investigations": DatasetSpec(
        name="investigations",
        delimiter="\t",
        has_header=False,
        field_names=INVESTIGATION_FIELDS,
        required_fields=("NHTSA_ACTION_NUMBER",),
        identity_fields=("NHTSA_ACTION_NUMBER", "MAKE", "MODEL", "YEAR", "COMPNAME"),
        external_id_field="NHTSA_ACTION_NUMBER",
        make_field="MAKE",
        model_field="MODEL",
        model_year_field="YEAR",
        campaign_field="CAMPNO",
        component_field="COMPNAME",
        summary_field="SUMMARY",
    ),
    "complaints": DatasetSpec(
        name="complaints",
        delimiter="\t",
        has_header=False,
        field_names=COMPLAINT_FIELDS,
        required_fields=("CMPLID", "ODINO"),
        identity_fields=("CMPLID",),
        external_id_field="CMPLID",
        make_field="MAKETXT",
        model_field="MODELTXT",
        model_year_field="YEARTXT",
        component_field="COMPDESC",
        summary_field="CDESCR",
    ),
    "manufacturer_communications_summary": DatasetSpec(
        name="manufacturer_communications_summary",
        delimiter=",",
        has_header=True,
        field_names=(),
        required_fields=("TSB_DOCUMENT_ID", "MAKE", "MODEL", "MODEL_YEAR"),
        identity_fields=("TSB_DOCUMENT_ID", "MAKE", "MODEL", "MODEL_YEAR"),
        external_id_field="TSB_DOCUMENT_ID",
        make_field="MAKE",
        model_field="MODEL",
        model_year_field="MODEL_YEAR",
        summary_field="CONCISE_SUMMARY",
        encoding="utf-8-sig",
    ),
    "manufacturer_communications": DatasetSpec(
        name="manufacturer_communications",
        delimiter="\t",
        has_header=False,
        field_names=MANUFACTURER_COMMUNICATION_FIELDS,
        required_fields=("NHTSA_ID_NUMBER", "TSB_DOCUMENT_ID"),
        identity_fields=MANUFACTURER_COMMUNICATION_FIELDS[:-1],
        external_id_field="NHTSA_ID_NUMBER",
        make_field="MAKE",
        model_field="MODEL",
        model_year_field="MODEL_YEAR",
        component_field="NHTSA_COMPONENTS",
        summary_field="SUMMARY",
    ),
    "vpic_makes": DatasetSpec(
        name="vpic_makes",
        delimiter=",",
        has_header=False,
        field_names=(),
        required_fields=("Make_ID", "Make_Name"),
        identity_fields=("Make_ID",),
        external_id_field="Make_ID",
        make_field="Make_Name",
    ),
    "vpic_models": DatasetSpec(
        name="vpic_models",
        delimiter=",",
        has_header=False,
        field_names=(),
        required_fields=("Make_ID", "Make_Name", "Model_ID", "Model_Name"),
        identity_fields=("Make_ID", "Model_ID"),
        external_id_field="Model_ID",
        make_field="Make_Name",
        model_field="Model_Name",
    ),
    # GetModelsForMakeYear 回應僅 4 欄、不含 Model_Year；年份由 ApiSource.context
    # 注入 payload（見 api.py parse() 的 payload.update(dict(source.context))）。
    "vpic_model_years": DatasetSpec(
        name="vpic_model_years",
        delimiter=",",
        has_header=False,
        field_names=(),
        required_fields=("Make_ID", "Make_Name", "Model_ID", "Model_Name"),
        identity_fields=("Model_ID",),
        external_id_field="Model_ID",
        make_field="Make_Name",
        model_field="Model_Name",
    ),
    "vpic_manufacturers": DatasetSpec(
        name="vpic_manufacturers",
        delimiter=",",
        has_header=False,
        field_names=(),
        required_fields=("Mfr_ID", "Mfr_Name"),
        identity_fields=("Mfr_ID",),
        external_id_field="Mfr_ID",
        make_field="Mfr_CommonName",
        summary_field="Mfr_Name",
    ),
    "vpic_variables": DatasetSpec(
        name="vpic_variables",
        delimiter=",",
        has_header=False,
        field_names=(),
        required_fields=("ID", "Name"),
        identity_fields=("ID",),
        external_id_field="ID",
        summary_field="Description",
    ),
    "vpic_variable_values": DatasetSpec(
        name="vpic_variable_values",
        delimiter=",",
        has_header=False,
        field_names=(),
        required_fields=("Variable_ID", "Id", "Name"),
        identity_fields=("Variable_ID", "Id"),
        external_id_field="Id",
        component_field="ElementName",
        summary_field="Name",
    ),
    "vpic_vin_decodes": DatasetSpec(
        name="vpic_vin_decodes",
        delimiter=",",
        has_header=False,
        field_names=(),
        # 與互動解碼契約一致：vPIC 對部分車型不回 Model，缺席不算錯。
        required_fields=("VIN", "Make", "ModelYear", "ErrorCode"),
        identity_fields=("VIN",),
        external_id_field="VIN",
        make_field="Make",
        model_field="Model",
        model_year_field="ModelYear",
        summary_field="ErrorText",
    ),
    "cssi_stations": DatasetSpec(
        name="cssi_stations",
        delimiter=",",
        has_header=False,
        field_names=(),
        required_fields=("Organization", "State", "Zip"),
        identity_fields=(
            "Organization",
            "AddressLine1",
            "City",
            "State",
            "Zip",
            "Phone1",
            "ContactFirstName",
            "ContactLastName",
            "Email",
            "LocationLatitude",
            "LocationLongitude",
        ),
        external_id_field="Phone1",
        summary_field="Organization",
    ),
}

RECALL_SOURCES = (
    BulkSource(
        "recalls_pre_2010",
        "recalls",
        "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_PRE_2010.zip",
        "FLAT_RCL_PRE_2010.txt",
    ),
    BulkSource(
        "recalls_post_2010",
        "recalls",
        "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip",
        "FLAT_RCL_POST_2010.txt",
    ),
)

COMPLAINT_PERIODS = (
    "1995-1999",
    "2000-2004",
    "2005-2009",
    "2010-2014",
    "2015-2019",
    "2020-2024",
    "2025-2026",
)
COMMUNICATION_PERIODS = COMPLAINT_PERIODS

COMPLAINT_SOURCES = tuple(
    BulkSource(
        f"complaints_{period}",
        "complaints",
        f"https://static.nhtsa.gov/odi/ffdd/cmpl/COMPLAINTS_RECEIVED_{period}.zip",
        f"COMPLAINTS_RECEIVED_{period}.txt",
    )
    for period in COMPLAINT_PERIODS
)

COMMUNICATION_SUMMARY_SOURCES = tuple(
    BulkSource(
        f"manufacturer_communications_summary_{period}",
        "manufacturer_communications_summary",
        f"https://static.nhtsa.gov/odi/ffdd/tsbs/MFR_COMMS_RECEIVED_{period}.zip",
        f"MFR_COMMS_RECEIVED_{period}.csv",
    )
    for period in COMMUNICATION_PERIODS
)

COMMUNICATION_SOURCES = tuple(
    BulkSource(
        f"manufacturer_communications_{period}",
        "manufacturer_communications",
        f"https://static.nhtsa.gov/odi/ffdd/tsbs/TSBS_RECEIVED_{period}.zip",
        f"TSBS_RECEIVED_{period}.txt",
    )
    for period in COMMUNICATION_PERIODS
)

SAFETY_RATING_SOURCES = (
    BulkSource(
        "safety_ratings",
        "safety_ratings",
        "https://static.nhtsa.gov/nhtsa/downloads/Safercar/Safercar_data.csv",
        "Safercar_data.csv",
        is_zip=False,
    ),
)

INVESTIGATION_SOURCES = (
    BulkSource(
        "investigations",
        "investigations",
        "https://static.nhtsa.gov/odi/ffdd/inv/FLAT_INV.zip",
        "FLAT_INV.txt",
    ),
)

BULK_SOURCES = (
    *SAFETY_RATING_SOURCES,
    *RECALL_SOURCES,
    *INVESTIGATION_SOURCES,
    *COMPLAINT_SOURCES,
    *COMMUNICATION_SUMMARY_SOURCES,
    *COMMUNICATION_SOURCES,
)

BULK_SOURCES_BY_SCOPE = {
    "all": BULK_SOURCES,
    "safety-ratings": SAFETY_RATING_SOURCES,
    "recalls": RECALL_SOURCES,
    "investigations": INVESTIGATION_SOURCES,
    "complaints": COMPLAINT_SOURCES,
    "manufacturer-communications-summary": COMMUNICATION_SUMMARY_SOURCES,
    "manufacturer-communications": COMMUNICATION_SOURCES,
}

VPIC_FIXED_SOURCES = (
    # vpic_models 不再有寫死 GetModelsForMakeId/0 的固定來源：改由
    # api_service.run() 在取得 GetAllMakes 文件後，對每個 Make_ID 動態建立
    # vpic_models_for_make_<id> 來源展開 GetModelsForMakeId/<id>。
    ApiSource(
        "vpic_all_makes",
        "vpic_makes",
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetAllMakes?format=json",
    ),
    ApiSource(
        "vpic_variables",
        "vpic_variables",
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetVehicleVariableList?format=json",
    ),
)

US_STATE_AND_TERRITORY_CODES = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "AS",
    "GU",
    "MP",
    "PR",
    "VI",
)

CSSI_SOURCES = tuple(
    ApiSource(
        f"cssi_state_{state.lower()}",
        "cssi_stations",
        f"https://api.nhtsa.gov/CSSIStation/state/{state}?format=json",
    )
    for state in US_STATE_AND_TERRITORY_CODES
)
