from __future__ import annotations

import json

import pytest

from partsouq_crawler.nhtsa.api import (
    NhtsaApiParser,
    NhtsaApiPolicy,
    NhtsaApiPolicyError,
    normalize_vin,
    vin_source_key,
)
from partsouq_crawler.nhtsa.api_service import (
    classify_undecodable_vin_payload,
    undecodable_outcome_note,
)
from partsouq_crawler.nhtsa.datasets import ApiSource
from partsouq_crawler.nhtsa.models import ParsedRecord

VIN = "ZZZTEST00X0000001"


def _decode_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "VIN": VIN,
        "Make": "TEST MAKE",
        "Model": "TEST MODEL",
        "ModelYear": "2020",
        "ErrorCode": "0",
        "ErrorText": "",
    }
    payload.update(overrides)
    return payload


def test_classify_undecodable_returns_none_when_core_fields_present() -> None:
    assert classify_undecodable_vin_payload(_decode_payload()) is None
    # ErrorCode 僅供消費端判讀；核心欄位齊全就不是終局無資料。
    assert (
        classify_undecodable_vin_payload(_decode_payload(ErrorCode="1 - Check Digit issue")) is None
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"Make": "", "ErrorCode": "7 - ManufacturerMarkedNotRegistered"}, "nhtsa_unregistered"),
        (
            {"ModelYear": "", "ErrorCode": "7 - ManufacturerMarkedNotRegistered"},
            "nhtsa_unregistered",
        ),
        ({"Make": "", "ErrorCode": "11 - VIN corrected, no information returned"}, "invalid_vin"),
        ({"Make": "", "ErrorCode": "400 - Provided VIN is not valid"}, "invalid_vin"),
        (
            {"ModelYear": "", "ErrorCode": "8 - No detailed data available currently"},
            "no_detail_data",
        ),
        ({"Make": "", "ErrorCode": ""}, "no_detail_data"),
        ({"Make": "", "ErrorCode": "unexpected text"}, "no_detail_data"),
    ),
)
def test_classify_undecodable_maps_error_codes_to_terminal_classes(
    overrides: dict[str, str],
    expected: str,
) -> None:
    assert classify_undecodable_vin_payload(_decode_payload(**overrides)) == expected


def test_undecodable_outcome_note_carries_code_and_suggested_vin() -> None:
    note = undecodable_outcome_note(
        _decode_payload(
            Make="",
            ErrorCode="7 - ManufacturerMarkedNotRegistered",
            SuggestedVIN="TMBJJ7AE0EJ123456",
        ),
        "nhtsa_unregistered",
    )

    assert note.startswith("NHTSA reports no usable decode (nhtsa_unregistered)")
    assert "ErrorCode='7 - ManufacturerMarkedNotRegistered'" in note
    assert "SuggestedVIN='TMBJJ7AE0EJ123456'" in note


