from partsouq_crawler.parsers.brands.base import BaseBrandAdapter


class ToyotaBrandAdapter(BaseBrandAdapter):
    name = "toyota"
    aliases = {
        **BaseBrandAdapter.aliases,
        "model": ("model", "model code", "frame", "frame code"),
        "description": ("description", "grade"),
        "options": ("options", "engine", "transmission", "destination"),
    }
