"""vlbaweather: the weather at the ten VLBA antenna sites, as a static page for people.

Built on the ``weather`` package of local-weather-awareness (copied into ``weather/``) for the
NWS products, alerts, radar maps and sun times, with these additions: the NWS raw gridpoint
forecast, GOES GLM lightning, NWS radar composites for the sites outside the MRMS grid, and
a GOOD / BAD verdict per antenna with the weather cuts of ttustatus as the boundary.
"""
__version__ = "0.1.0"
