from partsouq_crawler.parsers.brands.base import BaseBrandAdapter


class RenaultBrandAdapter(BaseBrandAdapter):
    name = "renault"
    aliases = {
        **BaseBrandAdapter.aliases,
        "model": ("model", "vehicle type", "type", "model code"),
        "options": ("options", "engine", "gearbox"),
    }
