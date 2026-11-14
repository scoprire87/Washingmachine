"""The options menu and forms have their labels."""
from homeassistant.helpers.translation import async_get_translations

from custom_components.appliance_cycle.const import DOMAIN


async def test_options_translations(hass):
    for language, label in (("en", "Rename a program"), ("it", "Rinomina un programma")):
        texts = await async_get_translations(hass, language, "options", {DOMAIN})
        prefix = f"component.{DOMAIN}.options.step"
        assert texts[f"{prefix}.init.menu_options.rename_program"] == label
        for step in ("settings", "rename_program", "set_last_program", "import_file",
                     "delete_program", "load_presets"):
            assert f"{prefix}.{step}.title" in texts, (language, step)
        assert f"{prefix}.settings.data.notify_services" in texts