def test_api_policy_allows_collections_and_one_full_vin_decode() -> None:
    policy = NhtsaApiPolicy()
    policy.validate("https://vpic.nhtsa.dot.gov/api/vehicles/GetAllMakes?format=json")
    policy.validate(
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetAllManufacturers?format=json&page=2"
    )
    policy.validate("https://api.nhtsa.gov/CSSIStation/state/NV?format=json")
    policy.validate(f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{VIN}?format=json")

    forbidden = (
        "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/123?format=json",
        "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/123?format=json",
        "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/?format=json",
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetAllMakes?format=json&page=2",
        "https://api.nhtsa.gov/recalls/recallsByVehicle?format=json",
        "https://example.com/api/vehicles/GetAllMakes?format=json",
    )
    for url in forbidden:
        with pytest.raises(NhtsaApiPolicyError):
            policy.validate(url)


def test_api_policy_allows_per_make_models_and_rejects_malformed_paths() -> None:
    policy = NhtsaApiPolicy()
    policy.validate("https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/460?format=json")
    policy.validate("https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/0?format=json")
    policy.validate(
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/make/Tesla/modelyear/2021"
        "?format=json"
    )
    policy.validate(
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/make/AM%20General"
        "/modelyear/2011?format=json"
    )

    forbidden = (
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/-1?format=json",
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/abc?format=json",
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/0460?format=json",
        "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/460?format=json&page=2",
        (
            "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/460"
            "/../DecodeVinValues/" + VIN + "?format=json"
        ),
        (
            "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/make/Tesla"
            "/modelyear/20?format=json"
        ),
        (
            "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/make/Tesla"
            "/modelyear/2021/extra?format=json"
        ),
        (
            "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/make/Tesla!"
            "/modelyear/2021?format=json"
        ),
    )
    for url in forbidden:
        with pytest.raises(NhtsaApiPolicyError):
            policy.validate(url)


def test_context_merge_happens_before_required_fields_validation() -> None:
    source = ApiSource(
        key="vpic_variable_5_values",
        dataset_name="vpic_variable_values",
        url=("https://vpic.nhtsa.dot.gov/api/vehicles/GetVehicleVariableValuesList/5?format=json"),
        context=(("Variable_ID", "5"),),
    )
    body = json.dumps(
        {
            "Count": 1,
            "Message": "Results returned successfully",
            "Results": [{"ElementName": "Body Class", "Id": 1, "Name": "Convertible"}],
        }
    ).encode()

    document = NhtsaApiParser().parse(body, source)

    assert document.rejections == ()
    assert len(document.records) == 1
    assert document.records[0].natural_key_text == "5\x1f1"


def test_vpic_models_natural_key_pairs_make_and_model() -> None:
    parser = NhtsaApiParser()

    def record_for_make(make_id: str, make_name: str, model_id: int) -> ParsedRecord:
        source = ApiSource(
            key=f"vpic_models_for_make_{make_id}",
            dataset_name="vpic_models",
            url=(
                f"https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/{make_id}?format=json"
            ),
            context=(("Make_ID", make_id), ("Make_Name", make_name)),
        )
        body = json.dumps(
            {
                "Count": 1,
                "Message": "Results returned successfully",
                "Results": [
                    {
                        "Make_ID": int(make_id),
                        "Make_Name": make_name,
                        "Model_ID": model_id,
                        "Model_Name": "Model 3",
                    }
                ],
            }
        ).encode()
        document = parser.parse(body, source)
        assert document.rejections == ()
        return document.records[0]

    tesla = record_for_make("955", "TESLA", 17102)
    overseas = record_for_make("9995", "TESLA (OVERSEAS)", 17102)

    assert tesla.external_id == "17102"
    assert tesla.make_name == "TESLA"
    assert tesla.model_name == "Model 3"
    assert tesla.natural_key_text == "955\x1f17102"
    assert overseas.natural_key_text == "9995\x1f17102"
    assert tesla.natural_key_sha256 != overseas.natural_key_sha256


def test_vpic_model_years_injects_year_from_context_into_payload() -> None:
    source = ApiSource(
        key="vpic_model_years_tesla_2021",
        dataset_name="vpic_model_years",
        url=(
            "https://vpic.nhtsa.dot.gov/api/vehicles/"
            "GetModelsForMakeYear/make/Tesla/modelyear/2021?format=json"
        ),
        context=(("Model_Year", "2021"),),
    )
    body = json.dumps(
        {
            "Count": 1,
            "Message": "Results returned successfully",
            "Results": [
                {"Make_ID": 955, "Make_Name": "TESLA", "Model_ID": 17102, "Model_Name": "Model 3"}
            ],
        }
    ).encode()

    document = NhtsaApiParser().parse(body, source)

    assert document.rejections == ()
    record = document.records[0]
    assert record.external_id == "17102"
    assert record.make_name == "TESLA"
    assert record.model_name == "Model 3"
    assert json.loads(record.payload_json)["Model_Year"] == "2021"


def test_normalize_vin_requires_full_valid_alphabet() -> None:
    assert normalize_vin(f" {VIN.lower()} ") == VIN
    for invalid in (VIN[:-1], f"{VIN[:-1]}I", f"{VIN[:-1]}Q"):
        with pytest.raises(ValueError, match="VIN 必須是 17 碼"):
            normalize_vin(invalid)


def test_vin_source_key_is_the_hash_of_the_normalized_vin() -> None:
    assert vin_source_key(f" {VIN.lower()} ") == (
        "vpic_vin_sha256_ede95f1201f438e841f7bab89e079c35bdda827ea9d32a4dec959442c08c9a7b"
    )


def test_api_parser_normalizes_vin_vehicle_fields() -> None:
    source = ApiSource(
        key=f"vpic_vin_{VIN}",
        dataset_name="vpic_vin_decodes",
        url=(f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{VIN}?format=json"),
    )
    body = json.dumps(
        {
            "Count": 1,
            "Message": "Results returned successfully",
            "Results": [
                {
                    "VIN": VIN,
                    "Make": "HYUNDAI",
                    "Model": "Kona",
                    "ModelYear": "2018",
                    "EngineModel": "MPI Nu",
                    "DisplacementL": "2.0",
                    "Trim": "SEL with Tech Pkg",
                    "ErrorCode": "0",
                }
            ],
        }
    ).encode()

    record = NhtsaApiParser().parse(body, source).records[0]
    assert record.external_id == VIN
    assert record.make_name == "HYUNDAI"
    assert record.model_name == "Kona"
    assert record.model_year == 2018


def test_api_parser_preserves_nested_payload_and_provenance() -> None:
    source = ApiSource(
        key="vpic_manufacturers_page_001",
        dataset_name="vpic_manufacturers",
        url=("https://vpic.nhtsa.dot.gov/api/vehicles/GetAllManufacturers?format=json&page=1"),
    )
    body = json.dumps(
        {
            "Count": 1,
            "Message": "Response returned successfully",
            "Results": [
                {
                    "Country": "UNITED STATES (USA)",
                    "Mfr_CommonName": "Tesla",
                    "Mfr_ID": 955,
                    "Mfr_Name": "TESLA, INC.",
                    "VehicleTypes": [{"IsPrimary": True, "Name": "Passenger Car"}],
                }
            ],
        }
    ).encode()

    document = NhtsaApiParser().parse(body, source)
    assert document.count == 1
    assert document.rejections == ()
    record = document.records[0]
    assert record.external_id == "955"
    assert record.make_name == "Tesla"
    assert record.source_line == 1
    assert json.loads(record.payload_json)["VehicleTypes"][0]["IsPrimary"] is True
    assert "VehicleTypes" in document.member.field_names


def test_variable_value_context_is_part_of_natural_key() -> None:
    source = ApiSource(
        key="vpic_variable_5_values",
        dataset_name="vpic_variable_values",
        url=("https://vpic.nhtsa.dot.gov/api/vehicles/GetVehicleVariableValuesList/5?format=json"),
        context=(("Variable_ID", "5"),),
    )
    body = json.dumps(
        {
            "Count": 1,
            "Message": "Results returned successfully",
            "Results": [{"ElementName": "Body Class", "Id": 1, "Name": "Convertible"}],
        }
    ).encode()

    record = NhtsaApiParser().parse(body, source).records[0]
    assert record.natural_key_text == "5\x1f1"
    assert json.loads(record.payload_json)["Variable_ID"] == "5"


def test_cssi_identity_preserves_same_station_rows_with_distinct_email() -> None:
    source = ApiSource(
        key="cssi_state_co",
        dataset_name="cssi_stations",
        url="https://api.nhtsa.gov/CSSIStation/state/CO?format=json",
    )
    common = {
        "Organization": "Colorado State University PD",
        "AddressLine1": "750 Meridian Ave",
        "City": "Fort Collins",
        "State": "CO",
        "Zip": "80523",
        "Phone1": "970-657-4823",
        "ContactFirstName": "Ashleigh",
        "ContactLastName": "Rose",
        "LocationLatitude": 40.569126,
        "LocationLongitude": -105.079308,
    }
    body = json.dumps(
        {
            "Count": 2,
            "Message": "Results returned successfully",
            "Results": [
                {**common, "Email": None},
                {**common, "Email": "ashleigh.rose@colostate.edu"},
            ],
        }
    ).encode()

    records = NhtsaApiParser().parse(body, source).records

    assert len(records) == 2
    assert records[0].natural_key_sha256 != records[1].natural_key_sha256
