from partsouq_crawler.parsers.brands.base import BaseBrandAdapter


class AudiBrandAdapter(BaseBrandAdapter):
    name = "audi"
    aliases = {
        **BaseBrandAdapter.aliases,
        "model": ("model", "model designation", "sales type"),
        "prod_period": ("prod period", "model year", "production period"),
    }
