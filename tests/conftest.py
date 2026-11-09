"""Load the Home Assistant test plugin for the smoke tests.

The mocked policy tests do not request the ``hass`` fixture.
"""

pytest_plugins = "pytest_homeassistant_custom_component"
