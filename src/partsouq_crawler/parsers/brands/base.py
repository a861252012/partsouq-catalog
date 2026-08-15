from __future__ import annotations

from partsouq_crawler.models.records import VehicleRecord
from partsouq_crawler.parsers.common import parse_unambiguous_range


class BaseBrandAdapter:
    name = "generic"
    aliases: dict[str, tuple[str, ...]] = {
        "brand": ("brand", "make"),
        "name": ("name", "vehicle", "vehicle name"),
        "model": ("model", "model code", "sales code", "npl"),
        "description": ("description",),
        "options": ("options", "option"),
        "prod_period": (
            "prod period",
            "production period",
            "prod range",
            "manufactured",
        ),
        "catalog_code": ("catalog", "catalog code"),
        "vehicle_external_id": ("vehicle id", "vid", "uid"),
    }

    def adapt(self, metadata: dict[str, str]) -> VehicleRecord | None:
        normalized = {key.strip().lower().rstrip(":"): value for key, value in metadata.items()}
        values = {field: self._find(normalized, aliases) for field, aliases in self.aliases.items()}
        if not any((values["brand"], values["name"], values["model"], values["prod_period"])):
            return None
        period = parse_unambiguous_range(values["prod_period"])
        brand = values["brand"]
        return VehicleRecord(
            catalog_brand=brand,
            brand_raw=brand,
            brand_normalized=brand.upper() if brand else None,
            name_raw=values["name"],
            model_raw=values["model"],
            description_raw=values["description"],
            options_raw=values["options"],
            prod_period_raw=values["prod_period"],
            production_from=period.start,
            production_to=period.end,
            production_precision=period.precision,
            catalog_code=values["catalog_code"],
            vehicle_external_id=values["vehicle_external_id"],
            metadata=metadata,
        )

    @staticmethod
    def _find(metadata: dict[str, str], aliases: tuple[str, ...]) -> str | None:
        return next((metadata[alias] for alias in aliases if alias in metadata), None)
